"""
launcher.py
===========
Handles MEmu instance launching and app startup.
Uses memuc.exe (MEmu Command Line) to control instances by index.

MEmu index is 0-based:
    MEmu-1 = index 0
    MEmu-2 = index 1
    MEmu-3 = index 2
    etc.
"""

import time
import subprocess
import logging
from typing import Optional, Callable

# ── Constants ────────────────────────────────────────────────────────────────

MEMUC_PATH   = r"C:\Program Files\Microvirt\MEmu\memuc.exe"
LASTZ_PKG    = "com.readygo.barrel.gp"
LASTZ_ACT    = "com.im30.aps.debug.UnityPlayerActivityCustom"

# MEmu-1 = index 0, port 21503
# MEmu-2 = index 1, port 21513  (MEmu increments by 10)
# MEmu-3 = index 2, port 21507
# etc.
BASE_PORT    = 21503
PORT_STEP    = 10

BOOT_TIMEOUT    = 180   # seconds to wait for emulator to boot
CONNECT_TIMEOUT = 60    # seconds to wait for ADB connection
GAME_TIMEOUT    = 90    # seconds to wait for Last Z to load


def index_to_port(index: int) -> int:
    """Convert MEmu 0-based index to ADB port."""
    return BASE_PORT + index * PORT_STEP


def port_to_index(port: int) -> int:
    """Convert ADB port to MEmu 0-based index."""
    return (port - BASE_PORT) // PORT_STEP


# ── Launcher ─────────────────────────────────────────────────────────────────

