"""Incremental candle cache over SQLite + venue adapters."""

from __future__ import annotations

import time
from collections.abc import Callable

import pandas as pd

from .venue import INTERVAL_MS

FetchFn = Callable[[str, str, int, int], list[dict]]  # (symbol, interval, start_ms, end_ms) -> rows

# 默认回填根数（首次拉取无本地数据时）
DEFAULT_BACKFILL = {  # interval -> bars
    "15m": 2000,
    "1h": 2000,
    "4h": 1000,
    "1d": 500,
    # 5 年周线约 260 根；取 400 留出余量，Bybit 单次上限 1000
    "1w": 400,
}


class CandleCache:
    def __init__(self, db, fetch: FetchFn):
        self.db = db
        self.fetch = fetch

    def ensure(
        self,
        venue: str,
        symbol: str,
        interval: str,
        *,
        backfill_bars: int | None = None,
        end_ms: int | None = None,
    ) -> int:
        """Fill the gap between local last bar and now; return fetched row count.

        The newest local bar is kept (it may be a still-forming bar captured
        earlier); refetch starts at its open so it gets refreshed.
        """
        if interval not in INTERVAL_MS:
            raise ValueError(interval)
        step = INTERVAL_MS[interval]
        now = int(end_ms or time.time() * 1000)
        last = self.db.last_open_ts(venue, symbol, interval)
        if last is None:
            bars = backfill_bars or DEFAULT_BACKFILL.get(interval, 500)
            start = now - bars * step
        else:
            start = last  # refresh the possibly-incomplete last bar too
        if start > now:
            return 0
        rows = self.fetch(symbol, interval, start, now)
        return self.db.upsert_candles(
            venue, symbol, interval, rows,
            source=getattr(self, "source", "venue_rest"),
            ingestion_mode=getattr(self, "ingestion_mode", "live"),
        ).written

    def load_df(
        self,
        venue: str,
        symbol: str,
        interval: str,
        *,
        limit: int | None = None,
        completed_only: bool = True,
    ) -> pd.DataFrame:
        rows = self.db.load_candles(venue, symbol, interval, limit=limit)
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        if completed_only and interval in INTERVAL_MS:
            now = int(time.time() * 1000)
            df = df[df["ts"] + INTERVAL_MS[interval] <= now]
        return df.reset_index(drop=True)
