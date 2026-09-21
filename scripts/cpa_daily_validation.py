#!/usr/bin/env python
"""日线级 CPA 验证：Walk-Forward + 参数搜索 + DSR/PBO。

只读本地库，不下单、不联网、不启用付费 Provider。统计校正全部调用引擎已有实现
（`campaign_stats.deflated_sharpe` / `campaign_stats.proposal_pbo`），本脚本不重写公式。

用法：
    engine/.venv/bin/python scripts/cpa_daily_validation.py \
        --symbols BTCUSDT ETHUSDT --timeframe 1d --bars 2000 \
        --stages 'wedge_pop' 'wedge_pop+ema_crossback' \
        --sides long_only symmetric --models single --out .tmp/daily-validation.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "src"))

from quantdesk.config.settings import quantdesk_home  # noqa: E402
from quantdesk.datahub.db import Database  # noqa: E402
from quantdesk.studies import (  # noqa: E402
    BacktestRequest,
    ValidationRequest,
    run_single,
    run_validation,
)

ANNUAL_BARS = {"15m": 365 * 96, "1h": 365 * 24, "4h": 365 * 6, "1d": 365, "1w": 52}


def per_bar_sharpe(values: list[float]) -> float | None:
    """Sharpe per observation, the unit `deflated_sharpe` expects."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return None if variance <= 0 else mean / math.sqrt(variance)


def equity_returns(curve: list[float]) -> list[float]:
    return [current / previous - 1.0 for previous, current in zip(curve, curve[1:])
            if previous > 0 and math.isfinite(previous) and math.isfinite(current)]


def curve_of(payload: dict) -> list[float]:
    """Equity points from either payload shape: `run_single` is snake_case, the
    validation study's selected run is camelCase."""
    curve = payload.get("equityCurve") or payload.get("equity_curve") or []
    return [float(point["equity"]) for point in curve if point.get("equity") is not None]