class MEmuLauncher:
    """
    Launches MEmu instances by index and starts Last Z automatically.

    Usage:
        launcher = MEmuLauncher()
        ok = launcher.launch_and_connect(index=0)
        if ok:
            bot = launcher.get_bot(index=0)
            # bot is ready with Last Z open
    """

    def __init__(
        self,
        memuc_path:   str = MEMUC_PATH,
        package:      str = LASTZ_PKG,
        activity:     str = LASTZ_ACT,
        log_callback: Optional[Callable[[str], None]] = None,
        boot_timeout: int = BOOT_TIMEOUT,
        game_timeout: int = GAME_TIMEOUT,
    ):
        self.memuc_path   = memuc_path
        self.boot_timeout = boot_timeout
        self.game_timeout = game_timeout
        self.package    = package
        self.activity   = activity
        self.log_callback = log_callback
        self.logger = logging.getLogger("MEmuLauncher")
        self._bots: dict[int, object] = {}  # index -> ADBWrapper

    # ── Public API ────────────────────────────────────────────────────────

    def launch_and_connect(
        self,
        index: int,
        wait_for_game: bool = True,
    ) -> bool:
        """
        Full sequence:
          1. Start MEmu instance by index
          2. Wait for boot
          3. Connect ADB
          4. Launch Last Z
          5. Wait for game to load
          6. Return ready ADBWrapper

        Returns True if everything succeeded.
        """
        port = index_to_port(index)
        self._log(f"[Launcher] Starting MEmu index {index} (port {port})...")

        # Step 1: Check if already running
        if self._is_instance_running(index):
            self._log(f"[Launcher] MEmu-{index+1} already running, skipping start")
        else:
            # Start the instance
            ok = self._memuc_start(index)
            if not ok:
                self._log(f"[Launcher] Failed to start MEmu-{index+1}", "error")
                return False

            # Wait for boot
            self._log(f"[Launcher] Waiting for MEmu-{index+1} to boot...")
            if not self._wait_for_boot(index, timeout=self.boot_timeout):
                self._log(f"[Launcher] MEmu-{index+1} boot timeout", "error")
                return False

        # Step 2: Connect ADB
        self._log(f"[Launcher] Connecting ADB on port {port}...")
        bot = self._connect_adb(port)
        if bot is None:
            self._log(f"[Launcher] ADB connection failed for port {port}", "error")
            return False

        self._bots[index] = bot
        self._log(f"[Launcher] Connected: {bot.info.model} Android {bot.info.android_version}")

        # Step 3: Launch Last Z
        self._log(f"[Launcher] Launching Last Z ({self.package})...")
        bot.launch_app(self.package, self.activity)

        if wait_for_game:
            self._log("[Launcher] Waiting for Last Z to load...")
            if not self._wait_for_game(bot, timeout=self.game_timeout):
                self._log("[Launcher] Last Z load timeout — continuing anyway", "warn")

        self._log(f"[Launcher] ✓ MEmu-{index+1} ready with Last Z running", "success")
        return True

    def stop_instance(self, index: int):
        """Stop a MEmu instance by index."""
        self._log(f"[Launcher] Stopping MEmu-{index+1}...")
        self._memuc("stop", "-i", str(index))

    def get_bot(self, index: int):
        """Return the ADBWrapper for a launched instance, or None."""
        return self._bots.get(index)

    def is_running(self, index: int) -> bool:
        return self._is_instance_running(index)

    # ── memuc Commands ────────────────────────────────────────────────────

    def _memuc(self, *args) -> tuple[int, str]:
        """Run a memuc.exe command. Returns (returncode, output)."""
        try:
            # Quote the path to handle spaces, build as string for shell=True
            quoted = f'"{self.memuc_path}"'
            cmd = quoted + " " + " ".join(str(a) for a in args)
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=30,
                shell=True,
                creationflags=0x08000000
            )
            output = (result.stdout + result.stderr).strip()
            return result.returncode, output
        except FileNotFoundError:
            self._log(f"[Launcher] memuc.exe not found at: {self.memuc_path}", "error")
            return -1, "memuc not found"
        except subprocess.TimeoutExpired:
            return -1, "timeout"
        except Exception as e:
            return -1, str(e)

    def _memuc_start(self, index: int) -> bool:
        """Start a MEmu instance by index."""
        code, out = self._memuc("start", "-i", str(index))
        self._log(f"[Launcher] memuc start (code={code}): {out.strip()}")
        if code != 0:
            self._log(f"[Launcher] ⚠ memuc.exe path: {self.memuc_path}", "error")
            import os
            self._log(f"[Launcher] ⚠ memuc.exe exists: {os.path.exists(self.memuc_path)}", "error")
        return code == 0

    def _is_instance_running(self, index: int) -> bool:
        """Check if a MEmu instance is currently running via memuc listvms."""
        # "isrunning" is not a valid memuc command — use listvms instead
        # listvms output: index,name,status  where status is "running" or "stopped"
        code, out = self._memuc("listvms")
        self._log(f"[Launcher] listvms (code={code}): {out.strip()}")
        if code != 0 or not out.strip():
            return False
        # Each line: index,"name",status
        for line in out.strip().splitlines():
            parts = line.split(",")
            if len(parts) >= 3:
                try:
                    vm_index = int(parts[0].strip())
                    status   = parts[-1].strip().lower()
                    if vm_index == index and status == "running":
                        return True
                except ValueError:
                    continue
        return False

    def _wait_for_boot(self, index: int, timeout: int = BOOT_TIMEOUT) -> bool:
        """
        Wait for MEmu instance to finish booting.
        Checks boot animation completion via ADB.
        """
        port = index_to_port(index)
        start = time.time()

        while time.time() - start < timeout:
            # First try: connect ADB and check boot property
            try:
                result = subprocess.run(
                    ["adb", "connect", f"127.0.0.1:{port}"],
                    capture_output=True, text=True, timeout=5
                )
                if "connected" in result.stdout.lower():
                    # Check if boot animation finished
                    prop = subprocess.run(
                        ["adb", "-s", f"127.0.0.1:{port}",
                         "shell", "getprop", "sys.boot_completed"],
                        capture_output=True, text=True, timeout=5
                    )
                    if prop.stdout.strip() == "1":
                        self._log(f"[Launcher] MEmu-{index+1} boot complete")
                        time.sleep(3)  # extra settle time
                        return True
            except Exception:
                pass
            time.sleep(3)

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
        """
        Wait for Last Z to finish loading by checking the foreground app
        and optionally looking for a known game UI template.
        """
        start = time.time()
        while time.time() - start < timeout:
            try:
                focus = bot.shell(
                    "dumpsys window windows | grep mCurrentFocus"
                )
                if self.package in focus:
                    # Game is in foreground — give it extra time to render
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


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("MEmu Launcher Test")
    print("=" * 40)

    def log(msg, level="info"):
        print(f"  [{level.upper()}] {msg}")

    launcher = MEmuLauncher(log_callback=log)

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
        print("\nMEmu-1 is not running.")
        print("Start it from the Multi-Instance Manager first,")
        print("or call launcher.launch_and_connect(0) to auto-start it.")
