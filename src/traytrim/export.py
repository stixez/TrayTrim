"""Build the export: one package with exactly the planned resources (stored bytes copied
unchanged), the household's tray files, a README and a report, zipped. Then verify it, and
optionally prepare an in-game test kit.

Files: everything is built in a hidden staging folder inside the chosen output folder and only
moved into place, as the zip and the test kit folder, once all of it succeeded; a failure or a
cancel leaves nothing behind. The only other file written is the export history in TrayTrim's
config folder. Output folders inside the Sims 4 user folder, the game or Sims 4 Studio's folder
are refused (see forbidden_reason).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from traytrim import __version__, dbpf, restypes
from traytrim.dbpf import PackageWriter
from traytrim.index import ResourceIndex
from traytrim.paths import config_dir, documents_candidates
from traytrim.plan import Cancelled, Plan, open_packages
from traytrim.resolve import GameplayMod, LocatedRef, summarize
from traytrim.tray import HouseholdContents, TrayItem
from traytrim.units import human_size
from traytrim.verify import Verification, verify_export

# progress(step, done, total): step is "package", "verify" or "zip".
Progress = Callable[[str, int, int], None]

TEST_BACKUP = "Mods_TrayTrim_backup"


class ExportError(Exception):
    """An export that can't go ahead. The message is meant to be shown to the user."""


@dataclass
class ExportOptions:
    out_dir: Path
    base_name: str                 # file names start with this ("Camille_Moreau")
    test_kit: bool = True
    user_dir: Path | None = None   # the Sims 4 folder, for the test kit's instructions
    game_dir: Path | None = None   # refused as an output location, like user_dir


@dataclass
class ExportResult:
    zip_path: Path
    package_name: str
    package_size: int
    zip_size: int
    verification: Verification
    test_kit: Path | None
    gameplay_mods: list[GameplayMod] = field(default_factory=list)


def safe_name(text: str, fallback: str = "Household") -> str:
    """A file-name-safe version of a Sim or household name: "Camille Moreau" -> Camille_Moreau."""
    cleaned = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned[:60] or fallback


def default_base_name(item: TrayItem, contents: HouseholdContents, sim_ids: set[int]) -> str:
    if contents.kind != "household":
        return safe_name(item.name, "Lot" if contents.kind == "lot" else "Room")
    chosen = [s for s in contents.sims if s.sim.sim_id in sim_ids]
    if len(chosen) == 1 and chosen[0].sim.name:
        return safe_name(chosen[0].sim.name)
    return safe_name(item.name)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def forbidden_reason(out_dir: Path, user_dir: Path | None, game_dir: Path | None) -> str | None:
    """Why exports can't go to out_dir, or None. TrayTrim never writes where the game, Sims 4
    Studio or the user's mods live: an export there could be loaded by the game as CC."""
    if user_dir is not None and _inside(out_dir, user_dir):
        return ("That folder is inside your Sims 4 folder, which TrayTrim never writes to. "
                "Pick another folder, for example your Desktop.")
    if game_dir is not None and _inside(out_dir, game_dir):
        return "That folder is inside the game's install folder. Pick another folder."
    for docs in documents_candidates():
        if _inside(out_dir, docs / "Sims 4 Studio"):
            return "That folder belongs to Sims 4 Studio. Pick another folder."
    return None


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"Too many exports named {path.name} in {path.parent}")


