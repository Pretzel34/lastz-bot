# Last Z Bot — Developer Context

Automates the Android game **Last Z** via ADB on Windows emulators (MEmu, LDPlayer, NOX).

## Architecture

```
gui.py              → UI entry point (tkinter)
launcher.py         → Emulator detection/launch (MEmu, LDPlayer, NOX)
bot_engine.py       → Orchestrates tasks, manages state and logs
action_executor.py  → Executes individual JSON actions against ADB device
adb_wrapper.py      → ADB connection wrapper
vision.py           → Template matching + OCR
tasks/              → JSON task definitions (one file per task)
templates/          → PNG template images for vision matching
config.json         → Runtime config (emulator host/port, vision confidence, etc.)
farms.json          → Farm list + per-farm task settings + bot_settings
```

## Current Emulator

**NOX Player** — ADB port 62025, instance Nox_1 (CLI index 1)

- NOX ADB: `C:\Program Files (x86)\Nox\bin\nox_adb.exe`
- Switch emulator: GUI → Bot Settings tab → Emulator dropdown (also updates `farms.json` `bot_settings.emulator`)

## Task JSON Schema

Each file in `tasks/` is a JSON object:
```json
{
  "name": "task_name",
  "actions": [ ...action objects... ]
}
```

All coordinates use **percentage of screen size** (`x_pct`, `y_pct`) — never hardcode pixel values. Screen is 540×960 baseline.

### All Supported Action Types

#### Navigation / View
| Action | Required fields | Optional fields | Notes |
|--------|----------------|-----------------|-------|
| `ensure_hq_view` | — | `hq_btn`, `world_btn`, `x_pct`, `y_pct` | **Use as first action in most tasks.** Detects HQ vs world view and navigates to HQ. |
| `center_hq` | — | — | Runs `tasks/center_hq.json` to reset camera to HQ. |
| `center_view` | — | `template` | Resets camera; optionally finds and taps template. |
| `zoom_out` | — | `steps` (default 3) | Sends emulator zoom key via Win32. |
| `press_back` | — | `required` (bool) | |
| `press_home` | — | — | |

#### Tapping
| Action | Required fields | Optional fields | Notes |
|--------|----------------|-----------------|-------|
| `tap` | `x` (px), `y` (px) | — | Avoid — use `tap_zone` for resolution-independence. |
| `tap_zone` | `x_pct`, `y_pct` | `note` | Preferred tap method. |
| `tap_template` | `template` | `timeout`, `threshold` | Fails if template not found. Use `if_template_tap` for optional UI elements. |
| `tap_text` | `text` | `region` | OCR tap. |
| `if_template_tap` | `template` | `required`, `skip_task_if_not_found`, `log_success`, `log_skip` | Tap only if template visible — no fail if missing. |
| `tap_template_or_zone` | `template`, `x_pct`, `y_pct` | — | Tries template first; falls back to zone. |
| `tap_template_or_template` | `template`, `fallback_template` | — | Tries primary, then fallback. |
| `repeat_if_template` | `template` | `max_taps` | Keeps tapping until template disappears. |

#### Scrolling / Swiping
| Action | Required fields | Optional fields |
|--------|----------------|-----------------|
| `swipe` | `x1`, `y1`, `x2`, `y2` | `duration_ms` (default 300) |
| `scroll_down` | — | `distance` (px), `duration_ms` |
| `scroll_up` | — | `distance` (px), `duration_ms` |
| `scroll_right` | — | `distance_pct`, `duration_ms` |
| `scroll_left` | — | `distance_pct`, `duration_ms` |

#### Waiting
| Action | Required fields | Optional fields |
|--------|----------------|-----------------|
| `wait` | `seconds` | — |
| `wait_for_template` | `template`, `timeout` | `threshold` |
| `wait_for_template_gone` | `template`, `timeout` | — |

#### Formation / Gathering
| Action | Required fields | Optional fields | Notes |
|--------|----------------|-----------------|-------|
| `check_formations_busy` | `slots` (list of `{x_pct, y_pct}`), `locked_template` | — | Aborts task early if all slots occupied. |
| `tap_free_formation` | `slots` (list of `{x_pct, y_pct}`), `locked_template` | `log_all_busy` | Taps first free formation slot. |
| `find_template_with_scroll` | `template`, `scroll_x_pct`, `scroll_y_pct` | `log_not_found` | Scrolls until template found. |
| `search_resource_level` | `resource_template`, `scroll_x_pct`, `scroll_y_pct`, `template_pattern`, `plus_template`, `minus_template`, `max_level` | — | Steps up level until resource found. |
| `adjust_resource_level` | `template_pattern`, `plus_template`, `minus_template`, `target_level` | — | Sets resource level directly. |

