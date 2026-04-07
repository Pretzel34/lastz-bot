"""
launcher.py
===========
Handles emulator instance launching and app startup.
Supports MEmu, LDPlayer, and Nox via a data-driven profile registry.

Emulator index is 0-based:
    Instance 1 = index 0
    Instance 2 = index 1
    etc.
"""

import os
import shutil
import time
import subprocess
import logging
from typing import Optional, Callable

# ── Constants ────────────────────────────────────────────────────────────────

MEMUC_PATH   = r"C:\Program Files\Microvirt\MEmu\memuc.exe"
LASTZ_PKG    = "com.readygo.barrel.gp"
LASTZ_ACT    = "com.im30.aps.debug.UnityPlayerActivityCustom"

BOOT_TIMEOUT    = 180   # seconds to wait for emulator to boot
CONNECT_TIMEOUT = 60    # seconds to wait for ADB connection
GAME_TIMEOUT    = 90    # seconds to wait for Last Z to load


# ── Emulator Profiles ─────────────────────────────────────────────────────────
# Each profile defines:
#   port_base / port_step   — ADB port formula: port = base + index * step
#   process_name            — Windows process for window tiling
#   window_title            — title pattern for zoom_out ({n} = 1-based instance)
#   window_title_bare       — fallback bare title
#   cli_exe                 — executable name inside install_path
#   cli_start/stop/list     — arg templates ({index} = 0-based index)
#   zoom_key                — pyautogui key for zoom shortcut, or None

EMULATOR_PROFILES = {
    "MEmu": {
        "port_base":         21503,
        "port_step":         10,
        "process_name":      "MEmu.exe",
        "window_title":      "(MEmu - {n})",
        "window_title_bare": "MEmu",
        "cli_exe":           "memuc.exe",
        "adb_exe":           "adb.exe",
        "cli_start":         ["start", "-i", "{index}"],
        "cli_stop":          ["stop",  "-i", "{index}"],
        "cli_list":          ["listvms"],
        "zoom_key":          "f3",
    },
    "LDPlayer": {
        "port_base":         5554,
        "port_step":         2,
        "process_name":      "LDPlayer.exe",
        "window_title":      "LDPlayer - {n}",
        "window_title_bare": "LDPlayer",
        "cli_exe":           "ldconsole.exe",
        "adb_exe":           "adb.exe",
        "cli_start":         ["launch", "--index", "{index}"],
        "cli_stop":          ["quit",   "--index", "{index}"],
        "cli_list":          ["list2"],
        "zoom_key":          None,
    },
    "Nox": {
        "port_base":         62001,
        "port_step":         24,
        "process_name":      "Nox.exe",
        "window_title":      "NoxPlayer",
        "window_title_bare": "NoxPlayer",
        "cli_exe":           "NoxConsole.exe",
        "adb_exe":           "nox_adb.exe",
        "cli_start":         ["launch", "-index:{index}"],
        "cli_stop":          ["quit",   "-index:{index}"],
        "cli_list":          ["list"],
        "zoom_key":          None,
    },
}


def get_profile(emulator_type: str) -> dict:
    """Return the emulator profile, defaulting to MEmu if unknown."""
    return EMULATOR_PROFILES.get(emulator_type, EMULATOR_PROFILES["MEmu"])


# Common subfolder candidates per emulator, verified by the CLI executable.
_INSTALL_SEARCH = {
    "MEmu": {
        "verify_exe": "memuc.exe",
        "subdirs": [
            r"Program Files\Microvirt\MEmu",
            r"Program Files (x86)\Microvirt\MEmu",
            r"Microvirt\MEmu",
            r"MEmu",
        ],
        "example": r"D:\Program Files\Microvirt\MEmu",
    },
    "LDPlayer": {
        "verify_exe": "ldconsole.exe",
        "subdirs": [
            r"LDPlayer\LDPlayer9",
            r"LDPlayer\LDPlayer4",
            r"Program Files\LDPlayer\LDPlayer9",
            r"Program Files\LDPlayer\LDPlayer4",
            r"LDPlayer9",
            r"LDPlayer4",
        ],
        "example": r"D:\LDPlayer\LDPlayer9",
    },
    "Nox": {
        "verify_exe": "NoxConsole.exe",
        "subdirs": [
            r"Program Files (x86)\Nox\bin",
            r"Program Files\Nox\bin",
            r"BigNox\NoxVM",
            r"Nox\bin",
        ],
        "example": r"D:\Program Files (x86)\Nox\bin",
    },
}


