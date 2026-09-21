"""Multi-timeframe resonance scoring.

Each timeframe produces a stance from three component votes (trend, momentum,
volume), each in {-1, 0, +1}. Timeframe stances are combined with configurable
weights into an overall resonance score in [-1, +1] (0..100 on the panel).
Weights default to higher timeframes dominating: 1d .40 / 4h .30 / 1h .20 / 15m .10.

Stock-class perpetuals keep printing bars while the US market is closed; those
bars carry a negligible fraction of session turnover, so the volume vote is
suppressed on them (`session_thin`) rather than read as a real expansion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_WEIGHTS = {"1d": 0.40, "4h": 0.30, "1h": 0.20, "15m": 0.10}
LABELS = [
    (-1.0, -0.6, "强共振看空"),
    (-0.6, -0.2, "偏空"),
    (-0.2, 0.2, "中性"),
    (0.2, 0.6, "偏多"),
    (0.6, 1.01, "强共振看多"),
]


@dataclass
class TFStance:
    interval: str
    trend: int
    momentum: int
    volume: int
    score: float          # (trend+momentum+volume) / 3
    stance: str           # bull / bear / neutral
    close: float
    notes: dict = field(default_factory=dict)


# TradFi stock perpetuals only have real flow while the underlying market is
# open. Outside those hours the venue still prints bars, but with a tiny
# fraction of the session's turnover — AAPL 15m has been observed between ~6
# and ~5,500 units in the same day. Treating such a bar as a volume signal
# invents conviction, so its volume vote is dropped instead.
THIN_SESSION_RATIO = 0.10
THIN_SESSION_LOOKBACK = 20
# Absolute floor on top of the ratio test. A bar that traded less than this in
# notional is not a liquidity event regardless of how quiet its neighbours were
# (AAPL 15m off-hours prints have been seen at ~2.1k USDT).
THIN_SESSION_MIN_NOTIONAL = 5_000.0


def is_thin_session(df, lookback: int = THIN_SESSION_LOOKBACK) -> tuple[bool, float | None]:
    """Is the last bar's traded notional negligible against its recent typical?

    Uses close*volume rather than a longer rolling mean so the off-hours zeros
    do not drag the baseline down with them. A bar is thin when it is both a
    small fraction of the recent median notional and small in absolute terms.
    """
    if len(df) < 2:
        return True, None
    notional = (df["close"] * df["volume"]).tail(lookback)
    baseline = notional.iloc[:-1].median()
    current = float(notional.iloc[-1])
    if not baseline or baseline <= 0:
        return True, None
    ratio = current / float(baseline)
    return (ratio < THIN_SESSION_RATIO or current < THIN_SESSION_MIN_NOTIONAL), ratio


def stance_for_interval(df) -> TFStance:
    """df: indicator-augmented candle frame (oldest-first). Uses the last row."""
    last = df.iloc[-1]
    close = float(last["close"])

    # trend: EMA staircase + ADX strength gate
    if close > last["ema50"] > last["ema200"]:
        trend = 1
    elif close < last["ema50"] < last["ema200"]:
        trend = -1
    else:
        trend = 0
    adx = last.get("adx14")
    if trend != 0 and adx is not None and adx < 15:
        trend = 0  # staircase present but trend too weak to trust

    # momentum: MACD body/signal alignment, RSI confirmation
    if last["macd"] > last["macds"] and last["macdh"] > 0:
        momentum = 1
    elif last["macd"] < last["macds"] and last["macdh"] < 0:
        momentum = -1
    else:
        momentum = 0
    rsi = last.get("rsi14")
    if momentum == 1 and rsi is not None and rsi > 70:
        momentum = 0  # overbought — momentum intact but chase risk high
    if momentum == -1 and rsi is not None and rsi < 30:
        momentum = 0

    # volume: only meaningful when it amplifies the bar direction
    thin, activity_ratio = is_thin_session(df)
    vr = last.get("vol_ratio")
    if not thin and vr is not None and vr >= 1.2:
        volume = 1 if close >= last["open"] else -1
    else:
        volume = 0

    score = (trend + momentum + volume) / 3
    return TFStance(
        interval=df.attrs.get("interval", "?"),
        trend=trend,
        momentum=momentum,
        volume=volume,
        score=score,
        stance="bull" if score > 0.2 else ("bear" if score < -0.2 else "neutral"),
        close=close,
        notes={
            "adx": adx,
            "rsi": rsi,
            "vol_ratio": vr,
            "session_thin": thin,
            "activity_ratio": None if activity_ratio is None else round(activity_ratio, 4),
        },
    )


def resonance(
    stances: list[TFStance],
    weights: dict[str, float] | None = None,
) -> dict:
    """Combine per-TF stances into an overall resonance result."""
    w = dict(weights or DEFAULT_WEIGHTS)
    total_w = 0.0
    score = 0.0
    for st in stances:
        weight = w.get(st.interval, 0.0)
        score += weight * st.score
        total_w += weight
    if total_w > 0:
        score /= total_w
    label = next(name for lo, hi, name in LABELS if lo <= score < hi)
    return {
        "score": round(score, 4),
        "score_100": round((score + 1) * 50, 1),
        "label": label,
        "timeframes": [
            {
                "interval": st.interval,
                "stance": st.stance,
                "score": st.score,
                "trend": st.trend,
                "momentum": st.momentum,
                "volume": st.volume,
                "close": st.close,
                "notes": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in st.notes.items()},
            }
            for st in stances
        ],
    }
