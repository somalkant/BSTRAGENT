"""
B2 — Time & macro quality filters (plan_phase1.md B2)

  first_candle_filter : drop non-exempt strategies firing on the 09:15 bar (gap noise)
  after_last_entry    : no NEW entries at/after LAST_ENTRY_TIME (14:30)
  event_day           : RBI MPC / Union Budget / US FOMC days -> SKIP or RAISE_THRESHOLD

These run before the cluster/EV gates. The macro calendar is data-driven from
config/events_calendar.json (SKIP is the default mode).
"""
from __future__ import annotations

import json
import logging
from datetime import date, time

from config.settings import (
    FIRST_CANDLE_TIME, FIRST_CANDLE_EXEMPT, LAST_ENTRY_TIME,
    EVENTS_CALENDAR_FILE, EVENT_DAY_MODE,
)

log = logging.getLogger(__name__)


# ── macro event calendar ──────────────────────────────────────────────────────
def _load_events() -> dict[str, list[str]]:
    if not EVENTS_CALENDAR_FILE.exists():
        log.warning("[PIT_MISSING] events_calendar.json absent — no event days will be filtered")
        return {}
    raw = json.loads(EVENTS_CALENDAR_FILE.read_text())
    merged: dict[str, list[str]] = {}
    for event_type, dates in raw.items():
        if event_type.startswith("_"):
            continue
        for d in dates:
            merged.setdefault(d, []).append(event_type)
    return merged


_EVENTS = _load_events()


def reload_events() -> None:
    global _EVENTS
    _EVENTS = _load_events()


def event_day(trade_date: date) -> tuple[bool, list[str]]:
    events = _EVENTS.get(str(trade_date), [])
    return (bool(events), events)


def event_mode() -> str:
    """'SKIP' (default) or 'RAISE_THRESHOLD'."""
    return EVENT_DAY_MODE


# ── time filters ──────────────────────────────────────────────────────────────
def _to_time(bar_time) -> time:
    if isinstance(bar_time, time):
        return bar_time
    h, m = map(int, str(bar_time).split(":")[:2])
    return time(h, m)


def first_candle_filter(signals: dict, bar_time) -> dict:
    """
    On the 09:15 bar, keep only FIRST_CANDLE_EXEMPT strategies; drop the rest
    (pre-filtered, not rejected — no log entry). Other bars pass through unchanged.
    """
    if _to_time(bar_time) != _to_time(FIRST_CANDLE_TIME):
        return signals
    return {n: s for n, s in signals.items() if n in FIRST_CANDLE_EXEMPT}


def after_last_entry(bar_time) -> bool:
    """True if at/after LAST_ENTRY_TIME — no NEW entries (exits still run to close)."""
    return _to_time(bar_time) >= LAST_ENTRY_TIME
