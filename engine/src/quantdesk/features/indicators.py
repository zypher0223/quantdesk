"""Technical indicators.

stockstats 0.6.x uses classic column names (close_10_ema etc.) and has no
ADX — ADX is implemented here with Wilder smoothing. SEPA needs SMA (simple),
resonance uses EMA; both computed explicitly.
"""

from __future__ import annotations

import pandas as pd
from stockstats import StockDataFrame

REQUIRED = ("open", "high", "low", "close", "volume")


def _stockstats(df: pd.DataFrame) -> StockDataFrame:
    base = df[["open", "high", "low", "close", "volume"]].copy()
    base.columns = [c.lower() for c in base.columns]
    return StockDataFrame.retype(base)


def add_adx(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    """Wilder's ADX/DI — stockstats 0.6 dropped it."""
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    down = -l.diff()
    plus_dm = pd.Series(
        [u if (u is not None and u > 0 and (d is None or u > d)) else 0.0 for u, d in zip(up, down)],
        index=df.index,
    )
    minus_dm = pd.Series(
        [d if (d is not None and d > 0 and (u is None or d > u)) else 0.0 for u, d in zip(up, down)],
        index=df.index,
    )
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / window, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / window, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / window, adjust=False).mean() / atr
    denom = (plus_di + minus_di).replace(0, float("nan"))
    dx = 100 * (plus_di - minus_di).abs() / denom
    df["plus_di"] = plus_di
    df["minus_di"] = minus_di
    df["adx"] = dx.ewm(alpha=1 / window, adjust=False).mean()
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with indicator columns appended.

    Expects columns ts/open/high/low/close/volume ordered oldest-first.
    Adds:
      ema10/ema50/ema200, sma50/sma150/sma200, vwma10
      boll/boll_ub/boll_lb, macd/macds/macdh, rsi14
      atr14, adx14/plus_di/minus_di, vr (volume ratio vs 20-bar mean),
      ret_1/ret_12m_pct (per-bar pct change / trailing 12m return % for daily)
    """
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"candles missing columns: {missing}")
    out = df.copy().reset_index(drop=True)
    s = _stockstats(out)

    out["ema10"] = s["close_10_ema"].values
    out["ema50"] = s["close_50_ema"].values
    out["ema200"] = s["close_200_ema"].values
    out["vwma10"] = s["close_10_vwma"].values
    out["boll"] = s["boll"].values
    out["boll_ub"] = s["boll_ub"].values
    out["boll_lb"] = s["boll_lb"].values
    out["macd"] = s["macd"].values
    out["macds"] = s["macds"].values
    out["macdh"] = s["macdh"].values
    out["rsi14"] = s["rsi_14"].values
    out["atr14"] = s["atr_14"].values
    out["vr"] = s["vr"].values
    out = add_adx(out)
    out = out.rename(columns={"adx": "adx14", "plus_di": "plus_di14", "minus_di": "minus_di14"})

    out["sma50"] = out["close"].rolling(50).mean()
    out["sma150"] = out["close"].rolling(150).mean()
    out["sma200"] = out["close"].rolling(200).mean()
    out["ret_1"] = out["close"].pct_change() * 100
    out["vol_ma20"] = out["volume"].rolling(20).mean()
    out["vol_ratio"] = out["volume"] / out["vol_ma20"].replace(0, float("nan"))
    return out
