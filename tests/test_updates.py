"""Telling a newer release apart, and picking its download for this platform."""

from traytrim import updates
from traytrim.updates import is_newer, parse_release, parse_version

BASE = "https://github.com/stixez/TrayTrim/releases/download/v0.4.0/"
RELEASE = {
    "tag_name": "v0.4.0",
    "html_url": "https://github.com/stixez/TrayTrim/releases/tag/v0.4.0",
    "body": "One package per CC item.",
    "assets": [{"name": f"TrayTrim-0.4.0-{suffix}", "browser_download_url": BASE + f"TrayTrim-0.4.0-{suffix}"}
               for suffix in ("linux-x86_64.tar.gz", "macos-arm64.zip", "setup.exe",
                              "windows-portable.zip")],
}


def test_versions_compare_as_numbers():
    assert parse_version("v0.10.0") == (0, 10, 0) and parse_version("1.2") == (1, 2, 0)
    assert parse_version("0.4.0-beta") is None
    assert is_newer("0.10.0", "0.9.9") and is_newer("v1.0", "0.3.0")
    assert not is_newer("0.3.0", "0.3.0") and not is_newer("junk", "0.3.0")


def test_each_platform_gets_its_own_download():
    assert parse_release(RELEASE, "win32").download.endswith("-setup.exe")
    assert parse_release(RELEASE, "darwin").download.endswith("-macos-arm64.zip")
    assert parse_release(RELEASE, "linux").download.endswith("-linux-x86_64.tar.gz")
    r = parse_release(RELEASE, "win32")
    assert (r.version, r.notes) == ("0.4.0", "One package per CC item.")


def test_releases_that_cant_be_offered():
    assert parse_release({**RELEASE, "prerelease": True}) is None
    assert parse_release({**RELEASE, "tag_name": "nightly"}) is None
    assert parse_release(["not", "a", "release"]) is None
    # Links that don't point at the project's own releases fall back to its release page.
    odd = {**RELEASE, "html_url": "https://example.com/x",
           "assets": [{"name": "TrayTrim-0.4.0-setup.exe",
                       "browser_download_url": "https://example.com/setup.exe"}]}
    r = parse_release(odd, "win32")
    assert r.page == r.download == updates.RELEASES_PAGE


def test_skipped_and_older_releases_are_not_offered(monkeypatch):
    release = parse_release(RELEASE, "win32")
    monkeypatch.setattr(updates, "latest_release", lambda: release)
    assert updates.available_update() == release
    assert updates.available_update(skipped="0.4.0") is None
    monkeypatch.setattr(updates, "__version__", "0.4.0")
    assert updates.available_update() is None
    monkeypatch.setattr(updates, "latest_release", lambda: None)  # offline
    assert updates.available_update() is None


def test_release_notes_open_only_web_pages(monkeypatch):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QApplication

    from traytrim.gui import system
    from traytrim.gui.update_dialog import UpdateDialog, open_https
    from traytrim.settings import Settings

    _app = QApplication.instance() or QApplication([])
    opened = []
    monkeypatch.setattr(system, "open_url", opened.append)
    for url in ("https://github.com/stixez/TrayTrim", "file:///C:/Windows/System32/calc.exe",
                "javascript:alert(1)", "ms-settings:"):
        open_https(QUrl(url))
    assert opened == ["https://github.com/stixez/TrayTrim"]
    dialog = UpdateDialog(parse_release(RELEASE, "win32"), Settings())
    dialog.download_button.click()
    assert opened[-1].endswith("-setup.exe")
    dialog.deleteLater()
