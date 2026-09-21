"""Bybit v5 public REST client (read-only, no auth).

Verified live (2026-09-12):
  - TradFi stock perpetuals use category=linear and symbolType=stock
  - kline categories spot & linear, intervals 15/60/240/D, 1000 bars/call,
    newest-first rows [startMs, open, high, low, close, volume, turnover]
  - linear funding/history (8h cadence), open-interest, tickers
    (lastPrice/fundingRate/openInterest/turnover24h)

Network: api.bybit.com is CloudFront-blocked from some regions; pass proxy=...
(or set QUANTDESK_PROXY / config [proxy].url) — e.g. local http://127.0.0.1:12003.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Iterable

import httpx

from ..config.instruments import INTERVAL_MS

DEFAULT_BASE = "https://api.bybit.com"

INTERVAL_MAP = {"15m": "15", "1h": "60", "4h": "240", "1d": "D", "1w": "W"}
OI_INTERVALS = {"5min", "15min", "30min", "1h", "4h", "1d"}

logger = logging.getLogger(__name__)


class BybitError(RuntimeError):
    pass


class BybitClient:
    name = "bybit"

    def __init__(
        self,
        base_url: str | None = None,
        proxy: str | None = None,
        timeout: float = 30.0,
        telemetry: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.base_url = (base_url or os.environ.get("QUANTDESK_BYBIT_URL") or DEFAULT_BASE).rstrip("/")
        proxy = proxy or os.environ.get("QUANTDESK_PROXY") or None
        self._client = httpx.Client(timeout=timeout, proxy=proxy) if proxy else httpx.Client(timeout=timeout)
        self._telemetry = telemetry

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        for attempt in range(1, 4):
            started = time.monotonic()
            response: httpx.Response | None = None
            try:
                response = self._client.get(self.base_url + path, params=params)
                response.raise_for_status()
                body = response.json()
                if body.get("retCode") != 0:
                    error = BybitError(f"bybit {path} retCode={body.get('retCode')}: {body.get('retMsg')}")
                    if body.get("retCode") == 10006 and attempt < 3:
                        self._emit_telemetry(path, params, "failed", attempt, started, response.status_code, str(error))
                        time.sleep(0.5 * attempt)
                        continue
                    raise error
                self._emit_telemetry(path, params, "succeeded", attempt, started, response.status_code, None)
                result = body.get("result", {})
                # Keep the response envelope's venue timestamp available to the
                # ticker caller. Comparing a locally stamped REST quote with
                # exchange-stamped WebSocket frames can otherwise suppress fresh
                # realtime updates when the two clocks differ.
                if isinstance(result, dict) and body.get("time") is not None:
                    result = dict(result)
                    result["_exchangeTime"] = body["time"]
                return result
            except (httpx.HTTPError, ValueError, BybitError) as exc:
                status = response.status_code if response is not None else None
                self._emit_telemetry(path, params, "failed", attempt, started, status, str(exc))
                retryable = status == 429 or (status is not None and status >= 500) or isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))
                if retryable and attempt < 3:
                    delay = min(2.0, 0.5 * (2 ** (attempt - 1)))
                    time.sleep(delay)
                    continue
                raise
        raise BybitError(f"bybit {path} exhausted retries")

    def _emit_telemetry(
        self,
        path: str,
        params: dict,
        status: str,
        attempt: int,
        started: float,
        http_status: int | None,
        error: str | None,
    ) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry(
                {
                    "provider": "bybit",
                    "operation": path,
                    "symbol": params.get("symbol"),
                    "status": status,
                    "http_status": http_status,
                    "attempt": attempt,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "error": error,
                    "created_ts": int(time.time() * 1000),
                }
            )
        except Exception:
            # Telemetry must never turn a successful market response into a
            # collection failure.
            pass

    # -- instruments -----------------------------------------------------
    def instruments(
        self,
        category: str,
        symbol: str | None = None,
        symbol_type: str | None = None,
    ) -> list[dict]:
        """Paginated instruments-info."""
        out: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict = {"category": category, "limit": 1000}
            if symbol:
                params["symbol"] = symbol
            if symbol_type:
                params["symbolType"] = symbol_type
            if cursor:
                params["cursor"] = cursor
            result = self._get("/v5/market/instruments-info", params)
            out.extend(result.get("list", []))
            cursor = result.get("nextPageCursor") or ""
            if not cursor or symbol:
                break
        return out

    def configured_instruments(self) -> dict[str, dict]:
        """Return live metadata for the fixed QuantDesk universe.

        The stock-class pool spans two venue symbolTypes: `stock` for single
        names and `ETF` for SOXL/SOXS. Asking only for `stock` reports the
        leveraged ETFs as INACTIVE even while they trade.
        """
        from ..config.instruments import (
            CRYPTO_VENUE_SYMBOLS,
            STOCK_CLASS_SYMBOLS,
            STOCK_CLASS_VENUE_SYMBOL_TYPES,
        )

        by_symbol: dict[str, dict] = {}
        for symbol_type in STOCK_CLASS_VENUE_SYMBOL_TYPES:
            for row in self.instruments("linear", symbol_type=symbol_type):
                by_symbol.setdefault(row.get("symbol"), row)
        crypto = set(CRYPTO_VENUE_SYMBOLS)

        found: dict[str, dict] = {}
        for symbol in STOCK_CLASS_SYMBOLS + CRYPTO_VENUE_SYMBOLS:
            rows = self.instruments("linear", symbol=symbol) if symbol in crypto else [by_symbol.get(symbol)]
            if rows and rows[0] and rows[0].get("status") == "Trading":
                found[symbol] = rows[0]
        return found

    # -- kline -----------------------------------------------------------
    def kline(
        self,
        category: str,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        max_bars: int = 5000,
    ) -> list[dict]:
        """Oldest-first normalized candles across paginated calls.

        interval: canonical ('15m'/'1h'/'4h'/'1d').
        """
        bybit_itv = INTERVAL_MAP.get(interval)
        if bybit_itv is None:
            raise ValueError(f"unsupported bybit interval {interval!r}")
        step = INTERVAL_MS[interval]
        out: list[dict] = []
        end = int(end_ms)
        while end > int(start_ms) and len(out) < max_bars:
            result = self._get(
                "/v5/market/kline",
                {
                    "category": category,
                    "symbol": symbol,
                    "interval": bybit_itv,
                    "start": int(start_ms),
                    "end": end,
                    "limit": 1000,
                },
            )
            rows = result.get("list", [])
            if not rows:
                break
            for r in rows:
                out.append(
                    {
                        "ts": int(r[0]),
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                        "volume": float(r[5]),
                        "trades": None,
                    }
                )
            oldest = min(int(r[0]) for r in rows)
            if len(rows) < 1000:
                break
            end = oldest - 1  # continue before the oldest bar we already have
        # dedupe + sort oldest-first
        uniq = {r["ts"]: r for r in out}
        return [uniq[ts] for ts in sorted(uniq)]

    # -- derivatives -----------------------------------------------------
    def funding_history(
        self,
        symbol: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 200,
    ) -> list[dict]:
        params: dict = {"category": "linear", "symbol": symbol, "limit": limit}
        if start_ms is not None:
            # Bybit rejects startTime when endTime is absent. Use one bounded
            # window ending now unless the caller supplies an explicit end.
            params["startTime"] = int(start_ms)
            params["endTime"] = int(end_ms if end_ms is not None else utc_now_ms())
        elif end_ms is not None:
            params["endTime"] = int(end_ms)
        rows = self._get("/v5/market/funding/history", params).get("list", [])
        out = [{"ts": int(r["fundingRateTimestamp"]), "rate": float(r["fundingRate"])} for r in rows]
        out.sort(key=lambda r: r["ts"])
        return out

    def open_interest(
        self, symbol: str, interval_time: str = "1h", limit: int = 200
    ) -> list[dict]:
        if interval_time not in OI_INTERVALS:
            raise ValueError(f"unsupported OI interval {interval_time!r}")
        rows = self._get(
            "/v5/market/open-interest",
            {"category": "linear", "symbol": symbol, "intervalTime": interval_time, "limit": limit},
        ).get("list", [])
        out = [{"ts": int(r["timestamp"]), "oi": float(r["openInterest"])} for r in rows]
        out.sort(key=lambda r: r["ts"])
        return out

    def kline_snapshot(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 300,
        category: str = "linear",
        completed_only: bool = True,
    ) -> list[dict]:
        """Recent candles, oldest-first, for read-only panel requests.

        Unlike `kline()` this uses single-call backward pagination from now
        (the `start`+`end` form is only reliable up to 1000 bars per window),
        and drops the still-forming bar so downstream analysis only ever sees
        closed candles.
        """
        bybit_itv = INTERVAL_MAP.get(interval)
        if bybit_itv is None:
            raise ValueError(f"unsupported bybit interval {interval!r}")
        step = INTERVAL_MS[interval]
        rows = self._get(
            "/v5/market/kline",
            {"category": category, "symbol": symbol, "interval": bybit_itv, "limit": max(1, min(limit, 1000))},
        ).get("list", [])
        out = [
            {
                "ts": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "turnover": float(r[6]) if len(r) > 6 else None,
                "trades": None,
            }
            for r in rows
        ]
        out.sort(key=lambda r: r["ts"])
        if completed_only:
            now = utc_now_ms()
            out = [r for r in out if r["ts"] + step <= now]
        return out

    def ticker(self, category: str, symbol: str) -> dict:
        """One ticker row, carrying the venue's own timestamp.

        `exchangeTs` is the venue clock from the response envelope. Without it a
        caller can only stamp a quote with the local clock, and a locally stamped
        quote cannot be ordered against the WebSocket frames that carry the real
        exchange time.
        """
        result = self._get("/v5/market/tickers", {"category": category, "symbol": symbol})
        rows = result.get("list", [])
        if not rows:
            raise BybitError(f"no ticker for {symbol}")
        row = dict(rows[0])
        venue_ts = result.get("_exchangeTime")
        try:
            row["exchangeTs"] = int(venue_ts)
        except (TypeError, ValueError):
            row["exchangeTs"] = None
        return row


    def funding_history_window(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        *,
        limit: int = 200,
    ) -> list[dict]:
        """One window of funding settlements, oldest-first.

        The venue caps this endpoint well below the kline cap, so deep history has
        to be walked window by window.
        """
        rows = self._get(
            "/v5/market/funding/history",
            {
                "category": "linear",
                "symbol": symbol,
                "startTime": int(start_ms),
                "endTime": int(end_ms),
                "limit": max(1, min(int(limit), 200)),
            },
        ).get("list", [])
        out = [{"ts": int(r["fundingRateTimestamp"]), "rate": float(r["fundingRate"])} for r in rows]
        out.sort(key=lambda r: r["ts"])
        return out

    def open_interest_window(
        self,
        symbol: str,
        *,
        interval_time: str = "1h",
        start_ms: int,
        end_ms: int,
        limit: int = 200,
    ) -> list[dict]:
        """One window of open-interest history, oldest-first."""
        if interval_time not in OI_INTERVALS:
            raise ValueError(f"unsupported OI interval {interval_time!r}")
        rows = self._get(
            "/v5/market/open-interest",
            {
                "category": "linear",
                "symbol": symbol,
                "intervalTime": interval_time,
                "startTime": int(start_ms),
                "endTime": int(end_ms),
                "limit": max(1, min(int(limit), 200)),
            },
        ).get("list", [])
        out = [{"ts": int(r["timestamp"]), "oi": float(r["openInterest"])} for r in rows]
        out.sort(key=lambda r: r["ts"])
        return out

    def risk_limit(self, symbol: str, category: str = "linear") -> list[dict]:
        """The venue's leverage / maintenance-margin ladder for one contract.

        Every rung carries the notional it covers, the maintenance margin rate
        that applies there, the maintenance amount deducted from it and the
        highest leverage the venue allows inside that rung.
        """
        result = self._get("/v5/market/risk-limit", {"category": category, "symbol": symbol})
        return list(result.get("list") or [])

    def risk_limits(self, symbols: Iterable[str], category: str = "linear") -> dict[str, list[dict]]:
        """One ladder per symbol; a contract the venue refuses is reported empty."""
        out: dict[str, list[dict]] = {}
        for symbol in symbols:
            try:
                out[symbol] = self.risk_limit(symbol, category=category)
            except Exception as exc:  # noqa: BLE001 - one contract must not stop the sweep
                logger.warning("risk-limit failed for %s: %s", symbol, exc)
                out[symbol] = []
        return out

    def mark_price_kline(
        self,
        symbol: str,
        interval: str,
        *,
        limit: int = 300,
        category: str = "linear",
        start_ms: int | None = None,
        end_ms: int | None = None,
        completed_only: bool = True,
    ) -> list[dict]:
        """Mark-price candles, oldest-first.

        Bybit returns five fields per row - start, open, high, low, close - with
        no volume, which is exactly why this series is the honest input for
        liquidation and funding: it is the venue's own mark, not the last trade.
        """
        bybit_itv = INTERVAL_MAP.get(interval)
        if bybit_itv is None:
            raise ValueError(f"unsupported bybit interval {interval!r}")
        params: dict = {
            "category": category,
            "symbol": symbol,
            "interval": bybit_itv,
            "limit": max(1, min(limit, 1000)),
        }
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        if end_ms is not None:
            params["endTime"] = int(end_ms)
        rows = self._get("/v5/market/mark-price-kline", params).get("list", [])
        out: list[dict] = []
        for row in rows:
            if len(row) < 5:
                continue
            try:
                out.append(
                    {
                        "ts": int(row[0]),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": 0.0,
                        "turnover": None,
                        "trades": None,
                    }
                )
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda r: r["ts"])
        if completed_only:
            step = INTERVAL_MS[interval]
            now = utc_now_ms()
            out = [r for r in out if r["ts"] + step <= now]
        return out


def utc_now_ms() -> int:
    return int(time.time() * 1000)
