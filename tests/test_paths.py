from pathlib import Path

import pytest

from traytrim import paths
from traytrim.settings import Settings


def make_user_dir(path: Path) -> Path:
    (path / "Mods").mkdir(parents=True)
    (path / "Tray").mkdir()
    return path


def test_finds_macos_user_dir(tmp_path):
    user = make_user_dir(tmp_path / "Documents" / "Electronic Arts" / "The Sims 4")
    assert paths.find_user_dir("darwin", tmp_path) == user


def test_finds_localised_folder_name(tmp_path):
    user = make_user_dir(tmp_path / "Documents" / "Electronic Arts" / "Die Sims 4")
    (tmp_path / "Documents" / "Electronic Arts" / "Some Other Game").mkdir()
    assert paths.find_user_dir("darwin", tmp_path) == user


def test_finds_linux_proton_user_dir(tmp_path):
    docs = (tmp_path / ".local" / "share" / "Steam" / "steamapps" / "compatdata" / "1222670"
            / "pfx" / "drive_c" / "users" / "steamuser" / "Documents")
    user = make_user_dir(docs / "Electronic Arts" / "The Sims 4")
    assert paths.find_user_dir("linux", tmp_path) == user


def test_folder_without_mods_or_tray_is_not_a_user_dir(tmp_path):
    (tmp_path / "Documents" / "Electronic Arts" / "The Sims 4" / "saves").mkdir(parents=True)
    assert paths.find_user_dir("darwin", tmp_path) is None


def test_finds_windows_onedrive_user_dir_without_touching_registry(tmp_path):
    user = make_user_dir(tmp_path / "OneDrive" / "Documents" / "Electronic Arts" / "The Sims 4")
    assert paths.find_user_dir("win32", tmp_path) == user


def test_missing_chosen_folder_falls_back_to_detection(tmp_path, monkeypatch):
    detected = make_user_dir(tmp_path / "detected")
    monkeypatch.setattr(paths, "find_user_dir", lambda: detected)
    assert paths.resolve_user_dir(str(tmp_path / "moved-away")) == detected
    chosen = make_user_dir(tmp_path / "chosen")
    assert paths.resolve_user_dir(str(chosen)) == chosen


def test_finds_linux_steam_game_dir(tmp_path):
    game = tmp_path / ".steam" / "steam" / "steamapps" / "common" / "The Sims 4"
    (game / "Data" / "Client").mkdir(parents=True)
    assert paths.find_game_dir("linux", tmp_path) == game


def test_macos_bundle_counts_as_game_dir(tmp_path):
    app = tmp_path / "The Sims 4.app"
    (app / "Contents" / "Data" / "Client").mkdir(parents=True)
    assert paths.looks_like_game_dir(app)


def test_chosen_folders_win(tmp_path):
    make_user_dir(tmp_path / "u")
    (tmp_path / "g" / "Data" / "Client").mkdir(parents=True)
    folders = paths.resolve_folders(str(tmp_path / "u"), str(tmp_path / "g"))
    assert folders.mods == tmp_path / "u" / "Mods"
    assert folders.tray == tmp_path / "u" / "Tray"
    assert folders.game_dir == tmp_path / "g"


def test_settings_round_trip_and_bad_file(tmp_path):
    path = tmp_path / "s.json"
    Settings(user_dir="x", theme="dark").save(path)
    loaded = Settings.load(path)
    assert loaded.user_dir == "x" and loaded.theme == "dark"
    path.write_text("{not json", encoding="utf-8")
    assert Settings.load(path) == Settings()
    path.write_text('{"theme": "light", "removed_option": 1}', encoding="utf-8")
    assert Settings.load(path).theme == "light"


@pytest.mark.parametrize("content", ["[]", "null", "5", '{"user_dir": 5, "theme": "purple"}',
                                     '{"theme": null, "window_geometry": []}'])
def test_settings_with_wrong_shape_fall_back_to_defaults(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content, encoding="utf-8")
    assert Settings.load(path) == Settings()
