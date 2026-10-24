"""Damaged or hostile input, odd installs and links: each fails cleanly or is found."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from traytrim import dbpf, paths, received, tray
from traytrim import protobuf as pb
from traytrim.dbpf import DbpfError
from traytrim.export import forbidden_reason
from traytrim.settings import Settings
from traytrim.verify import read_zip_comment


def _refpack_header(size: int) -> bytes:
    return bytes([0x80, 0xFB]) + size.to_bytes(4, "big")


def test_refpack_header_must_match_the_index_before_unpacking():
    # Says 256 MB, the index says 10 bytes: refused before anything is unpacked.
    stream = _refpack_header(256 << 20) + bytes([0xFC])
    with pytest.raises(DbpfError, match="index says"):
        dbpf.decompress(stream, dbpf.COMP_REFPACK, 10)
    with pytest.raises(DbpfError, match="more than any Sims 4"):
        dbpf.refpack_decompress(_refpack_header(1 << 31) + bytes([0xFC]))


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def test_tray_key_with_an_instance_past_64_bits_is_skipped():
    raw = b"\x08" + _varint(0x034AEECB) + b"\x18" + _varint((1 << 64) + 5)
    assert pb.first_int(pb.fields_by_number(raw), 3) == (1 << 64) + 5
    assert tray._key_message(raw) is None


def test_deeply_nested_json_is_treated_as_damaged(tmp_path):
    deep = "[" * 100_000 + "]" * 100_000
    path = tmp_path / "settings.json"
    path.write_text(deep, encoding="utf-8")
    assert Settings.load(path) == Settings()
    assert read_zip_comment(('{"traytrim": ' + deep + "}").encode()) is None


def test_history_rows_survive_hand_edited_values(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from traytrim.export import load_history
    from traytrim.gui.history_dialog import HistoryRow

    monkeypatch.setenv("TRAYTRIM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "history.json").write_text(json.dumps(
        [5, {"zip": 5, "name": ["x"], "kind": 3, "time": 1, "bytes": "big"}]), encoding="utf-8")
    QApplication.instance() or QApplication([])
    entries = load_history()
    assert len(entries) == 1
    row = HistoryRow(entries[0], lambda _p: None)
    assert row.zip == Path("")


@pytest.mark.parametrize("name", ["COM¹.package", "lpt³.txt", "CONOUT$", "nul.package",
                                  "NUL .package", "com1  .txt"])
def test_windows_device_names_are_not_unpacked(name):
    assert received._safe_name(f"Mods/{name}") is None


def test_steam_libraries_on_other_drives_are_searched(tmp_path):
    steam = tmp_path / ".local" / "share" / "Steam"
    (steam / "steamapps").mkdir(parents=True)
    card = tmp_path / "sdcard"
    def vdf(path: Path) -> str:  # Steam escapes backslashes in paths
        return str(path).replace("\\", "\\\\")

    entries = "".join(f'\t"{n}"\n\t{{\n\t\t"path"\t\t"{vdf(p)}"\n\t}}\n'
                      for n, p in enumerate((steam, card)))
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n{entries}}}\n', encoding="utf-8")
    assert card in paths.steam_libraries(tmp_path)
    docs = (card / "steamapps" / "compatdata" / "1222670" / "pfx" / "drive_c" / "users"
            / "steamuser" / "Documents")
    user = docs / "Electronic Arts" / "The Sims 4"
    (user / "Mods").mkdir(parents=True)
    assert paths.find_user_dir("linux", tmp_path) == user
    game = card / "steamapps" / "common" / "The Sims 4"
    assert game in paths.game_dir_candidates("linux", tmp_path)


def test_lutris_and_bottles_prefixes_are_searched(tmp_path):
    lutris = (tmp_path / "Games" / "the-sims-4" / "drive_c" / "users" / "sam" / "Documents"
              / "Electronic Arts" / "Les Sims 4")
    (lutris / "Tray").mkdir(parents=True)
    assert paths.find_user_dir("linux", tmp_path) == lutris
    bottle = (tmp_path / ".local" / "share" / "bottles" / "bottles" / "Sims" / "drive_c" / "users"
              / "sam" / "Documents" / "Electronic Arts" / "The Sims 4")
    (bottle / "Mods").mkdir(parents=True)
    assert bottle in paths.user_dirs("linux", tmp_path)


def test_macos_steam_game_is_a_candidate(tmp_path):
    common = (tmp_path / "Library" / "Application Support" / "Steam" / "steamapps" / "common"
              / "The Sims 4")
    assert common in paths.game_dir_candidates("darwin", tmp_path)


def _link(link: Path, target: Path) -> bool:
    """A directory junction on Windows (no admin rights needed), a symlink elsewhere."""
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True,
                           capture_output=True)
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def test_exports_into_a_linked_mods_folder_are_refused(tmp_path):
    sims = tmp_path / "Sims 4"
    sims.mkdir()
    stash = tmp_path / "ModStash"  # where a mod manager keeps the real Mods
    stash.mkdir()
    if not _link(sims / "Mods", stash):
        pytest.skip("can't make a directory link here")
    assert forbidden_reason(stash, sims, None)
    assert forbidden_reason(stash / "exports", sims, None)
    assert forbidden_reason(tmp_path / "Desktop", sims, None) is None
    # A linked folder the game doesn't load from (screenshots in Pictures) doesn't block its
    # target.
    pictures = tmp_path / "Pictures"
    pictures.mkdir()
    assert _link(sims / "Screenshots", pictures)
    assert forbidden_reason(pictures, sims, None) is None


def test_a_bug_in_a_worker_thread_is_reported_on_the_ui_thread(monkeypatch):
    import threading

    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    from traytrim.gui.app import CrashReporter

    app = QApplication.instance() or QApplication([])
    reporter = CrashReporter()
    shown = []
    monkeypatch.setattr(reporter, "ask", lambda headline, _details: shown.append(
        (headline, QThread.currentThread() is app.thread())))

    def fail():
        try:
            raise ValueError("boom")
        except ValueError:
            reporter(*sys.exc_info())

    for _ in range(2):  # the same bug twice: one report
        worker = threading.Thread(target=fail)
        worker.start()
        worker.join()
    for _ in range(5):
        app.processEvents()
    assert shown == [("TrayTrim hit a bug: ValueError: boom", True)]
