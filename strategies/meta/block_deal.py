"""B4c — BLOCK-DEAL (Cluster F, Meta): institutional block-deal net direction before open.
Entirely new data type — institutional order flow. Requires NSE block-deal history
(config/block_deals.json, date -> {symbol: net_sign}). Absent that file it votes nothing —
a graceful stub ready for the B4c data pipeline."""
from __future__ import annotations

import json
import logging

from config.settings import BLOCK_DEAL_FILE
from strategies.meta._meta_base import MetaStrategy

log = logging.getLogger(__name__)


def _load():
    if not BLOCK_DEAL_FILE.exists():
        log.warning("[PIT_MISSING] block_deals.json absent — BLOCK-DEAL votes nothing (stub)")
        return None
    return json.loads(BLOCK_DEAL_FILE.read_text())


_BLOCKS = _load()


class BlockDeal(MetaStrategy):
    name = "BLOCK-DEAL"

    def generate_signal(self, today_5min, history_5min, prev_day, nifty_today, trade_date):
        if _BLOCKS is None or today_5min.empty:
            return self._no_signal()
        day = _BLOCKS.get(str(trade_date))
        if not isinstance(day, dict):
            return self._no_signal()
        sym = str(today_5min.iloc[0]["symbol"]) if "symbol" in today_5min.columns else None
        net = day.get(sym)
        if net is None:
            return self._no_signal()
        net = float(net)
        if net > 0:
            return self._vote(+1, today_5min, "BLOCK-DEAL: net institutional buying")
        if net < 0:
            return self._vote(-1, today_5min, "BLOCK-DEAL: net institutional selling")
        return self._no_signal()
