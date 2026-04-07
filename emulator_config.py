"""
emulator_config.py — read, check, and patch MEmu instance config (.memu) files.

The .memu format is XML. We use regex replacement rather than a full XML
parse so that comments, whitespace, and attribute ordering are preserved
exactly as MEmu wrote them.

IMPORTANT: Never modify a .memu file while MEmu (or any MemuHyperv app)
is running — MEmu will overwrite changes on exit.
"""

import os
import re
import subprocess


# ---------------------------------------------------------------------------
# Required settings for Last Z Bot
# ---------------------------------------------------------------------------
# Hardware section (XML attributes, not GuestProperties)
# key → (required_value, display_label, settings_hint)
MEMU_HARDWARE_CHECKS = [
    ("cpu_count", "4",    "CPU cores",        "Performance → CPU: 4"),
    ("ram_mb",    "4096", "RAM",               "Performance → RAM: 4096 MB"),
    ("audio",     "Null", "Audio driver",      "Device → Audio driver: Mute"),
]

# GuestProperty section
MEMU_GUEST_CHECKS = [
    ("enable_su",              "1",       "Root mode",              "Performance → Root mode: On"),
    ("fps",                    "20",      "Frame rate",             "Display → Frame Rate: 20 fps"),
    ("graphics_render_mode",   "1",       "Render mode (DirectX)",  "Performance → Render mode: DirectX"),
    ("gmem_opt",               "1",       "GPU memory optimization","Performance → GPU memory optimization: On"),
    ("astc",                   "1",       "ASTC decode",            "Performance → ASTC decode: Auto"),
    ("is_customed_resolution", "1",       "Custom resolution",      "Display → Resolution: Customize"),
    ("resolution_width",       "540",     "Resolution width",       "Display → Resolution: W 540"),
    ("resolution_height",      "960",     "Resolution height",      "Display → Resolution: H 960"),
    ("vbox_dpi",               "180",     "DPI",                    "Display → Resolution: DPI 180"),
    ("mic_name_md5",           "NoSound", "Microphone",             "Device → Microphone: Disabled"),
    ("speaker_name_md5",       "NoSound", "Speaker/audio",          "Device → Audio driver: Mute"),
]


# ---------------------------------------------------------------------------
# File location helpers
# ---------------------------------------------------------------------------

def _folder_name(cli_index: int) -> str:
    """MEmu folder name for a given 0-based CLI index."""
    return "MEmu" if cli_index == 0 else f"MEmu_{cli_index}"


def find_memu_config(install_path: str, cli_index: int) -> str | None:
    """
    Return the path to the .memu config file for the given instance,
    or None if the file doesn't exist.
    """
    folder = _folder_name(cli_index)
    path = os.path.join(install_path, "MemuHyperv VMs", folder, f"{folder}.memu")
    return path if os.path.isfile(path) else None


# ---------------------------------------------------------------------------
# Read settings
# ---------------------------------------------------------------------------

def read_memu_settings(config_path: str) -> dict:
    """
    Parse a .memu file and return a flat dict of setting values.
    Hardware keys: 'cpu_count', 'ram_mb', 'audio'
    Plus all GuestProperty names as-is.
    """
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    settings = {}

    m = re.search(r'<CPU\s+count="(\d+)"', content)
    settings["cpu_count"] = m.group(1) if m else None

    m = re.search(r'<Memory\s+RAMSize="(\d+)"', content)
    settings["ram_mb"] = m.group(1) if m else None

    m = re.search(r'<AudioAdapter\s+driver="([^"]*)"', content)
    settings["audio"] = m.group(1) if m else None

    for m in re.finditer(r'<GuestProperty\s+name="([^"]+)"\s+value="([^"]*)"', content):
        settings[m.group(1)] = m.group(2)

    return settings


# ---------------------------------------------------------------------------
# Check settings
# ---------------------------------------------------------------------------

def check_memu_settings(config_path: str) -> list[dict]:
    """
    Check a .memu file against required values.

    Returns a list of result dicts:
        key, label, hint, required, actual, ok
    """
    settings = read_memu_settings(config_path)
    results = []

    for key, required, label, hint in MEMU_HARDWARE_CHECKS:
        actual = settings.get(key)
        results.append({
            "key": key, "label": label, "hint": hint,
            "required": required, "actual": actual,
            "ok": actual == required,
        })

    for key, required, label, hint in MEMU_GUEST_CHECKS:
        actual = settings.get(key)
        results.append({
            "key": key, "label": label, "hint": hint,
            "required": required, "actual": actual,
            "ok": actual == required,
        })

    return results


# ---------------------------------------------------------------------------
# Apply fixes
# ---------------------------------------------------------------------------

def apply_memu_fixes(config_path: str, issues: list[dict]) -> int:
    """
    Write corrected values for all failed checks back to the .memu file.
    Uses regex replacement to preserve comments, whitespace, and attribute order.
    Returns the number of settings changed.
    """
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    changed = 0
    for issue in issues:
        if issue["ok"]:
            continue

        key = issue["key"]
        val = issue["required"]

        if key == "cpu_count":
            new, n = re.subn(
                r'(<CPU\s+count=")[^"]*(")', rf'\g<1>{val}\2', content)
        elif key == "ram_mb":
            new, n = re.subn(
                r'(<Memory\s+RAMSize=")[^"]*(")', rf'\g<1>{val}\2', content)
        elif key == "audio":
            new, n = re.subn(
                r'(<AudioAdapter\s+driver=")[^"]*(")', rf'\g<1>{val}\2', content)
        else:
            pattern = rf'(<GuestProperty\s+name="{re.escape(key)}"\s+value=")[^"]*(")'
            new, n = re.subn(pattern, rf'\g<1>{val}\2', content)

        if n > 0:
            content = new
            changed += 1

    if changed > 0:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)

    return changed


# ---------------------------------------------------------------------------
# MEmu running check
# ---------------------------------------------------------------------------

def is_memu_running() -> bool:
    """Return True if any MEmu application process is currently running."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MEmu.exe", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
        return "MEmu.exe" in result.stdout
    except Exception:
        return False
