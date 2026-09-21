from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

import httpx
import pytest

from quantdesk.ai_paper import AiPaperError, AiPaperService, parse_decision, run_ai_paper_cycle
from quantdesk.api.server import app
from quantdesk.datahub.db import Database


def _seed(service: AiPaperService, symbol: str = "BTCUSDT") -> None:
    now = int(time.time() * 1000)
    step = 900_000
    # The final row opens one interval ago and is therefore freshly closed.
    start = now - 240 * step
    rows = []
    for index in range(240):
        close = 90 + index * 0.05
        rows.append({
            "ts": start + index * step, "open": close - 0.02, "high": close + 0.2,
            "low": close - 0.2, "close": close, "volume": 1000 + index,
        })
    service.db.upsert_candles("bybit", symbol, "15m", rows)
    service.db.upsert_market_snapshot("bybit", symbol, {
        "mark_price": 100.0, "last_price": 100.0, "received_ts": now, "exchange_ts": now, "source": "test",
    })
    service.db.upsert_instrument_meta({
        "venue": "bybit", "symbol": symbol, "display_symbol": "BTC", "status": "Trading",
        "tick_size": 0.1, "qty_step": 0.001, "min_notional": 5,
        "raw": {"leverageFilter": {"maxLeverage": "100"}}, "collected_ts": now,
    })
    service.update_profile(symbols=[symbol])


def _seed_fibonacci_retracement(service: AiPaperService) -> None:
    now = int(time.time() * 1000)
    step = 900_000
    start = now - 60 * step
    closes = (
        [110 - index for index in range(21)]
        + [92 + index * 2 for index in range(15)]
        + [120 - index * (20 / 23) for index in range(24)]
    )
    rows = [
        {
            "ts": start + index * step,
            "open": close + 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000 + index,
        }
        for index, close in enumerate(closes)
    ]
    service.db.upsert_candles("bybit", "BTCUSDT", "15m", rows)
    service.db.upsert_market_snapshot("bybit", "BTCUSDT", {
        "mark_price": 99.0, "last_price": 99.0, "received_ts": now,
        "exchange_ts": now, "source": "test",
    })
    service.db.upsert_instrument_meta({
        "venue": "bybit", "symbol": "BTCUSDT", "display_symbol": "BTC", "status": "Trading",
        "tick_size": 0.1, "qty_step": 0.001, "min_notional": 5,
        "raw": {"leverageFilter": {"maxLeverage": "100"}}, "collected_ts": now,
    })
    service.update_profile(symbols=["BTCUSDT"], fib_only=True)


def test_parse_decision_accepts_json_fence_and_rejects_unknown_action():
    assert parse_decision('```json\n{"action":"hold","reason":"等待"}\n```')["action"] == "hold"
    with pytest.raises(AiPaperError, match="hold、open 或 close"):
        parse_decision('{"action":"buy"}')
    with pytest.raises(AiPaperError, match="非有限数字"):
        parse_decision('{"action":"hold","confidence":NaN}')


def test_ai_account_is_isolated_and_memory_survives_model_changes(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed(service)
        service.update_profile(initial_cash=20_000, max_leverage=20, style="conservative", horizon="short")
        result = service.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.91,
            "leverage": 15, "notional_pct": 70, "stop_loss": 98,
            "take_profit_1": 104, "take_profit_2": 108,
            "reason": "15m 趋势和量价结构一致", "lesson_applied": "只在止损明确时入场",
            "evidence_used": ["candidates.BTCUSDT.timeframes.15m"],
        })
        assert result["position"]["leverage"] == 3  # conservative hard cap
        with pytest.raises(AiPaperError, match="不能移除仍有 AI 模拟持仓"):
            service.update_profile(symbols=["ETHUSDT"])
        main_db = Database(tmp_path / "quantdesk.db")
        try:
            assert main_db.query("SELECT * FROM positions") == []
        finally:
            main_db.close()
        isolated = Database(tmp_path / "ai-paper-default.db")
        try:
            assert len(isolated.query("SELECT * FROM positions WHERE closed_ts IS NULL")) == 1
        finally:
            isolated.close()

        service.db.upsert_market_snapshot("bybit", "BTCUSDT", {
            "mark_price": 105.0, "last_price": 105.0, "received_ts": int(time.time() * 1000),
            "exchange_ts": int(time.time() * 1000), "source": "test",
        })
        service.close_position(result["position"]["id"])
        snapshot = service.snapshot()
        assert snapshot["metrics"]["closedTrades"] == 1
        assert snapshot["metrics"]["wins"] == 1
        assert snapshot["metrics"]["returnPct"] > 0
        memory = service.memory_path.read_text(encoding="utf-8")
        assert "模型供应商无关" in memory
        assert "BTCUSDT" in memory and "盈利" in memory
        assert "只在止损明确时入场" in memory
    finally:
        service.close()


