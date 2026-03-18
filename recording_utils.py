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
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

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
