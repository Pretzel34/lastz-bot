"""Simple GitHub Releases-based updater.

The updater checks the latest release for a given GitHub repo, compares it to the
current version, and can download & launch an installer asset.

This is intentionally small and dependency-free (uses urllib).
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Optional, Tuple


def _normalize_version(v: str) -> Tuple[int, ...]:
    """Convert a version string like v1.2.3 into a tuple of ints."""
    if v is None:
        return ()
    v = v.strip().lstrip("vV")
    parts = v.split(".")
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            # ignore parts that are not numeric
            break
    return tuple(out)


def _compare_versions(a: str, b: str) -> int:
    """Compare two version strings.

    Returns:
        -1 if a < b
         0 if a == b
         1 if a > b
    """
    a_t = _normalize_version(a)
    b_t = _normalize_version(b)
    if a_t == b_t:
        return 0
    return -1 if a_t < b_t else 1


def get_latest_release(owner: str, repo: str, timeout: int = 10) -> Optional[dict]:
    """Fetch the latest GitHub release metadata."""
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    headers = {"User-Agent": "LastZBot-Updater"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except urllib.error.URLError:
        return None


def find_asset_url(release: dict, name_contains: str) -> Optional[str]:
    """Find an asset URL whose name contains a given string."""
    if not release or "assets" not in release:
        return None
    for asset in release.get("assets", []):
        if name_contains.lower() in asset.get("name", "").lower():
            return asset.get("browser_download_url")
    return None


def check_for_update(
    current_version: str,
    owner: str,
    repo: str,
    asset_name_contains: str,
) -> Optional[dict]:
    """Check whether a newer version is available.

    Returns dict: {"remote_version", "asset_url"} if update found, else None.
    """
    release = get_latest_release(owner, repo)
    if not release:
        return None
    remote_version = release.get("tag_name")
    if remote_version is None:
        return None
    if _compare_versions(current_version, remote_version) >= 0:
        return None
    asset_url = find_asset_url(release, asset_name_contains)
    if not asset_url:
        return None
    return {"remote_version": remote_version, "asset_url": asset_url}


def download_file(url: str, dest_path: str, timeout: int = 30) -> bool:
    """Download a URL to a file path. Returns True on success."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False


def launch_installer(path: str) -> None:
    """Launch the downloaded installer.

    On Windows, this executes the file directly. On macOS, it uses 'open'.
    """
    if sys.platform.startswith("win"):
        subprocess.Popen([path], shell=True)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen([path])


def download_and_launch(url: str, file_name: str) -> bool:
    """Download an installer and launch it.

    Returns True if download succeeded and process was launched.
    """
    tmp_dir = tempfile.gettempdir()
    dest_path = os.path.join(tmp_dir, file_name)
    if not download_file(url, dest_path):
        return False
    launch_installer(dest_path)
    return True
