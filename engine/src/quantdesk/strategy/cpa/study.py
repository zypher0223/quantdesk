"""CPA ablation and grouped reporting (stage C).

What this module is: the comparison the report asked for, built on machinery that
already exists rather than on new statistics. It runs a fixed set of **variants** over
one group of contracts, keeps the groups apart, and hands the resulting metrics to the
engine's existing corrections (`campaign_stats.deflated_sharpe`,
`campaign_stats.proposal_pbo`).

Three rules are structural here:

* **no variant is required to make money.** A run that loses money is a result; the
  report's own instruction is that correctness and reproducibility pass, not
  profitability. Nothing in this file treats a negative return as a failure;
* **groups stay apart.** The stock group excludes the leveraged ETFs, SOXL/SOXS are
  reported as their own group, and crypto is its own. A blended number would be a
  number about nothing;
* **the corrections are the engine's.** Deflated Sharpe and PBO come from the same
  implementations the campaign layer uses, so an ablation cannot quietly use a kinder
  version of either.

The variants themselves are the report's own list: buy and hold, two moving averages,
a channel breakout, the wedge pop alone, the wedge pop with the higher timeframe, the
wedge pop with volume confirmation, and the full simplified CPA.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ...config.instruments import require_instrument
from ...datahub.db import Database
from .defaults import PARAMETER_VERSION

# A variant is a named way of running a study: a strategy id plus the parameters that
# make it *that* variant, and the sentence a reader needs to know what changed.
@dataclass(frozen=True)
class Variant:
    key: str
    label: str
    strategy_id: str
    parameters: dict[str, Any] = field(default_factory=dict)
    note: str = ""


VARIANTS: tuple[Variant, ...] = (
    Variant("buy_hold", "买入并持有", "buy_hold", {},
            "基准：不看任何规则，只在第一根开仓后持有。"),
    Variant("ma_cross", "双均线交叉", "ma_cross", {"fastPeriod": 10, "slowPeriod": 20},
            "与 CPA 使用同一组均线周期（10/20），因此差异来自阶段规则而不是均线参数。"),
    Variant("channel_breakout", "价格通道突破", "channel_breakout", {"lookback": 20},
            "QuantDesk 既有的通道突破，用于对照平台突破。"),
    Variant("cpa_wedge_pop", "仅楔形突破", "cpa_cycle",
            {"entryStages": ["wedge_pop"], "volumeConfirm": 0.0, "exitOnExhaustion": True},
            "只保留楔形突破入场，并关闭放量确认（volumeConfirm=0 表示不设量能下限）。"),
    Variant("cpa_wedge_pop_volume", "楔形突破 + 放量", "cpa_cycle",
            {"entryStages": ["wedge_pop"], "volumeConfirm": 1.3, "exitOnExhaustion": True},
            "在上一版基础上要求放量确认。"),
    Variant("cpa_wedge_pop_htf", "楔形突破 + 高周期", "cpa_cycle",
            {"entryStages": ["wedge_pop"], "volumeConfirm": 0.0, "requireHigherTimeframe": True,
             "exitOnExhaustion": True},
            "在仅楔形突破的基础上要求管理周期未处于下行。"),
    Variant("cpa_full", "完整 CPA（简化仓位）", "cpa_cycle",
            {"entryStages": ["wedge_pop", "ema_crossback", "base_n_break"],
             "exitOnExhaustion": True},
            "三个阶段都可入场，衰竭即退出；仓位仍是单仓位简化模型。"),
)

# Variants whose exits may close a long even when the entry stages are restricted.
LONG_ONLY_SIDE_MODE = "long_only"


def variants_for(group: str) -> tuple[Variant, ...]:
    """The ablation list for a group. Crypto gets its own defaults, not the stock ones."""
    if group != "crypto":
        return VARIANTS
    return tuple(
        Variant(item.key, item.label, item.strategy_id,
                {**item.parameters, "sideMode": LONG_ONLY_SIDE_MODE}, item.note)
        for item in VARIANTS
    )


def _metrics(result: dict[str, Any]) -> dict[str, Any]:
    """The figures the report requires, taken from what the engine already reports."""
    trades = result.get("trades") or []
    curve = result.get("equity_curve") or []
    # The study payload carries no Sharpe/Sortino field, so they are computed from the
    # equity curve with the engine's own metrics module rather than invented here.
    sharpe = sortino = None
    if len(curve) > 2:
        from ...config.instruments import INTERVAL_MS
        from .. import metrics as metrics_module

        interval = str(result.get("interval") or "")
        computed = metrics_module.compute_metrics(
            [{"time": index, "equity": float(point["equity"])} for index, point in enumerate(curve)],
            trades=trades,
            interval_ms=INTERVAL_MS.get(interval),
            initial_capital=float(result.get("initial_capital") or 0.0) or None,
        )
        sharpe = getattr(computed, "sharpe", None)
        sortino = getattr(computed, "sortino", None)
    return {
        "symbol": result.get("symbol") or result.get("displaySymbol") or "",
        "bars": result.get("bars"),
        "trades": len(trades),
        "finalEquity": result.get("final_equity"),
        "netReturnPct": result.get("net_return_pct"),
        "maxDrawdownPct": result.get("max_drawdown_pct"),
        "sharpe": sharpe,
        "sortino": sortino,
        "profitFactor": result.get("profit_factor"),
        "winRatePct": result.get("win_rate_pct"),
        "totalFees": result.get("total_fees"),
        "totalFunding": result.get("total_funding"),
        "exposurePct": result.get("exposure_pct"),
        "curve": [float(point["equity"]) for point in curve],
        "degraded": bool(result.get("degraded")),
        "dataReady": result.get("dataReady"),
    }


def _mean(values: Sequence[float]) -> float | None:
    usable = [value for value in values if value is not None and math.isfinite(value)]
    if not usable:
        return None
    return sum(usable) / len(usable)


def _equity_returns(curve: Sequence[float]) -> list[float]:
    """Per-bar simple returns from one equity curve."""
    return [current / previous - 1.0 for previous, current in zip(curve, curve[1:])
            if previous > 0 and math.isfinite(previous) and math.isfinite(current)]


def _group_returns(rows: Sequence[dict[str, Any]]) -> list[float]:
    """Equal-weight symbol returns for one variant on the common timeline."""
    series = [_equity_returns(row.get("curve") or []) for row in rows]
    series = [values for values in series if values]
    if not series:
        return []
    length = min(len(values) for values in series)
    # Every symbol was requested over the same number of most-recent bars. Aligning
    # the tail keeps their common end date when a venue returned a shorter history.
    aligned = [values[-length:] for values in series]
    return [sum(values[index] for values in aligned) / len(aligned) for index in range(length)]


def _per_bar_sharpe(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return None if variance <= 0 else mean / math.sqrt(variance)


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Group summary. Missing figures stay missing - never averaged in as zero."""
    sharpes = [row["sharpe"] for row in rows]
    returns = [row["netReturnPct"] for row in rows]
    drawdowns = [row["maxDrawdownPct"] for row in rows]
    return {
        "symbols": len(rows),
        "trades": sum(row["trades"] or 0 for row in rows),
        "meanSharpe": _mean(sharpes),
        "meanReturnPct": _mean(returns),
        "worstDrawdownPct": min(
            (value for value in drawdowns if value is not None), default=None
        ),
        "totalFees": sum(row["totalFees"] or 0.0 for row in rows),
        "measurable": sum(1 for value in sharpes if value is not None),
        "symbolsMissingSharpe": sum(1 for value in sharpes if value is None),
    }


