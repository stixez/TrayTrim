"""Build the export: one package per CC item with exactly the resources it needs (stored bytes
copied unchanged), named the way creators name theirs ("Nivelle_yfTop_HalterBackZip_Black
.package"), the tray files of the household, lot or room (or of several, sharing the packages),
and a README that says how to install it, zipped. Then verify it, and optionally prepare an
in-game test kit.

Each resource goes into the package of the CC item that needs it; what several items need (a
mesh two recolors share) goes into one shared package (package_groups). So every file is in the
export once, in one package only, and deleting one item's package never breaks another item.

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
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections.abc import Callable

from traytrim import __version__, dbpf
from traytrim.dbpf import PackageWriter, ResourceKey
from traytrim.index import ResourceIndex
from traytrim.names import ItemName, cas_file_stem, item_names
from traytrim.paths import config_dir, documents_candidates, find_user_dir, user_dirs_in
from traytrim.plan import Cancelled, Plan, PlannedResource, open_packages
from traytrim.resolve import GameplayMod, LocatedRef, Where, summarize
from traytrim.tray import HouseholdContents, TrayItem, TrayRef
from traytrim.verify import Verification, packages_digest, verify_export

# progress(step, done, total): step is "package", "verify" or "zip".
Progress = Callable[[str, int, int], None]

TEST_BACKUP = "Mods_TrayTrim_backup"
SIMS_FOLDER = "Documents/Electronic Arts/The Sims 4"


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
    packages: list[str]            # file names in Mods/, one per CC item
    package_size: int              # all of them together
    zip_size: int
    verification: Verification
    test_kit: Path | None
    gameplay_mods: list[GameplayMod] = field(default_factory=list)


@dataclass
class Part:
    """One tray item in an export. An export holds one, or several sharing their packages."""
    item: TrayItem
    contents: HouseholdContents
    sim_ids: set[int]

    @property
    def kind(self) -> str:
        return self.contents.kind


def part_name(part: Part) -> str:
    return part.item.name or f"Unnamed {part.kind}"


def bundle_title(parts: list[Part]) -> str:
    """"Seaside", "Seaside and Moreau", "Seaside, Moreau and Nursery", "Seaside and 3 more"."""
    names = [part_name(p) for p in parts]
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) <= 3:
        return f"{', '.join(names[:-1])} and {names[-1]}"
    return f"{names[0]} and {len(names) - 1} more"


def safe_name(text: str, fallback: str = "Household", limit: int = 60) -> str:
    """A file-name-safe version of a Sim or household name: "Camille Moreau" -> Camille_Moreau."""
    cleaned = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned[:limit].strip("_") or fallback


# Short enough that the game's folder, a subfolder or two in Mods and the name stay within
# Windows' 260-character paths.
STEM_LIMIT = 64


# Ends every item's package name. The names follow the creators' own, so without it a trimmed
# package could share its name with the creator's whole package in the recipient's Mods, and
# copying it in would replace that (and every other item in it). It also keeps names like
# "Aux" or "Con", which Windows opens as a device, from being a whole file name.
SUFFIX = "_TT"


def package_stem(name: ItemName | None, fallback: str) -> str:
    """The file name, without .package, of one CC item's package. A CAS part keeps the name
    its creator gave it, without the date stamp ("Nivelle_yfTop_HalterBackZip_SolidBlack_TT");
    objects and build items use their catalog name ("Driftwood_Rattan_Chair_TT")."""
    text = ""
    if name is not None:
        if name.creator and name.raw:
            text = cas_file_stem(name.raw)
        else:
            text = name.title  # no title (only numbers): named like an item without a name
    stem = (safe_name(text, "", limit=STEM_LIMIT)
            or safe_name(fallback, "Item", limit=STEM_LIMIT))
    return stem + SUFFIX


SHARED = None  # package_groups' key for the files several CC items need


def package_groups(plan: Plan) -> dict[TrayRef | None, list[PlannedResource]]:
    """Which package each planned file goes into: the one of the CC item that needs it, or
    (SHARED) a package of its own when several items need it, like a mesh two recolors use.
    Then each item's package works without the other items' packages: deleting one CC item's
    package never breaks another."""
    children: dict[ResourceKey, set[ResourceKey]] = {}
    for res in plan.resources.values():
        for parent in res.parents:
            children.setdefault(parent, set()).add(res.key)
    needed_by: dict[ResourceKey, set[TrayRef]] = {}
    for ref in plan.refs:
        if ref.where is not Where.CC or ref.ref in plan.excluded:
            continue
        stack = [plan.aliases.get(k, k) for k in ref.keys]
        seen: set[ResourceKey] = set()
        while stack:
            key = stack.pop()
            if key in seen or key not in plan.resources:
                continue
            seen.add(key)
            needed_by.setdefault(key, set()).add(ref.ref)
            stack.extend(children.get(key, ()))
    groups: dict[TrayRef | None, list[PlannedResource]] = {}
    for res in plan.resources.values():
        who = needed_by.get(res.key) or {res.owner}
        groups.setdefault(next(iter(who)) if len(who) == 1 else SHARED, []).append(res)
    return groups


def package_names(groups: dict[TrayRef | None, list[PlannedResource]],
                  names: dict[TrayRef, ItemName], shared: str = "Shared"
                  ) -> dict[TrayRef | None, str]:
    """Each package's file name, unique ignoring case (as Windows and macOS compare file
    names, and as Check a zip unpacks them: casefold, so "Straße" and "Strasse" are one). An
    item without a name is named after the package it comes from and its id; the shared
    package after the export (`shared`)."""
    out: dict[TrayRef | None, str] = {}
    taken: set[str] = set()

    def unique(stem: str) -> str:
        candidate, n = stem, 1
        while candidate.casefold() in taken:
            n += 1
            candidate = f"{stem}_{n}"
        taken.add(candidate.casefold())
        return f"{candidate}.package"

    for owner in sorted((o for o in groups if o is not SHARED),
                        key=lambda o: (o.kind.value, o.instance)):
        first = min(groups[owner], key=lambda r: r.key)
        fallback = f"{Path(first.copy.rel).stem}_{owner.instance:016X}"
        out[owner] = unique(package_stem(names.get(owner), fallback))
    if SHARED in groups:
        out[SHARED] = unique(safe_name(shared, "Shared", limit=STEM_LIMIT))
    return out


def default_base_name(item: TrayItem, contents: HouseholdContents, sim_ids: set[int]) -> str:
    if contents.kind != "household":
        return safe_name(item.name, "Lot" if contents.kind == "lot" else "Room")
    chosen = [s for s in contents.sims if s.sim.sim_id in sim_ids]
    if len(chosen) == 1 and chosen[0].sim.name:
        return safe_name(chosen[0].sim.name)
    return safe_name(item.name)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _inside(path: Path, folder: Path) -> bool:
    """Whether path (already resolved) is folder or inside it, following links in folder."""
    try:
        path.relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


# The folders of a Sims 4 folder the game loads from or writes to. Each is checked on its own
# as well: Mods or Tray can be a link (a junction, made by a mod manager) to a folder elsewhere.
_GAME_FOLDERS = ("Mods", "Tray", "saves")


def forbidden_reason(out_dir: Path, user_dir: Path | None, game_dir: Path | None) -> str | None:
    """Why exports can't go to out_dir, or None. TrayTrim never writes where the game, Sims 4
    Studio or the user's mods live: an export there could be loaded by the game as CC. Every
    Sims 4 folder found counts (one per game language and Documents folder), not only the one
    in use."""
    out = _resolved(out_dir)
    docs = documents_candidates()
    for sims in dict.fromkeys(([user_dir] if user_dir is not None else []) + user_dirs_in(docs)):
        if any(_inside(out, folder) for folder in (sims, *(sims / f for f in _GAME_FOLDERS))):
            return ("That folder is inside your Sims 4 folder, which TrayTrim never writes to. "
                    "Pick another folder, for example your Desktop.")
    if game_dir is not None and _inside(out, game_dir):
        return "That folder is inside the game's install folder. Pick another folder."
    if any(_inside(out, d / "Sims 4 Studio") for d in docs):
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
    """Export one household, lot or room."""
    return export_parts([Part(item, contents, sim_ids)], refs, plan, gameplay, options, index,
                        progress, cancel)


def export_item(ref: LocatedRef, name: str, plan: Plan, options: ExportOptions,
                index: ResourceIndex, progress: Progress | None = None,
                cancel: threading.Event | None = None) -> ExportResult:
    """Export one CC item on its own: a package with the item and everything it needs, no tray
    files and no test kit."""
    return export_parts([], [ref], plan, [], replace(options, test_kit=False), index, progress,
                        cancel, item_name=name)


def export_parts(parts: list[Part], refs: list[LocatedRef], plan: Plan,
                 gameplay: list[GameplayMod], options: ExportOptions, index: ResourceIndex,
                 progress: Progress | None = None,
                 cancel: threading.Event | None = None, item_name: str = "") -> ExportResult:
    """Export tray items whose CC is planned together (`refs` and `plan` cover all of them)
    into one zip, one package per CC item. With no parts, `refs` is one CC item named
    `item_name`."""
    out_dir = options.out_dir
    reason = forbidden_reason(out_dir, options.user_dir, options.game_dir)
    if reason:
        raise ExportError(reason)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = options.base_name
    stage = Path(tempfile.mkdtemp(prefix=".traytrim-", dir=out_dir))
    try:
        content = stage / "zip"
        mods_dir = content / "Mods"
        mods_dir.mkdir(parents=True)
        names = item_names(plan.refs, cancel)
        if cancel is not None and cancel.is_set():
            raise Cancelled
        if not parts and item_name and refs and refs[0].ref not in names:
            names[refs[0].ref] = ItemName(item_name, "", item_name)
        groups = package_groups(plan)
        file_names = package_names(groups, names, f"{base}_Shared")
        package_paths = _write_packages(mods_dir, groups, file_names, progress, cancel)
        shared = file_names.get(SHARED)

        verification = verify_export(
            package_paths, plan, refs, index,
            (lambda d, t: progress("verify", d, t)) if progress else None, cancel,
            from_tray=bool(parts))

        tray_dir = content / "Tray"
        if parts:
            tray_dir.mkdir()
        for part in parts:
            for f in part.item.files:
                shutil.copy2(f, tray_dir / f.name)

        packs = summarize(refs).packs if parts else _dependency_packs(plan)
        readme = (_readme(parts, gameplay, packs, shared) if parts
                  else _item_readme(item_name, packs))
        (content / "README.txt").write_text(readme, encoding="utf-8")

        staged_kit = None
        if options.test_kit and parts:
            staged_kit = stage / "kit"
            _test_kit(staged_kit, mods_dir, tray_dir, options.user_dir, parts)

        staged_zip = stage / "export.zip"
        _write_zip(content, staged_zip, progress, cancel,
                   _zip_comment(parts, item_name, packs, gameplay, package_paths))
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
        result = ExportResult(zip_path, [p.name for p in package_paths],
                              sum(p.stat().st_size for p in package_paths),
                              zip_path.stat().st_size, verification, kit, gameplay)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    _add_to_history(parts, result, item_name)
    return result


def _write_packages(mods_dir: Path, groups: dict[TrayRef | None, list[PlannedResource]],
                    file_names: dict[TrayRef | None, str], progress: Progress | None,
                    cancel: threading.Event | None) -> list[Path]:
    """The packages of package_groups() in mods_dir, keys sorted within each; returns their
    paths, sorted by name. Written in the order of the packages they're copied from, so a big
    merge several items come from stays open instead of being opened again and again."""
    total, done = sum(len(r) for r in groups.values()), 0
    order = sorted(groups, key=lambda g: min(str(r.copy.path) for r in groups[g]))
    paths: list[Path] = []
    with open_packages() as sources:
        for group in order:
            resources = groups[group]
            path = mods_dir / file_names[group]
            with PackageWriter(path) as writer:
                for res in sorted(resources, key=lambda r: r.key):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled
                    _copy_resource(sources, writer, res)
                    done += 1
                    if progress and (done % 20 == 0 or done == total):
                        progress("package", done, total)
            paths.append(path)
    return sorted(paths, key=lambda p: p.name.lower())


def _copy_resource(sources, writer: PackageWriter, res: PlannedResource) -> None:
    entry = res.copy.entry
    # The index may be older than the package: re-check the entry against the package's own
    # index before copying from it.
    try:
        current = sources.get(res.copy.path).find(res.key)
    except (dbpf.DbpfError, OSError) as e:
        raise ExportError(f"Couldn't read {res.copy.rel}: {e}") from None
    if current is None or (current.offset, current.file_size, current.compression) != (
            entry.offset, entry.file_size, entry.compression):
        raise ExportError(
            f"{res.copy.rel} changed since TrayTrim indexed it. Let the index refresh (it does "
            "at start, or choose Try again in the status bar), then export again.")
    writer.add_raw(res.key, sources.read_raw(res.copy), entry.compression, entry.mem_size)


def _zip_comment(parts: list[Part], item_name: str, packs: list[str],
                 gameplay: list[GameplayMod], packages: list[Path]) -> bytes:
    """What Check a zip shows about an export that the files in it can't tell (read back with
    verify.read_zip_comment): its name, the game packs it needs, the gameplay mods it can't
    include, and its packages with how many files each holds (verify.record_check compares
    them). Kept in the zip's comment, not in a file, so the recipient unpacks nothing but the
    export. A zip comment holds at most 64 KB: when the list of packages doesn't fit (more
    than a thousand or so), a digest of it does, which still tells any change apart."""
    files = {path.name: len(dbpf.read_index(path)[1]) for path in packages}
    meta = {"traytrim": __version__, "name": bundle_title(parts) if parts else item_name,
            "kind": _kind(parts) if parts else "item", "game_packs": packs,
            "gameplay_mods": [m.rel for m in gameplay], "packages": files}
    data = _json_bytes(meta)
    if len(data) > 0xFFFF:
        meta["packages_digest"] = packages_digest(files)
        del meta["packages"]
        data = _json_bytes(meta)
    while len(data) > 0xFFFF and meta["gameplay_mods"]:  # hundreds of long mod paths
        meta["gameplay_mods"] = meta["gameplay_mods"][:len(meta["gameplay_mods"]) // 2]
        data = _json_bytes(meta)
    return data if len(data) <= 0xFFFF else b""


def _json_bytes(meta: dict) -> bytes:
    # UTF-8 as it is: escaped, a name in another script takes six bytes a letter.
    return json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode()


def _dependency_packs(plan: Plan) -> list[str]:
    """Game packs the planned CC takes meshes or textures from, base game last."""
    packs = set().union(*plan.from_game.values()) if plan.from_game else set()
    return sorted(packs - {"base"}) + (["base"] if "base" in packs else [])


def _write_zip(content: Path, zip_path: Path, progress: Progress | None,
               cancel: threading.Event | None, comment: bytes = b"") -> None:
    files = sorted(p for p in content.rglob("*") if p.is_file())
    with zipfile.ZipFile(zip_path, "w") as z:
        z.comment = comment
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


def _kind(parts: list[Part]) -> str:
    return parts[0].kind if len(parts) == 1 else "bundle"


_ENABLE_CC = ("In the game, turn on Game Options > Other > Enable Custom Content and Mods, "
              "then restart the game.")


def _packs_line(packs: list[str]) -> list[str]:
    needed = [p for p in packs if p != "base"]
    return [f"It needs these game packs: {', '.join(needed)}.", ""] if needed else []


def _item_readme(name: str, packs: list[str]) -> str:
    lines = [f"How to install {name}", "", *_packs_line(packs),
             f"1. Copy every .package file in Mods/ into {SIMS_FOLDER}/Mods",
             f"2. {_ENABLE_CC}", ""]
    return "\n".join(lines)


def _readme(parts: list[Part], gameplay: list[GameplayMod], packs: list[str],
            shared: str | None = None) -> str:
    kind = _kind(parts)
    find = "them" if kind == "bundle" else f"the {kind}"
    lines = [f"How to install {bundle_title(parts)}", "", *_packs_line(packs),
             f"1. Copy every file in Tray/ into {SIMS_FOLDER}/Tray",
             f"2. Copy every .package file in Mods/ into {SIMS_FOLDER}/Mods"
             + (f" ({shared} holds what several items share; keep it with them)"
                if shared else ""),
             f"3. {_ENABLE_CC}",
             f"4. Find {find} in the Library. Tick Include Custom Content in its filters if "
             f"{'they don' if kind == 'bundle' else 'it doesn'}'t show up."]
    if gameplay:
        lines += ["5. Also install these mods from their creators (they can't be trimmed, "
                  "so they're not included):"]
        lines += [f"     {m.rel}" for m in gameplay]
    lines.append("")
    return "\n".join(lines)


def _test_kit(kit: Path, mods_dir: Path, tray_dir: Path, user_dir: Path | None,
              parts: list[Part]) -> None:
    kit.mkdir()
    shutil.copytree(mods_dir, kit / "Mods")
    shutil.copytree(tray_dir, kit / "Tray")
    sims = user_dir or find_user_dir()
    scripts = sys.platform == "win32" and sims is not None  # they need the real folder
    kind = _kind(parts)
    one = parts[0]
    names = ", ".join(_sim_names(one.contents, one.sim_ids))
    them, it = ("them", "they're") if kind == "bundle" else (f"the {kind}", f"the {kind} is")
    steps = [
        f"In-game test for {bundle_title(parts)}" + (f" ({names})" if kind == "household"
                                                      else ""),
        "",
        "This checks the export in the game with nothing but the exported packages installed.",
        "TrayTrim never touches your Mods folder; these steps "
        + ("(or the two .bat files here) do," if scripts else "do,"),
        "and they put everything back.",
        "",
        "1. Close The Sims 4.",
        f"2. Rename your Mods folder ({Path(sims or SIMS_FOLDER) / 'Mods'}) to {TEST_BACKUP}.",
        "3. Make a new, empty Mods folder there, and copy into it:",
        "     - every .package file in this kit's Mods folder",
        f"     - Resource.cfg from {TEST_BACKUP}",
        f"4. Start the game, load {them} from the Library and compare with how it looks",
        f"   with all your mods. {it[0].upper() + it[1:]} already in your Tray.",
        "5. Close the game. Delete the test Mods folder and rename "
        f"{TEST_BACKUP} back to Mods.",
        "",
    ]
    if scripts:
        steps += ["1_start_test.bat does steps 2 and 3, and 2_end_test.bat does step 5.",
                  "Both refuse to do anything while the game is running, and 2_end_test.bat only",
                  "touches the Mods folder if it holds nothing but the test files.", ""]
    (kit / "HOW_TO_TEST.txt").write_text("\n".join(steps), encoding="utf-8")
    if scripts:
        _write_bats(kit, sims, kind)


# Windows' own tasklist and find, by full path: with Git or MSYS tools first on PATH, "find"
# is a different program, the check fails and the scripts would go on while the game runs.
_RUNNING_CHECK = """set "SYS=%SystemRoot%\\System32"
"%SYS%\\tasklist.exe" /FI "IMAGENAME eq TS4_x64.exe" | "%SYS%\\find.exe" /I "TS4_x64.exe" >nul && goto running
"%SYS%\\tasklist.exe" /FI "IMAGENAME eq TS4_DX9_x64.exe" | "%SYS%\\find.exe" /I "TS4_DX9_x64.exe" >nul && goto running
"""


def _write_bats(kit: Path, sims: Path, kind: str = "household") -> None:
    load = ("load the household in Create a Sim" if kind == "household"
            else "load them from the Library" if kind == "bundle"
            else f"place the {kind} from the Library")
    head = f"""@echo off