def find_emulator_install(emulator_type: str) -> str | None:
    """
    Search common drive letters and install locations for the given emulator.
    Verifies the path by checking the CLI executable exists inside it.
    Returns the install folder path if found, None otherwise.
    """
    spec = _INSTALL_SEARCH.get(emulator_type)
    if not spec:
        return None
    for drive in ["C", "D", "E", "F", "G"]:
        for subdir in spec["subdirs"]:
            candidate = os.path.join(f"{drive}:\\", subdir)
            if os.path.isfile(os.path.join(candidate, spec["verify_exe"])):
                return candidate
    return None


def emulator_path_example(emulator_type: str) -> str:
    """Return a human-readable example install path for the given emulator."""
    return _INSTALL_SEARCH.get(emulator_type, {}).get("example", "")


def index_to_port(index: int, emulator_type: str = "MEmu") -> int:
    """Convert 0-based instance index to ADB port."""
    p = get_profile(emulator_type)
    return p["port_base"] + index * p["port_step"]


def port_to_index(port: int, emulator_type: str = "MEmu") -> int:
    """Convert ADB port to 0-based instance index."""
    p = get_profile(emulator_type)
    return (port - p["port_base"]) // p["port_step"]


# ── Launcher ─────────────────────────────────────────────────────────────────

