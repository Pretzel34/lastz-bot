"""
vision.py
=========
Vision layer for Android bot automation.
Provides template matching, OCR, and screen detection
on top of PIL screenshots from adb_wrapper.py.

Requirements:
    pip install opencv-python pillow easyocr numpy
"""

import time
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

try:
    import cv2
except ImportError:
    raise ImportError("Run: pip install opencv-python")

try:
    from PIL import Image
except ImportError:
    raise ImportError("Run: pip install pillow")

try:
    import easyocr
except ImportError:
    raise ImportError("Run: pip install easyocr")


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    """Result of a template match search."""
    found: bool
    x: int = 0          # center x of match
    y: int = 0          # center y of match
    confidence: float = 0.0
    region: tuple = ()  # (x1, y1, x2, y2) bounding box

    def __bool__(self):
        return self.found


@dataclass
class OCRResult:
    """Result of OCR text detection."""
    text: str
    x: int = 0
    y: int = 0
    confidence: float = 0.0
    region: tuple = ()  # (x1, y1, x2, y2) bounding box


# ---------------------------------------------------------------------------
# Vision Engine
# ---------------------------------------------------------------------------

class VisionEngine:
    """
    Provides computer vision capabilities for bot automation.

    Works directly with PIL Images from ADBWrapper.screenshot().

    Usage:
        vision = VisionEngine()

        # Find a button by image
        result = vision.find_template(screenshot, "templates/play_button.png")
        if result:
            bot.tap(result.x, result.y)

        # Read text from screen
        texts = vision.read_text(screenshot)
        for t in texts:
            print(t.text, t.x, t.y)

        # Wait for a screen to appear
        result = vision.wait_for_template(bot, "templates/main_menu.png", timeout=15)
    """

    def __init__(self, confidence_threshold: float = 0.8, canonical_size: tuple = (540, 960)):
        """
        confidence_threshold: minimum match confidence (0.0–1.0).
        canonical_size: (width, height) that templates were captured at.
                        Screenshots larger than this are auto-scaled down before
                        matching, and result coordinates are scaled back up.
        """
        self.confidence_threshold = float(confidence_threshold)
        self.canonical_size = canonical_size
        self._ocr_reader = None  # lazy-loaded on first use
        self.templates: dict[str, np.ndarray] = {}  # cache loaded templates
        self._logged_scale = False  # only log scaling once per session

    # ------------------------------------------------------------------
    # Template Matching — find a button/image on screen
    # ------------------------------------------------------------------

    def find_template(
        self,
        screenshot: Image.Image,
        template_path: str,
        threshold: Optional[float] = None,
        region: Optional[tuple] = None,
    ) -> MatchResult:
        """
        Search for a template image within a screenshot.

        Args:
            screenshot:    PIL Image from bot.screenshot()
            template_path: path to your reference image (e.g. "templates/play_btn.png")
            threshold:     override default confidence threshold for this call
            region:        (x1, y1, x2, y2) to limit search area — faster and
                           more accurate when you know roughly where to look

        Returns:
            MatchResult with found=True and (x, y) center coords if found.

        Example:
            result = vision.find_template(screen, "templates/collect_button.png")
            if result:
                bot.tap(result.x, result.y)
        """
        threshold = threshold or self.confidence_threshold

        # Normalize screenshot to canonical size for template matching
        norm_screen, sx, sy = self._normalize_screenshot(screenshot)
        screen_cv = self._pil_to_cv(norm_screen)

        # Crop to region if specified (scale region to canonical space)
        offset_x, offset_y = 0, 0
        if region:
            x1, y1, x2, y2 = (int(region[0]/sx), int(region[1]/sy),
                               int(region[2]/sx), int(region[3]/sy))
            screen_cv = screen_cv[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1

        # Load template (with caching)
        template_cv = self._load_template(template_path)
        if template_cv is None:
            print(f"[Vision] Template not found: {template_path}")
            return MatchResult(found=False)

        # Run template matching
        result = cv2.matchTemplate(screen_cv, template_cv, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val < threshold:
            return MatchResult(found=False, confidence=max_val)

        # Calculate center coordinates in canonical space, then scale back to original
        th, tw = template_cv.shape[:2]
        cx = int((max_loc[0] + tw // 2 + offset_x) * sx)
        cy = int((max_loc[1] + th // 2 + offset_y) * sy)
        x1 = int((max_loc[0] + offset_x) * sx)
        y1 = int((max_loc[1] + offset_y) * sy)

        return MatchResult(
            found=True,
            x=cx,
            y=cy,
            confidence=max_val,
            region=(x1, y1, int(x1 + tw * sx), int(y1 + th * sy)),
        )

    def find_all_templates(
        self,
        screenshot: Image.Image,
        template_path: str,
        threshold: Optional[float] = None,
        max_results: int = 20,
    ) -> list[MatchResult]:
        """
        Find ALL occurrences of a template on screen.
        Useful for games where multiple identical buttons appear
        (e.g. collect rewards from multiple buildings at once).

        Returns list of MatchResult sorted by confidence (highest first).
        """
        threshold = threshold or self.confidence_threshold
        norm_screen, sx, sy = self._normalize_screenshot(screenshot)
        screen_cv = self._pil_to_cv(norm_screen)
        template_cv = self._load_template(template_path)

        if template_cv is None:
            return []

        result = cv2.matchTemplate(screen_cv, template_cv, cv2.TM_CCOEFF_NORMED)
        th, tw = template_cv.shape[:2]

        locations = np.where(result >= threshold)
        matches = []

        for pt in zip(*locations[::-1]):  # x, y pairs
            cx = int((pt[0] + tw // 2) * sx)
            cy = int((pt[1] + th // 2) * sy)
            conf = result[pt[1], pt[0]]
            matches.append(MatchResult(
                found=True,
                x=cx, y=cy,
                confidence=float(conf),
                region=(int(pt[0]*sx), int(pt[1]*sy),
                        int((pt[0]+tw)*sx), int((pt[1]+th)*sy)),
            ))

        # Non-maximum suppression — remove overlapping duplicates
        matches = self._suppress_duplicates(matches, min_distance=tw // 2)
        matches.sort(key=lambda m: m.confidence, reverse=True)
        return matches[:max_results]

    def template_exists(
        self,
        screenshot: Image.Image,
        template_path: str,
        threshold: Optional[float] = None,
    ) -> bool:
        """
        Quick check — is this template visible on screen right now?
        Use this to detect which screen/state the game is in.

        Example:
            if vision.template_exists(screen, "templates/main_menu.png"):
                print("We are on the main menu")
        """
        return self.find_template(screenshot, template_path, threshold).found

    # ------------------------------------------------------------------
    # Waiting — block until something appears or disappears
    # ------------------------------------------------------------------

    def wait_for_template(
        self,
        bot,                    # ADBWrapper instance
        template_path: str,
        timeout: float = 30.0,
        poll_interval: float = 1.0,
        threshold: Optional[float] = None,
    ) -> MatchResult:
        """
        Keep taking screenshots until template appears or timeout is reached.
        This is safer than fixed sleep() calls.

        Example:
            result = vision.wait_for_template(bot, "templates/play_button.png", timeout=15)
            if result:
                bot.tap(result.x, result.y)
            else:
                print("Play button never appeared!")
        """
        print(f"[Vision] Waiting for: {Path(template_path).name} (timeout={timeout}s)")
        start = time.time()

        while time.time() - start < timeout:
            screenshot = bot.screenshot()
            result = self.find_template(screenshot, template_path, threshold)
            if result:
                elapsed = time.time() - start
                print(f"[Vision] Found {Path(template_path).name} "
                      f"at ({result.x}, {result.y}) after {elapsed:.1f}s "
                      f"[conf: {result.confidence:.2f}]")
                return result
            time.sleep(poll_interval)

        print(f"[Vision] Timeout — {Path(template_path).name} never appeared")
        return MatchResult(found=False)

    def wait_for_template_gone(
        self,
        bot,
        template_path: str,
        timeout: float = 30.0,
        poll_interval: float = 1.0,
    ) -> bool:
        """
        Wait until a template disappears from screen.
        Useful for waiting for loading screens to finish.

        Example:
            vision.wait_for_template_gone(bot, "templates/loading_spinner.png", timeout=30)
        """
        print(f"[Vision] Waiting for {Path(template_path).name} to disappear...")
        start = time.time()

        while time.time() - start < timeout:
            screenshot = bot.screenshot()
            if not self.template_exists(screenshot, template_path):
                print(f"[Vision] {Path(template_path).name} is gone")
                return True
            time.sleep(poll_interval)

        print(f"[Vision] Timeout — {Path(template_path).name} never disappeared")
        return False

    # ------------------------------------------------------------------
    # OCR — read text from screen
    # ------------------------------------------------------------------

    def read_text(
        self,
        screenshot: Image.Image,
        region: Optional[tuple] = None,
        min_confidence: float = 0.5,
    ) -> list[OCRResult]:
        """
        Read all text visible on screen using EasyOCR.

        Args:
            screenshot:     PIL Image from bot.screenshot()
            region:         (x1, y1, x2, y2) to only read text in a specific area
                            e.g. just the top bar where resources are displayed
            min_confidence: filter out low-confidence readings

        Returns:
            List of OCRResult with text and position of each detected string.

        Example:
            texts = vision.read_text(screenshot, region=(0, 0, 400, 100))
            for t in texts:
                print(f"'{t.text}' at ({t.x}, {t.y})")
        """
        self._ensure_ocr()

        norm_screen, sx, sy = self._normalize_screenshot(screenshot)
        img = norm_screen
        offset_x, offset_y = 0, 0

        if region:
            # Scale region to canonical space
            rx1, ry1, rx2, ry2 = (int(region[0]/sx), int(region[1]/sy),
                                   int(region[2]/sx), int(region[3]/sy))
            img = norm_screen.crop((rx1, ry1, rx2, ry2))
            offset_x, offset_y = rx1, ry1

        img_np = np.array(img)
        raw_results = self._ocr_reader.readtext(img_np)

        ocr_results = []
        for (bbox, text, conf) in raw_results:
            if conf < min_confidence:
                continue
            # bbox is [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            # Scale coordinates back to original screenshot space
            cx = int((sum(xs) / 4 + offset_x) * sx)
            cy = int((sum(ys) / 4 + offset_y) * sy)
            x1r = int((min(xs) + offset_x) * sx)
            y1r = int((min(ys) + offset_y) * sy)
            x2r = int((max(xs) + offset_x) * sx)
            y2r = int((max(ys) + offset_y) * sy)

            ocr_results.append(OCRResult(
                text=text.strip(),
                x=cx, y=cy,
                confidence=conf,
                region=(x1r, y1r, x2r, y2r),
            ))

        return ocr_results

    def find_text(
        self,
        screenshot: Image.Image,
        search_text: str,
        region: Optional[tuple] = None,
        case_sensitive: bool = False,
    ) -> Optional[OCRResult]:
        """
        Find a specific piece of text on screen and return its position.
        Returns the first match, or None if not found.

        Example:
            result = vision.find_text(screenshot, "COLLECT")
            if result:
                bot.tap(result.x, result.y)
        """
        results = self.read_text(screenshot, region=region)
        for r in results:
            a = r.text if case_sensitive else r.text.lower()
            b = search_text if case_sensitive else search_text.lower()
            if b in a:
                return r
        return None

    def read_number(
        self,
        screenshot: Image.Image,
        region: tuple,
    ) -> Optional[int]:
        """
        Read a single number from a specific screen region.
        Useful for reading resource counts, timers, health values, etc.

        Args:
            region: (x1, y1, x2, y2) — tightly crop around the number

        Returns:
            Integer value, or None if no number could be read.

        Example:
            gold = vision.read_number(screenshot, region=(120, 45, 280, 75))
            print(f"Current gold: {gold}")
        """
        results = self.read_text(screenshot, region=region)
        for r in results:
            cleaned = "".join(c for c in r.text if c.isdigit())
            if cleaned:
                return int(cleaned)
        return None

    # ------------------------------------------------------------------
    # Screen State Detection
    # ------------------------------------------------------------------

    def detect_screen(
        self,
        screenshot: Image.Image,
        screens: dict[str, str],
        threshold: Optional[float] = None,
    ) -> Optional[str]:
        """
        Identify which game screen is currently shown by checking
        multiple templates and returning the first match.

        Args:
            screens: dict mapping screen name → template path
                     e.g. {"main_menu": "templates/main_menu.png",
                            "battle":    "templates/battle_hud.png",
                            "loading":   "templates/loading.png"}

        Returns:
            The name of the matched screen, or None if no match.

        Example:
            screen = vision.detect_screen(screenshot, {
                "main_menu": "templates/main_menu.png",
                "in_battle": "templates/battle_hud.png",
            })
            if screen == "main_menu":
                bot.tap(540, 820)  # tap play
        """
        for name, path in screens.items():
            if self.template_exists(screenshot, path, threshold):
                print(f"[Vision] Detected screen: {name}")
                return name
        return None

    # ------------------------------------------------------------------
    # Debug / Visualization
    # ------------------------------------------------------------------

    def debug_screenshot(
        self,
        screenshot: Image.Image,
        matches: list[MatchResult] = None,
        ocr_results: list[OCRResult] = None,
        save_path: str = "debug_vision.png",
    ):
        """
        Save an annotated screenshot showing all matches and OCR results.
        Use this while building your bot to verify detections are correct.

        Example:
            result = vision.find_template(screen, "templates/play_btn.png")
            vision.debug_screenshot(screen, matches=[result], save_path="debug.png")
        """
        img = self._pil_to_cv(screenshot)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        # Draw template match boxes in green
        if matches:
            for m in matches:
                if m.found and m.region:
                    x1, y1, x2, y2 = m.region
                    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    cv2.putText(img, f"{m.confidence:.2f}",
                                (x1, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX,
                                0.5, (0, 255, 0), 1)

        # Draw OCR boxes in blue
        if ocr_results:
            for r in ocr_results:
                if r.region:
                    x1, y1, x2, y2 = r.region
                    cv2.rectangle(img, (x1, y1), (x2, y2), (255, 100, 0), 2)
                    cv2.putText(img, r.text,
                                (x1, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX,
                                0.5, (255, 100, 0), 1)

        cv2.imwrite(save_path, img)
        print(f"[Vision] Debug image saved → {save_path}")

    # ------------------------------------------------------------------
    # Template Management
    # ------------------------------------------------------------------

    def preload_templates(self, template_dir: str):
        """
        Pre-load all PNG templates from a directory into memory.
        Call this at startup to avoid disk reads during bot execution.

        Example:
            vision.preload_templates("C:/bot/templates")
        """
        path = Path(template_dir)
        count = 0
        for f in path.glob("*.png"):
            self._load_template(str(f))
            count += 1
        print(f"[Vision] Preloaded {count} templates from {template_dir}")

    def capture_template(
        self,
        screenshot: Image.Image,
        region: tuple,
        save_path: str,
    ):
        """
        Crop a region from a screenshot and save it as a template.
        Use this to create your template library without external tools.

        Args:
            region:    (x1, y1, x2, y2) — the area to crop
            save_path: where to save the template PNG

        Example:
            # Take a screenshot, then crop out the play button as a template
            screen = bot.screenshot()
            vision.capture_template(screen, (480, 800, 600, 840), "templates/play_btn.png")
        """
        x1, y1, x2, y2 = region
        cropped = screenshot.crop((x1, y1, x2, y2))
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        cropped.save(save_path)
        print(f"[Vision] Template saved → {save_path} ({x2-x1}x{y2-y1}px)")

    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

    def _normalize_screenshot(self, screenshot: Image.Image):
        """
        Scale screenshot to canonical_size if needed.
        Returns (scaled_image, scale_x, scale_y) where scale factors
        convert canonical coords back to original screenshot coords.
        """
        w, h = screenshot.size
        cw, ch = self.canonical_size
        if (w, h) == (cw, ch):
            return screenshot, 1.0, 1.0
        if not self._logged_scale:
            print(f"[Vision] Auto-scaling screenshots {w}x{h} → {cw}x{ch} for template matching")
            self._logged_scale = True
        scaled = screenshot.resize((cw, ch), Image.LANCZOS)
        return scaled, w / cw, h / ch

    def _pil_to_cv(self, img: Image.Image) -> np.ndarray:
        """Convert PIL Image to OpenCV numpy array (RGB)."""
        return np.array(img.convert("RGB"))

    def _load_template(self, path: str) -> Optional[np.ndarray]:
        """Load and cache a template image."""
        if path in self.templates:
            return self.templates[path]
        p = Path(path)
        if not p.exists():
            return None
        img = cv2.imread(str(p))
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        self.templates[path] = img_rgb
        return img_rgb

    def _ensure_ocr(self):
        """Lazy-load EasyOCR on first use (takes a few seconds)."""
        if self._ocr_reader is None:
            import ssl as _ssl
            from paths import ensure_app_dir

            print("[Vision] Loading OCR engine (first use may take ~10 seconds)...")

            # On some Windows installs Python's bundled CA bundle is missing or
            # stale, causing EasyOCR model downloads to fail with SSL errors.
            # Patch the default HTTPS context to use the Windows system cert
            # store before init, then restore it afterwards.
            _orig = _ssl._create_default_https_context
            try:
                _ctx = _ssl.create_default_context()
                _ctx.load_default_certs()
                _ssl._create_default_https_context = lambda: _ctx
            except Exception:
                pass

            try:
                # Store models in the app's persistent data directory so they
                # survive reinstalls and don't need to be re-downloaded each time.
                model_dir = str(ensure_app_dir() / "models")
                self._ocr_reader = easyocr.Reader(["en"], gpu=False,
                                                   model_storage_directory=model_dir)
            finally:
                try:
                    _ssl._create_default_https_context = _orig
                except Exception:
                    pass

            print("[Vision] OCR engine ready")

    def _suppress_duplicates(
        self, matches: list[MatchResult], min_distance: int = 20
    ) -> list[MatchResult]:
        """Remove overlapping matches, keeping the highest confidence one."""
        if not matches:
            return []
        kept = []
        for m in sorted(matches, key=lambda x: x.confidence, reverse=True):
            too_close = False
            for k in kept:
                dist = ((m.x - k.x) ** 2 + (m.y - k.y) ** 2) ** 0.5
                if dist < min_distance:
                    too_close = True
                    break
            if not too_close:
                kept.append(m)
        return kept


# ---------------------------------------------------------------------------
# Quick Test / Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import os

    print("=" * 50)
    print("Vision Layer - Test")
    print("=" * 50)

    # -- Test 1: Load a screenshot and run template matching
    screenshot_path = "test_screenshot.png"

    if not os.path.exists(screenshot_path):
        print(f"\n[!] No screenshot found at '{screenshot_path}'")
        print("    Run adb_wrapper.py first to generate one.")
        sys.exit(1)

    print(f"\n[1] Loading screenshot: {screenshot_path}")
    screenshot = Image.open(screenshot_path)
    print(f"    Size: {screenshot.size}")

    vision = VisionEngine(confidence_threshold=0.8)

    # -- Test 2: OCR — read all text on screen
    print("\n[2] Running OCR on screenshot...")
    texts = vision.read_text(screenshot)
    if texts:
        print(f"    Found {len(texts)} text region(s):")
        for t in texts[:10]:  # show first 10
            print(f"    '{t.text}' at ({t.x}, {t.y}) [conf: {t.confidence:.2f}]")
    else:
        print("    No text detected (screen may be a loading screen or all graphics)")

    # -- Test 3: Template capture utility
    print("\n[3] Template capture utility test...")
    os.makedirs("templates", exist_ok=True)
    # Crop the top-left 200x100 area as an example template
    vision.capture_template(screenshot, (0, 0, 200, 100), "templates/sample_region.png")
    print("    Saved sample region as templates/sample_region.png")

    # -- Test 4: Find that template back in the screenshot
    print("\n[4] Finding the sample template back in the screenshot...")
    result = vision.find_template(screenshot, "templates/sample_region.png", threshold=0.95)
    if result:
        print(f"    Found at ({result.x}, {result.y}) [conf: {result.confidence:.2f}]")
    else:
        print("    Not found (unexpected)")

    # -- Test 5: Debug visualization
    print("\n[5] Saving debug visualization...")
    vision.debug_screenshot(
        screenshot,
        matches=[result],
        ocr_results=texts[:5],
        save_path="debug_vision.png"
    )

    print("\nDone. Check debug_vision.png to see annotated detections.")
    print("\nNext steps:")
    print("  1. Create a 'templates/' folder in C:\\bot\\")
    print("  2. Use vision.capture_template() to save buttons from your game")
    print("  3. Use vision.find_template() to locate them during bot execution")
