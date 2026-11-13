"""
run_and_record.py — Prototype
==============================
Launches a MEmu instance, runs a single task JSON against it standalone
(same execution path as capture_tool.py's "Tool Test"), records the
emulator window to an .mp4 via ffmpeg (no manual region-select needed),
and saves the recording to C:\\bot\\Task Recordings.

Usage:
    python run_and_record.py <task_name_or_path> --index N [--emulator MEmu]

Example:
    python run_and_record.py collect_radar --index 1
    python run_and_record.py "tasks/Read Mail.json" --index 1
"""

import argparse
import ctypes
import json
import subprocess
import sys
import time

# Windows console default codepage (cp1252) can't encode characters like the
# checkmarks/arrows in launcher.py/action_executor.py's log messages — same
# issue hit repeatedly running one-off `py -c` scripts against this codebase.
# Reconfigure stdout once here instead of requiring PYTHONIOENCODING=utf-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from datetime import datetime
from pathlib import Path

from launcher import EmulatorLauncher, LASTZ_PKG, LASTZ_ACT
from paths import get_resource_dir, get_farms_path
from vision import VisionEngine
from action_executor import ActionExecutor, ActionStatus

ROOT            = get_resource_dir()
TASKS_DIR       = ROOT / "tasks"
RECORDINGS_DIR  = Path(r"C:\bot\Task Recordings")
LOG_DIR         = ROOT / "logs" / "tool_tests"
MEMUC_PATH      = r"C:\Program Files\Microvirt\MEmu\memuc.exe"


def log(msg: str, level: str = "info"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}][{level.upper()}] {msg}")


def resolve_task_path(task_name: str) -> Path:
    p = Path(task_name)
    if p.suffix == ".json" and p.exists():
        return p
    candidate = TASKS_DIR / (task_name if task_name.endswith(".json") else f"{task_name}.json")
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"Task JSON not found: {task_name!r} (looked in {TASKS_DIR})")


def load_farm_settings(index: int) -> dict:
    """
    Look up the real farm_settings dict (farms.json's per-farm "tasks" block)
    for the given 0-based MEmu instance index, the same shape bot_engine.py
    passes to ActionExecutor in a live run. Without this, tasks that read
    farm_settings (shield.*, research.research_in, rally.boomer_level, etc.)
    silently no-op instead of reflecting what a real farm run would do —
    missing "setting" gates default to falsy/skip in run_task, so e.g. a
    shield task would never actually buy anything.

    farms.json's "emu_index" is 1-based (emu_index == memuc index + 1).
    Returns {} (matching the prior default) if farms.json or the farm entry
    can't be found, logging a warning either way so the gap is visible
    instead of silently producing a misleading recording.
    """
    try:
        farms_path = get_farms_path()
        data = json.loads(farms_path.read_text(encoding="utf-8"))
        farms = data.get("farms", data) if isinstance(data, dict) else data
        for farm in farms:
            if farm.get("emu_index") == index + 1:
                log(f"Loaded farm_settings for '{farm.get('name')}' (emu_index {index + 1})")
                return farm.get("tasks", {})
        log(f"No farms.json entry with emu_index={index + 1} — running with empty farm_settings", "warn")
    except Exception as e:
        log(f"Failed to load farm_settings from farms.json: {e} — running with empty farm_settings", "warn")
    return {}


def resolve_window_title(index: int) -> str:
    """
    Instance window titles are the instance's configured NAME (e.g. "(Prex Farm 2)"),
    not necessarily the default "(MEmu - N)" pattern — instances get renamed. Ask
    memuc directly instead of guessing from a static format string.
    """
    result = subprocess.run([MEMUC_PATH, "listvms"], capture_output=True, text=True, timeout=10)
    for line in result.stdout.strip().splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and parts[0].strip() == str(index):
            return f"({parts[1].strip()})"
    raise RuntimeError(f"Could not resolve window title for instance index {index} via memuc listvms")


