"""Simple GitHub Releases-based updater.

The updater checks the latest release for a given GitHub repo, compares it to the
current version, and can download & launch an installer asset.

This is intentionally small and dependency-free (uses urllib).
"""

import json
import os
import ssl
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

    def _fetch(context=None):
        req = urllib.request.Request(url, headers=headers)
        kwargs = {"timeout": timeout}
        if context is not None:
            kwargs["context"] = context
        with urllib.request.urlopen(req, **kwargs) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # First attempt: default SSL verification
    try:
        return _fetch()
    except Exception:
        pass

    # Second attempt: Windows system certificate store — handles machines
    # where Python's bundled CA bundle is missing or stale
    try:
        ctx = ssl.create_default_context()
        ctx.load_default_certs()
        return _fetch(context=ctx)
    except Exception:
        pass

    # Third attempt: unverified context — nuclear fallback for machines where
    # the CA bundle is completely absent (common in PyInstaller bundles)
    try:
        return _fetch(context=ssl._create_unverified_context())
    except Exception:
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


def launch_installer(path: str, args: list = None) -> None:
    """Launch the downloaded installer.

    On Windows, writes a small batch file that waits ~3 seconds before
    running the installer. This gives the calling process time to fully
    exit and release any locked files (e.g. the exe being replaced)
    before the installer writes to disk.

    Pass args=["/S"] for a silent NSIS install (used by auto-update).
    """
    extra = args or []
    if sys.platform.startswith("win"):
        # ping localhost 4 times ≈ 3-second delay on Windows
        args_str = " ".join(extra)
        batch = f'@echo off\nping -n 4 127.0.0.1 > nul\n"{path}" {args_str}\n'
        batch_path = os.path.join(tempfile.gettempdir(), "lastz_update.bat")
        with open(batch_path, "w") as f:
            f.write(batch)
        subprocess.Popen(["cmd.exe", "/c", batch_path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen([path] + extra)


def download_and_launch(url: str, file_name: str, args: list = None) -> bool:
    """Download an installer and launch it.

    Returns True if download succeeded and process was launched.
    Pass args=["/S"] for a silent NSIS install (used by auto-update).
    """
    tmp_dir = tempfile.gettempdir()
    dest_path = os.path.join(tmp_dir, file_name)
    if not download_file(url, dest_path):
        return False
    launch_installer(dest_path, args=args)
    return True
