"""Render the README screenshots from a made-up install (packaging/demo_world.py).

    python packaging/make_screenshots.py

Runs fully isolated: the demo world, TrayTrim's cache and its settings all live in a temporary
folder that's deleted afterwards. On Windows the world goes under C:\\Users\\Public so the paths
shown in the status bar don't include anyone's user name.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"

base = Path("C:/Users/Public") if sys.platform == "win32" else Path(tempfile.gettempdir())
WORK = base / "TrayTrim Demo"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)
# Isolate TrayTrim's cache and settings (on every platform) before it is imported.
os.environ["TRAYTRIM_CACHE_DIR"] = str(WORK / "cache")
os.environ["TRAYTRIM_CONFIG_DIR"] = str(WORK / "config")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT / "packaging"))

from traytrim.gui import app as appmod  # noqa: E402
from traytrim.gui.export_dialog import ExportDialog, ExportRequest  # noqa: E402
from traytrim.gui.gallery import ItemRole  # noqa: E402
from traytrim.gui.main_window import KINDS, MainWindow  # noqa: E402
from traytrim.export import default_base_name  # noqa: E402
from traytrim.settings import Settings  # noqa: E402

app = appmod.create_app(["make_screenshots"])
import demo_world  # noqa: E402  (needs the QApplication for drawing)


def pump(seconds: float, until=None) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)
        if until is not None and until():
            return


def open_window(theme: str, user: Path, game: Path, out: Path) -> MainWindow:
    appmod.set_theme(app, theme)
    settings = Settings(user_dir=str(user), game_dir=str(game), output_dir=str(out))
    window = MainWindow(settings)
    window.resize(1280, 800)
    window.show()
    pump(60, lambda: window.index.last_result is not None and window.trays.source.rowCount())
    pump(1)
    return window


def select(window: MainWindow, name: str) -> None:
    proxy = window.trays.proxy
    for row in range(proxy.rowCount()):
        index = proxy.index(row, 0)
        if index.data(ItemRole).name == name:
            window.trays.setCurrentIndex(index)
            break
    pump(30, lambda: window.household._plan is not None
         and window.household.current_item().name == name)
    pump(1.5)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        user, game = demo_world.build(WORK)
        exports = WORK / "Desktop"
        for theme in ("light", "dark"):
            w = open_window(theme, user, game, exports)
            if theme == "light":  # nothing picked yet: the start page
                w._select(None)
                pump(1)
                w.grab().save(str(OUT / "start-light.png"))
            select(w, "Moreau")
            w.grab().save(str(OUT / f"household-{theme}.png"))
            if theme == "light":
                select(w, "Hart")
                w.grab().save(str(OUT / "family-notices.png"))
                w.household.browser.set_view("list")
                pump(0.5)
                w.grab().save(str(OUT / "list-view.png"))
                w.household.browser.set_view("cards")
                select(w, "Moreau")
                pump(10, lambda: len(w.household.browser.shown()) > 1)
                card = w.household.browser.shown()[1]
                w.household.browser._activated(card)  # left out
                pump(30, lambda: w.household._plan is not None)
                pump(1)
                w.grab().save(str(OUT / "leave-out.png"))
                w.household.browser._activated(card)  # back in
                pump(30, lambda: w.household._plan is not None)
                pump(1)
                # A lot: names, pictures, a conflict to choose from
                w._set_kind(KINDS[1])
                pump(0.5)
                select(w, "Seaside Cottage")
                pump(10, lambda: len(w.household._names) >= 8)
                pump(1)
                w.household._expanded.add("conflicts")
                w.household._rebuild()
                pump(0.5)
                w.grab().save(str(OUT / "lot-light.png"))
                w.household.browser.search.setText("driftwood")
                w.household.browser.set_sort("size")
                pump(1)
                w.household.scroll.verticalScrollBar().setValue(560)
                pump(0.5)
                w.grab().save(str(OUT / "lot-search.png"))
                w._set_kind(KINDS[0])
                pump(0.5)
                select(w, "Moreau")
            report, plan, gameplay = w.household.export_state()
            request = ExportRequest(report.item, report.contents, report.refs, plan, gameplay,
                                    set(report.sim_ids),
                                    default_base_name(report.item, report.contents,
                                                      report.sim_ids),
                                    w.folders.user_dir, w.folders.game_dir)
            dialog = ExportDialog(request, w.settings, w)
            dialog.show()
            pump(0.5)
            if theme == "light":
                dialog.grab().save(str(OUT / "export-light.png"))
            dialog._start()
            pump(60, lambda: dialog.result is not None)
            pump(0.5)
            if theme == "light":
                dialog.grab().save(str(OUT / "verified-light.png"))
            dialog.close()
            w.close()
            pump(0.5)
        print(f"Screenshots in {OUT}")
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
