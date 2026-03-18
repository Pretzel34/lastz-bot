"""
adb_wrapper.py
==============
Core ADB interface for Android emulator bot automation.
Wraps adbutils to provide clean, bot-friendly methods for
connecting to MEmu instances, taking screenshots, and sending input.

Requirements:
    pip install adbutils pillow opencv-python
"""

import sys
import time
import subprocess
from typing import Optional
from dataclasses import dataclass, field


def _print(msg: str):
    """Print safely on Windows — replaces chars that cp1252 can't encode."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))

try:
    import adbutils
    from adbutils import AdbClient, AdbDevice
except ImportError:
    raise ImportError("Run: pip install adbutils")

try:
    from PIL import Image
    import io
except ImportError:
    raise ImportError("Run: pip install pillow")


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class DeviceInfo:
    serial: str       # e.g. "127.0.0.1:5555"
    port: int
    model: str = ""
    android_version: str = ""
    screen_width: int = 0
    screen_height: int = 0


# ---------------------------------------------------------------------------
# ADB Wrapper
# ---------------------------------------------------------------------------

class ADBWrapper:
    """
    Wraps a single ADB device connection with bot-friendly methods.

    Usage:
        bot = ADBWrapper(port=5555)
        bot.connect()
        bot.launch_app("com.example.mygame")
        bot.tap(540, 960)
        screenshot = bot.screenshot()
        screenshot.save("screen.png")
    """

    DEFAULT_HOST = "127.0.0.1"

    def __init__(self, port: int = 5555, adb_host: str = DEFAULT_HOST):
        self.port = port
        self.adb_host = adb_host
        self.serial = f"{adb_host}:{port}"
        self._client: Optional[AdbClient] = None
        self._device: Optional[AdbDevice] = None
        self.info: Optional[DeviceInfo] = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, timeout: int = 10) -> bool:
        """
        Connect to the emulator instance at self.port.
        Returns True on success, False on failure.
        """
        try:
            self._client = AdbClient(host="127.0.0.1", port=5037)
            self._client.connect(self.serial, timeout=timeout)
            self._device = self._client.device(self.serial)
            self.info = self._fetch_device_info()
            _print(f"[ADB] Connected to {self.serial} | "
                  f"{self.info.model} | Android {self.info.android_version} | "
                  f"{self.info.screen_width}x{self.info.screen_height}")
            return True
        except Exception as e:
            _print(f"[ADB] Connection failed for {self.serial}: {e}")
            return False

    def disconnect(self):
        """Disconnect from the device."""
        if self._client:
            try:
                self._client.disconnect(self.serial)
                _print(f"[ADB] Disconnected from {self.serial}")
            except Exception:
                pass
        self._device = None

    def is_connected(self) -> bool:
        """Check if the device is still responding."""
        try:
            self._require_device()
            self._device.shell("echo ok")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    def screenshot(self) -> Image.Image:
        """
        Capture the current screen and return a PIL Image.
        This is the primary input for your vision layer.
        """
        self._require_device()
        raw = self._device.screenshot()
        # adbutils returns a PIL Image directly
        if isinstance(raw, Image.Image):
            return raw
        # fallback: raw bytes
        return Image.open(io.BytesIO(raw))

    def screenshot_save(self, path: str) -> str:
        """Capture screenshot and save to file. Returns the path."""
        img = self.screenshot()
        img.save(path)
        _print(f"[ADB] Screenshot saved → {path}")
        return path

    # ------------------------------------------------------------------
    # Touch Input
    # ------------------------------------------------------------------

    def tap(self, x: int, y: int):
        """Send a tap at (x, y) screen coordinates."""
        self._require_device()
        self._device.shell(f"input tap {x} {y}")
        _print(f"[ADB] Tap ({x}, {y})")

    def tap_center(self):
        """Tap the center of the screen."""
        if self.info:
            self.tap(self.info.screen_width // 2, self.info.screen_height // 2)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300):
        """
        Swipe from (x1,y1) to (x2,y2).
        duration_ms controls swipe speed (higher = slower/more human-like).
        """
        self._require_device()
        self._device.shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")
        _print(f"[ADB] Swipe ({x1},{y1}) → ({x2},{y2}) over {duration_ms}ms")

    def long_press(self, x: int, y: int, duration_ms: int = 1000):
        """Long press at (x, y) for duration_ms milliseconds."""
        self.swipe(x, y, x, y, duration_ms)
        _print(f"[ADB] Long press ({x}, {y}) for {duration_ms}ms")

    # ------------------------------------------------------------------
    # Keyboard Input
    # ------------------------------------------------------------------

    def type_text(self, text: str):
        """
        Type a string of text. Note: special characters may need escaping.
        Use key_event for non-text keys.
        """
        self._require_device()
        # Escape spaces and special chars for shell
        escaped = text.replace(" ", "%s").replace("'", "\\'")
        self._device.shell(f"input text '{escaped}'")
        _print(f"[ADB] Type text: '{text}'")

    def key_event(self, keycode: int):
        """
        Send an Android key event by keycode.
        Common keycodes:
            3  = HOME
            4  = BACK
            24 = VOLUME_UP
            25 = VOLUME_DOWN
            26 = POWER
            66 = ENTER
            67 = BACKSPACE
        """
        self._require_device()
        self._device.shell(f"input keyevent {keycode}")
        _print(f"[ADB] Key event: {keycode}")

    def press_back(self):
        self.key_event(4)

    def press_home(self):
        self.key_event(3)

    def press_enter(self):
        self.key_event(66)

    # ------------------------------------------------------------------
    # App Management
    # ------------------------------------------------------------------

    def launch_app(self, package: str, activity: str = ""):
        """
        Launch an app by package name.
        If activity is not provided, uses the default launcher activity.
        
        Example:
            bot.launch_app("com.supercell.clashofclans")
        """
        self._require_device()
        if activity:
            cmd = f"am start -n {package}/{activity}"
        else:
            cmd = f"monkey -p {package} -c android.intent.category.LAUNCHER 1"
        self._device.shell(cmd)
        _print(f"[ADB] Launched app: {package}")

    def stop_app(self, package: str):
        """Force-stop an app."""
        self._require_device()
        self._device.shell(f"am force-stop {package}")
        _print(f"[ADB] Stopped app: {package}")

    def is_app_installed(self, package: str) -> bool:
        """Check if an app package is installed on the device."""
        self._require_device()
        result = self._device.shell(f"pm list packages {package}")
        return package in result

    def get_foreground_app(self) -> str:
        """Return the package name of the currently focused app."""
        self._require_device()
        result = self._device.shell(
            "dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp'"
        )
        return result.strip()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def wait(self, seconds: float):
        """Simple blocking wait. Prefer wait_for_image in production."""
        _print(f"[ADB] Waiting {seconds}s...")
        time.sleep(seconds)

    def shell(self, command: str) -> str:
        """
        Run a raw ADB shell command. Escape hatch for anything not covered above.
        Returns the output as a string.
        """
        self._require_device()
        result = self._device.shell(command)
        return result

    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

    def _require_device(self):
        if self._device is None:
            raise RuntimeError(
                f"Not connected to {self.serial}. Call connect() first."
            )

    def _fetch_device_info(self) -> DeviceInfo:
        model = self._device.shell("getprop ro.product.model").strip()
        android = self._device.shell("getprop ro.build.version.release").strip()

        # Use actual screenshot size — wm size returns physical resolution
        # which may differ from the ADB screenshot resolution (e.g. 1080 vs 540)
        try:
            img = self.screenshot()
            w, h = img.size  # PIL returns (width, height)
        except Exception:
            # Fallback to wm size
            size_raw = self._device.shell("wm size").strip()
            w, h = 540, 960
            if "x" in size_raw:
                parts = size_raw.split(":")[-1].strip().split("x")
                try:
                    w, h = int(parts[0]), int(parts[1])
                except (ValueError, IndexError):
                    pass

        return DeviceInfo(
            serial=self.serial,
            port=self.port,
            model=model,
            android_version=android,
            screen_width=w,
            screen_height=h,
        )


# ---------------------------------------------------------------------------
# Multi-Instance Manager
# ---------------------------------------------------------------------------

class InstanceManager:
    """
    Manages multiple ADB connections to parallel MEmu instances.

    MEmu assigns ports in sequence: 5555, 5557, 5559, 5561...
    Each instance gets its own ADBWrapper.

    Usage:
        manager = InstanceManager(ports=[5555, 5557, 5559])
        manager.connect_all()
        for bot in manager.get_connected():
            bot.screenshot_save(f"screen_{bot.port}.png")
    """

    MEMU_DEFAULT_PORTS = [5555 + i * 2 for i in range(8)]  # 5555–5569

    def __init__(self, ports: Optional[list] = None):
        self.ports = ports or self.MEMU_DEFAULT_PORTS
        self.instances: dict[int, ADBWrapper] = {}

    def connect_all(self) -> int:
        """Try to connect to all configured ports. Returns number connected."""
        connected = 0
        for port in self.ports:
            bot = ADBWrapper(port=port)
            if bot.connect():
                self.instances[port] = bot
                connected += 1
        _print(f"[Manager] {connected}/{len(self.ports)} instances connected")
        return connected

    def connect_port(self, port: int) -> Optional[ADBWrapper]:
        """Connect to a specific port and return its wrapper."""
        bot = ADBWrapper(port=port)
        if bot.connect():
            self.instances[port] = bot
            return bot
        return None

    def get_connected(self) -> list[ADBWrapper]:
        """Return all currently connected wrappers."""
        return list(self.instances.values())

    def get(self, port: int) -> Optional[ADBWrapper]:
        """Get a specific instance by port."""
        return self.instances.get(port)

    def disconnect_all(self):
        for bot in self.instances.values():
            bot.disconnect()
        self.instances.clear()

    def discover_memu_ports(self) -> list[int]:
        """
        Auto-discover which MEmu ports are actually active by scanning
        the default port range and checking ADB connectivity.
        Returns list of active ports.
        """
        active = []
        for port in self.MEMU_DEFAULT_PORTS:
            bot = ADBWrapper(port=port)
            if bot.connect(timeout=3):
                active.append(port)
                self.instances[port] = bot
            else:
                bot.disconnect()
        _print(f"[Manager] Discovered active ports: {active}")
        return active


# ---------------------------------------------------------------------------
# Quick Test / Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _print("=" * 50)
    _print("ADB Wrapper - Connection Test")
    _print("=" * 50)

    # Single instance test
    _print("\n[1] Testing single instance on port 5555...")
    bot = ADBWrapper(port=5555)
    
    if bot.connect():
        _print("\n[2] Taking screenshot...")
        try:
            img = bot.screenshot()
            img.save("test_screenshot.png")
            _print(f"    Screenshot saved: test_screenshot.png ({img.size})")
        except Exception as e:
            _print(f"    Screenshot failed: {e}")

        _print("\n[3] Device info:")
        _print(f"    Serial:  {bot.info.serial}")
        _print(f"    Model:   {bot.info.model}")
        _print(f"    Android: {bot.info.android_version}")
        _print(f"    Screen:  {bot.info.screen_width}x{bot.info.screen_height}")

        _print("\n[4] Foreground app:")
        _print(f"    {bot.get_foreground_app()}")

        bot.disconnect()
    else:
        _print("\n  Could not connect. Make sure:")
        _print("  1. MEmu is running with at least one instance active")
        _print("  2. ADB is enabled in MEmu settings")
        _print("  3. ADB server is running: `adb start-server`")

    # Multi-instance discovery
    _print("\n[5] Scanning for all active MEmu instances...")
    manager = InstanceManager()
    manager.discover_memu_ports()
    _print(f"    Found {len(manager.get_connected())} instance(s)")
    manager.disconnect_all()

    _print("\nDone.")