def test_ai_simulation_charges_gate_vip0_taker_fee_on_entry_and_exit(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed(service)
        opened = service.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.9,
            "leverage": 2, "notional_pct": 10, "stop_loss": 98, "reason": "Gate 费率测试",
        })
        position = opened["position"]
        assert position["fees_paid"] == pytest.approx(position["notional"] * 0.0005, abs=1e-6)

        now = int(time.time() * 1000)
        service.db.upsert_market_snapshot("bybit", "BTCUSDT", {
            "mark_price": 105.0, "last_price": 105.0, "received_ts": now,
            "exchange_ts": now, "source": "test",
        })
        service.close_position(position["id"])
        snapshot = service.snapshot()
        policy = snapshot["feePolicy"]
        assert policy == {
            "benchmark": "Gate", "tier": "VIP 0", "product": "USDT 永续合约",
            "fillType": "taker", "takerFeeBps": 5.0, "feeRatePct": 0.05,
            "chargedOn": ["open", "close"], "formula": "成交名义价值 × 0.0500%",
            "effectiveDate": "2026-09-01",
        }
        trade = snapshot["journal"][0]
        expected = position["fees_paid"] + float(trade["qty"]) * float(trade["exit_price"]) * 0.0005
        assert trade["fees"] == pytest.approx(expected, abs=1e-6)
        assert snapshot["account"]["fees_paid"] == pytest.approx(trade["fees"], abs=1e-6)
        assert "Gate VIP 0" in snapshot["memory"]["content"]
        assert "累计手续费" in snapshot["memory"]["content"]
    finally:
        service.close()


def test_low_confidence_is_audited_but_never_opened(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed(service)
        with pytest.raises(AiPaperError, match="低于稳妥模式门槛"):
            service.run_once(force=True, decision_provider=lambda _evidence: {
                "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.3,
                "leverage": 2, "notional_pct": 10, "stop_loss": 98,
                "reason": "低置信度测试", "lesson_applied": "无", "evidence_used": [],
            })
        snapshot = service.snapshot()
        assert snapshot["account"]["positions"] == []
        assert snapshot["decisions"][0]["status"] == "rejected"
        assert "低于稳妥模式门槛" in snapshot["decisions"][0]["error"]
    finally:
        service.close()


def test_stale_mark_is_never_used_for_an_ai_trade(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed(service)
        stale = int(time.time() * 1000) - 180_000
        service.db.execute(
            "UPDATE market_snapshots SET received_ts=? WHERE venue='bybit' AND symbol='BTCUSDT'",
            (stale,),
        )
        with pytest.raises(AiPaperError, match="没有可用标记价"):
            service.run_once(force=True, decision_provider=lambda _evidence: {
                "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.9,
                "leverage": 2, "notional_pct": 10, "stop_loss": 98, "reason": "测试", 
            })
        assert service.snapshot()["account"]["positions"] == []
    finally:
        service.close()


def test_risk_reducing_close_does_not_require_entry_confidence(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed(service)
        opened = service.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.9,
            "leverage": 2, "notional_pct": 10, "stop_loss": 98, "reason": "开仓测试",
        })
        assert opened["status"] == "executed"
        closed = service.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "close", "symbol": "BTCUSDT", "confidence": 0.1,
            "reason": "风险上升，主动退出",
        })
        assert closed["status"] == "executed"
        assert service.snapshot()["account"]["positions"] == []
    finally:
        service.close()


