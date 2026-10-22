"""Command-line entry point: `python -m traytrim <command>`. No command opens the app."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

from traytrim import __version__, dbpf, paths, tray
from traytrim.index import GAME, MODS, ResourceIndex, UpdateResult
from traytrim.settings import Settings
from traytrim.units import human_size

_COMPRESSION_NAMES = {
    dbpf.COMP_NONE: "none",
    dbpf.COMP_ZLIB: "zlib",
    dbpf.COMP_REFPACK: "refpack",
    dbpf.COMP_REFPACK_STREAMABLE: "refpack-stream",
}


def cmd_info(args: argparse.Namespace) -> int:
    with dbpf.Package(args.package) as pkg:
        print(f"{pkg.path}")
        print(f"DBPF {pkg.header.major}.{pkg.header.minor}, {len(pkg)} resources, {human_size(pkg.size)}")
        if args.list:
            for e in pkg:
                comp = _COMPRESSION_NAMES.get(e.compression, f"0x{e.compression:04X}")
                print(f"  {e.key}  {comp:<14} {e.file_size:>10} -> {e.mem_size:>10}")
        else:
            by_type = Counter(e.key.type for e in pkg)
            for t, n in by_type.most_common():
                print(f"  {t:08X}  {n:>6}")
        if args.verify:
            failed = 0
            for e in pkg:
                try:
                    pkg.read(e)
                except dbpf.DbpfError as err:
                    failed += 1
                    print(f"  FAILED {err}", file=sys.stderr)
            print(f"Decompressed {len(pkg) - failed} of {len(pkg)} resources.")
            return 1 if failed else 0
    return 0


def _folders(args: argparse.Namespace) -> paths.Sims4Folders:
    settings = Settings.load()
    return paths.resolve_folders(args.sims_folder or settings.user_dir,
                                 args.game_folder or settings.game_dir)


def _update_index(ix: ResourceIndex, folders: paths.Sims4Folders, quiet: bool) -> UpdateResult:
    last = 0.0

    def progress(phase: str, done: int, total: int) -> None:
        nonlocal last
        now = time.monotonic()
        if quiet or not sys.stderr.isatty() or (now - last < 0.2 and done != total):
            return
        last = now
        name = "Mods" if phase == MODS else "game"
        print(f"\rReading {name} packages: {done:,} of {total:,}", end="", file=sys.stderr)
        if done == total:
            print(file=sys.stderr)

    return ix.update(folders.mods, folders.game_dir, progress)


def _print_index_summary(r: UpdateResult, folders: paths.Sims4Folders) -> None:
    if folders.mods is None:
        print("Sims 4 folder not found; pass --sims-folder.")
    else:
        print(f"Mods: {r.mods_packages:,} packages, {r.mods_resources:,} resources ({folders.mods})")
    if folders.game_dir is None:
        print("Game folder not found; pass --game-folder. Without it TrayTrim can't tell EA "
              "content from CC.")
    else:
        print(f"Game: {r.game_packages:,} packages, {r.game_resources:,} resources "
              f"({folders.game_dir})")
    if r.too_deep:
        print(f"{len(r.too_deep):,} packages are more than {r.mods_depth} folders deep in Mods, "
              "so the game doesn't load them and they were skipped.")
    if r.failed:
        print(f"{len(r.failed):,} packages couldn't be read:")
        for path, reason in r.failed:
            print(f"  {path}: {reason}")


def cmd_index(args: argparse.Namespace) -> int:
    folders = _folders(args)
    with ResourceIndex() as ix:
        r = _update_index(ix, folders, args.quiet)
    _print_index_summary(r, folders)
    return 1 if r.failed else 0


def _parse_which(text: str) -> tuple[dbpf.ResourceKey | None, int]:
    """A full type:group:instance key, or just an instance (hex)."""
    if re.fullmatch(r"(0[xX])?[0-9A-Fa-f]{1,16}", text.strip()):
        return None, int(text, 16)
    key = dbpf.ResourceKey.parse(text)
    return key, key.instance


def cmd_which(args: argparse.Namespace) -> int:
    try:
        key, instance = _parse_which(args.key)
    except ValueError as e:
        print(f"traytrim: {e}", file=sys.stderr)
        return 2
    folders = _folders(args)
    with ResourceIndex() as ix:
        if not args.no_update:
            r = _update_index(ix, folders, quiet=True)
            if r.failed:
                print(f"({len(r.failed):,} packages couldn't be read; run `traytrim index` for "
                      "the list.)", file=sys.stderr)
        keys = [key] if key else ix.keys_with_instance(instance)
        found = False
        for k in keys:
            mods, game = ix.mods_hits(k), ix.game_hits(k)
            if not (mods or game):
                continue
            found = True
            print(k)
            for i, h in enumerate(mods):
                comp = _COMPRESSION_NAMES.get(h.entry.compression, f"0x{h.entry.compression:04X}")
                wins = "  (used: loads last)" if len(mods) > 1 and i == len(mods) - 1 else ""
                print(f"  Mods  {h.rel}  {comp}, {human_size(h.entry.mem_size)}{wins}")
            for h in game:
                print(f"  Game  {h.pack:<5} {h.rel}")
        if not found:
            print(f"{args.key} isn't in your Mods or in the game.")
            return 1
    return 0


def _find_item(items: list[tray.TrayItem], query: str) -> tray.TrayItem | None:
    q = query.strip().lower()
    if re.fullmatch(r"(0x)?[0-9a-f]{1,16}", q):
        wanted = int(q, 16)
        for item in items:
            if item.instance == wanted:
                return item
    for item in items:
        if q == item.name.lower():
            return item
    matches = [i for i in items if q in i.name.lower()
               or any(q in s.name.lower() for s in i.sims)]
    return matches[0] if len(matches) == 1 else None


def cmd_tray_list(args: argparse.Namespace) -> int:
    folders = _folders(args)
    if folders.tray is None or not folders.tray.is_dir():
        print("Tray folder not found; pass --sims-folder.", file=sys.stderr)
        return 1
    items, failed = tray.scan_tray(folders.tray)
    for item in items:
        if not args.all and item.type != tray.ITEM_HOUSEHOLD:
            continue
        sims = ", ".join(s.name for s in item.sims)
        print(f"{item.id_text}  {item.kind:<9} {item.name}" + (f"  ({sims})" if sims else ""))
    for path, reason in failed:
        print(f"Skipped {path.name}: {reason}", file=sys.stderr)
    return 0


def cmd_tray_show(args: argparse.Namespace) -> int:
    from traytrim.resolve import locate, summarize

    folders = _folders(args)
    if folders.tray is None or not folders.tray.is_dir():
        print("Tray folder not found; pass --sims-folder.", file=sys.stderr)
        return 1
    items, _failed = tray.scan_tray(folders.tray)
    item = _find_item(items, args.item)
    if item is None:
        print(f"No single tray item matches {args.item!r}. `traytrim tray list` shows them.",
              file=sys.stderr)
        return 1
    hh_file = item.file_with_suffix(".householdbinary")
    if hh_file is None:
        print(f"{item.name} is a {item.kind}; only households are supported so far.",
              file=sys.stderr)
        return 1
    contents = tray.read_household(hh_file)
    with ResourceIndex() as ix:
        if not args.no_update:
            _update_index(ix, folders, quiet=True)
        refs = locate(contents, ix)

    summary = summarize(refs)
    print(f"{item.name}: {', '.join(sim.sim.name for sim in contents.sims)}")
    print(f"CC: {len(summary.cc)} items from {len(summary.packages)} packages. From the game: "
          f"{len(summary.game)}" + (f" ({', '.join(summary.packs)})" if summary.packs else "")
          + f". Missing: {len(summary.missing)}.")
    missing = summary.missing
    group = None
    for r in summary.cc:
        if r.ref.group != group:
            group = r.ref.group
            print(f"\n{group}")
        label = tray.BODY_TYPES.get(r.ref.body_type, "") if r.ref.body_type is not None else ""
        conflict = f"  [{len(r.mods)} copies, using the last]" if len(r.mods) > 1 else ""
        approx = "  [approximate]" if r.approximate else ""
        print(f"  {label:<14} {r.used.rel}{conflict}{approx}")
        if args.verbose:
            for sim, uses in r.usage_by_name():
                print(f"      {sim}: {', '.join(sorted(uses))}")
    if args.plan:
        _print_plan(refs)
    if missing:
        print("\nMissing (the Sim will look different without these)")
        for r in missing:
            uses = "; ".join(f"{sim}: {', '.join(sorted(u))}" for sim, u in r.usage_by_name())
            print(f"  {r.ref.kind.value} {r.ref.instance:016X}  {uses}")
    return 0


def _print_plan(refs) -> None:
    from traytrim.plan import build_plan
    from traytrim.units import percent_smaller

    with ResourceIndex() as ix:
        plan = build_plan(refs, ix)
    whole = plan.whole_size
    saving = percent_smaller(plan.export_size, whole)
    print(f"\nExport: {human_size(plan.export_size)} in one package, {len(plan.resources):,} "
          f"files from {len(plan.source_packages):,} CC packages.")
    print(f"Whole packages: {human_size(whole)} ({len(plan.packages_with_copies):,} packages)"
          + (f", {saving}." if saving else "."))
    if plan.conflicts:
        print(f"{len(plan.conflicts)} files have different copies in Mods; using the one that "
              "loads last:")
        for c in plan.conflicts:
            print(f"  {c.key}  {c.copy.rel}  (of {len(c.copies)})")
    if plan.default_replacements:
        print(f"{len(plan.default_replacements)} items replace game content (default "
              "replacements):")
        for d in plan.default_replacements:
            print(f"  {d.key}  {d.copy.rel}")
    if plan.missing:
        print(f"{len(plan.missing)} files that CC needs are missing:")
        for key, needers in plan.missing.items():
            print(f"  {key}  needed by {', '.join(str(n) for n in needers)}")
    for key, path, reason in plan.unreadable:
        print(f"  Couldn't read {key} in {path}: {reason}")


def _export_error() -> type[Exception]:
    from traytrim.export import ExportError
    return ExportError


def cmd_export(args: argparse.Namespace) -> int:
    from traytrim.export import ExportOptions, default_base_name, export_household, safe_name
    from traytrim.plan import build_plan
    from traytrim.resolve import gameplay_mods, locate

    folders = _folders(args)
    if folders.tray is None or not folders.tray.is_dir():
        print("Tray folder not found; pass --sims-folder.", file=sys.stderr)
        return 1
    items, _failed = tray.scan_tray(folders.tray)
    item = _find_item(items, args.item)
    if item is None or item.file_with_suffix(".householdbinary") is None:
        print(f"No single household matches {args.item!r}. `traytrim tray list` shows them.",
              file=sys.stderr)
        return 1
    contents = tray.read_household(item.file_with_suffix(".householdbinary"))
    sim_ids = {s.sim.sim_id for s in contents.sims}
    out = Path(args.out).expanduser() if args.out else paths.desktop_dir()
    base = safe_name(args.name) if args.name else default_base_name(item, contents, sim_ids)
    steps = {"package": "Copying files", "verify": "Checking", "zip": "Zipping"}
    last = [0.0]

    def progress(step: str, done: int, total: int) -> None:
        now = time.monotonic()
        if sys.stderr.isatty() and (now - last[0] > 0.2 or done == total):
            last[0] = now
            print(f"\r{steps[step]}: {done:,} of {total:,}   ", end="", file=sys.stderr)
            if done == total:
                print(file=sys.stderr)

    with ResourceIndex() as ix:
        if not args.no_update:
            _update_index(ix, folders, quiet=True)
        refs = locate(contents, ix)
        plan = build_plan(refs, ix)
        gameplay = gameplay_mods(contents, ix)
        result = export_household(
            item, contents, refs, plan, gameplay, sim_ids,
            ExportOptions(out, base, not args.no_test_kit, folders.user_dir,
                          folders.game_dir), ix, progress)
    print(f"{result.zip_path}  ({human_size(result.zip_size)})")
    for check in result.verification.checks:
        print(f"  [{'ok' if check.passed else 'FAILED'}] {check.name}: {check.detail}")
        for problem in check.problems[:20]:
            print(f"      {problem}")
    if result.gameplay_mods:
        print("Also needs these gameplay mods (not included):")
        for m in result.gameplay_mods:
            print(f"  {m.rel}")
    if result.test_kit:
        print(f"In-game test kit: {result.test_kit}")
    return 0 if result.verification.ok else 1


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from traytrim.gui.app import run
    except ImportError as e:
        if not (e.name or "").startswith("PySide6"):
            raise
        print(f"The app needs PySide6: pip install traytrim[gui] ({e})", file=sys.stderr)
        return 1
    return run(getattr(args, "theme", None))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="traytrim", description=__doc__)
    parser.add_argument("--version", action="version", version=f"TrayTrim {__version__}")
    sub = parser.add_subparsers(dest="command")

    info = sub.add_parser("info", help="show what a package contains (read-only)")
    info.add_argument("package")
    info.add_argument("--list", action="store_true", help="list every resource")
    info.add_argument("--verify", action="store_true", help="decompress every resource")
    info.set_defaults(func=cmd_info)

    folder_args = argparse.ArgumentParser(add_help=False)
    folder_args.add_argument("--sims-folder", help="Sims 4 user folder (holds Mods and Tray)")
    folder_args.add_argument("--game-folder", help="folder The Sims 4 is installed in")

    index = sub.add_parser("index", parents=[folder_args],
                           help="build or refresh the index of Mods and game packages")
    index.add_argument("--quiet", action="store_true", help="no progress output")
    index.set_defaults(func=cmd_index)

    which = sub.add_parser("which", parents=[folder_args],
                           help="show which packages hold a resource")
    which.add_argument("key", help="type:group:instance in hex, or just an instance")
    which.add_argument("--no-update", action="store_true",
                       help="use the index as it is, without checking for changed packages")
    which.set_defaults(func=cmd_which)

    tray_cmd = sub.add_parser("tray", help="tray items and the CC they use")
    tray_sub = tray_cmd.add_subparsers(dest="tray_command", required=True)
    tray_list = tray_sub.add_parser("list", parents=[folder_args], help="list tray items")
    tray_list.add_argument("--all", action="store_true", help="include lots and rooms")
    tray_list.set_defaults(func=cmd_tray_list)
    tray_show = tray_sub.add_parser("show", parents=[folder_args],
                                    help="show the CC a household uses")
    tray_show.add_argument("item", help="tray id, household name, or part of a Sim's name")
    tray_show.add_argument("--verbose", "-v", action="store_true",
                           help="also show where each item is used")
    tray_show.add_argument("--plan", action="store_true",
                           help="also follow dependencies and show export sizes and conflicts")
    tray_show.add_argument("--no-update", action="store_true",
                           help="use the index as it is")
    tray_show.set_defaults(func=cmd_tray_show)

    export = sub.add_parser("export", parents=[folder_args],
                            help="export a household with only the CC it uses")
    export.add_argument("item", help="tray id, household name, or part of a Sim's name")
    export.add_argument("--out", help="folder for the zip (default: Desktop)")
    export.add_argument("--name", help="file name start (default: the Sim or household name)")
    export.add_argument("--no-test-kit", action="store_true",
                        help="don't make the in-game test folder")
    export.add_argument("--no-update", action="store_true", help="use the index as it is")
    export.set_defaults(func=cmd_export)

    gui = sub.add_parser("gui", help="open the app")
    gui.add_argument("--theme", choices=("system", "light", "dark"),
                     help="override the theme for this run")
    gui.set_defaults(func=cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Sim and package names can hold any script; a console that can't show a character
        # gets a placeholder instead of a crash.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    if args.command is None:
        return cmd_gui(args)
    try:
        return args.func(args)
    except (dbpf.DbpfError, tray.TrayError, OSError, _export_error()) as e:
        print(f"traytrim: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f"traytrim: the index cache couldn't be used ({e}). If the TrayTrim app is "
              "indexing right now, wait for it to finish and try again.", file=sys.stderr)
        return 1