def run_ablation(
    db: Database,
    *,
    group: str,
    interval: str,
    bars: int = 1200,
    variants: Sequence[Variant] | None = None,
    symbols: Sequence[str] | None = None,
    allow_degraded: bool = False,
    runner: Callable[[Database, Any], dict[str, Any]] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Run every variant over every contract of one group and report the comparison.

    `runner` is injectable so the aggregation, grouping and correction wiring can be
    tested without minutes of number crunching; production passes the real study.
    """
    from ...factor_gates import group_symbols
    from ...studies import BacktestRequest, run_single

    universe = list(symbols or group_symbols(group))
    if len(universe) < 2:
        raise ValueError(f"分组 {group} 的合约不足 2 个，无法做组合级对比")
    run = runner or (lambda database, request: run_single(database, request))
    chosen = tuple(variants or variants_for(group))

    results: dict[str, dict[str, dict[str, Any]]] = {}
    total = max(1, len(chosen) * len(universe))
    done = 0
    for variant in chosen:
        per_symbol: dict[str, dict[str, Any]] = {}
        for symbol in universe:
            spec = require_instrument(symbol)
            request = BacktestRequest(
                symbol=spec.display_symbol, timeframe=interval, bars=bars,
                strategyId=variant.strategy_id, strategyParams=dict(variant.parameters),
                allowDegraded=allow_degraded,
            )
            try:
                payload = run(db, request)
            except Exception as exc:  # one variant failing must not void the comparison
                per_symbol[symbol] = {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"[:200],
                                      "trades": 0, "sharpe": None, "netReturnPct": None,
                                      "maxDrawdownPct": None, "totalFees": None, "curve": []}
            else:
                per_symbol[symbol] = _metrics(payload)
            done += 1
            if progress:
                progress(0.05 + 0.85 * (done / total), f"{variant.label} · {spec.display_symbol}")
        results[variant.key] = per_symbol

    # The corrections come from the engine's own implementations.
    from ...campaign_stats import deflated_sharpe, proposal_pbo

    # Statistical corrections operate on the experiment's search dimension: each
    # column is a strategy variant. Symbols are combined inside a column, never used
    # as if they were competing proposals.
    variant_returns: dict[str, list[float]] = {}
    for variant in chosen:
        rows = [item for item in results[variant.key].values() if "error" not in item]
        variant_returns[variant.key] = _group_returns(rows)
    trial_sharpes = {
        key: value for key, values in variant_returns.items()
        if (value := _per_bar_sharpe(values)) is not None
    }
    pbo = proposal_pbo(variant_returns, blocks=8, seed=0)
    interval_ms = {
        "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000,
        "1d": 86_400_000, "1w": 604_800_000,
    }.get(interval)
    calendar_days = 365.0 if group == "crypto" else 252.0
    annualisation_factor = (
        calendar_days * 86_400_000.0 / interval_ms if interval_ms else None
    )

    table: list[dict[str, Any]] = []
    for variant in chosen:
        per_symbol = results[variant.key]
        rows = [item for item in per_symbol.values() if "error" not in item]
        returns = variant_returns[variant.key]
        observed = trial_sharpes.get(variant.key)
        dsr = deflated_sharpe(
            observed_sharpe=observed,
            trial_sharpes=list(trial_sharpes.values()),
            trials=len(chosen),
            sample_length=len(returns) or None,
            annualisation_factor=annualisation_factor,
            extra_note="试验维度是策略变体；每个变体先对组内合约收益做等权合并。",
        )
        table.append(
            {
                "variant": variant.key,
                "label": variant.label,
                "strategyId": variant.strategy_id,
                "parameters": dict(variant.parameters),
                "note": variant.note,
                "summary": _aggregate(rows),
                "deflatedSharpe": dsr.get("deflatedSharpe"),
                "expectedMaxSharpe": dsr.get("expectedMaxSharpe"),
                "dsrAvailable": dsr.get("available"),
                "dsrReason": dsr.get("reason") or "",
                "pbo": pbo.get("pbo"),
                "pboAvailable": pbo.get("available"),
                "pboReason": pbo.get("reason") or "",
                "pboScope": "variant-set",
                "perSymbol": per_symbol,
                "failures": {
                    symbol: item["error"] for symbol, item in per_symbol.items() if "error" in item
                },
            }
        )

    return {
        "kind": "cpa-ablation",
        "group": group,
        "interval": interval,
        "bars": bars,
        "universe": universe,
        "parameterVersion": PARAMETER_VERSION,
        "variants": [variant.key for variant in chosen],
        "table": table,
        "pbo": pbo,
        "statisticsScope": {
            "trialDimension": "strategy-variants",
            "symbolAggregation": "equal-weight-per-bar-returns",
            "trialSharpes": trial_sharpes,
        },
        "warnings": _warnings(group, table),
        "interpretation": (
            "本表只报告可复现的数值对比，不以「收益为正」作为通过条件："
            "正确性与可复现性通过不代表策略有效。Deflated Sharpe 与 PBO 来自引擎"
            "既有实现，N 取本次对比的变体数量。"
        ),
        "simplePositionNotice": (
            "仓位仍是单仓位简化模型，分批建仓与分批减仓尚未启用（阶段 D 之前）。"
        ),
    }


def _warnings(group: str, table: list[dict[str, Any]]) -> list[str]:
    out = []
    if group == "leveraged_etf":
        out.append(
            "SOXL/SOXS 是杠杆 ETF 合约：单独统计，不与普通股票合约合并，也不共用同一套扩张阈值。"
        )
    unmeasurable = [row["label"] for row in table if not row["summary"]["measurable"]]
    if unmeasurable:
        out.append(f"以下变体没有任何合约能算出 Sharpe：{', '.join(unmeasurable)}")
    missing = sum(row["summary"]["symbolsMissingSharpe"] for row in table)
    if missing:
        out.append(f"共有 {missing} 条 (变体,合约) 结果没有 Sharpe 读数，按缺失处理而不是按 0。")
    return out


def group_report(report: dict[str, Any]) -> str:
    """A compact text table for a CLI or a log line."""
    lines = [
        f"{report['group']} / {report['interval']} · {len(report['universe'])} 个合约 · "
        f"{report['bars']} 根 · 参数版本 {report['parameterVersion']}",
        f"{'变体':<22s} {'交易':>6s} {'均值Sharpe':>10s} {'均值收益%':>10s} {'最差回撤%':>10s} "
        f"{'DSR':>7s} {'PBO':>7s}",
    ]
    for row in report["table"]:
        summary = row["summary"]
        def fmt(value, digits=4):
            return "—" if value is None else f"{value:.{digits}f}"
        lines.append(
            f"{row['label']:<22s} {summary['trades']:>6d} {fmt(summary['meanSharpe']):>10s} "
            f"{fmt(summary['meanReturnPct'], 2):>10s} {fmt(summary['worstDrawdownPct'], 2):>10s} "
            f"{fmt(row['deflatedSharpe']):>7s} {fmt(row['pbo']):>7s}"
        )
    for warning in report["warnings"]:
        lines.append(f"! {warning}")
    return "\n".join(lines)
