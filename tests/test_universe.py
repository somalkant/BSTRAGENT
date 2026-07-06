"""
B2 universe / eligibility / data-integrity (plan_phase1.md B2).

Run:  python -m pytest tests/test_universe.py -v
"""
from __future__ import annotations

from datetime import date

from backtester.universe import (
    universe_for_year, in_universe, overnight_gap, data_integrity_ok,
    fno_eligible_short, long_eligible, liquidity_eligible, sector_of,
)
from config.settings import MIN_ADV_RS


def test_folder_presence_pit_universe():
    u2016 = universe_for_year(2016)
    u2025 = universe_for_year(2025)
    assert len(u2016) >= 300 and len(u2025) >= len(u2016)   # universe grows over time
    assert in_universe("RELIANCE", 2016)
    assert not in_universe("NOT-A-STOCK", 2016)


def test_overnight_gap():
    assert overnight_gap(120, 100) == 0.2
    assert overnight_gap(100, 0) == 0.0


def test_data_integrity_excludes_misscaled_day():
    # PIIND-like: normal ~950, corrupt day ~82 -> >40% deviation -> excluded
    assert data_integrity_ok(82.0, 950.0, "PIIND", date(2018, 12, 10)) is False
    # normal day within tolerance -> kept
    assert data_integrity_ok(960.0, 950.0) is True
    # no trailing baseline -> kept (can't judge)
    assert data_integrity_ok(100.0, 0.0) is True


def test_eligibility_graceful_fallback_when_files_absent():
    # PIT files not present in the repo -> Phase-1 approximations
    assert fno_eligible_short("RELIANCE", date(2018, 1, 1)) is True
    assert long_eligible("RELIANCE", date(2018, 1, 1)) is True
    assert sector_of("RELIANCE") is None       # sector rule skipped when map absent


def test_liquidity_eligible_floor():
    # at MIN_ADV_RS (Rs 50 Cr), 1% ADV cap = Rs 50L -- far above the 5L direction
    # capital, so liquidity is essentially never the binding constraint once eligible
    assert liquidity_eligible(MIN_ADV_RS) is True
    assert liquidity_eligible(MIN_ADV_RS - 1) is False
    assert liquidity_eligible(0.0) is False
    assert liquidity_eligible(MIN_ADV_RS * 10) is True