class EmulatorLauncher:
    """
    Launches emulator instances by index and starts Last Z automatically.
    Supports MEmu, LDPlayer, and Nox via EMULATOR_PROFILES.

    Usage:
        launcher = EmulatorLauncher(emulator_type="MEmu", install_path=r"C:\\...\\MEmu")
        ok = launcher.launch_and_connect(index=0)
        if ok:
            bot = launcher.get_bot(index=0)
    """

    def __init__(
        self,
        emulator_type: str = "MEmu",
        install_path:  str = None,
        memuc_path:    str = None,   # backwards compat: full path to memuc.exe
        adb_path:      str = "adb",
        package:       str = LASTZ_PKG,
        activity:      str = LASTZ_ACT,
        log_callback:  Optional[Callable[[str], None]] = None,
        boot_timeout:  int = BOOT_TIMEOUT,
        game_timeout:  int = GAME_TIMEOUT,
    ):
        self.emulator_type = emulator_type
        self.profile       = get_profile(emulator_type)
        self.boot_timeout  = boot_timeout
        self.game_timeout  = game_timeout
        self.package       = package
        self.activity      = activity
        self.log_callback  = log_callback
        self.logger        = logging.getLogger("EmulatorLauncher")
        self._bots: dict[int, object] = {}  # index -> ADBWrapper

        # Determine CLI path
        if memuc_path:
            # Legacy: caller passed full path to memuc.exe
            self.install_path = os.path.dirname(memuc_path)
            self.cli_path     = memuc_path
        else:
            defaults = {
                "MEmu":     r"C:\Program Files\Microvirt\MEmu",
                "LDPlayer": r"C:\LDPlayer\LDPlayer4",
                "Nox":      r"C:\Program Files\Nox\bin",
            }
            self.install_path = install_path or defaults.get(emulator_type, "")
            self.cli_path     = os.path.join(self.install_path, self.profile["cli_exe"])

        # Resolve ADB path — if "adb" not in PATH, fall back to emulator-bundled adb
        if adb_path == "adb" and not shutil.which("adb"):
            bundled = os.path.join(self.install_path, self.profile.get("adb_exe", "adb.exe"))
            if os.path.exists(bundled):
                adb_path = bundled
                self.logger.info(f"[Launcher] 'adb' not in PATH — using bundled ADB: {bundled}")

        self.adb_path = adb_path

    # ── Public API ────────────────────────────────────────────────────────

    def find_all_instances(self) -> list:
        """
        Query the emulator CLI and return (cli_index, port, is_running) for every
        known instance, whether running or stopped.
        """
        args = self.profile["cli_list"]
        code, out = self._run_cli(*args)
        results = []
        if code != 0 or not out.strip():
            return results

        emu = self.emulator_type
        p   = self.profile
        for line in out.strip().splitlines():
            parts = line.split(",")
            try:
                cli_idx = int(parts[0].strip())
                running = False
                if emu == "MEmu":
                    # listvms format: index,name,pid1,flag,pid2 — running if pid1 > 0
                    running = len(parts) >= 3 and int(parts[2].strip() or 0) > 0
                elif emu == "LDPlayer":
                    running = len(parts) >= 5 and int(parts[4].strip() or 0) > 0
                elif emu == "Nox":
                    running = len(parts) >= 6 and int(parts[5].strip() or 0) > 0
                port = p["port_base"] + cli_idx * p["port_step"]
                results.append((cli_idx, port, running))
            except (ValueError, IndexError):
                continue
        return results

    def find_running_instances(self) -> list:
        """
        Query the emulator CLI and return a list of (cli_index, port) for all
        running instances.  cli_index is the index shown by the list command
        (used directly in launch/stop args); port is calculated from it.
        """
        return [(i, p) for i, p, running in self.find_all_instances() if running]

    def launch_and_connect(
        self,
        index: int,
        wait_for_game: bool = True,
    ) -> bool:
        """
        Full sequence:
          1. Determine which instance to use (auto-detect if calculated port
             isn't running but another instance is)
          2. Start emulator instance if not already running
          3. Wait for boot
          4. Connect ADB and verify the connection
          5. Launch Last Z
          6. Wait for game to load

        Returns True if everything succeeded.
        """
        calculated_port = index_to_port(index, self.emulator_type)
        cli_index       = index

        self._log(f"[Launcher] Starting {self.emulator_type} index {index} (port {calculated_port})...")

        # ── Step 1: Check if the correct instance is already running ─────────
        all_instances = self.find_all_instances()
        running       = [(i, p) for i, p, r in all_instances if r]
        self._log(f"[Launcher] All instances: {all_instances}")
        self._log(f"[Launcher] Running (per CLI): {running if running else 'none'}")

        port = calculated_port

        if not any(i == cli_index for i, _, _ in all_instances):
            self._log(f"[Launcher] CLI index {cli_index} not found in instance list", "error")
            return False

        target_running = any(i == cli_index and r for i, _, r in all_instances)

        if target_running and calculated_port in [p for _, p, r in all_instances if r]:
            self._log(f"[Launcher] Instance CLI-{cli_index} already running on port {port}")
        elif target_running:
            self._log(f"[Launcher] Instance CLI-{cli_index} running but port mismatch — recalculated port {port}", "warn")

        # Check if ADB is actually reachable on the correct port before skipping the start step.
        needs_start = not target_running or not self._verify_adb_connected(port)

        if needs_start:
            if target_running:
                self._log(
                    f"[Launcher] CLI reports CLI-{cli_index} running but ADB not reachable on port {port} — "
                    f"starting instance...", "warn"
                )
            self._log(f"[Launcher] Starting CLI index {cli_index} (port {port})...")
            ok = self._start_instance_by_cli(cli_index)
            if not ok:
                self._log(f"[Launcher] Failed to start CLI index {cli_index}", "error")
                return False

            self._log(f"[Launcher] Waiting for {self.emulator_type} to boot...")
            if not self._wait_for_boot_port(port, timeout=self.boot_timeout):
                self._log(f"[Launcher] Boot timeout on port {port}", "error")
                return False
        else:
            self._log(f"[Launcher] Instance CLI-{cli_index} already running and ADB reachable on port {port}")

        # ── Step 2: Connect ADB ────────────────────────────────────────────
        self._log(f"[Launcher] Connecting ADB on port {port}...")
        bot = self._connect_adb(port)
        if bot is None:
            self._log(f"[Launcher] ADB connection failed for port {port}", "error")
            return False

        # ── Step 3: Verify the connection is real ─────────────────────────
        if not self._verify_adb_connected(port):
            self._log(f"[Launcher] ADB connected but device not confirmed in 'adb devices' for port {port}", "error")
            return False

        self._bots[index] = bot
        self._log(f"[Launcher] Connected: {bot.info.model} Android {bot.info.android_version}")

        # ── Step 4: Launch Last Z ──────────────────────────────────────────
        self._log(f"[Launcher] Launching Last Z ({self.package})...")
        bot.launch_app(self.package, self.activity)

        if wait_for_game:
            self._log("[Launcher] Waiting for Last Z to load...")
            if not self._wait_for_game(bot, timeout=self.game_timeout):
                self._log("[Launcher] Last Z failed to load within timeout — aborting", "error")
                return False

        # ── Final check: confirm ADB device is responsive ─────────────────
        try:
            screen = bot.screenshot()
            if screen is None:
                self._log("[Launcher] ADB connected but screenshot returned None — device not ready", "error")
                return False
        except Exception as e:
            self._log(f"[Launcher] ADB screenshot check failed: {e}", "error")
            return False

        self._log(f"[Launcher] ✓ {self.emulator_type} CLI-{cli_index} (port {port}) ready", "success")
        return True

    def stop_instance(self, index: int):
        """Stop an emulator instance by index."""
        self._log(f"[Launcher] Stopping {self.emulator_type}-{index+1}...")
        args = [a.format(index=index) for a in self.profile["cli_stop"]]
        self._run_cli(*args)

    def get_bot(self, index: int):
        """Return the ADBWrapper for a launched instance, or None."""
        return self._bots.get(index)

    def is_running(self, index: int) -> bool:
        return self._is_instance_running(index)

    # ── CLI Commands ──────────────────────────────────────────────────────

    def _run_cli(self, *args, timeout: int = 30) -> tuple[int, str]:
        """Run the emulator CLI tool. Returns (returncode, output)."""
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            result = subprocess.run(
                [self.cli_path] + [str(a) for a in args],
                capture_output=True, text=True, timeout=timeout,
                startupinfo=si
            )
            output = (result.stdout + result.stderr).strip()
            return result.returncode, output
        except FileNotFoundError:
            self._log(f"[Launcher] CLI not found at: {self.cli_path}", "error")
            return -1, "cli not found"
        except subprocess.TimeoutExpired:
            return -1, "timeout"
        except Exception as e:
            return -1, str(e)

    def _start_instance(self, index: int) -> bool:
        """Start an emulator instance by 0-based internal index (converts to CLI index for Nox)."""
        # For Nox the CLI list is not 0-based — prefer _start_instance_by_cli when cli_index is known
        return self._start_instance_by_cli(index)

    def _start_instance_by_cli(self, cli_index: int) -> bool:
        """Start an emulator instance using the raw CLI index (as shown in the list command)."""
        args = [a.format(index=cli_index) for a in self.profile["cli_start"]]
        code, out = self._run_cli(*args, timeout=90)
        self._log(f"[Launcher] start CLI-{cli_index} (code={code}): {out.strip()}")
        if code != 0:
            self._log(f"[Launcher] ⚠ CLI path: {self.cli_path}", "error")
            self._log(f"[Launcher] ⚠ CLI exists: {os.path.exists(self.cli_path)}", "error")
        return code == 0

    def _is_instance_running(self, index: int) -> bool:
        """Check if an emulator instance is running. Tries CLI first, then tasklist fallback."""
        args = self.profile["cli_list"]
        code, out = self._run_cli(*args)
        self._log(f"[Launcher] list (code={code}): {out.strip()}")

        if code == 0 and out.strip():
            emu = self.emulator_type
            for line in out.strip().splitlines():
                parts = line.split(",")
                if emu == "MEmu":
                    # listvms format: index,name,pid1,flag,pid2 — running if pid1 > 0
                    if len(parts) >= 3:
                        try:
                            if int(parts[0].strip()) == index:
                                pid = int(parts[2].strip() or 0)
                                return pid > 0
                        except ValueError:
                            continue
                elif emu == "LDPlayer":
                    # list2 format: index,title,top_window,bind_window,pid,pid2
                    if len(parts) >= 5:
                        try:
                            if int(parts[0].strip()) == index:
                                pid = int(parts[4].strip()) if parts[4].strip().isdigit() else 0
                                return pid > 0
                        except ValueError:
                            continue
                elif emu == "Nox":
                    # format: index,name,title,hwnd1,hwnd2,pid1,pid2
                    # pid1 (parts[5]) is 0 when stopped, >0 when running
                    if len(parts) >= 6:
                        try:
                            if int(parts[0].strip()) == index:
                                pid = int(parts[5].strip()) if parts[5].strip().isdigit() else 0
                                return pid > 0
                        except ValueError:
                            continue

        # Fallback: check if the emulator process is alive via tasklist
        proc = self.profile["process_name"]
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {proc}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            return proc.lower() in result.stdout.lower()
        except Exception:
            return False

    def _wait_for_boot(self, index: int, timeout: int = BOOT_TIMEOUT) -> bool:
        """Wait for boot using 0-based index (converts to port)."""
        port = index_to_port(index, self.emulator_type)
        return self._wait_for_boot_port(port, timeout=timeout)

    def _wait_for_boot_port(self, port: int, timeout: int = BOOT_TIMEOUT) -> bool:
        """Wait for the emulator to finish booting by polling ADB on the given port."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                result = subprocess.run(
                    [self.adb_path, "connect", f"127.0.0.1:{port}"],
                    capture_output=True, text=True, timeout=5
                )
                if "connected" in result.stdout.lower():
                    prop = subprocess.run(
                        [self.adb_path, "-s", f"127.0.0.1:{port}",
                         "shell", "getprop", "sys.boot_completed"],
                        capture_output=True, text=True, timeout=5
                    )
                    if prop.stdout.strip() == "1":
                        self._log(f"[Launcher] Boot complete on port {port}")
                        time.sleep(3)
                        return True
            except Exception:
                pass
            time.sleep(3)
        return False

    def _verify_adb_connected(self, port: int) -> bool:
        """Confirm 127.0.0.1:port is listed as 'device' in adb devices (not offline/unauthorized)."""
        serial = f"127.0.0.1:{port}"
        try:
            result = subprocess.run(
                [self.adb_path, "devices"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == serial and parts[1] == "device":
                    self._log(f"[Launcher] ADB verified: {serial} is online")
                    return True
            self._log(f"[Launcher] ADB verify failed — {serial} not found in: {result.stdout.strip()}", "warn")
            return False
        except Exception as e:
            self._log(f"[Launcher] ADB verify error: {e}", "warn")
            return False

    def _connect_adb(self, port: int, timeout: int = CONNECT_TIMEOUT):
        """Connect ADB and return an ADBWrapper, or None on failure."""
        try:
            from adb_wrapper import ADBWrapper
        except ImportError:
            self._log("[Launcher] adb_wrapper.py not found", "error")
            return None

        start = time.time()
        attempt = 0
        while time.time() - start < timeout:
            attempt += 1
            bot = ADBWrapper(port=port)
            self._log(f"[Launcher] ADB connect attempt {attempt} on port {port}...")
            if bot.connect(timeout=5):
                return bot
            time.sleep(3)

        self._log(f"[Launcher] ADB connect timed out after {timeout}s on port {port}", "error")
        return None

    def _wait_for_game(self, bot, timeout: int = GAME_TIMEOUT) -> bool:
        """Wait for Last Z to finish loading by checking the foreground app."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                focus = bot.shell(
                    "dumpsys window windows | grep mCurrentFocus"
                )
                if self.package in focus:
                    time.sleep(5)
                    return True
            except Exception:
                pass
            time.sleep(3)
        return False

    # ── Logging ───────────────────────────────────────────────────────────

    def _log(self, message: str, level: str = "info"):
        self.logger.info(message)
        if self.log_callback:
            self.log_callback(message, level)


# Backwards-compatibility alias
MEmuLauncher = EmulatorLauncher


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Emulator Launcher Test")
    print("=" * 40)

    def log(msg, level="info"):
        print(f"  [{level.upper()}] {msg}")

    launcher = EmulatorLauncher(emulator_type="MEmu", log_callback=log)

    print("\nChecking if MEmu-1 (index 0) is running...")
    running = launcher.is_running(0)
    print(f"  Running: {running}")

    if running:
        print("\nAttempting to connect and launch Last Z...")
        ok = launcher.launch_and_connect(index=0)
        if ok:
            bot = launcher.get_bot(0)
            print(f"\n✓ Ready! Device: {bot.info.model}")
            print(f"  Screen: {bot.info.screen_width}x{bot.info.screen_height}")
        else:
            print("✗ Launch failed")
    else:
        print(f"\n{launcher.emulator_type}-1 is not running.")
        print("Start it from the Multi-Instance Manager first,")
        print("or call launcher.launch_and_connect(0) to auto-start it.")
