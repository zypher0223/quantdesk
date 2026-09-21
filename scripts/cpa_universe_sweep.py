#!/usr/bin/env python
"""全部 17 个合约在给定收缩阈值下的多头可达性扫描（只读本地库）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "src"))

from quantdesk.config.instruments import INSTRUMENTS  # noqa: E402
from quantdesk.config.settings import quantdesk_home  # noqa: E402
from quantdesk.datahub.db import Database  # noqa: E402
from quantdesk.studies import BacktestRequest, run_single  # noqa: E402

CT = float(sys.argv[1]) if len(sys.argv) > 1 else 0.30
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / ".tmp" / f"universe-ct{CT}.json"
BARS = {"15m": 2000, "1h": 2000, "4h": 2000, "1d": 1000, "1w": 300}

db = Database(quantdesk_home() / "quantdesk.db")
rows = []
for spec in INSTRUMENTS:
    for interval, bars in BARS.items():
        try:
            result = run_single(db, BacktestRequest(
                symbol=spec.venue_symbol, timeframe=interval, bars=bars,
                strategyId="cpa_cycle",
                strategyParams={"sideMode": "long_only", "contractionThreshold": CT},
                allowDegraded=True,
            ))
        except Exception as exc:  # noqa: BLE001 - 一个周期失败不影响其余
            rows.append({"symbol": spec.venue_symbol, "interval": interval,
                         "error": f"{type(exc).__name__}: {exc}"[:160]})
            continue
        phases = result["cpaPhases"]
        rows.append({
            "symbol": spec.venue_symbol,
            "productType": spec.product_type,
            "interval": interval,
            "bars": phases.get("bars"),
            "wedgePop": phases["counts"].get("wedge_pop", 0),
            "emaCrossback": phases["counts"].get("ema_crossback", 0),
            "baseNBreak": phases["counts"].get("base_n_break", 0),
            "trades": len(result.get("trades") or []),
            "netReturnPct": result.get("net_return_pct"),
            "maxDrawdownPct": result.get("max_drawdown_pct"),
            "degraded": result.get("degraded"),
        })
        print(f"{spec.venue_symbol:<10}{interval:<5} 突破={rows[-1]['wedgePop']:<3} "
              f"回踩={rows[-1]['emaCrossback']:<4} 成交={rows[-1]['trades']:<3} "
              f"收益={rows[-1]['netReturnPct']}", flush=True)

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps({"contractionThreshold": CT, "rows": rows},
                          ensure_ascii=False, indent=2), encoding="utf-8")
quiet = [row for row in rows if "error" not in row and row["trades"] == 0]
print(f"\n零成交周期 {len(quiet)}/{len(rows)}，已写入 {OUT}")
