# Contributing to TrayTrim

Bug reports, ideas and pull requests are welcome. For anything bigger than a small fix, open an
issue first so we can agree on the approach.

## Set up

```
git clone https://github.com/stixez/TrayTrim
cd TrayTrim
python -m venv .venv
.venv\Scripts\activate            # macOS / Linux: source .venv/bin/activate
pip install -e ".[gui,dev]"
python -m traytrim
```

Before you open a pull request, both of these must pass (CI runs them on Windows, macOS and
Linux):

```
pytest
ruff check src tests packaging
```

To try the app without touching your own install, point TrayTrim's cache and settings somewhere
else with `TRAYTRIM_CACHE_DIR` and `TRAYTRIM_CONFIG_DIR`, and set `TRAYTRIM_NO_UPDATE_CHECK=1`.

## Rules

- **Nothing writes to the Sims 4 folders** (Mods, Tray, saves), the game or Sims 4 Studio's
  folder. TrayTrim only reads them; exports, caches and settings go elsewhere.
- **Formats aren't guessed.** Every resource type, field or layout has a source noted next to it
  (the game's own schemas, Sims 4 Toolkit, or a check on real files), and unverified results are
  marked approximate.
- **No real CC, game or tray files in the repo.** Tests build small synthetic ones.
- **Files from other people are untrusted.** Packages, tray files and zips can be damaged or
  hostile: check sizes and offsets before reading, and fail with a clear message.
- **UI changes follow [docs/DESIGN.md](docs/DESIGN.md)**: colours and sizes come from
  `src/traytrim/gui/theme.py`, both themes get checked, and everything works with the keyboard.

## Pull requests

- Keep each pull request to one change, with a test for it when it's in the core
  (`src/traytrim`).
- Describe what you checked by hand, especially for the UI (screenshots in both themes help).
- By contributing you agree that your work is released under the project's MIT License.

## Assets and screenshots

- `python packaging/build_assets.py` renders the app icon and the installer images from
  `src/traytrim/gui/assets/app/icon.svg`.
- `python packaging/make_screenshots.py` renders the README screenshots from the made-up install
  in `packaging/demo_world.py`, so no real names or CC end up in them.
- `python packaging/build.py` builds the app and, on Windows, the installer (see the README).