chcp 65001 >nul
setlocal
set "SIMS={str(sims).replace("%", "%%")}"
set "MODS=%SIMS%\\Mods"
set "BACKUP=%SIMS%\\{TEST_BACKUP}"
set "KIT=%~dp0Mods"
{_RUNNING_CHECK}"""
    start = head + f"""if exist "%BACKUP%\\" (
  echo A test is already set up: {TEST_BACKUP} exists. Run 2_end_test.bat first.
  goto end
)
if not exist "%MODS%\\" (
  echo No Mods folder in "%SIMS%". Nothing was changed.
  goto end
)
if not exist "%KIT%\\*.package" (
  echo This kit's packages are missing from "%KIT%". Nothing was changed.
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
copy "%KIT%\\*.package" "%MODS%\\" >nul || goto undo
echo The test Mods folder is ready. Start the game and {load}.
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
rem Only touch Mods if it holds nothing but the files 1_start_test.bat put there.
set "OTHER="
for /f "delims=" %%F in ('dir /b /a "%MODS%" 2^>nul') do (
  if /I not "%%F"=="Resource.cfg" if not exist "%KIT%\\%%F" set "OTHER=1"
)
if defined OTHER (
  echo "%MODS%" holds files the test didn't put there, so nothing was changed.
  echo If this is your real Mods folder, your mods are fine; delete {TEST_BACKUP} only
  echo after checking it holds nothing you need.
  goto end
)
for %%F in ("%KIT%\\*.package") do if exist "%MODS%\\%%~nxF" del /Q "%MODS%\\%%~nxF"
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
    except (OSError, ValueError, RecursionError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def clear_history() -> None:
    """Forget every export in the history (the zips themselves stay where they are)."""
    path = history_path()
    tmp = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text("[]", encoding="utf-8")
    os.replace(tmp, path)


def _add_to_history(parts: list[Part], result: ExportResult, item_name: str = "") -> None:
    entries = load_history()
    entries.insert(0, {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "name": bundle_title(parts) if parts else item_name,
        "kind": _kind(parts) if parts else "item",
        "tray_id": parts[0].item.id_text if len(parts) == 1 else None,
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
