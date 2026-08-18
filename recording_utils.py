"""
recording_utils.py
==================
Annotated screenshot sequences for debugging bot action flows.

Captures a screenshot before and after each action, draws an overlay
showing the action name, step number, and result, then saves as numbered
PNGs. A comparison generator produces side-by-side reference vs. actual
images you can share as context.

Usage:
    recorder = ScreenRecorder("start_rally", kind="reference")
    recorder.capture(bot, step=1, label="tap_template: btn_rally.png", status="before")
    # … execute action …
    recorder.capture(bot, step=1, label="tap_template: btn_rally.png", status="SUCCESS")

    out = generate_comparison(
        "recordings/reference/start_rally",
        "recordings/runs/start_rally_20260315_143200",
    )
    print("Comparison saved to:", out)

    report = generate_report(
        "recordings/run/start_rally_20260315_143200",
        "logs/gui_20260315.log",
    )
    print("Report saved to:", report)
"""

from __future__ import annotations

import ast
import base64
import html
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

from anomaly_watcher import AnomalyWatcher, _NOT_FOUND_RE

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

RECORDINGS_DIR = Path("recordings")

# ---------------------------------------------------------------------------
# Palette (matches the app dark theme)
# ---------------------------------------------------------------------------

_BG      = (13,  15,  18)
_ACCENT  = (240, 165,  0)
_SUCCESS = ( 63, 185, 80)
_FAIL    = (248,  81, 73)
_WARN    = (210, 153, 34)
_TEXT    = (174, 186, 199)
_DIM     = (118, 131, 144)
_DIVIDER = ( 48,  54,  61)

_STATUS_COLOR = {
    "before":  _TEXT,
    "SUCCESS": _SUCCESS,
    "FAILED":  _FAIL,
    "TIMEOUT": _FAIL,
    "SKIPPED": _WARN,
    "ABORT":   _WARN,
    "ABORT_TASK": _WARN,
}

# ---------------------------------------------------------------------------
# Font helper
# ---------------------------------------------------------------------------

_FONT_CACHE: dict = {}

def _font(size: int = 11, bold: bool = False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    try:
        from PIL import ImageFont
        candidates = [
            r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arialbd.ttf"  if bold else r"C:\Windows\Fonts\arial.ttf",
        ]
        for path in candidates:
            if Path(path).exists():
                f = ImageFont.truetype(path, size)
                _FONT_CACHE[key] = f
                return f
    except Exception:
        pass
    try:
        from PIL import ImageFont
        f = ImageFont.load_default()
        _FONT_CACHE[key] = f
        return f
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------

_BAR_H = 46   # height of the overlay bar in pixels

def _annotate(img: Image.Image, step: int, label: str,
              status: str, frame_n: int) -> Image.Image:
    """Draw a status overlay bar at the top of the screenshot."""
    img = img.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    w, _ = img.size

    # Dark background bar
    draw.rectangle([0, 0, w, _BAR_H], fill=(0, 0, 0))
    draw.line([(0, _BAR_H), (w, _BAR_H)], fill=_DIVIDER, width=1)

    # Step badge
    badge_w = 52
    draw.rectangle([6, 7, badge_w, _BAR_H - 7], fill=_ACCENT)
    draw.text((10, 12), f"#{step:02d}", fill=_BG, font=_font(13, bold=True))

    # Action label (truncated to fit)
    max_label = label[:60]
    draw.text((badge_w + 10, 14), max_label, fill=_TEXT, font=_font(11))

    # Status pill (right side)
    status_color = _STATUS_COLOR.get(status.upper(), _DIM)
    pill_text = status.upper()
    pill_w = max(len(pill_text) * 8 + 14, 72)
    pill_x = w - pill_w - 6
    draw.rectangle([pill_x, 9, w - 6, _BAR_H - 9], fill=status_color)
    draw.text((pill_x + 7, 15), pill_text, fill=_BG, font=_font(10, bold=True))

    # Frame counter bottom-right (subtle)
    draw.text((w - 62, _BAR_H - 14), f"f{frame_n:04d}", fill=_DIM, font=_font(8))

    return img


# ---------------------------------------------------------------------------
# ScreenRecorder
# ---------------------------------------------------------------------------

class ScreenRecorder:
    """
    Records annotated screenshots around each action execution.

    Frames are saved as:
        recordings/{kind}/{name}/{frame:04d}_step{step:02d}_{label}_{status}.png
    """

    def __init__(self, name: str, kind: str = "reference"):
        """
        name: sequence name (e.g. "start_rally")
        kind: "reference" (capture tool manual run) or "run" (live bot run)
        """
        self.name = name
        self.kind = kind
        self._frame_n = 0
        self.out_dir = RECORDINGS_DIR / kind / name
        self.out_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------

    def capture(self, bot, step: int, label: str,
                status: str = "before") -> Optional[Path]:
        """
        Screenshot the device and save an annotated frame.

        bot    : ADBWrapper instance
        step   : 1-based action index
        label  : short description, e.g. "tap_template: btn_rally.png"
        status : "before" | "SUCCESS" | "FAILED" | "TIMEOUT" | "SKIPPED"
        """
        try:
            img = bot.screenshot()
        except Exception:
            return None
        if img is None:
            return None

        annotated = _annotate(img, step, label, status, self._frame_n)

        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label[:28])
        filename = f"{self._frame_n:04d}_step{step:02d}_{safe}_{status}.png"
        path = self.out_dir / filename
        annotated.save(path)
        self._frame_n += 1
        return path

    # ------------------------------------------------------------------

    def close(self):
        """Nothing to close — kept for symmetry with video-based recorders."""
        pass

    def __repr__(self):
        return f"<ScreenRecorder kind={self.kind!r} name={self.name!r} frames={self._frame_n}>"


