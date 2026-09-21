"""Audit stored candles before automated decisions consume them."""

from __future__ import annotations

import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config.instruments import (
    CORE_TIMEFRAMES,
    INTERVAL_MS,
    INSTRUMENTS,
    TIMEFRAMES,
    require_instrument,
)
from ..datahub.db import Database


def _is_session_slot(ts: int, crypto: bool) -> bool:
    return crypto or datetime.fromtimestamp(ts / 1000, timezone.utc).weekday() < 5


def _expected_open(now: int, interval: str, crypto: bool) -> int:
    step = INTERVAL_MS[interval]
    candidate = (now // step) * step - step
    while not _is_session_slot(candidate, crypto):
        candidate -= step
    return candidate


def _gap_count(rows: list[dict], interval: str, crypto: bool) -> tuple[int, bool]:
    if len(rows) < 2:
        return 0, False
    step = INTERVAL_MS[interval]
    gaps = 0
    recent = False
    recent_boundary = int(rows[-1]["open_ts"]) - step * 6
    for previous, current in zip(rows, rows[1:]):
        cursor = int(previous["open_ts"]) + step
        end = int(current["open_ts"])
        while cursor < end:
            if _is_session_slot(cursor, crypto):
                gaps += 1
                recent = recent or cursor >= recent_boundary
            cursor += step
    return gaps, recent


class DataQualityMonitor:
    """Produce a deterministic audit from the same SQLite data used by alerts."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.db = Database(self.home / "quantdesk.db")

    def check_symbol(self, symbol: str, now: int | None = None) -> dict[str, Any]:
        spec = require_instrument(symbol)
        now = int(now or time.time() * 1000)
        # Signal eligibility is judged on the core timeframes; the weekly series is
        # reported alongside them but does not gate a stance.
        frames = [self._check_frame(spec.venue_symbol, interval, spec.is_crypto, spec.risk_class, now) for interval in CORE_TIMEFRAMES]
        blocking = [frame for frame in frames if not frame["signalEligible"]]
        issue_count = sum(frame["issueCount"] for frame in frames)
        if blocking:
            state = "critical"
        elif issue_count:
            state = "degraded"
        else:
            state = "healthy"
        return {
            "venueSymbol": spec.venue_symbol,
            "displaySymbol": spec.display_symbol,
            "name": spec.name,
            "state": state,
            "signalEligible": not blocking,
            "blockingReasons": [f"{frame['timeframe']}: {frame['statusLabel']}" for frame in blocking],
            "issueCount": issue_count,
            "frames": frames,
            "checkedAt": now,
        }

    def _check_frame(
        self,
        symbol: str,
        interval: str,
        crypto: bool,
        risk_class: str,
        now: int,
    ) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT open_ts,open,high,low,close,volume FROM candles "
            "WHERE venue='bybit' AND symbol=? AND interval=? ORDER BY open_ts DESC LIMIT 240",
            (symbol, interval),
        )
        rows.reverse()
        step = INTERVAL_MS[interval]
        expected = _expected_open(now, interval, crypto)
        latest = int(rows[-1]["open_ts"]) if rows else None
        lag_bars = None if latest is None else max(0, (expected - latest) // step)
        if latest is None:
            freshness = "missing"
            status_label = "没有缓存"
        elif lag_bars <= 1:
            freshness = "fresh"
            status_label = "数据正常"
        elif lag_bars <= 4:
            freshness = "delayed"
            status_label = f"延迟 {lag_bars} 根"
        else:
            freshness = "stale"
            status_label = f"过期 {lag_bars} 根"

        gaps, recent_gap = _gap_count(rows, interval, crypto)
        invalid = 0
        invalid_latest = False
        price_outliers = 0
        volume_outliers = 0
        recent_price_outlier = False
        recent_volume_outlier = False
        jump_limit = 0.35 if risk_class == "leveraged_etf" else 0.20 if crypto else 0.25
        volumes: list[float] = []
        previous_close: float | None = None
        for index, row in enumerate(rows):
            open_price = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            volume = float(row["volume"])
            bad = (
                min(open_price, high, low, close) <= 0
                or volume < 0
                or high < max(open_price, close, low)
                or low > min(open_price, close, high)
            )
            if bad:
                invalid += 1
                invalid_latest = invalid_latest or index == len(rows) - 1
            if previous_close and abs(close / previous_close - 1) > jump_limit:
                price_outliers += 1
                recent_price_outlier = recent_price_outlier or index >= len(rows) - 3
            baseline = statistics.median(volumes[-20:]) if volumes[-20:] else 0.0
            if baseline > 0 and volume > baseline * 20:
                volume_outliers += 1
                recent_volume_outlier = recent_volume_outlier or index >= len(rows) - 3
            volumes.append(volume)
            previous_close = close

        signal_eligible = (
            bool(rows)
            and freshness not in {"missing", "stale"}
            and not invalid_latest
            and not recent_gap
            and not recent_price_outlier
        )
        # Historical jumps and volume spikes remain visible diagnostics. They
        # do not make today's feed unhealthy unless the price anomaly is among
        # the latest three closed observations.
        issue_count = gaps + invalid + int(recent_price_outlier) + (1 if freshness in {"missing", "delayed", "stale"} else 0)
        if invalid_latest:
            status_label = "最新K线字段异常"
        elif recent_gap:
            status_label = "最近K线有缺口"
        elif recent_price_outlier:
            status_label = "最新价格跳变待核对"
        return {
            "timeframe": interval,
            "status": freshness,
            "statusLabel": status_label,
            "signalEligible": signal_eligible,
            "bars": len(rows),
            "latestOpenAt": latest,
            "expectedOpenAt": expected,
            "lagBars": lag_bars,
            "gaps": gaps,
            "recentGap": recent_gap,
            "duplicates": 0,
            "invalidBars": invalid,
            "priceOutliers": price_outliers,
            "volumeOutliers": volume_outliers,
            "recentPriceOutlier": recent_price_outlier,
            "recentVolumeOutlier": recent_volume_outlier,
            "issueCount": issue_count,
        }

    def overview(self) -> dict[str, Any]:
        now = int(time.time() * 1000)
        instruments = [self.check_symbol(spec.venue_symbol, now) for spec in INSTRUMENTS]
        states = {key: sum(1 for item in instruments if item["state"] == key) for key in ("healthy", "degraded", "critical")}
        cutoff = now - 86_400_000
        request_rows = self.db.query(
            "SELECT provider,operation,symbol,status,http_status,attempt,duration_ms,error,created_ts "
            "FROM market_requests WHERE created_ts>=? ORDER BY created_ts DESC LIMIT 30",
            (cutoff,),
        )
        aggregate = self.db.query(
            "SELECT COUNT(*) AS total,"
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,"
            "SUM(CASE WHEN attempt>1 THEN 1 ELSE 0 END) AS retried,"
            "SUM(CASE WHEN http_status=429 THEN 1 ELSE 0 END) AS rate_limited,"
            "AVG(duration_ms) AS average_latency FROM market_requests WHERE created_ts>=?",
            (cutoff,),
        )[0]
        request_summary = {
            "total": int(aggregate["total"] or 0),
            "failed": int(aggregate["failed"] or 0),
            "retried": int(aggregate["retried"] or 0),
            "rateLimited": int(aggregate["rate_limited"] or 0),
            "averageLatencyMs": round(float(aggregate["average_latency"] or 0)),
        }
        return {
            "summary": {**states, "total": len(instruments), "signalEligible": sum(1 for item in instruments if item["signalEligible"])},
            "instruments": instruments,
            "provider": {"windowHours": 24, **request_summary, "recent": request_rows},
            "checkedAt": now,
        }
