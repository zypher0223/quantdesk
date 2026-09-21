"""Research layer: price structure, trading-plan auditing, and the endpoint."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

from quantdesk.api.server import app
from quantdesk.research import (
    MAX_POSITION_PCT,
    build_bundle,
    compute_price_structure,
    parse_report,
    report_markdown,
    validate_plan,
    validate_report,
)

HOUR = 3_600_000
START = 1_700_000_000_000


def ramp_candles(count: int = 120, low: float = 90.0, high: float = 112.0) -> list[dict]:
    """Alternating swings so pivot detection has something real to find."""
    rows = []
    for index in range(count):
        # Zig-zag between `low` and `high` over eight-bar legs.
        phase = (index % 16) / 16
        price = low + (high - low) * (phase if phase <= 0.5 else 1 - phase) * 2
        rows.append(
            {
                "ts": START + index * HOUR,
                "open": price,
                "high": price + 1.5,
                "low": price - 1.5,
                "close": price,
                "volume": 5_000.0,
            }
        )
    return rows


class FakeSpec:
    display_symbol = "AMD"
    venue_symbol = "AMDSTOCKUSDT"
    product_type = "stock"
    risk_class = "sector"

    @property
    def is_crypto(self) -> bool:
        return False


def sample_bundle(**overrides):
    frames = {
        "15m": {"close": 517.4, "stance": "neutral", "score": 0.0, "trend": 1, "momentum": -1, "volume": 0, "adx": 30.1, "rsi": 51.2, "session_thin": True, "bars": 400},
        "1h": {"close": 517.9, "stance": "bear", "score": -0.333, "trend": 1, "momentum": -1, "volume": -1, "adx": 45.7, "rsi": 67.3, "session_thin": False, "bars": 400},
        "4h": {"close": 517.5, "stance": "bull", "score": 0.667, "trend": 1, "momentum": 0, "volume": 1, "adx": 36.9, "rsi": 74.4, "session_thin": False, "bars": 400},
        "1d": {"close": 516.0, "stance": "bull", "score": 0.667, "trend": 0, "momentum": 1, "volume": 1, "adx": 13.6, "rsi": 65.8, "session_thin": False, "bars": 137},
    }
    structure = {
        "last_close": 517.4,
        "atr": 4.0,
        # The engine always emits this explanatory field; the fixture carries it so
        # the audit's "not a reading" branch is exercised rather than the "missing".
        "atr_source": "最近14根K线真实波幅的均值",
        "recent_high": 521.6,
        "recent_low": 501.9,
        "swing_highs": [521.6, 519.8],
        "swing_lows": [501.9, 508.2],
        "bars": 400,
    }
    defaults = dict(
        spec=FakeSpec(),
        timeframe="1h",
        frames=frames,
        resonance={"score": 0.4, "score_100": 70.0, "label": "偏多", "unavailable": [{"interval": "1d", "bars": 137, "required": 220}]},
        ticker={"markPrice": "517.44", "indexPrice": "517.21", "fundingRate": "0", "fundingIntervalHour": 8, "openInterest": "3302.78", "openInterestValue": "1708990.48", "turnover24h": "99774799", "volume24h": "300608", "price24hPcnt": "0.017121"},
        funding_points=50,
        funding_all_zero=True,
        signals={"resonance_score_100": 70.0, "stance_4h": "bull"},
        backtest={"net_return_pct": -5.38, "max_drawdown_pct": 6.32, "win_rate_pct": 28.6, "trades": 14, "total_fees": 273.6, "total_funding": -0.25, "liquidations": 0, "timeframe": "1h", "warnings": []},
        price_structure=structure,
        missing=["1d 仅有 137/220 根已收盘K线"],
    )
    defaults.update(overrides)
    return build_bundle(**defaults)


LONG_PLAN = {
    "direction": "long",
    "entry": 517.44,
    "entry_zone": [516.0, 518.5],
    "stop_loss": 508.2,
    "take_profit_1": 521.6,
    "take_profit_2": 530.0,
    "risk_reward": 0.45,
    "position_size_pct": 10,
    "timeframe": "1h",
    "rationale": "4h.stance 为 bull，止损放在 swing_lows 之下",
    "trigger": None,
    "valid_until": "本根K线收盘前",
}


class PriceStructureTests(unittest.TestCase):
    def test_finds_swings_and_atr(self):
        structure = compute_price_structure(ramp_candles())
        self.assertTrue(structure["swing_highs"], "should detect pivot highs")
        self.assertTrue(structure["swing_lows"], "should detect pivot lows")
        self.assertGreater(structure["atr"], 0)
        self.assertGreaterEqual(structure["recent_high"], structure["recent_low"])
        self.assertEqual(structure["bars"], 120)
        # Swing highs must sit above swing lows on a bounded zig-zag.
        self.assertGreater(min(structure["swing_highs"]), min(structure["swing_lows"]))

    def test_empty_input_is_not_invented(self):
        self.assertEqual(compute_price_structure([]), {})

    def test_range_position_is_reported(self):
        structure = compute_price_structure(ramp_candles())
        position = structure["recent_range_position_pct"]
        self.assertIsNotNone(position)
        self.assertGreaterEqual(position, 0)
        self.assertLessEqual(position, 100)


class PlanValidationTests(unittest.TestCase):
    def test_coherent_long_plan_passes_and_recomputes_ratios(self):
        bundle = sample_bundle()
        result = validate_plan(LONG_PLAN, bundle)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["direction"], "long")
        self.assertAlmostEqual(result["computed"]["riskPerUnit"], 9.24, places=2)
        self.assertAlmostEqual(result["computed"]["rewardPerUnit"], 4.16, places=2)
        self.assertAlmostEqual(result["computed"]["riskReward"], 0.45, places=2)
        self.assertAlmostEqual(result["computed"]["stopAtrMultiple"], 2.31, places=2)

    def test_inverted_levels_are_rejected(self):
        bundle = sample_bundle()
        bad = {**LONG_PLAN, "stop_loss": 530.0, "take_profit_1": 540.0}
        result = validate_plan(bad, bundle)
        self.assertFalse(result["ok"])
        self.assertTrue(any("顺序" in problem for problem in result["problems"]))

    def test_stop_inside_the_noise_is_rejected(self):
        bundle = sample_bundle()
        tight = {**LONG_PLAN, "stop_loss": 516.0}  # 1.44 away vs ATR 4.0
        result = validate_plan(tight, bundle)
        self.assertFalse(result["ok"])
        self.assertTrue(any("噪音止损" in problem for problem in result["problems"]))

    def test_short_plan_ordering(self):
        bundle = sample_bundle()
        short = {
            "direction": "short",
            "entry": 517.44,
            "stop_loss": 526.0,
            "take_profit_1": 501.9,
            "take_profit_2": 490.0,
            "position_size_pct": 10,
        }
        result = validate_plan(short, bundle)
        self.assertTrue(result["ok"], result["problems"])
        inverted = {**short, "stop_loss": 510.0, "take_profit_1": 520.0}
        self.assertFalse(validate_plan(inverted, bundle)["ok"])

    def test_mismatched_claimed_ratio_is_corrected_not_trusted(self):
        bundle = sample_bundle()
        lying = {**LONG_PLAN, "risk_reward": 3.0}
        result = validate_plan(lying, bundle)
        self.assertAlmostEqual(result["computed"]["riskReward"], 0.45, places=2)
        self.assertTrue(any("重算" in note for note in result["notes"]))

    def test_oversized_position_is_noted_and_invalid_size_rejected(self):
        bundle = sample_bundle()
        big = {**LONG_PLAN, "position_size_pct": 80}
        result = validate_plan(big, bundle)
        self.assertTrue(any(f"{MAX_POSITION_PCT:g}%" in note for note in result["notes"]))
        nonsense = {**LONG_PLAN, "position_size_pct": -5}
        self.assertFalse(validate_plan(nonsense, bundle)["ok"])

    def test_missing_plan_and_missing_levels_are_reported(self):
        bundle = sample_bundle()
        self.assertFalse(validate_plan(None, bundle)["ok"])
        incomplete = {"direction": "long", "entry": 517.44}
        result = validate_plan(incomplete, bundle)
        self.assertFalse(result["ok"])
        self.assertTrue(any("不是数字" in problem for problem in result["problems"]))

    def test_stop_above_last_swing_low_is_flagged_as_scalpable(self):
        bundle = sample_bundle()
        above = {**LONG_PLAN, "stop_loss": 510.0}  # still > 0.5 ATR but above the 508.2 swing low
        result = validate_plan(above, bundle)
        self.assertTrue(any("摆动低点" in note for note in result["notes"]))


class ReportAuditTests(unittest.TestCase):
    def test_report_numbers_are_checked_against_the_bundle(self):
        bundle = sample_bundle()
        report = {
            "headline": "4h 偏多但 1h 转弱",
            "facts": [{"statement": "4h 的 ADX 为 36.9，RSI 74.4", "evidence": ["4h.adx", "4h.rsi"]}],
            "inferences": [{"statement": "共振 70 分", "basis": ["resonance.score_100"], "confidence": "medium"}],
            "trading_plan": LONG_PLAN,
            "invalidation": ["若 4h 立场转为 bear 则失效"],
            "data_caveats": ["15m 处于休市空 bar"],
        }
        validation = validate_report(report, bundle, validate_plan(LONG_PLAN, bundle))
        self.assertEqual(validation["unsupportedNumbers"], [])
        self.assertEqual(validation["unknownCitedKeys"], [])
        self.assertTrue(validation["verified"])

    def test_invented_number_is_caught(self):
        bundle = sample_bundle()
        report = {
            "headline": "价格在 640.5 处遇阻",
            "facts": [{"statement": "阻力位于 640.5", "evidence": ["4h.close"]}],
        }
        validation = validate_report(report, bundle, None)
        self.assertTrue(validation["unsupportedNumbers"])
        self.assertFalse(validation["verified"])

    def test_level_derived_from_the_evidence_is_not_a_hallucination(self):
        # A scenario may name a level computed from a reading (entry minus one
        # ATR). It sits inside the price range the evidence spans.
        bundle = sample_bundle()
        report = {
            "scenarios": [{"name": "回踩", "condition": "收盘不低于 512.4", "implication": "结构维持"}],
        }
        validation = validate_report(report, bundle, None)
        self.assertEqual(validation["unsupportedNumbers"], [])
        self.assertEqual([item["number"] for item in validation["derivedNumbers"]], [512.4])
        self.assertTrue(validation["verified"])

    def test_price_far_outside_the_evidence_range_is_still_rejected(self):
        bundle = sample_bundle()
        for invented in (300.0, 640.5):
            report = {"scenarios": [{"name": "远端", "condition": f"若跌到 {invented}", "implication": "结构破坏"}]}
            validation = validate_report(report, bundle, None)
            self.assertTrue(validation["unsupportedNumbers"], f"{invented} is nowhere near the evidence")

    def test_engine_computed_ratios_are_accepted(self):
        # A report that quotes the engine's own ratios must not be flagged: the
        # engine computed them, so they are accounted for by construction.
        bundle = sample_bundle()
        plan_check = validate_plan(LONG_PLAN, bundle)
        report = {
            "facts": [
                {"statement": "回测净收益 -5.38%，最大回撤 6.32%", "evidence": ["backtest.net_return_pct"]},
            ],
            "inferences": [
                {"statement": f"盈亏比 {plan_check['computed']['riskReward']}，止损约 {plan_check['computed']['stopAtrMultiple']} 倍 ATR",
                 "basis": ["price_structure.atr"]},
            ],
            "trading_plan": LONG_PLAN,
        }
        validation = validate_report(report, bundle, plan_check)
        self.assertEqual(validation["unsupportedNumbers"], [])
        self.assertTrue(validation["verified"])

    def test_plan_levels_named_in_prose_are_accepted(self):
        bundle = sample_bundle()
        plan_check = validate_plan(LONG_PLAN, bundle)
        report = {
            "headline": "等回踩 517.44 附近，止损 508.2",
            "facts": [{"statement": "入场 517.44，止损 508.2", "evidence": ["4h.stance"]}],
            "trading_plan": LONG_PLAN,
        }
        validation = validate_report(report, bundle, plan_check)
        self.assertEqual(validation["unsupportedNumbers"], [])

    def test_stances_are_valid_evidence_descriptive_prose_is_not(self):
        # A stance is a categorical reading and may back a directional claim.
        bundle = sample_bundle()
        ok = {"facts": [{"statement": "4h 立场为 bull", "evidence": ["4h.stance"]}]}
        validation = validate_report(ok, bundle, None)
        self.assertEqual(validation["nonEvidenceCitations"], [])
        self.assertTrue(validation["verified"], validation)

        # The ATR source note is explanatory text, not a measurement.
        prose = {"facts": [{"statement": "ATR 来自近期波幅", "evidence": ["price_structure.atr_source"]}]}
        validation = validate_report(prose, bundle, None)
        self.assertEqual(validation["nonEvidenceCitations"], ["price_structure.atr_source"])
        self.assertFalse(validation["verified"])

    def test_advisory_wording_is_caught_but_plan_prose_is_not(self):
        bundle = sample_bundle()
        plan_check = validate_plan(LONG_PLAN, bundle)
        for text in ("回踩做多计划失效", "追多盈亏比不理想", "计划在 517.44 做多"):
            report = {"inferences": [{"statement": text, "basis": ["4h.adx"]}], "trading_plan": LONG_PLAN}
            self.assertEqual(validate_report(report, bundle, plan_check)["directives"], [], text)
        for text in ("建议买入该合约", "应当做多", "可以考虑建立多头仓位"):
            report = {"inferences": [{"statement": text, "basis": ["4h.adx"]}]}
            self.assertTrue(validate_report(report, bundle, None)["directives"], text)

    def test_sign_matters(self):
        # The bundle has a -5.38% return. Quoting it as +5.38% is a different
        # claim and must not be quietly accepted.
        bundle = sample_bundle()
        honest = {"facts": [{"statement": "回测净收益 -5.38%", "evidence": ["backtest.net_return_pct"]}]}
        self.assertEqual(validate_report(honest, bundle, None)["unsupportedNumbers"], [])
        flipped = {"facts": [{"statement": "回测净收益 5.38%", "evidence": ["backtest.net_return_pct"]}]}
        validation = validate_report(flipped, bundle, None)
        self.assertEqual(validation["signFlippedNumbers"][0]["number"], 5.38)
        self.assertFalse(validation["verified"], "a flipped sign is a different number")

    def test_far_off_price_is_rejected_and_nearby_thresholds_are_reported_as_derived(self):
        bundle = sample_bundle()
        far = {"facts": [{"statement": "阻力在 640.5", "evidence": ["4h.close"]}]}
        self.assertTrue(validate_report(far, bundle, None)["unsupportedNumbers"])
        near = {"invalidation": ["收盘跌破 512.4"]}
        validation = validate_report(near, bundle, None)
        self.assertEqual(validation["unsupportedNumbers"], [])
        # Reported as derived, not as verified evidence: from digits alone the
        # audit cannot prove the model computed it rather than invented it.
        self.assertEqual([item["number"] for item in validation["derivedNumbers"]], [512.4])

    def test_a_range_is_not_read_as_a_negative_number(self):
        bundle = sample_bundle()
        report = {"facts": [{"statement": "价格在 501.9-521.6 区间内运行", "evidence": ["price_structure.recent_low"]}]}
        validation = validate_report(report, bundle, None)
        self.assertEqual(validation["unsupportedNumbers"], [], "-501.9 and -521.6 are the parsed artifacts being avoided")

    def test_a_ratio_may_be_quoted_as_a_percentage(self):
        bundle = sample_bundle()
        # 24h change is stored as a ratio; the report writes it with a % sign.
        report = {"facts": [{"statement": "24H 价格变动 +1.71%", "evidence": ["derivatives.price_24h_pct"]}]}
        validation = validate_report(report, bundle, None)
        self.assertEqual(validation["unsupportedNumbers"], [])

    def test_unknown_citation_key_is_caught(self):
        bundle = sample_bundle()
        report = {"facts": [{"statement": "4h 偏多", "evidence": ["4h.nonexistent_field"]}]}
        validation = validate_report(report, bundle, None)
        self.assertEqual(validation["unknownCitedKeys"], ["4h.nonexistent_field"])
        self.assertFalse(validation["verified"])

    def test_plan_levels_are_not_treated_as_hallucinations(self):
        bundle = sample_bundle()
        report = {"facts": [{"statement": "计划入场 517.44，止损 508.2", "evidence": ["price_structure.atr"]}], "trading_plan": LONG_PLAN}
        validation = validate_report(report, bundle, validate_plan(LONG_PLAN, bundle))
        self.assertEqual(validation["unsupportedNumbers"], [], "plan prices are model-chosen, not bundle facts")

    def test_plan_containing_action_words_is_allowed_but_body_is_not(self):
        bundle = sample_bundle()
        with_plan = {**LONG_PLAN, "rationale": "在此买入并设置止损"}
        validation = validate_report({"trading_plan": with_plan}, bundle, validate_plan(with_plan, bundle))
        self.assertEqual(validation["directives"], [], "the plan is expected to be actionable")
        stray = {"facts": [{"statement": "建议买入该合约", "evidence": ["4h.stance"]}]}
        self.assertTrue(validate_report(stray, bundle, None)["directives"])


class ParsingTests(unittest.TestCase):
    def test_parses_plain_and_fenced_json(self):
        payload = {"headline": "x", "trading_plan": LONG_PLAN}
        self.assertEqual(parse_report(json.dumps(payload)), payload)
        self.assertEqual(parse_report(f"```json\n{json.dumps(payload)}\n```"), payload)
        self.assertEqual(parse_report(f"好的：{json.dumps(payload)} 以上"), payload)

    def test_rejects_non_json(self):
        self.assertIsNone(parse_report("没有 JSON"))
        self.assertIsNone(parse_report(""))
        self.assertIsNone(parse_report("{不是合法 JSON"))


class MarkdownTests(unittest.TestCase):
    def test_archive_carries_plan_and_provenance(self):
        bundle = sample_bundle()
        report = {
            "headline": "4h 偏多但 1h 转弱",
            "confidence": "medium",
            "facts": [{"statement": "4h 立场 bull", "evidence": ["4h.stance"]}],
            "inferences": [{"statement": "结构偏多", "basis": ["4h.stance"], "confidence": "medium"}],
            "trading_plan": LONG_PLAN,
            "invalidation": ["4h 转 bear"],
            "missing_evidence": ["1d 数据不足"],
            "data_caveats": ["15m 休市空 bar"],
        }
        markdown = report_markdown(report, bundle, validate_report(report, bundle, validate_plan(LONG_PLAN, bundle)))
        for expected in ("交易计划（模拟盘候选）", "止损：508.2", "TP1 521.6", "盈亏比：0.45", "失效条件", "不是交易指令"):
            self.assertIn(expected, markdown)


class ResearchEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"QUANTDESK_HOME": self._tmp.name}, clear=False)
        self._env.start()
        os.environ["STUB_KEY"] = "stub"
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self._env.stop()
        self._tmp.cleanup()

    def _write_profile(self):
        from quantdesk.config.settings import quantdesk_home

        home = quantdesk_home()
        (home / "llm.toml").write_text(
            '[profiles.stub]\nprovider = "openai_compatible"\nbase_url = "http://127.0.0.1:9/v1"\n'
            'api_key_env = "STUB_KEY"\ndeep_model = "stub-1"\n\n[roles]\ntradingagents = "stub"\n',
            encoding="utf-8",
        )

    async def test_readiness_explains_what_is_missing(self):
        response = await self.client.get("/api/research/readiness")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ready"])
        self.assertIn("API Key", body["reason"] + body.get("action", ""))

    async def test_run_returns_plan_and_validation(self):
        self._write_profile()
        candles = [
            {"time": row["ts"], "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"], "volume": row["volume"]}
            for row in ramp_candles()
        ]
        model_report = {
            "headline": "4h 偏多但 1h 转弱",
            "confidence": "medium",
            "facts": [{"statement": "4h 的 ADX 为 30.0", "evidence": ["4h.adx"]}],
            "inferences": [{"statement": "共振偏多", "basis": ["resonance.score_100"], "confidence": "medium"}],
            # Built from scratch, not from LONG_PLAN: its entry_zone belongs to a
            # different price scale and would (correctly) fail the audit.
            "trading_plan": {
                "direction": "long",
                "entry": 100.0,
                "entry_zone": [99.0, 101.0],
                "stop_loss": 94.0,
                "take_profit_1": 108.0,
                "take_profit_2": 115.0,
                "risk_reward": 1.33,
                "position_size_pct": 10,
                "timeframe": "1h",
                "rationale": "4h.stance 为 bull，止损放在摆动低点之下",
                "trigger": None,
                "valid_until": "本根K线收盘前",
            },
            "invalidation": ["4h 转 bear"],
            "missing_evidence": [],
            "data_caveats": [],
        }

        class StubCompletion:
            text = json.dumps(model_report, ensure_ascii=False)
            model = "stub-1"
            usage = {"total_tokens": 900}
            latency_s = 0.8

            def as_dict(self):
                return {"text": self.text, "model": self.model, "usage": self.usage, "latency_s": self.latency_s, "kind": "chat"}

        class StubDriver:
            def complete(self, messages, **kwargs):
                joined = " ".join(message.text for message in messages)
                # The prompt must demand concrete levels and the evidence must
                # carry real structure to anchor them.
                assert "必须给出具体点位" in joined
                assert "price_structure" in joined
                assert "swing_lows" in joined
                return StubCompletion()

        async def fake_resonance(cache, venue_symbol, intervals, bars):
            return {
                "score": 0.4,
                "score_100": 70.0,
                "label": "偏多",
                "weights": {"1d": 0.4, "4h": 0.3, "1h": 0.2, "15m": 0.1},
                "unavailable": [],
                "barsByInterval": {interval: 400 for interval in intervals},
                "timeframes": [
                    {
                        "interval": interval,
                        "stance": "bull" if interval in {"4h", "1d"} else "neutral",
                        "score": 0.667 if interval in {"4h", "1d"} else 0.0,
                        "trend": 1,
                        "momentum": 0,
                        "volume": 1,
                        "close": 100.0,
                        "notes": {"adx": 30.0, "rsi": 60.0, "session_thin": interval == "15m"},
                    }
                    for interval in intervals
                ],
            }

        with (
            patch("quantdesk.api.research.compute_resonance", fake_resonance),
            patch("quantdesk.api.research._ticker", return_value={"markPrice": "100.0", "openInterestValue": "1000", "fundingRate": "0"}),
            patch("quantdesk.api.research._funding_stats", return_value=(50, True)),
            patch("quantdesk.api.research._backtest_summary", return_value={"net_return_pct": -5.38, "trades": 14, "warnings": []}),
            patch("quantdesk.api.research._profile_driver", return_value=StubDriver()),
        ):
            response = await self.client.post(
                "/api/research",
                json={"symbol": "AMD", "timeframe": "1h", "candles": candles},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["priceStructure"]["source"], "live", "source provenance must survive the request")
        self.assertGreater(body["priceStructure"]["atr"], 0)
        self.assertTrue(body["priceStructure"]["swing_lows"])
        plan = body["validation"]["plan"]
        self.assertTrue(plan["present"])
        self.assertTrue(plan["ok"], plan["problems"])
        self.assertAlmostEqual(plan["computed"]["riskReward"], 8 / 6, places=2)
        self.assertTrue(body["validation"]["verified"])
        self.assertIn("交易计划", body["markdown"])
        self.assertIsNotNone(body["reportId"])

        listing = await self.client.get("/api/research/reports")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.json()["reports"]), 1)
        detail = await self.client.get(f"/api/research/reports/{body['reportId']}")
        self.assertIn("交易计划", detail.json()["markdown"])

    async def test_unknown_symbol_and_timeframe_are_rejected(self):
        self.assertEqual((await self.client.post("/api/research", json={"symbol": "DOGEUSDT"})).status_code, 422)
        self.assertEqual((await self.client.post("/api/research", json={"symbol": "AMD", "timeframe": "5m"})).status_code, 422)

    async def test_demo_data_is_rejected_before_model_or_market_calls(self):
        self._write_profile()
        response = await self.client.post(
            "/api/research", json={"symbol": "AMD", "timeframe": "1h", "source": "demo"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("演示数据", response.text)

    async def test_uploaded_history_does_not_fetch_current_market_evidence(self):
        self._write_profile()
        candles = [
            {"time": row["ts"], "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"], "volume": row["volume"]}
            for row in ramp_candles()
        ]

        class Completion:
            def as_dict(self):
                return {"text": "{}", "model": "stub-1", "usage": {}, "latency_s": 0.01, "kind": "chat"}

        class Driver:
            def complete(self, messages, **kwargs):
                return Completion()

        with (
            patch("quantdesk.api.research.compute_resonance", side_effect=AssertionError("must not fetch live resonance")),
            patch("quantdesk.api.research._ticker", side_effect=AssertionError("must not fetch live ticker")),
            patch("quantdesk.api.research._profile_driver", return_value=Driver()),
        ):
            response = await self.client.post(
                "/api/research",
                json={"symbol": "AMD", "timeframe": "1h", "source": "upload", "candles": candles},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["priceStructure"]["source"], "upload")
        self.assertTrue(any("未混入当前" in item for item in body["missing"]))


if __name__ == "__main__":
    unittest.main()
