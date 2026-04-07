"""
gui.py
======
Last Z Bot — Command Interface
Redesigned with:
  - Left nav: Run Bot / Bot Settings / Farm Settings / Farm Stats / Restore-Backup
  - Farm Settings: per-instance list with index, name, enable toggle, edit
  - Per-farm config: expandable predefined task sections (Collect, Train, Quests, etc.)
  - Bot Settings: global toggles and emulator config
  - Run Bot: live preview + log + start/stop per instance
  - Farm Stats: session statistics table

Requirements:
    pip install customtkinter pillow
"""

import json
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from pathlib import Path
from datetime import datetime
from PIL import Image, ImageTk
import customtkinter as ctk

import version
from paths import ensure_app_dir, get_farms_path, get_resource_dir
from updater import check_for_update, download_and_launch

# Update these values for your repository.
GITHUB_OWNER = "Pretzel34"
GITHUB_REPO = "lastz-bot"
# The substring to match inside the GitHub release asset name.
# Example: "LastZBot-0.1.0.exe" will be matched by "LastZBot".
UPDATE_ASSET_NAME_CONTAINS = "Setup"

MEMUC_PATH = r"C:\Program Files\Microvirt\MEmu\memuc.exe"
LASTZ_PKG  = "com.readygo.barrel.gp"
LASTZ_ACT  = "com.im30.aps.debug.UnityPlayerActivityCustom"

try:
    from bot_engine import BotEngine, EngineState, load_config, save_config
    from action_executor import ActionStatus
    from launcher import EmulatorLauncher, MEmuLauncher, index_to_port, get_profile, find_emulator_install, emulator_path_example
    BOT_AVAILABLE = True
except ImportError as e:
    BOT_AVAILABLE = False
    IMPORT_ERROR = str(e)

# ── Theme ──────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

C = {
    "bg":         "#0d0f12",
    "nav":        "#13161b",
    "nav_sel":    "#1e2229",
    "panel":      "#161b22",
    "panel2":     "#1c2128",
    "card":       "#21262d",
    "border":     "#30363d",
    "accent":     "#f0a500",
    "accent2":    "#c47f00",
    "accent_dim": "#3d2900",
    "green":      "#3fb950",
    "green_dim":  "#0d3320",
    "red":        "#f85149",
    "red_dim":    "#3d1210",
    "yellow":     "#d29922",
    "blue":       "#58a6ff",
    "text":       "#e6edf3",
    "text2":      "#adbac7",
    "text3":      "#768390",
    "muted":      "#444c56",
    "white":      "#ffffff",
}

FT  = ("Segoe UI",          11)
FB  = ("Segoe UI Semibold", 11)
FM  = ("Consolas",          10)
FS  = ("Segoe UI",           9)
FT2 = ("Segoe UI Black",    12)
FSM = ("Consolas",           9)

# ── Predefined task categories for Last Z ─────────────────────────────────
# Daily task JSON files in tasks/ directory
# Each entry: (key=filename_without_json, label, default_enabled)
DAILY_TASKS = [
    ("collect_idle_reward",  "Collect Idle Reward",  True),
    ("collect_free_rewards", "Collect Free Rewards", True),
    ("collect_vip_rewards",  "Collect VIP Rewards",  True),
    ("collect_radar",        "Collect Radar",        True),
    ("Collect Fuel",         "Collect Fuel",         True),
    ("Read Mail",            "Read Mail",            True),
    ("collect_recruits",     "Collect Recruits",     True),
    ("healing",              "Enable Heal Troops",   False),
]

# Rally task JSON files — (setting_key, label, json_filename)
# setting_key must match the key in the "rally" settings block above
RALLY_TASKS = [
    ("quick_join_rally", "Quick Join Rally", "quick_join_rally"),
    ("create_rally",     "Create Rally",     "start_rally_on_boomer_alt"),
]

# Gathering task JSON files — (setting_key, label, json_filename, default_enabled)
GATHER_TASKS = [
    ("collect_wood",        "Collect Wood",        "gather_wood",  True),
    ("collect_food",        "Collect Food",        "gather_food",  True),
    ("collect_electricity", "Collect Electricity", "gather_power", False),
    ("collect_zents",       "Collect Zents",       "gather_zent",  False),
]

TASK_CATEGORIES = [
    {
        "key":   "daily_tasks",
        "label": "Daily Tasks",
        "icon":  "📋",
        "settings": [
            {"key": "enabled", "label": "Enable All Daily Tasks", "type": "toggle", "default": True},
        ] + [
            {"key": t[0], "label": t[1], "type": "toggle", "default": t[2]}
            for t in DAILY_TASKS
        ]
    },
    {
        "key":   "rally",
        "label": "Rally",
        "icon":  "⚔️",
        "settings": [
            {"key": "enabled",           "label": "Enable Rally Tab",      "type": "toggle",  "default": True},
            {"key": "quick_join_rally",  "label": "Quick Join Rally",      "type": "toggle",  "default": True},
            {"key": "create_rally",      "label": "Create Rally",          "type": "toggle",  "default": True},
            {"key": "boomer_level",      "label": "Boomer Level",          "type": "spinner", "default": 5, "min": 1, "max": 100},
            {"key": "use_max_formations","label": "Use Max Formations",    "type": "toggle",  "default": True},
            {"key": "max_rallies_per_day","label": "Max Rallies Per Day",  "type": "spinner", "default": 5, "min": 1, "max": 50},
        ]
    },
    {
        "key":   "gathering",
        "label": "Gathering",
        "icon":  "🪵",
        "settings": [
            {"key": "enabled",             "label": "Enable Gathering Tab",  "type": "toggle",  "default": False},
            {"key": "collect_wood",        "label": "Collect Wood",          "type": "toggle",  "default": True},
            {"key": "collect_food",        "label": "Collect Food",          "type": "toggle",  "default": True},
            {"key": "collect_electricity", "label": "Collect Electricity",   "type": "toggle",  "default": False},
            {"key": "collect_zents",       "label": "Collect Zents",         "type": "toggle",  "default": False},
            {"key": "resource_site_level", "label": "Resource Site Level",   "type": "spinner", "default": 6, "min": 1, "max": 10},
            {"key": "max_formations",      "label": "Max Formations To Use", "type": "spinner", "default": 3, "min": 1, "max": 4},
        ]
    },
]

DEFAULT_FARM = {
    "name": "Farm",
    "emu_index": 1,
    "port": 21503,
    "enabled": True,
    "tasks": {cat["key"]: {s["key"]: s["default"] for s in cat["settings"]}
              for cat in TASK_CATEGORIES}
}


def new_farm(index: int, port: int = None, emulator_type: str = "MEmu") -> dict:
    import copy
    f = copy.deepcopy(DEFAULT_FARM)
    f["name"] = f"Farm {index}"
    f["emu_index"] = index
    if port:
        f["port"] = port
    else:
        try:
            from launcher import index_to_port
            f["port"] = index_to_port(index - 1, emulator_type)  # index is 1-based in GUI
        except Exception:
            f["port"] = 21503 + (index - 1) * 10  # fallback
    return f


# ══════════════════════════════════════════════════════════════════════════════
# Main App
# ══════════════════════════════════════════════════════════════════════════════

class BotApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("LAST Z BOT")
        self.geometry("1300x840")
        self.minsize(1100, 720)
        self.configure(fg_color=C["bg"])

        # Ensure we have a place to store configuration.
        ensure_app_dir()
        self._data_path = get_farms_path()

        self.farms: list[dict] = []
        self.bot_settings: dict = self._default_bot_settings()
        self.engines: dict[int, BotEngine] = {}
        self._active_farm_idx: int = 0
        self._expanded_cats: dict = {}
        self._farm_widget_refs: dict = {}
        self._clipboard_farm = None
        self._record_runs = False
        self._arrange_lock = threading.Lock()

        # Timer state
        self._bot_start_time = None
        self._timer_running = False
        self._timer_after_id = None
        self._cycle_complete_time = None   # set when all tasks finish

        # Farm-settings category drag-and-drop
        self._cat_drag_key = None
        self._cat_drag_hover_key = None
        self._cat_frames: dict = {}

        self._load_data()
        self._build_ui()
        self._nav_select("run_bot")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Show setup wizard on first launch (no farms.json existed).
        if self._is_first_run:
            self.after(300, self._show_setup_wizard)

        # Check for updates in background (non-blocking).
        self._check_for_updates_async()

    # ── Data ──────────────────────────────────────────────────────────────

    def _default_bot_settings(self) -> dict:
        return {
            "emulator": "MEmu",
            "emulator_path": r"C:\Program Files\Microvirt\MEmu",
            "force_kill_hung": True,
            "auto_run_startup": False,
            "max_farm_timeout": 40,
            "max_concurrent_sessions": 1,
            "emulator_boot_timeout": 180,
            "game_load_wait": 20,
            "post_launch_wait": 5,
            "vision_confidence": 0.80,
            "retry_failed": True,
            "max_retries": 2,
            "loop_tasks": False,
            "loop_delay": 60,
            "screenshot_on_error": True,
            "auto_arrange": True,
            "minimum_cycle_time": 120,
        }

    def _load_data(self):
        p = self._data_path
        self._is_first_run = not p.exists()
        if p.exists():
            try:
                with open(p) as f:
                    data = json.load(f)
                self.farms = data.get("farms", [])
                self.bot_settings = {**self._default_bot_settings(),
                                     **data.get("bot_settings", {})}
                # Migrate legacy memu_path → emulator_path
                if "memu_path" in self.bot_settings and "emulator_path" not in data.get("bot_settings", {}):
                    self.bot_settings["emulator_path"] = self.bot_settings.pop("memu_path")
            except Exception:
                pass
        if not self.farms:
            self.farms = [new_farm(1)]

    def _save_data(self):
        try:
            with open(self._data_path, "w") as f:
                json.dump({"farms": self.farms,
                           "bot_settings": self.bot_settings}, f, indent=2)
        except Exception as e:
            self._log(f"Save error: {e}", "error")

    # ── UI Shell ──────────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_nav()
        self.content = ctk.CTkFrame(self, fg_color=C["bg"], corner_radius=0)
        self.content.pack(side="left", fill="both", expand=True)
        self.pages: dict[str, ctk.CTkFrame] = {}
        for key in ["run_bot", "bot_settings", "farm_settings",
                    "farm_stats", "restore_backup"]:
            pg = ctk.CTkFrame(self.content, fg_color=C["bg"], corner_radius=0)
            self.pages[key] = pg
        self._build_page_run_bot()
        self._build_page_bot_settings()
        self._build_page_farm_settings()
        self._build_page_farm_stats()
        self._build_page_restore_backup()

    # ── Nav ───────────────────────────────────────────────────────────────

    def _build_nav(self):
        nav = ctk.CTkFrame(self, fg_color=C["nav"], corner_radius=0, width=210)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        self._nav_frame = nav

        logo = ctk.CTkFrame(nav, fg_color="transparent", height=64)
        logo.pack(fill="x")
        logo.pack_propagate(False)
        ctk.CTkLabel(logo, text="⚡", font=("Segoe UI", 22),
                     text_color=C["accent"]).pack(side="left", padx=(16, 4), pady=16)
        ctk.CTkLabel(logo, text="LAST Z BOT", font=("Segoe UI Black", 13),
                     text_color=C["text"]).pack(side="left")

        ctk.CTkFrame(nav, height=1, fg_color=C["border"]).pack(fill="x", padx=12)

        self._nav_btns: dict[str, ctk.CTkButton] = {}
        items = [
            ("run_bot",        "▶   Run Bot"),
            ("bot_settings",   "🔧   Bot Settings"),
            ("farm_settings",  "⚙   Farm Settings"),
            ("farm_stats",     "📊   Farm Stats"),
            ("restore_backup", "💾   Restore / Backup"),
        ]
        for key, label in items:
            btn = ctk.CTkButton(
                nav, text=label, anchor="w", font=FT, height=42,
                fg_color="transparent", text_color=C["text2"],
                hover_color=C["nav_sel"], corner_radius=6,
                command=lambda k=key: self._nav_select(k)
            )
            btn.pack(fill="x", padx=8, pady=2)
            self._nav_btns[key] = btn

        ctk.CTkFrame(nav, height=1, fg_color=C["border"]).pack(
            fill="x", padx=12, side="bottom", pady=(0, 60))
        for label in ["👤   Profile", "❓   Help Center"]:
            ctk.CTkButton(nav, text=label, anchor="w", font=FT, height=38,
                          fg_color="transparent", text_color=C["text3"],
                          hover_color=C["nav_sel"], corner_radius=6,
                          command=lambda: None
                          ).pack(fill="x", padx=8, pady=1, side="bottom")

    def _nav_select(self, key: str):
        for k, btn in self._nav_btns.items():
            if k == key:
                btn.configure(fg_color=C["accent"], text_color=C["bg"], font=FB)
            else:
                btn.configure(fg_color="transparent", text_color=C["text2"], font=FT)
        for pg in self.pages.values():
            pg.pack_forget()
        self.pages[key].pack(fill="both", expand=True)

    # ══════════════════════════════════════════════════════════════════════
    # PAGE: Run Bot
    # ══════════════════════════════════════════════════════════════════════

    def _build_page_run_bot(self):
        pg = self.pages["run_bot"]
        hdr = self._page_header(pg, "▶  Run Bot",
                                 "Run your farm sequences.")
        ctrl = ctk.CTkFrame(hdr, fg_color="transparent")
        ctrl.pack(side="right", pady=10, padx=12)

        self._run_btn = ctk.CTkButton(
            ctrl, text="⚡ START BOT", command=self._toggle_bot,
            font=FB, height=36, corner_radius=6,
            fg_color=C["accent"], hover_color=C["accent2"],
            text_color=C["bg"], width=140,
        )
        self._run_btn.pack(side="left", padx=4)

        # HIDDEN: "Record Runs" button — kept for future debugging use
        # Divider before it also commented out; restore both when re-enabling
        # ctk.CTkFrame(ctrl, width=1, fg_color=C["border"]).pack(
        #     side="left", fill="y", padx=8, pady=4)
        # self.rec_toggle_btn = ctk.CTkButton(
        #     ctrl, text="⏺ Record Runs", command=self._toggle_record_runs,
        #     font=("Segoe UI Semibold", 10), height=28, corner_radius=4,
        #     fg_color=C["panel2"], hover_color=C["border"],
        #     text_color=C["text"], width=110,
        # )
        # self.rec_toggle_btn.pack(side="left", padx=4)

        # ── Timer + Status row ─────────────────────────────────────────────
        stats_row = ctk.CTkFrame(pg, fg_color="transparent")
        stats_row.pack(fill="x", padx=16, pady=(8, 0))

        # Timer card
        timer_card = ctk.CTkFrame(stats_row, fg_color=C["panel"], corner_radius=8,
                                   border_width=1, border_color=C["border"])
        timer_card.pack(side="left", fill="both", expand=True, padx=(0, 6), pady=4)

        ctk.CTkLabel(timer_card, text="⏱  Run Bot Timer", font=FB,
                     text_color=C["text2"]).pack(anchor="w", padx=14, pady=(10, 6))

        timer_vals = ctk.CTkFrame(timer_card, fg_color="transparent")
        timer_vals.pack(anchor="w", padx=14, pady=(0, 12))

        self._timer_labels = {}
        for key, label_text in [("days", "Days"), ("hrs", "Hr"),
                                  ("min", "Min"), ("sec", "Sec")]:
            unit_f = ctk.CTkFrame(timer_vals, fg_color="transparent")
            unit_f.pack(side="left", padx=(0, 18))
            num_lbl = ctk.CTkLabel(unit_f, text="0",
                                   font=("Segoe UI Black", 22), text_color=C["text"])
            num_lbl.pack(side="left")
            ctk.CTkLabel(unit_f, text=f"  {label_text}",
                         font=FT, text_color=C["text3"]).pack(side="left")
            self._timer_labels[key] = num_lbl

        # Status card
        status_card = ctk.CTkFrame(stats_row, fg_color=C["panel"], corner_radius=8,
                                    border_width=1, border_color=C["border"])
        status_card.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)

        ctk.CTkLabel(status_card, text="📊  Status", font=FB,
                     text_color=C["text2"]).pack(anchor="w", padx=14, pady=(10, 6))

        status_inner = ctk.CTkFrame(status_card, fg_color="transparent")
        status_inner.pack(anchor="w", padx=14, pady=(0, 12))

        self._status_bot_lbl = ctk.CTkLabel(status_inner, text="Bot Status: Stopped",
                                             font=FB, text_color=C["text2"])
        self._status_bot_lbl.pack(anchor="w")

        active_total = len([f for f in self.farms if f.get("enabled", True)])
        self._status_farms_lbl = ctk.CTkLabel(status_inner,
                                               text=f"Active Farms: 0 / {active_total}",
                                               font=FB, text_color=C["text2"])
        self._status_farms_lbl.pack(anchor="w")

        min_cycle = int(self.bot_settings.get("minimum_cycle_time", 120))
        self._status_cycle_lbl = ctk.CTkLabel(status_inner,
                                               text=f"Min Cycle Time: {min_cycle} min",
                                               font=FB, text_color=C["text2"])
        self._status_cycle_lbl.pack(anchor="w")

        self._status_next_lbl = ctk.CTkLabel(status_inner, text="Next Cycle: —",
                                              font=FB, text_color=C["text2"])
        self._status_next_lbl.pack(anchor="w")

        # ── Activity Log (main focus, full width) ──────────────────────────
        log_card = ctk.CTkFrame(pg, fg_color=C["panel"], corner_radius=8,
                                 border_width=1, border_color=C["border"])
        log_card.pack(fill="both", expand=True, padx=16, pady=(8, 16))

        log_hdr = ctk.CTkFrame(log_card, fg_color="transparent", height=40)
        log_hdr.pack(fill="x", padx=14, pady=(10, 0))
        log_hdr.pack_propagate(False)
        ctk.CTkLabel(log_hdr, text="📋  Activity Log", font=FT2,
                     text_color=C["accent"]).pack(side="left")
        self._btn(log_hdr, "🗑 Clear", self._clear_log, small=True).pack(side="right")

        log_bg = ctk.CTkFrame(log_card, fg_color=C["panel2"], corner_radius=6)
        log_bg.pack(fill="both", expand=True, padx=14, pady=(6, 14))

        self.log_box = tk.Text(
            log_bg, font=FSM, bg=C["panel2"], fg=C["text3"],
            relief="flat", bd=0, wrap="word",
            state="disabled", padx=8, pady=8
        )
        sb = tk.Scrollbar(log_bg, command=self.log_box.yview,
                          bg=C["panel2"], troughcolor=C["panel2"],
                          relief="flat", bd=0)
        sb.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=sb.set)
        self.log_box.pack(fill="both", expand=True, padx=2, pady=2)
        self.log_box.tag_config("error",   foreground=C["red"])
        self.log_box.tag_config("success", foreground=C["green"])
        self.log_box.tag_config("warn",    foreground=C["yellow"])
        self.log_box.tag_config("info",    foreground=C["text3"])
        self.log_box.tag_config("accent",  foreground=C["accent"])

        # Hidden stubs so references elsewhere don't crash
        self.preview_canvas = ctk.CTkLabel(log_card, text="", width=0, height=0)

    def _toggle_bot(self):
        if self._timer_running:
            self._stop_all()
        else:
            self._start_all()

    def _update_run_btn(self):
        if not hasattr(self, "_run_btn"):
            return
        if self._timer_running:
            self._run_btn.configure(
                text="■ STOP BOT", fg_color=C["red"],
                hover_color=C["red_dim"], text_color=C["white"])
        else:
            self._run_btn.configure(
                text="⚡ START BOT", fg_color=C["accent"],
                hover_color=C["accent2"], text_color=C["bg"])

    def _rebuild_run_list(self):
        pass  # instances list removed from Run Bot page

    # ── Category drag-and-drop ────────────────────────────────────────────

    def _cat_drag_start(self, event, cat_key: str):
        self._cat_drag_key = cat_key
        self._cat_drag_hover_key = None

        # Dim the source frame so it's clear it's being moved
        if cat_key in self._cat_frames:
            self._cat_frames[cat_key].configure(
                fg_color=C["accent_dim"], border_color=C["accent"])

        # Create a floating ghost that follows the cursor
        cat = next((c for c in TASK_CATEGORIES if c["key"] == cat_key), None)
        ghost_text = f"  ⠿  {cat['icon']}  {cat['label']}  " if cat else f"  ⠿  {cat_key}  "

        ghost = tk.Toplevel(self)
        ghost.overrideredirect(True)
        ghost.attributes("-topmost", True)
        try:
            ghost.attributes("-alpha", 0.88)
        except Exception:
            pass
        ghost.configure(bg=C["card"])
        tk.Label(ghost, text=ghost_text,
                 font=("Segoe UI Semibold", 11),
                 bg=C["card"], fg=C["accent"],
                 padx=10, pady=8).pack()

        self._cat_ghost = ghost
        mx, my = self.winfo_pointerxy()
        ghost.geometry(f"+{mx + 14}+{my - 20}")

    def _cat_drag_motion(self, event):
        if not self._cat_drag_key:
            return
        mx, my = self.winfo_pointerxy()

        # Keep ghost glued to cursor
        if hasattr(self, "_cat_ghost"):
            try:
                self._cat_ghost.geometry(f"+{mx + 14}+{my - 20}")
            except Exception:
                pass

        # Detect which item the cursor is over
        hovered = None
        for ckey, frame in self._cat_frames.items():
            if ckey == self._cat_drag_key:
                continue
            try:
                if (frame.winfo_rootx() <= mx <= frame.winfo_rootx() + frame.winfo_width()
                        and frame.winfo_rooty() <= my <= frame.winfo_rooty() + frame.winfo_height()):
                    hovered = ckey
                    break
            except Exception:
                pass

        # Only act when the hover target actually changes
        if hovered == self._cat_drag_hover_key:
            return
        self._cat_drag_hover_key = hovered

        # Refresh border highlights
        for ckey, frame in self._cat_frames.items():
            try:
                if ckey == self._cat_drag_key:
                    frame.configure(border_color=C["accent"])
                elif ckey == hovered:
                    frame.configure(border_color=C["blue"])
                else:
                    frame.configure(border_color=C["border"])
            except Exception:
                pass

        if hovered:
            # Live-reorder: repack frames to preview where the item would land
            order = list(self.bot_settings.get(
                "task_cat_order", [c["key"] for c in TASK_CATEGORIES]))
            for c in TASK_CATEGORIES:
                if c["key"] not in order:
                    order.append(c["key"])
            preview = list(order)
            src_i = preview.index(self._cat_drag_key) if self._cat_drag_key in preview else -1
            tgt_i = preview.index(hovered) if hovered in preview else -1
            if src_i >= 0 and tgt_i >= 0:
                preview.insert(tgt_i, preview.pop(src_i))
            for key in preview:
                if key in self._cat_frames:
                    self._cat_frames[key].pack_forget()
                    self._cat_frames[key].pack(fill="x", pady=4)

    def _cat_drag_end(self, event, farm_idx: int):
        if not self._cat_drag_key:
            return

        # Destroy ghost
        if hasattr(self, "_cat_ghost"):
            try:
                self._cat_ghost.destroy()
            except Exception:
                pass
            del self._cat_ghost

        target_key = self._cat_drag_hover_key

        # Reset frame appearances
        for frame in self._cat_frames.values():
            try:
                frame.configure(fg_color=C["panel"], border_color=C["border"])
            except Exception:
                pass

        if target_key:
            order = list(self.bot_settings.get(
                "task_cat_order", [c["key"] for c in TASK_CATEGORIES]))
            for c in TASK_CATEGORIES:
                if c["key"] not in order:
                    order.append(c["key"])
            if self._cat_drag_key in order and target_key in order:
                src_i = order.index(self._cat_drag_key)
                tgt_i = order.index(target_key)
                order.insert(tgt_i, order.pop(src_i))
                self.bot_settings["task_cat_order"] = order
                self._save_data()
                # Full rebuild to clean up any pack order artifacts
                self._show_farm_detail(farm_idx)
        else:
            # Dropped on nothing — restore original visual order
            order = list(self.bot_settings.get(
                "task_cat_order", [c["key"] for c in TASK_CATEGORIES]))
            for key in order:
                if key in self._cat_frames:
                    self._cat_frames[key].pack_forget()
                    self._cat_frames[key].pack(fill="x", pady=4)

        self._cat_drag_key = None
        self._cat_drag_hover_key = None

    # ══════════════════════════════════════════════════════════════════════
    # PAGE: Bot Settings
    # ══════════════════════════════════════════════════════════════════════

    def _build_page_bot_settings(self):
        pg = self.pages["bot_settings"]
        # Clear existing widgets so this method is safe to call on emulator type change
        for w in pg.winfo_children():
            w.destroy()
        self._page_header(pg, "🔧  Bot Settings",
                           "Global configuration for all farm instances.")

        scroll = ctk.CTkScrollableFrame(pg, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=16, pady=8)

        card = self._card(scroll, "Emulator Configuration")
        self._setting_row(card, "Emulator", "select", "emulator",
                          ["MEmu", "LDPlayer", "Nox"])
        emu_type = self.bot_settings.get("emulator", "MEmu")
        self._setting_row(card, f"{emu_type} Path", "path", "emulator_path")
        self._setting_row(card, "Force Kill Hung Emulator",  "toggle", "force_kill_hung")
        self._setting_row(card, "Auto Run on Startup",       "toggle", "auto_run_startup")
        self._setting_row(card, "Auto Arrange Emulator",     "toggle", "auto_arrange")
        self._setting_row(card, "Max Farm Timeout (min)",    "number", "max_farm_timeout")
        self._setting_row(card, "Max Concurrent Sessions",   "number", "max_concurrent_sessions")

        card_t = self._card(scroll, "⏱  Timing & Load Waits")
        self._setting_row(card_t, "Emulator Boot Timeout (sec)", "number", "emulator_boot_timeout")
        self._setting_row(card_t, "Game Load Wait (sec)",        "number", "game_load_wait")
        self._setting_row(card_t, "Post-Launch Wait (sec)",      "number", "post_launch_wait")

        card2 = self._card(scroll, "Bot Behavior")
        self._setting_row(card2, "Loop Tasks Continuously",        "toggle", "loop_tasks")
        self._setting_row(card2, "Loop Delay (seconds)",           "number", "loop_delay")
        self._setting_row(card2, "Minimum Cycle Time (in Minutes)", "number", "minimum_cycle_time")
        self._setting_row(card2, "Retry Failed Actions",    "toggle", "retry_failed")
        self._setting_row(card2, "Max Retries",             "number", "max_retries")
        self._setting_row(card2, "Screenshot on Error",     "toggle", "screenshot_on_error")

        card3 = self._card(scroll, "Vision")
        self._setting_row(card3, "Confidence Threshold (0.5–1.0)", "number", "vision_confidence")

        save_row = ctk.CTkFrame(scroll, fg_color="transparent")
        save_row.pack(fill="x", pady=12)
        self._btn(save_row, "💾  SAVE SETTINGS",
                  self._save_bot_settings, C["accent"]).pack(side="right")

    def _save_bot_settings(self):
        self._save_data()
        try:
            cfg = load_config("config.json")
            cfg["bot"]["loop_tasks"] = self.bot_settings.get("loop_tasks", False)
            cfg["bot"]["max_retries"] = self.bot_settings.get("max_retries", 2)
            cfg["bot"]["screenshot_on_error"] = self.bot_settings.get("screenshot_on_error", True)
            cfg["vision"]["confidence_threshold"] = float(
                self.bot_settings.get("vision_confidence", 0.8))
            save_config(cfg, "config.json")
        except Exception:
            pass
        self._log("Bot settings saved.", "success")

    # ══════════════════════════════════════════════════════════════════════
    # PAGE: Farm Settings
    # ══════════════════════════════════════════════════════════════════════

    def _build_page_farm_settings(self):
        pg = self.pages["farm_settings"]
        hdr = self._page_header(pg, "⚙  Farm Settings",
                                  "Configure each emulator instance and its task sequence.")
        ctrl = ctk.CTkFrame(hdr, fg_color="transparent")
        ctrl.pack(side="right", pady=10, padx=12)
        self._btn(ctrl, "＋ ADD FARM",    self._add_farm,       C["accent"]).pack(side="left", padx=4)
        self._btn(ctrl, "BATCH SETTINGS", self._batch_settings).pack(side="left", padx=4)

        body = ctk.CTkFrame(pg, fg_color="transparent")
        body.pack(fill="both", expand=True)

        list_panel = ctk.CTkFrame(body, fg_color=C["panel"], corner_radius=0, width=300)
        list_panel.pack(side="left", fill="y")
        list_panel.pack_propagate(False)

        ctk.CTkLabel(list_panel, text="INSTANCES", font=FT2,
                     text_color=C["accent"]).pack(anchor="w", padx=14, pady=(14, 8))

        self.farm_list_scroll = ctk.CTkScrollableFrame(list_panel, fg_color="transparent")
        self.farm_list_scroll.pack(fill="both", expand=True, padx=8)

        self.farm_detail_panel = ctk.CTkFrame(body, fg_color=C["bg"], corner_radius=0)
        self.farm_detail_panel.pack(side="left", fill="both", expand=True)

        self._rebuild_farm_list()
        if self.farms:
            self._show_farm_detail(0)

    def _rebuild_farm_list(self):
        for w in self.farm_list_scroll.winfo_children():
            w.destroy()
        for i, farm in enumerate(self.farms):
            self._farm_list_row(i, farm)

    def _farm_list_row(self, idx: int, farm: dict):
        active = (idx == self._active_farm_idx)
        row = ctk.CTkFrame(
            self.farm_list_scroll,
            fg_color=C["accent_dim"] if active else C["card"],
            corner_radius=6, border_width=1,
            border_color=C["accent"] if active else C["border"]
        )
        row.pack(fill="x", pady=3)

        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", padx=10, pady=8, fill="x", expand=True)

        ctk.CTkLabel(left,
            text=f" {farm['emu_index']:02d} ",
            font=("Consolas", 11, "bold"),
            text_color=C["accent"], fg_color=C["accent_dim"], corner_radius=4
        ).pack(side="left", padx=(0, 8))

        info = ctk.CTkFrame(left, fg_color="transparent")
        info.pack(side="left")
        ctk.CTkLabel(info, text=farm["name"], font=FB,
                     text_color=C["text"]).pack(anchor="w")
        ctk.CTkLabel(info, text=f"Emu ID: {farm['emu_index']}",
                     font=FSM, text_color=C["text3"]).pack(anchor="w")

        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="right", padx=10, pady=8)

        tog_var = ctk.BooleanVar(value=farm.get("enabled", True))
        ctk.CTkSwitch(right, text="", variable=tog_var, width=46,
                       progress_color=C["accent"], button_color=C["white"],
                       command=lambda v=tog_var, f=farm: self._toggle_farm(f, v)
                       ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(right, text="⚙", width=28, height=28,
                      font=("Segoe UI", 12), fg_color="transparent",
                      text_color=C["text3"], hover_color=C["border"],
                      command=lambda i=idx: self._show_farm_detail(i)
                      ).pack(side="left", padx=2)

        ctk.CTkButton(right, text="🗑", width=28, height=28,
                      font=("Segoe UI", 12), fg_color="transparent",
                      text_color=C["text3"], hover_color=C["red_dim"],
                      command=lambda i=idx: self._delete_farm(i)
                      ).pack(side="left", padx=2)

        row.bind("<Button-1>", lambda e, i=idx: self._show_farm_detail(i))
        left.bind("<Button-1>", lambda e, i=idx: self._show_farm_detail(i))
        info.bind("<Button-1>", lambda e, i=idx: self._show_farm_detail(i))

    def _show_farm_detail(self, idx: int):
        self._active_farm_idx = idx
        self._rebuild_farm_list()

        for w in self.farm_detail_panel.winfo_children():
            w.destroy()

        farm = self.farms[idx]

        # Header
        dh = ctk.CTkFrame(self.farm_detail_panel, fg_color=C["panel"],
                           corner_radius=0, height=52)
        dh.pack(fill="x")
        dh.pack_propagate(False)
        ctk.CTkLabel(dh, text=f"⚙  {farm['name']}", font=FT2,
                     text_color=C["text"]).pack(side="left", padx=16, pady=12)
        rhdr = ctk.CTkFrame(dh, fg_color="transparent")
        rhdr.pack(side="right", padx=12)
        self._btn(rhdr, "✏ Rename", lambda: self._rename_farm(idx)).pack(side="left", padx=4)

        # Meta bar
        meta = ctk.CTkFrame(self.farm_detail_panel, fg_color=C["panel2"],
                             corner_radius=0, height=40)
        meta.pack(fill="x")
        meta.pack_propagate(False)
        for label, val in [("Name:", farm["name"]),
                           ("Emu Index:", str(farm["emu_index"])),
                           ("Port:", str(farm["port"]))]:
            ctk.CTkLabel(meta, text=label, font=FSM,
                         text_color=C["text3"]).pack(side="left", padx=(14, 2), pady=10)
            ctk.CTkLabel(meta, text=val, font=("Consolas", 10, "bold"),
                         text_color=C["accent"]).pack(side="left", padx=(0, 16), pady=10)

        # Scrollable task categories
        scroll = ctk.CTkScrollableFrame(self.farm_detail_panel, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=16, pady=10)

        # Bottom bar
        bot_bar = ctk.CTkFrame(self.farm_detail_panel, fg_color=C["panel"],
                                corner_radius=0, height=52)
        bot_bar.pack(fill="x", side="bottom")
        bot_bar.pack_propagate(False)
        self._btn(bot_bar, "COPY",  lambda: self._copy_farm(idx)).pack(side="left", padx=10, pady=10)
        self._btn(bot_bar, "PASTE", lambda: self._paste_farm(idx)).pack(side="left", padx=4, pady=10)
        self._btn(bot_bar, "💾 SAVE", lambda: self._save_farm(idx),
                  C["accent"]).pack(side="right", padx=10, pady=10)

        # Build categories in user-defined order (drag-and-drop persisted)
        farm_key = f"farm_{idx}"
        if farm_key not in self._expanded_cats:
            self._expanded_cats[farm_key] = {}

        self._farm_widget_refs = {}
        self._cat_frames = {}

        order = self.bot_settings.get("task_cat_order",
                                       [c["key"] for c in TASK_CATEGORIES])
        cat_map = {c["key"]: c for c in TASK_CATEGORIES}
        ordered_cats = [cat_map[k] for k in order if k in cat_map]
        # Append any categories not yet in the saved order
        for c in TASK_CATEGORIES:
            if c["key"] not in order:
                ordered_cats.append(c)

        for cat in ordered_cats:
            self._build_cat_section(scroll, farm, idx, cat, farm_key)

    def _build_cat_section(self, parent, farm, farm_idx, cat, farm_key):
        cat_key = cat["key"]
        expanded = self._expanded_cats[farm_key].get(cat_key, False)

        outer = ctk.CTkFrame(parent, fg_color=C["panel"], corner_radius=8,
                              border_width=1, border_color=C["border"])
        outer.pack(fill="x", pady=4)
        self._cat_frames[cat_key] = outer  # store for drop detection

        # Header row
        hdr = ctk.CTkFrame(outer, fg_color="transparent", height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        drag_lbl = ctk.CTkLabel(hdr, text="⠿", font=("Segoe UI", 14),
                                 text_color=C["muted"], cursor="fleur")
        drag_lbl.pack(side="left", padx=(10, 4))
        drag_lbl.bind("<ButtonPress-1>",
                      lambda e, ck=cat_key: self._cat_drag_start(e, ck))
        drag_lbl.bind("<B1-Motion>", self._cat_drag_motion)
        drag_lbl.bind("<ButtonRelease-1>",
                      lambda e, fi=farm_idx: self._cat_drag_end(e, fi))
        ctk.CTkLabel(hdr, text=f"{cat['icon']}  {cat['label']}",
                     font=FB, text_color=C["text"]).pack(side="left", padx=4)

        rhdr = ctk.CTkFrame(hdr, fg_color="transparent")
        rhdr.pack(side="right", padx=10)
        ctk.CTkLabel(rhdr, text="⚙", font=("Segoe UI", 13),
                     text_color=C["accent"]).pack(side="left", padx=6)
        expand_lbl = ctk.CTkLabel(rhdr, text="∧" if expanded else "∨",
                                   font=("Segoe UI", 14), text_color=C["text3"])
        expand_lbl.pack(side="left")

        # Content
        content = ctk.CTkFrame(outer, fg_color=C["panel2"], corner_radius=0)
        if expanded:
            content.pack(fill="x")

        farm_tasks = farm.get("tasks", {}).get(cat_key, {})
        widget_refs = {}

        for setting in cat["settings"]:
            skey = setting["key"]
            sval = farm_tasks.get(skey, setting["default"])

            srow = ctk.CTkFrame(content, fg_color="transparent", height=44)
            srow.pack(fill="x", padx=16, pady=1)
            srow.pack_propagate(False)

            ctk.CTkLabel(srow, text=setting["label"], font=FT,
                         text_color=C["text2"]).pack(side="left", pady=8)

            if setting["type"] == "toggle":
                var = ctk.BooleanVar(value=bool(sval))
                ctk.CTkSwitch(srow, text="", variable=var, width=46,
                               progress_color=C["accent"], button_color=C["white"]
                               ).pack(side="right", pady=8)
                widget_refs[skey] = ("toggle", var)

            elif setting["type"] == "select":
                var = ctk.StringVar(value=str(sval))
                ctk.CTkOptionMenu(srow, values=setting["options"], variable=var,
                                   width=130, fg_color=C["card"],
                                   button_color=C["border"], text_color=C["text"],
                                   dropdown_fg_color=C["panel2"]
                                   ).pack(side="right", pady=6)
                widget_refs[skey] = ("select", var)

            elif setting["type"] == "number":
                var = ctk.StringVar(value=str(sval))
                ctk.CTkEntry(srow, textvariable=var, width=80,
                              fg_color=C["card"], border_color=C["border"],
                              text_color=C["text"], font=FM
                              ).pack(side="right", pady=6)
                widget_refs[skey] = ("number", var)

            elif setting["type"] == "spinner":
                s_min = setting.get("min", 0)
                s_max = setting.get("max", 999)
                var = ctk.IntVar(value=int(sval))
                spin_frame = ctk.CTkFrame(srow, fg_color="transparent")
                spin_frame.pack(side="right", pady=6)
                def _dec(v=var, lo=s_min):
                    v.set(max(lo, v.get() - 1))
                def _inc(v=var, hi=s_max):
                    v.set(min(hi, v.get() + 1))
                ctk.CTkButton(spin_frame, text="−", width=30, height=28,
                               fg_color=C["accent"], hover_color=C["accent2"],
                               text_color=C["bg"], font=FB,
                               command=_dec).pack(side="left")
                ctk.CTkLabel(spin_frame, textvariable=var, width=40,
                              font=FM, text_color=C["text"],
                              fg_color=C["card"], anchor="center"
                              ).pack(side="left", padx=2)
                ctk.CTkButton(spin_frame, text="+", width=30, height=28,
                               fg_color=C["accent"], hover_color=C["accent2"],
                               text_color=C["bg"], font=FB,
                               command=_inc).pack(side="left")
                widget_refs[skey] = ("spinner", var)

            ctk.CTkFrame(content, height=1, fg_color=C["border"]).pack(fill="x", padx=16)

        self._farm_widget_refs[cat_key] = widget_refs

        def toggle_expand(c=content, ck=cat_key, el=expand_lbl, fk=farm_key):
            currently = self._expanded_cats[fk].get(ck, False)
            self._expanded_cats[fk][ck] = not currently
            if not currently:
                c.pack(fill="x")
                el.configure(text="∧")
            else:
                c.pack_forget()
                el.configure(text="∨")

        hdr.bind("<Button-1>", lambda e, fn=toggle_expand: fn())
        expand_lbl.bind("<Button-1>", lambda e, fn=toggle_expand: fn())

    def _save_farm(self, idx: int):
        farm = self.farms[idx]
        for cat_key, refs in self._farm_widget_refs.items():
            if cat_key not in farm["tasks"]:
                farm["tasks"][cat_key] = {}
            for skey, (stype, var) in refs.items():
                val = var.get()
                if stype == "number":
                    try:
                        val = float(val) if "." in str(val) else int(val)
                    except ValueError:
                        pass
                elif stype == "spinner":
                    val = int(val)
                farm["tasks"][cat_key][skey] = val
        self._save_data()
        self._log(f"Saved: {farm['name']}", "success")

    def _copy_farm(self, idx: int):
        import copy
        self._clipboard_farm = copy.deepcopy(self.farms[idx])
        self._log(f"Copied settings from: {self.farms[idx]['name']}")

    def _paste_farm(self, idx: int):
        if not self._clipboard_farm:
            messagebox.showinfo("Paste", "Nothing copied yet.")
            return
        import copy
        paste = copy.deepcopy(self._clipboard_farm)
        paste["name"] = self.farms[idx]["name"]
        paste["emu_index"] = self.farms[idx]["emu_index"]
        paste["port"] = self.farms[idx]["port"]
        self.farms[idx] = paste
        self._save_data()
        self._show_farm_detail(idx)
        self._log(f"Pasted settings to: {self.farms[idx]['name']}", "success")

    def _toggle_farm(self, farm: dict, var: ctk.BooleanVar):
        farm["enabled"] = var.get()
        self._save_data()

    def _add_farm(self):
        next_idx = max((f["emu_index"] for f in self.farms), default=0) + 1
        emu_type = self.bot_settings.get("emulator", "MEmu")
        self.farms.append(new_farm(next_idx, emulator_type=emu_type))
        self._save_data()
        self._rebuild_farm_list()
        self._show_farm_detail(len(self.farms) - 1)
        self._rebuild_run_list()

    def _delete_farm(self, idx: int):
        if len(self.farms) <= 1:
            messagebox.showwarning("Delete", "Cannot delete the last farm.")
            return
        if messagebox.askyesno("Delete", f"Delete '{self.farms[idx]['name']}'?"):
            self.farms.pop(idx)
            self._active_farm_idx = max(0, idx - 1)
            self._save_data()
            self._rebuild_farm_list()
            self._show_farm_detail(self._active_farm_idx)
            self._rebuild_run_list()

    def _rename_farm(self, idx: int):
        name = simpledialog.askstring("Rename", "New name:",
                                       initialvalue=self.farms[idx]["name"])
        if name:
            self.farms[idx]["name"] = name
            self._save_data()
            self._rebuild_farm_list()
            self._show_farm_detail(idx)

    def _batch_settings(self):
        messagebox.showinfo("Batch Settings",
                            "Use Copy → Paste on each farm to apply one farm's config to others.")

    # ══════════════════════════════════════════════════════════════════════
    # PAGE: Farm Stats
    # ══════════════════════════════════════════════════════════════════════

    def _build_page_farm_stats(self):
        pg = self.pages["farm_stats"]
        self._page_header(pg, "📊  Farm Stats",
                           "Session performance across all instances.")

        scroll = ctk.CTkScrollableFrame(pg, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=16, pady=8)

        card = self._card(scroll, "Session Overview")

        hrow = ctk.CTkFrame(card, fg_color=C["panel2"], corner_radius=4)
        hrow.pack(fill="x", pady=(0, 4))
        for col in ["Farm", "Emu ID", "Status", "Runtime", "Actions", "Success %", "Tasks"]:
            ctk.CTkLabel(hrow, text=col, font=FB,
                         text_color=C["accent"], width=110).pack(side="left", padx=6, pady=6)

        self.stats_rows_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.stats_rows_frame.pack(fill="x")
        self._refresh_stats_table()

        self._btn(scroll, "⟳ Refresh", self._refresh_stats_table,
                  C["accent"]).pack(anchor="e", pady=8)

    def _refresh_stats_table(self):
        for w in self.stats_rows_frame.winfo_children():
            w.destroy()
        for farm in self.farms:
            engine = self.engines.get(farm["emu_index"])
            row = ctk.CTkFrame(self.stats_rows_frame, fg_color=C["card"],
                                corner_radius=4, border_width=1, border_color=C["border"])
            row.pack(fill="x", pady=2)
            vals = [
                farm["name"],
                str(farm["emu_index"]),
                engine.state.value if engine else "idle",
                f"{engine.stats.runtime_seconds:.0f}s" if engine else "—",
                f"{engine.stats.actions_succeeded}/{engine.stats.actions_run}" if engine else "—",
                f"{engine.stats.success_rate:.0f}%" if engine else "—",
                str(engine.stats.tasks_completed) if engine else "—",
            ]
            for v in vals:
                ctk.CTkLabel(row, text=v, font=FM,
                             text_color=C["text2"], width=110).pack(side="left", padx=6, pady=6)

    # ══════════════════════════════════════════════════════════════════════
    # PAGE: Restore / Backup
    # ══════════════════════════════════════════════════════════════════════

    def _build_page_restore_backup(self):
        pg = self.pages["restore_backup"]
        self._page_header(pg, "💾  Restore / Backup",
                           "Save and restore your farm configurations.")

        scroll = ctk.CTkScrollableFrame(pg, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=16, pady=8)

        card = self._card(scroll, "Backup")
        self._action_row(card, "Export all farm + bot settings to a JSON file",
                         "📤 Export All", self._export_backup)
        self._action_row(card, "Export bot settings only",
                         "📤 Export Bot Settings", self._export_bot_settings)

        card2 = self._card(scroll, "Restore")
        self._action_row(card2, "Import farm settings from a JSON backup",
                         "📥 Import", self._import_backup)
        self._action_row(card2, "Reset all farms to factory defaults",
                         "🔄 Reset All Farms", self._reset_farms)

    def _export_backup(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")],
            initialfile=f"lastz_backup_{datetime.now().strftime('%Y%m%d')}.json")
        if path:
            with open(path, "w") as f:
                json.dump({"farms": self.farms, "bot_settings": self.bot_settings}, f, indent=2)
            self._log(f"Backup exported → {path}", "success")

    def _export_bot_settings(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")],
            initialfile="bot_settings.json")
        if path:
            with open(path, "w") as f:
                json.dump(self.bot_settings, f, indent=2)
            self._log(f"Bot settings exported → {path}", "success")

    def _import_backup(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if path:
            try:
                with open(path) as f:
                    data = json.load(f)
                self.farms = data.get("farms", self.farms)
                self.bot_settings = {**self._default_bot_settings(),
                                     **data.get("bot_settings", {})}
                self._save_data()
                self._rebuild_farm_list()
                self._rebuild_run_list()
                if self.farms:
                    self._show_farm_detail(0)
                self._log(f"Imported from {Path(path).name}", "success")
            except Exception as e:
                messagebox.showerror("Import Error", str(e))

    def _reset_farms(self):
        if messagebox.askyesno("Reset", "Reset ALL farms to defaults? Cannot be undone."):
            self.farms = [new_farm(1)]
            self._save_data()
            self._rebuild_farm_list()
            self._rebuild_run_list()
            self._show_farm_detail(0)
            self._log("All farms reset.", "warn")

    # ══════════════════════════════════════════════════════════════════════
    # Bot Engine Control
    # ══════════════════════════════════════════════════════════════════════

    def _make_launcher(self, log_callback) -> "EmulatorLauncher":
        """Build an EmulatorLauncher from current bot settings."""
        emu_type     = self.bot_settings.get("emulator", "MEmu")
        emu_path     = self.bot_settings.get("emulator_path", r"C:\Program Files\Microvirt\MEmu")
        boot_timeout = int(float(self.bot_settings.get("emulator_boot_timeout", 180)))
        game_wait    = int(float(self.bot_settings.get("game_load_wait", 20)))
        try:
            import json, sys as _sys
            _base = _sys._MEIPASS if getattr(_sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(_base, "config.json")) as _f:
                _cfg = json.load(_f)
            adb_path = _cfg.get("emulator", {}).get("adb_path", "adb")
        except Exception:
            adb_path = "adb"
        return EmulatorLauncher(
            emulator_type=emu_type,
            install_path=emu_path,
            adb_path=adb_path,
            package=LASTZ_PKG,
            activity=LASTZ_ACT,
            log_callback=log_callback,
            boot_timeout=boot_timeout,
            game_timeout=game_wait,
        )

    def _connect_farm(self, farm: dict):
        if not BOT_AVAILABLE:
            messagebox.showerror("Error", f"Bot modules missing:\n{IMPORT_ERROR}")
            return

        emu_type = self.bot_settings.get("emulator", "MEmu")
        idx   = farm["emu_index"] - 1   # GUI is 1-based, emulator is 0-based
        port  = farm["port"]
        name  = farm["name"]
        self._log(f"Launching {name} ({emu_type} index {idx}, port {port})...", "accent")

        def do():
            try:
                # Step 1 — launch emulator instance + Last Z
                def launch_log(msg, level="info"):
                    self.after(0, lambda m=msg, l=level: self._log(m, l))

                post_wait = int(float(self.bot_settings.get("post_launch_wait", 5)))

                launcher = self._make_launcher(launch_log)
                ok = launcher.launch_and_connect(index=idx, wait_for_game=True)
                if ok and post_wait > 0:
                    self.after(0, lambda s=post_wait: self._log(
                        f"  ⏱ Post-launch wait {s}s for game to settle...", "info"))
                    import time as _time
                    _time.sleep(post_wait)
                if not ok:
                    self.after(0, lambda: self._log(
                        f"✗ {name} failed to launch", "error"))
                    self.after(0, self._stop_timer)
                    return

                # Step 2 — connect bot engine to the now-running instance
                engine = BotEngine("config.json")
                engine.config["emulator"]["ports"] = [port]
                engine.config["emulator"]["type"]  = emu_type
                engine.on_log = lambda m: self.after(0, lambda msg=m: self._log(msg))
                engine.on_state_change = lambda s: self.after(0, self._refresh_stats_table)
                engine.on_stats_update = lambda s: self.after(0, self._refresh_stats_table)

                # Reuse the already-connected bot from launcher
                launched_bot = launcher.get_bot(idx)
                if launched_bot:
                    from vision import VisionEngine
                    from action_executor import ActionExecutor
                    engine.bot     = launched_bot
                    # Enforce portrait orientation first
                    if launched_bot.info and launched_bot.info.screen_width > launched_bot.info.screen_height:
                        self.after(0, lambda: self._log("Screen is landscape — forcing portrait...", "info"))
                        launched_bot.enforce_portrait()
                    # Enforce expected resolution
                    exp_w = engine.config["emulator"].get("expected_width", 540)
                    exp_h = engine.config["emulator"].get("expected_height", 960)
                    if launched_bot.info and (launched_bot.info.screen_width != exp_w or launched_bot.info.screen_height != exp_h):
                        self.after(0, lambda w=exp_w, h=exp_h, cw=launched_bot.info.screen_width, ch=launched_bot.info.screen_height: self._log(
                            f"Resolution mismatch ({cw}x{ch}) — enforcing {w}x{h}...", "info"))
                        launched_bot.enforce_resolution(exp_w, exp_h)
                    engine.vision  = VisionEngine(
                        confidence_threshold=float(self.bot_settings.get("vision_confidence", 0.8)))
                    engine.executor = ActionExecutor(
                        bot=launched_bot,
                        vision=engine.vision,
                        template_dir=str(get_resource_dir() / self.bot_settings.get("template_dir", "templates")),
                        log_callback=lambda m: self.after(0, lambda msg=m: self._log(msg)),
                        emulator_type=emu_type,
                    )
                    self.engines[farm["emu_index"]] = engine
                    self.after(0, lambda: self._log(
                        f"✓ {name} ready — Last Z is open", "success"))
                else:
                    # fallback: connect normally
                    if engine.connect():
                        self.engines[farm["emu_index"]] = engine
                        self.after(0, lambda: self._log(
                            f"✓ {name} connected", "success"))
                    else:
                        self.after(0, lambda: self._log(
                            f"✗ {name} ADB connect failed", "error"))

            except Exception as e:
                self.after(0, lambda err=e: self._log(f"Error: {err}", "error"))

        threading.Thread(target=do, daemon=True).start()

    def _arrange_memu_windows(self):
        """Tile all visible emulator windows left-to-right across the screen.
        One window → right half. Two+ → equal columns from left to right."""
        import ctypes
        import subprocess

        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        emu_type     = self.bot_settings.get("emulator", "MEmu")
        process_name = get_profile(emu_type)["process_name"]

        # Find emulator PIDs via tasklist (reliable regardless of window title)
        try:
            result = subprocess.run(
                ['tasklist', '/FI', f'IMAGENAME eq {process_name}', '/FO', 'CSV', '/NH'],
                capture_output=True, text=True, timeout=5,
            )
            memu_pids = set()
            for line in result.stdout.strip().splitlines():
                parts = line.strip('"').split('","')
                if len(parts) >= 2:
                    try:
                        memu_pids.add(int(parts[1]))
                    except ValueError:
                        pass
        except Exception as e:
            self.after(0, lambda: self._log(f"  ⚠ arrange: could not list {process_name} PIDs: {e}", "warn"))
            return

        if not memu_pids:
            self.after(0, lambda: self._log(f"  ℹ arrange: no {process_name} processes found", "info"))
            return

        # Enumerate all top-level windows and collect those belonging to emulator PIDs
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)
        found = []

        def _cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            # Skip windows with no title (background/child windows)
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return True
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in memu_pids:
                # Only include top-level windows (no owner)
                if user32.GetWindow(hwnd, 4) == 0:  # GW_OWNER = 4
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, buf, 256)
                    found.append((buf.value, pid.value, hwnd))
            return True

        user32.EnumWindows(EnumWindowsProc(_cb), 0)

        if not found:
            self.after(0, lambda: self._log(f"  ℹ arrange: {emu_type} running but no top-level window found yet", "info"))
            return

        # Sort by PID for consistent top-to-bottom ordering on the left
        found.sort(key=lambda x: x[1])
        SW_RESTORE = 9

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        def get_size(hwnd):
            r = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            return r.right - r.left, r.bottom - r.top

        # Stack MEmu windows top-to-bottom at (0, 0) without resizing
        y_offset = 0
        max_memu_w = 0
        for _, _, hwnd in found:
            user32.ShowWindow(hwnd, SW_RESTORE)
            w, h = get_size(hwnd)
            user32.MoveWindow(hwnd, 0, y_offset, w, h, True)
            y_offset += h
            if w > max_memu_w:
                max_memu_w = w

        # Move the bot GUI to the right of the MEmu stack
        bot_hwnd = user32.FindWindowW(None, "LAST Z BOT")
        if bot_hwnd:
            bw, bh = get_size(bot_hwnd)
            user32.ShowWindow(bot_hwnd, SW_RESTORE)
            user32.MoveWindow(bot_hwnd, max_memu_w, 0, bw, bh, True)

        n = len(found)
        self.after(0, lambda count=n, et=emu_type: self._log(
            f"  📐 Arranged {count} {et} window(s) top-left, bot GUI to the right", "info"))

    def _start_farm(self, farm: dict, _semaphore=None):
        """Launch emulator, open Last Z, then start tasks — all in one click."""
        if not BOT_AVAILABLE:
            messagebox.showerror("Error", f"Bot modules missing:\n{IMPORT_ERROR}")
            return

        # Auto-save current UI widget values so spinner/toggle changes apply without a manual Save
        try:
            farm_idx = self.farms.index(farm)
            if getattr(self, "_farm_widget_refs", None):
                self._save_farm(farm_idx)
        except (ValueError, Exception):
            pass

        emu_type = self.bot_settings.get("emulator", "MEmu")
        name = farm["name"]
        idx  = farm["emu_index"] - 1   # GUI 1-based → emulator 0-based
        port = index_to_port(idx, emu_type)
        farm["port"] = port            # Keep in sync
        self._log(f"▶ Starting {name} — {emu_type} index={idx}, port={port}", "accent")
        self._start_timer()

        def do():
            if _semaphore:
                _semaphore.acquire()
            try:
                def launch_log(msg, level="info"):
                    self.after(0, lambda m=msg, l=level: self._log(m, l))

                post_wait = int(float(self.bot_settings.get("post_launch_wait", 5)))

                # Step 1 — launch emulator + Last Z
                launcher = self._make_launcher(launch_log)
                ok = launcher.launch_and_connect(index=idx, wait_for_game=True)
                if not ok:
                    self.after(0, lambda: self._log(f"✗ {name}: emulator launch failed", "error"))
                    self.after(0, self._stop_timer)
                    return
                if post_wait > 0:
                    self.after(0, lambda s=post_wait: self._log(
                        f"  ⏱ Post-launch wait {s}s for game to settle...", "info"))
                    import time as _time
                    _time.sleep(post_wait)
                if self.bot_settings.get("auto_arrange", True):
                    with self._arrange_lock:
                        self._arrange_memu_windows()

                # Step 2 — build engine using the live bot connection
                from vision import VisionEngine
                from action_executor import ActionExecutor

                bot    = launcher.get_bot(idx)
                vision = VisionEngine(
                    confidence_threshold=float(self.bot_settings.get("vision_confidence", 0.8)))

                # Engine must be created first so we can pass its stop_event to the executor.
                # Without this, engine.stop() sets the event but the executor never sees it.
                engine = BotEngine("config.json")
                engine.config["emulator"]["ports"] = [port]
                engine.config["emulator"]["type"]  = emu_type
                engine.bot    = bot
                engine.vision = vision
                engine.on_log          = lambda m: self.after(0, lambda msg=m: self._log(msg))
                engine.on_state_change = lambda s: self.after(0, self._refresh_stats_table)
                engine.on_stats_update = lambda s: self.after(0, self._refresh_stats_table)

                executor = ActionExecutor(
                    bot=bot,
                    vision=vision,
                    template_dir=str(get_resource_dir() / self.bot_settings.get("template_dir", "templates")),
                    log_callback=lambda m: self.after(0, lambda msg=m: self._log(msg)),
                    emulator_type=emu_type,
                    stop_event=engine._stop_event,  # wire stop signal to executor
                )
                engine.executor = executor
                self.engines[farm["emu_index"]] = engine

                # Recording
                if self._record_runs:
                    from datetime import datetime as _datetime
                    rec_name = f"{name}_{_datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    engine.enable_recording(rec_name)

                # Step 3 — run startup dismiss sequence before tasks
                import json as _json
                from pathlib import Path
                tasks_dir = get_resource_dir() / self.bot_settings.get("tasks_dir", "tasks")
                startup_file = tasks_dir / "startup_dismiss.json"
                if startup_file.exists():
                    try:
                        with open(startup_file) as sf:
                            startup_data = _json.load(sf)
                        startup_actions = startup_data.get("actions", startup_data) if isinstance(startup_data, dict) else startup_data
                        self.after(0, lambda: self._log("  ▶ Running startup dismiss...", "info"))
                        for act in startup_actions:
                            if engine._stop_event.is_set():
                                return
                            executor.execute(act)
                        self.after(0, lambda: self._log("  ✓ Startup dismiss done", "success"))
                    except Exception as e:
                        self.after(0, lambda err=e: self._log(f"  ⚠ Startup dismiss error: {err}", "warn"))
                else:
                    self.after(0, lambda: self._log("  ℹ No startup_dismiss.json found — skipping", "info"))

                # Step 3b — run identify_resources to determine gather priority
                identify_file = tasks_dir / "identify_resources.json"
                if identify_file.exists():
                    try:
                        with open(identify_file) as rf:
                            identify_data = _json.load(rf)
                        identify_actions = identify_data.get("actions", identify_data) if isinstance(identify_data, dict) else identify_data
                        self.after(0, lambda: self._log("  ▶ Identifying resource priorities...", "info"))
                        for act in identify_actions:
                            if engine._stop_event.is_set():
                                return
                            executor.execute(act)
                        self.after(0, lambda: self._log("  ✓ Resource priority identified", "success"))
                    except Exception as e:
                        self.after(0, lambda err=e: self._log(f"  ⚠ identify_resources error: {err}", "warn"))
                else:
                    self.after(0, lambda: self._log("  ℹ No identify_resources.json found — skipping", "info"))

                # Step 4 — load tasks from JSON files and start
                tasks = self._farm_to_tasks(farm)
                if not tasks:
                    self.after(0, lambda: self._log(
                        f"⚠ {name}: no tasks found in tasks/ — check your JSON files", "warn"))
                    return

                engine.load_tasks(tasks)
                engine.set_farm_settings(farm.get("tasks", {}))
                engine.config["bot"]["loop_tasks"] = self.bot_settings.get("loop_tasks", False)
                engine.start()
                self.after(0, lambda: self._log(
                    f"✓ {name} running — {len(tasks)} task(s) loaded", "success"))

                # Wait for the engine to finish before releasing the concurrency slot
                if engine._thread:
                    engine._thread.join()

                # Record cycle completion time (used for "Next Cycle" countdown)
                self.after(0, self._on_farm_cycle_complete)

                # Close the emulator instance after tasks complete
                try:
                    launcher.stop_instance(idx)
                    self.after(0, lambda: self._log(f"  ✓ {name} — emulator closed", "info"))
                    if self.bot_settings.get("auto_arrange", True):
                        with self._arrange_lock:
                            self._arrange_memu_windows()
                except Exception:
                    pass

            except Exception as e:
                import traceback
                self.after(0, lambda err=traceback.format_exc(): self._log(f"Error: {err}", "error"))
            finally:
                if _semaphore:
                    _semaphore.release()

        threading.Thread(target=do, daemon=True).start()

    def _stop_farm(self, farm: dict):
        engine = self.engines.get(farm["emu_index"])
        if engine:
            engine.stop()
            self._log(f"■ {farm['name']} stopped", "warn")
        # Delay the check slightly so engine state has time to update
        self.after(800, self._check_timer_stop)

    def _check_timer_stop(self):
        """Stop the session timer if no engines are still running."""
        any_running = any(
            hasattr(e, "state") and e.state.value == "running"
            for e in self.engines.values()
        )
        if not any_running:
            self._stop_timer()
        else:
            self._update_status_display()

    def _on_farm_cycle_complete(self):
        """Called when a farm's engine thread finishes — starts the Next Cycle countdown."""
        self._cycle_complete_time = datetime.now()
        self._update_status_display()

    def _toggle_record_runs(self):
        # Button is hidden — method kept for future re-enabling
        self._record_runs = not self._record_runs
        self._log(f"Run recording {'ON' if self._record_runs else 'OFF'}.", "info")

    def _start_all(self):
        max_concurrent = int(self.bot_settings.get("max_concurrent_sessions", 1))
        sem = threading.Semaphore(max_concurrent)
        for farm in self.farms:
            if farm.get("enabled", True):
                self._start_farm(farm, _semaphore=sem)

    def _stop_all(self):
        for farm in self.farms:
            self._stop_farm(farm)
        self._stop_timer()

    def _farm_to_tasks(self, farm: dict) -> list:
        """
        Load each enabled daily task JSON from tasks/ directory.
        Uses DAILY_TASKS list — each entry maps to a tasks/<key>.json file.
        """
        import json as _json
        from pathlib import Path

        tasks_dir = get_resource_dir() / self.bot_settings.get("tasks_dir", "tasks")
        tasks     = []
        farm_tasks = farm.get("tasks", {})
        daily_cfg  = farm_tasks.get("daily_tasks", {})

        if not daily_cfg.get("enabled", True):
            self._log("  Daily Tasks disabled — skipping all", "warn")
        else:
            for key, label, default_on in DAILY_TASKS:
                if not daily_cfg.get(key, default_on):
                    self._log(f"  ⏭ Skipping {label} (disabled)", "info")
                    continue
                json_file = tasks_dir / f"{key}.json"
                if json_file.exists():
                    try:
                        with open(json_file) as f:
                            data = _json.load(f)
                        actions = data.get("actions", data) if isinstance(data, dict) else data
                        tasks.append({"name": label, "actions": actions})
                        self._log(f"  ✓ {label} — {len(actions)} actions", "info")
                    except Exception as e:
                        self._log(f"  ✗ Failed to load {json_file.name}: {e}", "error")
                else:
                    self._log(f"  ⚠ {label} — tasks/{key}.json not found, skipping", "warn")

        # ── Rally tasks ───────────────────────────────────────────────
        rally_cfg = farm_tasks.get("rally", {})
        if rally_cfg.get("enabled", True):
            for key, label, json_key in RALLY_TASKS:
                if not rally_cfg.get(key, True):
                    self._log(f"  ⏭ Skipping {label} (disabled)", "info")
                    continue
                json_file = tasks_dir / f"{json_key}.json"
                if json_file.exists():
                    try:
                        with open(json_file) as f:
                            data = _json.load(f)
                        actions = data.get("actions", data) if isinstance(data, dict) else data
                        # Repeat create_rally up to max_rallies_per_day times;
                        # rally_count_check inside the JSON aborts each run once the limit is hit
                        repeat = int(rally_cfg.get("max_rallies_per_day", 1)) if key == "create_rally" else 1
                        for i in range(repeat):
                            tasks.append({"name": f"{label} ({i + 1}/{repeat})", "actions": actions})
                        self._log(f"  ✓ {label} — {len(actions)} actions x{repeat}", "info")
                    except Exception as e:
                        self._log(f"  ✗ Failed to load {json_file.name}: {e}", "error")
                else:
                    self._log(f"  ⚠ {label} — tasks/{json_key}.json not found, skipping", "warn")
        else:
            self._log("  Rally disabled — skipping all rally tasks", "warn")

        # ── Gathering tasks ───────────────────────────────────────────
        gather_cfg = farm_tasks.get("gathering", {})
        if gather_cfg.get("enabled", False):
            max_formations = int(gather_cfg.get("max_formations", 3))

            # Map priority resource names to GATHER_TASKS setting keys
            _priority_map = {
                "wood":        "collect_wood",
                "food":        "collect_food",
                "electricity": "collect_electricity",
                "zent":        "collect_zents",
            }

            # Load saved priority order; fall back to GATHER_TASKS order if missing
            priority_order = []
            priority_file = Path("logs/resource_priority.json")
            if priority_file.exists():
                try:
                    with open(priority_file) as pf:
                        priority_order = _json.load(pf).get("priority", [])
                except Exception:
                    pass

            def _priority_rank(task_entry):
                task_key = task_entry[0]
                for rank, resource in enumerate(priority_order):
                    if _priority_map.get(resource) == task_key:
                        return rank
                return len(priority_order)  # unknown resources go last

            # Build pool of enabled tasks with their loaded actions, sorted by priority
            gather_pool = []
            for key, label, json_key, default_on in GATHER_TASKS:
                if not gather_cfg.get(key, default_on):
                    self._log(f"  ⏭ Skipping {label} (disabled)", "info")
                    continue
                json_file = tasks_dir / f"{json_key}.json"
                if json_file.exists():
                    try:
                        with open(json_file) as f:
                            data = _json.load(f)
                        actions = data.get("actions", data) if isinstance(data, dict) else data
                        gather_pool.append((key, label, actions))
                        self._log(f"  ✓ {label} loaded — {len(actions)} actions", "info")
                    except Exception as e:
                        self._log(f"  ✗ Failed to load {json_file.name}: {e}", "error")
                else:
                    self._log(f"  ⚠ {label} — tasks/{json_key}.json not found, skipping", "warn")

            if gather_pool:
                gather_pool.sort(key=_priority_rank)
                priority_names = [label for _, label, _ in gather_pool]
                self._log(f"  ↑ Gather priority: {' > '.join(priority_names)}", "info")

                # Cycle through pool in priority order until max_formations runs are queued
                for i in range(max_formations):
                    key, label, actions = gather_pool[i % len(gather_pool)]
                    run_num = i + 1
                    tasks.append({"name": f"{label} ({run_num}/{max_formations})",
                                  "actions": actions,
                                  "farm_settings": {"gathering": gather_cfg}})
                self._log(f"  ✓ Queued {max_formations} gather run(s)", "info")
        else:
            self._log("  Gathering disabled — skipping all gathering tasks", "warn")

        return tasks

    # ══════════════════════════════════════════════════════════════════════
    # Preview + Log
    # ══════════════════════════════════════════════════════════════════════

    def _manual_preview(self):
        threading.Thread(target=self._do_preview, daemon=True).start()

    def _do_preview(self):
        try:
            engine = next((e for e in self.engines.values()
                           if e.bot and e.bot.is_connected()), None)
            if not engine:
                return
            img = engine.get_screenshot()
            if not img:
                return
            img.thumbnail((312, 175), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.after(0, lambda: self._set_preview(photo))
        except Exception:
            pass

    def _set_preview(self, photo):
        self.preview_canvas.configure(image=photo, text="")
        self.preview_canvas._image = photo

    def _log(self, message: str, level: str = "info"):
        def _do():
            self.log_box.configure(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_box.insert("end", f"[{ts}] {message}\n", level)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, _do)

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    # ── Timer ─────────────────────────────────────────────────────────────

    def _start_timer(self):
        if not self._timer_running:
            self._bot_start_time = datetime.now()
            self._timer_running = True
            self._tick_timer()
        self._update_status_display()
        self._update_run_btn()

    def _stop_timer(self):
        self._timer_running = False
        if self._timer_after_id:
            self.after_cancel(self._timer_after_id)
            self._timer_after_id = None
        self._bot_start_time = None
        self._cycle_complete_time = None
        if hasattr(self, "_timer_labels"):
            for lbl in self._timer_labels.values():
                lbl.configure(text="0")
        self._update_status_display()
        self._update_run_btn()

    def _tick_timer(self):
        if not self._timer_running or not self._bot_start_time:
            return
        elapsed = (datetime.now() - self._bot_start_time).total_seconds()
        days  = int(elapsed // 86400)
        hours = int((elapsed % 86400) // 3600)
        mins  = int((elapsed % 3600) // 60)
        secs  = int(elapsed % 60)
        if hasattr(self, "_timer_labels"):
            self._timer_labels["days"].configure(text=str(days))
            self._timer_labels["hrs"].configure(text=str(hours))
            self._timer_labels["min"].configure(text=str(mins))
            self._timer_labels["sec"].configure(text=str(secs))
        self._update_status_display()

        # Auto-restart: if a cycle finished, no farms are active, and the
        # minimum cycle time has elapsed, kick off the next cycle.
        if self._cycle_complete_time:
            no_farms_active = not any(
                getattr(e, "_thread", None) and e._thread.is_alive()
                for e in self.engines.values()
            )
            if no_farms_active:
                min_cycle_sec = int(self.bot_settings.get("minimum_cycle_time", 120)) * 60
                elapsed_since = (datetime.now() - self._cycle_complete_time).total_seconds()
                if elapsed_since >= min_cycle_sec:
                    self._log("⟳ Cycle time elapsed — starting next cycle...", "accent")
                    self._cycle_complete_time = None
                    self._start_all()

        self._timer_after_id = self.after(1000, self._tick_timer)

    def _update_status_display(self):
        if not hasattr(self, "_status_bot_lbl"):
            return
        running = self._timer_running
        self._status_bot_lbl.configure(
            text=f"Bot Status: {'Running' if running else 'Stopped'}",
            text_color=C["green"] if running else C["text2"],
        )
        active = sum(
            1 for e in self.engines.values()
            if hasattr(e, "state") and e.state.value == "running"
        )
        total = len(self.farms)
        self._status_farms_lbl.configure(text=f"Active Farms: {active} / {total}")

        min_cycle = int(self.bot_settings.get("minimum_cycle_time", 120))
        self._status_cycle_lbl.configure(text=f"Min Cycle Time: {min_cycle} min")

        if self._cycle_complete_time:
            elapsed_min = (datetime.now() - self._cycle_complete_time).total_seconds() / 60
            remaining = max(0.0, min_cycle - elapsed_min)
            if remaining <= 0:
                self._status_next_lbl.configure(
                    text="Next Cycle: Ready now", text_color=C["green"])
            else:
                h = int(remaining // 60)
                m = int(remaining % 60)
                label = f"In {h}h {m}m" if h else f"In {m}m"
                self._status_next_lbl.configure(
                    text=f"Next Cycle: {label}", text_color=C["text2"])
        else:
            self._status_next_lbl.configure(text="Next Cycle: —", text_color=C["text2"])

    # ══════════════════════════════════════════════════════════════════════
    # Shared UI Helpers
    # ══════════════════════════════════════════════════════════════════════

    def _page_header(self, parent, title: str, subtitle: str = ""):
        hdr = ctk.CTkFrame(parent, fg_color=C["panel"], corner_radius=0, height=58)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        inner = ctk.CTkFrame(hdr, fg_color="transparent")
        inner.pack(side="left", padx=16, pady=8)
        ctk.CTkLabel(inner, text=title, font=FT2, text_color=C["text"]).pack(anchor="w")
        if subtitle:
            ctk.CTkLabel(inner, text=subtitle, font=FS, text_color=C["text3"]).pack(anchor="w")
        return hdr

    def _card(self, parent, title: str) -> ctk.CTkFrame:
        outer = ctk.CTkFrame(parent, fg_color=C["panel"], corner_radius=8,
                              border_width=1, border_color=C["border"])
        outer.pack(fill="x", pady=6)
        ctk.CTkLabel(outer, text=title, font=FT2,
                     text_color=C["accent"]).pack(anchor="w", padx=14, pady=(12, 6))
        ctk.CTkFrame(outer, height=1, fg_color=C["border"]).pack(fill="x", padx=14)
        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=8)
        return inner

    def _setting_row(self, parent, label: str, stype: str,
                     key: str, options: list = None):
        row = ctk.CTkFrame(parent, fg_color="transparent", height=42)
        row.pack(fill="x", pady=1)
        row.pack_propagate(False)
        ctk.CTkLabel(row, text=label, font=FT, text_color=C["text2"]).pack(side="left")
        val = self.bot_settings.get(key, "")

        if stype == "toggle":
            var = ctk.BooleanVar(value=bool(val))
            ctk.CTkSwitch(row, text="", variable=var, width=46,
                           progress_color=C["accent"], button_color=C["white"],
                           command=lambda k=key, v=var: self._update_bot_setting(k, v.get())
                           ).pack(side="right")
        elif stype == "select":
            var = ctk.StringVar(value=str(val))
            ctk.CTkOptionMenu(row, values=options or [], variable=var, width=130,
                               fg_color=C["card"], button_color=C["border"],
                               text_color=C["text"], dropdown_fg_color=C["panel2"],
                               command=lambda v, k=key: self._update_bot_setting(k, v)
                               ).pack(side="right")
        elif stype == "number":
            var = ctk.StringVar(value=str(val))
            e = ctk.CTkEntry(row, textvariable=var, width=90, fg_color=C["card"],
                              border_color=C["border"], text_color=C["text"], font=FM)
            e.pack(side="right")
            e.bind("<FocusOut>",   lambda ev, k=key, v=var: self._update_bot_setting(k, v.get()))
            e.bind("<KeyRelease>", lambda ev, k=key, v=var: self._update_bot_setting(k, v.get()))
        elif stype == "path":
            var = ctk.StringVar(value=str(val))
            e = ctk.CTkEntry(row, textvariable=var, width=220, fg_color=C["card"],
                              border_color=C["border"], text_color=C["text"], font=FM)
            e.pack(side="right", padx=(0, 4))
            ctk.CTkButton(row, text="✏", width=28, height=28,
                          fg_color="transparent", text_color=C["accent"],
                          hover_color=C["border"],
                          command=lambda k=key, v=var: self._browse_path(k, v)
                          ).pack(side="right")

        ctk.CTkFrame(parent, height=1, fg_color=C["border"]).pack(fill="x", pady=1)

    def _update_bot_setting(self, key: str, value):
        # Cast numeric fields to correct types
        if key in ("vision_confidence", "loop_delay", "post_launch_wait",
                   "game_load_wait", "emulator_boot_timeout", "max_farm_timeout",
                   "max_retries", "loop_delay"):
            try:
                value = float(value)
                if key == "vision_confidence":
                    value = max(0.5, min(0.99, value))  # clamp 0.5–0.99, never 1.0
            except (ValueError, TypeError):
                return  # ignore invalid input mid-typing
        self.bot_settings[key] = value
        # When emulator type changes, auto-detect install path then rebuild the page
        if key == "emulator":
            found = find_emulator_install(value)
            if found:
                self.bot_settings["emulator_path"] = found
                self._log(f"[Bot Settings] {value} found at: {found}", "success")
            else:
                example = emulator_path_example(value)
                self._log(
                    f"[Bot Settings] {value} not found automatically. "
                    f"Set the path manually in Bot Settings. Example: {example}",
                    "warn",
                )
            self._build_page_bot_settings()

    def _browse_path(self, key: str, var: ctk.StringVar):
        path = filedialog.askdirectory()
        if path:
            var.set(path)
            self.bot_settings[key] = path

    def _action_row(self, parent, description: str, btn_label: str, cmd):
        row = ctk.CTkFrame(parent, fg_color=C["card"], corner_radius=6,
                            border_width=1, border_color=C["border"])
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text=description, font=FT,
                     text_color=C["text2"]).pack(side="left", padx=14, pady=12)
        self._btn(row, btn_label, cmd, C["accent"]).pack(side="right", padx=12, pady=8)

    def _btn(self, parent, text, cmd, color=None, small=False, full=False) -> ctk.CTkButton:
        dark_text = color in (C["accent"], C["green"], C["red"], C["blue"])
        return ctk.CTkButton(
            parent, text=text, command=cmd,
            font=FS if small else FB,
            height=28 if small else 34,
            fg_color=color or C["card"],
            hover_color=C["border"],
            text_color=C["bg"] if dark_text else C["text"],
            corner_radius=4,
            width=140 if full else 0,
        )

    # ── Setup Wizard ────────────────────────────────────────────────────────

    def _show_setup_wizard(self):
        win = ctk.CTkToplevel(self)
        win.title("Welcome to Last Z Bot")
        win.geometry("500x460")
        win.resizable(False, False)
        win.configure(fg_color=C["panel"])

        # Center over main window
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - 500) // 2
        y = self.winfo_y() + (self.winfo_height() - 460) // 2
        win.geometry(f"+{x}+{y}")
        win.after(100, win.grab_set)

        ctk.CTkLabel(win, text="Welcome to Last Z Bot",
                     font=("Segoe UI", 20, "bold"), text_color=C["accent"]).pack(pady=(30, 6))
        ctk.CTkLabel(win, text="Let's get you set up. You can change these\nsettings later in the Bot Settings tab.",
                     font=("Segoe UI", 12), text_color=C["text2"], justify="center").pack(pady=(0, 24))

        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="x", padx=40)

        # Emulator selector
        ctk.CTkLabel(body, text="Which emulator are you using?",
                     font=("Segoe UI", 13, "bold"), text_color=C["text"]).pack(anchor="w", pady=(0, 6))

        emu_var  = ctk.StringVar(value=self.bot_settings.get("emulator", "MEmu"))
        path_var = ctk.StringVar(value="")

        ctk.CTkOptionMenu(body, values=["MEmu", "LDPlayer", "Nox"],
                          variable=emu_var, fg_color=C["card"],
                          button_color=C["accent"], button_hover_color=C["accent2"],
                          command=lambda v: _detect(v)
                          ).pack(fill="x", pady=(0, 18))

        # Path field
        ctk.CTkLabel(body, text="Emulator install path:",
                     font=("Segoe UI", 13, "bold"), text_color=C["text"]).pack(anchor="w", pady=(0, 6))

        path_row = ctk.CTkFrame(body, fg_color="transparent")
        path_row.pack(fill="x")

        path_entry = ctk.CTkEntry(path_row, textvariable=path_var,
                                  fg_color=C["card"], border_color=C["border"])
        path_entry.pack(side="left", fill="x", expand=True)

        def browse():
            from tkinter import filedialog
            d = filedialog.askdirectory(title="Select emulator folder")
            if d:
                path_var.set(d)
                status_label.configure(text="")

        ctk.CTkButton(path_row, text="Browse", width=80,
                      fg_color=C["card"], hover_color=C["nav_sel"],
                      border_width=1, border_color=C["border"],
                      command=browse).pack(side="left", padx=(8, 0))

        # Status label — shown below the path row after auto-detect
        status_label = ctk.CTkLabel(body, text="", font=("Segoe UI", 11),
                                    wraplength=420, justify="left")
        status_label.pack(anchor="w", pady=(6, 0))

        def _detect(emu_type: str):
            found = find_emulator_install(emu_type)
            if found:
                path_var.set(found)
                status_label.configure(
                    text=f"✓ Found automatically: {found}",
                    text_color="#4caf50",
                )
            else:
                path_var.set("")
                example = emulator_path_example(emu_type)
                status_label.configure(
                    text=(
                        f"Could not find {emu_type} automatically.\n"
                        f"Use Browse to locate your install folder.\n"
                        f"Example: {example}"
                    ),
                    text_color="#ff9800",
                )

        # Run detection immediately for the default emulator
        _detect(emu_var.get())

        def finish():
            self.bot_settings["emulator"] = emu_var.get()
            self.bot_settings["emulator_path"] = path_var.get()
            self._save_data()
            self._refresh_bot_settings_ui()
            win.destroy()

        ctk.CTkButton(win, text="Get Started", width=200,
                      fg_color=C["accent"], hover_color=C["accent2"],
                      text_color="#000000", font=("Segoe UI", 13, "bold"),
                      command=finish).pack(pady=30)

    def _refresh_bot_settings_ui(self):
        self._build_page_bot_settings()

    # ── Update Checker ─────────────────────────────────────────────────────

    def _check_for_updates_async(self):
        # Skip update checking unless configured.
        if not GITHUB_OWNER or not GITHUB_REPO or "YOUR_" in GITHUB_OWNER or "YOUR_" in GITHUB_REPO:
            return
        threading.Thread(target=self._check_for_updates, daemon=True).start()

    def _check_for_updates(self):
        try:
            result = check_for_update(
                version.__version__,
                GITHUB_OWNER,
                GITHUB_REPO,
                UPDATE_ASSET_NAME_CONTAINS,
            )
            if not result:
                return
            remote = result["remote_version"]
            asset_url = result["asset_url"]

            self.after(0, lambda: self._log(f"Update {remote} found — downloading in background...", "info"))

            file_name = f"LastZBot-{remote}-Setup.exe" if sys.platform.startswith("win") else f"{GITHUB_REPO}-{remote}.dmg"
            success = download_and_launch(asset_url, file_name, args=["/S"])

            if not success:
                self.after(0, lambda: self._log("Update download failed — will retry next launch.", "warn"))
                return

            # Stop all running engines before the installer replaces the exe
            def apply_update():
                self._log(f"Update {remote} downloaded — installing now...", "success")
                for engine in self.engines.values():
                    try:
                        if engine.state == EngineState.RUNNING:
                            engine.stop()
                        engine.disconnect()
                    except Exception:
                        pass
                self._save_data()
                self.destroy()

            self.after(0, apply_update)
        except Exception:
            pass

    # ── Close ─────────────────────────────────────────────────────────────

    def _on_close(self):
        for engine in self.engines.values():
            try:
                if engine.state == EngineState.RUNNING:
                    engine.stop()
                engine.disconnect()
            except Exception:
                pass
        self._save_data()
        self.destroy()


if __name__ == "__main__":
    # Single-instance guard — prevent multiple GUI windows running at once.
    if sys.platform.startswith("win"):
        import ctypes
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "LastZBot_SingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(
                0,
                "Last Z Bot is already running.\nCheck your taskbar or system tray.",
                "Already Running",
                0x40 | 0x1000,  # MB_ICONINFORMATION | MB_SYSTEMMODAL
            )
            sys.exit(0)

    app = BotApp()
    app.mainloop()
