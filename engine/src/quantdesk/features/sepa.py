"""SEPA — Minervini trend template (8 mandatory conditions) + VCP heuristic.

Conditions 1-8 per finance-skills sepa-strategy/references/trend-template.md.
Requires DAILY candles (>= 250 bars) with indicators appended. RS (condition 8)
uses the documented manual method: 12-month return vs a benchmark series
(e.g. SPY/BTC). A full cross-sectional percentile ranking is deferred.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Condition:
    index: int
    name: str
    passed: bool | None      # None = 无法评估（数据不足）
    detail: str


@dataclass
class TemplateResult:
    qualified: bool
    conditions: list[Condition]

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.conditions if c.passed)


def trend_template(daily, benchmark_close=None) -> TemplateResult:
    """daily: indicator-augmented daily frame (needs sma50/150/200, adx optional).

    benchmark_close: optional pd.Series of benchmark closes (e.g. SPY daily) —
    enables a manual RS estimate instead of None.
    """
    row = daily.iloc[-1]
    price = float(row["close"])
    conds: list[Condition] = []

    def add(i, name, passed, detail):
        conds.append(Condition(i, name, passed, detail))

    sma50, sma150, sma200 = row.get("sma50"), row.get("sma150"), row.get("sma200")
    c1 = price > sma150 and price > sma200 if sma150 and sma200 else None
    add(1, "价格 > 150MA 且 > 200MA", c1, f"price={price:.2f} sma150={sma150:.2f} sma200={sma200:.2f}" if c1 is not None else "数据不足")

    c2 = sma150 > sma200 if sma150 and sma200 else None
    add(2, "150MA > 200MA", c2, f"sma150-sma200={sma150 - sma200:.2f}" if c2 is not None else "数据不足")

    # 200MA trending up for >= 1 month (~21 trading days)
    c3 = None
    detail3 = "数据不足"
    if sma200 is not None and len(daily) > 21:
        prev200 = float(daily["sma200"].iloc[-22])
        c3 = sma200 > prev200
        detail3 = f"sma200 {prev200:.2f} → {sma200:.2f} (21日)"
    add(3, "200MA 上行 ≥ 1 个月", c3, detail3)

    c4 = sma50 > sma150 and sma50 > sma200 if sma50 and sma150 and sma200 else None
    add(4, "50MA > 150MA 且 > 200MA", c4, f"sma50={sma50:.2f}" if c4 is not None else "数据不足")

    c5 = price > sma50 if sma50 else None
    add(5, "价格 > 50MA", c5, f"price-sma50={price - sma50:.2f}" if c5 is not None else "数据不足")

    # 52w position (use up to 250 daily bars)
    win = daily["close"].tail(250)
    low52, high52 = float(win.min()), float(win.max())
    above_low_pct = (price / low52 - 1) * 100 if low52 else None
    below_high_pct = (1 - price / high52) * 100 if high52 else None
    c6 = above_low_pct >= 30 if above_low_pct is not None else None
    add(6, "高于 52 周低点 ≥ 30%", c6, f"{above_low_pct:.1f}%" if above_low_pct is not None else "数据不足")

    c7 = below_high_pct <= 25 if below_high_pct is not None else None
    add(7, "距 52 周高点 ≤ 25%", c7, f"{below_high_pct:.1f}%" if below_high_pct is not None else "数据不足")

    # RS: manual method — 12m return vs benchmark (percentile ranking deferred)
    c8 = None
    detail8 = "无基准序列，RS 无法评估（面板显示 unknown）"
    ret = daily["close"].tail(250)
    if len(ret) >= 200:
        stock_12m = ret.iloc[-1] / ret.iloc[0] - 1
        if benchmark_close is not None and len(benchmark_close) >= 200:
            bench_12m = benchmark_close.iloc[-1] / benchmark_close.iloc[0] - 1
            excess = stock_12m - bench_12m
            c8 = excess > 0
            detail8 = f"12个月超额收益 {excess * 100:+.1f}%（>0 视为通过；全截面百分位待 M5）"
        else:
            detail8 = f"12个月收益 {stock_12m * 100:+.1f}%，无基准对照"
    add(8, "相对强度 RS > 70 分位", c8, detail8)

    known = [c for c in conds if c.passed is not None]
    qualified = bool(known) and all(c.passed for c in known)
    return TemplateResult(qualified=qualified, conditions=conds)


def vcp(daily, lookback: int = 60, min_contractions: int = 3) -> dict:
    """Heuristic VCP detection: successive swing contraction + volume dry-up.

    Splits the window into `min_contractions+1` segments, measures each
    segment's (high-low)/low range; contractions = descending ranges. Volume
    dry-up = last segment mean volume < 0.8 × window mean.
    """
    win = daily.tail(lookback)
    seg = len(win) // (min_contractions + 1)
    if seg < 3:
        return {"is_vcp": False, "reason": "样本不足"}
    ranges, vols = [], []
    for k in range(min_contractions + 1):
        part = win.iloc[k * seg : (k + 1) * seg if k < min_contractions else len(win)]
        if part.empty:
            continue
        ranges.append((part["high"].max() - part["low"].min()) / part["low"].min() * 100)
        vols.append(part["volume"].mean())
    contracting = all(ranges[i] > ranges[i + 1] for i in range(len(ranges) - 1))
    dry_up = vols[-1] < 0.8 * (sum(vols) / len(vols))
    return {
        "is_vcp": bool(contracting and dry_up),
        "contracting": contracting,
        "dry_up": bool(dry_up),
        "ranges_pct": [round(r, 2) for r in ranges],
        "pivot": float(win["high"].max()),
    }
