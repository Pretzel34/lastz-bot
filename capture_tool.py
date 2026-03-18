"""
capture_tool.py
===============
Enhanced template capture and action recorder.

Features:
  - Screenshot viewer with drag-to-capture regions
  - Click a captured template → adds it to the action sequence
  - Drag-to-reorder actions in the sequence list
  - In-tool test runner — run your saved JSON without leaving the tool
  - Save/load sequences as JSON

Requirements:
    pip install customtkinter pillow opencv-python
"""

import json
import time
import threading
import tkinter as tk
from tkinter import simpledialog, messagebox
from pathlib import Path
from PIL import Image, ImageTk
import customtkinter as ctk

try:
    from adb_wrapper import ADBWrapper
    ADB_AVAILABLE = True
except ImportError:
    ADB_AVAILABLE = False

try:
    from vision import VisionEngine
    from action_executor import ActionExecutor, ActionStatus
    EXECUTOR_AVAILABLE = True
except ImportError:
    EXECUTOR_AVAILABLE = False

try:
    from recording_utils import ScreenRecorder, generate_comparison
    RECORDING_AVAILABLE = True
except ImportError:
    RECORDING_AVAILABLE = False

ctk.set_appearance_mode("dark")

C = {
    "bg":        "#0d0f12",
    "panel":     "#161b22",
    "panel2":    "#1c2128",
    "card":      "#21262d",
    "card_sel":  "#2d333b",
    "border":    "#30363d",
    "accent":    "#f0a500",
    "accent_dim":"#3d2900",
    "green":     "#3fb950",
    "green_dim": "#0d2d1a",
    "red":       "#f85149",
    "red_dim":   "#3d1210",
    "yellow":    "#d29922",
    "blue":      "#58a6ff",
    "text":      "#e6edf3",
    "text2":     "#adbac7",
    "text3":     "#768390",
    "muted":     "#444c56",
}

FB  = ("Segoe UI Semibold", 11)
FM  = ("Consolas", 10)
FS  = ("Segoe UI", 10)
FSM = ("Consolas", 9)
FSB = ("Segoe UI Semibold", 9)