def test_only_one_model_evaluation_can_run_per_profile(tmp_path: Path):
    first = AiPaperService(tmp_path)
    second = AiPaperService(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def provider(_evidence):
        entered.set()
        assert release.wait(5)
        return {"action": "hold", "confidence": 0.5, "reason": "并发测试"}

    try:
        _seed(first)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first.run_once, force=True, decision_provider=provider)
            assert entered.wait(5)
            assert second.run_once(force=True, decision_provider=lambda _e: {"action": "hold"})["status"] == "busy"
            release.set()
            assert future.result(timeout=5)["status"] == "held"
        assert len(first.db.query("SELECT * FROM ai_paper_decisions")) == 1
    finally:
        release.set()
        first.close()
        second.close()


def test_metrics_separate_total_and_realized_pnl(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed(service)
        service.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.9,
            "leverage": 2, "notional_pct": 10, "stop_loss": 98, "reason": "估值测试",
        })
        now = int(time.time() * 1000)
        service.db.upsert_market_snapshot("bybit", "BTCUSDT", {
            "mark_price": 105.0, "last_price": 105.0, "received_ts": now,
            "exchange_ts": now, "source": "test",
        })
        metrics = service.snapshot()["metrics"]
        assert metrics["realizedNetPnl"] == 0
        assert metrics["netPnl"] > 0
        assert metrics["returnPct"] > 0
    finally:
        service.close()


def test_style_exposure_limit_applies_to_the_whole_portfolio(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed(service, "BTCUSDT")
        _seed(service, "ETHUSDT")
        service.update_profile(symbols=["BTCUSDT", "ETHUSDT"])

        def open_symbol(symbol: str):
            return service.run_once(force=True, decision_provider=lambda _evidence: {
                "action": "open", "symbol": symbol, "side": "long", "confidence": 0.9,
                "leverage": 2, "notional_pct": 20, "stop_loss": 98, "reason": "组合敞口测试",
            })

        open_symbol("BTCUSDT")
        open_symbol("ETHUSDT")
        snapshot = service.snapshot()
        total_notional = sum(float(position["notional"]) for position in snapshot["account"]["positions"])
        cap = float(snapshot["account"]["equity"]) * snapshot["policy"]["max_notional_fraction"]
        assert total_notional <= cap + 1.0  # quantity-step rounding tolerance
    finally:
        service.close()


def test_entry_capacity_respects_venue_quantity_step_and_explains_rejection(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed(service, "BTCUSDT")
        _seed(service, "ETHUSDT")
        now = int(time.time() * 1000)
        service.db.upsert_market_snapshot("bybit", "ETHUSDT", {
            "mark_price": 2_500.0, "last_price": 2_500.0, "received_ts": now,
            "exchange_ts": now, "source": "test",
        })
        service.db.upsert_instrument_meta({
            "venue": "bybit", "symbol": "ETHUSDT", "display_symbol": "ETH", "status": "Trading",
            "tick_size": 0.01, "qty_step": 0.01, "min_notional": 5,
            "raw": {"leverageFilter": {"maxLeverage": "100"}}, "collected_ts": now,
        })
        service.update_profile(symbols=["BTCUSDT", "ETHUSDT"], style="aggressive")
        service.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.9,
            "leverage": 5, "notional_pct": 100, "stop_loss": 98,
            "reason": "建立接近组合敞口上限的测试仓位",
        })

        evidence = service.build_evidence()
        eth = next(item for item in evidence["candidates"] if item["symbol"] == "ETHUSDT")
        constraint = eth["executionConstraint"]
        assert constraint["canOpen"] is False
        assert constraint["minimumExecutableNotional"] == pytest.approx(25.0, abs=0.01)
        assert constraint["remainingExposureNotional"] < constraint["minimumExecutableNotional"]
        assert "数量步长 0.01" in constraint["reason"]

        with pytest.raises(AiPaperError, match="组合敞口上限仅剩 .*数量步长 0.01.*最少需要约 25.00 USDT.*未下单"):
            service.run_once(force=True, decision_provider=lambda supplied: {
                "action": "open", "symbol": "ETHUSDT", "side": "long", "confidence": 0.9,
                "leverage": 5, "notional_pct": 25, "stop_loss": 2_450,
                "reason": supplied["candidates"][0].get("executionConstraint", {}).get("reason", "测试"),
            })
        snapshot = service.snapshot()
        assert len(snapshot["account"]["positions"]) == 1
        assert "本轮未下单" in snapshot["profile"]["last_error"]
        assert snapshot["decisions"][0]["status"] == "rejected"
    finally:
        service.close()


