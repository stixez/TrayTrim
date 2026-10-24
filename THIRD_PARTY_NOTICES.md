# Third-party notices

TrayTrim itself is released under the MIT License (see `LICENSE`). The app you download also
contains the software and fonts below, each under its own license.

## Qt for Python (PySide6, Shiboken6) and Qt

- Copyright (C) The Qt Company Ltd. and other contributors.
- Used under the GNU Lesser General Public License, version 3 (LGPL-3.0-only), which builds on
  the GNU General Public License, version 3. Both texts are in `LICENSES/LGPL-3.0.txt` and
  `LICENSES/GPL-3.0.txt`.
- TrayTrim doesn't change Qt or PySide6. The builds keep them as separate shared libraries (the
  PySide6 and Qt files in the app folder), so you can replace them with your own build of the
  same version.
- Source code: <https://code.qt.io/cgit/pyside/pyside-setup.git/> (PySide6 and Shiboken6) and
  <https://code.qt.io/cgit/qt/> (Qt). Released versions are listed at
  <https://download.qt.io/official_releases/QtForPython/>.

## IBM Plex fonts

- Copyright 2017 IBM Corp., with Reserved Font Name "Plex".
- Licensed under the SIL Open Font License, version 1.1. The license text is in
  `src/traytrim/gui/assets/fonts/OFL.txt` (in the app: `traytrim/gui/assets/fonts/OFL.txt`).
- <https://github.com/IBM/plex>

## Python and PyInstaller

- The builds include the Python runtime, under the Python Software Foundation License:
  <https://docs.python.org/3/license.html>.
- They are made with PyInstaller, whose bootloader is GPL-2.0 with an exception that allows it
  in apps under any license: <https://pyinstaller.org/en/stable/license.html>.

## Formats and descriptions

No code from these projects is included; they were used as documentation.

- The lot and room layouts and the snappy container were described by
  [Harbor](https://github.com/Harborofficial/Harbor) (MIT).
- Resource type ids were cross-checked with [Sims 4 Toolkit](https://github.com/sims4toolkit),
  s4pi-reforged and TS4Tools.
- The snappy decoder is written from Google's
  [format description](https://github.com/google/snappy). RefPack and DBPF are written from
  public descriptions of the formats.

## Icons and images

The icons in `src/traytrim/gui/assets/icons/`, the app icon and the installer images were made
for TrayTrim and are under its MIT License. No stock icon set is used. The README screenshots
show made-up data from `packaging/demo_world.py`, not EA artwork.

## Trademarks

The Sims is a trademark of Electronic Arts Inc. TrayTrim is a fan-made tool and isn't affiliated
with or endorsed by Electronic Arts or Maxis.
