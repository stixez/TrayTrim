"""Entry point of the frozen app (TrayTrim.exe): opens the window."""

import sys

from traytrim.gui.app import run

if __name__ == "__main__":
    sys.exit(run())