def ensure_window_restored(window_title: str):
    """
    Un-minimize the target MEmu window before ffmpeg attaches to it.

    gdigrab can't capture a minimized window's client area — it has none — and
    silently produces a 0-byte .mp4 with no clear error (root-caused live
    2026-10-05: IsIconic()==True, window parked at Windows' off-screen minimize
    coordinates -32000,-32000; see C:\\bot\\logs\\bug_reports\\20261005_083517_start_build.md).
    An idle instance left over from an earlier session is the common way this
    happens — nothing in this script previously checked for it.

    Uses the same timeout-bounded GetWindowText pattern as gui.py's
    _arrange_memu_windows (plain GetWindowTextW can block forever against a
    hung/frozen emulator guest — fixed there 2026-09-28, same hazard applies
    here) instead of a naive FindWindowW, since window_title here is only a
    substring of the real title bar text (e.g. ffmpeg's "-i title=(Prex Farm 2)"
    matches "MEMU PLAY (Prex Farm 2)" by substring, which FindWindowW's exact
    match would miss).
    """
    user32 = ctypes.windll.user32
    SW_RESTORE       = 9
    SMTO_ABORTIFHUNG = 0x0002
    WM_GETTEXTLENGTH = 0x000E
    WM_GETTEXT       = 0x000D
    needle = window_title.strip("()")

    # Explicit argtypes required here: without them ctypes guesses the LPARAM
    # type from the Python arg it's given, and a raw pointer int from
    # ctypes.addressof() on a 64-bit build overflows the guessed 32-bit type
    # (hit live 2026-10-05 testing this exact fix — "OverflowError: int too
    # long to convert" inside the EnumWindows callback). c_void_p accepts
    # both an int address and a ctypes buffer/array directly.
    user32.SendMessageTimeoutW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

    def _win_text_safe(hwnd, timeout_ms: int = 250):
        result = ctypes.c_size_t(0)
        ok = user32.SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, None, None,
                                         SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result))
        if not ok or result.value == 0 or result.value > 8192:
            return None
        length = result.value
        buf = ctypes.create_unicode_buffer(length + 1)
        ok = user32.SendMessageTimeoutW(hwnd, WM_GETTEXT, length + 1, buf,
                                         SMTO_ABORTIFHUNG, timeout_ms, ctypes.byref(result))
        return buf.value if ok else None

    found_hwnd = [None]

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _win_text_safe(hwnd)
        if title and needle in title:
            found_hwnd[0] = hwnd
            return False  # stop enumerating
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)
    user32.EnumWindows(EnumWindowsProc(_cb), 0)

    hwnd = found_hwnd[0]
    if not hwnd:
        log(f"ensure_window_restored: no window matching '{needle}' found — skipping", "warn")
        return
    if user32.IsIconic(hwnd):
        log(f"Window '{needle}' is minimized — restoring before recording")
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.5)  # let the restore animation/redraw settle before ffmpeg attaches