def test_fibonacci_only_mode_enforces_zone_direction_and_agent_exit_plan(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed_fibonacci_retracement(service)
        evidence = service.build_evidence()
        candidate = evidence["candidates"][0]
        fibonacci = candidate["fibonacci"]
        assert evidence["fibOnly"] is True
        assert fibonacci["eligible"] is True
        assert fibonacci["side"] == "long"
        assert fibonacci["zoneLow"] <= 99 <= fibonacci["zoneHigh"]
        assert candidate["agentCouncil"]["otherAgentsExitPlanningOnly"] is True

        result = service.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.1,
            "leverage": 2, "notional_pct": 10,
            "stop_loss": 1, "take_profit_1": 2, "take_profit_2": 3,
            "reason": "价格进入 0.618–0.786 回调区",
        })
        assert result["status"] == "executed"
        position = service.snapshot()["account"]["positions"][0]
        triggers = {order["type"]: order["trigger_price"] for order in position["protective_orders"]}
        plan = fibonacci["exitPlan"]
        assert triggers["stop_loss"] == pytest.approx(plan["stopLoss"], abs=0.11)
        assert triggers["take_profit_1"] == pytest.approx(plan["takeProfit1"], abs=0.11)
        assert triggers["take_profit_2"] == pytest.approx(plan["takeProfit2"], abs=0.11)
        decision = service.snapshot()["decisions"][0]
        assert decision["evidence"]["fibOnly"] is True
        assert decision["evidence"]["agentsConsulted"] == [
            "structure_agent", "volatility_agent", "risk_agent",
        ]
    finally:
        service.close()


def test_fibonacci_only_mode_holds_without_calling_model_outside_zone(tmp_path: Path):
    service = AiPaperService(tmp_path)
    called = False

    def provider(_evidence):
        nonlocal called
        called = True
        return {"action": "open"}

    try:
        _seed(service)
        service.update_profile(fib_only=True)
        result = service.run_once(force=True, decision_provider=provider)
        assert result["status"] == "held"
        assert called is False
        decision = service.snapshot()["decisions"][0]
        assert decision["model_profile"] == "local/fibonacci-gate"
        assert "0.618–0.786" in decision["reason"]
    finally:
        service.close()


def test_multiple_simulations_keep_cash_positions_memory_and_conditions_isolated(tmp_path: Path):
    first = AiPaperService(tmp_path)
    second = AiPaperService.create(
        tmp_path, name="激进 BTC", initial_cash=25_000, max_leverage=8,
        horizon="short", style="aggressive", fib_only=False, symbols=["BTCUSDT"],
    )
    try:
        _seed(first)
        _seed(second)
        first.update_profile(name="稳妥 BTC", initial_cash=10_000, symbols=["BTCUSDT"])
        first_open = first.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.9,
            "leverage": 2, "notional_pct": 10, "stop_loss": 98, "reason": "独立多头",
        })
        second_open = second.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "BTCUSDT", "side": "short", "confidence": 0.9,
            "leverage": 5, "notional_pct": 20, "stop_loss": 102, "reason": "独立空头",
        })
        assert first_open["position"]["side"] == "long"
        assert second_open["position"]["side"] == "short"
        first_snapshot = first.snapshot()
        second_snapshot = second.snapshot()
        assert first_snapshot["account"]["initial_cash"] == 10_000
        assert second_snapshot["account"]["initial_cash"] == 25_000
        assert first_snapshot["account"]["positions"][0]["simulation"]["name"] == "稳妥 BTC"
        assert second_snapshot["account"]["positions"][0]["simulation"] == {
            "id": second.profile_id, "profileId": second.profile_id,
            "name": "激进 BTC", "style": "aggressive",
            "styleLabel": "激进", "horizon": "short", "horizonLabel": "短线",
            "fibOnly": False, "entryLabel": "多 Agent 综合", "maxLeverage": 8.0,
            "configRevision": 1, "symbols": ["BTCUSDT"],
        }
        assert first.account_path != second.account_path
        assert first.memory_path != second.memory_path
        assert "稳妥 BTC" in first.memory_path.read_text(encoding="utf-8")
        assert "激进 BTC" in second.memory_path.read_text(encoding="utf-8")
        cycle = run_ai_paper_cycle(tmp_path)
        assert {item["profileId"] for item in cycle["profiles"]} == {"default", second.profile_id}
    finally:
        first.close()
        second.close()


