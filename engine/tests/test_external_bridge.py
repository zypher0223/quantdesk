"""The bridge that puts external evidence in front of research.

The behaviours worth protecting: research runs whether or not the external side
answers, a switched-off feature is not reported as a degradation, a degraded run
says so, and nothing from OpenBB can rewrite a price.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quantdesk.datahub.db import Database
from quantdesk.research.external import EvidenceBundle, EvidenceRecord, STATUS_OK
from quantdesk.research.external_bridge import (
    DEFAULT_TOPICS,
    appendix,
    collect_best_effort,
    collect_evidence,
    prompt_block,
    summary_meta,
    topic_expiry_map,
)
from quantdesk.tradingagents_bybit import _with_evidence

OPENBB_CONFIG = {
    "providers": {"news": "yfinance", "fundamentals": "sec", "company_profile": "yfinance"},
    "fallbacks": {"news": ["benzinga"]},
    "point_in_time": {"default": True, "exempt": ["macro_calendar"]},
}


def record(**overrides) -> EvidenceRecord:
    payload = dict(
        key="fundamentals.sec.nvda.income.0", label="NVDA 最近季度营收",
        value={"revenue": 46_700_000_000}, source="https://www.sec.gov/x",
        provider="sec", endpoint="equity.fundamental.income", symbol="NVDAUSDT",
        topic="fundamentals", observed_at="2026-09-15T10:00:00Z", as_of="2026-07-31",
        published_at="2026-08-20T20:00:00Z", point_in_time=True, status=STATUS_OK,
        cache_key="k1", request_key="r1",
    )
    payload.update(overrides)
    return EvidenceRecord(**payload)


class DisabledTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_a_switched_off_feature_is_not_a_degradation(self):
        bundle = collect_evidence(symbol="NVDAUSDT", trade_date="2026-09-15", home=self.home)
        self.assertFalse(bundle.external_enabled)
        self.assertFalse(bundle.degraded, "未启用的功能不应显示为证据降级")
        self.assertEqual(prompt_block(bundle), "", "未启用时不应向模型注入外部证据段")
        self.assertIn("未启用", appendix(bundle))
        self.assertFalse(summary_meta(bundle)["degraded"])

    def test_a_demo_chart_does_not_trigger_an_external_call(self):
        bundle = collect_best_effort(
            symbol="NVDAUSDT", trade_date="2026-09-15", home=self.home,
            skip_reason="演示数据不调用外部研究",
        )
        self.assertFalse(bundle.external_enabled)
        self.assertFalse(bundle.degraded)
        self.assertIn("演示数据", bundle.unavailable[0]["reason"])

    def test_a_failure_inside_the_bridge_never_reaches_the_caller(self):
        with patch("quantdesk.research.external_bridge.load_app_config", side_effect=RuntimeError("配置坏了")):
            bundle = collect_best_effort(symbol="NVDAUSDT", trade_date="2026-09-15", home=self.home)
        self.assertIn("RuntimeError", bundle.errors[0]["reason"])
        self.assertTrue(bundle.degraded, "开启状态下采集失败必须标记为降级")


class EnabledBundleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "config.toml").write_text(
            "[external]\nopenbb_enabled = true\n\n"
            "[openbb.providers]\nnews = \"yfinance\"\nfundamentals = \"sec\"\n"
            'macro_calendar = ""\n'
            "[openbb.fallbacks]\nnews = [\"benzinga\"]\n"
            "[openbb.point_in_time]\ndefault = true\n",
            encoding="utf-8",
        )

    def _bundle(self, **overrides) -> EvidenceBundle:
        bundle = EvidenceBundle(symbol="NVDAUSDT", trade_date="2026-09-15")
        bundle.evidence.append(record(**overrides))
        return bundle

    def test_the_prompt_block_forbids_using_external_numbers_as_prices(self):
        block = prompt_block(self._bundle())
        self.assertIn("价格、成交量、资金费率与强平一律以 Bybit 为准", block)
        self.assertIn("provider=sec", block)
        self.assertIn("publishedAt=2026-08-20T20:00:00Z", block)
        self.assertIn("source=https://www.sec.gov/x", block)

    def test_the_prompt_block_names_what_is_missing_and_the_degradation(self):
        bundle = self._bundle()
        bundle.unavailable.append({"topic": "news", "provider": "yfinance", "reason": "上游超时"})
        bundle.rejected.append({"topic": "earnings", "reason": "该数据发布于研判日期之后", "basis": "publishedAt"})
        block = prompt_block(bundle)
        self.assertIn("上游超时", block)
        self.assertIn("该数据发布于研判日期之后", block)
        self.assertIn("证据降级", block)
        self.assertTrue(bundle.degraded)

    def test_the_appendix_lists_sources_publish_times_and_gaps(self):
        bundle = self._bundle()
        bundle.unavailable.append({"topic": "news", "provider": "yfinance", "reason": "上游超时"})
        text = appendix(bundle)
        self.assertIn("| fundamentals |", text)
        self.assertIn("[链接](https://www.sec.gov/x)", text)
        self.assertIn("不可用", text)
        self.assertIn("上游超时", text)

    def test_the_summary_keeps_providers_publish_times_and_sources(self):
        meta = summary_meta(self._bundle())
        self.assertEqual(meta["providers"], ["sec"])
        self.assertEqual(meta["topics"], ["fundamentals"])
        self.assertEqual(meta["publishedAt"]["fundamentals.sec.nvda.income.0"], "2026-08-20T20:00:00Z")
        self.assertEqual(meta["asOf"]["fundamentals.sec.nvda.income.0"], "2026-07-31")
        self.assertEqual(meta["sources"]["fundamentals.sec.nvda.income.0"], "https://www.sec.gov/x")

    def test_an_unconfigured_topic_is_not_asked_for(self):
        # Every shipped topic has a default provider, so "unconfigured" now means
        # the operator cleared one. That still has to be reported rather than
        # attempted. A stub manager supplies the enabled adapter; the fetcher must
        # never be reached.
        calls: list[str] = []

        class _Manifest:
            id = "stub-research"
            capabilities = ("research_tool",)

        class _Record:
            enabled = True
            manifest = _Manifest()

        class _Manager:
            def discover(self):
                return ([_Record()], [])

        class _Registry:
            def collect_research(self, plugin_id, request):
                calls.append(plugin_id)
                raise AssertionError("未配置 Provider 的 topic 不应发起调用")

        bundle = collect_evidence(
            symbol="NVDAUSDT", trade_date="2026-09-15", home=self.home,
            topics=["macro_calendar"], manager=_Manager(), registry=_Registry(),
        )
        self.assertEqual(calls, [])
        reasons = {item["topic"]: item["reason"] for item in bundle.unavailable}
        self.assertIn("macro_calendar", reasons)
        self.assertIn("未配置 Provider", reasons["macro_calendar"])
        self.assertIn("news", DEFAULT_TOPICS)

    def test_the_expiry_map_covers_the_topics_and_the_configured_windows(self):
        expiry = topic_expiry_map({"cache_ttl_minutes": {"news": 30}})
        self.assertEqual(expiry["news"], 30)
        self.assertGreater(expiry["company_profile"], expiry["news"], "公司资料缓存应长于新闻")


class ProviderBoundaryTests(unittest.TestCase):
    def test_external_evidence_is_appended_to_fundamentals_and_news_only(self):
        calls: list[str] = []

        def implementation(*args, **kwargs):
            calls.append("called")
            return "原始基本面文本"

        wrapped = _with_evidence(implementation, "## 外部证据\n- revenue=46700000000")
        text = wrapped("NVDA")
        self.assertIn("原始基本面文本", text)
        self.assertIn("外部证据", text)

    def test_a_non_text_vendor_result_is_passed_through_untouched(self):
        wrapped = _with_evidence(lambda *a, **k: {"rows": [1, 2]}, "证据块")
        self.assertEqual(wrapped("NVDA"), {"rows": [1, 2]})

    def test_an_empty_evidence_block_changes_nothing(self):
        wrapped = _with_evidence(lambda *a, **k: "原始文本", "   ")
        self.assertEqual(wrapped("NVDA"), "原始文本")


class BoundaryTests(unittest.TestCase):
    """Rules from the execution report that must hold by construction."""

    def test_external_prices_never_enter_the_candle_store(self):
        # Bybit is the only source of tradable prices. External readings land in
        # external_evidence; if one ever appeared as a candle, a backtest could be
        # run on someone else's price series.
        import tempfile

        from quantdesk.datahub.db import Database
        from quantdesk.research.external import ExternalEvidenceService

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "q.db")
            service = ExternalEvidenceService(
                db, {"cache_ttl_minutes": {"news": 30}},
                {"providers": {"news": "yfinance"}, "point_in_time": {"default": True}},
            )
            service.collect(
                symbol="NVDAUSDT", trade_date="2026-09-15", topics=["news"],
                fetcher=lambda provider, topic, symbol: {
                    "evidence": [record(key="news.yfinance.nvda.0", topic="news",
                                        value={"close": 999.0, "price": 999.0}, as_of="",
                                        published_at="2026-09-14T10:00:00Z").as_dict()],
                    "observedAt": "2026-09-15T10:00:00Z",
                },
            )
            self.assertEqual(db.count_candles("bybit", "NVDAUSDT", "1h"), 0)
            self.assertEqual(db.count_candles("bybit", "NVDAUSDT", "1d"), 0)
            self.assertEqual(len(db.list_external_evidence(symbol="NVDAUSDT")), 1,
                             "价格样式的读数只能进外部证据表")

    def test_the_bridge_writes_no_market_data(self):
        # The module's whole surface is read-only with respect to the candle store:
        # it may upsert external_evidence, never candles or funding.
        import inspect

        from quantdesk.research import external_bridge

        source = inspect.getsource(external_bridge)
        for forbidden in ("upsert_candles", "upsert_funding", "upsert_mark_candles", "open_interest"):
            self.assertNotIn(forbidden, source, f"外部证据层不得写入 {forbidden}")

    def test_the_analytics_layer_has_no_trading_surface(self):
        import inspect
        import re

        from quantdesk import analytics

        source = inspect.getsource(analytics)
        # A *mutating* call is what must not exist. Reads are fine: the layer has
        # to look at the open book to price it. The trailing "(" is what tells the
        # two apart, so `open_positions()` does not look like `open_position(`.
        for forbidden in ("open_position", "close_position", "place_order", "submit_order", "set_leverage"):
            self.assertIsNone(
                re.search(rf"\b{forbidden}\s*\(", source),
                f"外部分析层不得包含 {forbidden} 调用",
            )


class StoredEvidenceTests(unittest.TestCase):
    def test_only_usable_readings_reach_the_prompt(self):
        bundle = EvidenceBundle(symbol="NVDAUSDT", trade_date="2026-09-15")
        bundle.evidence.append(record(status="unavailable", value=None))
        block = prompt_block(bundle)
        self.assertNotIn("NVDA 最近季度营收", block)

    def test_the_evidence_service_records_what_the_bridge_sends_it(self):
        from quantdesk.research.external import ExternalEvidenceService

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "q.db")
            service = ExternalEvidenceService(
                db, {"cache_ttl_minutes": {"fundamentals": 60}},
                {"providers": {"fundamentals": "sec"}, "point_in_time": {"default": True}},
            )
            bundle = service.collect(
                symbol="NVDAUSDT", trade_date="2026-09-15", topics=["fundamentals"],
                fetcher=lambda provider, topic, symbol: {
                    "evidence": [record().as_dict()], "observedAt": "2026-09-15T10:00:00Z",
                },
            )
            self.assertEqual(len(bundle.usable), 1)
            stored = db.list_external_evidence(symbol="NVDAUSDT")
            self.assertEqual(json.loads(stored[0]["payload_json"])["revenue"], 46_700_000_000)


if __name__ == "__main__":
    unittest.main()


class PortfolioRiskContextTests(unittest.TestCase):
    """The Fincept summary that a research brief carries."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "config.toml").write_text(
            "[external]\nfincept_enabled = true\n\n[paper]\n\n[app]\ndefault_cash_usd = 50000\n",
            encoding="utf-8",
        )

    def _book(self):
        from quantdesk.analytics import paper_config
        from quantdesk.datahub.db import Database
        from quantdesk.paper.engine import PaperEngine

        db = Database(self.home / "quantdesk.db")
        engine = PaperEngine(db, paper_config(self.home))
        engine.open_position(
            symbol="NVDAUSDT", side="long", notional=368.0, leverage=5.0,
            mark_price=184.0, rationale="fixture",
        )
        return db

    def test_an_empty_book_says_so_instead_of_inventing_a_summary(self):
        from quantdesk.research.external_bridge import portfolio_risk_context

        block, meta = portfolio_risk_context(self.home, symbol="NVDAUSDT")
        self.assertEqual(block, "")
        self.assertFalse(meta["ok"])
        self.assertIn("没有持仓", meta["reason"])

    def test_no_adapter_is_reported_not_guessed(self):
        from quantdesk.research.external_bridge import portfolio_risk_context

        self._book()
        block, meta = portfolio_risk_context(self.home, symbol="NVDAUSDT")
        self.assertEqual(block, "")
        self.assertIn("analytics 插件", meta["reason"])

    def test_a_summary_names_the_position_contribution_and_the_snapshot(self):
        from quantdesk.research.external_bridge import portfolio_risk_context

        self._book()

        def provider(kind, params):
            return {
                "provider": "fincept-api",
                "asOf": params["asOf"],
                "metrics": {"volatility": 0.21, "var": -0.031, "cvar": -0.047, "maxDrawdown": -0.12},
                "riskContributions": [
                    {"symbol": "NVDAUSDT", "value": 0.031, "percentage": 100.0},
                ],
                "correlation": {"symbols": ["NVDAUSDT"], "matrix": [[1.0]]},
                "optimization": None,
                "source": "Fincept QuantLib API",
                "requestId": "req-1",
                "warnings": [],
            }

        block, meta = portfolio_risk_context(self.home, symbol="NVDAUSDT", provider=provider)
        self.assertIn("Fincept 组合风险摘要", block)
        self.assertIn("NVDAUSDT 的风险贡献", block)
        self.assertIn("市场快照版本", block)
        self.assertIn("只用于提示风险", block)
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["positions"], 1)
        self.assertTrue(meta["marketVersion"])

    def test_a_symbol_outside_the_book_is_stated_rather_than_omitted(self):
        from quantdesk.research.external_bridge import portfolio_risk_context

        self._book()

        def provider(kind, params):
            return {
                "provider": "fincept-api", "asOf": params["asOf"], "metrics": {},
                "riskContributions": [{"symbol": "NVDAUSDT", "value": 1.0, "percentage": 100.0}],
                "correlation": None, "optimization": None, "source": "", "requestId": "", "warnings": [],
            }

        block, _ = portfolio_risk_context(self.home, symbol="BTCUSDT", provider=provider)
        self.assertIn("BTCUSDT 当前不在持仓中", block)

    def test_a_failing_provider_degrades_the_summary_without_failing_research(self):
        from quantdesk.research.external_bridge import portfolio_risk_context

        self._book()

        def provider(kind, params):
            raise TimeoutError("上游超时")

        block, meta = portfolio_risk_context(self.home, symbol="NVDAUSDT", provider=provider)
        self.assertEqual(block, "")
        self.assertFalse(meta["ok"])
        self.assertIn("TimeoutError", meta["reason"])

    def test_a_cached_result_is_marked_as_reused(self):
        from quantdesk.research.external_bridge import portfolio_risk_context

        self._book()

        def provider(kind, params):
            return {
                "provider": "fincept-api", "asOf": params["asOf"],
                "metrics": {"var": -0.01}, "riskContributions": [], "correlation": None,
                "optimization": None, "source": "", "requestId": "", "warnings": [],
            }

        portfolio_risk_context(self.home, symbol="NVDAUSDT", provider=provider)
        _, meta = portfolio_risk_context(self.home, symbol="NVDAUSDT", provider=provider)
        self.assertTrue(meta["cached"], "同持仓同快照不得重复付费计算")
