---
name: record-task-run
description: Run a Last Z bot task JSON against a live MEmu emulator instance and record the screen to an .mp4 — fully unattended, no manual drag-select or clicks. Use this whenever the user wants to "see a task in action," get video proof a task/fix works, debug a task JSON by watching what actually happens on screen, or asks to "record"/"capture" a run for this bot project (C:\bot). Also use it proactively after fixing a task JSON bug in this project, to verify the fix instead of just asserting it works. Do not use Windows Snipping Tool or manual screen recording for this — that requires a human to drag-select a region every time, which defeats the point.
---

# Record a task run

Wraps `C:\bot\run_and_record.py`, a script validated with several live runs on 2026-09-28. It launches a MEmu instance (or reuses one already running), runs one task JSON's actions against it through the normal `ActionExecutor`/`VisionEngine` path (same execution as the GUI's "Tool Test" feature), records the emulator window via `ffmpeg`'s `gdigrab` the whole time, and saves the result to `C:\bot\Task Recordings\<task>_<timestamp>.mp4`. It also writes a full action-by-action log to `C:\bot\logs\tool_tests\test_<timestamp>_scripted.log`, in the same format as manual Tool Test runs.

Read `run_and_record.py` itself if you need to know exactly how a step works — this file only covers how to invoke it and what to check afterward. Don't duplicate its logic; if you need to change behavior, edit the script.

## Before running: pick a safe instance

MEmu instances are identified by a 0-based index. The machine normally runs many instances at once (10-17 during a live farm rotation), so launching one more alongside others is normal — but **never target an instance that's currently mid-cycle in the automated farm loop**, or you'll interfere with real automation.

Check what's live and what's safe:

```bash
"C:\Program Files\Microvirt\MEmu\memuc.exe" listvms
```

Each row is `index,name,diskUsage,running(0/1),pid`. Any row with `running=1` is currently booted — cross-check it against which `C:\bot\logs\bot_*.log` file was most recently modified (`ls -t /c/bot/logs/bot_*.log | head -1`) and whether its last line is recent/still advancing. If a farm is actively logging in the last minute or two, that instance is live — pick a different, idle (`running=0`) index instead. An idle instance is always safe to launch fresh.

## Running it

```bash
cd /c/bot && python run_and_record.py <task_name_or_path> --index <N>
```

- `<task_name_or_path>` — a bare name like `collect_radar` (resolved against `C:\bot\tasks\`) or an explicit path.
- `--index N` — the 0-based instance index chosen above.
- Optional: `--emulator MEmu` (default), `--install-path` (default `C:\Program Files\Microvirt\MEmu`).

**Always run this via Bash with `run_in_background: true`.** A cold instance takes a few minutes to boot, connect ADB, and load Last Z before the task even starts; an already-running instance is fast (seconds). Either way, don't block the conversation waiting on it — you'll get a completion notification.

## Known limitations — read before picking a task

- **No farm-specific settings are passed.** Tasks that read `farm_settings` (e.g. `research.json` needs `research.research_in`; shield tasks need `shield.shield_type`) may abort early or behave differently than they would in a live farm run, since those values are empty here. Prefer tasks that don't depend on farm settings, or be aware results may not reflect a real farm's configuration. This is a known gap, not a bug to "fix" reflexively.
- A task that itself calls other tasks via `run_task` (common in this codebase) runs them all fine — `ActionExecutor` resolves those internally, nothing extra needed.
- **`loop: true` is not implemented at all.** The script runs the action list exactly once (`while i < len(actions)`, no restart-from-top-on-exhaustion) — it has none of `bot_engine.py`'s real looping/`max_loop_iterations` behavior. For a `loop: true` task, a recording here is a single-pass preview of the action list only; it cannot reproduce or verify anything about the task's actual repeat/restart semantics (verified 2026-10-01 by reading the script).

## After it finishes: verify, don't just trust exit code 0

**A clean exit code does not mean the recording is real.** ffmpeg can exit 0 while having silently written a 0-byte file (this happened during initial testing — an odd window height crashed the libx264 encoder, and the failure was invisible until the output was actually inspected). Always confirm both the log and the video:

1. Read the script's own tail output — it already checks file size and reports `Recording saved` vs `Recording FAILED`, plus the run's pass/fail per action.
2. Independently verify the video is real and complete:
   ```bash
   ffprobe -v error -show_entries format=duration,size -show_entries stream=width,height,codec_name,avg_frame_rate -of default=noprint_wrappers=1 "C:\bot\Task Recordings\<file>.mp4"
   ```
   A working recording has a duration roughly matching the task's actual run time, non-zero size, and `h264`. Zero duration or a missing stream means it failed even if the file exists.
3. If you need to actually see what happened (not just confirm the file is valid), use the `/watch` skill on the output path — pull a frame at whatever timestamp matters, don't just glance at the log.

## Example

```bash
# Check what's safe to use
"C:\Program Files\Microvirt\MEmu\memuc.exe" listvms
ls -t /c/bot/logs/bot_*.log | head -1 | xargs tail -3

# Run it (background — takes a few minutes on a cold instance)
cd /c/bot && python run_and_record.py hero_progression --index 1

# After the completion notification, verify
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 \
  "C:\bot\Task Recordings\hero_progression_<timestamp>.mp4"
```
