"""Hyperliquid public info API client.

Python port of finance-skills opencli-plugins/hyperliquid/lib/*.js — the wire
protocol is a single unauthenticated POST to https://api.hyperliquid.xyz/info
with {"type": ...}. Read-only; trading actions on /exchange are NOT touched.

Funding: HL perps fund hourly → APR = rate * 24 * 365. Other venues in
predictedFundings fund on their own interval — always normalize with the
per-row fundingIntervalHours.
"""

from __future__ import annotations

import httpx

INFO_URL = "https://api.hyperliquid.xyz/info"

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}

VENUE_KEYS = {"HlPerp": "hl", "BinPerp": "binance", "BybitPerp": "bybit"}


def _client(proxy: str | None, timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, proxy=proxy) if proxy else httpx.Client(timeout=timeout)


def info_fetch(body: dict, *, proxy: str | None = None, timeout: float = 30.0):
    """POST one info request and return the parsed JSON body."""
    with _client(proxy, timeout) as c:
        res = c.post(INFO_URL, json=body)
        res.raise_for_status()
        return res.json()


def _num(v):
    if v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n else None  # drop NaN


def normalize_candles(rows) -> list[dict]:
    """Wire row {t,T,s,i,o,h,l,c,v,n} → oldest-first normalized dicts (ts in ms)."""
    out = []
    for c in rows or []:
        out.append(
            {
                "ts": int(c["t"]),
                "open": _num(c["o"]),
                "high": _num(c["h"]),
                "low": _num(c["l"]),
                "close": _num(c["c"]),
                "volume": _num(c["v"]) or 0.0,
                "trades": c.get("n"),
            }
        )
    out.sort(key=lambda r: r["ts"])
    return out


def candles(
    coin: str,
    interval: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    *,
    proxy: str | None = None,
) -> list[dict]:
    """candleSnapshot — up to 5000 bars per call."""
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported HL interval {interval!r}")
    req: dict = {"coin": coin, "interval": interval}
    if start_ms is not None:
        req["startTime"] = int(start_ms)
    if end_ms is not None:
        req["endTime"] = int(end_ms)
    rows = info_fetch({"type": "candleSnapshot", "req": req}, proxy=proxy)
    return normalize_candles(rows)


def normalize_perp_markets(meta, ctxs) -> list[dict]:
    """Zip meta.universe[i] with ctxs[i] (parallel arrays, same order)."""
    universe = (meta or {}).get("universe", [])
    out = []
    for i, u in enumerate(universe):
        c = (ctxs or [])[i] if i < len(ctxs or []) else {}
        mark = _num(c.get("markPx"))
        funding = _num(c.get("funding"))
        oi = _num(c.get("openInterest"))
        prev = _num(c.get("prevDayPx"))
        out.append(
            {
                "coin": u["name"],
                "markPx": mark,
                "midPx": _num(c.get("midPx")),
                "oraclePx": _num(c.get("oraclePx")),
                "change24hPct": ((mark - prev) / prev * 100) if mark and prev else None,
                "fundingHrPct": funding * 100 if funding is not None else None,
                "fundingAprPct": funding * 24 * 365 * 100 if funding is not None else None,
                "openInterest": oi,
                "oiNotional": oi * mark if oi is not None and mark else None,
                "dayNtlVlm": _num(c.get("dayNtlVlm")),
                "premiumPct": (_num(c.get("premium")) or 0) * 100,
                "maxLeverage": u.get("maxLeverage"),
                "delisted": u.get("isDelisted") is True,
            }
        )
    return out


def perp_markets(*, proxy: str | None = None) -> list[dict]:
    meta, ctxs = info_fetch({"type": "metaAndAssetCtxs"}, proxy=proxy)
    return normalize_perp_markets(meta, ctxs)


def funding_history(
    coin: str,
    start_time: int | None = None,
    end_time: int | None = None,
    *,
    proxy: str | None = None,
) -> list[dict]:
    """Hourly funding prints, oldest-first: {ts, rate (per hour), premiumPct}."""
    body: dict = {"type": "fundingHistory", "coin": coin}
    if start_time is not None:
        body["startTime"] = int(start_time)
    if end_time is not None:
        body["endTime"] = int(end_time)
    rows = info_fetch(body, proxy=proxy) or []
    out = [
        {
            "ts": int(r["time"]),
            "rate": _num(r.get("fundingRate")) or 0.0,
            "premiumPct": (_num(r.get("premium")) or 0) * 100,
        }
        for r in rows
    ]
    out.sort(key=lambda r: r["ts"])
    return out


def predicted_fundings(coin: str | None = None, *, proxy: str | None = None) -> list[dict]:
    """Cross-venue predicted funding APR screen (hl vs binance vs bybit).

    Wire: [[coin, [[venueName, {fundingRate, fundingIntervalHours, nextFundingTime}], ...]], ...]
    Each leg annualized with its own interval.
    """
    data = info_fetch({"type": "predictedFundings"}, proxy=proxy) or []
    want = coin.upper() if coin else None
    out = []
    for entry in data:
        name = entry[0]
        if want and str(name).upper() != want:
            continue
        apr: dict = {}
        next_hl = None
        for v_name, v in entry[1] or []:
            key = VENUE_KEYS.get(v_name)
            if not key or not v:
                continue
            rate = _num(v.get("fundingRate"))
            hours = _num(v.get("fundingIntervalHours")) or 1
            apr[key] = (rate / hours) * 24 * 365 * 100 if rate is not None else None
            if key == "hl":
                next_hl = v.get("nextFundingTime")
        hl = apr.get("hl")
        out.append(
            {
                "coin": name,
                "hlAprPct": hl,
                "binanceAprPct": apr.get("binance"),
                "bybitAprPct": apr.get("bybit"),
                "hlVsBinancePct": (hl - apr["binance"]) if hl is not None and apr.get("binance") is not None else None,
                "hlVsBybitPct": (hl - apr["bybit"]) if hl is not None and apr.get("bybit") is not None else None,
                "nextHlFundingMs": next_hl,
            }
        )
    return out
