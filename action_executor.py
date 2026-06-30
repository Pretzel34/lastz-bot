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
from paths import get_resource_dir


# ---------------------------------------------------------------------------
# Result Types
# ---------------------------------------------------------------------------

class ActionStatus(Enum):
    SUCCESS      = "success"
    FAILED       = "failed"
    SKIPPED      = "skipped"
    TIMEOUT      = "timeout"
    ABORT_TASK   = "abort_task"
    RESTART_TASK = "restart_task"


@dataclass
class ActionResult:
    status: ActionStatus
    action: dict
    message: str = ""
    duration_ms: int = 0
    match: Optional[MatchResult] = None
    skip_to: Optional[int] = None  # 0-indexed action index to jump to next

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
        read_server_time         - OCR Apocalypse Time screen, save server date/time to logs/server_time.json
        read_local_time          - OCR local time panel, save to logs/local_time.json
        ensure_server_time_mode  - if panel is showing local time, tap btn_alternate_time.png to switch to server time
        ensure_local_time_mode   - if panel is showing server time, tap btn_alternate_time.png to switch to local time
        skip_if_server_time_fresh - ABORT_TASK if logs/server_time.json was written within the last 24 hours
        skip_if_done_today       - ABORT_TASK if task_key was already completed on today's server date for this farm
        mark_done_today          - record task_key as completed on today's server date for this farm
        read_fp_schedule         - OCR Full Preparedness Event Calendar and save today's time→task schedule (self-skips if already current)
        dispatch_fp_task         - derive current server time, look up fp_schedule.json, run the matching child task
        loop_until_template      - run on_each actions repeatedly until any of the specified templates appears
        run_task_if_template     - run a sub-task JSON only if an identifying template is visible; silently skips if not found
    """

    def __init__(
        self,
        bot: ADBWrapper,
        vision: VisionEngine,
        template_dir: str = "templates",
        log_callback: Optional[Callable[[str], None]] = None,
        farm_settings: Optional[dict] = None,
        stop_event=None,
        emulator_type: str = "MEmu",
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
        self.emulator_type = emulator_type

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
        if self._stop_event and self._stop_event.is_set():
            return ActionResult(status=ActionStatus.SKIPPED, action=action, message="Bot stopped")

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
        if result.status == ActionStatus.SUCCESS and action.get("finish_task_on_success"):
            result.status = ActionStatus.ABORT_TASK
        status_icon = "✓" if result else "✗"
        self._log(f"  {status_icon} {result.message} ({result.duration_ms}ms)")
        return result

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    def _dispatch(self, action_type: str, action: dict) -> ActionResult:
        handlers = {
            "rally_count_check":        self._rally_count_check,
            "rally_count_record":       self._rally_count_record,
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
            "verify_in_hq":             self._verify_in_hq,
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
            "if_template_found":        self._if_template_found,
            "tap_if_slots_available":   self._tap_if_slots_available,
            "check_formations_busy":    self._check_formations_busy,
            "tap_template_or_zone":     self._tap_template_or_zone,
            "find_template_with_scroll": self._find_template_with_scroll,
            "tap_template_search":      self._tap_template_search,
            "tap_template_or_template": self._tap_template_or_template,
            "tap_first_found":          self._tap_first_found,
            "tap_template_or_ocr_pattern": self._tap_template_or_ocr_pattern,
            "tap_ocr_pattern":          self._tap_ocr_pattern,
            "tap_active_alliance_mine":    self._tap_active_alliance_mine,
            "tap_free_formation":       self._tap_free_formation,
            "check_claimed":            self._check_claimed,
            "repeat_if_template":       self._repeat_if_template,
            "verify_setting_template":  self._verify_setting_template,
            "adjust_boomer_level":      self._adjust_boomer_level,
            "adjust_boomer_level_ocr":  self._adjust_boomer_level_ocr,
            "adjust_boomer_level_from_one": self._adjust_boomer_level_from_one,
            "adjust_resource_level":    self._adjust_resource_level,
            "search_resource_level":    self._search_resource_level,
            "compare_resources":        self._compare_resources,
            "read_resource_priority":   self._read_resource_priority,
            "loop_until_template":      self._loop_until_template,
            "loop_while_template":      self._loop_while_template,
            "loop_task":                self._loop_task,
            "run_task":                 self._run_task,
            "run_task_if_template":     self._run_task_if_template,
            "check_truck_quality":      self._check_truck_quality,
            "execute_truck_attack":     self._execute_truck_attack,
            "read_server_time":         self._read_server_time,
            "read_local_time":          self._read_local_time,
            "ensure_server_time_mode":  self._ensure_server_time_mode,
            "ensure_local_time_mode":   self._ensure_local_time_mode,
            "skip_if_server_time_fresh": self._skip_if_server_time_fresh,
            "skip_if_done_today":       self._skip_if_done_today,
            "mark_done_today":          self._mark_done_today,
            "skip_if_fp_cycle_current":  self._skip_if_fp_cycle_current,
            "read_fp_schedule":          self._read_fp_schedule,
            "skip_if_fp_event_current":  self._skip_if_fp_event_current,
            "read_current_fp_event":     self._read_current_fp_event,
            "tap_selected_research":     self._tap_selected_research,
            "dispatch_fp_task":          self._dispatch_fp_task,
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
        self._do_tap(int(x), int(y))
        return self._ok(action, f"Tapped ({x}, {y})")

    def _do_tap(self, x: int, y: int, count: int = 1):
        """Tap count times. Use count=2 for buttons that the game sometimes drops."""
        import time as _t
        self.bot.tap(x, y)
        for _ in range(count - 1):
            _t.sleep(0.15)
            self.bot.tap(x, y)

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

        self._do_tap(match.x, match.y)
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

        self._do_tap(result.x, result.y)
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

        seq_path = get_resource_dir() / "tasks" / "center_hq.json"

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

    def _verify_in_hq(self, action: dict) -> ActionResult:
        """
        Confirms the player is in HQ view using universal view-toggle buttons.

        Logic:
          1. Look for btn_go_to_world_view_universal.png — visible only in HQ view.
             If found, we are already in HQ; proceed without tapping.
          2. If not found, look for btn_go_to_hq_view_universal.png — visible only in world view.
             If found, tap it to return to HQ, then proceed.
          3. If neither found and required=true, fail.
        """
        import time

        HQ_BTN    = "btn_go_to_world_view_universal.png"
        WORLD_BTN = "btn_go_to_hq_view_universal.png"
        required  = action.get("required", True)

        screenshot = self.bot.screenshot()
        hq_path    = self._template_path(HQ_BTN)
        world_path = self._template_path(WORLD_BTN)

        in_hq    = self.vision.find_template(screenshot, hq_path)    is not None
        in_world = self.vision.find_template(screenshot, world_path) is not None

        if self.log_callback:
            self.log_callback(f"verify_in_hq: in_hq={in_hq} in_world={in_world}")

        if in_hq:
            if self.log_callback:
                self.log_callback("verify_in_hq: HQ view confirmed — no action needed")
            return self._ok(action, "verify_in_hq: already in HQ")

        if in_world:
            match = self.vision.find_template(screenshot, world_path)
            if self.log_callback:
                self.log_callback(f"verify_in_hq: world view — tapping {WORLD_BTN} to return to HQ")
            self.bot.tap(match.x, match.y)
            time.sleep(2.0)
            return self._ok(action, "verify_in_hq: world→HQ via tap")

        if required:
            return self._fail(action, "verify_in_hq: neither HQ nor world view button found")
        if self.log_callback:
            self.log_callback("verify_in_hq: neither button found — skipping (not required)")
        return self._ok(action, "verify_in_hq: unknown state, skipped")

    def _zoom_out(self, action: dict) -> ActionResult:
        """
        Zoom out using the emulator's zoom shortcut key.
        Finds the correct emulator window by port→index mapping, then sends the key.
        """
        import time
        import ctypes
        from ctypes import wintypes
        from launcher import get_profile, port_to_index

        profile   = get_profile(self.emulator_type)
        zoom_key  = profile.get("zoom_key")
        if not zoom_key:
            return self._fail(action, f"zoom_out: {self.emulator_type} has no zoom key configured")

        steps  = int(action.get("steps", 3))
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

        hwnd, title = None, ""

        # Find the emulator window by process name — reliable regardless of window title
        process_name = profile["process_name"]
        try:
            import subprocess as _sp
            result = _sp.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            proc_pids = set()
            for line in result.stdout.strip().splitlines():
                parts = line.strip('"').split('","')
                if len(parts) >= 2:
                    try:
                        proc_pids.add(int(parts[1]))
                    except ValueError:
                        pass

            if proc_pids:
                for h, t in all_windows:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                    if pid.value in proc_pids:
                        hwnd, title = h, t
                        if self.log_callback:
                            self.log_callback(f"zoom_out: matched '{t}' hwnd={h} via {process_name} pid={pid.value}")
                        break
        except Exception as e:
            if self.log_callback:
                self.log_callback(f"zoom_out: process match failed ({e}) — falling back to title")

        # Fallback: match by window title if process search failed
        if not hwnd:
            bare_title = profile["window_title_bare"]
            for h, t in all_windows:
                if t == bare_title or t.startswith(bare_title):
                    hwnd, title = h, t
                    break

        if not hwnd:
            if self.log_callback:
                self.log_callback(f"zoom_out: no {self.emulator_type} window found — all titles:")
                for h, t in all_windows[:20]:
                    self.log_callback(f"    hwnd={h} '{t}'")
            return self._fail(action, f"zoom_out: {self.emulator_type} window not found")

        if self.log_callback:
            self.log_callback(f"zoom_out: targeting '{title}' hwnd={hwnd}")

        # VK codes for supported zoom keys
        VK_CODES = {
            "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
            "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
        }
        WM_KEYDOWN = 0x0100
        WM_KEYUP   = 0x0101

        vk = VK_CODES.get(zoom_key.lower())
        if vk:
            # PostMessage sends directly to the window handle — no focus required
            for i in range(steps):
                user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
                time.sleep(0.05)
                user32.PostMessageW(hwnd, WM_KEYUP,   vk, 0xC0000001)
                if self.log_callback:
                    self.log_callback(f"zoom_out: PostMessage {zoom_key.upper()} ({i+1}/{steps}) → hwnd={hwnd}")
                time.sleep(0.5)
            return self._ok(action, f"zoom_out: {zoom_key.upper()} x{steps} → '{title}'")
        else:
            # Fallback for keys not in VK table: focus window and use pyautogui
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.6)
            try:
                import pyautogui
                pyautogui.FAILSAFE = False
                for i in range(steps):
                    pyautogui.press(zoom_key)
                    if self.log_callback:
                        self.log_callback(f"zoom_out: pyautogui {zoom_key.upper()} ({i+1}/{steps})")
                    time.sleep(0.5)
                return self._ok(action, f"zoom_out: {zoom_key.upper()} x{steps} → '{title}'")
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

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleep for up to `seconds`. Returns True immediately if stop is requested."""
        if self._stop_event:
            return self._stop_event.wait(timeout=seconds)
        time.sleep(seconds)
        return False

    def _wait(self, action: dict) -> ActionResult:
        seconds = float(action.get("seconds", 1.0))
        stopped = self._interruptible_sleep(seconds)
        if stopped:
            return self._ok(action, f"Wait interrupted by stop request")
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

        # Optional scan region — pass as region to find_template (avoids normalization distortion)
        _w, _h = screenshot.size
        _x_off = int(_w * float(action.get("scan_x_min_pct", 0))   / 100)
        _y_off = int(_h * float(action.get("scan_y_min_pct", 0))   / 100)
        _x_end = int(_w * float(action.get("scan_x_max_pct", 100)) / 100)
        _y_end = int(_h * float(action.get("scan_y_max_pct", 100)) / 100)
        scan_region = ((_x_off, _y_off, _x_end, _y_end)
                       if (_x_off or _y_off or _x_end != _w or _y_end != _h)
                       else None)

        # Build list of templates to try: primary + optional fallbacks
        templates_to_try = [template]
        fallback = action.get("fallback_template")
        if fallback:
            templates_to_try.append(fallback)
        templates_to_try.extend(action.get("fallback_templates", []))

        # Optional secondary verification: when set, find ALL badge matches and pick the
        # first one where verify_template is visible at the specified offset (e.g. GO button).
        # This skips in-progress badges that show a timer instead of GO.
        verify_template = action.get("verify_template")
        verify_offset_x = int(action.get("verify_offset_x", 0))
        verify_offset_y = int(action.get("verify_offset_y", 0))
        verify_margin_x = int(action.get("verify_margin_x", 60))
        verify_margin_y = int(action.get("verify_margin_y", 25))
        verify_threshold = float(action.get("verify_threshold")) if action.get("verify_threshold") else None

        used_threshold = threshold if threshold is not None else self.vision.confidence_threshold

        match = None
        matched_template = None

        if verify_template:
            verify_path = self._template_path(verify_template)
            for tmpl in templates_to_try:
                path = self._template_path(tmpl)
                candidates = self.vision.find_all_templates(screenshot, path, threshold=threshold, region=scan_region)
                if self.log_callback:
                    self.log_callback(f"  [if_template_tap] '{tmpl}' found {len(candidates)} candidate(s)")
                for candidate in candidates:
                    vx = candidate.x + verify_offset_x
                    vy = candidate.y + verify_offset_y
                    verify_region = (vx - verify_margin_x, vy - verify_margin_y,
                                     vx + verify_margin_x, vy + verify_margin_y)
                    vm = self.vision.find_template(screenshot, verify_path,
                                                   threshold=verify_threshold, region=verify_region)
                    vconf = getattr(vm, "confidence", 0) if vm else 0
                    if self.log_callback:
                        self.log_callback(
                            f"  [verify] '{verify_template}' at ({vx},{vy}) "
                            f"conf={vconf:.3f} found={bool(vm)}"
                        )
                    if vm:
                        match = candidate
                        matched_template = tmpl
                        break
                if match:
                    break
        else:
            for tmpl in templates_to_try:
                path = self._template_path(tmpl)
                match = self.vision.find_template(screenshot, path, threshold=threshold, region=scan_region)
                if self.log_callback:
                    conf = getattr(match, "confidence", 0) if match else 0
                    self.log_callback(f"  [if_template_tap] '{tmpl}' conf={conf:.3f} threshold={used_threshold:.3f} found={bool(match)}")
                if match:
                    matched_template = tmpl
                    break

        if match:
            zone_x = action.get("tap_x_pct") if action.get("tap_x_pct") is not None else action.get("x_pct")
            zone_y = action.get("tap_y_pct") if action.get("tap_y_pct") is not None else action.get("y_pct")
            if zone_x is not None and zone_y is not None:
                w, h = screenshot.size
                tap_x = int(w * float(zone_x) / 100)
                tap_y = int(h * float(zone_y) / 100)
                default_msg = f"Found '{matched_template}' — tapping zone ({tap_x},{tap_y})"
            else:
                tap_x = match.x + int(action.get("tap_offset_x", 0))
                tap_y = match.y + int(action.get("tap_offset_y", 0))
                default_msg = f"Found and tapped '{matched_template}'"
            self._do_tap(tap_x, tap_y)
            msg = action.get("log_success", default_msg)
            if self.log_callback:
                self.log_callback(f"  ✓ {msg}")
            return self._ok(action, msg)

        # Not found — check for fuel-empty fallback before giving up
        fuel_template = action.get("fuel_template")
        if fuel_template:
            fuel_path = self._template_path(fuel_template)
            fuel_match = self.vision.find_template(screenshot, fuel_path)
            if fuel_match:
                if self.log_callback:
                    self.log_callback(f"  ⛽ '{fuel_template}' detected — running restore_fuel")
                import json as _json
                from pathlib import Path
                fuel_task_path = get_resource_dir() / "tasks" / "restore_fuel.json"
                if fuel_task_path.exists():
                    try:
                        fuel_task = _json.loads(fuel_task_path.read_text(encoding="utf-8"))
                        for sub_action in fuel_task.get("actions", []):
                            self.execute(sub_action)
                    except Exception as e:
                        if self.log_callback:
                            self.log_callback(f"  ⚠ restore_fuel failed: {e}")
                # Retry the march tap after refuelling
                screenshot2 = self.bot.screenshot()
                for tmpl in templates_to_try:
                    path = self._template_path(tmpl)
                    retry_match = self.vision.find_template(screenshot2, path, threshold=threshold)
                    if retry_match:
                        self._do_tap(retry_match.x, retry_match.y)
                        msg = action.get("log_success", f"Found and tapped '{tmpl}' after refuelling")
                        if self.log_callback:
                            self.log_callback(f"  ✓ {msg}")
                        return self._ok(action, msg)
                if self.log_callback:
                    self.log_callback(f"  ✗ March still not found after restore_fuel")

        skip_task     = action.get("skip_task_if_not_found", False)
        exhaust_rallies = action.get("exhaust_rallies_if_not_found", False)
        on_not_found  = action.get("on_not_found", [])
        skip_msg      = action.get("log_skip", f"'{template}' not found — skipping task")
        if self.log_callback:
            self.log_callback(f"  ⏭ {skip_msg}")

        if exhaust_rallies:
            msg = "Troops busy — march not available, skipping rally attempt"
            if self.log_callback:
                self.log_callback(f"  ⏭ {msg}")
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)

        if on_not_found:
            for sub in on_not_found:
                if self._stop_event and self._stop_event.is_set():
                    break
                self.execute(sub)
            # on_not_found_continue: true → keep running the parent task after fallback actions
            if action.get("on_not_found_continue"):
                return ActionResult(status=ActionStatus.SKIPPED, action=action, message=skip_msg)
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=skip_msg)

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

    def _if_template_found(self, action: dict) -> ActionResult:
        """
        Detect whether a template is visible WITHOUT tapping it.
        If found: log, execute on_found sub-actions, then abort the current task
                  so remaining steps are skipped.
        If not found: return SKIPPED and continue normally.

        Required: template
        Optional: log_found   — message logged when template is detected
                  log_skip    — message logged when template is absent
                  on_found    — list of action dicts to execute when template is visible
        """
        template = action.get("template")
        if not template:
            return self._fail(action, "if_template_found requires 'template'")

        screenshot = self.bot.screenshot()
        path = self._template_path(template)
        match = self.vision.find_template(screenshot, path)

        skip_task_if_not_found = action.get("skip_task_if_not_found", False)

        if match:
            msg = action.get("log_found", f"'{template}' detected")
            if self.log_callback:
                self.log_callback(f"  ✓ {msg}")
            for sub in action.get("on_found", []):
                if self._stop_event and self._stop_event.is_set():
                    break
                self.execute(sub)
            if skip_task_if_not_found:
                return self._ok(action, msg)
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)

        skip_msg = action.get("log_skip", f"'{template}' not visible — continuing")
        if self.log_callback:
            self.log_callback(f"  ⏭ {skip_msg}")
        if skip_task_if_not_found:
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=skip_msg)
        return ActionResult(status=ActionStatus.SKIPPED, action=action, message=skip_msg)

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

    # ------------------------------------------------------------------
    # Server-time / daily task helpers
    # ------------------------------------------------------------------

    def _detect_time_panel_mode(self) -> str:
        """
        OCR the time panel and return 'server', 'local', or 'unknown'.

        Detects mode by checking for known title strings in the center band.
        NOTE: title strings are English-only for now — will read from a language
        config once the first-run language selection system is implemented.
        """
        ss = self.bot.screenshot()
        w, h = ss.size
        time_region = (int(w * 0.20), int(h * 0.30), int(w * 0.80), int(h * 0.42))
        raw = " ".join(r.text for r in self.vision.read_text(ss, region=time_region, min_confidence=0.3))
        if "Apocalypse" in raw:
            return "server"
        if "Local" in raw:
            return "local"
        return "unknown"

    def _ensure_time_mode(self, action: dict, target: str) -> ActionResult:
        """Shared logic for ensure_server_time_mode and ensure_local_time_mode."""
        import time as _time
        mode = self._detect_time_panel_mode()
        if self.log_callback:
            self.log_callback(f"  [time_mode] detected='{mode}' target='{target}'")
        if mode == target or mode == "unknown":
            return self._ok(action, f"ensure_{target}_time_mode: already in {mode} mode")
        # Wrong mode — tap the toggle
        tmpl = "btn_alternate_time.png"
        result = self.vision.find_template(self.bot.screenshot(), self._template_path(tmpl))
        if result and result.confidence >= 0.8:
            self.bot.tap(result.x, result.y)
            _time.sleep(2.0)
            if self.log_callback:
                self.log_callback(f"  [time_mode] toggled to {target} mode")
        else:
            return self._fail(action, f"ensure_{target}_time_mode: '{tmpl}' not found to toggle")
        return self._ok(action, f"ensure_{target}_time_mode: switched from {mode} to {target}")

    def _ensure_server_time_mode(self, action: dict) -> ActionResult:
        """If panel is in local time mode, tap the toggle to switch to server time."""
        return self._ensure_time_mode(action, "server")

    def _ensure_local_time_mode(self, action: dict) -> ActionResult:
        """If panel is in server time mode, tap the toggle to switch to local time."""
        return self._ensure_time_mode(action, "local")

    def _ocr_time_panel(self, action: dict, label: str, save_file: str) -> ActionResult:
        """
        Shared OCR helper for both read_server_time and read_local_time.
        Reads the Apocalypse Time panel (server or local view) and saves results.

          label     : log prefix, e.g. "server_time" or "local_time"
          save_file : filename under logs/, e.g. "server_time.json" or "local_time.json"
        """
        import re as _re
        import json as _json
        from datetime import datetime as _dt
        from pathlib import Path as _Path

        ss = self.bot.screenshot()
        w, h = ss.size

        cal_region  = (int(w * 0.60), int(h * 0.15), int(w * 0.92), int(h * 0.31))
        time_region = (int(w * 0.20), int(h * 0.30), int(w * 0.80), int(h * 0.42))

        cal_raw  = " ".join(r.text for r in self.vision.read_text(ss, region=cal_region,  min_confidence=0.3))
        time_raw = " ".join(r.text for r in self.vision.read_text(ss, region=time_region, min_confidence=0.3))

        if self.log_callback:
            self.log_callback(f"  [{label}] cal='{cal_raw}'  time='{time_raw}'")

        # Parse year-month from calendar, then find day immediately after
        ym_m = _re.search(r'(\d{4})-(\d{1,2})', cal_raw)
        date_str = None
        if ym_m:
            after_ym = cal_raw[ym_m.end():]
            day_m = _re.search(r'\b(\d{1,2})\b', after_ym)
            if day_m:
                date_str = f"{ym_m.group(0)}-{day_m.group(1)}"

        # Accept HH:MM:SS or HH:MM (clean OCR)
        time_m = _re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)', time_raw)
        if time_m:
            time_str = time_m.group(1)
        else:
            # Fallback: stylized font causes colons to OCR as a stray digit (e.g. "20:22:48" → "20822248"
            # or "20:52:05" → "20352305"). Colon can render as any digit, so constrain by valid time
            # ranges instead: HH=00-23, MM=00-59, SS=00-59, each colon replaced by one stray digit.
            _tp = r'((?:[01]\d|2[0-3]))\d([0-5]\d)\d([0-5]\d)'
            garbled_m = _re.search(_tp, time_raw)
            if not garbled_m:
                garbled_m = _re.search(_tp, " ".join(
                    r.text for r in self.vision.read_text(ss, min_confidence=0.3)))
            time_str = f"{garbled_m.group(1)}:{garbled_m.group(2)}:{garbled_m.group(3)}" if garbled_m else ""

        if not date_str or not time_str:
            try:
                debug_path = _Path(f"logs/{label}_debug.png")
                ss.save(str(debug_path))
                if self.log_callback:
                    self.log_callback(f"  [{label}] debug screenshot → {debug_path}")
            except Exception:
                pass
            full_raw = " ".join(r.text for r in self.vision.read_text(ss, min_confidence=0.3))
            if self.log_callback:
                self.log_callback(f"  [{label}] full-screen OCR: '{full_raw}'")
            return self._fail(action,
                f"{label}: could not parse date/time (cal='{cal_raw}' time='{time_raw}')")

        now = _dt.now()
        _Path("logs").mkdir(exist_ok=True)
        out = {
            "date":        date_str,
            "time":        time_str,
            "recorded_at": now.isoformat(timespec="seconds"),
        }
        _Path(f"logs/{save_file}").write_text(_json.dumps(out, indent=2))

        if self.log_callback:
            self.log_callback(f"  [{label}] {date_str} {time_str} — saved")

        return date_str, time_str, now

    def _read_server_time(self, action: dict) -> ActionResult:
        """OCR the Apocalypse Time screen (server time view) and save to logs/server_time.json."""
        result = self._ocr_time_panel(action, "server_time", "server_time.json")
        if isinstance(result, ActionResult):
            return result
        server_date, server_time, now = result
        self._server_date        = server_date
        self._server_time        = server_time
        self._server_recorded_at = now
        return self._ok(action, f"read_server_time: {server_date} {server_time}")

    def _read_local_time(self, action: dict) -> ActionResult:
        """OCR the Apocalypse Time screen (local time view) and save to logs/local_time.json."""
        result = self._ocr_time_panel(action, "local_time", "local_time.json")
        if isinstance(result, ActionResult):
            return result
        local_date, local_time, now = result
        self._local_date        = local_date
        self._local_time        = local_time
        self._local_recorded_at = now
        return self._ok(action, f"read_local_time: {local_date} {local_time}")

    def _skip_if_server_time_fresh(self, action: dict) -> ActionResult:
        """
        ABORT_TASK if logs/server_time.json was written within the last 24 hours.

        Use this as the first action in check_server_time to avoid re-opening
        the Apocalypse Time panel when the server time is already current.
        """
        import json as _json
        from datetime import datetime as _dt, timedelta as _td
        from pathlib import Path as _Path

        p = _Path("logs/server_time.json")
        if p.exists():
            try:
                data = _json.loads(p.read_text())
                recorded_at = _dt.fromisoformat(data.get("recorded_at", ""))
                age = _dt.now() - recorded_at
                if age < _td(hours=24):
                    msg = (f"server_time fresh ({int(age.total_seconds() // 3600)}h "
                           f"{int((age.total_seconds() % 3600) // 60)}m old) — skipping")
                    if self.log_callback:
                        self.log_callback(f"  ⏭ [server_time] {msg}")
                    return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)
            except Exception:
                pass

        return self._ok(action, "skip_if_server_time_fresh: stale or missing — continuing")

    def _skip_if_done_today(self, action: dict) -> ActionResult:
        """
        Return ABORT_TASK if task_key was already completed on today's server date for this farm.

        Required:
          task_key  - string key matching what mark_done_today used (e.g. "collect_free_rewards")

        Reads logs/daily_completions.json, keyed by port → task_key → server_date.
        """
        import json as _json
        from pathlib import Path as _Path

        task_key = action.get("task_key")
        if not task_key:
            return self._fail(action, "skip_if_done_today requires 'task_key'")

        server_date = getattr(self, "_server_date", None)
        if not server_date:
            try:
                p = _Path("logs/server_time.json")
                if p.exists():
                    server_date = _json.loads(p.read_text()).get("server_date")
                    if server_date:
                        self._server_date = server_date
            except Exception:
                pass

        if not server_date:
            if self.log_callback:
                self.log_callback(f"  [daily] no server date — cannot skip '{task_key}', continuing")
            return self._ok(action, f"skip_if_done_today: no server date — continuing")

        farm_key = str(getattr(self.bot, "port", "default"))
        comp_path = _Path("logs/daily_completions.json")
        completions: dict = {}
        if comp_path.exists():
            try:
                completions = _json.loads(comp_path.read_text())
            except Exception:
                pass

        last_done = completions.get(farm_key, {}).get(task_key)
        if last_done == server_date:
            msg = f"'{task_key}' already done on {server_date} — skipping"
            if self.log_callback:
                self.log_callback(f"  ✅ [daily] {msg}")
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)

        return self._ok(action, f"skip_if_done_today: '{task_key}' not yet done on {server_date}")

    def _mark_done_today(self, action: dict) -> ActionResult:
        """
        Record that task_key was completed on today's server date for this farm.

        Required:
          task_key  - string key (e.g. "collect_free_rewards")

        Writes/updates logs/daily_completions.json.
        """
        import json as _json
        from pathlib import Path as _Path

        task_key = action.get("task_key")
        if not task_key:
            return self._fail(action, "mark_done_today requires 'task_key'")

        server_date = getattr(self, "_server_date", None)
        if not server_date:
            if self.log_callback:
                self.log_callback(f"  [daily] no server date — skipping mark for '{task_key}'")
            return self._ok(action, "mark_done_today: no server date — skipping")

        farm_key = str(getattr(self.bot, "port", "default"))
        comp_path = _Path("logs/daily_completions.json")
        completions: dict = {}
        if comp_path.exists():
            try:
                completions = _json.loads(comp_path.read_text())
            except Exception:
                pass

        comp_path.parent.mkdir(exist_ok=True)
        completions.setdefault(farm_key, {})[task_key] = server_date
        comp_path.write_text(_json.dumps(completions, indent=2))

        if self.log_callback:
            self.log_callback(f"  [daily] marked '{task_key}' done on {server_date} (farm:{farm_key})")

        return self._ok(action, f"mark_done_today: '{task_key}' = {server_date}")

    # ------------------------------------------------------------------
    # Full Preparedness schedule helpers
    # ------------------------------------------------------------------

    def _skip_if_fp_cycle_current(self, action: dict) -> ActionResult:
        """
        ABORT_TASK if fp_schedule.json already holds data for today's ISO day number
        within the current week. Runs daily — skips only once today's slot is captured.
        """
        import json as _json
        from datetime import date as _date, timedelta as _td
        from pathlib import Path as _Path

        server_date = getattr(self, "_server_date", None)
        if not server_date:
            try:
                p = _Path("logs/server_time.json")
                if p.exists():
                    server_date = _json.loads(p.read_text()).get("server_date")
            except Exception:
                pass

        if server_date:
            try:
                parts = server_date.split("-")
                sv_dt = _date(int(parts[0]), int(parts[1]), int(parts[2]))
            except (ValueError, IndexError):
                sv_dt = _date.today()
        else:
            sv_dt = _date.today()

        today_day = str(sv_dt.isoweekday())
        cycle_start = (sv_dt - _td(days=sv_dt.isoweekday() - 1)).isoformat()

        sched_path = _Path("logs/fp_schedule.json")
        if sched_path.exists():
            try:
                data = _json.loads(sched_path.read_text())
                day_slots = data.get("days", {}).get(today_day, {})
                if data.get("cycle_start_date") == cycle_start and len(day_slots) >= 6:
                    msg = f"FP Day {today_day} fully captured ({len(day_slots)} slots) for week of {cycle_start} — skipping"
                    if self.log_callback:
                        self.log_callback(f"  ✅ [fp_cycle] {msg}")
                    return ActionResult(
                        status=ActionStatus.ABORT_TASK, action=action, message=msg)
                elif day_slots:
                    if self.log_callback:
                        self.log_callback(
                            f"  [fp_cycle] Day {today_day} only has {len(day_slots)}/6 slots — running again")
            except Exception:
                pass

        return self._ok(action,
            f"skip_if_fp_cycle_current: Day {today_day} not yet fully captured — continuing")

    # Maps OCR text (lowercase, stripped) → task filename stem
    _FP_THEME_MAP = {
        # Full phrases (preferred — most specific)
        "hero progression":  "hero_progression",
        "army expansion":    "army_expansion",
        "age of science":    "age_of_science",
        "mod vehicle boost": "mod_vehicle",
        "mod vehicle":       "mod_vehicle",
        "shelter upgrade":   "shelter_upgrade",
        # Single-keyword fallbacks for partial/garbled OCR
        "hero":     "hero_progression",
        "army":     "army_expansion",
        "science":  "age_of_science",
        "shelter":  "shelter_upgrade",
        "boost":    "mod_vehicle",
        "mod":      "mod_vehicle",
    }

    def _read_fp_schedule(self, action: dict) -> ActionResult:
        """
        OCR the Full Preparedness calendar screen and save today's day schedule.

        Reads the single day currently visible (the game shows today's day when the
        calendar opens). Merges into logs/fp_schedule.json under that day's ISO key.

        Output file: logs/fp_schedule.json
          {
            "cycle_start_date": "2026-06-01",
            "recorded_at": "...",
            "days": {
              "3": { "00:00": "hero_progression", "04:00": "army_expansion", ... },
              ...
            }
          }
        """
        import re as _re
        import json as _json
        from datetime import datetime as _dt, date as _date, timedelta as _td
        from pathlib import Path as _Path

        server_date = getattr(self, "_server_date", None)
        if not server_date:
            try:
                p = _Path("logs/server_time.json")
                if p.exists():
                    server_date = _json.loads(p.read_text()).get("server_date")
                    if server_date:
                        self._server_date = server_date
            except Exception:
                pass

        current_day_num: str | None = None
        cycle_start_str: str | None = None
        if server_date:
            try:
                parts = server_date.split("-")
                sv_dt = _date(int(parts[0]), int(parts[1]), int(parts[2]))
                current_day_num = str(sv_dt.isoweekday())  # Mon=1, Sun=7
                cycle_start_str = (sv_dt - _td(days=sv_dt.isoweekday() - 1)).isoformat()
            except (ValueError, IndexError):
                pass
        if not current_day_num:
            # No server date — fall back to local date (same day in practice)
            local_dt = _date.today()
            current_day_num = str(local_dt.isoweekday())
            cycle_start_str = (local_dt - _td(days=local_dt.isoweekday() - 1)).isoformat()

        # OCR the current screen
        ss = self.bot.screenshot()
        ocr_results = self.vision.read_text(ss, min_confidence=0.2)

        # Group items into rows by Y proximity
        rows: list[list] = []
        for item in sorted(ocr_results, key=lambda r: r.y):
            placed = False
            for row in rows:
                if abs(item.y - row[0].y) <= 15:
                    row.append(item)
                    placed = True
                    break
            if not placed:
                rows.append([item])

        screen_day: str | None = None
        slots: dict[str, str] = {}

        for row in rows:
            row_text = " ".join(r.text for r in sorted(row, key=lambda r: r.x)).strip()
            if not row_text:
                continue

            # Identify day header
            if screen_day is None:
                day_m = _re.search(r'\bDay\s+(\d+)\b', row_text, _re.IGNORECASE)
                if day_m:
                    day_num = int(day_m.group(1))
                    if 1 <= day_num <= 7:
                        screen_day = str(day_num)
                    continue
                if _re.search(r'\bToday\b', row_text, _re.IGNORECASE) and current_day_num:
                    screen_day = current_day_num
                    continue
                continue

            # Stop if a new day header appears — don't let the next day's slots bleed in
            next_day_m = _re.search(r'\bDay\s+(\d+)\b', row_text, _re.IGNORECASE)
            if next_day_m and str(int(next_day_m.group(1))) != screen_day:
                break

            # Parse time + theme slots under the header
            time_m = _re.search(r'\b(\d{2}:\d{2})\b', row_text)
            if not time_m:
                continue
            slot_time = time_m.group(1)
            theme_raw = row_text.replace(slot_time, "").strip().lower()
            task_name = self._FP_THEME_MAP.get(theme_raw)
            if not task_name:
                for k, v in self._FP_THEME_MAP.items():
                    if k in theme_raw:
                        task_name = v
                        break
            if task_name:
                slots[slot_time] = task_name
                if self.log_callback:
                    self.log_callback(
                        f"  [fp_schedule] Day {screen_day}  {slot_time} → {task_name}")
            elif self.log_callback:
                self.log_callback(
                    f"  [fp_schedule] Day {screen_day}  {slot_time} — unknown theme: '{theme_raw}'")

            # Each day has exactly 6 slots — stop once we have them all
            if len(slots) >= 6:
                break

        # Fall back to server date if OCR missed the day header
        if screen_day is None:
            screen_day = current_day_num

        if not screen_day:
            return self._fail(action,
                "read_fp_schedule: could not determine current day — check OCR or server_time.json")

        # Load and merge into existing schedule
        sched_path = _Path("logs/fp_schedule.json")
        existing_data: dict = {}
        if sched_path.exists():
            try:
                existing_data = _json.loads(sched_path.read_text())
                # Migrate old single-day format
                if "schedule" in existing_data and "days" not in existing_data:
                    old = existing_data
                    existing_data = {
                        "cycle_start_date": old.get("cycle_start_date", ""),
                        "recorded_at": old.get("recorded_at", ""),
                        "days": {},
                    }
            except Exception:
                existing_data = {}

        existing_days: dict = existing_data.get("days", {})
        existing_days[screen_day] = slots

        _Path("logs").mkdir(exist_ok=True)
        out = {
            "cycle_start_date": cycle_start_str or existing_data.get("cycle_start_date", ""),
            "recorded_at":      _dt.now().isoformat(timespec="seconds"),
            "days":             {k: v for k, v in existing_days.items() if v},
        }
        sched_path.write_text(_json.dumps(out, indent=2))

        if self.log_callback:
            self.log_callback(
                f"  [fp_schedule] saved Day {screen_day}, {len(slots)} slot(s) "
                f"(week of {cycle_start_str})")

        return self._ok(action,
            f"read_fp_schedule: Day {screen_day}, {len(slots)} slot(s) saved")

    # Maps the farm's research.research_in dropdown value → on-screen template.
    _RESEARCH_TEMPLATE_MAP = {
        "New Home":              "btn_new_home.png",
        "Shelter Building":      "btn_shelter_building.png",
        "Elite Troops":          "btn_elite_troops.png",
        "Hero Training":         "btn_hero_training.png",
        "Alliance Recognition":  "btn_alliance_recognition.png",
        "Fully Armed Alliance":  "btn_fully_armed_alliance.png",
        "HQ Management":         "btn_hq_managment.png",
        "Seige To Sieze":        "btn_siege_to_seize.png",
        "Rapid Growth":          "btn_rapid_growth.png",
        "Military Strategies":   "btn_military_strategies.png",
        "Unit Special Training": "btn_unit_special_training.png",
        "Age of Steel":          "btn_age_of_steel.png",
        "Peace Shield":          "btn_peace_shield.png",
    }

    def _tap_selected_research(self, action: dict) -> ActionResult:
        """
        Look for the research the farm selected (research.research_in), tap it if found.
        If not found, scroll down and look again (max_scrolls times).  If still not
        found, ABORT_TASK.

        Parameters
        ----------
        max_scrolls   : how many scroll-down retries before giving up (default 1)
        scroll_x_pct  : X centre of scroll swipe as % of width  (default 50)
        scroll_y_pct  : Y centre of scroll swipe as % of height (default 60)
        distance_pct  : swipe distance as % of screen height    (default 40)
        duration_ms   : swipe duration in milliseconds          (default 500)
        wait_seconds  : pause after each scroll before rechecking (default 1.5)
        threshold     : vision confidence override (optional)
        """
        import time as _time

        cfg       = self.farm_settings.get("research", {})
        selection = cfg.get("research_in", "")
        tmpl      = self._RESEARCH_TEMPLATE_MAP.get(selection)
        if not tmpl:
            msg = f"tap_selected_research: no template for selection '{selection}' — stopping task"
            if self.log_callback:
                self.log_callback(f"  ✗ {msg}")
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)

        threshold    = float(action.get("threshold")) if action.get("threshold") is not None else None
        max_scrolls  = int(action.get("max_scrolls", 1))
        scroll_x_pct = float(action.get("scroll_x_pct", 50))
        scroll_y_pct = float(action.get("scroll_y_pct", 60))
        distance_pct = float(action.get("distance_pct", 40))
        duration_ms  = int(action.get("duration_ms", 500))
        wait_secs    = float(action.get("wait_seconds", 1.5))
        path = self._template_path(tmpl)

        def _check():
            ss   = self.bot.screenshot()
            m    = self.vision.find_template(ss, path, threshold=threshold)
            conf = getattr(m, "confidence", 0) if m else 0
            self._log(f"  [research] '{selection}' ({tmpl}) conf={conf:.3f} found={bool(m)}")
            return m

        # ── Attempt 1: check immediately ─────────────────────────────────
        match = _check()
        if match:
            self.bot.tap(match.x, match.y)
            return self._ok(action, f"tap_selected_research: tapped '{selection}'")

        # ── Scroll down and retry ────────────────────────────────────────
        w, h = self._screen_size()
        cx = int(w * scroll_x_pct / 100)
        cy = int(h * scroll_y_pct / 100)
        dy = int(h * distance_pct / 100)
        for i in range(max_scrolls):
            if self._stop_event and self._stop_event.is_set():
                return ActionResult(status=ActionStatus.SKIPPED, action=action,
                                    message="Bot stopped")
            self._log(f"  [research] not found — scrolling down ({i+1}/{max_scrolls})")
            self.bot.swipe(cx, cy, cx, cy - dy, duration_ms=duration_ms)
            _time.sleep(wait_secs)
            match = _check()
            if match:
                self.bot.tap(match.x, match.y)
                return self._ok(action,
                    f"tap_selected_research: tapped '{selection}' after scroll")

        msg = (f"tap_selected_research: '{selection}' ({tmpl}) not found after "
               f"{max_scrolls} scroll(s) — stopping task")
        if self.log_callback:
            self.log_callback(f"  ✗ {msg}")
        return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)

    def _get_estimated_server_time(self) -> tuple | None:
        """
        Returns (hour, minute) estimated current server time using server_time.json
        plus elapsed local wall-clock time.  Returns None if unavailable.
        """
        import json as _json
        from datetime import datetime as _dt
        from pathlib import Path as _Path

        try:
            p = _Path("logs/server_time.json")
            if not p.exists():
                return None
            data = _json.loads(p.read_text())
            sv_time_str  = data.get("time") or data.get("server_time", "")
            recorded_str = data.get("recorded_at", "")
            if not sv_time_str:
                return None
            parts = sv_time_str.split(":")
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
            sv_seconds = h * 3600 + m * 60 + s
            try:
                elapsed = (_dt.now() - _dt.fromisoformat(recorded_str)).total_seconds()
            except Exception:
                elapsed = 0
            current = sv_seconds + elapsed
            return (int(current // 3600) % 24, int((current % 3600) // 60))
        except Exception:
            return None

    def _fp_slot_is_current(self, slot_time: str, srv: tuple) -> bool:
        """Return True if slot_time (HH:MM) is the active 4-hour window for server (hour, min)."""
        try:
            sh, sm = map(int, slot_time.split(":"))
        except ValueError:
            return False
        server_mins = srv[0] * 60 + srv[1]
        slot_mins   = sh * 60 + sm
        next_mins   = slot_mins + 240
        if next_mins >= 1440:
            return server_mins >= slot_mins or server_mins < (next_mins % 1440)
        return slot_mins <= server_mins < next_mins

    def _skip_if_fp_event_current(self, action: dict) -> ActionResult:
        """
        ABORT_TASK if fp_current_event.json holds an event that is still active
        based on estimated server time (each FP slot spans 4 hours).
        Continues if the event has expired, so the caller re-captures the current one.
        """
        import json as _json
        from pathlib import Path as _Path

        p = _Path("logs/fp_current_event.json")
        if not p.exists():
            return self._ok(action, "skip_if_fp_event_current: no event captured yet — continuing")

        try:
            data      = _json.loads(p.read_text())
            slot_time = data.get("slot_time", "")
            task_name = data.get("task", "unknown")
        except Exception:
            return self._ok(action, "skip_if_fp_event_current: unreadable file — continuing")

        srv = self._get_estimated_server_time()
        if srv is None:
            return self._ok(action, "skip_if_fp_event_current: no server time — continuing")

        if self._fp_slot_is_current(slot_time, srv):
            msg = f"FP event '{task_name}' at {slot_time} still active — skipping"
            if self.log_callback:
                self.log_callback(f"  [fp_event] {msg}")
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)

        return self._ok(action,
            f"skip_if_fp_event_current: '{task_name}' at {slot_time} has expired — re-capturing")

    def _read_current_fp_event(self, action: dict) -> ActionResult:
        """
        Detect the green-highlighted row in the FP schedule view and OCR its time + theme.
        Saves logs/fp_current_event.json with slot_time, task, and recorded_at.
        """
        import re as _re
        import json as _json
        from datetime import datetime as _dt
        from pathlib import Path as _Path

        ss  = self.bot.screenshot()
        w, h = ss.size
        img = ss.convert("RGB")
        pix = img.load()

        # Scan rows for green-dominant pixels (G channel beats R and B by margin)
        sample_step = max(1, w // 60)
        sample_cols = list(range(0, w, sample_step))
        green_rows  = []
        for y in range(h):
            green_count = sum(
                1 for x in sample_cols
                if (lambda r, g, b: g > r + 25 and g > b + 25 and g > 90)(*pix[x, y])
            )
            if green_count >= max(1, len(sample_cols) // 6):
                green_rows.append(y)

        if not green_rows:
            return self._fail(action, "read_current_fp_event: no green highlight detected")

        y_min = max(0,  min(green_rows) - 5)
        y_max = min(h,  max(green_rows) + 5)
        if self.log_callback:
            self.log_callback(f"  [fp_event] green band y={y_min}..{y_max}")

        raw_results = self.vision.read_text(ss, region=(0, y_min, w, y_max), min_confidence=0.25)
        raw = " ".join(r.text for r in raw_results)
        if self.log_callback:
            self.log_callback(f"  [fp_event] OCR: '{raw}'")

        time_m = _re.search(r'\b(\d{2}:\d{2})\b', raw)
        if not time_m:
            return self._fail(action, f"read_current_fp_event: no time in '{raw}'")
        slot_time  = time_m.group(1)
        theme_raw  = raw.replace(time_m.group(0), "").strip().lower()

        task_name = self._FP_THEME_MAP.get(theme_raw)
        if not task_name:
            for k, v in self._FP_THEME_MAP.items():
                if k in theme_raw:
                    task_name = v
                    break

        if not task_name:
            return self._fail(action, f"read_current_fp_event: unknown theme '{theme_raw}'")

        _Path("logs").mkdir(exist_ok=True)
        out = {
            "slot_time":   slot_time,
            "task":        task_name,
            "recorded_at": _dt.now().isoformat(timespec="seconds"),
        }
        _Path("logs/fp_current_event.json").write_text(_json.dumps(out, indent=2))
        if self.log_callback:
            self.log_callback(f"  [fp_event] {slot_time} → {task_name} — saved")

        return self._ok(action, f"read_current_fp_event: {slot_time} → {task_name}")

    def _dispatch_fp_task(self, action: dict) -> ActionResult:
        """
        Read the current FP event from fp_current_event.json and run its child task.
        Warns if the event has expired (re-run check_event_calander to refresh).
        """
        import json as _json
        from pathlib import Path as _Path

        p = _Path("logs/fp_current_event.json")
        if not p.exists():
            if self.log_callback:
                self.log_callback("  [fp_dispatch] no fp_current_event.json — run check_event_calander first")
            return ActionResult(status=ActionStatus.SKIPPED, action=action,
                                message="dispatch_fp_task: no current event captured")

        try:
            data      = _json.loads(p.read_text())
            slot_time = data["slot_time"]
            active_task = data["task"]
        except Exception as e:
            return self._fail(action, f"dispatch_fp_task: bad fp_current_event.json — {e}")

        srv = self._get_estimated_server_time()
        if srv is not None and not self._fp_slot_is_current(slot_time, srv):
            if self.log_callback:
                self.log_callback(
                    f"  [fp_dispatch] WARNING: '{active_task}' at {slot_time} has expired — "
                    f"re-run check_event_calander to update")

        if self.log_callback:
            self.log_callback(f"  [fp_dispatch] slot {slot_time} → '{active_task}'")

        task_path = get_resource_dir() / "tasks" / f"{active_task}.json"
        if not task_path.exists():
            return self._fail(action, f"dispatch_fp_task: task file not found: {active_task}.json")

        try:
            task_data    = _json.loads(task_path.read_text(encoding="utf-8"))
            task_actions = (task_data.get("actions", task_data)
                            if isinstance(task_data, dict) else task_data)
        except Exception as e:
            return self._fail(action, f"dispatch_fp_task: failed to load {active_task}.json — {e}")

        for sub_action in task_actions:
            if self._stop_event and self._stop_event.is_set():
                return ActionResult(status=ActionStatus.SKIPPED, action=action,
                                    message="Bot stopped")
            result = self.execute(sub_action)
            if result.status in (ActionStatus.FAILED, ActionStatus.TIMEOUT,
                                  ActionStatus.ABORT_TASK):
                return result

        return self._ok(action, f"dispatch_fp_task: '{active_task}' (slot {slot_time}) complete")

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

    def _loop_while_template(self, action: dict) -> ActionResult:
        """
        Run on_each actions repeatedly as long as a template is visible.
        Checks for the template at the top of every iteration; exits when not found.

        Required:
          template        - template filename to check each iteration

        Optional:
          on_each         - list of actions to run each iteration (default: [])
          max_iterations  - safety cap (default: 10)
          threshold       - vision confidence override for the check

        Example:
          {
            "action": "loop_while_template",
            "template": "btn_collect_truck.png",
            "max_iterations": 4,
            "on_each": [ ... ]
          }
        """
        template  = action.get("template")
        if not template:
            return self._fail(action, "loop_while_template requires 'template'")

        on_each   = action.get("on_each", [])
        max_iter  = int(action.get("max_iterations", 10))
        threshold = float(action.get("threshold")) if action.get("threshold") is not None else None

        for iteration in range(max_iter):
            if self._stop_event and self._stop_event.is_set():
                return self._ok(action, f"loop_while_template: stop requested after {iteration} iteration(s)")

            screenshot = self.bot.screenshot()
            path  = self._template_path(template)
            match = self.vision.find_template(screenshot, path, threshold=threshold)
            conf  = getattr(match, "confidence", 0) if match else 0
            self.log_callback and self.log_callback(
                f"  [loop_while_template] iter {iteration + 1}/{max_iter} — '{template}' conf={conf:.3f} found={bool(match)}")

            if not match:
                return self._ok(action, f"loop_while_template: '{template}' not found — exiting after {iteration} iteration(s)")

            for sub in on_each:
                if self._stop_event and self._stop_event.is_set():
                    return self._ok(action, f"loop_while_template: stop requested mid-iteration {iteration + 1}")
                sub_result = self.execute(sub)
                if sub_result.status == ActionStatus.ABORT_TASK:
                    return sub_result

        return self._ok(action, f"loop_while_template: reached max_iterations ({max_iter})")

    def _check_truck_quality(self, action: dict) -> ActionResult:
        """
        Identifies truck quality on screen and taps go or refresh based on farm settings.

        Farm settings (trucks.*):
          enabled         - skip entirely if False
          allowed_trucks  - "S & A Trucks" | "B, C, & D" | "All"
          use_diamonds    - bool (reserved; affects future diamond-refresh logic)
          refresh_tickets - int, max number of refresh attempts before giving up

        Templates:
          btn_s_truck.png, btn_a_truck.png — S/A quality
          btn_b_truck.png, btn_c_truck.png, btn_d_truck.png — B/C/D quality

        On match:    taps btn_truck_go.png
        On no match: taps btn_truck_refresh.png (up to refresh_tickets times), then skips
        """
        truck_cfg     = self.farm_settings.get("trucks", {})
        enabled       = truck_cfg.get("enabled", True)
        allowed       = truck_cfg.get("allowed_trucks", "All")
        max_refreshes = int(truck_cfg.get("refresh_tickets", 0))

        if not enabled:
            return self._ok(action, "check_truck_quality: trucks disabled — skipping")

        QUALITY_MAP = {
            "S & A Trucks": ["btn_s_trucks.png", "btn_a_truck.png"],
            "B, C, & D":    ["btn_b_truck.png", "btn_c_truck.png", "btn_d_truck.png"],
            "All":          ["btn_s_trucks.png", "btn_a_truck.png",
                             "btn_b_truck.png", "btn_c_truck.png", "btn_d_truck.png"],
        }
        allowed_templates = QUALITY_MAP.get(allowed, QUALITY_MAP["All"])

        for attempt in range(max_refreshes + 1):
            if self._stop_event and self._stop_event.is_set():
                return self._ok(action, "check_truck_quality: stop requested")

            screenshot = self.bot.screenshot()

            matched = None
            for tmpl_name in allowed_templates:
                path  = self._template_path(tmpl_name)
                match = self.vision.find_template(screenshot, path, threshold=0.85)
                conf  = getattr(match, "confidence", 0) if match else 0
                self.log_callback and self.log_callback(
                    f"  [check_truck_quality] '{tmpl_name}' conf={conf:.3f} found={bool(match)}")
                if match:
                    matched = tmpl_name
                    break

            if matched:
                self.log_callback and self.log_callback(
                    f"  [check_truck_quality] Match: '{matched}' (allowed={allowed}) — tapping GO")
                go_path  = self._template_path("btn_truck_go.png")
                time.sleep(0.5)
                go_screenshot = self.bot.screenshot()
                go_match = self.vision.find_template(go_screenshot, go_path)
                if go_match:
                    self.bot.tap(go_match.x, go_match.y)
                    return self._ok(action, f"check_truck_quality: matched '{matched}' — truck sent")
                # GO not visible — check if formation needs to be saved first
                escort_path = self._template_path("btn_deploy_escort_truck.png")
                escort_match = self.vision.find_template(go_screenshot, escort_path)
                if escort_match:
                    self.log_callback and self.log_callback(
                        "  [check_truck_quality] Formation not saved — running save sequence")
                    self.bot.tap(escort_match.x, escort_match.y)
                    time.sleep(4.0)
                    sel_path = self._template_path("btn_select_all_unit_truck.png")
                    sel_ss = self.bot.screenshot()
                    sel_match = self.vision.find_template(sel_ss, sel_path)
                    if sel_match:
                        self.bot.tap(sel_match.x, sel_match.y)
                    time.sleep(4.0)
                    save_path = self._template_path("btn_save_formation_truck.png")
                    save_ss = self.bot.screenshot()
                    save_match = self.vision.find_template(save_ss, save_path)
                    if save_match:
                        self.bot.tap(save_match.x, save_match.y)
                    time.sleep(4.0)
                    # Retry GO button after saving formation
                    go_ss2 = self.bot.screenshot()
                    go_match2 = self.vision.find_template(go_ss2, go_path)
                    if go_match2:
                        self.bot.tap(go_match2.x, go_match2.y)
                        return self._ok(action,
                            f"check_truck_quality: matched '{matched}' — formation saved, truck sent")
                    self.log_callback and self.log_callback(
                        "  [check_truck_quality] btn_truck_go still not visible after save — skipping")
                    return self._ok(action, "check_truck_quality: GO not found after save sequence — skipping")
                self.log_callback and self.log_callback(
                    "  [check_truck_quality] btn_truck_go not visible — skipping send")
                return self._ok(action, "check_truck_quality: GO button not found — skipping")

            # No match on this attempt
            if attempt < max_refreshes:
                self.log_callback and self.log_callback(
                    f"  [check_truck_quality] No match (attempt {attempt + 1}/{max_refreshes + 1}) — refreshing")
                ref_path  = self._template_path("btn_truck_refresh.png")
                ref_match = self.vision.find_template(screenshot, ref_path)
                if ref_match:
                    self.bot.tap(ref_match.x, ref_match.y)
                    time.sleep(1.5)
                else:
                    self.log_callback and self.log_callback(
                        "  [check_truck_quality] btn_truck_refresh not on screen — stopping early")
                    break

        return self._ok(action,
            f"check_truck_quality: no qualifying truck after {max_refreshes} refresh(es) "
            f"(allowed='{allowed}') — skipped")

    def _execute_truck_attack(self, action: dict) -> ActionResult:
        """
        Scans enemy trucks in the Other Trucks view, filters by target state, compares
        power before committing to a fight, and handles the formation-save / next-truck /
        refresh cycle automatically.

        Farm settings (trucks.*):
          attack_truck         - bool, must be True or this action is a no-op
          attack_state         - 1-4 digit string; only attack trucks from this state
                                 (leave empty to attack any state)
          attack_max_attempts  - int, how many full refresh cycles to try (default 3)

        Templates used:
          btn_other_blue_truck, btn_other_white_truck, btn_other_purple_truck
          btn_next_truck, btn_other_truck_refresh
          btn_other_truck_loot, btn_other_truck_plunder
          btn_other_truck_fight, btn_other_truck_fight_cont
          btn_back
        """
        import re as _re

        trucks_cfg   = self.farm_settings.get("trucks", {})
        if not trucks_cfg.get("attack_truck", False):
            return self._ok(action, "execute_truck_attack: disabled — skipping")

        target_state = str(trucks_cfg.get("attack_state", "")).strip()
        max_attempts = int(trucks_cfg.get("attack_max_attempts", 3))

        TRUCK_TEMPLATES = [
            "btn_other_blue_truck.png",
            "btn_other_white_truck.png",
            "btn_other_purple_truck.png",
        ]

        from pathlib import Path as _Path
        _debug_log = _Path("logs") / "truck_attack_debug.log"
        _debug_log.parent.mkdir(exist_ok=True)

        def _parse_power(text: str) -> float:
            # Return the LARGEST numeric value found — power (millions) always
            # dominates state numbers (3-4 digits) that share the same OCR band.
            t = text.replace(",", "").replace(" ", "")
            best = 0.0
            for num_str, sfx in _re.findall(r"([\d.]+)([KkMm]?)", t):
                try:
                    val = float(num_str)
                except ValueError:
                    continue
                sfx = sfx.upper()
                if sfx == "M":
                    val *= 1_000_000
                elif sfx == "K":
                    val *= 1_000
                if val > best:
                    best = val
            return best

        def _dbg(msg: str):
            """Write directly to file AND to GUI — survives bot being closed quickly."""
            self._log(msg)
            try:
                with open(_debug_log, "a", encoding="utf-8") as _f:
                    from datetime import datetime as _dt
                    _f.write(f"[{_dt.now():%H:%M:%S}] {msg}\n")
            except Exception:
                pass

        _ocr_save_counter = [0]  # mutable counter accessible inside nested function

        def _ocr_state(ss) -> tuple:
            """Return (state_number_str, raw_ocr_text) from the truck detail header."""
            w, h = ss.size
            x1, y1 = int(w * 0.02), int(h * 0.57)
            x2, y2 = int(w * 0.92), int(h * 0.75)
            # Popup header sits at the bottom of the screen (~60-67% height).
            # Scan 57-75% to capture the blue #NNN header band.
            results = self.vision.read_text(
                ss,
                region=(x1, y1, x2, y2),
                min_confidence=0.3,
            )
            raw = " ".join(r.text for r in results)
            _dbg(f"  [truck_attack] state OCR raw: '{raw}'")
            # Primary: "#NNN" — also accept common OCR misreads of '#' (H, &)
            # * intentionally excluded: it matches in-game chat scores like "*50"
            m = _re.search(r"[#H&]\s*(\d{1,4})", raw)
            if m:
                _dbg(f"  [truck_attack] state extracted (primary): '{m.group(1)}'")
                return m.group(1), raw
            # Fallback: first standalone 3-4 digit number (state numbers are always 3-4 digits)
            m = _re.search(r"\b(\d{3,4})\b", raw)
            if m:
                _dbg(f"  [truck_attack] state extracted (fallback): '{m.group(1)}'")
                return m.group(1), raw
            _dbg("  [truck_attack] state OCR: no state number found")
            # Save the full screenshot + cropped region so we can see what the bot sees
            _ocr_save_counter[0] += 1
            if _ocr_save_counter[0] <= 3:
                try:
                    _idx = _ocr_save_counter[0]
                    ss.save(str(_debug_log.parent / f"ocr_full_{_idx}.png"))
                    ss.crop((x1, y1, x2, y2)).save(
                        str(_debug_log.parent / f"ocr_region_{_idx}.png"))
                    _dbg(f"  [truck_attack] saved ocr_full_{_idx}.png and ocr_region_{_idx}.png")
                except Exception as _e:
                    _dbg(f"  [truck_attack] screenshot save failed: {_e}")
            return "", raw

        def _extract_name(raw: str) -> str:
            """Extract player name (between Lv.NN and power number) from truck OCR text."""
            m = _re.search(r'Lv\.\d+\s*(.+?)\s+[\d,]{5,}', raw)
            return m.group(1).strip() if m else ""

        def _tap_template(tmpl_name, screenshot=None) -> bool:
            if screenshot is None:
                screenshot = self.bot.screenshot()
            m = self.vision.find_template(screenshot, self._template_path(tmpl_name))
            if m:
                self.bot.tap(m.x, m.y)
                return True
            return False

        def _open_first_truck() -> bool:
            """Tap any visible truck icon to open its detail panel. Returns True on success."""
            ss = self.bot.screenshot()
            for tmpl in TRUCK_TEMPLATES:
                m = self.vision.find_template(ss, self._template_path(tmpl))
                if m:
                    _dbg(f"  [truck_attack] tapping '{tmpl}'")
                    self.bot.tap(m.x, m.y)
                    time.sleep(2.0)
                    return True
            return False

        if not _open_first_truck():
            return ActionResult(
                status=ActionStatus.ABORT_TASK,
                action=action,
                message="execute_truck_attack: no truck icons visible",
            )

        if not hasattr(self, "_truck_attack_skip_names"):
            self._truck_attack_skip_names = set()
        if not hasattr(self, "_truck_attack_count"):
            self._truck_attack_count = 0

        for attempt in range(max_attempts):
            if self._stop_event and self._stop_event.is_set():
                return self._ok(action, "execute_truck_attack: stop requested")

            _dbg(f"  [truck_attack] cycle {attempt + 1}/{max_attempts}")

            for slot in range(12):
                if self._stop_event and self._stop_event.is_set():
                    return self._ok(action, "execute_truck_attack: stop requested")

                ss             = self.bot.screenshot()
                state, _raw    = _ocr_state(ss)
                player_name    = _extract_name(_raw)
                _dbg(f"  [truck_attack] slot {slot + 1}/12 — state='{state}' target='{target_state}'")

                # Apply state filter
                if target_state and state != target_state:
                    _dbg(f"  [truck_attack] state mismatch '{state}' — advancing")
                    if not _tap_template("btn_next_truck.png"):
                        break
                    time.sleep(1.5)
                    continue

                # Skip players we already know we can't beat
                if player_name and player_name in self._truck_attack_skip_names:
                    _dbg(f"  [truck_attack] skipping '{player_name}' (outmatched previously)")
                    if not _tap_template("btn_next_truck.png"):
                        break
                    time.sleep(1.5)
                    continue

                # State matches — save screenshot then attempt loot
                _dbg(f"  [truck_attack] state '{state}' matched — saving screenshot")
                try:
                    self.bot.screenshot().save(str(_debug_log.parent / "match_found.png"))
                except Exception:
                    pass

                # Loot — opens the attack detail screen where Plunder button appears
                _dbg("  [truck_attack] tapping Loot")
                if not _tap_template("btn_other_truck_loot.png"):
                    _dbg("  [truck_attack] Loot button not found — advancing")
                    if not _tap_template("btn_next_truck.png"):
                        break
                    time.sleep(1.5)
                    continue
                time.sleep(3.0)

                # Plunder — try primary + alternates, pick best confidence
                # Extra second to let the world-map screen fully render after Loot
                time.sleep(1.0)
                _PLUNDER_TMPLS = [
                    "btn_other_truck_plunder_world.png",  # fresh crop from world-map view
                    "btn_other_trucks_plunder_alt.png",
                    "btn_other_trucks_plunder_alt_2.png",
                    "btn_other_truck_plunder.png",
                    "btn_other_trucks_plunder_alt_1.png",
                ]
                plunder_ss = self.bot.screenshot()
                _pw, _ph = plunder_ss.size
                # Search 45-100% height so the Plunder badge (~y=520-660 on 960px screen)
                # is fully captured. x capped at 80% to skip right-side nav buttons.
                _plunder_region = (int(_pw * 0.05), int(_ph * 0.45), int(_pw * 0.80), _ph)
                try:
                    plunder_ss.save(str(_debug_log.parent / "plunder_screen.png"))
                except Exception:
                    pass

                # Build annotated copy for visual debugging
                try:
                    from PIL import ImageDraw as _IDraw
                    _ann = plunder_ss.copy()
                    _ann_draw = _IDraw.Draw(_ann)
                    # Draw the search region boundary
                    _ann_draw.rectangle(
                        [_plunder_region[0], _plunder_region[1],
                         _plunder_region[2], _plunder_region[3]],
                        outline="blue", width=2,
                    )
                except Exception:
                    _ann = _ann_draw = None

                plunder_match = None
                for _pt in _PLUNDER_TMPLS:
                    _m = self.vision.find_template(
                        plunder_ss, self._template_path(_pt),
                        threshold=0.3, region=_plunder_region,
                    )
                    _c = getattr(_m, "confidence", 0)
                    _mx, _my = getattr(_m, "x", 0), getattr(_m, "y", 0)
                    _dbg(f"  [truck_attack] Plunder '{_pt}' conf={_c:.3f} found={bool(_m)} xy=({_mx},{_my})")
                    if _m and _ann_draw:
                        _col = "green" if _c >= 0.5 else ("yellow" if _c >= 0.4 else "red")
                        _ann_draw.ellipse([_mx-18, _my-18, _mx+18, _my+18], outline=_col, width=3)
                        _ann_draw.text((_mx+20, _my-10), f"{_pt[:22]} {_c:.2f}", fill=_col)
                    if _m and (plunder_match is None or _c > getattr(plunder_match, "confidence", 0)):
                        plunder_match = _m

                try:
                    if _ann:
                        _ann.save(str(_debug_log.parent / "plunder_annotated.png"))
                except Exception:
                    pass

                if not plunder_match:
                    _dbg("  [truck_attack] Plunder button not found in region — backing off")
                    _tap_template("btn_back.png", self.bot.screenshot())
                    time.sleep(2.0)
                    if not _tap_template("btn_next_truck.png"):
                        break
                    time.sleep(1.5)
                    continue
                _dbg(f"  [truck_attack] tapping Plunder at ({plunder_match.x},{plunder_match.y}) conf={getattr(plunder_match,'confidence',0):.3f}")
                self.bot.tap(plunder_match.x, plunder_match.y)
                # Wait for the pre-fight formation screen to fully render
                time.sleep(4.0)

                # Power check BEFORE tapping Fight — fight starts immediately on Fight tap
                # with no confirmation screen, so this is the only safe moment to back off.
                pre_fight_ss = self.bot.screenshot()
                try:
                    pre_fight_ss.save(str(_debug_log.parent / "pre_fight_screen.png"))
                except Exception:
                    pass
                w, h = pre_fight_ss.size
                # Read abbreviated M-values from the top header bar (e.g. "3.77M" / "12.55M").
                # Same band that shows cleanly in fight_screen.png — should also appear here.
                left_raw  = " ".join(r.text for r in self.vision.read_text(
                    pre_fight_ss, region=(int(w*0.10), int(h*0.01), int(w*0.45), int(h*0.08)), min_confidence=0.3))
                right_raw = " ".join(r.text for r in self.vision.read_text(
                    pre_fight_ss, region=(int(w*0.55), int(h*0.01), int(w*0.90), int(h*0.08)), min_confidence=0.3))
                our_pw    = _parse_power(left_raw)
                their_pw  = _parse_power(right_raw)
                _dbg(f"  [truck_attack] PRE-fight power check — ours={our_pw:,.0f} theirs={their_pw:,.0f}")
                _dbg(f"  [truck_attack] PRE-fight OCR left='{left_raw}'  right='{right_raw}'")

                # Back off if OCR failed to read either side — never fight blind
                if our_pw == 0 or their_pw == 0:
                    _dbg("  [truck_attack] power OCR failed (one or both sides = 0) — backing off, restarting scan")
                    if player_name:
                        self._truck_attack_skip_names.add(player_name)
                        _dbg(f"  [truck_attack] added '{player_name}' to skip list (OCR fail — {len(self._truck_attack_skip_names)} skipped)")
                    _tap_template("btn_back.png", pre_fight_ss)
                    time.sleep(2.0)
                    _tap_template("btn_back.png", self.bot.screenshot())
                    time.sleep(2.0)
                    break  # restart scan from slot 1

                if their_pw > our_pw:
                    _dbg("  [truck_attack] outmatched — backing off, restarting scan")
                    if player_name:
                        self._truck_attack_skip_names.add(player_name)
                        _dbg(f"  [truck_attack] added '{player_name}' to skip list ({len(self._truck_attack_skip_names)} skipped total)")
                    _tap_template("btn_back.png", pre_fight_ss)
                    time.sleep(2.0)
                    _tap_template("btn_back.png", self.bot.screenshot())
                    time.sleep(2.0)
                    break  # restart scan from slot 1

                # Power ok — now tap Fight (commits immediately, no confirmation)
                _dbg("  [truck_attack] power ok — tapping Fight")
                if not _tap_template("btn_other_truck_fight.png", pre_fight_ss):
                    _dbg("  [truck_attack] Fight button not visible — backing off")
                    _tap_template("btn_back.png", self.bot.screenshot())
                    time.sleep(2.0)
                    if not _tap_template("btn_next_truck.png"):
                        break
                    time.sleep(1.5)
                    continue

                time.sleep(2.0)
                fight_ss = self.bot.screenshot()
                try:
                    fight_ss.save(str(_debug_log.parent / "fight_screen.png"))
                except Exception:
                    pass

                _dbg("  [truck_attack] fight started — tapping Fight Continue")
                _tap_template("btn_other_truck_fight_cont.png", fight_ss)
                time.sleep(20.0)
                self._truck_attack_count += 1
                _dbg(f"  [truck_attack] fight complete ({self._truck_attack_count}/4) — navigating back")
                _tap_template("btn_back.png", self.bot.screenshot())
                time.sleep(1.5)
                _tap_template("btn_back.png", self.bot.screenshot())
                time.sleep(1.5)
                if self._truck_attack_count >= 4:
                    _dbg("  [truck_attack] 4 attacks complete — stopping loop")
                    self._truck_attack_count = 0
                    self._truck_attack_skip_names = set()
                    self._mark_done_today({"task_key": "truck_attack"})
                    return ActionResult(
                        status=ActionStatus.ABORT_TASK,
                        action=action,
                        message=f"execute_truck_attack: 4 attacks complete (state={state})",
                    )
                break  # restart task from action 1 to attack again

            # Inner loop ended (slots exhausted or break after fight/back-off) — refresh for next cycle
            if attempt < max_attempts - 1:
                _dbg("  [truck_attack] refreshing truck list for next cycle")
                if not _tap_template("btn_other_truck_refresh.png"):
                    _dbg("  [truck_attack] refresh button not found — stopping")
                    break
                time.sleep(3.0)
                if not _open_first_truck():
                    _dbg("  [truck_attack] no trucks after refresh — stopping")
                    break

        return self._ok(action,
            f"execute_truck_attack: no target found after {max_attempts} cycle(s)")

    def _loop_task(self, action: dict) -> ActionResult:
        """
        Repeatedly run a sub-task JSON until it returns ABORT_TASK (e.g. badge not found).
        Use this to keep processing bounties/items until none remain.

        Required:
          task            - filename inside the tasks directory (e.g. "enable_purple_bounty.json")

        Optional:
          max_iterations  - safety cap (default: 20)

        The sub-task should use skip_task_if_not_found on its first detection step so it
        returns ABORT_TASK when there's nothing left to process, ending the loop cleanly.
        """
        import json as _json
        task_name = action.get("task")
        if not task_name:
            return self._fail(action, "loop_task requires 'task'")
        max_iter = int(action.get("max_iterations", 20))

        task_path = get_resource_dir() / "tasks" / task_name
        if not task_path.exists():
            return self._fail(action, f"loop_task: task file not found: {task_name}")

        try:
            task_data = _json.loads(task_path.read_text(encoding="utf-8"))
            task_actions = task_data.get("actions", task_data) if isinstance(task_data, dict) else task_data
        except Exception as e:
            return self._fail(action, f"loop_task: failed to load {task_name}: {e}")

        completed = 0
        for i in range(max_iter):
            if self._stop_event and self._stop_event.is_set():
                break
            if self.log_callback:
                self.log_callback(f"  [loop_task] '{task_name}' — attempt {i + 1}/{max_iter}")

            aborted = False
            for sub_action in task_actions:
                if self._stop_event and self._stop_event.is_set():
                    return self._ok(action, f"loop_task: stopped at attempt {i + 1}")
                result = self.execute(sub_action)
                if result.status == ActionStatus.ABORT_TASK:
                    aborted = True
                    break

            if aborted:
                if self.log_callback:
                    self.log_callback(
                        f"  [loop_task] '{task_name}' — nothing found, loop done ({completed} completed)")
                break
            completed += 1

        if completed >= max_iter and self.log_callback:
            self.log_callback(f"  [loop_task] reached max {max_iter} iterations for '{task_name}'")

        return self._ok(action, f"loop_task: '{task_name}' done — {completed} completed")

    def _run_task(self, action: dict) -> ActionResult:
        """
        Unconditionally load and execute all actions from a sub-task JSON file.

        Example:
          {"action": "run_task", "task": "fp_reward_collection.json"}
        """
        import json as _json
        task_name = action.get("task")
        if not task_name:
            return self._fail(action, "run_task requires 'task'")

        task_path = get_resource_dir() / "tasks" / task_name
        if not task_path.exists():
            return self._fail(action, f"run_task: task file not found: {task_name}")

        try:
            task_data    = _json.loads(task_path.read_text(encoding="utf-8"))
            task_actions = task_data.get("actions", task_data) if isinstance(task_data, dict) else task_data
        except Exception as e:
            return self._fail(action, f"run_task: failed to load {task_name}: {e}")

        if self.log_callback:
            self.log_callback(f"  [run_task] running '{task_name}' ({len(task_actions)} action(s))")

        for sub_action in task_actions:
            if self._stop_event and self._stop_event.is_set():
                break
            result = self.execute(sub_action)
            if result.status == ActionStatus.ABORT_TASK:
                return result

        return self._ok(action, f"run_task: '{task_name}' complete")

    def _run_task_if_template(self, action: dict) -> ActionResult:
        """
        Run a sub-task only if an identifying template is currently visible on screen.
        If the template is not found, continues silently to the next action.

        Required:
          template  - template filename to look for (e.g. "btn_fp_hero_progression.png")
          task      - task JSON filename inside tasks/ (e.g. "hero_progression.json")

        Optional:
          threshold - confidence override for the template check (default: engine threshold)

        Example:
          {"action": "run_task_if_template", "template": "btn_fp_hero_progression.png",
           "task": "hero_progression.json"}
        """
        import json as _json

        template  = action.get("template")
        task_name = action.get("task")
        if not template or not task_name:
            return self._fail(action, "run_task_if_template requires 'template' and 'task'")

        path       = self._template_path(template)
        screenshot = self.bot.screenshot()
        threshold  = action.get("threshold")
        match      = self.vision.find_template(screenshot, path,
                         **({"threshold": threshold} if threshold is not None else {}))

        if not match:
            if self.log_callback:
                self.log_callback(
                    f"  [run_task_if_template] '{template}' not found — skipping '{task_name}'")
            return ActionResult(status=ActionStatus.SKIPPED, action=action,
                                message=f"template not found, skipping {task_name}")

        task_path = get_resource_dir() / "tasks" / task_name
        if not task_path.exists():
            return self._fail(action, f"run_task_if_template: task file not found: {task_name}")

        try:
            task_data    = _json.loads(task_path.read_text(encoding="utf-8"))
            task_actions = task_data.get("actions", task_data) if isinstance(task_data, dict) else task_data
        except Exception as e:
            return self._fail(action, f"run_task_if_template: failed to load {task_name}: {e}")

        if self.log_callback:
            self.log_callback(
                f"  [run_task_if_template] '{template}' matched — running '{task_name}'")

        for sub_action in task_actions:
            if self._stop_event and self._stop_event.is_set():
                return ActionResult(status=ActionStatus.SKIPPED, action=action, message="Bot stopped")
            result = self.execute(sub_action)
            if result.status in (ActionStatus.FAILED, ActionStatus.TIMEOUT, ActionStatus.ABORT_TASK):
                return result

        return self._ok(action, f"run_task_if_template: '{task_name}' complete")

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

        timer_re      = _re.compile(r'[A-Za-z0-9]{2,}[hHmMsS](?:\b|$)|\d+[hHmMsS]\d+|\d{3,}|\d+:\d+')
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
            self._do_tap(tmatch.x, tmatch.y)
            return self._ok(action, f"Tapped template '{template}'")

        if x_pct is None or y_pct is None:
            return self._fail(action, f"tap_template_or_zone: '{template}' not found and no fallback zone specified")

        w, h = self._screen_size()
        fx = int(w * float(x_pct) / 100)
        fy = int(h * float(y_pct) / 100)
        self._log(f"  [tap_template_or_zone] '{template}' not found — fallback tap ({x_pct}%, {y_pct}%) = ({fx},{fy})")
        self._do_tap(fx, fy)
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

    def _tap_template_search(self, action: dict) -> ActionResult:
        """
        Search for a template across the full HQ view using a 5-scroll sweep:
        down → left → up → right → down. Taps it if found at any point.
        If not found after all scrolls, logs a warning and continues (never aborts task).

        Parameters
        ----------
        template       : template filename to search for and tap (required)
        scroll_x_pct   : X centre of scroll swipe as % of screen width  (default 50)
        scroll_y_pct   : Y centre of scroll swipe as % of screen height (default 50)
        distance_pct   : swipe distance as % of screen dimension        (default 40)
        duration_ms    : swipe duration in milliseconds                  (default 500)
        wait_seconds   : pause after each scroll before rechecking       (default 1.5)
        threshold      : vision confidence override (optional)
        """
        import time as _time

        template = action.get("template")
        if not template:
            return self._fail(action, "tap_template_search requires 'template'")

        threshold      = float(action.get("threshold")) if action.get("threshold") is not None else None
        scroll_x_pct   = float(action.get("scroll_x_pct", 50))
        scroll_y_pct   = float(action.get("scroll_y_pct", 50))
        distance_x_pct = float(action.get("distance_x_pct", action.get("distance_pct", 40)))
        distance_y_pct = float(action.get("distance_y_pct", action.get("distance_pct", 40)))
        duration_ms    = int(action.get("duration_ms", 500))
        wait_secs      = float(action.get("wait_seconds", 1.5))
        ignore_top_pct = float(action.get("ignore_top_pct", 0))

        path = self._template_path(template)

        def _check():
            ss   = self.bot.screenshot()
            w, h = ss.size
            region = (0, int(h * ignore_top_pct / 100), w, h) if ignore_top_pct > 0 else None
            m = self.vision.find_template(ss, path, threshold=threshold, region=region)
            self._log(f"  [tap_template_search] '{template}' found={bool(m)}")
            return m

        def _scroll(direction):
            w, h = self._screen_size()
            cx = int(w * scroll_x_pct / 100)
            cy = int(h * scroll_y_pct / 100)
            dx = int(w * distance_x_pct / 100)
            dy = int(h * distance_y_pct / 100)
            if direction == "down":
                self.bot.swipe(cx, cy, cx, cy - dy, duration_ms=duration_ms)
            elif direction == "up":
                self.bot.swipe(cx, cy, cx, cy + dy, duration_ms=duration_ms)
            elif direction == "left":
                self.bot.swipe(cx, cy, cx + dx, cy, duration_ms=duration_ms)
            elif direction == "right":
                self.bot.swipe(cx, cy, cx - dx, cy, duration_ms=duration_ms)
            _time.sleep(wait_secs)

        # Initial check — no scroll yet
        match = _check()
        if match:
            self.bot.tap(match.x, match.y)
            return self._ok(action, f"tap_template_search: '{template}' found (no scroll)")

        for direction in ("left", "left", "up", "right", "right", "right", "right", "down", "left", "left", "left"):
            self._log(f"  [tap_template_search] not found — scrolling {direction}")
            _scroll(direction)
            match = _check()
            if match:
                self.bot.tap(match.x, match.y)
                return self._ok(action, f"tap_template_search: '{template}' found after scroll {direction}")

        self._log(f"  [tap_template_search] '{template}' not found after full search — skipping")
        return self._ok(action, f"tap_template_search: '{template}' not found, skipped")

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
        skip_to_step = action.get("skip_to_on_found")
        skip_to_idx  = (int(skip_to_step) - 1) if skip_to_step is not None else None

        if tmatch:
            self._log(f"  [tap_template_or_template] tapping at ({tmatch.x}, {tmatch.y})")
            self._do_tap(tmatch.x, tmatch.y)
            result = self._ok(action, f"Tapped primary template '{template}' at ({tmatch.x}, {tmatch.y})")
            result.skip_to = skip_to_idx
            return result

        # ── Try fallback ─────────────────────────────────────────────────
        fb_path  = self._template_path(fallback_template)
        fb_match = self.vision.find_template(screenshot, fb_path, threshold=threshold)
        fb_conf  = getattr(fb_match, "confidence", 0) if fb_match else 0
        self._log(f"  [tap_template_or_template] fallback '{fallback_template}' conf={fb_conf:.3f} found={bool(fb_match)}")
        if fb_match:
            self._log(f"  [tap_template_or_template] tapping fallback at ({fb_match.x}, {fb_match.y})")
            self._do_tap(fb_match.x, fb_match.y)
            result = self._ok(action,
                f"Primary '{template}' not found — tapped fallback '{fallback_template}' at ({fb_match.x}, {fb_match.y})")
            result.skip_to = skip_to_idx
            return result

        # ── Neither found ────────────────────────────────────────────────
        skip_msg = action.get("log_skip", f"Neither '{template}' nor '{fallback_template}' found — skipping")
        if self.log_callback:
            self.log_callback(f"  ⏭ {skip_msg}")
        if action.get("skip_task_if_not_found", False):
            return ActionResult(
                status=ActionStatus.ABORT_TASK,
                action=action,
                message=skip_msg,
            )
        if not action.get("required", True):
            return ActionResult(
                status=ActionStatus.SKIPPED,
                action=action,
                message=skip_msg,
            )
        return self._fail(action, f"Neither '{template}' nor '{fallback_template}' found")

    def _tap_first_found(self, action: dict) -> ActionResult:
        """
        Try each template in 'templates' list in order; tap the first one found.
        If none are found, retries up to not_found_retries times before giving up.

        Parameters
        ----------
        templates          : list of template filenames to try in order (required)
        threshold          : vision confidence override (optional)
        required           : if False, returns SKIPPED when none found (default False)
        not_found_retries  : number of additional attempts if nothing is found (default 0)
        retry_delay        : seconds to wait between retry attempts (default 1.0)
        log_skip           : message to log when none found after all retries
        """
        templates = action.get("templates", [])
        if not templates:
            return self._fail(action, "tap_first_found requires a non-empty 'templates' list")

        threshold         = float(action.get("threshold")) if action.get("threshold") is not None else None
        not_found_retries = int(action.get("not_found_retries", 0))
        retry_delay       = float(action.get("retry_delay", 1.0))

        for attempt in range(not_found_retries + 1):
            screenshot = self.bot.screenshot()
            for tpl in templates:
                path  = self._template_path(tpl)
                match = self.vision.find_template(screenshot, path, threshold=threshold)
                conf  = getattr(match, "confidence", 0) if match else 0
                self._log(f"  [tap_first_found] '{tpl}' conf={conf:.3f} found={bool(match)}")
                if match:
                    self._log(f"  [tap_first_found] tapping at ({match.x}, {match.y})")
                    self.bot.tap(match.x, match.y)
                    return self._ok(action, f"Tapped '{tpl}' at ({match.x}, {match.y})")

            if attempt < not_found_retries:
                self._log(f"  [tap_first_found] nothing found, retrying ({attempt + 1}/{not_found_retries})...")
                time.sleep(retry_delay)

        skip_msg = action.get("log_skip", f"None of {templates} found — skipping")
        if self.log_callback:
            self.log_callback(f"  ⏭ {skip_msg}")
        if not action.get("required", False):
            return ActionResult(status=ActionStatus.SKIPPED, action=action, message=skip_msg)
        return self._fail(action, skip_msg)


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
        timer_re = _re.compile(r'[A-Za-z0-9]{2,}[hHmMsS](?:\b|$)|\d+[hHmMsS]\d+|\d{3,}|\d+:\d+')

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
        max_taps        = int(action.get("max_taps", 20))
        delay           = float(action.get("delay", 0.5))
        not_found_retries = int(action.get("not_found_retries", 2))
        threshold       = float(action.get("threshold")) if action.get("threshold") is not None else None
        taps            = 0
        misses          = 0

        on_each = action.get("on_each", [])

        while taps < max_taps:
            if self._stop_event and self._stop_event.is_set():
                self._log(f"  repeat_if_template: stop requested — exiting loop after {taps} tap(s)")
                break
            screenshot = self.bot.screenshot()
            match = self.vision.find_template(screenshot, path, threshold=threshold)
            if not match:
                misses += 1
                if misses > not_found_retries:
                    self._log(f"  repeat_if_template: '{template}' not found after {not_found_retries} retries — done ({taps} tap(s))")
                    break
                self._log(f"  repeat_if_template: '{template}' not found, retrying ({misses}/{not_found_retries})...")
                time.sleep(delay)
                continue
            misses = 0
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

    def _tap_active_alliance_mine(self, action: dict) -> ActionResult:
        """
        Scans the Alliance Mines panel via OCR for an active mine, identified
        by map coordinates in parentheses next to the mine name — e.g. Alliance
        Food ( 308,499 ). Taps that row to navigate to the building on the map.

        If neither Wood nor Food shows coordinates the mine has not been placed;
        the task is skipped (required=False default) or aborted (required=True).

        Parameters
        ----------
        scan_y_min_pct : top of OCR scan area as % of screen height (default 8)
        scan_y_max_pct : bottom of OCR scan area as % of screen height (default 50)
        required       : abort task when no active mine found (default False)
        """
        import re as _re

        screenshot = self.bot.screenshot()
        w, h = screenshot.size

        y_min = int(h * float(action.get("scan_y_min_pct", 8))  / 100)
        y_max = int(h * float(action.get("scan_y_max_pct", 85)) / 100)
        region = (0, y_min, w, y_max)

        self._log(f"  [tap_active_alliance_mine] scanning region y={y_min}-{y_max} (screen {w}x{h})")
        ocr_results = self.vision.read_text(screenshot, region=region, min_confidence=0.3)

        self._log(f"  [tap_active_alliance_mine] OCR found {len(ocr_results)} result(s):")
        for r in sorted(ocr_results, key=lambda x: x.y):
            self._log(f"    y={r.y:4d}  conf={r.confidence:.2f}  '{r.text}'")

        # Strict: parentheses around a coordinate pair, any separator
        # Loose: bare coordinate pair in case OCR drops the parens
        # Separator can be comma, period, apostrophe, space, or combination (e.g. ",." from OCR noise)
        strict = _re.compile(r'\(\s*\d+[,.\'\s]+\d+\s*\)')
        loose  = _re.compile(r'\b\d{2,4}[,.\'\s]+\d{2,4}\b')

        for pattern in (strict, loose):
            for r in sorted(ocr_results, key=lambda x: x.y):
                if pattern.search(r.text):
                    self._log(f"  [tap_active_alliance_mine] matched '{r.text}' at ({r.x},{r.y})")
                    self.bot.tap(r.x, r.y)
                    return self._ok(action, f"tap_active_alliance_mine: tapped '{r.text}' at ({r.x},{r.y})")

        msg = "tap_active_alliance_mine: no active mine found — building not placed, skipping task"
        self._log(f"  ⏭ {msg}")
        if action.get("required", False):
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)
        return ActionResult(status=ActionStatus.SKIPPED, action=action, message=msg)

    def _tap_template_or_ocr_pattern(self, action: dict) -> ActionResult:
        """
        Try a template first. If not found, scan the screen with OCR and tap the
        first text that matches ocr_pattern (a Python regex). Useful when a
        preferred target may be unavailable but any matching text will do —
        e.g. finding a donatable alliance tech item showing "6/10" or "1/10".

        Parameters
        ----------
        template    : template filename to try first (required)
        ocr_pattern : Python regex string to match against OCR text (required)
        required    : if False, returns SKIPPED when neither found (default False)
        log_skip    : message logged when nothing is found
        threshold   : vision confidence override (optional)
        """
        import re as _re

        template    = action.get("template")
        ocr_pattern = action.get("ocr_pattern")
        if not template or not ocr_pattern:
            return self._fail(action, "tap_template_or_ocr_pattern requires 'template' and 'ocr_pattern'")

        threshold  = float(action.get("threshold")) if action.get("threshold") is not None else None
        screenshot = self.bot.screenshot()

        # ── Try template first ───────────────────────────────────────────
        path  = self._template_path(template)
        match = self.vision.find_template(screenshot, path, threshold=threshold)
        conf  = getattr(match, "confidence", 0) if match else 0
        self._log(f"  [tap_template_or_ocr_pattern] '{template}' conf={conf:.3f} found={bool(match)}")
        if match:
            self.bot.tap(match.x, match.y)
            return self._ok(action, f"Tapped preferred template '{template}' at ({match.x}, {match.y})")

        # ── Fallback: OCR scan for pattern ───────────────────────────────
        self._log(f"  [tap_template_or_ocr_pattern] template not found — scanning OCR for pattern '{ocr_pattern}'")
        ocr_results = self.vision.read_text(screenshot, min_confidence=0.3)
        pattern = _re.compile(ocr_pattern)
        for r in ocr_results:
            if pattern.search(r.text):
                self._log(f"  [tap_template_or_ocr_pattern] OCR match: '{r.text}' at ({r.x}, {r.y})")
                self.bot.tap(r.x, r.y)
                return self._ok(action, f"Tapped OCR match '{r.text}' at ({r.x}, {r.y})")

        # ── Nothing found ────────────────────────────────────────────────
        skip_msg = action.get("log_skip", f"Neither '{template}' nor any OCR match for '{ocr_pattern}' found")
        if self.log_callback:
            self.log_callback(f"  ⏭ {skip_msg}")
        if not action.get("required", False):
            return ActionResult(status=ActionStatus.SKIPPED, action=action, message=skip_msg)
        return self._fail(action, skip_msg)

    def _tap_ocr_pattern(self, action: dict) -> ActionResult:
        """
        Scan the screen with OCR and tap the first text matching ocr_pattern (regex).
        Useful for tapping a progress counter like "0/5" when no template exists —
        e.g. the first available research showing a {number}/{number} sequence.

        Parameters
        ----------
        ocr_pattern    : Python regex matched against OCR text (required)
        region         : optional (x1, y1, x2, y2) pixel region to limit OCR
        min_confidence : OCR confidence floor (default 0.3)
        required       : if True, ABORT_TASK when nothing matches; else SKIPPED (default False)
        log_skip       : message logged when nothing matches
        """
        import re as _re

        ocr_pattern = action.get("ocr_pattern")
        if not ocr_pattern:
            return self._fail(action, "tap_ocr_pattern requires 'ocr_pattern'")

        region   = action.get("region", None)
        min_conf = float(action.get("min_confidence", 0.3))
        screenshot = self.bot.screenshot()
        ocr_results = self.vision.read_text(screenshot, region=region, min_confidence=min_conf)
        pattern = _re.compile(ocr_pattern)

        for r in ocr_results:
            if pattern.search(r.text):
                self._log(f"  [tap_ocr_pattern] match '{r.text}' at ({r.x}, {r.y})")
                self.bot.tap(r.x, r.y)
                return self._ok(action, f"tap_ocr_pattern: tapped '{r.text}' at ({r.x}, {r.y})")

        skip_msg = action.get("log_skip", f"tap_ocr_pattern: no OCR match for '{ocr_pattern}'")
        if self.log_callback:
            self.log_callback(f"  ⏭ {skip_msg}")
        if action.get("required", False):
            return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=skip_msg)
        return ActionResult(status=ActionStatus.SKIPPED, action=action, message=skip_msg)

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
        recent_levels: list[int] = []  # rolling window for oscillation detection
        consecutive_misses = 0
        abort_after_misses = int(action.get("abort_after_misses", 3))

        for attempt in range(1, max_attempts + 1):
            if self._stop_event and self._stop_event.is_set():
                return self._ok(action, "adjust_boomer_level: stop requested")
            screenshot = self.bot.screenshot()
            current = detect_current_level(screenshot)

            if current is None:
                consecutive_misses += 1
                self._log(f"  [adjust_boomer_level] attempt {attempt}: no level template matched ({consecutive_misses}/{abort_after_misses})")
                if consecutive_misses >= abort_after_misses:
                    msg = f"adjust_boomer_level: no level template matched {abort_after_misses} times in a row — wrong screen, aborting task"
                    self._log(f"  ⏭ {msg}")
                    return ActionResult(status=ActionStatus.ABORT_TASK, action=action, message=msg)
                if self._interruptible_sleep(tap_delay):
                    return self._ok(action, "adjust_boomer_level: stop requested")
                continue
            consecutive_misses = 0

            self._log(f"  [adjust_boomer_level] attempt {attempt}: current={current} target={target}")

            if current == target:
                return self._ok(action, f"Boomer level set to {target}")

            # ── Oscillation detection ─────────────────────────────────
            # If we keep bouncing between two values that straddle the target,
            # the template for the target level is being misread. Accept it.
            recent_levels.append(current)
            if len(recent_levels) > 4:
                recent_levels.pop(0)
            if len(recent_levels) >= 4:
                unique = set(recent_levels)
                if len(unique) == 2:
                    lo, hi = min(unique), max(unique)
                    if lo < target < hi:
                        self._log(
                            f"  [adjust_boomer_level] oscillation detected between {lo} and {hi} "
                            f"— target {target} is between them, likely a template misread. Accepting."
                        )
                        return self._ok(action, f"Boomer level set to {target} (oscillation resolved)")

            if current < target:
                self._log(f"  [adjust_boomer_level] tapping + ({plus_tpl})")
                tap_tpl(plus_tpl)
            else:
                self._log(f"  [adjust_boomer_level] tapping - ({minus_tpl})")
                tap_tpl(minus_tpl)

            if self._interruptible_sleep(tap_delay):
                return self._ok(action, "adjust_boomer_level: stop requested")

        return self._fail(action, f"adjust_boomer_level: could not reach level {target} after {max_attempts} attempts")

    def _adjust_boomer_level_ocr(self, action: dict) -> ActionResult:
        """
        Use OCR to read the current boomer level shown on screen, calculate the delta
        to the target level, then tap + or - exactly that many times.

        Action fields:
            setting       - dot-path to target level, e.g. "rally.boomer_level"
            ocr_region    - dict with x_pct, y_pct, w_pct, h_pct (percentage of screen)
            plus_template - template to tap when level too low,  e.g. "btn_plus_boomer.png"
            minus_template- template to tap when level too high, e.g. "btn_subtract_boomer_lvl.png"

        Optional:
            max_taps      - safety cap on total taps (default 100)
            tap_delay     - seconds to wait between taps (default 0.4)
            threshold     - vision confidence override for +/- button detection
        """
        setting_path = action.get("setting", "rally.boomer_level")
        ocr_region   = action.get("ocr_region", {})
        plus_tpl     = action.get("plus_template",  "btn_plus_boomer.png")
        minus_tpl    = action.get("minus_template", "btn_subtract_boomer_lvl.png")
        max_taps     = int(action.get("max_taps",   100))
        tap_delay    = float(action.get("tap_delay", 0.4))
        threshold    = float(action.get("threshold")) if action.get("threshold") is not None else None

        # ── Resolve target level from farm settings ────────────────────
        value = self.farm_settings
        for key in setting_path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            self._log(f"  [adjust_boomer_level_ocr] setting '{setting_path}' not found — skipping")
            return self._ok(action, f"Skipped: '{setting_path}' not configured")
        target = int(value)
        self._log(f"  [adjust_boomer_level_ocr] target level = {target}")

        # ── OCR current level ──────────────────────────────────────────
        screenshot = self.bot.screenshot()
        if screenshot is None:
            return self._fail(action, "adjust_boomer_level_ocr: failed to take screenshot")

        sw, sh = screenshot.size
        x_pct = ocr_region.get("x_pct", 35.0)
        y_pct = ocr_region.get("y_pct", 38.0)
        w_pct = ocr_region.get("w_pct", 30.0)
        h_pct = ocr_region.get("h_pct", 8.0)
        x1 = int(sw * x_pct / 100)
        y1 = int(sh * y_pct / 100)
        x2 = int(sw * (x_pct + w_pct) / 100)
        y2 = int(sh * (y_pct + h_pct) / 100)

        current = self.vision.read_number(screenshot, region=(x1, y1, x2, y2))
        if current is None:
            return self._fail(action, f"adjust_boomer_level_ocr: OCR could not read a number in region ({x1},{y1},{x2},{y2})")
        self._log(f"  [adjust_boomer_level_ocr] OCR current level={current}, target={target}")

        if current == target:
            return self._ok(action, f"Boomer level already at {target}")

        delta = target - current
        tpl   = plus_tpl if delta > 0 else minus_tpl
        taps  = min(abs(delta), max_taps)
        self._log(f"  [adjust_boomer_level_ocr] delta={delta:+d} — tapping '{tpl}' x{taps}")

        # Locate the button once, then tap its coordinates repeatedly
        btn_path  = self._template_path(tpl)
        btn_shot  = self.bot.screenshot()
        btn_match = self.vision.find_template(btn_shot, btn_path, threshold=threshold)
        if not btn_match:
            return self._fail(action, f"adjust_boomer_level_ocr: '{tpl}' not found on screen")

        for _ in range(taps):
            if self._stop_event and self._stop_event.is_set():
                return self._ok(action, "adjust_boomer_level_ocr: stop requested")
            self.bot.tap(btn_match.x, btn_match.y)
            if self._interruptible_sleep(tap_delay):
                return self._ok(action, "adjust_boomer_level_ocr: stop requested")

        self._log(f"  [adjust_boomer_level_ocr] done — tapped {taps} times, level should now be {target}")
        return self._ok(action, f"Boomer level adjusted from {current} to {target}")

    def _adjust_boomer_level_from_one(self, action: dict) -> ActionResult:
        """
        Reset boomer level to 1 by tapping minus until btn_lvl_1_boomer.png is confirmed,
        then tap plus (target - 1) times to reach the target level.

        Action fields:
            setting          - dot-path to target level, e.g. "rally.boomer_level"
            level_1_template - template that confirms level 1 is selected (default: btn_lvl_1_boomer.png)
            plus_template    - tap to increase level (default: btn_plus_boomer.png)
            minus_template   - tap to decrease level (default: btn_subtract_boomer_lvl.png)

        Optional:
            max_minus_taps - safety cap on minus taps while resetting to 1 (default 50)
            tap_delay      - seconds between taps (default 0.4)
            threshold      - vision confidence override
        """
        setting_path   = action.get("setting", "rally.boomer_level")
        lvl1_tpl       = action.get("level_1_template", "btn_lvl_1_boomer.png")
        plus_tpl       = action.get("plus_template",    "btn_plus_boomer.png")
        minus_tpl      = action.get("minus_template",   "btn_subtract_boomer_lvl.png")
        max_minus_taps = int(action.get("max_minus_taps", 50))
        tap_delay      = float(action.get("tap_delay",  0.4))
        threshold      = float(action.get("threshold")) if action.get("threshold") is not None else None

        # ── Resolve target level ───────────────────────────────────────
        value = self.farm_settings
        for key in setting_path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            self._log(f"  [adjust_boomer_level_from_one] setting '{setting_path}' not found — skipping")
            return self._ok(action, f"Skipped: '{setting_path}' not configured")
        target = int(value)
        self._log(f"  [adjust_boomer_level_from_one] target level = {target}")

        minus_path = self._template_path(minus_tpl)
        plus_path  = self._template_path(plus_tpl)
        true_lvl1_path = self._template_path(action.get("true_level_1_template", "btn_true_lvl_1.png"))

        # ── Step 1: confirm level 1 using the combined template ───────────────
        # btn_true_lvl_1.png shows both the "1" level display and the grayed-out
        # minus button together — a single unambiguous indicator of being at floor.
        self._log(f"  [adjust_boomer_level_from_one] step 1 — checking for level 1")
        missed_button = 0
        actual_taps   = 0
        at_level_one  = False

        for i in range(max_minus_taps + 1):
            if self._stop_event and self._stop_event.is_set():
                return self._ok(action, "adjust_boomer_level_from_one: stop requested")

            shot       = self.bot.screenshot()
            lvl1_match = self.vision.find_template(shot, true_lvl1_path, threshold=threshold)
            self._log(f"  [adjust_boomer_level_from_one] check — true_lvl1={'none' if not lvl1_match else f'{lvl1_match.confidence:.3f}'}")

            if lvl1_match:
                self._log(f"  [adjust_boomer_level_from_one] ✓ level 1 confirmed after {actual_taps} minus tap(s) (conf={lvl1_match.confidence:.3f})")
                at_level_one = True
                break

            if i == max_minus_taps:
                break  # exhausted — fail below

            # Not confirmed yet — tap minus
            match = self.vision.find_template(shot, minus_path, threshold=threshold)
            if match:
                self.bot.tap(match.x, match.y)
                actual_taps  += 1
                missed_button = 0
            else:
                missed_button += 1
                self._log(f"  [adjust_boomer_level_from_one] WARNING: '{minus_tpl}' not found (attempt {missed_button})")
                if missed_button >= 3:
                    return self._fail(action, f"adjust_boomer_level_from_one: '{minus_tpl}' not visible — search panel may not be open")

            if self._interruptible_sleep(tap_delay):
                return self._ok(action, "adjust_boomer_level_from_one: stop requested")

        if not at_level_one:
            return self._fail(action, f"adjust_boomer_level_from_one: level 1 not confirmed after {actual_taps} minus taps")

        if target == 1:
            return self._ok(action, "Boomer level set to 1")

        # ── Step 2: tap plus (target - 2) times ───────────────────────
        # Template matches at level 2 (one step before true floor), so subtract 2 instead of 1
        plus_taps = target - 2
        self._log(f"  [adjust_boomer_level_from_one] step 2 — tapping + {plus_taps} time(s) to reach level {target}")
        shot       = self.bot.screenshot()
        plus_match = self.vision.find_template(shot, plus_path, threshold=threshold)
        if not plus_match:
            return self._fail(action, f"adjust_boomer_level_from_one: '{plus_tpl}' not found on screen")

        for _ in range(plus_taps):
            if self._stop_event and self._stop_event.is_set():
                return self._ok(action, "adjust_boomer_level_from_one: stop requested")
            self.bot.tap(plus_match.x, plus_match.y)
            if self._interruptible_sleep(tap_delay):
                return self._ok(action, "adjust_boomer_level_from_one: stop requested")

        return self._ok(action, f"Boomer level set to {target}")

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
            if self._stop_event and self._stop_event.is_set():
                return self._ok(action, "adjust_resource_level: stop requested")
            screenshot = self.bot.screenshot()
            current = detect_current_level(screenshot)

            if current is None:
                self._log(f"  [adjust_resource_level] attempt {attempt}: no level template matched — retrying")
                if self._interruptible_sleep(tap_delay):
                    return self._ok(action, "adjust_resource_level: stop requested")
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

            if self._interruptible_sleep(tap_delay):
                return self._ok(action, "adjust_resource_level: stop requested")

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
                if self._stop_event and self._stop_event.is_set():
                    return False
                current = detect_level(self.bot.screenshot())
                if current is None:
                    self._log(f"  [search_resource_level] set_level attempt {attempt}: no match — retrying")
                    if self._interruptible_sleep(tap_delay):
                        return False
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
                if self._interruptible_sleep(tap_delay):
                    return False
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
            if self._interruptible_sleep(2.0):
                return

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
                if self._interruptible_sleep(1.0):
                    return
                shot = self.bot.screenshot()
                res_match = self.vision.find_template(shot, self._template_path(resource_tpl))
                if res_match:
                    self._log(f"  [search_resource_level] re-selected '{resource_tpl}' after scroll")
                    self.bot.tap(res_match.x, res_match.y)
                else:
                    self._log(f"  [search_resource_level] WARNING: could not re-select '{resource_tpl}'")
            self._interruptible_sleep(2.0)

        for lvl in range(start_level, max_level + 1):
            if self._stop_event and self._stop_event.is_set():
                return self._ok(action, "search_resource_level: stop requested")
            self._log(f"  [search_resource_level] trying level {lvl}")

            if not set_level(lvl):
                if self._stop_event and self._stop_event.is_set():
                    return self._ok(action, "search_resource_level: stop requested")
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
            if self._interruptible_sleep(2.0):
                return self._ok(action, "search_resource_level: stop requested")

            # Tap result zone
            shot = self.bot.screenshot()
            w, h = shot.size
            self._log(f"  [search_resource_level] tapping resource zone ({res_x}%, {res_y}%)")
            self.bot.tap(int(w * res_x / 100), int(h * res_y / 100))
            if self._interruptible_sleep(2.0):
                return self._ok(action, "search_resource_level: stop requested")

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
        Check the daily rally counter for this farm — does NOT increment.

        Reads logs/rally_counts.json keyed by "<port>_<YYYY-MM-DD>".
        If the count has reached rally.max_rallies_per_day → ABORT_TASK.
        Otherwise returns SUCCESS so the task continues.

        The counter is only incremented by rally_count_record, which should
        be placed after a march is actually confirmed (btn_march_on_boomer tapped).

        farm_settings must contain {"rally": {"max_rallies_per_day": N}}.
        """
        import json as _json
        from pathlib import Path
        from datetime import date

        rally_cfg    = self.farm_settings.get("rally", {})
        farm_max     = int(rally_cfg.get("max_rallies_per_day", 999))
        # Inline override takes priority — used by event tasks that need a higher limit
        max_rallies  = int(action["max_rallies"]) if "max_rallies" in action else farm_max

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

        msg = f"Rally count {current}/{max_rallies} — proceeding"
        self._log(f"  ✓ {msg}")
        return self._ok(action, msg)

    def _rally_count_record(self, action: dict) -> ActionResult:
        """
        Increment the daily rally counter for this farm.

        Call this only after a march has been successfully sent (i.e. after
        btn_march_on_boomer is confirmed tapped). Reads/writes
        logs/rally_counts.json keyed by "<port>_<YYYY-MM-DD>".
        """
        import json as _json
        from pathlib import Path
        from datetime import date

        rally_cfg   = self.farm_settings.get("rally", {})
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

        counts[key] = counts.get(key, 0) + 1
        Path("logs").mkdir(exist_ok=True)
        with open(counts_path, "w") as f:
            _json.dump(counts, f, indent=2)

        msg = f"Rally recorded: {counts[key]}/{max_rallies} sent today"
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