def test_running_conditions_are_immutable_and_destructive_refusals_release_leases(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed(service)
        service.db.execute(
            "UPDATE ai_paper_profiles SET enabled=1 WHERE id=?", (service.profile_id,)
        )
        with pytest.raises(AiPaperError, match="运行中的模拟不能修改交易条件"):
            service.update_profile(style="aggressive")
        service.update_profile(name="运行中可改名")
        service.db.execute(
            "UPDATE ai_paper_profiles SET enabled=0 WHERE id=?", (service.profile_id,)
        )
        service.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.9,
            "leverage": 2, "notional_pct": 10, "stop_loss": 98, "reason": "租约释放测试",
        })
        with pytest.raises(AiPaperError, match="未平仓"):
            service.reset()
        assert service.db.query("SELECT * FROM ai_paper_leases WHERE profile_id=?", (service.profile_id,)) == []
        with pytest.raises(AiPaperError, match="未平仓"):
            service.delete()
        assert service.db.query("SELECT * FROM ai_paper_leases WHERE profile_id=?", (service.profile_id,)) == []
    finally:
        service.close()


def test_stop_during_background_model_evaluation_prevents_the_pending_trade(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        _seed(service)
        service.db.execute(
            "UPDATE ai_paper_profiles SET enabled=1 WHERE id=?", (service.profile_id,)
        )

        def provider(_evidence):
            service.db.execute(
                "UPDATE ai_paper_profiles SET enabled=0 WHERE id=?", (service.profile_id,)
            )
            return {
                "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.9,
                "leverage": 2, "notional_pct": 10, "stop_loss": 98, "reason": "已经过时的开仓决定",
            }

        result = service.run_once(force=False, decision_provider=provider)
        assert result["status"] == "held"
        snapshot = service.snapshot()
        assert snapshot["account"]["positions"] == []
        assert snapshot["decisions"][0]["model_profile"] == "local/stop-gate"
        assert "停止" in snapshot["decisions"][0]["reason"]
    finally:
        service.close()


@pytest.mark.asyncio
async def test_ai_paper_api_defaults_stopped_and_updates_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QUANTDESK_HOME", str(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = await client.get("/api/ai-paper")
        assert before.status_code == 200
        assert before.json()["simulationOnly"] is True
        assert before.json()["profile"]["enabled"] is False
        saved = await client.put("/api/ai-paper/config", json={
            "initialCash": 50_000, "maxLeverage": 8, "horizon": "swing", "style": "aggressive",
            "fibOnly": True, "symbols": ["BTCUSDT", "ETHUSDT"],
        })
        assert saved.status_code == 200, saved.text
        assert saved.json()["horizon"] == "swing"
        assert saved.json()["style"] == "aggressive"
        assert saved.json()["max_leverage"] == 8
        assert saved.json()["fib_only"] is True
        assert saved.json()["symbols"] == ["BTCUSDT", "ETHUSDT"]


@pytest.mark.asyncio
async def test_ai_paper_api_creates_lists_and_deletes_an_independent_simulation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QUANTDESK_HOME", str(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.get("/api/ai-paper")
        created = await client.post("/api/ai-paper/profiles", json={
            "name": "Fib 加密短线", "initialCash": 12_000, "maxLeverage": 4,
            "horizon": "short", "style": "conservative", "fibOnly": True,
            "symbols": ["BTCUSDT", "ETHUSDT"],
        })
        assert created.status_code == 201, created.text
        profile_id = created.json()["profile"]["id"]
        assert profile_id != "default"
        listed = await client.get("/api/ai-paper/profiles")
        assert {row["profile"]["name"] for row in listed.json()["profiles"]} == {"主模拟", "Fib 加密短线"}
        selected = await client.get(f"/api/ai-paper/profiles/{profile_id}")
        assert selected.json()["profile"]["fib_only"] is True
        removed = await client.delete(f"/api/ai-paper/profiles/{profile_id}?confirm=true")
        assert removed.status_code == 200
        missing = await client.get(f"/api/ai-paper/profiles/{profile_id}")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_ai_paper_start_translates_configuration_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QUANTDESK_HOME", str(tmp_path))

    def refuse(_self, _enabled):
        raise AiPaperError("缺少测试模型凭据")

    monkeypatch.setattr(AiPaperService, "set_enabled", refuse)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/ai-paper/start")
    assert response.status_code == 409
    assert response.json()["detail"] == "缺少测试模型凭据"


# ---------------------------------------------------------------- atomic start
#
# The bug these cover: the start button enabled whatever rules were already stored,
# so "short + aggressive, SOXL/SOXS removed" on screen ran the previous configuration
# and nothing in the record said so. Starting must save the page's rules first, and
# must not start anything at all when that save cannot be completed.

TEST_KEY = "qd-test-key-9f3a1c"  # shape-valid, never sent anywhere: these tests do not call a model


def _allow_start(monkeypatch) -> None:
    """Let the credential gate pass without a real provider key."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)


async def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _new_profile(client, **overrides) -> str:
    payload = {
        "name": "临时模拟", "initialCash": 100_000, "maxLeverage": 3,
        "horizon": "short", "style": "conservative", "fibOnly": False,
        "symbols": ["BTCUSDT", "ETHUSDT", "SOXLUSDT", "SOXSUSDT"],
    }
    payload.update(overrides)
    created = await client.post("/api/ai-paper/profiles", json=payload)
    assert created.status_code == 201, created.text
    return created.json()["profile"]["id"]


@pytest.mark.asyncio
async def test_starting_saves_the_page_rules_before_enabling(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QUANTDESK_HOME", str(tmp_path))
    _allow_start(monkeypatch)
    async with await _client() as client:
        profile_id = await _new_profile(client)
        started = await client.post(f"/api/ai-paper/profiles/{profile_id}/start", json={
            "name": "短线激进模拟", "initialCash": 100_000, "maxLeverage": 8,
            "horizon": "short", "style": "aggressive", "fibOnly": False,
            "symbols": ["AAPLUSDT", "NVDAUSDT", "BTCUSDT", "ETHUSDT"],
        })
        assert started.status_code == 200, started.text
        stored = (await client.get(f"/api/ai-paper/profiles/{profile_id}")).json()

    profile = stored["profile"]
    assert profile["enabled"] is True
    assert profile["style"] == "aggressive"
    assert profile["horizon"] == "short"
    assert profile["max_leverage"] == 8
    assert profile["name"] == "短线激进模拟"
    assert "SOXLUSDT" not in profile["symbols"]
    assert "SOXSUSDT" not in profile["symbols"]
    assert set(profile["symbols"]) == {"AAPLUSDT", "NVDAUSDT", "BTCUSDT", "ETHUSDT"}


@pytest.mark.asyncio
async def test_the_start_response_carries_the_rules_it_saved(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QUANTDESK_HOME", str(tmp_path))
    _allow_start(monkeypatch)
    async with await _client() as client:
        profile_id = await _new_profile(client)
        body = {
            "name": "短线激进模拟", "initialCash": 100_000, "maxLeverage": 8,
            "horizon": "short", "style": "aggressive", "fibOnly": True,
            "symbols": ["BTCUSDT", "ETHUSDT"],
        }
        response = await client.post(f"/api/ai-paper/profiles/{profile_id}/start", json=body)
        assert response.status_code == 200, response.text
        profile = response.json()["profile"]

    # The page verifies this receipt against what it displayed; it must be the saved
    # rules, not an echo of the request or the previous configuration.
    assert profile["style"] == "aggressive"
    assert profile["horizon"] == "short"
    assert profile["max_leverage"] == 8
    assert profile["fib_only"] is True
    assert profile["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert profile["config_revision"] >= 2


@pytest.mark.asyncio
async def test_a_rejected_configuration_leaves_the_instance_stopped(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QUANTDESK_HOME", str(tmp_path))
    _allow_start(monkeypatch)
    async with await _client() as client:
        profile_id = await _new_profile(client)
        before = (await client.get(f"/api/ai-paper/profiles/{profile_id}")).json()["profile"]
        rejected = await client.post(f"/api/ai-paper/profiles/{profile_id}/start", json={
            "name": "坏配置", "initialCash": 100_000, "maxLeverage": 3,
            "horizon": "short", "style": "yolo", "fibOnly": False,
            "symbols": ["BTCUSDT"],
        })
        after = (await client.get(f"/api/ai-paper/profiles/{profile_id}")).json()["profile"]

    assert rejected.status_code == 409
    assert after["enabled"] is False
    # Nothing was partially written: the old rules are exactly as they were, and the
    # revision did not move.
    assert after["style"] == before["style"] == "conservative"
    assert after["name"] == before["name"]
    assert after["config_revision"] == before["config_revision"]


@pytest.mark.asyncio
async def test_a_failed_save_never_starts_the_old_configuration(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QUANTDESK_HOME", str(tmp_path))
    _allow_start(monkeypatch)
    calls: list[bool] = []

    def explode(self, **_updates):
        calls.append(True)
        raise AiPaperError("保存失败（测试注入）")

    async with await _client() as client:
        profile_id = await _new_profile(client)
        # Patch only the save step, after the instance exists: creation saves too.
        monkeypatch.setattr(AiPaperService, "_update_profile", explode)
        response = await client.post(f"/api/ai-paper/profiles/{profile_id}/start", json={
            "name": "不该启动", "initialCash": 100_000, "maxLeverage": 8,
            "horizon": "short", "style": "aggressive", "fibOnly": False,
            "symbols": ["BTCUSDT"],
        })
        monkeypatch.undo()
        monkeypatch.setenv("QUANTDESK_HOME", str(tmp_path))
        stored = (await client.get(f"/api/ai-paper/profiles/{profile_id}")).json()["profile"]

    assert calls, "保存步骤必须被调用"
    assert response.status_code == 409
    assert stored["enabled"] is False, "保存失败时绝不能启动旧配置"
    assert stored["style"] == "conservative", "旧配置保持不变"


@pytest.mark.asyncio
async def test_a_held_symbol_cannot_be_dropped_by_the_atomic_start(tmp_path: Path, monkeypatch):
    """A start that would orphan an open position is refused, and nothing changes."""
    monkeypatch.setenv("QUANTDESK_HOME", str(tmp_path))
    _allow_start(monkeypatch)
    service = AiPaperService.create(
        tmp_path, name="持有 SOXL", initial_cash=100_000, max_leverage=3,
        horizon="short", style="conservative", fib_only=False,
        symbols=["BTCUSDT", "SOXLUSDT"],
    )
    try:
        _seed(service, "SOXLUSDT")
        opened = service.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "SOXLUSDT", "side": "long", "confidence": 0.9,
            "leverage": 2, "notional_pct": 10, "stop_loss": 98, "reason": "测试持仓",
        })
        assert opened["status"] == "executed", opened
        with pytest.raises(AiPaperError) as refused:
            service.start_with_config(
                name="临时模拟", initial_cash=100_000, max_leverage=3,
                horizon="short", style="conservative", fib_only=False,
                symbols=["BTCUSDT"],
            )
        assert "SOXLUSDT" in str(refused.value)
        after = service.profile()
        assert after["enabled"] is False, "拒绝后必须保持停止"
        assert "SOXLUSDT" in after["symbols"], "被拒绝的配置不得写入"
        assert service.snapshot()["account"]["positions"][0]["symbol"] == "SOXLUSDT"
    finally:
        service.close()


def test_the_config_revision_moves_only_when_the_rules_change(tmp_path: Path):
    service = AiPaperService(tmp_path)
    try:
        assert service.profile()["config_revision"] == 1
        service.update_profile(style="aggressive")
        assert service.profile()["config_revision"] == 2
        service.update_profile(style="aggressive")
        assert service.profile()["config_revision"] == 2, "重复保存同一配置不应增加版本号"
        service.update_profile(symbols=["BTCUSDT", "ETHUSDT"])
        assert service.profile()["config_revision"] == 3
        service.update_profile(symbols=["ETHUSDT", "BTCUSDT"])
        assert service.profile()["config_revision"] == 3, "合约顺序不同不算配置变化"
    finally:
        service.close()


def test_two_instances_keep_their_own_rules_when_both_are_started(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", TEST_KEY)
    first = AiPaperService.create(
        tmp_path, name="短线激进", initial_cash=100_000, max_leverage=8,
        horizon="short", style="aggressive", fib_only=False, symbols=["BTCUSDT"],
    )
    second = AiPaperService.create(
        tmp_path, name="中长线稳妥", initial_cash=50_000, max_leverage=3,
        horizon="swing", style="conservative", fib_only=True, symbols=["ETHUSDT"],
    )
    try:
        first.start_with_config(
            name="短线激进", initial_cash=100_000, max_leverage=8,
            horizon="short", style="aggressive", fib_only=False, symbols=["BTCUSDT"],
        )
        second.start_with_config(
            name="中长线稳妥", initial_cash=50_000, max_leverage=3,
            horizon="swing", style="conservative", fib_only=True, symbols=["ETHUSDT"],
        )
        one, two = first.profile(), second.profile()
        assert (one["style"], one["horizon"], one["symbols"]) == ("aggressive", "short", ["BTCUSDT"])
        assert (two["style"], two["horizon"], two["symbols"]) == ("conservative", "swing", ["ETHUSDT"])
        assert one["enabled"] is True and two["enabled"] is True
    finally:
        first.close()
        second.close()


def test_decision_evidence_records_the_revision_and_rule_snapshot(tmp_path: Path):
    """A decision must say which version of the rules produced it."""
    service = AiPaperService.create(
        tmp_path, name="短线激进", initial_cash=100_000, max_leverage=8,
        horizon="short", style="aggressive", fib_only=False, symbols=["BTCUSDT"],
    )
    try:
        _seed(service)
        assert service.profile()["config_revision"] == 1
        service.update_profile(max_leverage=5)  # a real change: revision moves to 2
        service.run_once(force=True, decision_provider=lambda _evidence: {
            "action": "open", "symbol": "BTCUSDT", "side": "long", "confidence": 0.9,
            "leverage": 2, "notional_pct": 10, "stop_loss": 98, "reason": "证据追踪",
        })
        snapshot = service.snapshot()
        simulation = snapshot["decisions"][0]["evidence"]["simulation"]
        assert simulation["configRevision"] == 2
        assert simulation["style"] == "aggressive"
        assert simulation["horizon"] == "short"
        assert simulation["fibOnly"] is False
        assert simulation["maxLeverage"] == 5.0
        assert simulation["symbols"] == ["BTCUSDT"]
        assert simulation["profileId"] == service.profile_id
        # An open position carries the rules it was opened under, not the current ones.
        position_conditions = snapshot["account"]["positions"][0]["simulation"]
        assert position_conditions["configRevision"] == 2
        assert position_conditions["symbols"] == ["BTCUSDT"]
    finally:
        service.close()


@pytest.mark.asyncio
async def test_the_legacy_bodyless_start_still_works(tmp_path: Path, monkeypatch):
    """Old clients keep the enable-only call; it returns the profile, not a snapshot."""
    monkeypatch.setenv("QUANTDESK_HOME", str(tmp_path))
    _allow_start(monkeypatch)
    async with await _client() as client:
        profile_id = await _new_profile(client)
        legacy = await client.post(f"/api/ai-paper/profiles/{profile_id}/start")
        assert legacy.status_code == 200, legacy.text
        body = legacy.json()

    assert body["enabled"] is True, "旧接口仍然只做启用"
    assert "profile" not in body, "旧接口不返回 snapshot 形状"
    assert body["style"] == "conservative", "旧接口不修改任何规则"
