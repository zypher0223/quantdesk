"""Derivatives panel: funding stats, OI change, volume anomalies (perp only)."""

from __future__ import annotations

import math
import time

FUNDING_INTERVAL_MS = 8 * 3600 * 1000  # bybit linear 8h; HL hourly handled by APR math


def _zscore(values: list[float], current: float) -> float | None:
    if len(values) < 10:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    sd = math.sqrt(var)
    return (current - mean) / sd if sd > 0 else None


def funding_stats(rates: list[dict], *, interval_hours: float = 8.0) -> dict:
    """rates: [{ts, rate}] oldest-first (rate per interval)."""
    if not rates:
        return {"available": False}
    vals = [r["rate"] for r in rates]
    current = vals[-1]
    apr = current / interval_hours * 24 * 365 * 100
    recent = vals[-90:]  # ~30 days of 8h prints
    return {
        "available": True,
        "current_rate_pct": round(current * 100, 4),
        "apr_pct": round(apr, 2),
        "zscore_30d": _zscore(recent, current),
        "mean_apr_pct_30d": round(sum(v / interval_hours * 24 * 365 for v in recent) / len(recent) * 100, 2),
        "count": len(vals),
        "last_ts": rates[-1]["ts"],
    }


def oi_stats(points: list[dict], *, now_ms: int | None = None) -> dict:
    """points: [{ts, oi}] oldest-first (1h cadence)."""
    if len(points) < 24:
        return {"available": False}
    now = now_ms or int(time.time() * 1000)
    current = points[-1]["oi"]

    def _ago(hours: int) -> float | None:
        target = now - hours * 3600 * 1000
        best = min(points, key=lambda p: abs(p["ts"] - target))
        return best["oi"] if abs(best["ts"] - target) < 2 * 3600 * 1000 else None

    oi_24h = _ago(24)
    oi_72h = _ago(72)
    return {
        "available": True,
        "current_oi": current,
        "change_24h_pct": round((current / oi_24h - 1) * 100, 2) if oi_24h else None,
        "change_72h_pct": round((current / oi_72h - 1) * 100, 2) if oi_72h else None,
        "zscore_30d": _zscore([p["oi"] for p in points[-720:]], current),
    }


def volume_stats(daily_df) -> dict:
    """daily_df: indicator-augmented daily frame."""
    if daily_df.empty:
        return {"available": False}
    last = daily_df.iloc[-1]
    vr = last.get("vol_ratio")
    return {
        "available": True,
        "volume": float(last["volume"]),
        "vol_ratio_20d": round(float(vr), 2) if vr == vr and vr is not None else None,
        "turnover_note": "xStocks 成交量稀薄，滑点假设见 config [paper]",
    }


def derivatives_panel(funding: dict, oi: dict, volume: dict) -> dict:
    """Combine into one panel dict with an aggregated bias in [-1, 1]."""
    bias = 0.0
    if funding.get("available"):
        z = funding.get("zscore_30d")
        if z is not None:
            # 极端正费率 → 拥挤多头 → 反向偏空；极端负费率反之
            bias += max(-1.0, min(1.0, -z / 2)) * 0.5
    if oi.get("available") and oi.get("change_24h_pct") is not None:
        # OI 上升 + 费率正 = 趋势健康；仅作轻微加权
        bias += max(-0.5, min(0.5, oi["change_24h_pct"] / 10)) * 0.3
    return {
        "funding": funding,
        "open_interest": oi,
        "volume": volume,
        "bias": round(max(-1.0, min(1.0, bias)), 3),
    }
