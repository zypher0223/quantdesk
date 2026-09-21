#!/usr/bin/env python
"""AI 模拟「保存规则并启动」验收（第 1–10 步）。

严格约束：
* **不调用真实大模型**——决策来自注入式桩函数，启动只需要凭据"形状"通过检查；
* **不接触线上数据**——整个流程跑在临时 QUANTDESK_HOME 里，结束时删除临时实例；
* 走的是真实 FastAPI 应用（httpx ASGITransport），不是模拟的路由。

用法：engine/.venv/bin/python scripts/ai_paper_acceptance.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine" / "src"))

TMP_HOME = tempfile.mkdtemp(prefix="qd-ai-accept-")
os.environ["QUANTDESK_HOME"] = TMP_HOME
# 仅用于通过「凭据形状」检查；脚本从不发起任何网络请求。
os.environ["DEEPSEEK_API_KEY"] = "qd-acceptance-stub-key"

import httpx  # noqa: E402

from quantdesk.ai_paper import AiPaperService  # noqa: E402
from quantdesk.api.server import app  # noqa: E402

FAILURES: list[str] = []
LABELS = {"short": "短线", "swing": "中长线"}


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def seed(service: AiPaperService, symbol: str = "BTCUSDT") -> None:
    """给临时实例造一份最小行情，让注入桩真的被调用。"""
    now = int(time.time() * 1000)
    step = 900_000
    rows = [
        {
            "ts": now - 240 * step + index * step,
            "open": 90 + index * 0.05 - 0.02, "high": 90 + index * 0.05 + 0.2,
            "low": 90 + index * 0.05 - 0.2, "close": 90 + index * 0.05,
            "volume": 1000 + index,
        }
        for index in range(240)
    ]
    service.db.upsert_candles("bybit", symbol, "15m", rows)
    service.db.upsert_market_snapshot("bybit", symbol, {
        "mark_price": 100.0, "last_price": 100.0, "received_ts": now,
        "exchange_ts": now, "source": "acceptance",
    })
    service.db.upsert_instrument_meta({
        "venue": "bybit", "symbol": symbol, "display_symbol": symbol.replace("USDT", ""),
        "status": "Trading", "tick_size": 0.1, "qty_step": 0.001, "min_notional": 5,
    })


ALL_SYMBOLS = ["AAPLUSDT", "MSFTUSDT", "GOOGLUSDT", "AMZNUSDT", "NVDAUSDT", "METAUSDT",
               "TSLAUSDT", "SNDKUSDT", "MUUSDT", "AMDSTOCKUSDT", "NBISUSDT", "SPCXUSDT",
               "SKHYUSDT", "SOXLUSDT", "SOXSUSDT", "BTCUSDT", "ETHUSDT"]
DRAFT = {
    "name": "短线激进模拟",
    "initialCash": 100_000,
    "maxLeverage": 8,
    "horizon": "short",
    "style": "aggressive",
    "fibOnly": False,
    "symbols": [symbol for symbol in ALL_SYMBOLS if symbol not in ("SOXLUSDT", "SOXSUSDT")],
}


async def run() -> int:
    print(f"临时数据目录：{TMP_HOME}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://accept", timeout=60) as client:
        print("\n[2] 创建临时模拟实例（初始仍是 稳妥 + 全部 17 个合约）")
        created = await client.post("/api/ai-paper/profiles", json={
            "name": "临时验收实例", "initialCash": 100_000, "maxLeverage": 3,
            "horizon": "short", "style": "conservative", "fibOnly": False,
            "symbols": ALL_SYMBOLS,
        })
        assert created.status_code == 201, created.text
        profile_id = created.json()["profile"]["id"]
        before = created.json()["profile"]
        check("临时实例已创建", profile_id.startswith("sim-"), profile_id)
        check("创建时的旧配置确实是 稳妥 + 17 个合约",
              before["style"] == "conservative" and len(before["symbols"]) == 17)

        print("\n[3–5] 选择「短线 + 激进 + 取消 SOXL/SOXS」，直接点「保存规则并启动」")
        started = await client.post(f"/api/ai-paper/profiles/{profile_id}/start", json=DRAFT)
        check("启动请求被接受", started.status_code == 200, started.text[:200])

        print("\n[6] 查询实例：配置必须与页面显示的一致")
        stored = (await client.get(f"/api/ai-paper/profiles/{profile_id}")).json()["profile"]
        check("enabled = true", stored["enabled"] is True)
        check("horizon = short", stored["horizon"] == "short", stored["horizon"])
        check("style = aggressive", stored["style"] == "aggressive", stored["style"])
        check("max_leverage = 8", float(stored["max_leverage"]) == 8.0, str(stored["max_leverage"]))
        check("symbols 不含 SOXLUSDT", "SOXLUSDT" not in stored["symbols"])
        check("symbols 不含 SOXSUSDT", "SOXSUSDT" not in stored["symbols"])
        check("symbols 正好是 15 个", len(stored["symbols"]) == 15, str(len(stored["symbols"])))
        check("启动响应回执与页面一致",
              started.json()["profile"]["style"] == DRAFT["style"]
              and set(started.json()["profile"]["symbols"]) == set(DRAFT["symbols"]))
        check("配置版本号随本次变更前进",
              stored["config_revision"] > before["config_revision"],
              f"{before['config_revision']} → {stored['config_revision']}")

        print("\n[7] 运行一轮注入式模型评估（不调用真实大模型）")
        service = AiPaperService(Path(TMP_HOME), profile_id=profile_id, create_if_missing=False)
        called: list[bool] = []

        def stub(evidence: dict) -> dict:
            called.append(True)
            return {"action": "hold", "confidence": 0.9, "reason": "验收注入桩：本轮观望"}

        seed(service)
        result = service.run_once(force=True, decision_provider=stub)
        service.close()
        check("注入桩确实被调用", bool(called), str(result.get("status")))
        check("本轮已写入决策记录", result.get("status") in ("held", "executed"), str(result.get("status")))

        print("\n[8] 决策证据必须显示 激进 / 短线 / 不含 SOXL、SOXS")
        snapshot = (await client.get(f"/api/ai-paper/profiles/{profile_id}")).json()
        decision = snapshot["decisions"][0]
        simulation = decision["evidence"]["simulation"]
        check("styleLabel = 激进", simulation["styleLabel"] == "激进", simulation["styleLabel"])
        check("horizonLabel = 短线", simulation["horizonLabel"] == LABELS["short"], simulation["horizonLabel"])
        check("证据里的 configRevision 与配置一致",
              simulation["configRevision"] == stored["config_revision"],
              f"{simulation['configRevision']} vs {stored['config_revision']}")
        check("证据合约不含 SOXLUSDT", "SOXLUSDT" not in simulation["symbols"])
        check("证据合约不含 SOXSUSDT", "SOXSUSDT" not in simulation["symbols"])
        check("证据合约数与配置一致", len(simulation["symbols"]) == 15, str(len(simulation["symbols"])))
        check("fibOnly 与配置一致", simulation["fibOnly"] is False)
        check("maxLeverage 与配置一致", float(simulation["maxLeverage"]) == 8.0)

        print("\n[9] 连续两次刷新（模拟页面轮询）配置不得变化")
        first = (await client.get(f"/api/ai-paper/profiles/{profile_id}")).json()["profile"]
        time.sleep(1)
        second = (await client.get(f"/api/ai-paper/profiles/{profile_id}")).json()["profile"]
        same = all(first[key] == second[key] for key in
                   ("style", "horizon", "max_leverage", "fib_only", "symbols", "config_revision", "enabled"))
        check("两次刷新返回完全相同的配置", same)
        check("刷新后仍是 激进 + 短线", second["style"] == "aggressive" and second["horizon"] == "short")

        print("\n[10] 停止并删除临时实例")
        refused = await client.delete(f"/api/ai-paper/profiles/{profile_id}?confirm=true")
        check("运行中的实例不允许直接删除（必须先停止）", refused.status_code == 409, refused.text[:120])
        stopped = await client.post(f"/api/ai-paper/profiles/{profile_id}/stop")
        check("已停止临时实例", stopped.status_code == 200 and stopped.json()["enabled"] is False)
        removed = await client.delete(f"/api/ai-paper/profiles/{profile_id}?confirm=true")
        check("临时实例已删除", removed.status_code == 200, removed.text[:120])
        check("删除后查询返回 404",
              (await client.get(f"/api/ai-paper/profiles/{profile_id}")).status_code == 404)

    print("\n" + ("=" * 60))
    if FAILURES:
        print(f"验收失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("验收通过：页面显示的规则与服务器执行的规则一致（含证据与版本号）")
    print(json.dumps({"draft": DRAFT, "profileId": profile_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(run()))
