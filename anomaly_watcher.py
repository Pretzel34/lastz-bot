"""
anomaly_watcher.py
===================
Real-time classification of bot log lines into likely-bug signals, so a
run can be flagged as it happens instead of only being reviewed after the
whole task finishes by reading back through the raw text log.

Two entry points:

    classify_log_line(line) -> Optional[Anomaly]
        Stateless, single-line classifier. Safe to call on any line in
        isolation. Covers the patterns that are unconditionally worth
        flagging regardless of what action produced them (unhandled
        exceptions, a required action aborting the task, emulator/ADB
        infra failures). Every pattern here corresponds to an actual bug
        or infra failure that was previously only found by hand.

    AnomalyWatcher().feed(line) -> Optional[Anomaly]
        Stateful wrapper that also tracks the most recently started
        action's params (to notice a *required* step quietly failing to
        find its target) and per-action-type rolling duration stats (to
        flag steps that take far longer than usual, e.g. an ADB stall).

Usage:
    from anomaly_watcher import classify_log_line, AnomalyWatcher

    watcher = AnomalyWatcher()
    for line in log_lines:
        anomaly = watcher.feed(line)
        if anomaly:
            print(anomaly.severity, anomaly.reason)
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Anomaly:
    severity: str   # "high" | "medium" | "low"
    reason: str
    line: str
    timestamp: Optional[str] = None


_TIMESTAMP_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")

# Ordered, context-free, always-worth-flagging patterns. First match wins.
_HIGH_PATTERNS = [
    (re.compile(r"Required action failed — aborting task"),
        "required action failed, task aborted"),
    (re.compile(r"✗\s*Exception:"),
        "unhandled exception during action execution"),
    (re.compile(r"error: \(-215"),
        "OpenCV assertion failure (template/crop size mismatch)"),
    (re.compile(r"farm thread hung|hung emulator"),
        "emulator/farm thread hung, force-killed by watchdog"),
    (re.compile(r"ADB lost|device .* not found", re.IGNORECASE),
        "ADB connection lost / device unreachable"),
    (re.compile(r"farm exceeded .* timeout"),
        "farm exceeded max runtime timeout"),
    (re.compile(r"emulator (relaunch|launch) failed"),
        "emulator failed to (re)launch"),
    (re.compile(r"watchdog failed to kill"),
        "watchdog could not recover a hung emulator"),
    (re.compile(r"Screen appears frozen/blank"),
        "screen frozen/blank — game force-restarted"),
]

_MEDIUM_PATTERNS = [
    (re.compile(r"backed out:"), "sub-task backed out early"),
    (re.compile(r"reached max iterations"),
        "loop_task/loop_until_template hit its iteration cap without finishing"),
]


def classify_log_line(line: str) -> Optional[Anomaly]:
    """Stateless single-line classifier — see module docstring."""
    ts_match = _TIMESTAMP_RE.match(line)
    timestamp = ts_match.group(1) if ts_match else None

    for pattern, reason in _HIGH_PATTERNS:
        if pattern.search(line):
            return Anomaly(severity="high", reason=reason, line=line.strip(), timestamp=timestamp)
    for pattern, reason in _MEDIUM_PATTERNS:
        if pattern.search(line):
            return Anomaly(severity="medium", reason=reason, line=line.strip(), timestamp=timestamp)
    return None


# Matches the "▶ action_type {params}" line action_executor.py logs at the
# start of every action (action_executor.py:262, _params_str at :5389-5392
# emits a plain str(dict), i.e. a valid Python literal).
_ACTION_START_RE = re.compile(r"▶\s*(\w+)\s*(\{.*\})?\s*$")
# Matches the final "✓/✗ <message> (Nms)" result line for an action.
_RESULT_LINE_RE = re.compile(r"([✓✗])\s.*\((\d+)ms\)\s*$")
_NOT_FOUND_RE = re.compile(r"not found", re.IGNORECASE)
# Wrapper actions (run_task/loop_task/loop_until_template) log their own
# "(Nms)" summary line reporting the CUMULATIVE time across every nested
# sub-action they ran — by the time that line appears, _last_action_type
# has already been overwritten by whichever nested action ran last, so
# attributing this duration to it would misattribute a normal ~60s
# multi-step sub-task as e.g. "'wait' took 60000ms". Recognize and skip these.
_WRAPPER_SUMMARY_RE = re.compile(
    r"\b(run_task|run_task_if_template|loop_task|loop_until_template|dispatch_fp_task):\s"
)


class AnomalyWatcher:
    """
    Stateful wrapper around classify_log_line(). Feed it every log line in
    order as it's produced (e.g. as a second consumer of the same on_log
    callback the GUI already uses) — it never needs to see a line twice
    and keeps only a small rolling window of state.
    """

    _WINDOW = 20            # rolling duration samples kept per action type
    _MIN_SAMPLES = 5        # don't flag outliers until there's a baseline
    _OUTLIER_FACTOR = 6.0   # duration must be this many times the median

    def __init__(self):
        self._last_action_type: Optional[str] = None
        self._last_action_params: dict = {}
        self._durations: dict[str, list[float]] = {}

    def feed(self, line: str) -> Optional[Anomaly]:
        anomaly = classify_log_line(line)
        if anomaly:
            return anomaly

        start_match = _ACTION_START_RE.search(line)
        if start_match:
            self._last_action_type = start_match.group(1)
            self._last_action_params = self._parse_params(start_match.group(2))
            return None

        result_match = _RESULT_LINE_RE.search(line)
        if not result_match:
            return None
        if _WRAPPER_SUMMARY_RE.search(line):
            return None

        icon, duration_str = result_match.group(1), result_match.group(2)
        duration_ms = float(duration_str)
        ts_match = _TIMESTAMP_RE.match(line)
        timestamp = ts_match.group(1) if ts_match else None

        # Quiet-required-miss: a step that defaults to required (or is
        # explicitly required) failed to find its target, wasn't guarded by
        # skip_task_if_not_found/skip_to_on_found (both intentionally-optional
        # patterns), and still ended in a "not found" style skip.
        required = self._last_action_params.get("required", True)
        intentionally_optional = (
            self._last_action_params.get("skip_task_if_not_found", False)
            or self._last_action_params.get("skip_to_on_found") is not None
        )
        if required and not intentionally_optional and icon == "✗" and _NOT_FOUND_RE.search(line):
            return Anomaly(
                severity="medium",
                reason=f"required step '{self._last_action_type}' did not find its target",
                line=line.strip(),
                timestamp=timestamp,
            )

        # Duration outlier vs this action type's own recent history.
        result = None
        if self._last_action_type:
            samples = self._durations.setdefault(self._last_action_type, [])
            if len(samples) >= self._MIN_SAMPLES:
                sorted_samples = sorted(samples)
                median = sorted_samples[len(sorted_samples) // 2]
                if median > 0 and duration_ms > median * self._OUTLIER_FACTOR:
                    result = Anomaly(
                        severity="low",
                        reason=(f"'{self._last_action_type}' took {duration_ms:.0f}ms, "
                                f"~{duration_ms / median:.1f}x the recent median ({median:.0f}ms)"),
                        line=line.strip(),
                        timestamp=timestamp,
                    )
            samples.append(duration_ms)
            if len(samples) > self._WINDOW:
                samples.pop(0)

        return result

    @staticmethod
    def _parse_params(params_repr: Optional[str]) -> dict:
        if not params_repr:
            return {}
        try:
            value = ast.literal_eval(params_repr)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
