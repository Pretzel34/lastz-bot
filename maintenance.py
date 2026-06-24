"""
maintenance.py
==============
Tracks per-farm run counts and performs cleanup after every N runs.

ADB cleanup (temp files, game cache, system cache) runs while the emulator
is still active.  Emulator-level disk cleanup runs after the instance stops.

Adding support for a new emulator:
  1. Add an elif branch in _run_disk_cleanup() calling a new _<emu>_disk_clean() method.
  2. The ADB cleanup (_run_adb_cleanup) is emulator-agnostic and needs no changes.
"""

import json
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

GAME_PACKAGE = "com.readygo.barrel.gp"
STATE_FILE   = "maintenance_state.json"

log = logging.getLogger("Maintenance")


class MaintenanceManager:
    """
    Tracks how many bot-run cycles each farm instance has completed and
    triggers cleanup when the count reaches every_n_runs.

    State is persisted in maintenance_state.json so counts survive restarts.

    Typical call order per farm (from gui.py):
        maint = MaintenanceManager(...)
        needs_clean = maint.is_due(farm_port, every_n_runs)
        maint.record_run(farm_port)           # always increment
        if needs_clean:
            maint.run_adb_cleanup(bot)        # while emulator is running
            # --- caller stops emulator here ---
            maint.run_disk_cleanup(install_path, instance_index)
            maint.reset_count(farm_port)
    """

    def __init__(
        self,
        emulator_type: str = "MEmu",
        log_callback:  Optional[Callable[[str], None]] = None,
    ):
        self.emulator_type = emulator_type
        self._cb           = log_callback or (lambda m: log.info(m))
        self._state        = self._load_state()

    # ── State ─────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"farms": {}}

    def _save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            self._cb(f"[Maintenance] Could not save state: {e}")

    def _farm(self, port: int) -> dict:
        key = str(port)
        return self._state["farms"].setdefault(key, {
            "run_count": 0, "last_cleanup": None})

    def run_count(self, port: int) -> int:
        return self._farm(port).get("run_count", 0)

    def record_run(self, port: int) -> int:
        """Increment run counter. Returns the new count."""
        f = self._farm(port)
        f["run_count"] = f.get("run_count", 0) + 1
        self._save_state()
        return f["run_count"]

    def is_due(self, port: int, every_n: int) -> bool:
        """True when the *next* record_run() call will hit the threshold."""
        return every_n > 0 and (self.run_count(port) + 1) % every_n == 0

    def reset_count(self, port: int):
        f = self._farm(port)
        f["run_count"]    = 0
        f["last_cleanup"] = datetime.now().isoformat()
        self._save_state()

    # ── ADB cleanup (while emulator is running) ───────────────────────────

    def run_adb_cleanup(self, bot) -> None:
        """
        Runs inside the active emulator via ADB.
        Safe to call while the game is running — does not touch user data.
        """
        self._cb("[Maintenance] Running ADB cleanup...")
        steps = [
            ("Clear ADB temp files",   "rm -rf /data/local/tmp/*"),
            ("Clear system cache",     "rm -rf /cache/*"),
        ]
        for label, cmd in steps:
            try:
                out = (bot.shell(cmd) or "").strip()
                self._cb(f"[Maintenance]   ✓ {label}" + (f": {out}" if out else ""))
            except Exception as e:
                self._cb(f"[Maintenance]   ✗ {label}: {e}")

    # ── Disk cleanup (after emulator has stopped) ─────────────────────────

    def run_disk_cleanup(self, install_path: str, instance_index: int) -> None:
        """
        Runs emulator-level disk cleanup after the instance has been stopped.
        install_path — folder containing the emulator CLI executable.
        instance_index — 0-based CLI index of the instance.
        """
        self._cb("[Maintenance] Running disk cleanup...")
        if self.emulator_type == "MEmu":
            self._memu_disk_clean(install_path, instance_index)
        elif self.emulator_type == "Nox":
            self._nox_disk_clean(install_path, instance_index)
        elif self.emulator_type == "LDPlayer":
            self._ldplayer_disk_clean(install_path, instance_index)
        else:
            self._cb(f"[Maintenance]   No disk-clean command defined for {self.emulator_type}")

    def _run_cli(self, cli: str, *args, timeout: int = 120) -> tuple[int, str]:
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            r = subprocess.run(
                [cli] + [str(a) for a in args],
                capture_output=True, text=True,
                timeout=timeout, startupinfo=si,
            )
            return r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            return -1, "timeout"
        except FileNotFoundError:
            return -1, f"not found: {cli}"
        except Exception as e:
            return -1, str(e)

    def _memu_disk_clean(self, install_path: str, instance_index: int) -> None:
        """
        MEmu: memuc.exe cleandisk -i <index>
        Corresponds to the "Clean" option in MEmu Multi-Instance Manager.
        Requires the instance to already be stopped.
        """
        cli = str(Path(install_path) / "memuc.exe")
        code, out = self._run_cli(cli, "cleandisk", "-i", str(instance_index))
        if code == 0:
            self._cb(f"[Maintenance]   ✓ MEmu disk clean (instance {instance_index})" +
                     (f": {out}" if out else ""))
        else:
            self._cb(f"[Maintenance]   ✗ MEmu disk clean failed (code {code}): {out}")
            if "not found" in out:
                self._cb(f"[Maintenance]     Looked for memuc.exe at: {cli}")
            elif code != 0:
                # cleandisk may not exist in all MEmu versions; log a hint
                self._cb("[Maintenance]     If this command is unrecognised, check your MEmu "
                         "version — the equivalent is 'Clean' under the ⋯ menu in "
                         "Multi-Instance Manager.")

    def _nox_disk_clean(self, install_path: str, instance_index: int) -> None:
        """
        Nox: NoxConsole does not expose a disk-clean CLI command.
        ADB cleanup (run_adb_cleanup) is sufficient for Nox.
        """
        self._cb("[Maintenance]   Nox: no CLI disk-clean available — ADB cleanup only")

    def _ldplayer_disk_clean(self, install_path: str, instance_index: int) -> None:
        """
        LDPlayer: ldconsole.exe does not expose a disk-clean CLI command.
        ADB cleanup (run_adb_cleanup) is sufficient for LDPlayer.
        """
        self._cb("[Maintenance]   LDPlayer: no CLI disk-clean available — ADB cleanup only")