def start_recording(window_title: str, out_path: Path, ffmpeg_log_path: Path) -> subprocess.Popen:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "gdigrab",
        "-framerate", "15",
        "-i", f"title={window_title}",
        # Captured window dimensions are whatever size the window happens to be —
        # not guaranteed even. libx264 requires even width/height; without this,
        # an odd-height window (e.g. 542x993) fails the encoder immediately and
        # silently produces a 0-byte file (root-caused live 2026-09-28: Prex Farm 2
        # demo run). Force both dimensions even by trimming at most 1px.
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-vcodec", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        str(out_path),
    ]
    log(f"ffmpeg: {' '.join(cmd)}")
    ffmpeg_log = open(ffmpeg_log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=ffmpeg_log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def stop_recording(proc: subprocess.Popen, timeout: float = 20.0):
    """Ask ffmpeg to finish the file cleanly ('q' on stdin) instead of killing it,
    which can leave the mp4 truncated/unplayable."""
    if proc.poll() is not None:
        return
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log("ffmpeg didn't exit cleanly after 'q' — killing", "warn")
        proc.kill()


def run_task_actions(bot, emulator_type: str, actions: list, tlog, farm_settings: dict = None) -> list:
    vision = VisionEngine(confidence_threshold=0.8)
    executor = ActionExecutor(
        bot=bot,
        vision=vision,
        template_dir=str(ROOT / "templates"),
        log_callback=lambda m: tlog(m, "info"),
        emulator_type=emulator_type,
        farm_settings=farm_settings or {},
    )

    log_lines = []
    i = 0
    while i < len(actions):
        action = actions[i]
        label = action.get("action", "?")
        tlog(f"[{i + 1}/{len(actions)}] {label}", "info")
        result = executor.execute(action)
        tag = "ok" if result else "fail"
        tlog(f"  -> {result.status.value}: {result.message}", tag)
        if result.status == ActionStatus.ABORT_TASK:
            tlog("Task aborted — stopping run", "warn")
            break
        i = result.skip_to if result.skip_to is not None else i + 1
    tlog("Done.", "ok")
    return log_lines


def launch_instance(emulator: str, install_path: str, index: int):
    """Launch (or reuse) a MEmu instance and return (bot, window_title)."""
    log(f"Launching {emulator} index={index}...")
    launcher = EmulatorLauncher(
        emulator_type=emulator,
        install_path=install_path,
        adb_path="adb",
        package=LASTZ_PKG,
        activity=LASTZ_ACT,
        log_callback=log,
    )
    ok = launcher.launch_and_connect(index=index, wait_for_game=True)
    if not ok:
        raise RuntimeError(f"Launch failed for index {index}")
    bot = launcher.get_bot(index)
    window_title = resolve_window_title(index)
    log(f"Resolved window title: {window_title}")
    return bot, window_title


def record_task(bot, emulator_type: str, window_title: str, task_path: Path, farm_settings: dict = None) -> dict:
    """
    Run one task JSON against an already-connected bot, recording the whole
    thing. Returns a result dict: {name, out_path, size, log_path, ok}.
    Never raises for a task-level failure (aborted/skipped actions) — only
    for something that prevents recording at all (e.g. bad JSON).
    """
    task_data = json.loads(task_path.read_text(encoding="utf-8"))
    actions   = task_data.get("actions", task_data) if isinstance(task_data, dict) else task_data
    task_name = task_data.get("name", task_path.stem) if isinstance(task_data, dict) else task_path.stem

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_lines = [f"Run And Record — {task_name} — {ts}", "=" * 50]

    def tlog(msg, level="info"):
        line = f"[{time.strftime('%H:%M:%S')}][{level.upper()}] {msg}"
        log_lines.append(line)
        log(msg, level)

    out_path = RECORDINGS_DIR / f"{task_name}_{ts}.mp4"
    ffmpeg_log_path = LOG_DIR / f"ffmpeg_{ts}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ensure_window_restored(window_title)
    rec_proc = start_recording(window_title, out_path, ffmpeg_log_path)
    time.sleep(2)  # let ffmpeg actually attach to the window before actions start

    log(f"Running {len(actions)} actions from {task_path.name}...")
    try:
        run_task_actions(bot, emulator_type, actions, tlog, farm_settings)
    finally:
        log("Stopping recording...")
        stop_recording(rec_proc)

    log_file = LOG_DIR / f"test_{ts}_scripted.log"
    log_file.write_text("\n".join(log_lines), encoding="utf-8")

    # A 0-byte or missing file means ffmpeg failed — don't report success blindly,
    # surface its actual log instead of leaving that failure silent (root-caused
    # live 2026-09-28: an odd window height crashed the encoder with a 0-byte
    # output and no visible error since ffmpeg's stderr was never captured).
    size = out_path.stat().st_size if out_path.exists() else 0
    if size > 0:
        log(f"Recording saved -> {out_path} ({size / 1_000_000:.1f} MB)", "ok")
    else:
        log(f"Recording FAILED (missing or 0 bytes) at {out_path}", "error")
        try:
            tail = ffmpeg_log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
            log("ffmpeg log tail:\n" + "\n".join(tail), "error")
        except Exception:
            pass
    log(f"Log saved -> {log_file}", "ok")

    return {"name": task_name, "out_path": out_path, "size": size, "log_path": log_file, "ok": size > 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", help="Task name (e.g. 'collect_radar') or path to a task JSON")
    parser.add_argument("--index", type=int, required=True, help="0-based MEmu instance index")
    parser.add_argument("--emulator", default="MEmu")
    parser.add_argument("--install-path", default=r"C:\Program Files\Microvirt\MEmu")
    args = parser.parse_args()

    task_path = resolve_task_path(args.task)
    farm_settings = load_farm_settings(args.index)
    bot, window_title = launch_instance(args.emulator, args.install_path, args.index)
    record_task(bot, args.emulator, window_title, task_path, farm_settings)


if __name__ == "__main__":
    main()
