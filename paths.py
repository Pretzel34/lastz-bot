"""Platform-aware paths for config and data files."""

import os
import sys
from pathlib import Path

APP_NAME = "LastZBot"


def get_app_dir() -> Path:
    """Return the directory where user-specific config/data should live."""
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_NAME


def ensure_app_dir() -> Path:
    """Create the app directory if it doesn't exist and return it."""
    p = get_app_dir()
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_farms_path() -> Path:
    """Return the path to the farms.json settings file."""
    return ensure_app_dir() / "farms.json"
