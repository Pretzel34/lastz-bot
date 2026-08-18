"""
log_diagnostician.py
=====================
Phase 2 of the "discovery + diagnosis" idea: reads a completed gui log,
re-derives the same anomalies AnomalyWatcher flags live in the GUI (they
aren't persisted anywhere, so this re-scans the raw log rather than
depending on gui.py's in-memory anomaly panel), groups near-duplicate
anomalies into distinct patterns, and asks Claude to diagnose each pattern
— what likely broke, why, and where to look — instead of a human doing
that log-reading by hand.

Usage:
    python log_diagnostician.py                  # most recent gui_*.log
    python log_diagnostician.py --log path\to.log
    python log_diagnostician.py --date 20260806

Requires ANTHROPIC_API_KEY (or an `ant auth login` profile) in the
environment. Writes a markdown report to logs/diagnostics/ and prints it.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
from datetime import datetime

from anthropic import Anthropic

from anomaly_watcher import AnomalyWatcher, Anomaly

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
DIAG_DIR = os.path.join(LOG_DIR, "diagnostics")
CONTEXT_LINES_BEFORE = 6
MAX_EXAMPLES_PER_GROUP = 2
MODEL = "claude-sonnet-5"

# Strips instance-specific numbers/names out of an anomaly's reason string
# so "'wait' took 8213ms, ~7.1x the recent median (1150ms)" and "...took
# 9500ms, ~6.3x... (1500ms)" collapse into the same group.
_NUMERIC_RE = re.compile(r"\d+(\.\d+)?")
_QUOTED_RE = re.compile(r"'[^']*'")


def _group_key(anomaly: Anomaly) -> str:
    key = _NUMERIC_RE.sub("#", anomaly.reason)
    # Keep the quoted action/step name (it's the load-bearing identifier),
    # everything else numeric is instance noise.
    return f"{anomaly.severity}:{key}"


def load_lines(log_path: str) -> list[str]:
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


def find_anomalies(lines: list[str]) -> list[tuple[int, Anomaly]]:
    watcher = AnomalyWatcher()
    found = []
    for i, line in enumerate(lines):
        anomaly = watcher.feed(line)
        if anomaly:
            found.append((i, anomaly))
    return found


def group_anomalies(
    found: list[tuple[int, Anomaly]], lines: list[str]
) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for idx, anomaly in found:
        key = _group_key(anomaly)
        group = groups.setdefault(
            key, {"severity": anomaly.severity, "reason_sample": anomaly.reason,
                  "count": 0, "examples": []}
        )
        group["count"] += 1
        if len(group["examples"]) < MAX_EXAMPLES_PER_GROUP:
            start = max(0, idx - CONTEXT_LINES_BEFORE)
            excerpt = "".join(lines[start:idx + 1])
            group["examples"].append(excerpt)
    return groups


def build_prompt(groups: dict[str, dict]) -> str:
    sections = []
    for i, (key, g) in enumerate(sorted(
        groups.items(), key=lambda kv: (-{"high": 2, "medium": 1, "low": 0}[kv[1]["severity"]], -kv[1]["count"])
    ), start=1):
        examples = "\n---\n".join(g["examples"])
        sections.append(
            f"### Pattern {i} — severity={g['severity']}, occurrences={g['count']}\n"
            f"Representative reason: {g['reason_sample']}\n"
            f"Log excerpt(s) with a few lines of preceding context:\n```\n{examples}\n```"
        )
    return (
        "You are diagnosing recurring anomalies from an Android game-automation bot's "
        "run log. Each pattern below is a distinct group of similar flagged log lines "
        "(already deduplicated from possibly many occurrences). For EACH pattern, give:\n"
        "1. A one-line diagnosis of the likely root cause.\n"
        "2. A category: asset_drift (a template image no longer matches the game's UI), "
        "timing (a wait/settle issue — action fired before the screen was ready), "
        "logic_gap (the task JSON or code has a real bug/missing case), or "
        "unknown (not enough evidence in the excerpt to tell).\n"
        "3. Which task file, action type, or function is implicated, if identifiable from "
        "the log line's action name.\n"
        "4. A suggested next step to confirm or fix it.\n\n"
        "Be concise — a few sentences per pattern, not an essay. If two patterns are "
        "clearly the same underlying bug, say so and combine them.\n\n"
        + "\n\n".join(sections)
    )


def diagnose(groups: dict[str, dict]) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    client = Anthropic(api_key=api_key) if api_key else Anthropic()
    prompt = build_prompt(groups)
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in response.content if b.type == "text")


def latest_gui_log() -> str:
    candidates = sorted(glob.glob(os.path.join(LOG_DIR, "gui_*.log")))
    candidates = [c for c in candidates if "stdout" not in os.path.basename(c)]
    if not candidates:
        raise FileNotFoundError(f"No gui_*.log files found in {LOG_DIR}")
    return candidates[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", help="Path to a specific gui log file")
    parser.add_argument("--date", help="Date suffix, e.g. 20260806 -> logs/gui_20260806.log")
    args = parser.parse_args()

    if args.log:
        log_path = args.log
    elif args.date:
        log_path = os.path.join(LOG_DIR, f"gui_{args.date}.log")
    else:
        log_path = latest_gui_log()

    print(f"Analyzing: {log_path}")
    lines = load_lines(log_path)
    found = find_anomalies(lines)
    print(f"Found {len(found)} raw anomaly hits")

    if not found:
        print("No anomalies found — nothing to diagnose.")
        return

    groups = group_anomalies(found, lines)
    print(f"Grouped into {len(groups)} distinct patterns")

    report_body = diagnose(groups)

    os.makedirs(DIAG_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(DIAG_DIR, f"diagnosis_{stamp}.md")
    header = (
        f"# Log Diagnosis Report\n\n"
        f"Source log: `{log_path}`\n"
        f"Raw anomaly hits: {len(found)}  |  Distinct patterns: {len(groups)}\n\n---\n\n"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + report_body)

    print("\n" + header + report_body)
    print(f"\nSaved report -> {out_path}")


if __name__ == "__main__":
    main()
