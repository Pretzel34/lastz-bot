"""
bot_engine.py
=============
Runs sequences of actions, manages state, tracks stats, and writes logs.
This is the brain that orchestrates everything.

Works with action_executor.py, adb_wrapper.py, and vision.py.
"""

import json
import time
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable
from dataclasses import dataclass, field
from enum import Enum

from adb_wrapper import ADBWrapper, InstanceManager
from vision import VisionEngine
from action_executor import ActionExecutor, ActionStatus
import sys
from paths import get_resource_dir, ensure_app_dir


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "emulator": {
        "host": "127.0.0.1",
        "ports": [21503],
        "adb_path": "adb",
        "type": "MEmu",
        "expected_width": 540,
        "expected_height": 960
    },
    "bot": {
        "template_dir": "templates",
        "log_dir": "logs",
        "tasks_dir": "tasks",
        "screenshot_on_error": True,
        "retry_failed_actions": True,
        "max_retries": 2,
        "retry_delay_seconds": 2.0,
        "loop_tasks": False,
        "loop_delay_seconds": 60.0
    },
    "vision": {
        "confidence_threshold": 0.8
    }
}


def load_config(path: str = "config.json") -> dict:
    """Load config from file, filling in defaults for missing keys."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy defaults
    if Path(path).exists():
        with open(path) as f:
            user_config = json.load(f)
        _deep_merge(config, user_config)
    else:
        save_config(config, path)
        print(f"[Config] Created default config at {path}")
    return config


def save_config(config: dict, path: str = "config.json"):
    """Save config to file."""
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def _deep_merge(base: dict, override: dict):
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class SessionStats:
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    actions_run: int = 0
    actions_succeeded: int = 0
    actions_failed: int = 0
    actions_skipped: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    loops_completed: int = 0
    errors: list = field(default_factory=list)

    @property
    def runtime_seconds(self) -> float:
        end = self.end_time or datetime.now()
        return (end - self.start_time).total_seconds()

    @property
    def success_rate(self) -> float:
        total = self.actions_succeeded + self.actions_failed
        return (self.actions_succeeded / total * 100) if total > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "runtime_seconds": round(self.runtime_seconds, 1),
            "actions_run": self.actions_run,
            "actions_succeeded": self.actions_succeeded,
            "actions_failed": self.actions_failed,
            "actions_skipped": self.actions_skipped,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "loops_completed": self.loops_completed,
            "success_rate_pct": round(self.success_rate, 1),
            "errors": self.errors[-20:],  # last 20 errors
        }

    def summary(self) -> str:
        return (
            f"Runtime: {self.runtime_seconds:.0f}s | "
            f"Actions: {self.actions_succeeded}/{self.actions_run} OK | "
            f"Tasks: {self.tasks_completed} done, {self.tasks_failed} failed | "
            f"Success: {self.success_rate:.0f}%"
        )


# ---------------------------------------------------------------------------
# Engine State
# ---------------------------------------------------------------------------

class EngineState(Enum):
    IDLE     = "idle"
    RUNNING  = "running"
    PAUSED   = "paused"
    STOPPING = "stopping"
    STOPPED  = "stopped"
    ERROR    = "error"


# ---------------------------------------------------------------------------
# Bot Engine
# ---------------------------------------------------------------------------

class BotEngine:
    """
    Orchestrates the full bot loop:
      - Loads config and connects to emulator
      - Runs task sequences (lists of action dicts)
      - Handles retries, errors, and logging
      - Tracks session statistics
      - Supports start/pause/stop from GUI

    Usage:
        engine = BotEngine()
        engine.connect()
        engine.load_task_file("tasks/lastZ_daily.json")
        engine.start()   # runs in background thread
        # ...
        engine.stop()
        print(engine.stats.summary())
    """

    def __init__(self, config_path: str = "config.json"):
        self.config = load_config(config_path)
        self.state = EngineState.IDLE
        self.stats = SessionStats()

        # Core components
        self.bot: Optional[ADBWrapper] = None
        self.vision: Optional[VisionEngine] = None
        self.executor: Optional[ActionExecutor] = None

        # Task management
        self.tasks: list[dict] = []          # list of task dicts
        self.current_task_index: int = 0
        self.current_action_index: int = 0
        self.farm_settings: dict = {}        # farm task settings (e.g. rally.boomer_level)

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused by default

        # Callbacks for GUI
        self.on_log: Optional[Callable[[str], None]] = None
        self.on_state_change: Optional[Callable[[EngineState], None]] = None
        self.on_stats_update: Optional[Callable[[SessionStats], None]] = None
        self.on_action_complete: Optional[Callable[[dict, bool], None]] = None
        # Called when ADB device disconnect is detected mid-run; should close and
        # reopen the emulator instance, update engine.bot, and return True on success.
        self.on_device_not_found: Optional[Callable[[], bool]] = None

        # Recording
        self.recorder = None   # ScreenRecorder instance, set via enable_recording()

        # Logging
        self._setup_logging()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Connect to the emulator using config settings."""
        ports = self.config["emulator"]["ports"]
        host = self.config["emulator"]["host"]
        port = ports[0]  # single instance for now

        self._log(f"Connecting to {host}:{port}...")
        self.bot = ADBWrapper(port=port, adb_host=host)

        if not self.bot.connect():
            self._log(f"Failed to connect to {host}:{port}")
            self._set_state(EngineState.ERROR)
            return False

        threshold = self.config["vision"]["confidence_threshold"]
        self.vision = VisionEngine(confidence_threshold=threshold)

        template_dir = str(get_resource_dir() / self.config["bot"]["template_dir"])
        self._log(f"[Init] template_dir: {template_dir} (exists: {Path(template_dir).exists()})")
        self.executor = ActionExecutor(
            bot=self.bot,
            vision=self.vision,
            template_dir=template_dir,
            log_callback=self._log,
            farm_settings=self.farm_settings,
            stop_event=self._stop_event,
            emulator_type=self.config["emulator"].get("type", "MEmu"),
        )

        if self.bot.info.screen_width > self.bot.info.screen_height:
            self._log(f"Screen is landscape ({self.bot.info.screen_width}x{self.bot.info.screen_height}) — forcing portrait...")
            self.bot.enforce_portrait()

        exp_w = self.config["emulator"].get("expected_width", 540)
        exp_h = self.config["emulator"].get("expected_height", 960)
        if self.bot.info.screen_width != exp_w or self.bot.info.screen_height != exp_h:
            self._log(f"Resolution mismatch ({self.bot.info.screen_width}x{self.bot.info.screen_height}) — enforcing {exp_w}x{exp_h}...")
            self.bot.enforce_resolution(exp_w, exp_h)

        self._log(f"Connected: {self.bot.info.model} | "
                  f"Android {self.bot.info.android_version} | "
                  f"{self.bot.info.screen_width}x{self.bot.info.screen_height}")
        return True

    def disconnect(self):
        if self.bot:
            self.bot.disconnect()
            self.bot = None

    # ------------------------------------------------------------------
    # Task Loading
    # ------------------------------------------------------------------

    def load_task_file(self, path: str) -> bool:
        """Load a task sequence from a JSON file."""
        p = Path(path)
        if not p.exists():
            self._log(f"Task file not found: {path}")
            return False
        with open(p) as f:
            data = json.load(f)
        # Support both a bare list and {"name": ..., "actions": [...]}
        if isinstance(data, list):
            self.tasks = [{"name": p.stem, "actions": data}]
        elif isinstance(data, dict) and "tasks" in data:
            self.tasks = data["tasks"]
        else:
            self.tasks = [data]
        self._log(f"Loaded {len(self.tasks)} task(s) from {p.name}")
        return True

    def load_tasks(self, tasks: list[dict]):
        """Load tasks directly from a list (used by GUI)."""
        self.tasks = tasks
        self._log(f"Loaded {len(tasks)} task(s)")

    def set_farm_settings(self, settings: dict):
        """
        Store farm task settings so actions like verify_setting_template can read them.
        Call this before start() with the farm's tasks dict.

        Example:
            engine.set_farm_settings(farm["tasks"])
            # Now "rally.boomer_level" resolves to farm["tasks"]["rally"]["boomer_level"]
        """
        self.farm_settings = settings or {}
        if self.executor:
            self.executor.set_farm_settings(self.farm_settings)

    def enable_recording(self, name: str):
        """Start recording annotated screenshots for each action. kind='run'."""
        try:
            from recording_utils import ScreenRecorder
            self.recorder = ScreenRecorder(name=name, kind="run")
            self._log(f"Recording enabled → recordings/run/{name}/")
        except Exception as e:
            self._log(f"Recording unavailable: {e}")
            self.recorder = None

    def disable_recording(self):
        """Stop recording and close the recorder."""
        if self.recorder:
            self.recorder.close()
            self.recorder = None

    def save_task_file(self, tasks: list[dict], path: str):
        """Save a task sequence to a JSON file."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(tasks, f, indent=2)
        self._log(f"Task saved → {path}")

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self):
        """Start the bot in a background thread."""
        if self.state == EngineState.RUNNING:
            self._log("Already running")
            return
        if not self.executor:
            self._log("Not connected — call connect() first")
            return
        if not self.tasks:
            self._log("No tasks loaded — load a task file first")
            return

        self._stop_event.clear()
        self._pause_event.set()
        self.stats = SessionStats()
        self.current_task_index = 0
        self.current_action_index = 0

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._set_state(EngineState.RUNNING)
        self._log("Bot started")

    def stop(self):
        """Signal the bot to stop immediately."""
        self._log("Stop requested...")
        self._stop_event.set()
        self._pause_event.set()  # unpause so thread can see stop
        self._set_state(EngineState.STOPPING)
        # Disconnect ADB to break any in-flight blocking socket call immediately.
        # The _run_loop exception handler will treat the resulting error as a
        # clean stop (not ERROR) when _stop_event is set.
        if self.bot:
            try:
                self.bot.disconnect()
            except Exception:
                pass

    def pause(self):
        """Pause execution after the current action."""
        if self.state == EngineState.RUNNING:
            self._pause_event.clear()
            self._set_state(EngineState.PAUSED)
            self._log("Bot paused")

    def resume(self):
        """Resume from paused state."""
        if self.state == EngineState.PAUSED:
            self._pause_event.set()
            self._set_state(EngineState.RUNNING)
            self._log("Bot resumed")

    # ------------------------------------------------------------------
    # Main Run Loop
    # ------------------------------------------------------------------

    def _run_loop(self):
        loop_count = 0
        loop_tasks = self.config["bot"]["loop_tasks"]
        loop_delay = self.config["bot"]["loop_delay_seconds"]

        try:
            while not self._stop_event.is_set():
                loop_count += 1
                self._log(f"--- Loop {loop_count} ---")

                all_ok = self._run_all_tasks()

                if all_ok:
                    self.stats.loops_completed += 1

                if self._stop_event.is_set():
                    break

                if not loop_tasks:
                    self._log("All tasks complete. Bot finished.")
                    break

                self._log(f"Loop complete. Waiting {loop_delay}s before next loop...")
                self._interruptible_sleep(loop_delay)

        except Exception as e:
            if self._stop_event.is_set():
                # Exception was caused by ADB disconnect on stop — treat as clean stop
                pass
            else:
                self._log(f"FATAL ERROR in bot loop: {e}")
                self._set_state(EngineState.ERROR)
                self.stats.errors.append(str(e))
                return

        self.stats.end_time = datetime.now()
        self._log(f"Bot stopped. {self.stats.summary()}")
        self._save_session_log()
        self._set_state(EngineState.STOPPED)
        self._close_logging()

    def _run_all_tasks(self) -> bool:
        all_ok = True
        for i, task in enumerate(self.tasks):
            if self._stop_event.is_set():
                break
            self.current_task_index = i
            ok = self._run_task(task)
            if not ok:
                all_ok = False
        return all_ok

    def _run_task(self, task: dict) -> bool:
        name = task.get("name", "unnamed")
        actions = task.get("actions", [])

        self._log(f"▶▶ Task: {name} ({len(actions)} actions)")

        for i, action in enumerate(actions):
            if self._stop_event.is_set():
                return False

            # Pause support
            self._pause_event.wait()

            self.current_action_index = i
            result = self._run_action_with_retry(action)
            self.stats.actions_run += 1

            if result.status == ActionStatus.SUCCESS:
                self.stats.actions_succeeded += 1
            elif result.status == ActionStatus.SKIPPED:
                self.stats.actions_skipped += 1
            elif result.status == ActionStatus.ABORT_TASK:
                # Graceful skip — not a failure, just nothing to do
                self.stats.actions_skipped += 1
                self.stats.tasks_completed += 1
                self._log(f"  ⏭ Task '{name}' skipped: {result.message}")
                return True
            elif result.status in (ActionStatus.FAILED, ActionStatus.TIMEOUT):
                self.stats.actions_failed += 1
                self.stats.errors.append(
                    f"[{name}] action {i}: {result.message}"
                )
                if action.get("required", True):
                    self._log(f"Required action failed — aborting task '{name}'")
                    self.stats.tasks_failed += 1
                    if self.on_action_complete:
                        self.on_action_complete(action, False)
                    return False

            if self.on_action_complete:
                self.on_action_complete(action, bool(result))
            if self.on_stats_update:
                self.on_stats_update(self.stats)

        self.stats.tasks_completed += 1
        self._log(f"✓ Task '{name}' complete")
        return True

    def _is_device_disconnect(self, message: str) -> bool:
        msg = message.lower()
        return "not found" in msg and ("device" in msg or "127.0.0.1" in msg)

    def _recover_view(self):
        """
        Error recovery — determine current view state and navigate back to HQ.

        btn_go_headquarters.png  → visible when in WORLD view  → tap to go to HQ
        btn_go_world.png         → visible when in HQ view     → already at HQ,
                                   cycle world→HQ to recenter camera
        """
        self._log("  🔄 Recovery: checking view state...")
        try:
            self.executor.execute({
                "action":     "ensure_hq_view",
                "hq_btn":     "btn_go_world.png",
                "world_btn":  "btn_go_headquarters.png",
                "x_pct":      91.1,
                "y_pct":      95.6,
                "required":   False,
            })
            import time as _t; _t.sleep(1.5)
        except Exception as e:
            self._log(f"  ⚠ Recovery failed: {e}")

    def _run_action_with_retry(self, action: dict):
        """Run an action. On failure, recover view state then retry."""
        max_retries = self.config["bot"]["max_retries"]
        retry_delay = self.config["bot"]["retry_delay_seconds"]
        should_retry = self.config["bot"]["retry_failed_actions"]

        # Recording: capture screen before action
        step = self.current_action_index + 1
        rec_label = f"{action.get('action','?')}: {action.get('template', action.get('note', ''))}"
        if self.recorder and self.bot:
            try:
                self.recorder.capture(self.bot, step, rec_label, "before")
            except Exception:
                pass

        result = self.executor.execute(action)

        # Only recover on hard failures — not on SKIPPED or ABORT_TASK
        hard_failure = result.status in (ActionStatus.FAILED, ActionStatus.TIMEOUT)

        # ADB device disconnect: pause execution, restart emulator, then retry once
        if hard_failure and self._is_device_disconnect(result.message):
            self._log("  ⚠ ADB device not found — pausing tasks to restart emulator...")
            self._log("  ⚠ Troubleshooting: close and reopen the emulator instance to restore the ADB connection")
            if self.on_device_not_found:
                reconnected = self.on_device_not_found()
                if reconnected:
                    self._log("  ✓ Emulator reconnected — resuming")
                    result = self.executor.execute(action)
                    hard_failure = result.status in (ActionStatus.FAILED, ActionStatus.TIMEOUT)
                else:
                    self._log("  ✗ Emulator restart failed — continuing with remaining tasks")

        if hard_failure and should_retry and action.get("required", True):
            for attempt in range(1, max_retries + 1):
                if self._stop_event.is_set():
                    break
                self._log(f"  ↩ Action failed — recovering view (attempt {attempt}/{max_retries})")
                self._recover_view()
                result = self.executor.execute(action)
                if result.status == ActionStatus.SUCCESS:
                    self._log(f"  ✓ Recovered successfully on attempt {attempt}")
                    break

        # Recording: capture screen after action with final status
        if self.recorder and self.bot:
            try:
                self.recorder.capture(self.bot, step, rec_label, result.status.value.upper())
            except Exception:
                pass

        # Take error screenshot if enabled
        if result.status in (ActionStatus.FAILED, ActionStatus.TIMEOUT) and self.config["bot"]["screenshot_on_error"]:
            try:
                ts = datetime.now().strftime("%H%M%S")
                path = f"logs/error_{ts}.png"
                Path("logs").mkdir(exist_ok=True)
                self.bot.screenshot_save(path)
            except Exception:
                pass

        return result

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def get_screenshot(self):
        """Get current screenshot (for GUI live preview)."""
        if self.bot and self.bot.is_connected():
            return self.bot.screenshot()
        return None

    def run_single_action(self, action: dict):
        """Run a single action immediately (for GUI manual testing)."""
        if not self.executor:
            self._log("Not connected")
            return None
        return self.executor.execute(action)

    def _interruptible_sleep(self, seconds: float):
        """Sleep that can be interrupted by stop signal."""
        end = time.time() + seconds
        while time.time() < end:
            if self._stop_event.is_set():
                break
            time.sleep(0.5)

    def _set_state(self, state: EngineState):
        self.state = state
        if self.on_state_change:
            self.on_state_change(state)

    def _log(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}] {message}"
        self.logger.info(full)
        if self.on_log:
            self.on_log(full)

    def _setup_logging(self):
        if getattr(sys, "frozen", False):
            log_dir = str(ensure_app_dir() / "logs")
        else:
            log_dir = self.config["bot"]["log_dir"]
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_file = f"{log_dir}/bot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

        # Use a unique named logger per instance so that multiple BotEngine objects
        # in the same process each write to their own log file. logging.basicConfig()
        # only configures the root logger once and is a no-op on subsequent calls,
        # which caused every farm after the first to produce an empty log file.
        logger_name = f"BotEngine_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{id(self)}"
        self.logger = logging.getLogger(logger_name)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False  # don't bubble up to root logger

        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(fh)

        # Console handler — only in development (not in frozen exe)
        if not getattr(sys, "frozen", False):
            sh = logging.StreamHandler()
            sh.setFormatter(logging.Formatter("%(message)s"))
            try:
                if sh.stream is not None and sh.stream.fileno() >= 0:
                    sh.stream = open(sh.stream.fileno(), mode='w',
                                     encoding='utf-8', closefd=False, buffering=1)
            except Exception:
                pass
            self.logger.addHandler(sh)

    def _close_logging(self):
        """Flush and close all handlers on this instance's logger."""
        for h in list(self.logger.handlers):
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            self.logger.removeHandler(h)

    def _save_session_log(self):
        if getattr(sys, "frozen", False):
            log_dir = str(ensure_app_dir() / "logs")
        else:
            log_dir = self.config["bot"]["log_dir"]
        path = f"{log_dir}/session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(path, "w") as f:
            json.dump(self.stats.to_dict(), f, indent=2)
        self._log(f"Session log saved → {path}")
