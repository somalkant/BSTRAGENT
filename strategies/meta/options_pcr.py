"""B4c — PCR (Cluster F, Meta): options Put-Call Ratio sentiment (contrarian at extremes).
Entirely new data type. Requires NSE PCR history (config/pcr_history.json, keyed by date
or date->symbol). Absent that file it votes nothing — a graceful stub ready for the B4c
data pipeline."""
from __future__ import annotations

import json
import logging

from config.settings import PCR_HISTORY_FILE
from strategies.meta._meta_base import MetaStrategy

log = logging.getLogger(__name__)
PCR_HIGH = 1.3     # excessive bearishness -> bullish contrarian
PCR_LOW  = 0.7     # excessive bullishness -> bearish contrarian


def _load():
    if not PCR_HISTORY_FILE.exists():
        log.warning("[PIT_MISSING] pcr_history.json absent — PCR votes nothing (stub)")
        return None
    return json.loads(PCR_HISTORY_FILE.read_text())


_PCR = _load()


class OptionsPCR(MetaStrategy):
    name = "PCR"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date):
        if _PCR is None:
            return self._no_signal()
        entry = _PCR.get(str(trade_date))
        if isinstance(entry, dict):
            # per-symbol; symbol carried on today's rows
            sym = str(today_5min.iloc[0]["symbol"]) if "symbol" in today_5min.columns else None
            val = entry.get(sym)
        else:
            val = entry
        if val is None:
            return self._no_signal()
        pcr = float(val)
        if pcr >= PCR_HIGH:
            return self._vote(+1, today_5min, f"PCR: {pcr:.2f} extreme bearishness (contrarian long)")
        if pcr <= PCR_LOW:
            return self._vote(-1, today_5min, f"PCR: {pcr:.2f} extreme bullishness (contrarian short)")
        return self._no_signal()