def export_household(item: TrayItem, contents: HouseholdContents, refs: list[LocatedRef],
                     plan: Plan, gameplay: list[GameplayMod], sim_ids: set[int],
                     options: ExportOptions, index: ResourceIndex,
                     progress: Progress | None = None,
                     cancel: threading.Event | None = None) -> ExportResult:
    out_dir = options.out_dir
    reason = forbidden_reason(out_dir, options.user_dir, options.game_dir)
    if reason:
        raise ExportError(reason)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = options.base_name
    package_name = f"{base}_TrayTrim.package"
    stage = Path(tempfile.mkdtemp(prefix=".traytrim-", dir=out_dir))
    try:
        content = stage / "zip"
        package_path = content / "Mods" / package_name
        package_path.parent.mkdir(parents=True)
        _write_package(package_path, plan, progress, cancel)

        verification = verify_export(
            package_path, plan, refs, index,
            (lambda d, t: progress("verify", d, t)) if progress else None, cancel)

        tray_dir = content / "Tray"
        tray_dir.mkdir()
        for f in item.files:
            shutil.copy2(f, tray_dir / f.name)

        summary = summarize(refs)
        report = _report(item, contents, sim_ids, plan, summary, gameplay, verification,
                         package_name)
        (content / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (content / "README.txt").write_text(
            _readme(item, contents, sim_ids, plan, summary, gameplay, verification,
                    package_name), encoding="utf-8")

        staged_kit = None
        if options.test_kit:
            staged_kit = stage / "kit"
            _test_kit(staged_kit, package_path, tray_dir, options.user_dir, item, contents,
                      sim_ids)

        staged_zip = stage / "export.zip"
        _write_zip(content, staged_zip, progress, cancel)
        if cancel is not None and cancel.is_set():
            raise Cancelled

        # Everything is built; move it into place. Undo the first move if the second fails.
        zip_path = _unique(out_dir / f"{base}_TrayTrim.zip")
        kit = _unique(out_dir / f"{base}_TrayTrim_test") if staged_kit else None
        os.replace(staged_zip, zip_path)
        if staged_kit is not None:
            try:
                os.replace(staged_kit, kit)
            except OSError:
                zip_path.unlink(missing_ok=True)
                raise
        result = ExportResult(zip_path, package_name, package_path.stat().st_size,
                              zip_path.stat().st_size, verification, kit, gameplay)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    _add_to_history(item, result)
    return result


def _write_package(path: Path, plan: Plan, progress: Progress | None,
                   cancel: threading.Event | None) -> None:
    # Grouped by source package so each one is opened once; keys sorted within it.
    resources = sorted(plan.resources.values(), key=lambda r: (str(r.copy.path), r.key))
    with open_packages() as sources, PackageWriter(path) as writer:
        for done, res in enumerate(resources, 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled
            entry = res.copy.entry
            # The index may be older than the package: re-check the entry against the
            # package's own index before copying from it.
            try:
                current = sources.get(res.copy.path).find(res.key)
            except (dbpf.DbpfError, OSError) as e:
                raise ExportError(f"Couldn't read {res.copy.rel}: {e}") from None
            if current is None or (current.offset, current.file_size, current.compression) != (
                    entry.offset, entry.file_size, entry.compression):
                raise ExportError(
                    f"{res.copy.rel} changed since TrayTrim indexed it. Let the index refresh "
                    "(it does at start, or choose Try again in the status bar), then export "
                    "again.")
            writer.add_raw(res.key, sources.read_raw(res.copy), entry.compression,
                           entry.mem_size)
            if progress and (done % 20 == 0 or done == len(resources)):
                progress("package", done, len(resources))


def _write_zip(content: Path, zip_path: Path, progress: Progress | None,
               cancel: threading.Event | None) -> None:
    files = sorted(p for p in content.rglob("*") if p.is_file())
    with zipfile.ZipFile(zip_path, "w") as z:
        for done, f in enumerate(files, 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled
            # Package payloads are already compressed; text files are worth deflating.
            method = zipfile.ZIP_STORED if f.suffix == ".package" else zipfile.ZIP_DEFLATED
            z.write(f, f.relative_to(content).as_posix(), compress_type=method)
            if progress:
                progress("zip", done, len(files))


def _sim_names(contents: HouseholdContents, sim_ids: set[int]) -> list[str]:
    return [s.sim.name or "Unnamed Sim" for s in contents.sims if s.sim.sim_id in sim_ids]


def _packs_text(packs: list[str]) -> str:
    return ", ".join("base game" if p == "base" else p for p in packs) or "none"


def _report(item, contents, sim_ids, plan: Plan, summary, gameplay, verification,
            package_name) -> dict:
    return {
        "traytrim": __version__,
        "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "household": item.name,
        "tray_id": item.id_text,
        "sims": _sim_names(contents, sim_ids),
        "package": package_name,
        "package_bytes": plan.export_size,
        "whole_packages_bytes": plan.whole_size,
        "files": [{"key": str(r.key), "type": restypes.name(r.key.type), "source": r.copy.rel,
                   "bytes": r.size, "via": r.via}
                  for r in sorted(plan.resources.values(), key=lambda r: r.key)],
        "left_out": sorted(f"{r.kind.value}:{r.instance:016X}" for r in plan.excluded),
        "game_packs": summary.packs,
        "conflicts": [{"key": str(c.key), "used": c.copy.rel,
                       "copies": [h.rel for h in c.copies]} for c in plan.conflicts],
        "default_replacements": [{"key": str(d.key), "source": d.copy.rel}
                                 for d in plan.default_replacements],
        "missing_files": [{"key": str(k), "needed_by": sorted(str(p) for p in parents)}
                          for k, parents in plan.missing.items()],
        "not_installed_items": [f"{r.ref.kind.value}:{r.ref.instance:016X}"
                                for r in summary.missing],
        "gameplay_mods": [m.rel for m in gameplay],
        "verification": [{"check": c.name, "passed": c.passed, "detail": c.detail,
                          "problems": c.problems} for c in verification.checks],
    }


def _readme(item, contents, sim_ids, plan: Plan, summary, gameplay, verification,
            package_name) -> str:
    kind = contents.kind
    what = {"household": "these Sims use", "lot": "this lot uses",
            "room": "this room uses"}[kind]
    lines = [f"{item.name}: exported with TrayTrim {__version__}", ""]
    if kind == "household":
        lines += [f"Sims: {', '.join(_sim_names(contents, sim_ids))}", ""]
    lines += [
        "How to install",
        "  1. Copy the files in Tray/ into Documents/Electronic Arts/The Sims 4/Tray",
        f"  2. Copy Mods/{package_name} into Documents/Electronic Arts/The Sims 4/Mods",
        f"  3. Start the game and find the {kind} in the Library.",
        "",
        "What's in the package",
        f"  Only the custom content {what}: {len(plan.resources):,} files "
        f"({human_size(plan.export_size)}), taken from {len(plan.source_packages):,} CC "
        "packages.",
        f"  Game packs it uses content from: {_packs_text(summary.packs)}.",
    ]
    if plan.default_replacements:
        lines += ["",
                  "Default replacements included",
                  "  These change an item of the game itself, for every Sim:"]
        lines += [f"    {d.copy.rel}" for d in plan.default_replacements]
    if gameplay:
        lines += ["",
                  "Also needed, not included",
                  "  It uses traits, moods or other gameplay content from these mods.",
                  "  They can't be trimmed; install the whole mods from their creators:"]
        lines += [f"    {m.rel}" for m in gameplay]
    if summary.missing or plan.missing:
        lines += ["",
                  "Missing before export",
                  f"  {len(summary.missing)} items and {len(plan.missing)} files the CC "
                  "needs weren't installed when this was exported, so they're not included."]
    lines += ["",
              "Checked after export"]
    lines += [f"  [{'ok' if c.passed else 'FAILED'}] {c.name}" for c in verification.checks]
    lines += ["",
              "Exports are meant for your own backups and transfers, or for CC you have the",
              "rights to share. Many creators don't allow re-uploading their CC.",
              ""]
    return "\n".join(lines)


def _test_kit(kit: Path, package_path: Path, tray_dir: Path, user_dir: Path | None,
              item: TrayItem, contents: HouseholdContents, sim_ids: set[int]) -> None:
    (kit / "Mods").mkdir(parents=True)
    shutil.copy2(package_path, kit / "Mods" / package_path.name)
    shutil.copytree(tray_dir, kit / "Tray")
    sims = user_dir or Path("Documents/Electronic Arts/The Sims 4")
    names = ", ".join(_sim_names(contents, sim_ids))
    steps = [
        f"In-game test for {item.name}" + (f" ({names})" if contents.kind == "household" else ""),
        "",
        "This checks the export in the game with nothing but the exported package installed.",
        "TrayTrim never touches your Mods folder; these steps (or the two .bat files on",
        "Windows) do, and they put everything back.",
        "",
        "1. Close The Sims 4.",
        f"2. Rename your Mods folder ({sims / 'Mods'}) to {TEST_BACKUP}.",
        "3. Make a new, empty Mods folder there, and copy into it:",
        f"     - {package_path.name} from this kit's Mods folder",
        f"     - Resource.cfg from {TEST_BACKUP}",
        (f"4. Start the game, load the {contents.kind} from the Library and compare it with how "
         "it looks"),
        f"   with all your mods. The {contents.kind} is already in your Tray.",
        "5. Close the game. Delete the test Mods folder and rename "
        f"{TEST_BACKUP} back to Mods.",
        "",
        "On Windows, 1_start_test.bat does steps 2 and 3, and 2_end_test.bat does step 5.",
        "Both refuse to do anything while the game is running, and 2_end_test.bat only",
        "touches the Mods folder if it holds nothing but the two test files.",
        "",
    ]
    (kit / "HOW_TO_TEST.txt").write_text("\n".join(steps), encoding="utf-8")
    if sys.platform == "win32":
        _write_bats(kit, Path(sims), package_path.name)


_RUNNING_CHECK = """tasklist /FI "IMAGENAME eq TS4_x64.exe" | find /I "TS4_x64.exe" >nul && goto running
tasklist /FI "IMAGENAME eq TS4_DX9_x64.exe" | find /I "TS4_DX9_x64.exe" >nul && goto running
"""


def _write_bats(kit: Path, sims: Path, package: str) -> None:
    head = f"""@echo off
chcp 65001 >nul
setlocal
set "SIMS={sims}"
set "MODS=%SIMS%\\Mods"
set "BACKUP=%SIMS%\\{TEST_BACKUP}"
set "PKG={package}"
{_RUNNING_CHECK}"""
    start = head + f"""if exist "%BACKUP%\\" (
  echo A test is already set up: {TEST_BACKUP} exists. Run 2_end_test.bat first.
  goto end
)
if not exist "%MODS%\\" (
  echo No Mods folder in "%SIMS%". Nothing was changed.
  goto end
)
if not exist "%~dp0Mods\\%PKG%" (
  echo This kit's package is missing: "%~dp0Mods\\%PKG%". Nothing was changed.
  goto end
)
ren "%MODS%" {TEST_BACKUP} || (
  echo Couldn't rename the Mods folder; is it open somewhere? Nothing was changed.
  goto end
)
mkdir "%MODS%" || goto undo
if exist "%BACKUP%\\Resource.cfg" (
  copy "%BACKUP%\\Resource.cfg" "%MODS%\\" >nul || goto undo
)
copy "%~dp0Mods\\%PKG%" "%MODS%\\" >nul || goto undo
echo The test Mods folder is ready. Start the game and load the household in Create a Sim.
echo When you're done, close the game and run 2_end_test.bat.
goto end
:undo
rmdir /S /Q "%MODS%" 2>nul
ren "%BACKUP%" Mods && (
  echo Setting up the test failed, so your Mods folder was put back as it was.
) || (
  echo Setting up the test failed, and your Mods folder could not be renamed back.
  echo It is safe in "%BACKUP%": rename that folder to Mods.
)
goto end
:running
echo The Sims 4 is running. Close it first; nothing was changed.
:end
pause
"""
    end = head + f"""if not exist "%BACKUP%\\" (
  echo No test is set up: there is no {TEST_BACKUP} folder. Nothing was changed.
  goto end
)
rem Only touch Mods if it holds nothing but the two files 1_start_test.bat put there.
set "OTHER="
for /f "delims=" %%F in ('dir /b /a "%MODS%" 2^>nul') do (
  if /I not "%%F"=="%PKG%" if /I not "%%F"=="Resource.cfg" set "OTHER=1"
)
if defined OTHER (
  echo "%MODS%" holds files the test didn't put there, so nothing was changed.
  echo If this is your real Mods folder, your mods are fine; delete {TEST_BACKUP} only
  echo after checking it holds nothing you need.
  goto end
)
if exist "%MODS%\\%PKG%" del /Q "%MODS%\\%PKG%"
if exist "%MODS%\\Resource.cfg" del /Q "%MODS%\\Resource.cfg"
rmdir "%MODS%" 2>nul
if exist "%MODS%\\" (
  echo Couldn't remove the test Mods folder; is it open somewhere? Your mods are safe in
  echo "%BACKUP%". Close other programs and run this again.
  goto end
)
ren "%BACKUP%" Mods && (
  echo Your Mods folder is back.
) || (
  echo Couldn't rename "%BACKUP%" back to Mods; is it open somewhere?
  echo Your mods are safe in it. Close other programs and run this again.
)
goto end
:running
echo The Sims 4 is running. Close it first; nothing was changed.
:end
pause
"""
    for name, text in (("1_start_test.bat", start), ("2_end_test.bat", end)):
        (kit / name).write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")


# History


def history_path() -> Path:
    return config_dir() / "history.json"


def load_history() -> list[dict]:
    try:
        data = json.loads(history_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _add_to_history(item: TrayItem, result: ExportResult) -> None:
    entries = load_history()
    entries.insert(0, {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "household": item.name,
        "tray_id": item.id_text,
        "zip": str(result.zip_path),
        "bytes": result.zip_size,
        "verified": result.verification.ok,
        "test_kit": str(result.test_kit) if result.test_kit else None,
    })
    try:
        path = history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries[:200], indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # the export itself is done; a lost history line isn't worth failing it