class CaptureTool(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("Last Z Bot — Capture Tool")
        self.geometry("1280x820")
        self.minsize(1100, 700)
        self.configure(fg_color=C["bg"])

        # Connection
        self.bot: ADBWrapper = None
        self.port = 21503

        # Screenshot state
        self.screenshot: Image.Image = None
        self.display_img = None
        self.scale_x = 1.0
        self.scale_y = 1.0
        self._img_x = 0
        self._img_y = 0
        self._auto_refresh = False

        # Selection
        self._sel_start = None
        self._sel_rect  = None
        self._selecting = False
        self._suppress_tap = False  # True while waiting for a fallback zone click

        # Sequence
        self.recorded_actions: list[dict] = []
        self._drag_source: int = None   # index being dragged
        self._drag_target: int = None

        # Recording
        self._recording = False
        self.recorder: "ScreenRecorder | None" = None

        # Test runner stop flag
        self._stop_test = False

        # Paths
        self.template_dir = Path("templates")
        self.template_dir.mkdir(exist_ok=True)
        Path("tasks").mkdir(exist_ok=True)

        self._build_ui()
        threading.Thread(target=self._connect, daemon=True).start()

    # ══════════════════════════════════════════════════════════════════════
    # UI
    # ══════════════════════════════════════════════════════════════════════

    def _build_ui(self):
        self._build_topbar()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)

        self._build_screenshot_panel(body)
        self._build_right_panel(body)

    def _build_topbar(self):
        top = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=0, height=50)
        top.pack(fill="x")
        top.pack_propagate(False)

        ctk.CTkLabel(top, text="⚡ Capture Tool",
                     font=("Segoe UI Black", 13),
                     text_color=C["accent"]).pack(side="left", padx=16, pady=12)

        ctk.CTkLabel(top, text="Port:", font=FS,
                     text_color=C["text2"]).pack(side="left", padx=(20, 4))
        self.port_entry = ctk.CTkEntry(top, width=75, font=FM,
                                        fg_color=C["panel2"],
                                        border_color=C["border"],
                                        text_color=C["text"])
        self.port_entry.insert(0, "21503")
        self.port_entry.pack(side="left", pady=12)

        self._topbtn(top, "CONNECT",      self._connect,             C["blue"])
        self._topbtn(top, "📷 SCREENSHOT", self._take_screenshot,     C["accent"])
        self._topbtn(top, "⟳ AUTO",        self._toggle_auto_refresh, C["panel2"])

        ctk.CTkFrame(top, width=1, fg_color=C["border"]).pack(
            side="left", fill="y", padx=12, pady=10)

        self._topbtn(top, "📂 LOAD JSON",  self._load_sequence,  C["panel2"])
        self._topbtn(top, "💾 SAVE JSON",  self._save_sequence,  C["panel2"])

        self.run_btn = ctk.CTkButton(
            top, text="▶ RUN TEST", command=self._run_test,
            font=FSB, height=30, corner_radius=4,
            fg_color=C["green"], hover_color=C["border"],
            text_color=C["bg"], width=90,
        )
        self.run_btn.pack(side="left", padx=3, pady=10)

        self.stop_btn = ctk.CTkButton(
            top, text="■ STOP", command=self._stop_test_run,
            font=FSB, height=30, corner_radius=4,
            fg_color=C["red_dim"], hover_color=C["border"],
            text_color=C["red"], width=70,
        )
        self.stop_btn.pack(side="left", padx=3, pady=10)

        ctk.CTkFrame(top, width=1, fg_color=C["border"]).pack(
            side="left", fill="y", padx=8, pady=10)

        self.rec_btn = ctk.CTkButton(
            top, text="⏺ REC", command=self._toggle_recording,
            font=FSB, height=30, corner_radius=4,
            fg_color=C["panel2"], hover_color=C["border"],
            text_color=C["text"], width=72,
        )
        self.rec_btn.pack(side="left", padx=3, pady=10)

        self.rec_indicator = ctk.CTkLabel(top, text="", font=FSB,
                                           text_color=C["red"])
        self.rec_indicator.pack(side="left", padx=(0, 4))

        self._topbtn(top, "🔍 COMPARE", self._open_compare, C["panel2"])

        self.status_lbl = ctk.CTkLabel(top, text="● Disconnected",
                                        font=FM, text_color=C["text3"])
        self.status_lbl.pack(side="left", padx=16)

        # Run status on right
        self.run_status = ctk.CTkLabel(top, text="",
                                        font=FM, text_color=C["text3"])
        self.run_status.pack(side="right", padx=16)

    def _topbtn(self, parent, text, cmd, color=None):
        return ctk.CTkButton(
            parent, text=text, command=cmd,
            font=FSB, height=30, corner_radius=4,
            fg_color=color or C["panel2"],
            hover_color=C["border"],
            text_color=C["bg"] if color in (C["blue"], C["accent"], C["green"]) else C["text"],
        ).pack(side="left", padx=3, pady=10)

    # ── Screenshot Panel ──────────────────────────────────────────────────

    def _build_screenshot_panel(self, parent):
        left = ctk.CTkFrame(parent, fg_color=C["panel2"], corner_radius=0)
        left.pack(side="left", fill="both", expand=True)

        info = ctk.CTkFrame(left, fg_color="transparent", height=26)
        info.pack(fill="x", padx=8, pady=(4, 0))
        info.pack_propagate(False)

        ctk.CTkLabel(info,
            text="LEFT DRAG = capture template region   |   RIGHT CLICK = record tap",
            font=FSM, text_color=C["text3"]
        ).pack(side="left")

        self.coord_lbl = ctk.CTkLabel(info, text="x: —  y: —",
                                       font=FSM, text_color=C["text3"])
        self.coord_lbl.pack(side="right")

        self.canvas = tk.Canvas(left, bg="#0a0c0f", cursor="crosshair",
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=4, pady=4)

        self.canvas.bind("<ButtonPress-1>",  self._on_press)
        self.canvas.bind("<B1-Motion>",       self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>",        self._on_right_click)
        self.canvas.bind("<Motion>",          self._on_mouse_move)

    # ── Right Panel ───────────────────────────────────────────────────────

    def _build_right_panel(self, parent):
        right = ctk.CTkFrame(parent, fg_color=C["panel"],
                              corner_radius=0, width=440)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        # ── Templates section
        tmpl_hdr = ctk.CTkFrame(right, fg_color="transparent", height=32)
        tmpl_hdr.pack(fill="x", padx=12, pady=(12, 0))
        tmpl_hdr.pack_propagate(False)

        ctk.CTkLabel(tmpl_hdr, text="CAPTURED TEMPLATES",
                     font=("Segoe UI Black", 10),
                     text_color=C["accent"]).pack(side="left")
        ctk.CTkLabel(tmpl_hdr, text="click to add to sequence",
                     font=FSM, text_color=C["text3"]).pack(side="right")

        self.tmpl_scroll = ctk.CTkScrollableFrame(
            right, fg_color=C["panel2"], corner_radius=6, height=200)
        self.tmpl_scroll.pack(fill="x", padx=12, pady=4)
        self._refresh_template_list()

        # Divider
        ctk.CTkFrame(right, height=1, fg_color=C["border"]).pack(
            fill="x", padx=12, pady=6)

        # ── Sequence section
        seq_hdr = ctk.CTkFrame(right, fg_color="transparent", height=32)
        seq_hdr.pack(fill="x", padx=12, pady=(0, 4))
        seq_hdr.pack_propagate(False)

        ctk.CTkLabel(seq_hdr, text="RECORDED ACTIONS",
                     font=("Segoe UI Black", 10),
                     text_color=C["accent"]).pack(side="left")
        ctk.CTkLabel(seq_hdr, text="drag ⠿ to reorder",
                     font=FSM, text_color=C["text3"]).pack(side="right")

        # Quick-add buttons split into two rows
        # Quick-add buttons — grid layout, 3 per row, uniform width
        all_btns = [
            ("＋ Wait",       self._add_wait,         C["card"],      C["text"]),
            ("＋ Back",       self._add_back,         C["card"],      C["text"]),
            ("＋ Center HQ",  self._add_center_view,  C["card"],      C["text"]),
            ("＋ Zoom Out",   self._add_zoom_out,     C["card"],      C["text"]),
            ("＋ Scroll",     self._add_scroll,       C["card"],      C["text"]),
            ("＋ Dismiss",    self._add_dismiss_menu, C["card"],      C["text"]),
            ("＋ Tap Zone",   self._add_tap_zone,     C["card"],      C["text"]),
            ("✓ Ensure HQ",  self._add_ensure_hq,    C["green_dim"], C["green"]),
            ("🗑 Clear All",  self._clear_sequence,   C["card"],      C["red"]),
        ]

        btn_grid = ctk.CTkFrame(right, fg_color="transparent")
        btn_grid.pack(fill="x", padx=12, pady=(0, 6))

        # 3 columns, auto rows
        cols = 3
        for i, (label, cmd, fg, tc) in enumerate(all_btns):
            btn = ctk.CTkButton(
                btn_grid, text=label, command=cmd,
                font=FSB, height=30, corner_radius=4,
                fg_color=fg, text_color=tc,
                hover_color=C["border"],
                border_width=1, border_color=C["border"]
            )
            btn.grid(row=i // cols, column=i % cols,
                     padx=2, pady=2, sticky="ew")

        for c in range(cols):
            btn_grid.grid_columnconfigure(c, weight=1)

        # Sequence list
        self.seq_scroll = ctk.CTkScrollableFrame(
            right, fg_color=C["panel2"], corner_radius=6)
        self.seq_scroll.pack(fill="both", expand=True, padx=12, pady=4)

        # Save bar
        save_bar = ctk.CTkFrame(right, fg_color="transparent")
        save_bar.pack(fill="x", padx=12, pady=(4, 12))

        self.seq_name_entry = ctk.CTkEntry(
            save_bar, placeholder_text="Sequence name...",
            font=FM, fg_color=C["panel2"],
            border_color=C["border"], text_color=C["text"])
        self.seq_name_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        ctk.CTkButton(save_bar, text="💾 SAVE", font=FB, width=80,
                      fg_color=C["accent"], text_color=C["bg"],
                      hover_color=C["border"], corner_radius=4,
                      command=self._save_sequence).pack(side="right")

        # Test log
        ctk.CTkFrame(right, height=1, fg_color=C["border"]).pack(
            fill="x", padx=12, pady=(0, 4))

        ctk.CTkLabel(right, text="TEST OUTPUT",
                     font=("Segoe UI Black", 10),
                     text_color=C["accent"]).pack(anchor="w", padx=12)

        log_bg = ctk.CTkFrame(right, fg_color=C["panel2"], corner_radius=6)
        log_bg.pack(fill="x", padx=12, pady=(4, 8))

        self.test_log = tk.Text(
            log_bg, font=FSM, height=7,
            bg=C["panel2"], fg=C["text3"],
            relief="flat", bd=0, wrap="word",
            state="disabled", padx=6, pady=4
        )
        self.test_log.pack(fill="x", padx=2, pady=2)
        self.test_log.tag_config("ok",   foreground=C["green"])
        self.test_log.tag_config("fail", foreground=C["red"])
        self.test_log.tag_config("info", foreground=C["text3"])
        self.test_log.tag_config("warn", foreground=C["yellow"])

    # ══════════════════════════════════════════════════════════════════════
    # Connection
    # ══════════════════════════════════════════════════════════════════════

    def _connect(self):
        try:
            port = int(self.port_entry.get())
        except ValueError:
            port = 21503
        self.port = port

        if not ADB_AVAILABLE:
            self._set_status("● adb_wrapper.py not found", C["red"])
            return

        self._set_status("● Connecting...", C["yellow"])
        bot = ADBWrapper(port=port)
        if bot.connect():
            self.bot = bot
            self._set_status(
                f"● {bot.info.model}  {bot.info.screen_width}×{bot.info.screen_height}",
                C["green"])
            self.after(0, self._take_screenshot)
        else:
            self._set_status("● Connection failed", C["red"])

    def _set_status(self, text, color):
        self.after(0, lambda: self.status_lbl.configure(text=text, text_color=color))

    # ══════════════════════════════════════════════════════════════════════
    # Screenshot
    # ══════════════════════════════════════════════════════════════════════

    def _take_screenshot(self):
        if not self.bot:
            messagebox.showwarning("Not Connected", "Connect to emulator first.")
            return
        threading.Thread(target=self._do_screenshot, daemon=True).start()

    def _do_screenshot(self):
        try:
            img = self.bot.screenshot()
            self.screenshot = img
            self.after(0, self._display_screenshot)
        except Exception as e:
            self.after(0, lambda: self._tlog(f"Screenshot error: {e}", "fail"))

    def _display_screenshot(self):
        if not self.screenshot:
            return
        cw = self.canvas.winfo_width()  or 700
        ch = self.canvas.winfo_height() or 700
        iw, ih = self.screenshot.size
        scale = min(cw / iw, ch / ih)
        dw = int(iw * scale)
        dh = int(ih * scale)
        self.scale_x = iw / dw
        self.scale_y = ih / dh
        self._img_x  = (cw - dw) // 2
        self._img_y  = (ch - dh) // 2

        disp = self.screenshot.resize((dw, dh), Image.LANCZOS)
        self.display_img = ImageTk.PhotoImage(disp)
        self.canvas.delete("screenshot")
        self.canvas.create_image(self._img_x, self._img_y,
                                  anchor="nw", image=self.display_img,
                                  tags="screenshot")
        # Keep overlays on top
        self.canvas.tag_raise("capture")
        self.canvas.tag_raise("tap_dot")

    def _toggle_auto_refresh(self):
        self._auto_refresh = not self._auto_refresh
        if self._auto_refresh:
            self._tlog("Auto-refresh ON (every 2s)", "info")
            self._do_auto_refresh()
        else:
            self._tlog("Auto-refresh OFF", "info")

    def _do_auto_refresh(self):
        if self._auto_refresh and self.bot:
            self._do_screenshot()
            self.after(2000, self._do_auto_refresh)

    # ══════════════════════════════════════════════════════════════════════
    # Mouse / Selection
    # ══════════════════════════════════════════════════════════════════════

    def _on_mouse_move(self, event):
        if self.screenshot:
            gx, gy = self._to_game(event.x, event.y)
            self.coord_lbl.configure(text=f"x: {gx}  y: {gy}")

    def _on_press(self, event):
        self._sel_start = (event.x, event.y)
        self._selecting = True
        if self._sel_rect:
            self.canvas.delete(self._sel_rect)
            self._sel_rect = None

    def _on_drag(self, event):
        if not self._selecting or not self._sel_start:
            return
        if self._sel_rect:
            self.canvas.delete(self._sel_rect)
        x1, y1 = self._sel_start
        self._sel_rect = self.canvas.create_rectangle(
            x1, y1, event.x, event.y,
            outline=C["accent"], width=2, dash=(4, 4), tags="sel"
        )

    def _on_release(self, event):
        if not self._selecting or not self._sel_start:
            return
        self._selecting = False
        x1, y1 = self._sel_start
        x2, y2 = event.x, event.y
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)

        if self._sel_rect:
            self.canvas.delete(self._sel_rect)
            self._sel_rect = None

        # Too small = tap
        if abs(x2 - x1) < 8 or abs(y2 - y1) < 8:
            if self._suppress_tap:
                return
            gx, gy = self._to_game(event.x, event.y)
            self._record_tap_prompt(gx, gy)
            return

        gx1, gy1 = self._to_game(x1, y1)
        gx2, gy2 = self._to_game(x2, y2)
        self._capture_region(gx1, gy1, gx2, gy2, cx1=x1, cy1=y1, cx2=x2, cy2=y2)

    def _on_right_click(self, event):
        if not self.screenshot:
            return
        gx, gy = self._to_game(event.x, event.y)
        self._record_tap_prompt(gx, gy)

    def _to_game(self, cx, cy):
        gx = int((cx - self._img_x) * self.scale_x)
        gy = int((cy - self._img_y) * self.scale_y)
        if self.screenshot:
            gx = max(0, min(gx, self.screenshot.width  - 1))
            gy = max(0, min(gy, self.screenshot.height - 1))
        return gx, gy

    # ══════════════════════════════════════════════════════════════════════
    # Template Capture
    # ══════════════════════════════════════════════════════════════════════

    def _capture_region(self, gx1, gy1, gx2, gy2, cx1=0, cy1=0, cx2=0, cy2=0):
        if not self.screenshot:
            return

        name = simpledialog.askstring(
            "Save Template",
            f"Name this template  ({gx1},{gy1}) → ({gx2},{gy2}):",
            initialvalue="btn_"
        )
        if not name:
            return
        if not name.endswith(".png"):
            name += ".png"

        save_path = self.template_dir / name
        if save_path.exists():
            if not messagebox.askyesno(
                "Template Already Exists",
                f"'{name}' already exists in templates.\nOverwrite it?"
            ):
                return

        cropped = self.screenshot.crop((gx1, gy1, gx2, gy2))
        cropped.save(save_path)

        # Draw green box on canvas
        self.canvas.create_rectangle(
            cx1 + self._img_x - int((cx1 - self._img_x) * 0),
            cy1,
            cx2 + (self._img_x - int((cx2) * 0)),
            cy2,
            outline=C["green"], width=2, tags="capture"
        )
        # Simpler: just use the passed canvas coords directly
        self.canvas.delete("sel")
        self.canvas.create_rectangle(
            cx1 + self._img_x - cx1 + cx1,
            cy1,
            cx2,
            cy2,
            outline=C["green"], width=2, tags="capture"
        )
        self.canvas.create_text(
            cx1 + 3, cy1 + 3,
            text=name.replace(".png", ""),
            fill=C["green"], font=("Consolas", 8),
            anchor="nw", tags="capture"
        )

        self._tlog(f"✓ Saved: {name}  ({gx2-gx1}×{gy2-gy1}px)", "ok")
        self._refresh_template_list()

        # Ask to add to sequence — use topmost dialog
        dialog2 = ctk.CTkToplevel(self)
        dialog2.title("Add to Sequence?")
        dialog2.geometry("300x130")
        dialog2.configure(fg_color=C["panel"])
        dialog2.attributes("-topmost", True)
        dialog2.grab_set()
        dialog2.resizable(False, False)
        ctk.CTkLabel(dialog2, text=f"Add tap_template: {name} to the sequence?",
                     font=FB, text_color=C["text"]).pack(pady=(16, 10), padx=16)
        result2 = {"ok": False}
        br2 = ctk.CTkFrame(dialog2, fg_color="transparent")
        br2.pack()
        def yes2(r=result2, d=dialog2):
            r["ok"] = True; d.destroy()
        ctk.CTkButton(br2, text="Yes", font=FB, width=90,
                      fg_color=C["accent"], text_color=C["bg"],
                      hover_color=C["border"], corner_radius=4,
                      command=yes2).pack(side="left", padx=6)
        ctk.CTkButton(br2, text="No", font=FB, width=90,
                      fg_color=C["card"], text_color=C["text"],
                      hover_color=C["border"], corner_radius=4,
                      command=dialog2.destroy).pack(side="left", padx=6)
        dialog2.wait_window()
        if result2["ok"]:
            self._add_template_action(name)

    def _record_tap_prompt(self, x: int, y: int):
        """Right-click records a tap_zone using % coordinates for cross-resolution support."""
        w = self.bot.info.screen_width  if self.bot and self.bot.info else 540
        h = self.bot.info.screen_height if self.bot and self.bot.info else 960
        x_pct = round(x / w * 100, 1)
        y_pct = round(y / h * 100, 1)

        dialog = ctk.CTkToplevel(self)
        dialog.title("Record Tap Zone")
        dialog.geometry("300x200")
        dialog.configure(fg_color=C["panel"])
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.resizable(False, False)

        ctk.CTkLabel(dialog, text="Record Tap Zone",
                     font=FB, text_color=C["accent"]).pack(pady=(14, 2), padx=16)
        ctk.CTkLabel(dialog,
            text=f"Position: ({x}, {y})  →  {x_pct}% × {y_pct}%   Stored as % for all screen sizes.",
            font=FS, text_color=C["text3"], justify="center").pack(padx=16, pady=(0,8))

        label_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        label_frame.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(label_frame, text="Label (optional):", font=FS,
                     text_color=C["text2"]).pack(side="left")
        label_entry = ctk.CTkEntry(label_frame, font=FM, width=140,
                                    fg_color=C["panel2"], border_color=C["border"],
                                    text_color=C["text"])
        label_entry.pack(side="right")

        result = {"ok": False, "note": ""}
        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(pady=(0, 12))

        def yes():
            result["ok"] = True
            result["note"] = label_entry.get().strip()
            dialog.destroy()

        ctk.CTkButton(btn_row, text="✓ Add", font=FB, width=90,
                      fg_color=C["accent"], text_color=C["bg"],
                      hover_color=C["border"], corner_radius=4,
                      command=yes).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text="Cancel", font=FB, width=90,
                      fg_color=C["card"], text_color=C["text"],
                      hover_color=C["border"], corner_radius=4,
                      command=dialog.destroy).pack(side="left", padx=6)

        # Enter key also confirms
        dialog.bind("<Return>", lambda e: yes())

        dialog.wait_window()

        if result["ok"]:
            note = result["note"] or f"tap {x_pct}%,{y_pct}%"
            self.recorded_actions.append({
                "action": "tap_zone",
                "x_pct": x_pct,
                "y_pct": y_pct,
                "note":  note,
            })
            self._refresh_sequence()
            # Draw circle marker on canvas
            cx = int(x / self.scale_x) + self._img_x
            cy = int(y / self.scale_y) + self._img_y
            self.canvas.create_oval(cx-5, cy-5, cx+5, cy+5,
                                     fill=C["accent"], outline="", tags="tap_dot")
            self.canvas.create_text(cx+9, cy,
                text=f"{x_pct}%,{y_pct}%",
                fill=C["accent"], font=("Consolas", 8),
                anchor="w", tags="tap_dot")
            self._tlog(f"+ tap_zone: {x_pct}% × {y_pct}%  [{note}]", "ok")

    # ══════════════════════════════════════════════════════════════════════
    # Template List
    # ══════════════════════════════════════════════════════════════════════

    def _refresh_template_list(self):
        for w in self.tmpl_scroll.winfo_children():
            w.destroy()

        templates = sorted(self.template_dir.glob("*.png"))
        if not templates:
            ctk.CTkLabel(self.tmpl_scroll,
                         text="No templates yet.\nDrag regions on the screenshot.",
                         font=FS, text_color=C["text3"]).pack(pady=10)
            return

        for path in templates:
            row = ctk.CTkFrame(self.tmpl_scroll, fg_color=C["card"],
                                corner_radius=4, cursor="hand2")
            row.pack(fill="x", pady=2)

            # Thumbnail
            try:
                thumb = Image.open(path)
                thumb.thumbnail((28, 28))
                photo = ImageTk.PhotoImage(thumb)
                lbl = tk.Label(row, image=photo, bg=C["card"], cursor="hand2")
                lbl.image = photo
                lbl.pack(side="left", padx=4, pady=3)
            except Exception:
                ctk.CTkLabel(row, text="?", font=FM,
                             text_color=C["text3"], width=28).pack(side="left", padx=4)

            name_lbl = ctk.CTkLabel(row, text=path.name, font=FSM,
                                     text_color=C["text2"], cursor="hand2")
            name_lbl.pack(side="left", padx=4, fill="x", expand=True)

            # Click anywhere on row = add to sequence
            for widget in [row, name_lbl]:
                widget.bind("<Button-1>",
                            lambda e, n=path.name: self._add_template_action(n))
                widget.bind("<Enter>",
                            lambda e, r=row: r.configure(fg_color=C["card_sel"]))
                widget.bind("<Leave>",
                            lambda e, r=row: r.configure(fg_color=C["card"]))

            # Delete button
            ctk.CTkButton(row, text="✕", width=22, height=22,
                          font=FSM, fg_color="transparent",
                          text_color=C["text3"], hover_color=C["red"],
                          command=lambda p=path: self._delete_template(p)
                          ).pack(side="right", padx=4)

    def _delete_template(self, path: Path):
        if messagebox.askyesno("Delete", f"Delete '{path.name}'?"):
            path.unlink()
            self._refresh_template_list()

    # ══════════════════════════════════════════════════════════════════════
    # Action Builders
    # ══════════════════════════════════════════════════════════════════════

    def _add_template_action(self, name: str):
        """Add a tap_template action for the given template filename."""
        # Ask action type
        choice = self._action_type_dialog(name)
        if not choice:
            return

        if choice == "tap_template":
            timeout = simpledialog.askinteger(
                "Timeout", f"Timeout seconds for '{name}':\n(0 = don't wait, just check once)",
                initialvalue=10, minvalue=0, maxvalue=120)
            if timeout is None:
                return
            action = {
                "action": "tap_template",
                "template": name,
                "timeout": timeout,
            }
        elif choice == "wait_for_template":
            timeout = simpledialog.askinteger(
                "Timeout", f"Wait up to how many seconds for '{name}'?",
                initialvalue=15, minvalue=1, maxvalue=120)
            if timeout is None:
                return
            action = {"action": "wait_for_template", "template": name, "timeout": timeout}
        elif choice == "if_template_tap":
            action = {"action": "if_template_tap", "template": name, "required": False}
        elif choice == "repeat_if_template":
            max_taps = simpledialog.askinteger(
                "Max Taps", "Max times to tap?", initialvalue=10, minvalue=1, maxvalue=50)
            action = {"action": "repeat_if_template", "template": name,
                      "max_taps": max_taps or 10, "delay": 0.8, "required": False}
        elif choice == "if_not_found_fallback":
            action = self._build_fallback_action(name)
            if not action:
                return

        self.recorded_actions.append(action)
        self._refresh_sequence()
        self._tlog(f"+ Added: {choice} → {name}", "ok")

    def _action_type_dialog(self, template_name: str) -> str:
        """Show a small dialog to pick which action type to add."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Action Type")
        dialog.geometry("300x300")
        dialog.configure(fg_color=C["panel"])
        dialog.grab_set()
        dialog.resizable(False, False)

        ctk.CTkLabel(dialog, text=f"Add action for:\n{template_name}",
                     font=FB, text_color=C["text"],
                     wraplength=260).pack(pady=(16, 12), padx=16)

        result = {"choice": None}

        options = [
            ("tap_template",        "Tap it (wait for it to appear first)"),
            ("if_template_tap",     "Tap if visible (skip if not — optional)"),
            ("wait_for_template",   "Wait until it appears (don't tap)"),
            ("repeat_if_template",  "Keep tapping until it disappears"),
            ("if_not_found_fallback", "If not found, look for this..."),
        ]

        for value, label in options:
            ctk.CTkButton(
                dialog, text=label, font=FS, height=34,
                fg_color=C["card"], text_color=C["text2"],
                hover_color=C["accent_dim"], corner_radius=4,
                anchor="w",
                command=lambda v=value, d=dialog, r=result: (
                    r.update({"choice": v}), d.destroy()
                )
            ).pack(fill="x", padx=16, pady=2)

        dialog.wait_window()
        return result["choice"]

    def _build_fallback_action(self, primary_name: str) -> dict:
        """
        Ask whether the fallback is a screen zone or another template PNG,
        then build and return the appropriate action dict.
        Returns None if the user cancels.
        """
        # ── Step 1: zone or template? ────────────────────────────────────
        fb_type = {"choice": None}
        dlg = ctk.CTkToplevel(self)
        dlg.title("Fallback Type")
        dlg.geometry("280x150")
        dlg.configure(fg_color=C["panel"])
        dlg.attributes("-topmost", True)
        dlg.grab_set()
        dlg.resizable(False, False)
        ctk.CTkLabel(dlg, text=f"If '{primary_name}' not found,\nfallback to:",
                     font=FB, text_color=C["text"]).pack(pady=(14, 10), padx=16)
        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack()
        for label, val in [("Zone (tap location)", "zone"), ("Template (another PNG)", "template")]:
            ctk.CTkButton(row, text=label, font=FS, width=120, height=30,
                          fg_color=C["card"], text_color=C["text2"],
                          hover_color=C["accent_dim"], corner_radius=4,
                          command=lambda v=val, d=dlg, r=fb_type: (
                              r.update({"choice": v}), d.destroy()
                          )).pack(side="left", padx=6)
        dlg.wait_window()
        if not fb_type["choice"]:
            return None

        # ── Step 2a: zone fallback — click on the screenshot ────────────
        if fb_type["choice"] == "zone":
            coords = {"x_pct": None, "y_pct": None}
            press_pos = {}

            instr = ctk.CTkToplevel(self)
            instr.title("Pick Fallback Location")
            instr.geometry("300x90")
            instr.configure(fg_color=C["panel"])
            instr.attributes("-topmost", True)
            instr.resizable(False, False)
            # No grab_set — canvas must stay clickable
            ctk.CTkLabel(instr, text=f"Click on the screenshot to set\nthe fallback tap location for '{primary_name}'",
                         font=FS, text_color=C["text"]).pack(pady=(12, 6), padx=16)
            ctk.CTkButton(instr, text="Cancel", font=FS, width=80, height=26,
                          fg_color=C["card"], text_color=C["text"],
                          hover_color=C["border"], corner_radius=4,
                          command=instr.destroy).pack()

            self.canvas.configure(cursor="crosshair")
            self._suppress_tap = True

            def _on_press(event):
                press_pos["x"] = event.x
                press_pos["y"] = event.y

            def _on_release(event):
                # Ignore drags — only act on clean clicks
                if abs(event.x - press_pos.get("x", event.x)) > 5 or \
                   abs(event.y - press_pos.get("y", event.y)) > 5:
                    return
                if not instr.winfo_exists():
                    return
                gx, gy = self._to_game(event.x, event.y)
                w_px = self.bot.info.screen_width  if self.bot and self.bot.info else 540
                h_px = self.bot.info.screen_height if self.bot and self.bot.info else 960
                coords["x_pct"] = round(gx / w_px * 100, 1)
                coords["y_pct"] = round(gy / h_px * 100, 1)
                instr.destroy()

            press_id   = self.canvas.bind("<ButtonPress-1>",   _on_press,   add=True)
            release_id = self.canvas.bind("<ButtonRelease-1>", _on_release, add=True)

            instr.wait_window()

            self.canvas.configure(cursor="")
            self._suppress_tap = False
            self.canvas.unbind("<ButtonPress-1>",   press_id)
            self.canvas.unbind("<ButtonRelease-1>", release_id)

            if coords["x_pct"] is None:
                return None

            self._tlog(f"Fallback zone: ({coords['x_pct']}%, {coords['y_pct']}%)", "ok")
            return {
                "action": "tap_template_or_zone",
                "template": primary_name,
                "x_pct": coords["x_pct"],
                "y_pct": coords["y_pct"],
                "required": False,
            }

        # ── Step 2b: template fallback ───────────────────────────────────
        existing = sorted(p.name for p in self.template_dir.glob("*.png"))
        fb_name  = {"value": None}
        dlg2 = ctk.CTkToplevel(self)
        dlg2.title("Fallback Template")
        dlg2.geometry("300x180")
        dlg2.configure(fg_color=C["panel"])
        dlg2.attributes("-topmost", True)
        dlg2.grab_set()
        dlg2.resizable(False, False)
        ctk.CTkLabel(dlg2, text="Select fallback template:", font=FB,
                     text_color=C["text"]).pack(pady=(14, 6), padx=16)
        combo = ctk.CTkComboBox(dlg2, values=existing, width=260, font=FS)
        combo.pack(padx=16, pady=4)
        if existing:
            combo.set(existing[0])
        btn_row = ctk.CTkFrame(dlg2, fg_color="transparent")
        btn_row.pack(pady=10)
        ctk.CTkButton(btn_row, text="OK", font=FB, width=80,
                      fg_color=C["accent"], text_color=C["bg"],
                      hover_color=C["border"], corner_radius=4,
                      command=lambda: (fb_name.update({"value": combo.get()}), dlg2.destroy())
                      ).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text="Cancel", font=FB, width=80,
                      fg_color=C["card"], text_color=C["text"],
                      hover_color=C["border"], corner_radius=4,
                      command=dlg2.destroy).pack(side="left", padx=6)
        dlg2.wait_window()
        if not fb_name["value"]:
            return None
        return {
            "action": "tap_template_or_template",
            "template": primary_name,
            "fallback_template": fb_name["value"],
            "required": False,
        }

    def _add_tap_action(self, x: int, y: int):
        self.recorded_actions.append({"action": "tap", "x": x, "y": y})
        self._refresh_sequence()
        self._tlog(f"+ Added: tap ({x}, {y})", "ok")

    def _add_wait(self):
        secs = simpledialog.askfloat("Wait", "Seconds to wait:",
                                      initialvalue=2.0, minvalue=0.1, maxvalue=60.0)
        if secs:
            self.recorded_actions.append({"action": "wait", "seconds": round(secs, 1)})
            self._refresh_sequence()

    def _add_back(self):
        self.recorded_actions.append({"action": "press_back"})
        self._refresh_sequence()

    def _add_center_view(self):
        """Add center_hq — runs your saved tasks/center_hq.json sequence."""
        self.recorded_actions.append({
            "action": "center_hq",
            "note": "Run tasks/center_hq.json to reset camera"
        })
        self._refresh_sequence()
        self._tlog("+ Added: center_hq  (runs tasks/center_hq.json)", "ok")

    def _add_ensure_hq(self):
        """Add ensure_hq_view — runs before any task to guarantee HQ view."""
        self.recorded_actions.append({
            "action":    "ensure_hq_view",
            "hq_btn":    "btn_go_to_world_view.png",
            "world_btn": "btn_go_to_hq_view.png",
            "note":      "Ensure HQ view before task"
        })
        self._refresh_sequence()
        self._tlog("+ Added: ensure_hq_view", "ok")

    def _add_zoom_out(self):
        """Add zoom_out — uses Ctrl+scroll to zoom out in Last Z."""
        self.recorded_actions.append({
            "action": "zoom_out",
            "steps":  3,
            "note":   "Ctrl+scroll up x3 to zoom out"
        })
        self._refresh_sequence()
        self._tlog("+ Added: zoom_out (Ctrl+scroll x3)", "ok")

    def _add_scroll(self):
        """Add a scroll action with direction picker."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Add Scroll")
        dialog.geometry("320x380")
        dialog.configure(fg_color=C["panel"])
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.resizable(False, False)

        ctk.CTkLabel(dialog, text="Scroll / Pan",
                     font=FB, text_color=C["accent"]).pack(pady=(14, 4), padx=16)

        dir_var = ctk.StringVar(value="scroll_right")
        dir_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        dir_frame.pack(fill="x", padx=16, pady=4)
        for val, lbl in [
            ("scroll_right", "→  Right"),
            ("scroll_left",  "←  Left"),
            ("scroll_up",    "↑  Up"),
            ("scroll_down",  "↓  Down"),
        ]:
            ctk.CTkRadioButton(dir_frame, text=lbl, variable=dir_var, value=val,
                               font=FS, text_color=C["text2"],
                               fg_color=C["accent"]).pack(anchor="w", padx=4, pady=2)

        grid = ctk.CTkFrame(dialog, fg_color="transparent")
        grid.pack(fill="x", padx=16, pady=(4, 0))

        def lbl(parent, text):
            ctk.CTkLabel(parent, text=text, font=FS,
                         text_color=C["text2"], width=90, anchor="w").pack(side="left")

        def entry(parent, default, w=50):
            e = ctk.CTkEntry(parent, width=w, font=FM,
                             fg_color=C["panel2"], border_color=C["border"],
                             text_color=C["text"])
            e.insert(0, str(default))
            e.pack(side="left", padx=(0, 12))
            return e

        r1 = ctk.CTkFrame(grid, fg_color="transparent"); r1.pack(fill="x", pady=2)
        lbl(r1, "Start X %:"); sx_e = entry(r1, 80)
        lbl(r1, "Start Y %:"); sy_e = entry(r1, 88)

        r2 = ctk.CTkFrame(grid, fg_color="transparent"); r2.pack(fill="x", pady=2)
        lbl(r2, "Distance %:"); dist_e = entry(r2, 60)
        lbl(r2, "Steps:");      steps_e = entry(r2, 1, 40)

        ctk.CTkLabel(grid,
            text="Tip: Start Y 80% = bottom of screen. Start X 50% = center.",
            font=FSM, text_color=C["text3"], wraplength=260
        ).pack(anchor="w", pady=(4,0))

        result = {"ok": False, "dir": "scroll_right", "steps": 1, "dist": 60, "sx": 80, "sy": 88}

        def confirm():
            result["dir"] = dir_var.get()
            try: result["steps"] = int(steps_e.get())
            except: result["steps"] = 1
            try: result["dist"]  = int(dist_e.get())
            except: result["dist"]  = 40
            try: result["sx"] = float(sx_e.get())
            except: result["sx"] = 50
            try: result["sy"] = float(sy_e.get())
            except: result["sy"] = 80
            result["ok"] = True
            dialog.destroy()

        btn_bar = ctk.CTkFrame(dialog, fg_color=C["panel2"])
        btn_bar.pack(side="bottom", fill="x")
        ctk.CTkButton(btn_bar, text="Cancel", font=FB, width=90, height=34,
                      fg_color=C["card"], text_color=C["text"],
                      hover_color=C["border"], corner_radius=4,
                      command=dialog.destroy).pack(side="right", padx=(4,12), pady=8)
        ctk.CTkButton(btn_bar, text="＋ Add", font=FB, width=90, height=34,
                      fg_color=C["accent"], text_color=C["bg"],
                      hover_color=C["border"], corner_radius=4,
                      command=confirm).pack(side="right", pady=8)
        dialog.bind("<Return>", lambda e: confirm())
        dialog.wait_window()

        if not result["ok"]:
            return

        dirs = {"scroll_right": "→", "scroll_left": "←", "scroll_up": "↑", "scroll_down": "↓"}
        d = dirs.get(result["dir"], "")
        self.recorded_actions.append({
            "action":       result["dir"],
            "steps":        result["steps"],
            "distance_pct": result["dist"],
            "start_x_pct":  result["sx"],
            "start_y_pct":  result["sy"],
            "note":         f"{d} scroll x{result['steps']} from ({result['sx']}%,{result['sy']}%)"
        })
        self._refresh_sequence()
        self._tlog(f"+ Added: {result['dir']} steps={result['steps']} start=({result['sx']}%,{result['sy']}%) dist={result['dist']}%", "ok")

    def _add_dismiss_menu(self):
        """Tap an empty sky area to dismiss building menus. Uses % for cross-resolution."""
        self.recorded_actions.append({
            "action": "tap_zone",
            "x_pct": 15.0,
            "y_pct": 15.0,
            "note":  "Dismiss menu — tap empty sky area"
        })
        self._refresh_sequence()
        self._tlog("+ Added: dismiss menu (tap_zone 15%,15% — empty sky area)", "ok")

    def _add_tap_zone(self):
        """
        Tap a fixed screen position defined as % of screen width/height.
        Works across all instances regardless of resolution or image content.
        Two ways to define:
          - Draw a box on the screenshot (middle-click drag) → taps center
          - Enter % coordinates manually
        """
        if not self.screenshot:
            messagebox.showwarning("Screenshot Needed",
                                   "Take a screenshot first so you can pick a position.")
            return

        # Show a dialog letting user choose method
        dialog = ctk.CTkToplevel(self)
        dialog.title("Tap Zone")
        dialog.geometry("320x300")
        dialog.configure(fg_color=C["panel"])
        dialog.grab_set()
        dialog.resizable(False, False)

        ctk.CTkLabel(dialog, text="Tap Zone — Fixed Screen Position",
                     font=FB, text_color=C["text"],
                     wraplength=280).pack(pady=(16, 4), padx=16)
        ctk.CTkLabel(dialog,
            text="Uses % of screen size so it works on any resolution or account.",
            font=FS, text_color=C["text3"],
            wraplength=280).pack(pady=(0, 12), padx=16)

        w = self.bot.info.screen_width  if self.bot and self.bot.info else 540
        h = self.bot.info.screen_height if self.bot and self.bot.info else 960

        grid = ctk.CTkFrame(dialog, fg_color="transparent")
        grid.pack(padx=16, fill="x")

        ctk.CTkLabel(grid, text="X % (left→right):", font=FS,
                     text_color=C["text2"]).grid(row=0, column=0, sticky="w", pady=4)
        x_entry = ctk.CTkEntry(grid, width=80, font=FM,
                                fg_color=C["panel2"], border_color=C["border"],
                                text_color=C["text"])
        x_entry.insert(0, "50")
        x_entry.grid(row=0, column=1, padx=8)

        ctk.CTkLabel(grid, text="Y % (top→bottom):", font=FS,
                     text_color=C["text2"]).grid(row=1, column=0, sticky="w", pady=4)
        y_entry = ctk.CTkEntry(grid, width=80, font=FM,
                                fg_color=C["panel2"], border_color=C["border"],
                                text_color=C["text"])
        y_entry.insert(0, "50")
        y_entry.grid(row=1, column=1, padx=8)

        ctk.CTkLabel(grid, text="Label (optional):", font=FS,
                     text_color=C["text2"]).grid(row=2, column=0, sticky="w", pady=4)
        label_entry = ctk.CTkEntry(grid, width=140, font=FM,
                                    fg_color=C["panel2"], border_color=C["border"],
                                    text_color=C["text"], placeholder_text="e.g. 'collect btn'")
        label_entry.grid(row=2, column=1, padx=8)

        result = {"ok": False, "x": None, "y": None, "label": ""}

        def confirm():
            try:
                result["x"] = float(x_entry.get())
                result["y"] = float(y_entry.get())
                result["label"] = label_entry.get().strip()
                result["ok"] = True
            except ValueError:
                messagebox.showerror("Invalid", "Enter numbers between 0 and 100.")
                return
            dialog.destroy()

        ctk.CTkButton(dialog, text="＋ Add to Sequence", font=FB,
                      fg_color=C["accent"], text_color=C["bg"],
                      hover_color=C["border"], corner_radius=4,
                      command=confirm).pack(pady=16, padx=16, fill="x")

        dialog.wait_window()

        if not result["ok"]:
            return

        xpct = max(0.0, min(100.0, result["x"]))
        ypct = max(0.0, min(100.0, result["y"]))
        label = result["label"] or f"zone {xpct:.0f}% {ypct:.0f}%"

        # Convert % to absolute for current screen size (stored both ways)
        abs_x = int(w * xpct / 100)
        abs_y = int(h * ypct / 100)

        self.recorded_actions.append({
            "action":  "tap_zone",
            "x_pct":   xpct,
            "y_pct":   ypct,
            "x":       abs_x,
            "y":       abs_y,
            "note":    label,
        })
        self._refresh_sequence()

        # Draw marker on canvas
        cx = int(abs_x / self.scale_x) + self._img_x
        cy = int(abs_y / self.scale_y) + self._img_y
        self.canvas.create_oval(cx-7, cy-7, cx+7, cy+7,
                                 outline=C["blue"], fill="", width=2,
                                 tags="tap_dot")
        self.canvas.create_text(cx+12, cy, text=f"ZONE {xpct:.0f}%,{ypct:.0f}%",
                                 fill=C["blue"], font=("Consolas", 8),
                                 anchor="w", tags="tap_dot")
        self._tlog(f"+ Added: tap_zone {xpct:.0f}%×{ypct:.0f}% → ({abs_x},{abs_y})  [{label}]", "ok")

    def _clear_sequence(self):
        if self.recorded_actions and messagebox.askyesno("Clear", "Clear all actions?"):
            self.recorded_actions.clear()
            self._refresh_sequence()

    # ══════════════════════════════════════════════════════════════════════
    # Sequence List with Drag-to-Reorder
    # ══════════════════════════════════════════════════════════════════════

    def _refresh_sequence(self):
        for w in self.seq_scroll.winfo_children():
            w.destroy()

        if not self.recorded_actions:
            ctk.CTkLabel(self.seq_scroll,
                         text="No actions yet.\nCapture templates or add actions above.",
                         font=FS, text_color=C["text3"]).pack(pady=16)
            return

        for i, action in enumerate(self.recorded_actions):
            self._seq_row(i, action)

    def _seq_row(self, idx: int, action: dict):
        row = ctk.CTkFrame(self.seq_scroll, fg_color=C["card"],
                            corner_radius=4, border_width=1,
                            border_color=C["border"])
        row.pack(fill="x", pady=2)
        row._idx = idx

        # Drag handle
        handle = ctk.CTkLabel(row, text="⠿", font=("Segoe UI", 14),
                               text_color=C["muted"], cursor="fleur", width=20)
        handle.pack(side="left", padx=(4, 0), pady=6)
        handle.bind("<ButtonPress-1>",   lambda e, i=idx: self._drag_start(i))
        handle.bind("<B1-Motion>",        lambda e, i=idx: self._drag_motion(e, i))
        handle.bind("<ButtonRelease-1>",  lambda e, i=idx: self._drag_end(i))

        # Step number
        ctk.CTkLabel(row,
            text=f"{idx+1:02d}",
            font=("Consolas", 10, "bold"),
            text_color=C["accent"],
            fg_color=C["accent_dim"],
            corner_radius=3, width=24
        ).pack(side="left", padx=(4, 6), pady=6)

        # Buttons MUST pack before summary label so they aren't pushed off screen
        btn_frame = ctk.CTkFrame(row, fg_color="transparent")
        btn_frame.pack(side="right", padx=4)

        ctk.CTkButton(btn_frame, text="↑", width=22, height=22,
                      font=FM, fg_color="transparent",
                      text_color=C["text3"], hover_color=C["border"],
                      command=lambda i=idx: self._move_action(i, -1)
                      ).pack(side="left")
        ctk.CTkButton(btn_frame, text="↓", width=22, height=22,
                      font=FM, fg_color="transparent",
                      text_color=C["text3"], hover_color=C["border"],
                      command=lambda i=idx: self._move_action(i, 1)
                      ).pack(side="left")
        ctk.CTkButton(btn_frame, text="✕", width=22, height=22,
                      font=FSM, fg_color="transparent",
                      text_color=C["text3"], hover_color=C["red"],
                      command=lambda i=idx: self._delete_action(i)
                      ).pack(side="left", padx=(2, 0))

        # Summary label packs last — double-click to edit raw JSON
        summary = self._action_summary(action)
        lbl = ctk.CTkLabel(row, text=summary, font=FSM,
                           text_color=C["text2"],
                           anchor="w", cursor="hand2")
        lbl.pack(side="left", fill="x", expand=True, pady=6)
        lbl.bind("<Double-Button-1>", lambda e, i=idx: self._edit_action(i))
        row.bind("<Double-Button-1>", lambda e, i=idx: self._edit_action(i))

    def _edit_action(self, idx: int):
        """Double-click an action row to edit its raw JSON."""
        if idx < 0 or idx >= len(self.recorded_actions):
            return

        action = self.recorded_actions[idx]
        current_json = json.dumps(action, indent=2)

        dialog = ctk.CTkToplevel(self)
        dialog.title(f"Edit Action {idx + 1}")
        dialog.geometry("440x400")
        dialog.configure(fg_color=C["panel"])
        dialog.attributes("-topmost", True)
        dialog.grab_set()
        dialog.resizable(True, True)

        # Header
        hdr = ctk.CTkFrame(dialog, fg_color="transparent")
        hdr.pack(fill="x", padx=14, pady=(12, 2))
        ctk.CTkLabel(hdr, text=f"Edit Action {idx + 1} — JSON",
                     font=FB, text_color=C["accent"]).pack(side="left")
        ctk.CTkLabel(hdr, text="Ctrl+S to save",
                     font=FSM, text_color=C["text3"]).pack(side="right")

        ctk.CTkLabel(dialog, text="Edit the JSON fields below:",
                     font=FS, text_color=C["text3"]).pack(anchor="w", padx=14)

        # Save/Cancel buttons FIRST so they're never pushed off screen
        btn_row = ctk.CTkFrame(dialog, fg_color=C["panel2"])
        btn_row.pack(fill="x", side="bottom")

        err_lbl = ctk.CTkLabel(btn_row, text="", font=FSM, text_color=C["red"])
        err_lbl.pack(side="left", padx=12, pady=8)

        result = {"saved": False}

        def save():
            raw = editor.get("1.0", "end").strip()
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("Must be a JSON object {}")
                result["saved"] = True
                self.recorded_actions[idx] = parsed
                dialog.destroy()
                self._refresh_sequence()
                self._tlog(f"✓ Action {idx+1} updated", "ok")
            except json.JSONDecodeError as e:
                err_lbl.configure(text=f"JSON error: {e}")
            except ValueError as e:
                err_lbl.configure(text=str(e))

        ctk.CTkButton(btn_row, text="Cancel", font=FB, width=90, height=34,
                      fg_color=C["card"], text_color=C["text"],
                      hover_color=C["border"], corner_radius=4,
                      command=dialog.destroy).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(btn_row, text="💾 Save", font=FB, width=90, height=34,
                      fg_color=C["accent"], text_color=C["bg"],
                      hover_color=C["border"], corner_radius=4,
                      command=save).pack(side="right", pady=8)

        # Text editor fills remaining space
        editor_frame = ctk.CTkFrame(dialog, fg_color=C["panel2"], corner_radius=6)
        editor_frame.pack(fill="both", expand=True, padx=14, pady=6)

        editor = tk.Text(editor_frame, font=FM, bg=C["panel2"], fg=C["text"],
                         relief="flat", bd=0, wrap="none",
                         insertbackground=C["accent"],
                         padx=8, pady=8)
        editor.pack(fill="both", expand=True)
        editor.insert("1.0", current_json)
        editor.focus_set()

        # Ctrl+S to save
        dialog.bind("<Control-s>", lambda e: save())

    def _action_summary(self, action: dict) -> str:
        atype = action.get("action", "")
        if atype == "tap_template":
            t = action.get("timeout", 0)
            return f"tap_template: {action.get('template','')}  [{t}s]"
        elif atype == "if_template_tap":
            return f"if visible → tap: {action.get('template','')}"
        elif atype == "wait_for_template":
            return f"wait for: {action.get('template','')}  [{action.get('timeout','')}s]"
        elif atype == "repeat_if_template":
            return f"repeat tap: {action.get('template','')}  ×{action.get('max_taps','')}"
        elif atype == "tap_zone":
            note = action.get("note", "")
            return f"tap_zone: {action.get('x_pct')}% × {action.get('y_pct')}%  {note}"
        elif atype == "ensure_hq_view":
            return "ensure_hq_view: guarantee HQ view before task"
        elif atype == "center_hq":
            return "center_hq: run tasks/center_hq.json"
        elif atype in ("scroll_right", "scroll_left", "scroll_up", "scroll_down"):
            dirs = {"scroll_right": "→", "scroll_left": "←", "scroll_up": "↑", "scroll_down": "↓"}
            d = dirs.get(atype, "?")
            return f"scroll {d}  steps={action.get('steps',1)}  dist={action.get('distance_pct',40)}%"
        elif atype == "zoom_out":
            s = action.get("steps", 3)
            return f"zoom_out: Ctrl+scroll x{s}"
        elif atype == "tap":
            return f"tap_zone: {action.get('x')}px, {action.get('y')}px  [fixed coords]"
        elif atype == "wait":
            return f"wait: {action.get('seconds')}s"
        elif atype == "press_back":
            return "press_back"
        elif atype == "center_view":
            return "center_view"
        else:
            return atype

    def _move_action(self, idx: int, direction: int):
        new_idx = idx + direction
        if 0 <= new_idx < len(self.recorded_actions):
            actions = self.recorded_actions
            actions[idx], actions[new_idx] = actions[new_idx], actions[idx]
            self._refresh_sequence()

    def _delete_action(self, idx: int):
        if 0 <= idx < len(self.recorded_actions):
            self.recorded_actions.pop(idx)
            self._refresh_sequence()

    # ── Drag reorder ──────────────────────────────────────────────────────

    def _drag_start(self, idx: int):
        self._drag_source = idx

    def _drag_motion(self, event, idx: int):
        # Highlight target position based on mouse Y in scroll frame
        pass  # Visual feedback handled by up/down buttons for simplicity

    def _drag_end(self, idx: int):
        self._drag_source = None

    # ══════════════════════════════════════════════════════════════════════
    # Save / Load
    # ══════════════════════════════════════════════════════════════════════

    def _save_sequence(self):
        name = self.seq_name_entry.get().strip()
        if not name:
            messagebox.showwarning("Name Required", "Enter a sequence name.")
            return
        if not self.recorded_actions:
            messagebox.showwarning("Empty", "No actions to save.")
            return

        path = Path("tasks") / f"{name}.json"
        data = {"name": name, "actions": self.recorded_actions}
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

        self._tlog(f"✓ Saved {len(self.recorded_actions)} actions → {path}", "ok")
        messagebox.showinfo("Saved", f"Saved to:\n{path}")

    def _load_sequence(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Load Sequence",
            initialdir="tasks",
            filetypes=[("JSON", "*.json"), ("All", "*.*")]
        )
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
            actions = data.get("actions", data) if isinstance(data, dict) else data
            self.recorded_actions = actions
            name = data.get("name", Path(path).stem) if isinstance(data, dict) else Path(path).stem
            self.seq_name_entry.delete(0, "end")
            self.seq_name_entry.insert(0, name)
            self._refresh_sequence()
            self._tlog(f"✓ Loaded: {Path(path).name}  ({len(actions)} actions)", "ok")
        except Exception as e:
            messagebox.showerror("Load Error", str(e))

    # ══════════════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════════════
    # Recording
    # ══════════════════════════════════════════════════════════════════════

    def _toggle_recording(self):
        if not RECORDING_AVAILABLE:
            messagebox.showerror("Missing Module",
                                  "recording_utils.py not found.")
            return

        self._recording = not self._recording
        if self._recording:
            self.rec_btn.configure(fg_color=C["red"], text_color=C["bg"])
            self.rec_indicator.configure(text="● REC ON")
        else:
            self.rec_btn.configure(fg_color=C["panel2"], text_color=C["text"])
            self.rec_indicator.configure(text="")
            # Close any active recorder
            if self.recorder:
                self.recorder.close()
                self.recorder = None

    def _open_compare(self):
        """Pick two recording folders and generate side-by-side comparison images."""
        if not RECORDING_AVAILABLE:
            messagebox.showerror("Missing Module",
                                  "recording_utils.py not found.")
            return

        from tkinter import filedialog
        import subprocess

        ref_dir = filedialog.askdirectory(
            title="Select REFERENCE recording folder",
            initialdir="recordings/reference",
        )
        if not ref_dir:
            return

        run_dir = filedialog.askdirectory(
            title="Select ACTUAL RUN recording folder",
            initialdir="recordings/runs",
        )
        if not run_dir:
            return

        try:
            out = generate_comparison(ref_dir, run_dir)
            messagebox.showinfo(
                "Comparison Ready",
                f"Saved {len(list(out.glob('*.png')))} comparison images to:\n{out}",
            )
            # Open output folder in Explorer
            subprocess.Popen(f'explorer "{out}"')
        except Exception as e:
            messagebox.showerror("Compare Failed", str(e))

    # ══════════════════════════════════════════════════════════════════════
    # In-Tool Test Runner
    # ══════════════════════════════════════════════════════════════════════

    def _run_test(self):
        if not self.bot:
            messagebox.showwarning("Not Connected", "Connect to emulator first.")
            return
        if not self.recorded_actions:
            messagebox.showwarning("Empty", "No actions to run.")
            return
        if not EXECUTOR_AVAILABLE:
            messagebox.showerror("Missing Modules",
                                  "vision.py or action_executor.py not found.")
            return

        # Clear log
        self.test_log.configure(state="normal")
        self.test_log.delete("1.0", "end")
        self.test_log.configure(state="disabled")

        self._stop_test = False
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(fg_color=C["red"], text_color=C["bg"])
        self.run_status.configure(text="● RUNNING", text_color=C["yellow"])
        threading.Thread(target=self._do_run_test, daemon=True).start()

    def _do_run_test(self):
        import json as _json
        from datetime import datetime as _dt
        from pathlib import Path as _Path

        log_dir = _Path("logs/tool_tests")
        log_dir.mkdir(parents=True, exist_ok=True)
        ts_str  = _dt.now().strftime("%Y%m%d_%H%M%S")
        log_file = log_dir / f"test_{ts_str}.log"
        log_lines = []

        def tlog_and_save(msg, level="info"):
            ts = time.strftime("%H:%M:%S")
            log_lines.append(f"[{ts}][{level.upper()}] {msg}")
            try:
                self._tlog(msg, level)
            except Exception:
                pass

        stop_event = threading.Event()
        self._test_stop_event = stop_event

        try:
            vision   = VisionEngine(confidence_threshold=0.8)
            executor = ActionExecutor(
                bot=self.bot,
                vision=vision,
                template_dir=str(self.template_dir),
                log_callback=lambda msg: tlog_and_save(msg, "info"),
                stop_event=stop_event,
            )

            actions = self.recorded_actions
            tlog_and_save(f"Running {len(actions)} actions...", "info")

            # Set up recorder for this test run if recording is enabled
            if self._recording and RECORDING_AVAILABLE:
                seq_name = self.seq_name_entry.get().strip() or "sequence"
                self.recorder = ScreenRecorder(
                    name=f"reference_{seq_name}_{ts_str}",
                    kind="reference",
                )
                tlog_and_save(f"Recording to: {self.recorder.out_dir}", "info")
            else:
                self.recorder = None

            for i, action in enumerate(actions):
                if not self._running_test:
                    tlog_and_save("Stopped.", "warn")
                    break

                label = self._action_summary(action)
                tlog_and_save(f"[{i+1}/{len(actions)}] {label}", "info")

                # Capture before
                if self.recorder:
                    try:
                        self.recorder.capture(self.bot, i + 1, label, "before")
                    except Exception:
                        pass

                result = executor.execute(action)

                # Capture after with result status
                if self.recorder:
                    try:
                        self.recorder.capture(self.bot, i + 1, label,
                                               result.status.value.upper())
                    except Exception:
                        pass

                tag = "ok" if result else "fail"
                tlog_and_save(f"  → {result.status.value}: {result.message}", tag)

                # Refresh screenshot after each action
                self.after(0, self._do_screenshot)

                # Stop the test run if the action aborted the task
                if result.status == ActionStatus.ABORT_TASK:
                    tlog_and_save(f"  ⏭ Task aborted — stopping test run", "warn")
                    break

            if self.recorder:
                tlog_and_save(f"Frames saved → {self.recorder.out_dir}", "ok")
                self.recorder.close()
                self.recorder = None

            tlog_and_save("Done.", "ok")
            self.after(0, lambda: self.run_status.configure(
                text="● DONE", text_color=C["green"]))

        except Exception as e:
            tlog_and_save(f"Error: {e}", "fail")
            self.after(0, lambda: self.run_status.configure(
                text="● ERROR", text_color=C["red"]))
        finally:
            self.after(0, self._reset_run_buttons)
            # Save log to file
            try:
                with open(log_file, "w", encoding="utf-8") as f:
                    f.write(f"Tool Test — {ts_str}\n")
                    f.write("=" * 50 + "\n")
                    f.write("\n".join(log_lines))
                self._tlog(f"Log saved → {log_file}", "info")
            except Exception:
                pass

    def _stop_test_run(self):
        self._stop_test = True
        if hasattr(self, "_test_stop_event") and self._test_stop_event:
            self._test_stop_event.set()
        self.stop_btn.configure(fg_color=C["red_dim"], text_color=C["red"])
        self.run_status.configure(text="● STOPPING...", text_color=C["yellow"])

    def _reset_run_buttons(self):
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(fg_color=C["red_dim"], text_color=C["red"])

    @property
    def _running_test(self):
        return not self._stop_test

    def _tlog(self, message: str, level: str = "info"):
        def _do():
            self.test_log.configure(state="normal")
            ts = time.strftime("%H:%M:%S")
            self.test_log.insert("end", f"[{ts}] {message}\n", level)
            self.test_log.see("end")
            self.test_log.configure(state="disabled")
        self.after(0, _do)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = CaptureTool()
    app.mainloop()
