"""Recorded verification of BotEngine soft-stop + 3-consecutive-failed-rally guard on one live instance."""
import copy, json, sys, threading, time
from datetime import datetime
sys.path.insert(0, r"C:\bot")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import run_and_record as rr
from bot_engine import BotEngine
from vision import VisionEngine
from action_executor import ActionExecutor

INDEX = 11
bot, title = rr.launch_instance("MEmu", r"C:\Program Files\Microvirt\MEmu", INDEX)
farm_settings = rr.load_farm_settings(INDEX)
rally = json.loads((rr.TASKS_DIR / "start_rally_on_boomer.json").read_text(encoding="utf-8"))
real_actions = rally["actions"]
fail_actions = copy.deepcopy(real_actions)
for a in fail_actions:
    if a.get("template") == "btn_team_up_boomer.png":      # force "no march" for a failing rally
        a["template"] = "btn_zz_force_fail.png"; a["fallback_template"] = "btn_zz_force_fail2.png"
assert fail_actions != real_actions
def rt(i, kind, acts): return {"name": f"Create Rally [{kind}] ({i})", "actions": acts, "rally_group": "create_rally"}
settle = {"name": "SETTLE (wait for game to finish loading, return to HQ)", "actions": [
    {"action": "wait", "seconds": 60.0}, {"action": "ensure_hq_view", "hq_btn": "btn_go_world.png", "world_btn": "btn_go_headquarters.png"},
    {"action": "wait", "seconds": 8.0}, {"action": "ensure_hq_view", "hq_btn": "btn_go_world.png", "world_btn": "btn_go_headquarters.png"},
    {"action": "wait", "seconds": 4.0}]}
ONLY = sys.argv[1] if len(sys.argv) > 1 else ""
tail = {"name": "TAIL marker task", "actions": [{"action": "wait", "seconds": 2.0}]}

def make_engine():
    e = BotEngine()
    e.bot = bot
    e.vision = VisionEngine(confidence_threshold=0.8)
    e.set_farm_settings(farm_settings)
    e.executor = ActionExecutor(bot=bot, vision=e.vision, template_dir=str(rr.ROOT / "templates"),
                                log_callback=e._log, farm_settings=farm_settings,
                                stop_event=e._stop_event, emulator_type="MEmu")
    e.on_log = lambda m: print(m, flush=True)
    return e

ts = datetime.now().strftime("%Y%m%d_%H%M%S")
out = rr.RECORDINGS_DIR / f"rally_guard_verify_{ts}.mp4"
rr.ensure_window_restored(title)
rec = rr.start_recording(title, out, rr.LOG_DIR / f"ffmpeg_{ts}.log"); time.sleep(2)
try:
    print("\n##### SCENARIO A: 1 real rally, then 4 forced-failure rallies, then TAIL — expect 3 fails then skip, TAIL runs #####", flush=True)
    e = make_engine()
    e.load_tasks([settle, rt(1, "REAL", real_actions)] + [rt(i, "FAIL", fail_actions) for i in range(2, 6)] + [tail])
    e.start(); e._thread.join(timeout=900)
    print("##### A done, thread alive:", e._thread.is_alive(), flush=True)

    if ONLY != "A":
        print("\n##### SCENARIO B: soft stop requested 15s into the first failing rally — expect it to finish, then stop; TAIL must NOT run #####", flush=True)
        e2 = make_engine()
        e2.load_tasks([rt(1, "FAIL", fail_actions), rt(2, "FAIL", fail_actions), tail])
        e2.start()
        threading.Timer(15, e2.request_soft_stop).start()
        e2._thread.join(timeout=600)
        print("##### B done, thread alive:", e2._thread.is_alive(), flush=True)
finally:
    rr.stop_recording(rec)
    print("RECORDING:", out, out.stat().st_size if out.exists() else 0, flush=True)