#### Rally / Game State
| Action | Required fields | Optional fields | Notes |
|--------|----------------|-----------------|-------|
| `adjust_boomer_level` | `plus_template`, `minus_template`, `target_level` | — | Reads current level, taps +/- to reach target. |
| `rally_count_check` | — | — | Checks rally count against farm setting `rally.max_rallies_per_day`. |
| `check_claimed` | `template` | — | Verifies a reward was already claimed. |
| `verify_setting_template` | `template_pattern`, `setting_key`, `setting_value` | — | Checks UI matches a farms.json setting. |

#### Input / App
| Action | Required fields | Optional fields |
|--------|----------------|-----------------|
| `type_text` | `text` | — |
| `press_enter` | — | — |
| `launch_app` | `package` | — |
| `stop_app` | `package` | — |
| `screenshot` | — | `filename` |

#### Advanced / Control Flow
| Action | Required fields | Optional fields | Notes |
|--------|----------------|-----------------|-------|
| `loop_until_template` | `templates` (list), `on_each` (list of actions) | — | Repeats `on_each` until any template in `templates` appears. |
| `compare_resources` | `anchor_template` | — | OCR resource panel, compares two values. |
| `read_resource_priority` | `anchor_template` | — | OCR resource panel, ranks by Total RSS, writes `logs/resource_priority.json`. |

## Template Naming Convention

All templates are PNG files in `templates/`. Naming pattern: `btn_<description>.png`

Examples: `btn_go_world.png`, `btn_march.png`, `btn_gather_lumber.png`, `btn_resource_lvl_3.png`

Dynamic templates use `{value}` placeholder: `btn_resource_lvl_{value}.png`

## farms.json Structure

Top-level keys:
- `farms` — array of farm objects
- `bot_settings` — global settings (emulator type/path, timeouts, vision confidence)

**Farm object:**
```json
{
  "name": "Farm Name",
  "emu_index": 1,
  "port": 62025,
  "enabled": true,
  "tasks": { ...task blocks... }
}
```

**Task blocks in farms.json** (control what bot_engine runs per farm):
- `daily_tasks` — `collect_idle_reward`, `collect_free_rewards`, `collect_vip_rewards`, `collect_radar`, `complete_radar`, `Collect Fuel`, `Read Mail`, `collect_recruits`
- `rally` — `quick_join_rally`, `create_rally`, `boomer_level`, `use_max_formations`, `max_rallies_per_day`
- `gathering` — `collect_wood`, `collect_food`, `collect_electricity`, `collect_zents`, `resource_site_level`, `max_formations`

Note: task names in `farms.json` must match filenames in `tasks/` (without `.json`).

## Game Package

`com.readygo.barrel.gp`

## Common Pitfalls

- Always start tasks with `ensure_hq_view` — tasks assume HQ view by default
- Use `if_template_tap` (not `tap_template`) for UI elements that may not be present
- `tap_zone` coordinates are `%` of screen — test at 540×960 baseline
- Formation slots are 0-indexed internally; `tap_free_formation` tracks `_used_slot_indices` across a session
- `farms.json` `bot_settings.emulator` must match what launcher expects ("MEmu", "LDPlayer", or "Nox")

## Running / Testing

```bash
cd /c/bot
python gui.py                    # main entry point
python bot_engine.py             # run bot headless
python launcher.py               # test launcher/emulator detection
python capture_tool.py           # screenshot + template capture utility
```

Logs written to `logs/`. Enable `screenshot_on_error` in bot_settings to capture failure screenshots.

---

## Session Log

> This section is maintained automatically. Each entry captures what was worked on, decisions made, and what's pending. Claude should read this at the start of each session to restore context, and update it at the end (or when asked to "save to memory" / "update the log").

<!-- LATEST ENTRY AT TOP -->

### 2026-04-13
- **Topic:** Task verification + self-healing execution design
- **Discussed:** Two approaches to action verification:
  1. `verify` field in task JSON (post-action template assertion) — tabled for now, revisit later
  2. **Self-healing execution loop** — preferred approach, to be implemented
- **Self-healing design (agreed upon):**
  - Action attempted → if fails → recovery routine → retry → if success: continue / if still failing: log report + skip to next task
  - Recovery steps (in order): wait + retry (lag), press back 1-2x (unexpected popup/dialog), re-run `ensure_hq_view` (wrong view state), give up (unknown state)
  - Recovery happens silently — only escalates to a report if all steps exhausted
  - Failure report should include: task name, action index, recovery steps attempted, screenshot of final state
  - `action_executor.py` handles recovery logic; signals `bot_engine.py` with "unrecoverable" to skip to next task
- **Pending:** Implementation of self-healing loop in `action_executor.py` + `bot_engine.py`
- **TODO:** Test `collect_radar` changes at next reset — verify laura radar log_skip, collect loop, first-tap popup dismissal, and repeat_if_template behavior
- **TODO:** Review `collect_vip_rewards` task — check current implementation and identify any improvements needed

### 2026-04-13 (earlier)
- **Topic:** Persistent memory setup
- **Done:** Added this Session Log section to CLAUDE.md so context carries over between Cowork sessions automatically.
- **How it works:** Since CLAUDE.md is loaded at the start of every session, anything logged here is instantly available next time — no extra steps needed.
