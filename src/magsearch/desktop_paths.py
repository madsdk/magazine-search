"""Per-OS user-data directory resolution for the desktop app."""

import os
import sys
from pathlib import Path

APP_NAME = "magsearch"


def get_app_data_dir() -> Path:
    """Return the per-OS directory where the desktop app stores its DB and bundles.

    macOS:   ~/Library/Application Support/magsearch
    Windows: %APPDATA%\\magsearch
    Linux:   $XDG_DATA_HOME/magsearch, falling back to ~/.local/share/magsearch
    """
    if sys.platform == "darwin":
        home = Path(os.environ.get("HOME", str(Path.home())))
        return home / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    home = Path(os.environ.get("HOME", str(Path.home())))
    return home / ".local" / "share" / APP_NAME