def run_symbol(db: Database, symbol: str, args) -> dict:
    stages = [[part for part in item.split("+") if part] for item in args.stages]
    grid = {"entryStages": stages, "sideMode": list(args.sides)}
    if args.thresholds:
        grid["contractionThreshold"] = list(args.thresholds)
    if args.models:
        grid["positionModel"] = list(args.models)
    # Fixed knobs travel as single-value grid entries so every candidate carries them
    # and the trial count still equals the number of combinations actually evaluated.
    for key, value in json.loads(args.params or "{}").items():
        grid[key] = [value]
    combinations = math.prod(len(values) for values in grid.values())

    request = ValidationRequest(
        symbol=symbol, timeframe=args.timeframe, bars=args.bars, strategyId="cpa_cycle",
        parameterGrid=grid, train=args.train, validation=args.validation,
        walkForwardWindows=args.windows, allowDegraded=True,
    )
    print(f"[{symbol}] 验证：{combinations} 组参数 × {args.windows} 折 Walk-Forward", flush=True)
    result = run_validation(db, request)

    # 每个候选在整段样本上各跑一次，取逐根收益 → DSR 的试验分布与 PBO 的提案矩阵。
    series: dict[str, list[float]] = {}
    for combination in result["parameterSearch"]["grid"].get("combinations") or []:
        key = json.dumps(combination, ensure_ascii=False, sort_keys=True)
        try:
            payload = run_single(db, BacktestRequest(
                symbol=symbol, timeframe=args.timeframe, bars=args.bars,
                strategyId="cpa_cycle", strategyParams=combination, allowDegraded=True,
            ))
        except Exception as exc:  # noqa: BLE001 - 单组失败不得作废整轮验证
            series[key] = []
            print(f"  ! {key} 失败：{type(exc).__name__}: {exc}", flush=True)
            continue
        series[key] = equity_returns(curve_of(payload))

    usable = {key: values for key, values in series.items() if len(values) >= 2}
    length = min((len(values) for values in usable.values()), default=0)
    aligned = {key: values[-length:] for key, values in usable.items()}

    from quantdesk.campaign_stats import deflated_sharpe, proposal_pbo

    trial_sharpes = {key: value for key, values in aligned.items()
                     if (value := per_bar_sharpe(values)) is not None}
    pbo = proposal_pbo(aligned, blocks=min(8, max(2, length)), seed=0)
    annualisation = float(ANNUAL_BARS.get(args.timeframe) or 365)

    table = []
    for key in series:
        combination = json.loads(key)
        returns = aligned.get(key) or []
        observed = trial_sharpes.get(key)
        dsr = deflated_sharpe(
            observed_sharpe=observed,
            trial_sharpes=list(trial_sharpes.values()),
            trials=len(usable),
            sample_length=len(returns) or None,
            annualisation_factor=annualisation,
            extra_note="试验维度是 CPA 参数组合；每个组合在整段样本上各跑一次回测。",
        )
        table.append({
            "parameters": combination,
            "observations": len(returns),
            "perBarSharpe": observed,
            "deflatedSharpe": dsr.get("deflatedSharpe"),
            "expectedMaxSharpe": dsr.get("expectedMaxSharpe"),
            "dsrAvailable": dsr.get("available"),
            "dsrReason": dsr.get("reason") or "",
            "pbo": pbo.get("pbo"),
            "pboAvailable": pbo.get("available"),
            "pboReason": pbo.get("reason") or "",
        })

    walk = result.get("walkForward") or {}
    return {
        "symbol": symbol,
        "timeframe": args.timeframe,
        "bars": result.get("bars"),
        "grid": grid,
        "combinations": len(series),
        # 每个窗口：引擎选出的参数、样本外收益、成交笔数与告警。窗口的键是
        # {window: {index, train, validation}, parameters, validation, warnings}。
        "walkForward": [
            {
                "index": (item.get("window") or {}).get("index"),
                "trainBars": ((item.get("window") or {}).get("train") or {}).get("bars"),
                "validationBars": ((item.get("window") or {}).get("validation") or {}).get("bars"),
                "parameters": item.get("parameters"),
                "validationReturnPct": (item.get("validation") or {}).get("total_return_pct"),
                "validationBenchmarkPct": (item.get("validation") or {}).get("benchmark_return_pct"),
                "validationExcessPct": (item.get("validation") or {}).get("excess_return_pct"),
                "validationTrades": (item.get("validation") or {}).get("trades"),
                "warnings": item.get("warnings") or [],
            }
            for item in (walk.get("windows") or [])
        ],
        "walkForwardStableParameters": walk.get("stableParameters"),
        "walkForwardPositiveWindows": walk.get("positiveWindows"),
        "walkForwardWarnings": walk.get("warnings"),
        "selected": (result.get("parameterSearch") or {}).get("best", {}).get("parameters"),
        "selectedMetrics": (result.get("selectedRun") or {}).get("metrics"),
        "holdout": (result.get("parameterSearch") or {}).get("test"),
        "leakage": result.get("leakage"),
        "overfitWarnings": (result.get("parameterSearch") or {}).get("warnings"),
        "statistics": {"proposals": len(usable), "observations": length,
                       "blocks": pbo.get("blocks"), "scope": "cpa-parameter-combinations"},
        "pbo": pbo,
        "table": table,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--bars", type=int, default=2000)
    parser.add_argument("--stages", nargs="+",
                        default=["wedge_pop", "wedge_pop+ema_crossback"])
    parser.add_argument("--sides", nargs="+", default=["long_only", "symmetric"])
    parser.add_argument("--thresholds", nargs="*", type=float, default=[],
                        help="收缩阈值搜索轴，例如 0.2 0.3 0.4")
    parser.add_argument("--models", nargs="*", default=[])
    parser.add_argument("--params", default="", help='固定参数，JSON，例如 \'{"contractionThreshold": 0.3}\'')
    parser.add_argument("--train", type=float, default=0.6)
    parser.add_argument("--validation", type=float, default=0.2)
    parser.add_argument("--windows", type=int, default=4)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    db = Database(quantdesk_home() / "quantdesk.db")
    report = {
        "kind": "cpa-daily-validation",
        "timeframe": args.timeframe,
        "bars": args.bars,
        "train": args.train,
        "validation": args.validation,
        "windows": args.windows,
        "symbols": {},
    }
    for symbol in args.symbols:
        report["symbols"][symbol] = run_symbol(db, symbol, args)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {path}")

    for symbol, payload in report["symbols"].items():
        print(f"\n=== {symbol} {payload['timeframe']} · {payload['bars']} 根 ===")
        print(f"选定参数：{json.dumps(payload['selected'], ensure_ascii=False)}")
        metrics = payload["selectedMetrics"] or {}
        print(f"整段表现：收益 {metrics.get('net_return_pct')}% · 回撤 {metrics.get('max_drawdown_pct')}%"
              f" · 手续费 {metrics.get('total_fees')}")
        print(f"PBO：{payload['pbo'].get('pbo')}（{payload['pbo'].get('available')}）"
              f"｜{payload['pbo'].get('reason') or '可用'}")
        for row in payload["table"]:
            print(f"  {json.dumps(row['parameters'], ensure_ascii=False):<70}"
                  f" DSR={row['deflatedSharpe']} 每根Sharpe={row['perBarSharpe']}"
                  f" {'可用' if row['dsrAvailable'] else '不可用:' + row['dsrReason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