# ---------------------------------------------------------------------------
# Comparison generator
# ---------------------------------------------------------------------------

# Thumbnail dimensions for each side of the comparison
_THUMB_W = 480
_THUMB_H = 853
_PAD     = 16
_HDR_H   = 36


def generate_comparison(
    ref_dir: "str | Path",
    run_dir: "str | Path",
    out_dir: "str | Path | None" = None,
) -> Path:
    """
    Build side-by-side comparison PNGs from two recording folders.

    Pairs frames by index (ref[0] vs run[0], …).
    Returns the output directory path.

    Raises ValueError if either directory has no PNG frames.
    """
    ref_dir = Path(ref_dir)
    run_dir = Path(run_dir)

    if out_dir is None:
        out_dir = RECORDINGS_DIR / "comparisons" / f"{ref_dir.name}_vs_{run_dir.name}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ref_frames = sorted(ref_dir.glob("*.png"))
    run_frames = sorted(run_dir.glob("*.png"))

    if not ref_frames:
        raise ValueError(f"No PNG frames in reference dir: {ref_dir}")
    if not run_frames:
        raise ValueError(f"No PNG frames in run dir: {run_dir}")

    canvas_w = _THUMB_W * 2 + _PAD * 3
    canvas_h = _THUMB_H + _HDR_H + _PAD * 2
    div_x    = _PAD + _THUMB_W + _PAD // 2

    n = max(len(ref_frames), len(run_frames))

    for i in range(n):
        canvas = Image.new("RGB", (canvas_w, canvas_h), _BG)
        draw   = ImageDraw.Draw(canvas)

        # ── Header ────────────────────────────────────────────────────
        draw.text((_PAD, 8),
                  f"Step {i + 1:02d}  |  ref: {ref_dir.name}",
                  fill=_ACCENT, font=_font(12, bold=True))

        left_label_x  = _PAD + _THUMB_W // 2 - 40
        right_label_x = _PAD * 2 + _THUMB_W + _THUMB_W // 2 - 30
        draw.text((left_label_x,  _HDR_H - 2), "REFERENCE", fill=_TEXT, font=_font(11))
        draw.text((right_label_x, _HDR_H - 2), "ACTUAL",    fill=_WARN, font=_font(11))

        # Vertical divider
        draw.line([(div_x, 0), (div_x, canvas_h)], fill=_DIVIDER, width=1)

        top = _HDR_H + _PAD

        # ── Reference frame (left) ─────────────────────────────────────
        if i < len(ref_frames):
            ref_img = Image.open(ref_frames[i]).convert("RGB").resize((_THUMB_W, _THUMB_H))
            canvas.paste(ref_img, (_PAD, top))
        else:
            draw.rectangle([_PAD, top, _PAD + _THUMB_W, top + _THUMB_H], fill=(20, 20, 20))
            draw.text((_PAD + 160, top + _THUMB_H // 2), "— no frame —",
                      fill=_DIM, font=_font(11))

        # ── Run frame (right) ──────────────────────────────────────────
        run_x = _PAD * 2 + _THUMB_W
        if i < len(run_frames):
            run_img = Image.open(run_frames[i]).convert("RGB").resize((_THUMB_W, _THUMB_H))
            canvas.paste(run_img, (run_x, top))
        else:
            draw.rectangle([run_x, top, run_x + _THUMB_W, top + _THUMB_H], fill=(20, 20, 20))
            draw.text((run_x + 160, top + _THUMB_H // 2), "— no frame —",
                      fill=_DIM, font=_font(11))

        out_path = out_dir / f"compare_{i + 1:04d}.png"
        canvas.save(out_path)

    return out_dir


# ---------------------------------------------------------------------------
# Verification report
# ---------------------------------------------------------------------------

REPORTS_DIR = RECORDINGS_DIR / "reports"

_FRAME_RE = re.compile(r"^\d{4}_step(\d+)_.+_(before|SUCCESS|FAILED|TIMEOUT|SKIPPED|ABORT|ABORT_TASK)\.png$")
_RECORDING_ENABLED_RE = re.compile(r"Recording enabled → recordings/run/(.+)/")
_ACTION_START_RE = re.compile(r"▶\s*(\w+)\s*(\{.*\})?\s*$")
_RESULT_LINE_RE = re.compile(r"([✓✗])\s(.*)\((\d+)ms\)\s*$")
_CONF_RE = re.compile(r"conf=([\d.]+)")


def _group_frames_by_step(run_dir: Path) -> dict[int, dict[str, Path]]:
    """{step: {"before": path, "after": path}} — "after" is whichever
    non-"before" status frame exists for that step (there's exactly one)."""
    groups: dict[int, dict[str, Path]] = {}
    for f in sorted(run_dir.glob("*.png")):
        m = _FRAME_RE.match(f.name)
        if not m:
            continue
        step, status = int(m.group(1)), m.group(2)
        slot = "before" if status == "before" else "after"
        groups.setdefault(step, {})[slot] = f
    return groups


def _extract_run_steps(log_path: Path, run_name: str) -> tuple[list[dict], list]:
    """
    Walk the log file starting at this run's "Recording enabled" marker and
    ending just before the *next* "Recording enabled" marker for a different
    run (or EOF if this is the most recent/only run) — grouping lines into
    per-step records and feeding every line through AnomalyWatcher so its
    rolling state (duration history, last-action params) builds up exactly
    as it would watching the run live.

    Deliberately does NOT bound the scan by the recorded frame count:
    ScreenRecorder.capture() calls are wrapped in a bare try/except in
    bot_engine.py, so a single dropped screenshot would silently undercount
    frames and — if frame count were used as the stop condition — truncate
    anomaly detection partway through an otherwise-normal run.

    Returns (steps, anomalies) where each step dict has:
      {"step": int, "action_type": str, "params": str, "status": str,
       "duration_ms": float, "confidence": Optional[float], "lines": [str]}
    and each anomaly is (step_index, Anomaly).
    """
    if not log_path.exists():
        return [], []

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()

    start_idx = None
    end_idx = len(lines)
    for i, line in enumerate(lines):
        m = _RECORDING_ENABLED_RE.search(line)
        if not m:
            continue
        if start_idx is None and m.group(1) == run_name:
            start_idx = i
        elif start_idx is not None and m.group(1) != run_name:
            end_idx = i
            break
    if start_idx is None:
        return [], []

    watcher = AnomalyWatcher()
    steps: list[dict] = []
    anomalies: list = []
    current: Optional[dict] = None

    for line in lines[start_idx:end_idx]:
        anomaly = watcher.feed(line)
        if anomaly:
            anomalies.append((len(steps) + (1 if current else 0), anomaly))

        start_match = _ACTION_START_RE.search(line)
        if start_match:
            if current:
                steps.append(current)
            current = {
                "step": len(steps) + 1,
                "action_type": start_match.group(1),
                "params": start_match.group(2) or "",
                "status": None,
                "duration_ms": None,
                "confidence": None,
                "lines": [line.strip()],
            }
            continue

        if current is None:
            continue
        current["lines"].append(line.strip())

        conf_match = _CONF_RE.search(line)
        if conf_match:
            current["confidence"] = float(conf_match.group(1))

        result_match = _RESULT_LINE_RE.search(line)
        if result_match:
            current["status"] = "OK" if result_match.group(1) == "✓" else "FAIL"
            current["duration_ms"] = float(result_match.group(3))

    if current:
        steps.append(current)

    return steps, anomalies


_REPORT_THUMB_W = 300  # source frames are full-resolution screenshots (~1MB
                       # each) — embedding those directly makes a multi-step
                       # report hundreds of MB; a resized JPEG keeps a report
                       # with dozens of steps in the low single-digit MB range


def _img_data_uri(path: Optional[Path]) -> str:
    if not path or not path.exists():
        return ""
    import io
    img = Image.open(path).convert("RGB")
    w, h = img.size
    if w > _REPORT_THUMB_W:
        new_h = max(1, int(h * (_REPORT_THUMB_W / w)))
        img = img.resize((_REPORT_THUMB_W, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=78)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _step_badge(step: dict) -> str:
    """
    "OK" for a clean success, "SKIP" for anything else that isn't a genuine
    quiet-required-miss, "FAIL" only for the exact condition AnomalyWatcher
    itself flags as a "required step did not find its target" (mirrored
    here rather than re-derived, so the badges and the anomaly count in the
    header never disagree). This also correctly treats control-flow actions
    like skip_if_server_time_fresh — whose normal, expected outcome IS to
    report a skip — as "SKIP" rather than "FAIL", since their messages never
    contain "not found".
    """
    if step.get("status") != "FAIL":
        return "OK"
    last_line = step["lines"][-1] if step["lines"] else ""
    if "✗" not in last_line or not _NOT_FOUND_RE.search(last_line):
        return "SKIP"
    try:
        params = ast.literal_eval(step["params"]) if step["params"] else {}
    except Exception:
        params = {}
    required = params.get("required", True)
    guarded = (params.get("skip_task_if_not_found", False)
               or params.get("skip_to_on_found") is not None)
    if (not required) or guarded:
        return "SKIP"
    return "FAIL"


_SEVERITY_COLOR = {"high": "#f85149", "medium": "#d29922", "low": "#8b949e"}


def generate_report(run_dir: "str | Path", log_path: "str | Path", out_path: "str | Path | None" = None) -> Path:
    """
    Build a single self-contained HTML verification report for one recorded
    run: a step-by-step timeline with before/after thumbnails, confidence
    scores, and durations, with a "Flagged Anomalies" section (computed via
    AnomalyWatcher, the same classifier used for real-time flagging) pinned
    at the top linking down to the relevant step.

    run_dir  : recordings/run/<name>/ produced by ScreenRecorder
    log_path : the gui_YYYYMMDD.log covering that run
    """
    run_dir = Path(run_dir)
    log_path = Path(log_path)
    run_name = run_dir.name

    frame_groups = _group_frames_by_step(run_dir)
    if not frame_groups:
        raise ValueError(f"No recorded frames found in: {run_dir}")

    steps, anomalies = _extract_run_steps(log_path, run_name)

    if out_path is None:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = REPORTS_DIR / f"{run_name}.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_steps = max(len(frame_groups), len(steps))
    badges = {s["step"]: _step_badge(s) for s in steps}
    n_fail = sum(1 for b in badges.values() if b == "FAIL")

    parts: list[str] = []
    parts.append(f"""<!doctype html><html><head><meta charset="utf-8">
<title>Verification report — {html.escape(run_name)}</title>
<style>
  body {{ background:#0d0f12; color:#c9d1d9; font-family:Segoe UI,Arial,sans-serif; margin:0; padding:24px; }}
  h1 {{ color:#f0a500; font-size:20px; margin:0 0 4px; }}
  .meta {{ color:#8b949e; font-size:13px; margin-bottom:20px; }}
  .anomalies {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:14px 18px; margin-bottom:24px; }}
  .anomalies h2 {{ margin-top:0; font-size:15px; color:#e6edf3; }}
  .anomaly-row {{ display:flex; gap:10px; padding:6px 0; border-bottom:1px solid #21262c; font-size:13px; }}
  .anomaly-row:last-child {{ border-bottom:none; }}
  .sev {{ font-weight:600; min-width:70px; }}
  .step {{ border:1px solid #30363d; border-radius:8px; margin-bottom:14px; overflow:hidden; }}
  .step-hdr {{ display:flex; align-items:center; gap:12px; padding:8px 14px; background:#161b22; font-size:13px; }}
  .badge {{ font-weight:700; padding:2px 8px; border-radius:4px; font-size:11px; }}
  .badge-ok {{ background:#3fb950; color:#0d0f12; }}
  .badge-skip {{ background:#8b949e; color:#0d0f12; }}
  .badge-fail {{ background:#f85149; color:#0d0f12; }}
  .frames {{ display:flex; gap:8px; padding:10px 14px; }}
  .frames img {{ max-width:260px; border:1px solid #30363d; border-radius:4px; }}
  .no-frame {{ width:200px; height:120px; display:flex; align-items:center; justify-content:center;
               color:#484f58; border:1px dashed #30363d; border-radius:4px; font-size:12px; }}
  .params {{ color:#8b949e; font-size:12px; padding:0 14px 10px; word-break:break-all; }}
  a {{ color:#58a6ff; text-decoration:none; }}
</style></head><body>""")

    parts.append(f"<h1>Verification report — {html.escape(run_name)}</h1>")
    parts.append(
        f'<div class="meta">generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} · '
        f'{n_steps} step(s) · {n_fail} failed step(s) · {len(anomalies)} flagged anomal{"y" if len(anomalies)==1 else "ies"}</div>'
    )

    parts.append('<div class="anomalies"><h2>⚑ Flagged Anomalies</h2>')
    if anomalies:
        for step_idx, a in anomalies:
            color = _SEVERITY_COLOR.get(a.severity, "#8b949e")
            parts.append(
                f'<div class="anomaly-row"><span class="sev" style="color:{color}">{a.severity.upper()}</span>'
                f'<a href="#step-{step_idx}">step {step_idx}</a>'
                f'<span>{html.escape(a.reason)}</span></div>'
            )
    else:
        parts.append('<div class="anomaly-row">None — this run looked clean.</div>')
    parts.append("</div>")

    for step in steps:
        n = step["step"]
        frames = frame_groups.get(n, {})
        badge_text = badges.get(n, "?")
        badge_cls = {"OK": "badge-ok", "SKIP": "badge-skip", "FAIL": "badge-fail"}.get(badge_text, "badge-skip")
        conf = step.get("confidence")
        dur = step.get("duration_ms")

        parts.append(f'<div class="step" id="step-{n}">')
        parts.append('<div class="step-hdr">')
        parts.append(f'<span>#{n:02d}</span>')
        parts.append(f'<b>{html.escape(step["action_type"])}</b>')
        parts.append(f'<span class="badge {badge_cls}">{badge_text}</span>')
        if dur is not None:
            parts.append(f'<span>{dur:.0f}ms</span>')
        if conf is not None:
            parts.append(f'<span>conf={conf:.2f}</span>')
        parts.append("</div>")

        parts.append('<div class="frames">')
        for slot, caption in (("before", "before"), ("after", "after")):
            img_path = frames.get(slot)
            uri = _img_data_uri(img_path)
            if uri:
                parts.append(f'<img src="{uri}" title="{caption}">')
            else:
                parts.append(f'<div class="no-frame">no {caption} frame</div>')
        parts.append("</div>")

        if step.get("params"):
            parts.append(f'<div class="params">{html.escape(step["params"])}</div>')
        parts.append("</div>")

    parts.append("</body></html>")

    out_path.write_text("\n".join(parts), encoding="utf-8")
    return out_path
