"""
action_executor.py
==================
Executes individual bot actions by combining ADBWrapper + VisionEngine.

Each action is a dict with an "action" key and parameters.
The ActionExecutor interprets and runs them one at a time.

This is the bridge between your task definitions and the actual device.
"""

import time
import logging
from typing import Optional, Callable
from dataclasses import dataclass, field
from enum import Enum

from adb_wrapper import ADBWrapper
from vision import VisionEngine, MatchResult


# ---------------------------------------------------------------------------
# Result Types
# ---------------------------------------------------------------------------

class ActionStatus(Enum):
    SUCCESS     = "success"
    FAILED      = "failed"
    SKIPPED     = "skipped"
    TIMEOUT     = "timeout"
    ABORT_TASK  = "abort_task"


@dataclass
class ActionResult:
    status: ActionStatus
    action: dict
    message: str = ""
    duration_ms: int = 0
    match: Optional[MatchResult] = None

    def __bool__(self):
        return self.status == ActionStatus.SUCCESS


# ---------------------------------------------------------------------------
# Action Executor
# ---------------------------------------------------------------------------

class ActionExecutor:
    """
    Executes individual actions against a connected ADB device.

    Supported actions:
        tap                  - tap at x, y coordinates
        tap_template         - find template on screen and tap it
        tap_text             - find text on screen and tap it
        swipe                - swipe between two points
        press_back           - press Android back button
        press_home           - press Android home button
        press_enter          - press enter key
        type_text            - type a string
        launch_app           - launch app by package name
        stop_app             - force stop an app
        wait                 - wait N seconds
        wait_for_template    - wait until template appears
        wait_for_template_gone - wait until template disappears
        screenshot           - take and save a screenshot
        if_template_tap          - tap template only if it exists (no fail if missing)
        repeat_if_template       - keep tapping template until it disappears
        scroll_down              - scroll down on screen
        scroll_up                - scroll up on screen
        verify_setting_template  - check if a template matching a setting value is on screen
        adjust_boomer_level      - detect boomer level and tap +/- to reach the setting target
        compare_resources        - OCR resource panel and compare two resource values (e.g. wood < food)
        read_resource_priority   - screenshot resource panel, rank by Total RSS ascending, save to logs/resource_priority.json
        loop_until_template      - run on_each actions repeatedly until any of the specified templates appears
    """

    def __init__(
        self,
        bot: ADBWrapper,
        vision: VisionEngine,
        template_dir: str = "templates",
        log_callback: Optional[Callable[[str], None]] = None,
        farm_settings: Optional[dict] = None,
        stop_event=None,
    ):
        self.bot = bot
        self.vision = vision
        self.template_dir = template_dir.rstrip("/\\")
        self.log_callback = log_callback
        self.logger = logging.getLogger("ActionExecutor")
        self.farm_settings: dict = farm_settings or {}
        self._stop_event = stop_event  # threading.Event from BotEngine, or None
        self._formations_sent = 0      # tracks how many gather formations have been dispatched this session
        self._used_slot_indices: set = set()  # slot indices already tapped this session (0-based)

    def set_farm_settings(self, settings: dict):
        """Update farm task settings (e.g. rally.boomer_level). Called by BotEngine before each task."""
        self.farm_settings = settings or {}

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    # Numeric fields that must always be the correct type
    _FLOAT_FIELDS = {"timeout", "seconds", "threshold", "delay", "x_pct", "y_pct",
                     "start_x_pct", "start_y_pct", "distance_pct", "fallback_x_pct",
                     "fallback_y_pct", "duration_ms"}
    _INT_FIELDS   = {"steps", "max_taps", "repeat"}

    @staticmethod
    def _normalize(action: dict) -> dict:
        """Coerce all known numeric fields to float/int so JSON string values never cause type errors."""
        a = dict(action)
        for k in ActionExecutor._FLOAT_FIELDS:
            if k in a and a[k] is not None:
                try: a[k] = float(a[k])
                except (ValueError, TypeError): pass
        for k in ActionExecutor._INT_FIELDS:
            if k in a and a[k] is not None:
                try: a[k] = int(float(a[k]))
                except (ValueError, TypeError): pass
        return a

    def execute(self, action: dict) -> ActionResult:
        """
        Execute a single action dict. Returns an ActionResult.

        Example:
            result = executor.execute({
                "action": "tap_template",
                "template": "nav_mail.png",
                "timeout": 10
            })
        """
        action = self._normalize(action)
        action_type = action.get("action", "").strip()
        if not action_type:
            return self._fail(action, "No 'action' key in action dict")

        start = time.time()
        self._log(f"▶ {action_type} {self._params_str(action)}")

        try:
            result = self._dispatch(action_type, action)
        except Exception as e:
            result = self._fail(action, f"Exception: {e}")

        result.duration_ms = int((time.time() - start) * 1000)
        status_icon = "✓" if result else "✗"
        self._log(f"  {status_icon} {result.message} ({result.duration_ms}ms)")
        return result

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    def _dispatch(self, action_type: str, action: dict) -> ActionResult:
        handlers = {
            "rally_count_check":        self._rally_count_check,
            "tap":                      self._tap,
            "tap_zone":                 self._tap_zone,
            "tap_template":             self._tap_template,
            "tap_text":                 self._tap_text,
            "swipe":                    self._swipe,
            "scroll_down":              self._scroll_down,
            "scroll_up":                self._scroll_up,
            "center_view":              self._center_view,
            "center_hq":                self._center_hq,
            "ensure_hq_view":           self._ensure_hq_view,
            "zoom_out":                 self._zoom_out,
            "scroll_right":             self._scroll_right,
            "scroll_left":              self._scroll_left,
            "scroll_up":                self._scroll_up,
            "scroll_down":              self._scroll_down,
            "press_back":               self._press_back,
            "press_home":               self._press_home,
            "press_enter":              self._press_enter,
            "type_text":                self._type_text,
            "launch_app":               self._launch_app,
            "stop_app":                 self._stop_app,
            "wait":                     self._wait,
            "wait_for_template":        self._wait_for_template,
            "wait_for_template_gone":   self._wait_for_template_gone,
            "screenshot":               self._screenshot,
            "if_template_tap":          self._if_template_tap,
            "tap_if_slots_available":   self._tap_if_slots_available,
            "check_formations_busy":    self._check_formations_busy,
            "tap_template_or_zone":     self._tap_template_or_zone,
            "find_template_with_scroll": self._find_template_with_scroll,
            "tap_template_or_template": self._tap_template_or_template,
            "tap_free_formation":       self._tap_free_formation,
            "check_claimed":            self._check_claimed,
            "repeat_if_template":       self._repeat_if_template,
            "verify_setting_template":  self._verify_setting_template,
            "adjust_boomer_level":      self._adjust_boomer_level,
            "adjust_resource_level":    self._adjust_resource_level,
            "search_resource_level":    self._search_resource_level,
            "compare_resources":        self._compare_resources,
            "read_resource_priority":   self._read_resource_priority,
            "loop_until_template":      self._loop_until_template,
        }

        handler = handlers.get(action_type)
        if not handler:
            return self._fail(action, f"Unknown action type: '{action_type}'")
        return handler(action)

    # ------------------------------------------------------------------
    # Action Handlers
    # ------------------------------------------------------------------

    def _tap(self, action: dict) -> ActionResult:
        x = action.get("x")
        y = action.get("y")
        if x is None or y is None:
            return self._fail(action, "tap requires 'x' and 'y'")
        self.bot.tap(int(x), int(y))
        return self._ok(action, f"Tapped ({x}, {y})")

    def _screen_size(self):
        """Get live screen dimensions from a screenshot — always accurate."""
        try:
            img = self.bot.screenshot()
            w, h = img.size
            self._log(f"  [screen] {w}x{h}")
            return w, h
        except Exception:
            w = self.bot.info.screen_width  if self.bot.info else 540
            h = self.bot.info.screen_height if self.bot.info else 960
            return w, h

    def _tap_zone(self, action: dict) -> ActionResult:
        """Tap a position defined as % of screen size. Works across all resolutions."""
        w, h = self._screen_size()

        x_pct = float(action.get("x_pct", 50))
        y_pct = float(action.get("y_pct", 50))
        x = int(w * x_pct / 100)
        y = int(h * y_pct / 100)
        self._log(f"  [tap_zone] pct=({x_pct},{y_pct}) -> pixel=({x},{y})")
        self.bot.tap(x, y)
        label = action.get("note", f"{x_pct:.0f}%x{y_pct:.0f}%")
        return self._ok(action, f"Tap zone [{label}] -> ({x}, {y})")

    def _tap_template(self, action: dict) -> ActionResult:
        template = action.get("template")
        if not template:
            return self._fail(action, "tap_template requires 'template'")

        path = self._template_path(template)
        timeout = float(action.get("timeout", 0))
        threshold = float(action.get("threshold")) if action.get("threshold") is not None else None

        if timeout > 0:
            match = self.vision.wait_for_template(
                self.bot, path, timeout=timeout, threshold=threshold
            )
        else:
            screenshot = self.bot.screenshot()
            match = self.vision.find_template(screenshot, path, threshold=threshold)

        if not match:
            return ActionResult(
                status=ActionStatus.TIMEOUT if timeout > 0 else ActionStatus.FAILED,
                action=action,
                message=f"Template not found: {template}",
            )

        self.bot.tap(match.x, match.y)
        return ActionResult(
            status=ActionStatus.SUCCESS,
            action=action,
            message=f"Tapped {template} at ({match.x}, {match.y}) [conf: {match.confidence:.2f}]",
            match=match,
        )

    def _tap_text(self, action: dict) -> ActionResult:
        text = action.get("text")
        if not text:
            return self._fail(action, "tap_text requires 'text'")

        region = action.get("region", None)
        screenshot = self.bot.screenshot()
        result = self.vision.find_text(screenshot, text, region=region)

        if not result:
            return self._fail(action, f"Text not found on screen: '{text}'")

        self.bot.tap(result.x, result.y)
        return self._ok(action, f"Tapped text '{result.text}' at ({result.x}, {result.y})")

    def _swipe(self, action: dict) -> ActionResult:
        x1 = action.get("x1", 0)
        y1 = action.get("y1", 0)
        x2 = action.get("x2", 0)
        y2 = action.get("y2", 0)
        duration = action.get("duration_ms", 300)
        self.bot.swipe(int(x1), int(y1), int(x2), int(y2), int(duration))
        return self._ok(action, f"Swiped ({x1},{y1}) → ({x2},{y2})")

    def _scroll_down(self, action: dict) -> ActionResult:
        # Swipe upward to scroll content down
        w, h = self._screen_size()
        cx = w // 2
        distance = action.get("distance", 300)
        duration = action.get("duration_ms", 400)
        self.bot.swipe(cx, h // 2, cx, h // 2 - distance, duration)
        return self._ok(action, f"Scrolled down {distance}px")

    def _scroll_up(self, action: dict) -> ActionResult:
        w, h = self._screen_size()
        cx = w // 2
        distance = action.get("distance", 300)
        duration = action.get("duration_ms", 400)
        self.bot.swipe(cx, h // 2, cx, h // 2 + distance, duration)
        return self._ok(action, f"Scrolled up {distance}px")

    def _center_view(self, action: dict) -> ActionResult:
        """
        Reset camera to show the full base with HQ centered.

        Strategy:
          1. Press Android HOME key to dismiss any open menus
          2. Wait for base to settle
          3. Use ADB keyevent 3 (home) which in Last Z resets the camera
          4. If a template is provided, search the full screen for it and tap
          5. Fallback: pinch-out gesture to zoom out then tap center

        Example:
            {"action": "center_view"}
            {"action": "center_view", "template": "building_hq.png"}
        """
        import time
        w, h = self._screen_size()
        cx, cy = w // 2, h // 2

        # Step 1: Press back to dismiss any open building menus
        self.bot.press_back()
        time.sleep(0.4)

        # Step 2: Zoom out by pinching — sends a swipe from center outward
        # Two-finger spread simulated as two fast opposite swipes
        self.bot.shell(
            f"input swipe {cx} {cy} {cx - 150} {cy} 300 & "
            f"input swipe {cx} {cy} {cx + 150} {cy} 300"
        )
        time.sleep(0.5)

        # Step 3: Tap center to reset focus
        self.bot.tap(cx, cy)
        time.sleep(0.4)

        # Step 4: If template provided, search whole screen and tap it
        template = action.get("template")
        if template:
            screenshot = self.bot.screenshot()
            path = self._template_path(template)
            match = self.vision.find_template(screenshot, path)
            if match:
                self.bot.tap(match.x, match.y)
                time.sleep(0.5)
                return self._ok(action, f"Centered: found and tapped {template}")
            else:
                # Template not visible even after reset — scroll to find it
                # Try scrolling in each direction briefly
                for dx, dy in [(0, 200), (0, -200), (200, 0), (-200, 0)]:
                    self.bot.swipe(cx, cy, cx + dx, cy + dy, duration_ms=400)
                    time.sleep(0.5)
                    screenshot = self.bot.screenshot()
                    match = self.vision.find_template(screenshot, path)
                    if match:
                        self.bot.tap(match.x, match.y)
                        time.sleep(0.5)
                        return self._ok(action, f"Centered: found {template} after scroll")
                return self._ok(action, f"center_view: {template} not found, camera reset only")

        return self._ok(action, f"Camera reset to center ({cx}, {cy})")

    def _center_hq(self, action: dict) -> ActionResult:
        """
        Runs the saved tasks/center_hq.json sequence to reset the camera.
        Falls back to minimap tap if the file is not found.

        To customize: edit tasks/center_hq.json in the Capture Tool.
        """
        import time, json as _json
        from pathlib import Path

        seq_path = Path("tasks") / "center_hq.json"

        if seq_path.exists():
            try:
                with open(seq_path) as f:
                    data = _json.load(f)
                actions = data.get("actions", data) if isinstance(data, dict) else data
                for step in actions:
                    self.execute(step)
                return self._ok(action, f"center_hq: ran {seq_path} ({len(actions)} steps)")
            except Exception as e:
                # Fall through to minimap fallback
                if self.log_callback:
                    self.log_callback(f"center_hq: could not run {seq_path}: {e}")

        # Fallback: tap minimap
        w, h = self._screen_size()
        self.bot.press_back()
        time.sleep(0.3)
        mx = int(w * 0.08)
        my = int(h * 0.88)
        self.bot.tap(mx, my)
        time.sleep(0.6)
        return self._ok(action, f"center_hq: fallback minimap tap ({mx},{my})")

    def _ensure_hq_view(self, action: dict) -> ActionResult:
        """
        Guarantees the player is in HQ (base) view before any task runs.

        Logic:
          1. Take a screenshot
          2. Check for btn_world.png  → player is IN HQ view
             - Tap btn_world.png to go to world map
             - Wait for transition
             - Tap btn_hq_out.png to return to HQ (re-centers camera)
          3. Check for btn_hq_out.png → player is IN world view
             - Tap btn_hq_out.png to return to HQ view
          4. Neither found → take no action, log warning

        Templates used:
          btn_go_world.png         — visible only when inside HQ/base view
          btn_go_headquarters.png  — visible only when in world map view

        Override template names via action params:
          {"action": "ensure_hq_view",
           "hq_btn": "btn_go_world.png",
           "world_btn": "btn_go_headquarters.png"}
        """
        import time

        # hq_btn:    visible when IN HQ view   → tap it to go to world
        # world_btn: visible when IN WORLD view → tap it to go to HQ
        hq_btn    = action.get("hq_btn",    "btn_go_world.png")
        world_btn = action.get("world_btn", "btn_go_headquarters.png")

        screenshot = self.bot.screenshot()

        hq_path    = self._template_path(hq_btn)
        world_path = self._template_path(world_btn)

        w, h = self._screen_size()
        x_pct = float(action.get("x_pct", 91.1))
        y_pct = float(action.get("y_pct", 95.6))
        x = int(w * x_pct / 100)
        y = int(h * y_pct / 100)

        # Check which view we are in using the HQ view indicator
        # hq_btn is ONLY visible when in HQ view
        # world_btn is ONLY visible when in world view
        in_hq    = self.vision.find_template(screenshot, hq_path)    is not None
        in_world = self.vision.find_template(screenshot, world_path) is not None

        if self.log_callback:
            self.log_callback(f"ensure_hq_view: in_hq={in_hq} in_world={in_world}")

        if in_hq:
            # btn_go_world.png visible — we are in HQ view. btn_go_headquarters.png may also
            # match due to a false positive on the HQ screen; ignore it and stay put.
            if self.log_callback:
                self.log_callback("ensure_hq_view: in HQ (btn_go_world found) — no action needed")
            return self._ok(action, "ensure_hq_view: already in HQ")

        elif in_world:
            # Confirmed in world view — tap_zone to return to HQ
            if self.log_callback:
                self.log_callback(f"ensure_hq_view: world view confirmed — tapping {x_pct}%,{y_pct}%")
            self.bot.tap(x, y)
            time.sleep(2.0)
            return self._ok(action, f"ensure_hq_view: world→HQ via tap_zone {x_pct}%,{y_pct}%")

        else:
            # Neither detected — just try tap_zone anyway
            if self.log_callback:
                self.log_callback("ensure_hq_view: unknown state — attempting tap_zone")
            self.bot.tap(x, y)
            time.sleep(2.0)
            return self._ok(action, "ensure_hq_view: unknown state, tapped tap_zone")

    def _zoom_out(self, action: dict) -> ActionResult:
        """
        Zoom out using MEmu F3 shortcut.
        Enumerates all windows, logs them, focuses MEmu, sends F3.
        """
        import time
        import ctypes
        from ctypes import wintypes

        steps = int(action.get("steps", 3))
        user32 = ctypes.windll.user32

        # Enumerate ALL visible windows for debug
        all_windows = []
        def enum_cb(h, _):
            if user32.IsWindowVisible(h):
                length = user32.GetWindowTextLengthW(h)
                if length:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(h, buf, length + 1)
                    all_windows.append((h, buf.value))
            return True
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

        if self.log_callback:
            self.log_callback(f"zoom_out: visible windows found: {len(all_windows)}")
            for h, t in all_windows:
                if any(k in t for k in ["MEmu", "Last", "Play", "MicroVirt"]):
                    self.log_callback(f"  >> hwnd={h}  title='{t}'")

        # Find the correct MEmu instance window — prefer "(MEmu - N)" over bare "MEmu"
        # The instance windows are titled "(MEmu - 1)", "(MEmu - 2)", etc.
        # We match by ADB port: port 21503 = index 0 = MEmu-1, etc.
        hwnd, title = None, ""

        # Try to match by bot port → instance number
        try:
            port = self.bot.port if hasattr(self.bot, "port") else 21503
            index = (port - 21503) // 2 + 1  # 21503→1, 21505→2, etc.
            target_title = f"(MEmu - {index})"
            for h, t in all_windows:
                if t == target_title:
                    hwnd, title = h, t
                    break
        except Exception:
            pass

        # Fallback: first "(MEmu - N)" window found
        if not hwnd:
            for h, t in all_windows:
                if t.startswith("(MEmu - "):
                    hwnd, title = h, t
                    break

        # Last resort: bare "MEmu"
        if not hwnd:
            for h, t in all_windows:
                if t == "MEmu":
                    hwnd, title = h, t
                    break

        if not hwnd:
            if self.log_callback:
                self.log_callback("zoom_out: no MEmu window found — all titles:")
                for h, t in all_windows[:20]:
                    self.log_callback(f"    hwnd={h} '{t}'")
            return self._fail(action, "zoom_out: MEmu window not found")

        if self.log_callback:
            self.log_callback(f"zoom_out: targeting '{title}' hwnd={hwnd}")

        # Focus window
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.6)

        # Verify focus
        focused = user32.GetForegroundWindow()
        if self.log_callback:
            self.log_callback(f"zoom_out: foreground hwnd={focused} (expected {hwnd}) match={focused==hwnd}")

        # Send F3
        try:
            import pyautogui
            pyautogui.FAILSAFE = False
            for i in range(steps):
                pyautogui.press('f3')
                if self.log_callback:
                    self.log_callback(f"zoom_out: sent F3 ({i+1}/{steps})")
                time.sleep(0.4)
            return self._ok(action, f"zoom_out: F3 x{steps} → '{title}'")
        except ImportError:
            return self._fail(action, "zoom_out: pyautogui not installed")
        except ImportError:
            return self._fail(action, "zoom_out: pyautogui not installed — run: pip install pyautogui")

    def _scroll_right(self, action: dict) -> ActionResult:
        """Scroll view right — finger starts at bottom-right, swipes left along bottom row."""
        import time
        w, h = self._screen_size()
        start_x  = int(w * float(action.get("start_x_pct", 80)) / 100)
        start_y  = int(h * float(action.get("start_y_pct", 88)) / 100)
        distance = int(w * float(action.get("distance_pct", 60)) / 100)
        duration = int(action.get("duration_ms", 600))
        steps    = int(action.get("steps", 1))
        self._log(f"  [scroll_right] screen={w}x{h} start=({start_x},{start_y}) dist={distance}px dur={duration}ms x{steps}")
        for _ in range(steps):
            self.bot.swipe(start_x, start_y, start_x - distance, start_y, duration_ms=duration)
            time.sleep(0.4)
        return self._ok(action, f"scroll_right: from ({start_x},{start_y}) -{distance}px x{steps}")

    def _scroll_left(self, action: dict) -> ActionResult:
        """Scroll left — swipe from left to right."""
        import time
        w, h = self._screen_size()
        start_x  = int(w * float(action.get("start_x_pct", 25)) / 100)
        start_y  = int(h * float(action.get("start_y_pct", 80)) / 100)
        distance = int(w * float(action.get("distance_pct", 40)) / 100)
        duration = int(action.get("duration_ms", 500))
        steps    = int(action.get("steps", 1))
        for _ in range(steps):
            self.bot.swipe(start_x, start_y, start_x + distance, start_y, duration_ms=duration)
            time.sleep(0.3)
        return self._ok(action, f"scroll_left: from ({start_x},{start_y}) dist={distance}px x{steps}")

    def _scroll_up(self, action: dict) -> ActionResult:
        """Scroll up — swipe finger downward to reveal content above."""
        import time
        w, h = self._screen_size()
        start_x  = int(w * float(action.get("start_x_pct", 50)) / 100)
        start_y  = int(h * float(action.get("start_y_pct", 30)) / 100)
        distance = int(h * float(action.get("distance_pct", 40)) / 100)
        duration = int(action.get("duration_ms", 500))
        steps    = int(action.get("steps", 1))
        for _ in range(steps):
            self.bot.swipe(start_x, start_y, start_x, start_y + distance, duration_ms=duration)
            time.sleep(0.3)
        return self._ok(action, f"scroll_up: from ({start_x},{start_y}) dist={distance}px x{steps}")

    def _scroll_down(self, action: dict) -> ActionResult:
        """Scroll down — swipe finger upward from bottom to reveal content below."""
        import time
        w, h = self._screen_size()
        start_x  = int(w * float(action.get("start_x_pct", 50)) / 100)
        start_y  = int(h * float(action.get("start_y_pct", 80)) / 100)
        distance = int(h * float(action.get("distance_pct", 40)) / 100)
        duration = int(action.get("duration_ms", 500))
        steps    = int(action.get("steps", 1))
        for _ in range(steps):
            self.bot.swipe(start_x, start_y, start_x, start_y - distance, duration_ms=duration)
            time.sleep(0.3)
        return self._ok(action, f"scroll_down: from ({start_x},{start_y}) dist={distance}px x{steps}")

    def _press_back(self, action: dict) -> ActionResult:
        self.bot.press_back()
        return self._ok(action, "Pressed BACK")

    def _press_home(self, action: dict) -> ActionResult:
        self.bot.press_home()
        return self._ok(action, "Pressed HOME")

    def _press_enter(self, action: dict) -> ActionResult:
        self.bot.press_enter()
        return self._ok(action, "Pressed ENTER")

    def _type_text(self, action: dict) -> ActionResult:
        text = action.get("text", "")
        self.bot.type_text(text)
        return self._ok(action, f"Typed: '{text}'")

    def _launch_app(self, action: dict) -> ActionResult:
        package = action.get("package")
        if not package:
            return self._fail(action, "launch_app requires 'package'")
        activity = action.get("activity", "")
        self.bot.launch_app(package, activity)
        return self._ok(action, f"Launched {package}")

    def _stop_app(self, action: dict) -> ActionResult:
        package = action.get("package")
        if not package:
            return self._fail(action, "stop_app requires 'package'")
        self.bot.stop_app(package)
        return self._ok(action, f"Stopped {package}")

    def _wait(self, action: dict) -> ActionResult:
        seconds = float(action.get("seconds", 1.0))
        time.sleep(seconds)
        return self._ok(action, f"Waited {seconds}s")

    def _wait_for_template(self, action: dict) -> ActionResult:
        template = action.get("template")
        if not template:
            return self._fail(action, "wait_for_template requires 'template'")
        timeout = float(action.get("timeout", 30))
        path = self._template_path(template)
        match = self.vision.wait_for_template(self.bot, path, timeout=timeout)
        if match:
            return ActionResult(
                status=ActionStatus.SUCCESS,
                action=action,
                message=f"'{template}' appeared at ({match.x}, {match.y})",
                match=match,
            )
        return ActionResult(
            status=ActionStatus.TIMEOUT,
            action=action,
            message=f"Timeout waiting for '{template}'",
        )

    def _wait_for_template_gone(self, action: dict) -> ActionResult:
        template = action.get("template")
        if not template:
            return self._fail(action, "wait_for_template_gone requires 'template'")
        timeout = float(action.get("timeout", 30))
        path = self._template_path(template)
        gone = self.vision.wait_for_template_gone(self.bot, path, timeout=timeout)
        if gone:
            return self._ok(action, f"'{template}' disappeared")
        return ActionResult(
            status=ActionStatus.TIMEOUT,
            action=action,
            message=f"Timeout — '{template}' never disappeared",
        )

    def _screenshot(self, action: dict) -> ActionResult:
        filename = action.get("filename", f"screenshot_{int(time.time())}.png")
        self.bot.screenshot_save(filename)
        return self._ok(action, f"Screenshot saved: {filename}")

    def _check_claimed(self, action: dict) -> ActionResult:
        """
        Check if a reward has already been claimed.
        If claimed_template is found, log the message and continue to next action.
        Does NOT abort the task — use this when other rewards may still be available.
        """
        claimed_template = action.get("claimed_template")
        if not claimed_template:
            return self._fail(action, "check_claimed requires 'claimed_template'")
        path = self._template_path(claimed_template)
        screenshot = self.bot.screenshot()
        match = self.vision.find_template(screenshot, path)
        if match:
            msg = action.get("claimed_message", "Already claimed — continuing")
            if self.log_callback:
                self.log_callback(f"  ✅ {msg}")
            return ActionResult(
                status=ActionStatus.SKIPPED,
                action=action,
                message=msg,
            )
        return self._ok(action, "Not claimed yet — continuing")

    def _if_template_tap(self, action: dict) -> ActionResult:
        """
        Tap a template only if it exists. Does NOT fail if template is absent.
        Use for optional popups that may or may not appear.

        Optional flags:
          skip_task_if_not_found: true       — aborts the whole task with a log message if not found
          exhaust_rallies_if_not_found: true — sets rally counter to max so no further rallies run,
                                               then aborts the current task
          log_success: "My message"          — custom log message on success
          log_skip: "My message"             — custom log message when skipped
        """
        template = action.get("template")
        if not template:
            return self._fail(action, "if_template_tap requires 'template'")
        threshold = float(action.get("threshold")) if action.get("threshold") is not None else None
        screenshot = self.bot.screenshot()

        # Build list of templates to try: primary + optional fallbacks
        templates_to_try = [template]
        fallback = action.get("fallback_template")
        if fallback:
            templates_to_try.append(fallback)

        match = None
        matched_template = None
        for tmpl in templates_to_try:
            path = self._template_path(tmpl)
            match = self.vision.find_template(screenshot, path, threshold=threshold)
            if self.log_callback:
                conf = getattr(match, "confidence", 0) if match else 0
                used_threshold = threshold if threshold is not None else self.vision.confidence_threshold
                self.log_callback(f"  [if_template_tap] '{tmpl}' conf={conf:.3f} threshold={used_threshold:.3f} found={bool(match)}")
            if match:
                matched_template = tmpl
                break

        if match:
            self.bot.tap(match.x, match.y)
            msg = action.get("log_success", f"Found and tapped '{matched_template}'")
            if self.log_callback:
                self.log_callback(f"  ✓ {msg}")
            return self._ok(action, msg)

        # Not found
        skip_task     = action.get("skip_task_if_not_found", False)
        exhaust_rallies = action.get("exhaust_rallies_if_not_found", False)
        skip_msg      = action.get("log_skip", f"'{template}' not found — skipping task")
        if self.log_callback:
            self.log_callback(f"  ⏭ {skip_msg}")

        if exhaust_rallies:
            # Set counter to max so all future rally_count_check calls abort
            import json as _json
            from pathlib import Path
            from datetime import date
            rally_cfg   = self.farm_settings.get("rally", {})
            max_rallies = int(rally_cfg.get("max_rallies_per_day", 999))
            port        = getattr(self.bot, "port", 0)
            key         = f"{port}_{date.today()}"
            counts_path = Path("logs/rally_counts.json")
            counts: dict = {}
            if counts_path.exists():
                try:
                    with open(counts_path) as f:
                        counts = _json.load(f)
                except Exception:
                    counts = {}
            counts[key] = max_rallies
            Path("logs").mkdir(exist_ok=True)
            with open(counts_path, "w") as f:
                _json.dump(counts, f, indent=2)
            msg = f"Troops busy — rally counter exhausted ({max_rallies}/{max_rallies}), stopping all rallies"
            if self.log_callback:
                self.log_callback(f"  🛑 {msg}")
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)

        if skip_task:
            return ActionResult(
                status=ActionStatus.ABORT_TASK,
                action=action,
                message=skip_msg,
            )
        return ActionResult(
            status=ActionStatus.SKIPPED,
            action=action,
            message=skip_msg,
        )

    # ------------------------------------------------------------------
    # Resource comparison
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_resource_value(text: str) -> Optional[float]:
        """
        Parse a resource value string like '71.4M', '7,340', '225.9M', '0' into a float.
        Returns None if the text cannot be parsed as a number.
        """
        import re
        text = text.strip().replace(",", "")
        m = re.fullmatch(r"([\d.]+)\s*([MmKkBb]?)", text)
        if not m:
            return None
        value = float(m.group(1))
        suffix = m.group(2).upper()
        if suffix == "B":
            value *= 1_000_000_000
        elif suffix == "M":
            value *= 1_000_000
        elif suffix == "K":
            value *= 1_000
        return value

    def _read_resource_panel(self, screenshot, anchor_template: str) -> dict[str, float]:
        """
        Locate the resource panel via anchor_template, OCR it, and return a dict
        mapping lowercase resource name → Total Items value.

        Known resources: exp, wood, food, electricity, zent, steel, fuel
        """
        KNOWN_RESOURCES = {"exp", "wood", "food", "electricity", "zent", "steel", "fuel"}

        # Find the panel on screen
        path = self._template_path(anchor_template)
        match = self.vision.find_template(screenshot, path, threshold=0.6)
        if match and match.region:
            rx1, ry1, rx2, ry2 = match.region
            # Expand to cover the full panel below/around the anchor
            sw, sh = screenshot.size
            region = (max(0, rx1 - 20), max(0, ry1 - 20),
                      min(sw, rx2 + 20), min(sh, ry2 + 300))
        else:
            # Fall back to full screen OCR
            region = None
            if self.log_callback:
                self.log_callback(f"  [compare_resources] anchor '{anchor_template}' not found — scanning full screen")

        ocr_results = self.vision.read_text(screenshot, region=region, min_confidence=0.3)

        # Group OCR results by row (similar Y within ±12px)
        rows: list[list] = []
        for item in sorted(ocr_results, key=lambda r: r.y):
            placed = False
            for row in rows:
                if abs(item.y - row[0].y) <= 12:
                    row.append(item)
                    placed = True
                    break
            if not placed:
                rows.append([item])

        resources: dict[str, float] = {}
        for row in rows:
            row.sort(key=lambda r: r.x)
            texts = [r.text.strip() for r in row]
            # First token in the row — is it a known resource name?
            name = texts[0].lower() if texts else ""
            if name not in KNOWN_RESOURCES:
                continue
            # Pick first numeric token after the name as "Total Items"
            for token in texts[1:]:
                val = self._parse_resource_value(token)
                if val is not None:
                    resources[name] = val
                    break

        return resources

    def _compare_resources(self, action: dict) -> ActionResult:
        """
        OCR the resource panel and compare two values.

        Required fields:
          anchor_template  - template to locate the panel (e.g. 'btn_resource_example.png')
          left             - resource name on left side  (e.g. 'wood')
          operator         - one of: <  <=  >  >=  ==  !=
          right            - resource name OR a literal number (e.g. 'food' or '1000000')

        Optional:
          skip_task_if_false: true  — abort task if condition is false
          skip_task_if_true:  true  — abort task if condition is true
          column            - 'total_items' (default) or 'total_rss'

        Example:
          {"action": "compare_resources", "anchor_template": "btn_resource_example.png",
           "left": "wood", "operator": "<", "right": "food", "skip_task_if_false": true}
        """
        anchor   = action.get("anchor_template", "btn_resource_example.png")
        left_key = action.get("left", "").lower()
        operator = action.get("operator", "").strip()
        right_raw = str(action.get("right", "")).lower()

        if not left_key or not operator:
            return self._fail(action, "compare_resources requires 'left' and 'operator'")

        OPERATORS = {"<", "<=", ">", ">=", "==", "!="}
        if operator not in OPERATORS:
            return self._fail(action, f"compare_resources: unknown operator '{operator}'. Use one of {OPERATORS}")

        screenshot = self.bot.screenshot()
        resources = self._read_resource_panel(screenshot, anchor)

        if self.log_callback:
            summary = ", ".join(f"{k}={v:,.0f}" for k, v in resources.items())
            self.log_callback(f"  [compare_resources] found: {summary or '(none)'}")

        # Resolve left value
        if left_key not in resources:
            return self._fail(action, f"compare_resources: '{left_key}' not found in resource panel")
        left_val = resources[left_key]

        # Resolve right value — either a known resource or a literal number
        try:
            right_val = float(right_raw.replace(",", ""))
        except ValueError:
            if right_raw not in resources:
                return self._fail(action, f"compare_resources: '{right_raw}' not found in resource panel")
            right_val = resources[right_raw]

        # Evaluate
        ops = {
            "<":  left_val <  right_val,
            "<=": left_val <= right_val,
            ">":  left_val >  right_val,
            ">=": left_val >= right_val,
            "==": left_val == right_val,
            "!=": left_val != right_val,
        }
        result_bool = ops[operator]

        msg = f"{left_key}={left_val:,.0f} {operator} {right_raw}={right_val:,.0f}  →  {'TRUE' if result_bool else 'FALSE'}"
        if self.log_callback:
            self.log_callback(f"  [compare_resources] {msg}")

        if action.get("skip_task_if_false") and not result_bool:
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action,
                                message=f"Condition false — {msg}")
        if action.get("skip_task_if_true") and result_bool:
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action,
                                message=f"Condition true — {msg}")

        return self._ok(action, msg)

    def _read_resource_priority(self, action: dict) -> ActionResult:
        """
        Take a screenshot, OCR the resource panel, rank resources by Total RSS
        ascending (lowest = highest priority), and save to logs/resource_priority.json.

        Required:
          anchor_template  - template to locate the panel (default: 'btn_resource_example.png')

        Output file: logs/resource_priority.json
          {
            "timestamp": "2026-03-12T19:00:00",
            "priority": ["food", "wood", "zent", "electricity", "fuel", "steel"],
            "values": {"food": 73300000, "wood": 72100000, ...}
          }

        Example:
          {"action": "read_resource_priority", "anchor_template": "btn_resource_example.png"}
        """
        import json as _json
        from datetime import datetime as _dt
        from pathlib import Path as _Path

        KNOWN_RESOURCES = {"exp", "wood", "food", "electricity", "zent", "steel", "fuel"}
        anchor = action.get("anchor_template", "btn_resource_example.png")

        screenshot = self.bot.screenshot()

        # Locate the panel
        path = self._template_path(anchor)
        match = self.vision.find_template(screenshot, path, threshold=0.6)
        if match and match.region:
            rx1, ry1, rx2, ry2 = match.region
            sw, sh = screenshot.size
            region = (max(0, rx1 - 20), max(0, ry1 - 20),
                      min(sw, rx2 + 20), min(sh, ry2 + 300))
            if self.log_callback:
                self.log_callback(f"  [read_resource_priority] panel found at {match.region}")
        else:
            region = None
            if self.log_callback:
                self.log_callback(f"  [read_resource_priority] anchor not found — scanning full screen")

        ocr_results = self.vision.read_text(screenshot, region=region, min_confidence=0.3)

        # Group into rows by Y proximity
        rows: list[list] = []
        for item in sorted(ocr_results, key=lambda r: r.y):
            placed = False
            for row in rows:
                if abs(item.y - row[0].y) <= 12:
                    row.append(item)
                    placed = True
                    break
            if not placed:
                rows.append([item])

        values: dict[str, float] = {}
        for row in rows:
            row.sort(key=lambda r: r.x)
            texts = [r.text.strip() for r in row]
            name = texts[0].lower() if texts else ""
            if name not in KNOWN_RESOURCES:
                continue
            # Total RSS = rightmost numeric token in the row
            for token in reversed(texts[1:]):
                val = self._parse_resource_value(token)
                if val is not None:
                    values[name] = val
                    break

        if not values:
            return self._fail(action, "read_resource_priority: no resources found via OCR")

        # Sort ascending — lowest Total RSS = highest priority
        priority = sorted(values, key=lambda k: values[k])

        if self.log_callback:
            ranked = ", ".join(f"{i+1}.{r}={values[r]:,.0f}" for i, r in enumerate(priority))
            self.log_callback(f"  [read_resource_priority] {ranked}")

        # Save
        _Path("logs").mkdir(exist_ok=True)
        out = {
            "timestamp": _dt.now().isoformat(timespec="seconds"),
            "priority":  priority,
            "values":    {k: int(values[k]) for k in priority},
        }
        out_path = _Path("logs/resource_priority.json")
        with open(out_path, "w") as f:
            _json.dump(out, f, indent=2)

        return self._ok(action, f"Priority saved: {' > '.join(priority)}")

    def _loop_until_template(self, action: dict) -> ActionResult:
        """
        Run on_each actions repeatedly until any template in 'any_of' is visible.
        Checks the done condition BEFORE each iteration — exits immediately if already done.

        Required:
          any_of          - list of template filenames; loop stops when any one is found

        Optional:
          on_each         - list of actions to run each iteration (default: [])
          max_iterations  - safety cap to prevent infinite loops (default: 30)
          threshold       - vision confidence override for the done-check templates

        Example:
          {
            "action": "loop_until_template",
            "any_of": ["btn_empty_radar_1.png", "btn_empty_radar_2.png"],
            "max_iterations": 30,
            "on_each": [ ... ]
          }
        """
        any_of = action.get("any_of", [])
        if not any_of:
            return self._fail(action, "loop_until_template requires 'any_of' list")

        on_each       = action.get("on_each", [])
        max_iter      = int(action.get("max_iterations", 30))
        threshold     = float(action.get("threshold")) if action.get("threshold") is not None else None

        for iteration in range(max_iter + 1):  # +1 so we check done state after final iteration too
            if self._stop_event and self._stop_event.is_set():
                return self._ok(action, f"loop_until_template: stop requested after {iteration} iteration(s)")

            # Check done condition
            screenshot = self.bot.screenshot()
            for tmpl in any_of:
                path = self._template_path(tmpl)
                match = self.vision.find_template(screenshot, path, threshold=threshold)
                if self.log_callback:
                    conf = getattr(match, "confidence", 0) if match else 0
                    self.log_callback(f"  [loop_until_template] done-check '{tmpl}' conf={conf:.3f} found={bool(match)}")
                if match:
                    return self._ok(action, f"Done — '{tmpl}' found after {iteration} iteration(s)")

            if iteration == max_iter:
                break

            self.log_callback and self.log_callback(f"  [loop_until_template] iteration {iteration + 1}/{max_iter} — running on_each")

            for sub in on_each:
                if self._stop_event and self._stop_event.is_set():
                    return self._ok(action, f"loop_until_template: stop requested mid-iteration {iteration + 1}")
                sub_result = self.execute(sub)
                if sub_result.status == ActionStatus.ABORT_TASK:
                    return sub_result

        return self._ok(action, f"loop_until_template: reached max_iterations ({max_iter}) without finding done template")

    def _tap_if_slots_available(self, action: dict) -> ActionResult:
        """
        Like if_template_tap, but first reads the formation counter (e.g. "3/3")
        via OCR. If all slots are occupied, skips the tap and returns SKIPPED so
        the enclosing loop can continue normally.

        Use this instead of if_template_tap for btn_radar_gather / btn_radar_attack
        so the bot doesn't attempt a march when every formation is already deployed.

        Parameters
        ----------
        template        : template filename to look for and tap (required)
        slots_x1_pct    : left edge of formation counter region  (default 83)
        slots_y1_pct    : top edge of formation counter region   (default 43)
        slots_x2_pct    : right edge of formation counter region (default 98)
        slots_y2_pct    : bottom edge of formation counter region(default 51)
        threshold       : vision confidence override (optional)
        """
        import re as _re

        template = action.get("template")
        if not template:
            return self._fail(action, "tap_if_slots_available requires 'template'")

        threshold = float(action.get("threshold")) if action.get("threshold") is not None else None
        screenshot = self.bot.screenshot()

        # ── Step 1: Is the template even on screen? ────────────────────
        path  = self._template_path(template)
        tmatch = self.vision.find_template(screenshot, path, threshold=threshold)
        conf  = getattr(tmatch, "confidence", 0) if tmatch else 0
        self._log(f"  [tap_if_slots_available] '{template}' conf={conf:.3f} found={bool(tmatch)}")

        if not tmatch:
            return ActionResult(
                status=ActionStatus.SKIPPED,
                action=action,
                message=f"'{template}' not visible — skip",
            )

        # ── Step 2: Read the formation counter via OCR ─────────────────
        w, h = screenshot.size
        x1 = int(w * float(action.get("slots_x1_pct", 83)) / 100)
        y1 = int(h * float(action.get("slots_y1_pct", 43)) / 100)
        x2 = int(w * float(action.get("slots_x2_pct", 98)) / 100)
        y2 = int(h * float(action.get("slots_y2_pct", 51)) / 100)

        ocr_results = self.vision.read_text(screenshot, region=(x1, y1, x2, y2),
                                             min_confidence=0.3)
        raw = " ".join(r.text for r in ocr_results)
        self._log(f"  [tap_if_slots_available] formation OCR: '{raw}'")

        slot_match = _re.search(r'(\d+)\s*/\s*(\d+)', raw)
        if slot_match:
            current = int(slot_match.group(1))
            total   = int(slot_match.group(2))
            if current >= total:
                msg = f"Radar incomplete — all formations busy ({current}/{total}), skipping radar task"
                self._log(f"  ⚠ {msg}")
                return ActionResult(
                    status=ActionStatus.ABORT_TASK,
                    action=action,
                    message=msg,
                )
            self._log(f"  [tap_if_slots_available] {current}/{total} — slot available, tapping")
        else:
            # Counter unreadable — proceed anyway (fail-open)
            self._log(f"  [tap_if_slots_available] counter unreadable ('{raw}') — proceeding")

        # ── Step 3: Tap ────────────────────────────────────────────────
        self.bot.tap(tmatch.x, tmatch.y)
        slot_info = f"{current}/{total}" if slot_match else "?/?"
        return self._ok(action, f"Tapped '{template}' ({slot_info} slots used)")

    def _check_formations_busy(self, action: dict) -> ActionResult:
        """
        Abort the task if max formations have already been dispatched this session,
        or if a slot scan shows all eligible slots are occupied.

        When 'slots' are provided (same format as tap_free_formation), uses the
        proven slot-by-slot timer OCR scan. This is far more reliable than the
        small counter region OCR and works on the world view before any navigation.

        Parameters
        ----------
        slots           : list of {"x_pct", "y_pct"} — same as tap_free_formation
        locked_template : template filename to exclude last slot (optional)
        timer_width_pct : half-width of OCR region per slot (default 6.0)
        """
        import re as _re

        # ── Session counter check ─────────────────────────────────────────
        max_formations = int(self.farm_settings.get("gathering", {}).get("max_formations", 0))
        if max_formations > 0 and self._formations_sent >= max_formations:
            msg = f"Max formations reached ({self._formations_sent}/{max_formations}) — skipping task"
            self._log(f"  ⚠ {msg}")
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)

        # ── Slot-by-slot scan (if slots provided) ────────────────────────
        slots = action.get("slots", [])
        if not slots:
            # No slots provided — skip slot scan, proceed
            return self._ok(action, "Formations available (no slot scan)")

        timer_re      = _re.compile(r'[A-Za-z0-9]{2,}[hHmMsS](?:\b|$)|\d{3,}|\d+:\d+')
        timer_width   = float(action.get("timer_width_pct", 6.0))
        locked_template = action.get("locked_template")
        threshold     = float(action.get("threshold")) if action.get("threshold") is not None else None

        screenshot = self.bot.screenshot()
        w, h       = screenshot.size

        active_slots = list(slots)
        if locked_template and active_slots:
            path  = self._template_path(locked_template)
            match = self.vision.find_template(screenshot, path, threshold=threshold)
            if match:
                active_slots = active_slots[:-1]
                self._log(f"  [check_formations_busy] last slot locked — checking {len(active_slots)} slot(s)")

        all_y      = [float(s.get("y_pct", 50)) for s in slots]
        free_count = 0

        for idx, slot in enumerate(active_slots):
            sx_pct = float(slot.get("x_pct", 50))
            sy_pct = float(slot.get("y_pct", 50))

            orig_idx = slots.index(slot)
            if orig_idx + 1 < len(all_y):
                next_y  = all_y[orig_idx + 1]
                scan_y2 = sy_pct + (next_y - sy_pct) * 0.85
            else:
                scan_y2 = sy_pct + 6.0

            x1 = int(w * max(sx_pct - timer_width, 0) / 100)
            y1 = int(h * (sy_pct + 1.0) / 100)
            x2 = int(w * min(sx_pct + timer_width, 100) / 100)
            y2 = int(h * scan_y2 / 100)

            ocr_results = self.vision.read_text(screenshot, region=(x1, y1, x2, y2),
                                                min_confidence=0.3)
            raw       = " ".join(r.text for r in ocr_results).strip()
            has_timer = bool(timer_re.search(raw))
            self._log(f"  [check_formations_busy] slot {idx + 1} OCR: '{raw}' timer={has_timer} → {'busy' if has_timer else 'free'}")
            if not has_timer:
                free_count += 1

        self._log(f"  [check_formations_busy] {free_count}/{len(active_slots)} slot(s) free")

        if free_count == 0:
            msg = "All formation slots busy — skipping task"
            self._log(f"  ⚠ {msg}")
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)

        return self._ok(action, f"Formations available ({free_count} free)")

    def _tap_template_or_zone(self, action: dict) -> ActionResult:
        """
        Try to find and tap a template. If not found, tap a fallback screen zone.

        Parameters
        ----------
        template  : template filename to look for (required)
        x_pct     : fallback tap X position as % of screen width  (required if template may not be found)
        y_pct     : fallback tap Y position as % of screen height (required if template may not be found)
        threshold : vision confidence override (optional)
        """
        template = action.get("template")
        if not template:
            return self._fail(action, "tap_template_or_zone requires 'template'")

        x_pct = action.get("x_pct")
        y_pct = action.get("y_pct")
        threshold = float(action.get("threshold")) if action.get("threshold") is not None else None

        screenshot = self.bot.screenshot()
        path   = self._template_path(template)
        tmatch = self.vision.find_template(screenshot, path, threshold=threshold)
        conf   = getattr(tmatch, "confidence", 0) if tmatch else 0
        self._log(f"  [tap_template_or_zone] '{template}' conf={conf:.3f} found={bool(tmatch)}")

        if tmatch:
            self.bot.tap(tmatch.x, tmatch.y)
            return self._ok(action, f"Tapped template '{template}'")

        if x_pct is None or y_pct is None:
            return self._fail(action, f"tap_template_or_zone: '{template}' not found and no fallback zone specified")

        w, h = self._screen_size()
        fx = int(w * float(x_pct) / 100)
        fy = int(h * float(y_pct) / 100)
        self._log(f"  [tap_template_or_zone] '{template}' not found — fallback tap ({x_pct}%, {y_pct}%) = ({fx},{fy})")
        self.bot.tap(fx, fy)
        return self._ok(action, f"'{template}' not found — tapped fallback zone ({x_pct}%, {y_pct}%)")

    def _find_template_with_scroll(self, action: dict) -> ActionResult:
        """
        Look for a template on screen. If not found, scroll right then left and
        recheck after each scroll. Aborts the task if still not found after both.

        Parameters
        ----------
        template        : template filename to look for and tap (required)
        scroll_x_pct   : X centre of the scroll swipe as % of screen width  (default 70)
        scroll_y_pct   : Y centre of the scroll swipe as % of screen height (default 67)
        distance_pct   : swipe distance as % of screen width                (default 40)
        duration_ms    : swipe duration in milliseconds                      (default 500)
        wait_seconds   : pause after each scroll before rechecking           (default 1.5)
        log_not_found  : message logged and returned when aborting task
        threshold      : vision confidence override (optional)
        """
        import time as _time

        template = action.get("template")
        if not template:
            return self._fail(action, "find_template_with_scroll requires 'template'")

        threshold    = float(action.get("threshold")) if action.get("threshold") is not None else None
        scroll_x_pct = float(action.get("scroll_x_pct", 70))
        scroll_y_pct = float(action.get("scroll_y_pct", 67))
        distance_pct = float(action.get("distance_pct", 40))
        duration_ms  = int(action.get("duration_ms", 500))
        wait_secs    = float(action.get("wait_seconds", 1.5))
        not_found_msg = action.get("log_not_found",
                                   f"'{template}' not found after scrolling — skipping task")

        path = self._template_path(template)

        def _check():
            ss   = self.bot.screenshot()
            m    = self.vision.find_template(ss, path, threshold=threshold)
            conf = getattr(m, "confidence", 0) if m else 0
            self._log(f"  [find_template_with_scroll] '{template}' conf={conf:.3f} found={bool(m)}")
            return m

        # ── Attempt 1: check immediately ─────────────────────────────────
        match = _check()
        if match:
            self.bot.tap(match.x, match.y)
            return self._ok(action, f"Found and tapped '{template}' (no scroll needed)")

        w, h = self._screen_size()
        sx   = int(w * scroll_x_pct / 100)
        sy   = int(h * scroll_y_pct / 100)
        dist = int(w * distance_pct / 100)

        # ── Attempt 2: scroll right, then check ──────────────────────────
        self._log(f"  [find_template_with_scroll] not found — scrolling right")
        self.bot.swipe(sx, sy, sx - dist, sy, duration_ms=duration_ms)
        _time.sleep(wait_secs)
        match = _check()
        if match:
            self.bot.tap(match.x, match.y)
            return self._ok(action, f"Found and tapped '{template}' after scroll right")

        # ── Attempt 3: scroll left, then check ───────────────────────────
        self._log(f"  [find_template_with_scroll] not found — scrolling left")
        left_sx = int(w * 0.2)
        self.bot.swipe(left_sx, sy, left_sx + dist, sy, duration_ms=duration_ms)
        _time.sleep(wait_secs)
        match = _check()
        if match:
            self.bot.tap(match.x, match.y)
            return self._ok(action, f"Found and tapped '{template}' after scroll left")

        # ── Not found after both scrolls ─────────────────────────────────
        self._log(f"  ⚠ {not_found_msg}")
        return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=not_found_msg)

    def _tap_template_or_template(self, action: dict) -> ActionResult:
        """
        Try to find and tap a primary template. If not found, try a fallback template.
        If neither is found, skips (if required=false) or fails.

        Parameters
        ----------
        template          : primary template filename (required)
        fallback_template : fallback template filename (required)
        threshold         : vision confidence override (optional)
        required          : if False, returns SKIPPED when neither found (default True)
        """
        template          = action.get("template")
        fallback_template = action.get("fallback_template")
        if not template or not fallback_template:
            return self._fail(action,
                "tap_template_or_template requires 'template' and 'fallback_template'")

        threshold  = float(action.get("threshold")) if action.get("threshold") is not None else None
        screenshot = self.bot.screenshot()

        # ── Try primary ──────────────────────────────────────────────────
        path   = self._template_path(template)
        tmatch = self.vision.find_template(screenshot, path, threshold=threshold)
        conf   = getattr(tmatch, "confidence", 0) if tmatch else 0
        self._log(f"  [tap_template_or_template] '{template}' conf={conf:.3f} found={bool(tmatch)}")
        if tmatch:
            self.bot.tap(tmatch.x, tmatch.y)
            return self._ok(action, f"Tapped primary template '{template}'")

        # ── Try fallback ─────────────────────────────────────────────────
        fb_path  = self._template_path(fallback_template)
        fb_match = self.vision.find_template(screenshot, fb_path, threshold=threshold)
        fb_conf  = getattr(fb_match, "confidence", 0) if fb_match else 0
        self._log(f"  [tap_template_or_template] fallback '{fallback_template}' conf={fb_conf:.3f} found={bool(fb_match)}")
        if fb_match:
            self.bot.tap(fb_match.x, fb_match.y)
            return self._ok(action,
                f"Primary '{template}' not found — tapped fallback '{fallback_template}'")

        # ── Neither found ────────────────────────────────────────────────
        if not action.get("required", True):
            return ActionResult(
                status=ActionStatus.SKIPPED,
                action=action,
                message=f"Neither '{template}' nor '{fallback_template}' found — skipping",
            )
        return self._fail(action, f"Neither '{template}' nor '{fallback_template}' found")

    def _tap_free_formation(self, action: dict) -> ActionResult:
        """
        Scan formation slots for one with no active timer, then tap it.

        Takes a screenshot, optionally checks a locked template to exclude the last
        slot, then OCRs the timer region below each slot. Taps the first slot whose
        timer area contains no time-like text (e.g. 3h31m, 24s, 1m30s, 2:45).
        Returns ABORT_TASK if every eligible slot is busy or returning.

        Parameters
        ----------
        slots               : list of {"x_pct": float, "y_pct": float} per slot
        locked_template     : template filename — if found on screen, last slot is excluded
        timer_y_offset_pct  : Y% below slot centre where timer text starts (default 3.5)
        timer_height_pct    : height of OCR scan region as % of screen height (default 3.5)
        timer_width_pct     : half-width of OCR region per slot as % of screen (default 6.0)
        log_all_busy        : message logged when all slots are busy
        threshold           : vision confidence override for locked_template (optional)
        """
        import re as _re

        slots = action.get("slots", [])
        if not slots:
            return self._fail(action, "tap_free_formation requires 'slots'")

        locked_template    = action.get("locked_template")
        timer_width        = float(action.get("timer_width_pct", 6.0))
        log_all_busy       = action.get("log_all_busy",
                                        "All formations busy or returning — skipping task")
        threshold          = float(action.get("threshold")) if action.get("threshold") is not None else None

        screenshot = self.bot.screenshot()
        w, h       = screenshot.size

        # ── Determine eligible slots ─────────────────────────────────────
        active_slots = list(slots)
        if locked_template and active_slots:
            path  = self._template_path(locked_template)
            match = self.vision.find_template(screenshot, path, threshold=threshold)
            conf  = getattr(match, "confidence", 0) if match else 0
            self._log(f"  [tap_free_formation] '{locked_template}' conf={conf:.3f} found={bool(match)}")
            if match:
                active_slots = active_slots[:-1]
                self._log(f"  [tap_free_formation] last slot locked — checking {len(active_slots)} slot(s)")

        # Matches timer text including OCR misreads. Three patterns:
        #   1. Word ending in h/m/s: catches "2h56m", "24s", and garbled "Imzs"
        #   2. 3+ consecutive digits: catches colon-timers where OCR drops the colon ("5:38" → "598")
        #   3. Explicit colon format: catches "5:38" when OCR reads it correctly
        # Free formations show only an idle icon — no digits or time-unit text.
        timer_re = _re.compile(r'[A-Za-z0-9]{2,}[hHmMsS](?:\b|$)|\d{3,}|\d+:\d+')

        # ── Scan each slot ───────────────────────────────────────────────
        # Use the space between this slot's Y and the next slot's Y as the OCR
        # region — this avoids hardcoded offsets and always lands on the timer.
        all_y = [float(s.get("y_pct", 50)) for s in slots]  # full list for spacing

        for idx, slot in enumerate(active_slots):
            sx_pct = float(slot.get("x_pct", 50))
            sy_pct = float(slot.get("y_pct", 50))

            # Bottom of scan region: midpoint to next slot, or +6% for last slot
            orig_idx = slots.index(slot)
            if orig_idx + 1 < len(all_y):
                next_y = all_y[orig_idx + 1]
                scan_y2 = sy_pct + (next_y - sy_pct) * 0.85
            else:
                scan_y2 = sy_pct + 6.0

            x1 = int(w * max(sx_pct - timer_width, 0) / 100)
            y1 = int(h * (sy_pct + 1.0) / 100)   # small gap below slot centre
            x2 = int(w * min(sx_pct + timer_width, 100) / 100)
            y2 = int(h * scan_y2 / 100)

            ocr_results = self.vision.read_text(screenshot, region=(x1, y1, x2, y2),
                                                min_confidence=0.3)
            raw       = " ".join(r.text for r in ocr_results).strip()
            has_timer = bool(timer_re.search(raw))
            self._log(f"  [tap_free_formation] slot {idx + 1} OCR ({sy_pct+1.0:.1f}%-{scan_y2:.1f}%): '{raw}' timer={has_timer}")

            if idx in self._used_slot_indices:
                self._log(f"  [tap_free_formation] slot {idx + 1} already used this session — skipping")
                continue

            if not has_timer:
                sx = int(w * sx_pct / 100)
                sy = int(h * sy_pct / 100)
                self._log(f"  [tap_free_formation] slot {idx + 1} is free — tapping ({sx_pct}%, {sy_pct}%)")
                self.bot.tap(sx, sy)
                self._formations_sent += 1
                self._used_slot_indices.add(idx)
                self._log(f"  [tap_free_formation] formations sent this session: {self._formations_sent}")
                return self._ok(action, f"Tapped free formation slot {idx + 1}")

        # ── All slots busy ───────────────────────────────────────────────
        self._log(f"  ⚠ {log_all_busy}")
        return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=log_all_busy)

    def _repeat_if_template(self, action: dict) -> ActionResult:
        """
        Keep tapping a template until it disappears from screen.
        Useful for collecting multiple rewards, dismissing stacked popups,
        or training troops until the queue is full.
        """
        template = action.get("template")
        if not template:
            return self._fail(action, "repeat_if_template requires 'template'")
        path = self._template_path(template)
        max_taps = int(action.get("max_taps", 20))
        delay = float(action.get("delay", 0.5))
        threshold = float(action.get("threshold")) if action.get("threshold") is not None else None
        taps = 0

        on_each = action.get("on_each", [])

        while taps < max_taps:
            if self._stop_event and self._stop_event.is_set():
                self._log(f"  repeat_if_template: stop requested — exiting loop after {taps} tap(s)")
                break
            screenshot = self.bot.screenshot()
            match = self.vision.find_template(screenshot, path, threshold=threshold)
            if not match:
                self._log(f"  repeat_if_template: '{template}' no longer found after {taps} tap(s)")
                break
            self.bot.tap(match.x, match.y)
            taps += 1
            self._log(f"  repeat_if_template: tap {taps} on '{template}'")
            time.sleep(delay)

            # Run sub-actions after each tap
            for sub in on_each:
                if self._stop_event and self._stop_event.is_set():
                    break
                sub_result = self.execute(sub)
                if sub_result.status == ActionStatus.ABORT_TASK:
                    return sub_result

        return self._ok(action, f"Repeated tap '{template}' x{taps}")

    def _verify_setting_template(self, action: dict) -> ActionResult:
        """
        Verify the on-screen boomer level matches the farm's rally.boomer_level setting.

        Required action fields:
            setting          - dot-path into farm_settings, e.g. "rally.boomer_level"
            template_pattern - template filename with {value} placeholder,
                               e.g. "btn_lvl_{value}_boomer.png"

        Optional:
            required         - if True (default False), FAIL when level doesn't match
            threshold        - vision confidence override

        Example JSON step:
            {
                "action": "verify_setting_template",
                "setting": "rally.boomer_level",
                "template_pattern": "btn_lvl_{value}_boomer.png",
                "required": false
            }
        """
        setting_path = action.get("setting")
        template_pattern = action.get("template_pattern")
        required = action.get("required", False)

        if not setting_path or not template_pattern:
            return self._fail(action, "verify_setting_template requires 'setting' and 'template_pattern'")

        # Resolve value from farm_settings using dot-notation (e.g. "rally.boomer_level")
        value = self.farm_settings
        for key in setting_path.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)

        if value is None:
            self._log(f"  [verify_setting_template] setting '{setting_path}' not found in farm_settings — skipping")
            return self._ok(action, f"Skipped: setting '{setting_path}' not configured")

        template_name = template_pattern.replace("{value}", str(value))
        path = self._template_path(template_name)

        threshold = float(action.get("threshold")) if action.get("threshold") is not None else None
        screenshot = self.bot.screenshot()
        match = self.vision.find_template(screenshot, path, threshold=threshold)

        conf = match.confidence if match else 0.0
        self._log(f"  [verify_setting_template] '{template_name}' conf={conf:.3f} found={bool(match)}")

        if match:
            return self._ok(action, f"Verified level {value} on screen (conf={conf:.2f})")

        msg = f"Level {value} template '{template_name}' not found on screen"
        if required:
            return self._fail(action, msg)
        # Not required — log and continue (bot will proceed with whatever level is shown)
        self._log(f"  [verify_setting_template] WARNING: {msg} — continuing anyway")
        return self._ok(action, f"Level mismatch noted but not required: {msg}")

    def _adjust_boomer_level(self, action: dict) -> ActionResult:
        """
        Detect the boomer level currently shown on screen and tap + or - until
        it matches the rally.boomer_level farm setting.

        Action fields:
            setting          - dot-path to target level, e.g. "rally.boomer_level"
            template_pattern - e.g. "btn_lvl_{value}_boomer.png"
            plus_template    - template to tap when level is too low,  e.g. "btn_plus_boomer.png"
            minus_template   - template to tap when level is too high, e.g. "btn_subtract_boomer_lvl.png"

        Optional:
            min_level    - lowest level to scan for (default 1)
            max_level    - highest level to scan for (default 10)
            max_attempts - safety cap on total taps (default 20)
            tap_delay    - seconds to wait between taps (default 1.0)
            threshold    - vision confidence override

        Example JSON:
            {
                "action": "adjust_boomer_level",
                "setting": "rally.boomer_level",
                "template_pattern": "btn_lvl_{value}_boomer.png",
                "plus_template": "btn_plus_boomer.png",
                "minus_template": "btn_subtract_boomer_lvl.png"
            }
        """
        setting_path    = action.get("setting", "rally.boomer_level")
        template_pattern = action.get("template_pattern", "btn_lvl_{value}_boomer.png")
        plus_tpl        = action.get("plus_template",  "btn_plus_boomer.png")
        minus_tpl       = action.get("minus_template", "btn_subtract_boomer_lvl.png")
        min_level       = int(action.get("min_level",    1))
        max_level       = int(action.get("max_level",   10))
        max_attempts    = int(action.get("max_attempts", 20))
        tap_delay       = float(action.get("tap_delay",  1.0))
        threshold       = float(action.get("threshold")) if action.get("threshold") is not None else None

        # ── Resolve target level from farm settings ───────────────────
        value = self.farm_settings
        for key in setting_path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            self._log(f"  [adjust_boomer_level] setting '{setting_path}' not found — skipping")
            return self._ok(action, f"Skipped: '{setting_path}' not configured")
        target = int(value)
        self._log(f"  [adjust_boomer_level] target level = {target}")

        def detect_current_level(screenshot) -> int | None:
            """Check each level template and return the best-confidence match."""
            best_level, best_conf = None, 0.0
            for lvl in range(min_level, max_level + 1):
                tpl_name = template_pattern.replace("{value}", str(lvl))
                path = self._template_path(tpl_name)
                match = self.vision.find_template(screenshot, path, threshold=threshold)
                if match and match.confidence > best_conf:
                    best_level, best_conf = lvl, match.confidence
            return best_level

        def tap_tpl(tpl_name: str) -> bool:
            """Take a fresh screenshot, find tpl_name, and tap it. Returns True on success."""
            path = self._template_path(tpl_name)
            shot = self.bot.screenshot()
            match = self.vision.find_template(shot, path, threshold=threshold)
            if match:
                self.bot.tap(match.x, match.y)
                return True
            self._log(f"  [adjust_boomer_level] WARNING: '{tpl_name}' not found on screen")
            return False

        # ── Adjust loop ───────────────────────────────────────────────
        for attempt in range(1, max_attempts + 1):
            screenshot = self.bot.screenshot()
            current = detect_current_level(screenshot)

            if current is None:
                self._log(f"  [adjust_boomer_level] attempt {attempt}: no level template matched — retrying")
                time.sleep(tap_delay)
                continue

            self._log(f"  [adjust_boomer_level] attempt {attempt}: current={current} target={target}")

            if current == target:
                return self._ok(action, f"Boomer level set to {target}")

            if current < target:
                self._log(f"  [adjust_boomer_level] tapping + ({plus_tpl})")
                tap_tpl(plus_tpl)
            else:
                self._log(f"  [adjust_boomer_level] tapping - ({minus_tpl})")
                tap_tpl(minus_tpl)

            time.sleep(tap_delay)

        return self._fail(action, f"adjust_boomer_level: could not reach level {target} after {max_attempts} attempts")

    def _adjust_resource_level(self, action: dict) -> ActionResult:
        """
        Detect the resource site level shown on the search panel using the same
        btn_lvl_{value}_boomer.png templates as the boomer level adjuster, then
        tap + or - until it matches gathering.resource_site_level from farm settings.

        Action fields (all optional):
            template_pattern - filename with {value} placeholder (default btn_lvl_{value}_boomer.png)
            plus_template    - tap to increase level             (default btn_plus_boomer.png)
            minus_template   - tap to decrease level             (default btn_subtract_boomer_lvl.png)
            min_level        - lowest level to scan for          (default 1)
            max_level        - highest level to scan for         (default 10)
            max_attempts     - safety limit on taps              (default 20)
            tap_delay        - seconds between taps              (default 0.6)
        """
        target = int(self.farm_settings.get("gathering", {}).get("resource_site_level", 0))
        if target <= 0:
            self._log("  [adjust_resource_level] resource_site_level not set — skipping")
            return self._ok(action, "Skipped: resource_site_level not configured")

        template_pattern = action.get("template_pattern", "btn_lvl_{value}_boomer.png")
        plus_tpl         = action.get("plus_template",    "btn_plus_boomer.png")
        minus_tpl        = action.get("minus_template",   "btn_subtract_boomer_lvl.png")
        min_level        = int(action.get("min_level",    1))
        max_level        = int(action.get("max_level",    10))
        max_attempts     = int(action.get("max_attempts", 20))
        tap_delay        = float(action.get("tap_delay",  0.6))
        # Restrict search to the bottom panel so the game world above can't cause false positives
        panel_y_pct      = float(action.get("panel_y_pct", 60))

        self._log(f"  [adjust_resource_level] target level = {target}")

        def detect_current_level(screenshot) -> int | None:
            w, h = screenshot.size
            region = (0, int(h * panel_y_pct / 100), w, h)
            best_level, best_conf = None, 0.0
            for lvl in range(min_level, max_level + 1):
                tpl_name = template_pattern.replace("{value}", str(lvl))
                path  = self._template_path(tpl_name)
                match = self.vision.find_template(screenshot, path, region=region)
                conf  = getattr(match, "confidence", 0) if match else 0
                if conf > best_conf:
                    best_conf, best_level = conf, lvl
            if best_level is not None:
                self._log(f"  [adjust_resource_level] detected level {best_level} (conf={best_conf:.3f})")
            return best_level

        def tap_tpl(tpl_name: str) -> bool:
            screenshot = self.bot.screenshot()
            path  = self._template_path(tpl_name)
            match = self.vision.find_template(screenshot, path)
            if match:
                self.bot.tap(match.x, match.y)
                return True
            self._log(f"  [adjust_resource_level] WARNING: '{tpl_name}' not found on screen")
            return False

        for attempt in range(1, max_attempts + 1):
            screenshot = self.bot.screenshot()
            current = detect_current_level(screenshot)

            if current is None:
                self._log(f"  [adjust_resource_level] attempt {attempt}: no level template matched — retrying")
                time.sleep(tap_delay)
                continue

            self._log(f"  [adjust_resource_level] attempt {attempt}: current={current} target={target}")

            if current == target:
                return self._ok(action, f"Resource level set to {target}")

            if current < target:
                self._log(f"  [adjust_resource_level] tapping + ({plus_tpl})")
                tap_tpl(plus_tpl)
            else:
                self._log(f"  [adjust_resource_level] tapping - ({minus_tpl})")
                tap_tpl(minus_tpl)

            time.sleep(tap_delay)

        return self._fail(action, f"adjust_resource_level: could not reach level {target} after {max_attempts} attempts")

    def _search_resource_level(self, action: dict) -> ActionResult:
        """
        Set the resource search panel to a level, execute the search, tap the result zone,
        and check for the gather button.  If no gather button appears, reopen the search
        panel, re-select the resource type, step up one level, and retry until max_level.

        Action fields:
            resource_template    - resource type tab to (re-)select, e.g. btn_gather_food.png
            search_lens_template - icon that opens the search panel   (default btn_search_lens_icon.png)
            search_lens_x_pct    - fallback X% if lens template not found (default 5.2)
            search_lens_y_pct    - fallback Y% if lens template not found (default 80.7)
            scroll_x_pct         - X% anchor for scroll when finding resource tab (default 70.6)
            scroll_y_pct         - Y% anchor for scroll when finding resource tab (default 67.4)
            template_pattern     - level template with {value} placeholder  (default btn_resource_lvl_{value}.png)
            plus_template        - tap to increment level                    (default btn_plus_boomer.png)
            minus_template       - tap to decrement level                    (default btn_subtract_boomer_lvl.png)
            search_template      - search/confirm button                     (default btn_search.png)
            gather_template      - gather CTA confirming a resource was found (default btn_gather.png)
            resource_x_pct       - X% to tap on the map after searching     (default 49.1)
            resource_y_pct       - Y% to tap on the map after searching     (default 49.4)
            min_level            - lowest level to attempt                   (default 1)
            max_level            - highest level to attempt                  (default 6)
            panel_y_pct          - top of level-detection region             (default 60)
            tap_delay            - seconds between +/- taps                  (default 0.6)
            max_attempts         - safety cap on level-adjustment taps       (default 20)
        """
        target        = int(self.farm_settings.get("gathering", {}).get("resource_site_level", 1))
        resource_tpl  = action.get("resource_template")
        lens_tpl      = action.get("search_lens_template", "btn_search_lens_icon.png")
        lens_x        = float(action.get("search_lens_x_pct", 5.2))
        lens_y        = float(action.get("search_lens_y_pct", 80.7))
        scroll_x      = float(action.get("scroll_x_pct", 70.6))
        scroll_y      = float(action.get("scroll_y_pct", 67.4))
        tpl_pattern   = action.get("template_pattern", "btn_resource_lvl_{value}.png")
        plus_tpl      = action.get("plus_template",    "btn_plus_boomer.png")
        minus_tpl     = action.get("minus_template",   "btn_subtract_boomer_lvl.png")
        search_tpl    = action.get("search_template",  "btn_search.png")
        gather_tpl    = action.get("gather_template",  "btn_gather.png")
        res_x         = float(action.get("resource_x_pct", 49.1))
        res_y         = float(action.get("resource_y_pct", 49.4))
        min_level     = int(action.get("min_level",    1))
        max_level     = int(action.get("max_level",    6))
        panel_y_pct   = float(action.get("panel_y_pct", 60))
        tap_delay     = float(action.get("tap_delay",  0.6))
        max_attempts  = int(action.get("max_attempts", 20))

        start_level = max(min_level, min(target, max_level))

        def detect_level(screenshot) -> int | None:
            w, h = screenshot.size
            region = (0, int(h * panel_y_pct / 100), w, h)
            best_level, best_conf = None, 0.0
            for lvl in range(min_level, max_level + 1):
                tpl_name = tpl_pattern.replace("{value}", str(lvl))
                match = self.vision.find_template(screenshot, self._template_path(tpl_name), region=region)
                conf = getattr(match, "confidence", 0) if match else 0
                if conf > best_conf:
                    best_conf, best_level = conf, lvl
            if best_level is not None:
                self._log(f"  [search_resource_level] detected level {best_level} (conf={best_conf:.3f})")
            return best_level

        def set_level(target_lvl: int) -> bool:
            for attempt in range(1, max_attempts + 1):
                current = detect_level(self.bot.screenshot())
                if current is None:
                    self._log(f"  [search_resource_level] set_level attempt {attempt}: no match — retrying")
                    time.sleep(tap_delay)
                    continue
                self._log(f"  [search_resource_level] set_level attempt {attempt}: current={current} target={target_lvl}")
                if current == target_lvl:
                    return True
                btn = plus_tpl if current < target_lvl else minus_tpl
                match = self.vision.find_template(self.bot.screenshot(), self._template_path(btn))
                if match:
                    self.bot.tap(match.x, match.y)
                else:
                    self._log(f"  [search_resource_level] '{btn}' not found")
                time.sleep(tap_delay)
            return False

        def reopen_search_panel():
            """Re-open the search panel and re-select the resource type tab."""
            shot = self.bot.screenshot()
            lens_match = self.vision.find_template(shot, self._template_path(lens_tpl))
            if lens_match:
                self.bot.tap(lens_match.x, lens_match.y)
            else:
                w, h = shot.size
                self._log(f"  [search_resource_level] lens template not found — tapping fallback zone")
                self.bot.tap(int(w * lens_x / 100), int(h * lens_y / 100))
            time.sleep(2.0)

            if not resource_tpl:
                return
            shot = self.bot.screenshot()
            res_match = self.vision.find_template(shot, self._template_path(resource_tpl))
            if res_match:
                self._log(f"  [search_resource_level] re-selected '{resource_tpl}'")
                self.bot.tap(res_match.x, res_match.y)
            else:
                # Try scrolling once to reveal the tab
                w, h = shot.size
                self.bot.swipe(
                    int(w * scroll_x / 100), int(h * scroll_y / 100),
                    int(w * (scroll_x - 30) / 100), int(h * scroll_y / 100),
                    300,
                )
                time.sleep(1.0)
                shot = self.bot.screenshot()
                res_match = self.vision.find_template(shot, self._template_path(resource_tpl))
                if res_match:
                    self._log(f"  [search_resource_level] re-selected '{resource_tpl}' after scroll")
                    self.bot.tap(res_match.x, res_match.y)
                else:
                    self._log(f"  [search_resource_level] WARNING: could not re-select '{resource_tpl}'")
            time.sleep(2.0)

        for lvl in range(start_level, max_level + 1):
            self._log(f"  [search_resource_level] trying level {lvl}")

            if not set_level(lvl):
                self._log(f"  [search_resource_level] could not set level {lvl} — skipping")
                if lvl < max_level:
                    reopen_search_panel()
                continue

            # Execute search
            match = self.vision.find_template(self.bot.screenshot(), self._template_path(search_tpl))
            if not match:
                self._log(f"  [search_resource_level] '{search_tpl}' not found — cannot search")
                if lvl < max_level:
                    reopen_search_panel()
                continue
            self.bot.tap(match.x, match.y)
            time.sleep(2.0)

            # Tap result zone
            shot = self.bot.screenshot()
            w, h = shot.size
            self._log(f"  [search_resource_level] tapping resource zone ({res_x}%, {res_y}%)")
            self.bot.tap(int(w * res_x / 100), int(h * res_y / 100))
            time.sleep(2.0)

            # Check for gather button
            match = self.vision.find_template(self.bot.screenshot(), self._template_path(gather_tpl))
            if match:
                self._log(f"  [search_resource_level] gather button found at level {lvl} — tapping")
                self.bot.tap(match.x, match.y)
                return self._ok(action, f"Resource found and gather tapped at level {lvl}")

            self._log(f"  [search_resource_level] no resource at level {lvl} — reopening search for level {lvl + 1}")
            if lvl < max_level:
                reopen_search_panel()

        return ActionResult(
            status=ActionStatus.ABORT_TASK, action=action,
            message=f"No resource found at levels {start_level}–{max_level}",
        )

    def _rally_count_check(self, action: dict) -> ActionResult:
        """
        Check and increment the daily rally counter for this farm.

        Reads logs/rally_counts.json keyed by "<port>_<YYYY-MM-DD>".
        If the count has reached rally.max_rallies_per_day → ABORT_TASK.
        Otherwise increments the counter, saves, and returns SUCCESS.

        farm_settings must contain {"rally": {"max_rallies_per_day": N}}.
        """
        import json as _json
        from pathlib import Path
        from datetime import date

        rally_cfg  = self.farm_settings.get("rally", {})
        max_rallies = int(rally_cfg.get("max_rallies_per_day", 999))

        port  = getattr(self.bot, "port", 0)
        today = str(date.today())
        key   = f"{port}_{today}"

        counts_path = Path("logs/rally_counts.json")
        counts: dict = {}
        if counts_path.exists():
            try:
                with open(counts_path) as f:
                    counts = _json.load(f)
            except Exception:
                counts = {}

        current = counts.get(key, 0)

        if current >= max_rallies:
            msg = f"Rally limit reached ({current}/{max_rallies}) for today — skipping"
            self._log(f"  ⏭ {msg}")
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)

        counts[key] = current + 1
        Path("logs").mkdir(exist_ok=True)
        with open(counts_path, "w") as f:
            _json.dump(counts, f, indent=2)

        msg = f"Rally {counts[key]}/{max_rallies} — proceeding"
        self._log(f"  ✓ {msg}")
        return self._ok(action, msg)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _template_path(self, template: str) -> str:
        """Resolve template filename to full path."""
        if "\\" in template or "/" in template:
            return template  # already a full path
        return f"{self.template_dir}/{template}"

    def _ok(self, action: dict, message: str) -> ActionResult:
        return ActionResult(status=ActionStatus.SUCCESS, action=action, message=message)

    def _fail(self, action: dict, message: str) -> ActionResult:
        return ActionResult(status=ActionStatus.FAILED, action=action, message=message)

    def _log(self, message: str):
        try:
            self.logger.info(message)
        except Exception:
            # Swallow encoding errors from Windows console handler — GUI still gets the message
            pass
        if self.log_callback:
            self.log_callback(message)

    def _params_str(self, action: dict) -> str:
        """Format action params for logging (exclude the 'action' key)."""
        params = {k: v for k, v in action.items() if k != "action"}
        return str(params) if params else ""
