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
from paths import ensure_app_dir, get_farms_path
from updater import check_for_update, download_and_launch

# Update these values for your repository.
GITHUB_OWNER = "Pretzel34"
GITHUB_REPO = "lastz-bot"
# The substring to match inside the GitHub release asset name.
# Example: "LastZBot-0.1.0.exe" will be matched by "LastZBot".
UPDATE_ASSET_NAME_CONTAINS = "LastZBot"

MEMUC_PATH = r"C:\Program Files\Microvirt\MEmu\memuc.exe"
LASTZ_PKG  = "com.readygo.barrel.gp"
LASTZ_ACT  = "com.im30.aps.debug.UnityPlayerActivityCustom"

try:
    from bot_engine import BotEngine, EngineState, load_config, save_config
    from action_executor import ActionStatus
    from launcher import MEmuLauncher, index_to_port
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
    ("complete_radar",       "Complete Radar",       True),
    ("Collect Fuel",         "Collect Fuel",         True),
    ("Read Mail",            "Read Mail",            True),
    ("collect_recruits",     "Collect Recruits",     True),
]

# Rally task JSON files — (setting_key, label, json_filename)
# setting_key must match the key in the "rally" settings block above
RALLY_TASKS = [
    ("quick_join_rally", "Quick Join Rally", "quick_join_rally"),
    ("create_rally",     "Create Rally",     "start_rally_on_boomer"),
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
            {"key": "boomer_level",      "label": "Boomer Level",          "type": "spinner", "default": 5, "min": 1, "max": 10},
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


def new_farm(index: int, port: int = None) -> dict:
    import copy
    f = copy.deepcopy(DEFAULT_FARM)
    f["name"] = f"Farm {index}"
    f["emu_index"] = index
    f["port"] = port or (21503 + (index - 1) * 10)
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

        self._load_data()
        self._build_ui()
        self._nav_select("run_bot")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Check for updates in background (non-blocking).
        self._check_for_updates_async()

    # ── Data ──────────────────────────────────────────────────────────────

    def _default_bot_settings(self) -> dict:
        return {
            "emulator": "MEmu",
            "memu_path": r"C:\Program Files\Microvirt\MEmu",
            "force_kill_hung": True,
            "auto_run_startup": False,
            "max_farm_timeout": 40,
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
        }

    def _load_data(self):
        p = self._data_path
        if p.exists():
            try:
                with open(p) as f:
                    data = json.load(f)
                self.farms = data.get("farms", [])
                self.bot_settings = {**self._default_bot_settings(),
                                     **data.get("bot_settings", {})}
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
                                 "Connect instances and run your farm sequences.")
        ctrl = ctk.CTkFrame(hdr, fg_color="transparent")
        ctrl.pack(side="right", pady=10, padx=12)
        self._btn(ctrl, "⚡ START ALL", self._start_all, C["accent"]).pack(side="left", padx=4)
        self._btn(ctrl, "■ STOP ALL",  self._stop_all,  C["red"]).pack(side="left", padx=4)

        ctk.CTkFrame(ctrl, width=1, fg_color=C["border"]).pack(
            side="left", fill="y", padx=8, pady=4)

        self.rec_toggle_btn = ctk.CTkButton(
            ctrl, text="⏺ Record Runs", command=self._toggle_record_runs,
            font=("Segoe UI Semibold", 10), height=28, corner_radius=4,
            fg_color=C["panel2"], hover_color=C["border"],
            text_color=C["text"], width=110,
        )
        self.rec_toggle_btn.pack(side="left", padx=4)

        body = ctk.CTkFrame(pg, fg_color="transparent")
        body.pack(fill="both", expand=True)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True, padx=(16, 8), pady=8)

        ctk.CTkLabel(left, text="INSTANCES", font=FT2,
                     text_color=C["accent"]).pack(anchor="w", pady=(4, 8))

        self.run_scroll = ctk.CTkScrollableFrame(left, fg_color=C["panel"],
                                                  corner_radius=8)
        self.run_scroll.pack(fill="both", expand=True)
        self._rebuild_run_list()

        right = ctk.CTkFrame(body, fg_color=C["panel"], corner_radius=8, width=340)
        right.pack(side="right", fill="y", padx=(0, 16), pady=8)
        right.pack_propagate(False)

        ctk.CTkLabel(right, text="LIVE PREVIEW", font=FT2,
                     text_color=C["accent"]).pack(anchor="w", padx=14, pady=(12, 4))

        self.preview_canvas = ctk.CTkLabel(
            right, text="No preview\n(connect first)",
            font=FM, text_color=C["text3"],
            fg_color=C["panel2"], corner_radius=6, width=312, height=175
        )
        self.preview_canvas.pack(padx=14, pady=4)

        self._btn(right, "⟳ Refresh Preview",
                  self._manual_preview).pack(padx=14, pady=4, fill="x")

        ctk.CTkFrame(right, height=1, fg_color=C["border"]).pack(fill="x", padx=14, pady=6)

        ctk.CTkLabel(right, text="ACTIVITY LOG", font=FT2,
                     text_color=C["accent"]).pack(anchor="w", padx=14, pady=(4, 4))

        log_bg = ctk.CTkFrame(right, fg_color=C["panel2"], corner_radius=6)
        log_bg.pack(fill="both", expand=True, padx=14, pady=(0, 4))

        self.log_box = tk.Text(
            log_bg, font=FSM, bg=C["panel2"], fg=C["text3"],
            relief="flat", bd=0, wrap="word",
            state="disabled", padx=6, pady=6
        )
        self.log_box.pack(fill="both", expand=True, padx=2, pady=2)
        self.log_box.tag_config("error",   foreground=C["red"])
        self.log_box.tag_config("success", foreground=C["green"])
        self.log_box.tag_config("warn",    foreground=C["yellow"])
        self.log_box.tag_config("info",    foreground=C["text3"])
        self.log_box.tag_config("accent",  foreground=C["accent"])

        self._btn(right, "🗑 Clear Log", self._clear_log).pack(
            padx=14, pady=(0, 8), fill="x")

    def _rebuild_run_list(self):
        for w in self.run_scroll.winfo_children():
            w.destroy()
        for farm in self.farms:
            self._run_farm_row(self.run_scroll, farm)

    def _run_farm_row(self, parent, farm: dict):
        row = ctk.CTkFrame(parent, fg_color=C["card"], corner_radius=6,
                           border_width=1, border_color=C["border"])
        row.pack(fill="x", padx=8, pady=4)

        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", padx=12, pady=10, fill="x", expand=True)

        ctk.CTkLabel(left,
            text=f" {farm['emu_index']:02d} ",
            font=("Consolas", 11, "bold"),
            text_color=C["accent"], fg_color=C["accent_dim"], corner_radius=4
        ).pack(side="left", padx=(0, 10))

        info = ctk.CTkFrame(left, fg_color="transparent")
        info.pack(side="left")
        ctk.CTkLabel(info, text=farm["name"], font=FB, text_color=C["text"]).pack(anchor="w")
        ctk.CTkLabel(info, text=f"Port: {farm['port']}  |  Emu ID: {farm['emu_index']}",
                     font=FSM, text_color=C["text3"]).pack(anchor="w")

        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="right", padx=12, pady=10)

        enabled = farm.get("enabled", True)
        status_lbl = ctk.CTkLabel(right,
            text="● ACTIVE" if enabled else "● DISABLED",
            font=FSM, text_color=C["green"] if enabled else C["muted"])
        status_lbl.pack(side="left", padx=(0, 12))

        self._btn(right, "▶ START",
                  lambda f=farm: self._start_farm(f), C["green"], small=True).pack(side="left", padx=2)
        self._btn(right, "■ STOP",
                  lambda f=farm: self._stop_farm(f),  C["red"],   small=True).pack(side="left", padx=2)

    # ══════════════════════════════════════════════════════════════════════
    # PAGE: Bot Settings
    # ══════════════════════════════════════════════════════════════════════

    def _build_page_bot_settings(self):
        pg = self.pages["bot_settings"]
        self._page_header(pg, "🔧  Bot Settings",
                           "Global configuration for all farm instances.")

        scroll = ctk.CTkScrollableFrame(pg, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=16, pady=8)

        card = self._card(scroll, "Emulator Configuration")
        self._setting_row(card, "Emulator", "select", "emulator",
                          ["MEmu", "LDPlayer", "Nox"])
        self._setting_row(card, "MEmu Path", "path", "memu_path")
        self._setting_row(card, "Force Kill Hung Emulator", "toggle", "force_kill_hung")
        self._setting_row(card, "Auto Run on Startup",      "toggle", "auto_run_startup")
        self._setting_row(card, "Auto Arrange Emulator",    "toggle", "auto_arrange")
        self._setting_row(card, "Max Farm Timeout (min)",   "number", "max_farm_timeout")

        card2 = self._card(scroll, "Bot Behavior")
        # Timing card
        card_t = self._card(pg, "⏱  Timing & Load Waits")
        self._setting_row(card_t, "Emulator Boot Timeout (sec)",  "number", "emulator_boot_timeout")
        self._setting_row(card_t, "Game Load Wait (sec)",         "number", "game_load_wait")
        self._setting_row(card_t, "Post-Launch Wait (sec)",       "number", "post_launch_wait")

        self._setting_row(card2, "Loop Tasks Continuously", "toggle", "loop_tasks")
        self._setting_row(card2, "Loop Delay (seconds)",    "number", "loop_delay")
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

        # Build categories
        farm_key = f"farm_{idx}"
        if farm_key not in self._expanded_cats:
            self._expanded_cats[farm_key] = {}

        self._farm_widget_refs = {}

        for cat in TASK_CATEGORIES:
            self._build_cat_section(scroll, farm, idx, cat, farm_key)

    def _build_cat_section(self, parent, farm, farm_idx, cat, farm_key):
        cat_key = cat["key"]
        expanded = self._expanded_cats[farm_key].get(cat_key, False)

        outer = ctk.CTkFrame(parent, fg_color=C["panel"], corner_radius=8,
                              border_width=1, border_color=C["border"])
        outer.pack(fill="x", pady=4)

        # Header row
        hdr = ctk.CTkFrame(outer, fg_color="transparent", height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        ctk.CTkLabel(hdr, text="⠿", font=("Segoe UI", 14),
                     text_color=C["muted"]).pack(side="left", padx=(10, 4))
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
        self.farms.append(new_farm(next_idx))
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

    def _connect_farm(self, farm: dict):
        if not BOT_AVAILABLE:
            messagebox.showerror("Error", f"Bot modules missing:\n{IMPORT_ERROR}")
            return

        idx   = farm["emu_index"] - 1   # GUI is 1-based, MEmu is 0-based
        port  = farm["port"]
        name  = farm["name"]
        self._log(f"Launching {name} (MEmu index {idx}, port {port})...", "accent")

        def do():
            try:
                # Step 1 — launch emulator instance + Last Z
                def launch_log(msg, level="info"):
                    self.after(0, lambda m=msg, l=level: self._log(m, l))

                # memu_path stores the folder; append memuc.exe
                import os
                memu_folder = self.bot_settings.get("memu_path", 
                    r"C:\Program Files\Microvirt\MEmu")
                memuc_exe = os.path.join(memu_folder, "memuc.exe")
                boot_timeout = int(float(self.bot_settings.get("emulator_boot_timeout", 180)))
                game_wait    = int(float(self.bot_settings.get("game_load_wait", 20)))
                post_wait    = int(float(self.bot_settings.get("post_launch_wait", 5)))

                launcher = MEmuLauncher(
                    memuc_path=memuc_exe,
                    package=LASTZ_PKG,
                    activity=LASTZ_ACT,
                    log_callback=launch_log,
                    boot_timeout=boot_timeout,
                    game_timeout=game_wait,
                )
                ok = launcher.launch_and_connect(index=idx, wait_for_game=True)
                if ok and post_wait > 0:
                    self.after(0, lambda s=post_wait: self._log(
                        f"  ⏱ Post-launch wait {s}s for game to settle...", "info"))
                    import time as _time
                    _time.sleep(post_wait)
                if not ok:
                    self.after(0, lambda: self._log(
                        f"✗ {name} failed to launch", "error"))
                    return

                # Step 2 — connect bot engine to the now-running instance
                engine = BotEngine("config.json")
                engine.config["emulator"]["ports"] = [port]
                engine.on_log = lambda m: self.after(0, lambda msg=m: self._log(msg))
                engine.on_state_change = lambda s: self.after(0, self._refresh_stats_table)
                engine.on_stats_update = lambda s: self.after(0, self._refresh_stats_table)

                # Reuse the already-connected bot from launcher
                launched_bot = launcher.get_bot(idx)
                if launched_bot:
                    from vision import VisionEngine
                    from action_executor import ActionExecutor
                    engine.bot     = launched_bot
                    engine.vision  = VisionEngine(
                        confidence_threshold=float(self.bot_settings.get("vision_confidence", 0.8)))
                    engine.executor = ActionExecutor(
                        bot=launched_bot,
                        vision=engine.vision,
                        template_dir=self.bot_settings.get("template_dir", "templates"),
                        log_callback=lambda m: self.after(0, lambda msg=m: self._log(msg)),
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

    def _start_farm(self, farm: dict):
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

        name = farm["name"]
        idx  = farm["emu_index"] - 1   # GUI 1-based → MEmu 0-based (MEmu-1=0, MEmu-2=1)
        port = 21503 + idx * 10        # Always derive port from idx: 0→21503, 1→21513, 2→21523
        farm["port"] = port            # Keep in sync
        self._log(f"▶ Starting {name} — memuc index={idx}, port={port}", "accent")

        def do():
            try:
                import os
                memu_folder = self.bot_settings.get("memu_path",
                    r"C:\Program Files\Microvirt\MEmu")
                memuc_exe = os.path.join(memu_folder, "memuc.exe")

                def launch_log(msg, level="info"):
                    self.after(0, lambda m=msg, l=level: self._log(m, l))

                boot_timeout = int(float(self.bot_settings.get("emulator_boot_timeout", 180)))
                game_wait    = int(float(self.bot_settings.get("game_load_wait", 20)))
                post_wait    = int(float(self.bot_settings.get("post_launch_wait", 5)))

                # Step 1 — launch emulator + Last Z
                launcher = MEmuLauncher(
                    memuc_path=memuc_exe,
                    package=LASTZ_PKG,
                    activity=LASTZ_ACT,
                    log_callback=launch_log,
                    boot_timeout=boot_timeout,
                    game_timeout=game_wait,
                )
                ok = launcher.launch_and_connect(index=idx, wait_for_game=True)
                if not ok:
                    self.after(0, lambda: self._log(f"✗ {name}: emulator launch failed", "error"))
                    return
                if post_wait > 0:
                    self.after(0, lambda s=post_wait: self._log(
                        f"  ⏱ Post-launch wait {s}s for game to settle...", "info"))
                    import time as _time
                    _time.sleep(post_wait)

                # Step 2 — build engine using the live bot connection
                from vision import VisionEngine
                from action_executor import ActionExecutor

                bot    = launcher.get_bot(idx)
                vision = VisionEngine(
                    confidence_threshold=float(self.bot_settings.get("vision_confidence", 0.8)))
                executor = ActionExecutor(
                    bot=bot,
                    vision=vision,
                    template_dir=self.bot_settings.get("template_dir", "templates"),
                    log_callback=lambda m: self.after(0, lambda msg=m: self._log(msg)),
                )
                engine = BotEngine("config.json")
                engine.config["emulator"]["ports"] = [port]
                engine.bot      = bot
                engine.vision   = vision
                engine.executor = executor
                engine.on_log          = lambda m: self.after(0, lambda msg=m: self._log(msg))
                engine.on_state_change = lambda s: self.after(0, self._refresh_stats_table)
                engine.on_stats_update = lambda s: self.after(0, self._refresh_stats_table)
                self.engines[farm["emu_index"]] = engine

                # Recording
                if self._record_runs:
                    from datetime import datetime as _datetime
                    rec_name = f"{name}_{_datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    engine.enable_recording(rec_name)

                # Step 3 — run startup dismiss sequence before tasks
                import json as _json
                from pathlib import Path
                tasks_dir = Path(self.bot_settings.get("tasks_dir", "tasks"))
                startup_file = tasks_dir / "startup_dismiss.json"
                if startup_file.exists():
                    try:
                        with open(startup_file) as sf:
                            startup_data = _json.load(sf)
                        startup_actions = startup_data.get("actions", startup_data) if isinstance(startup_data, dict) else startup_data
                        self.after(0, lambda: self._log("  ▶ Running startup dismiss...", "info"))
                        for act in startup_actions:
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

            except Exception as e:
                import traceback
                self.after(0, lambda err=traceback.format_exc(): self._log(f"Error: {err}", "error"))

        threading.Thread(target=do, daemon=True).start()

    def _stop_farm(self, farm: dict):
        engine = self.engines.get(farm["emu_index"])
        if engine:
            engine.stop()
            self._log(f"■ {farm['name']} stopped", "warn")

    def _toggle_record_runs(self):
        self._record_runs = not self._record_runs
        if self._record_runs:
            self.rec_toggle_btn.configure(fg_color=C["red"], text_color=C["bg"],
                                           text="⏺ Recording ON")
            self._log("Run recording ON — next farm start will record frames.", "accent")
        else:
            self.rec_toggle_btn.configure(fg_color=C["panel2"], text_color=C["text"],
                                           text="⏺ Record Runs")
            self._log("Run recording OFF.", "info")

    def _start_all(self):
        for farm in self.farms:
            if farm.get("enabled", True):
                self._start_farm(farm)

    def _stop_all(self):
        for farm in self.farms:
            self._stop_farm(farm)

    def _farm_to_tasks(self, farm: dict) -> list:
        """
        Load each enabled daily task JSON from tasks/ directory.
        Uses DAILY_TASKS list — each entry maps to a tasks/<key>.json file.
        """
        import json as _json
        from pathlib import Path

        tasks_dir = Path(self.bot_settings.get("tasks_dir", "tasks"))
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

            def prompt():
                if messagebox.askyesno(
                    "Update Available",
                    f"A new version ({remote}) is available. Download and install?",
                ):
                    file_name = f"{GITHUB_REPO}-{remote}.exe" if sys.platform.startswith("win") else f"{GITHUB_REPO}-{remote}.dmg"
                    success = download_and_launch(asset_url, file_name)
                    if not success:
                        messagebox.showerror("Update Failed", "Failed to download the installer.")
                    else:
                        self.destroy()

            self.after(0, prompt)
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
    app = BotApp()
    app.mainloop()
