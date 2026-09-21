"""Point-in-time filtering and the external evidence store.

The rule these tests protect: a historical judgement must not be able to see a
reading that was published after the date it is judging. That is the difference
between a backtest and a fantasy, and it is not something a provider can be
trusted to enforce for us.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quantdesk.datahub.db import Database
from quantdesk.research.external import (
    RELEASE_REVISION,
    RELEASE_UNKNOWN,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    ExternalEvidenceService,
    cache_key_for,
    check_point_in_time,
    content_hash,
    macro_release_kind,
    parameters_hash,
    point_in_time_required,
    provider_plan,
    ttl_minutes,
)

EXTERNAL_CONFIG = {
    "cache_ttl_minutes": {"news": 30, "fundamentals": 1440, "macro_calendar": 180},
    "retention_days": {"evidence": 400, "analytics": 400},
}
OPENBB_CONFIG = {
    "providers": {"news": "yfinance", "fundamentals": "sec", "macro": "fred"},
    "fallbacks": {"news": ["benzinga", "polygon"], "fundamentals": ["yfinance"]},
    "point_in_time": {"default": True, "exempt": ["macro_calendar"]},
}


class PointInTimeTests(unittest.TestCase):
    def test_a_filing_published_before_the_judgement_date_is_accepted(self):
        decision = check_point_in_time(
            topic="fundamentals", trade_date="2026-09-15", as_of="2026-07-31",
            published_at="2026-08-20T20:00:00Z", observed_at="2026-09-15T10:00:00Z",
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.basis, "publishedAt")

    def test_a_filing_published_after_the_judgement_date_is_rejected(self):
        decision = check_point_in_time(
            topic="fundamentals", trade_date="2026-09-15", as_of="2026-07-31",
            published_at="2026-09-20T20:00:00Z", observed_at="2026-09-15T10:00:00Z",
        )
        self.assertFalse(decision.accepted)
        self.assertIn("发布于研判日期之后", decision.reason)

    def test_a_period_end_date_cannot_stand_in_for_a_publication_date(self):
        # Accepting this would make a quarter's results visible on the last day of
        # that quarter, which is exactly the leak the report calls out.
        decision = check_point_in_time(
            topic="earnings", trade_date="2026-09-15", as_of="2026-07-31",
            published_at="2026-07-31", observed_at="2026-09-15T10:00:00Z",
        )
        self.assertFalse(decision.accepted)
        self.assertIn("期末", decision.reason)

    def test_a_reading_with_no_publication_time_is_not_history(self):
        decision = check_point_in_time(
            topic="fundamentals", trade_date="2026-09-15", as_of="2026-07-31",
            published_at="", observed_at="2026-09-15T10:00:00Z",
        )
        self.assertFalse(decision.accepted)
        self.assertIn("缺少可解析的发布时间", decision.reason)

    def test_an_undated_reading_is_allowed_when_point_in_time_is_not_required(self):
        decision = check_point_in_time(
            topic="fundamentals", trade_date="2026-09-15", as_of="", published_at="",
            observed_at="2026-09-15T10:00:00Z", required=False,
        )
        self.assertTrue(decision.accepted)

    def test_a_calendar_entry_is_exempt_because_it_describes_the_future(self):
        decision = check_point_in_time(
            topic="macro_calendar", trade_date="2026-09-15", as_of="", published_at="",
            observed_at="2026-09-15T10:00:00Z",
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.basis, "calendar")
        self.assertFalse(point_in_time_required("macro_calendar", OPENBB_CONFIG))
        self.assertTrue(point_in_time_required("news", OPENBB_CONFIG))

    def test_the_same_day_publication_is_visible_that_day(self):
        decision = check_point_in_time(
            topic="news", trade_date="2026-09-15", as_of="",
            published_at="2026-09-15T23:30:00Z", observed_at="2026-09-16T01:00:00Z",
        )
        self.assertTrue(decision.accepted, "同一天发布的消息在当天可见")

    def test_a_revision_is_recognised_and_treated_as_later_information(self):
        self.assertEqual(macro_release_kind({"release": "revision"}), RELEASE_REVISION)
        self.assertEqual(macro_release_kind({"vintage": "2026-09-30"}), RELEASE_REVISION)
        self.assertEqual(macro_release_kind({}), RELEASE_UNKNOWN)
        self.assertTrue(macro_release_kind({"isRevision": False}).startswith("first"))
        # A revision published after the judgement date is rejected like anything
        # else that did not exist yet.
        decision = check_point_in_time(
            topic="macro", trade_date="2026-09-15", as_of="2026-08-01",
            published_at="2026-09-30T12:00:00Z", observed_at="2026-10-01T00:00:00Z",
            payload={"release": "revision"},
        )
        self.assertFalse(decision.accepted)

    def test_a_contradictory_timestamp_pair_is_rejected(self):
        decision = check_point_in_time(
            topic="news", trade_date="2026-09-15", as_of="",
            published_at="2026-09-14T10:00:00Z", observed_at="2026-09-13T10:00:00Z",
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.basis, "observedAt<publishedAt")

    def test_an_unparseable_judgement_date_is_refused_rather_than_assumed(self):
        decision = check_point_in_time(
            topic="news", trade_date="not-a-date", as_of="",
            published_at="2026-09-14T10:00:00Z", observed_at="2026-09-15T10:00:00Z",
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.basis, "tradeDate")


class CacheKeyTests(unittest.TestCase):
    def test_the_key_covers_provider_endpoint_symbol_as_of_and_parameters(self):
        base = dict(provider="sec", endpoint="equity.fundamental.income", symbol="NVDAUSDT", as_of="2026-09-15")
        first = cache_key_for(**base)
        self.assertEqual(first, cache_key_for(**base))
        self.assertNotEqual(first, cache_key_for(**{**base, "provider": "yfinance"}))
        self.assertNotEqual(first, cache_key_for(**{**base, "symbol": "AAPLUSDT"}))
        self.assertNotEqual(first, cache_key_for(**{**base, "as_of": "2026-09-16"}))
        self.assertNotEqual(first, cache_key_for(**base, parameters={"limit": 4}))
        self.assertEqual(parameters_hash({"limit": 4}), parameters_hash({"limit": 4}))

    def test_the_hash_ignores_dictionary_order_but_not_content(self):
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))
        self.assertNotEqual(content_hash({"a": 1}), content_hash({"a": 2}))

    def test_ttl_comes_from_config_with_a_sane_floor(self):
        self.assertEqual(ttl_minutes("news", EXTERNAL_CONFIG), 30)
        self.assertEqual(ttl_minutes("company_profile", EXTERNAL_CONFIG), 7 * 24 * 60)
        self.assertEqual(ttl_minutes("news", {"cache_ttl_minutes": {"news": 5}}), 5)
        self.assertGreater(ttl_minutes("something_new", {}), 0, "未知类型也不能表示永久缓存")


class ProviderPlanTests(unittest.TestCase):
    def test_the_plan_is_primary_then_declared_fallbacks(self):
        self.assertEqual(provider_plan("news", OPENBB_CONFIG), ["yfinance", "benzinga", "polygon"])
        self.assertEqual(provider_plan("fundamentals", OPENBB_CONFIG), ["sec", "yfinance"])

    def test_an_unconfigured_interface_has_no_plan_instead_of_a_default(self):
        # Silently choosing a provider nobody named is how a report ends up citing
        # a source the operator never approved.
        self.assertEqual(provider_plan("short_interest", {"providers": {"news": "yfinance"}}), [])


class _Clock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        import datetime as dt

        self.moment = self.moment + dt.timedelta(**kwargs)


class EvidenceServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        import datetime as dt

        self.clock = _Clock(dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.timezone.utc))
        self.service = ExternalEvidenceService(
            self.db, EXTERNAL_CONFIG, OPENBB_CONFIG, now=self.clock
        )

    def _payload(self, value=42.0, published="2026-08-20T20:00:00Z", as_of="2026-07-31", topic="fundamentals"):
        return {
            "source": "https://www.sec.gov/Archives/edgar/data/1045810/",
            "evidence": [
                {
                    "key": f"{topic}.sec.nvda.revenue",
                    "label": "NVDA最近季度营收",
                    "value": {"revenue": value},
                    "publishedAt": published,
                    "asOf": as_of,
                    "topic": topic,
                }
            ],
        }

    def test_collect_stores_gated_readings_with_full_provenance(self):
        calls: list[tuple[str, str, str]] = []

        def fetcher(provider, topic, symbol):
            calls.append((provider, topic, symbol))
            return self._payload()

        bundle = self.service.collect(
            symbol="NVDAUSDT", trade_date="2026-09-15", topics=["fundamentals"], fetcher=fetcher
        )
        self.assertEqual([item.topic for item in bundle.usable], ["fundamentals"])
        self.assertFalse(bundle.degraded)
        record = bundle.usable[0]
        self.assertEqual(record.provider, "sec", "必须记录真实 Provider")
        self.assertEqual(record.published_at, "2026-08-20T20:00:00Z")
        self.assertTrue(record.point_in_time)
        self.assertTrue(record.content_hash)
        stored = self.db.list_external_evidence(symbol="NVDAUSDT")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["provider"], "sec")
        self.assertEqual(stored[0]["status"], STATUS_OK)

    def test_a_second_collect_is_served_from_cache_without_calling_again(self):
        calls: list[str] = []

        def fetcher(provider, topic, symbol):
            calls.append(topic)
            return self._payload()

        self.service.collect(symbol="NVDAUSDT", trade_date="2026-09-15", topics=["fundamentals"], fetcher=fetcher)
        bundle = self.service.collect(symbol="NVDAUSDT", trade_date="2026-09-15", topics=["fundamentals"], fetcher=fetcher)
        self.assertEqual(len(calls), 1)
        self.assertEqual(bundle.cache_hits, 1)
        self.assertTrue(bundle.usable[0].cached)

    def test_an_expired_cache_entry_is_refetched(self):
        calls: list[str] = []

        def fetcher(provider, topic, symbol):
            calls.append(topic)
            return self._payload(topic="news", as_of="", published="2026-09-15T09:00:00Z")

        self.service.collect(symbol="BTCUSDT", trade_date="2026-09-15", topics=["news"], fetcher=fetcher)
        self.clock.advance(minutes=31)
        self.service.collect(symbol="BTCUSDT", trade_date="2026-09-15", topics=["news"], fetcher=fetcher)
        self.assertEqual(len(calls), 2, "新闻缓存 30 分钟后必须重新取数")

    def test_future_dated_evidence_is_rejected_and_named(self):
        def fetcher(provider, topic, symbol):
            return self._payload(published="2026-09-20T20:00:00Z")

        bundle = self.service.collect(
            symbol="NVDAUSDT", trade_date="2026-09-15", topics=["fundamentals"], fetcher=fetcher
        )
        self.assertEqual(bundle.usable, [])
        self.assertTrue(bundle.degraded)
        self.assertIn("发布于研判日期之后", bundle.rejected[0]["reason"])

    def test_a_provider_with_nothing_is_recorded_not_filled(self):
        def fetcher(provider, topic, symbol):
            return {"unavailable": "该标的没有 SEC 记录"}

        bundle = self.service.collect(
            symbol="SPCXUSDT", trade_date="2026-09-15", topics=["fundamentals"], fetcher=fetcher
        )
        self.assertEqual(bundle.usable, [])
        self.assertTrue(bundle.degraded)
        self.assertIn("没有 SEC 记录", bundle.unavailable[0]["reason"])
        stored = self.db.list_external_evidence(symbol="SPCXUSDT")
        self.assertEqual(stored[0]["status"], STATUS_UNAVAILABLE)
        self.assertIsNone(stored[0]["payload_json"] or None)

    def test_a_failing_provider_falls_back_to_the_configured_alternative(self):
        seen: list[str] = []

        def fetcher(provider, topic, symbol):
            seen.append(provider)
            if provider == "sec":
                raise RuntimeError("provider 拒绝请求")
            return self._payload()

        bundle = self.service.collect(
            symbol="NVDAUSDT", trade_date="2026-09-15", topics=["fundamentals"], fetcher=fetcher
        )
        self.assertEqual(seen, ["sec", "yfinance"])
        self.assertEqual(bundle.usable[0].provider, "yfinance")
        self.assertEqual(bundle.errors[0]["provider"], "sec")
        self.assertIn("provider 拒绝请求", bundle.errors[0]["reason"])

    def test_an_unconfigured_interface_is_reported_without_a_call(self):
        calls: list[str] = []

        def fetcher(provider, topic, symbol):  # pragma: no cover - must not run
            calls.append(topic)
            return self._payload()

        bundle = self.service.collect(
            symbol="NVDAUSDT", trade_date="2026-09-15", topics=["short_interest"], fetcher=fetcher
        )
        self.assertEqual(calls, [])
        self.assertIn("未配置 Provider", bundle.unavailable[0]["reason"])

    def test_every_provider_failing_leaves_the_run_degraded_but_alive(self):
        def fetcher(provider, topic, symbol):
            raise TimeoutError("上游超时")

        bundle = self.service.collect(
            symbol="NVDAUSDT", trade_date="2026-09-15", topics=["fundamentals"], fetcher=fetcher
        )
        self.assertTrue(bundle.degraded)
        # Both configured providers were tried and both failed: one error each,
        # plus the summary line saying the topic could not be covered.
        self.assertEqual(len(bundle.errors), 2, "每个尝试过的 Provider 都记录失败原因")
        self.assertIn("全部 Provider 均失败", bundle.unavailable[-1]["reason"])
        self.assertFalse(bundle.usable)

    def test_a_cached_negative_result_is_reused_within_its_window(self):
        calls: list[str] = []

        def fetcher(provider, topic, symbol):
            calls.append(topic)
            return {"unavailable": "无记录"}

        for _ in range(2):
            self.service.collect(
                symbol="SPCXUSDT", trade_date="2026-09-15", topics=["fundamentals"], fetcher=fetcher
            )
        self.assertEqual(len(calls), 1, "不可用结果也要缓存，避免反复打上游")
        stored = self.db.list_external_evidence(symbol="SPCXUSDT")
        self.assertEqual(stored[0]["status"], STATUS_UNAVAILABLE)

    def test_a_stored_error_status_is_kept_apart_from_success(self):
        def fetcher(provider, topic, symbol):
            raise RuntimeError("boom")

        self.service.collect(symbol="NVDAUSDT", trade_date="2026-09-15", topics=["news"], fetcher=fetcher)
        stored = self.db.list_external_evidence(symbol="NVDAUSDT")
        self.assertEqual(stored[0]["status"], STATUS_ERROR)
        self.assertIn("boom", stored[0]["warning"])

    def test_pruning_keeps_the_retention_window(self):
        self.service.collect(
            symbol="NVDAUSDT", trade_date="2026-09-15", topics=["fundamentals"],
            fetcher=lambda provider, topic, symbol: self._payload(),
        )
        self.assertEqual(self.service.prune()["evidence"], 0)
        self.clock.advance(days=401)
        self.assertEqual(self.service.prune()["evidence"], 1)

    def test_the_summary_names_what_is_missing_and_why(self):
        def fetcher(provider, topic, symbol):
            return {"unavailable": "没有数据"}

        bundle = self.service.collect(
            symbol="SPCXUSDT", trade_date="2026-09-15", topics=["fundamentals", "news"], fetcher=fetcher
        )
        summary = bundle.summary()
        self.assertTrue(summary["degraded"])
        self.assertEqual(summary["usable"], 0)
        self.assertEqual(len(summary["unavailable"]), 2)
        self.assertEqual(summary["symbol"], "SPCXUSDT")


if __name__ == "__main__":
    unittest.main()
