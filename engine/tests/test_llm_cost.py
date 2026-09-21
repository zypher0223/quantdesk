"""Cost governance: published rates, budget limits, reuse and the ledger.

The rates asserted here are the ones the provider publishes, including the peak
window rule and the cache-hit/cache-miss split, because a budget feature that
prices the wrong thing is worse than none. Nothing in this file calls a model.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quantdesk.llm.governance import (
    PROMPT_VERSION,
    AgentCostGovernor,
    LedgerCallback,
    RunAttempt,
    RunBudget,
    config_fingerprint,
    governed_run,
    reuse_key,
)
from quantdesk.llm.pricing import (
    TokenUsage,
    budget_state,
    is_peak,
    price_usage,
    rates_for,
    usage_from_payload,
)

PEAK = dt.datetime(2026, 9, 15, 2, 0, tzinfo=dt.timezone.utc)      # Tuesday, 02:00 UTC
PEAK_LATE = dt.datetime(2026, 9, 15, 8, 30, tzinfo=dt.timezone.utc)  # Tuesday, 08:30 UTC
OFF_PEAK = dt.datetime(2026, 9, 15, 13, 0, tzinfo=dt.timezone.utc)   # Tuesday, 13:00 UTC
WEEKEND = dt.datetime(2026, 9, 13, 2, 0, tzinfo=dt.timezone.utc)     # Sunday


class _Profile:
    name = "deepseek"
    provider = "deepseek"
    deep_model = "deepseek-v4-pro"
    quick_model = "deepseek-flash"
    base_url = "https://api.deepseek.com/v1"
    max_tokens = 20000
    temperature = None


class PricingTests(unittest.TestCase):
    def test_published_peak_rates(self):
        pro = rates_for("deepseek", "deepseek-v4-pro", at=PEAK)
        self.assertIsNotNone(pro)
        self.assertEqual(pro.cache_hit, 0.044)
        self.assertEqual(pro.cache_miss, 1.32)
        self.assertEqual(pro.output, 3.96)
        flash = rates_for("deepseek", "deepseek-flash", at=PEAK)
        self.assertEqual(flash.cache_hit, 0.006)
        self.assertEqual(flash.cache_miss, 0.30)
        self.assertEqual(flash.output, 1.20)

    def test_off_peak_is_half_and_the_window_is_the_published_one(self):
        for moment in (PEAK, PEAK_LATE):
            self.assertTrue(is_peak(moment), moment)
        for moment in (OFF_PEAK, WEEKEND, dt.datetime(2026, 9, 15, 0, 30, tzinfo=dt.timezone.utc),
                       dt.datetime(2026, 9, 15, 4, 30, tzinfo=dt.timezone.utc)):
            self.assertFalse(is_peak(moment), moment)
        peak_rates = rates_for("deepseek", "deepseek-v4-pro", at=PEAK)
        off_rates = rates_for("deepseek", "deepseek-v4-pro", at=OFF_PEAK)
        self.assertEqual(off_rates.cache_hit, peak_rates.cache_hit / 2)
        self.assertEqual(off_rates.cache_miss, peak_rates.cache_miss / 2)
        self.assertEqual(off_rates.output, peak_rates.output / 2)

    def test_an_unpublished_model_is_unpriced_not_free(self):
        self.assertIsNone(rates_for("deepseek", "some-new-model", at=PEAK))
        breakdown = price_usage(
            {"some-new-model": TokenUsage(cache_miss=10_000, output=1_000)},
            provider="deepseek",
            at=PEAK,
        )
        self.assertIsNone(breakdown.usd)
        self.assertEqual(breakdown.unpriced, ["some-new-model"])

    def test_operator_rates_override_the_published_ones(self):
        override = {"deepseek:deepseek-v4-pro": {"cache_hit": 1.0, "cache_miss": 2.0, "output": 4.0}}
        rates = rates_for("deepseek", "deepseek-v4-pro", at=OFF_PEAK, overrides=override)
        self.assertEqual(rates.cache_miss, 2.0, "an override is not halved by the peak rule")

    def test_cost_splits_by_model_and_by_token_class(self):
        usage = {
            "deepseek-v4-pro": TokenUsage(cache_hit=1_000_000, cache_miss=0, output=0),
            "deepseek-flash": TokenUsage(cache_hit=0, cache_miss=1_000_000, output=0),
        }
        # One million cached pro tokens at peak: $0.044. One million fresh flash
        # tokens at peak: $0.30.
        breakdown = price_usage(usage, provider="deepseek", at=PEAK)
        self.assertAlmostEqual(breakdown.usd or 0, 0.044 + 0.30, places=6)
        self.assertEqual([item["model"] for item in breakdown.models], ["deepseek-flash", "deepseek-v4-pro"])
        for item in breakdown.models:
            self.assertIsNotNone(item["rates"])

    def test_a_reasoning_run_costs_more_off_peak_than_peak_by_exactly_half(self):
        usage = {"deepseek-v4-pro": TokenUsage(cache_hit=120_000, cache_miss=30_000, output=12_000)}
        peak = price_usage(usage, provider="deepseek", at=PEAK).usd or 0
        off = price_usage(usage, provider="deepseek", at=OFF_PEAK).usd or 0
        self.assertAlmostEqual(off, peak / 2, places=6)

    def test_usage_is_read_from_every_payload_shape(self):
        cases = [
            ({"prompt_tokens": 1000, "completion_tokens": 200, "prompt_cache_hit_tokens": 600,
              "prompt_cache_miss_tokens": 400}, (600, 400, 200)),
            ({"prompt_tokens": 1000, "completion_tokens": 200,
              "prompt_tokens_details": {"cached_tokens": 700}}, (700, 300, 200)),
            ({"input_tokens": 1000, "output_tokens": 200,
              "input_token_details": {"cache_read": 800}}, (800, 200, 200)),
            ({"prompt_tokens": 1000, "completion_tokens": 200}, (0, 1000, 200)),
        ]
        for payload, expected in cases:
            usage = usage_from_payload(payload)
            self.assertEqual((usage.cache_hit, usage.cache_miss, usage.output), expected, payload)
            self.assertEqual(usage.input_total, 1000)

    def test_a_missing_usage_block_is_not_a_crash(self):
        self.assertEqual(usage_from_payload({}).total, 0)
        self.assertEqual(usage_from_payload(None).total, 0)

    def test_a_run_that_never_reached_a_model_costs_zero(self):
        # Nothing was spent, so the cost is known to be zero. Calling it unknown
        # would let an unrelated failure - a broken import, a refused key - read
        # as an unpriced model and refuse the next run over money never spent.
        breakdown = price_usage({}, provider="deepseek", at=PEAK)
        self.assertEqual(breakdown.usd, 0.0)
        self.assertEqual(breakdown.unpriced, [])
        self.assertEqual(breakdown.models, [])


class BudgetTests(unittest.TestCase):
    def test_limits_are_enforced_with_stated_reasons(self):
        self.assertTrue(budget_state(spent_today_usd=1.0, spent_run_usd=0.1, daily_limit_usd=5.0,
                                     per_run_limit_usd=1.0)["allowed"])
        daily = budget_state(spent_today_usd=5.0, spent_run_usd=0.1, daily_limit_usd=5.0, per_run_limit_usd=1.0)
        self.assertFalse(daily["allowed"])
        self.assertIn("当日开销", daily["problems"][0])
        single = budget_state(spent_today_usd=1.0, spent_run_usd=2.0, daily_limit_usd=5.0, per_run_limit_usd=1.0)
        self.assertFalse(single["allowed"])
        self.assertIn("单次上限", single["problems"][0])

    def test_an_unenforceable_limit_is_refused(self):
        state = budget_state(spent_today_usd=None, spent_run_usd=None, daily_limit_usd=5.0, per_run_limit_usd=1.0)
        self.assertFalse(state["allowed"])
        self.assertEqual(len(state["problems"]), 2)
        no_limit = budget_state(spent_today_usd=None, spent_run_usd=None, daily_limit_usd=None, per_run_limit_usd=None)
        self.assertTrue(no_limit["allowed"])

    def test_budget_from_config_ignores_nonsense(self):
        budget = RunBudget.from_config({
            "research": {"agent_run_budget_usd": "0.5", "agent_daily_budget_usd": "abc",
                         "reuse_window_hours": 6, "max_retries": 2, "allow_unpriced_agents": True}
        })
        self.assertEqual(budget.per_run_usd, 0.5)
        self.assertIsNone(budget.daily_usd)
        self.assertEqual(budget.reuse_window_hours, 6)
        self.assertEqual(budget.max_retries, 2)
        self.assertTrue(budget.allow_unpriced)
        default = RunBudget.from_config(None)
        self.assertIsNone(default.per_run_usd)
        self.assertEqual(default.reuse_window_hours, 24)


class LedgerCallbackTests(unittest.TestCase):
    def _response(self, payload=None, metadata=None):
        class Generation:
            def __init__(self, message):
                self.message = message

        class Message:
            def __init__(self):
                self.usage_metadata = metadata
                self.response_metadata = {}

        class Response:
            def __init__(self):
                self.llm_output = payload or {}
                self.generations = [[Generation(Message())]] if metadata else []

        return Response()

    def test_usage_is_aggregated_per_model(self):
        callback = LedgerCallback()
        callback.on_llm_end(self._response({"token_usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                                           "prompt_cache_hit_tokens": 60},
                                            "model_name": "deepseek-v4-pro"}))
        callback.on_llm_end(self._response({"token_usage": {"prompt_tokens": 50, "completion_tokens": 5,
                                                           "prompt_cache_hit_tokens": 20},
                                            "model_name": "deepseek-v4-pro"}))
        callback.on_llm_end(self._response(metadata={"input_tokens": 30, "output_tokens": 3}),
                            invocation_params={"model": "deepseek-flash"})
        snapshot = callback.snapshot()
        self.assertEqual(snapshot["deepseek-v4-pro"]["cacheHit"], 80)
        self.assertEqual(snapshot["deepseek-v4-pro"]["output"], 15)
        self.assertEqual(snapshot["deepseek-flash"]["cacheMiss"], 30)
        self.assertEqual(callback.totals()["calls"], 3)

    def test_the_engine_and_the_worker_read_the_same_numbers(self):
        # The subprocess collector and the engine parser must agree, or a run's
        # recorded cost would not match the tokens that were actually billed.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ta_worker", Path(__file__).resolve().parents[1] / "src" / "quantdesk" / "tradingagents_worker.py"
        )
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        payload = {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_cache_hit_tokens": 600,
                   "prompt_cache_miss_tokens": 400}
        collector = worker.UsageCollector()
        collector.on_llm_end(self._response({"token_usage": payload, "model_name": "m"}))
        engine_usage = usage_from_payload(payload)
        totals = collector.totals()
        self.assertEqual(totals["cacheHit"], engine_usage.cache_hit)
        self.assertEqual(totals["cacheMiss"], engine_usage.cache_miss)
        self.assertEqual(totals["output"], engine_usage.output)


class FingerprintTests(unittest.TestCase):
    def test_the_same_configuration_fingerprints_the_same(self):
        first = config_fingerprint(_Profile(), ["market", "news"], "commit1")
        second = config_fingerprint(_Profile(), ["news", "market"], "commit1")
        self.assertEqual(first, second, "analyst order is not part of the question")

    def test_a_changed_model_changes_the_fingerprint(self):
        base = config_fingerprint(_Profile(), ["market"], "commit1")
        other = _Profile()
        other.deep_model = "deepseek-v4-pro-0813"
        self.assertNotEqual(base, config_fingerprint(other, ["market"], "commit1"))
        self.assertNotEqual(base, config_fingerprint(_Profile(), ["market", "news"], "commit1"))
        self.assertNotEqual(base, config_fingerprint(_Profile(), ["market"], "commit2"))

    def test_the_reuse_key_covers_the_data_version(self):
        fingerprint = config_fingerprint(_Profile(), ["market"], "commit1")
        first = reuse_key(venue_symbol="BTCUSDT", trade_date="2026-09-15",
                          config_fingerprint_value=fingerprint, data_version="v1")
        repaired = reuse_key(venue_symbol="BTCUSDT", trade_date="2026-09-15",
                             config_fingerprint_value=fingerprint, data_version="v2")
        other_day = reuse_key(venue_symbol="BTCUSDT", trade_date="2026-09-16",
                              config_fingerprint_value=fingerprint, data_version="v1")
        self.assertNotEqual(first, repaired, "a repaired dataset must not reuse the old report")
        self.assertNotEqual(first, other_day)
        self.assertEqual(first, reuse_key(venue_symbol="BTCUSDT", trade_date="2026-09-15",
                                          config_fingerprint_value=fingerprint, data_version="v1"))

    def test_the_prompt_version_is_recorded(self):
        self.assertTrue(PROMPT_VERSION.startswith("ta-prompt/"))


class GovernorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.budget = RunBudget(per_run_usd=1.0, daily_usd=5.0, reuse_window_hours=24, max_retries=1)
        self.governor = AgentCostGovernor(self.home, self.budget)
        self.fingerprint = config_fingerprint(_Profile(), ["market", "news"], "commit1")
        self.key = reuse_key(venue_symbol="BTCUSDT", trade_date="2026-09-15",
                             config_fingerprint_value=self.fingerprint, data_version="v1")

    def _attempt(self, *, usd_tokens: int = 10_000, ok: bool = True) -> RunAttempt:
        usage = {"deepseek-v4-pro": TokenUsage(cache_hit=usd_tokens // 2, cache_miss=usd_tokens // 2,
                                              output=1_000, calls=4)}
        attempt = RunAttempt(ok=ok, usage=usage, duration_s=12.0)
        attempt.cost = self.governor.price(usage, "deepseek", at=OFF_PEAK)
        return attempt

    def _record(self, attempt: RunAttempt, **overrides):
        payload = dict(
            run_id="run-1",
            venue_symbol="BTCUSDT",
            trade_date="2026-09-15",
            profile=_Profile(),
            analysts=["market", "news"],
            missing_analysts=[],
            fingerprint=self.fingerprint,
            data_reference={"version": "v1", "asOf": "2026-09-15"},
            attempt=attempt,
            ok=attempt.ok,
            rating="Hold" if attempt.ok else None,
            staleness={"stale": False},
            reuse_key_value=self.key,
        )
        payload.update(overrides)
        return self.governor.record(**payload)

    def test_a_successful_run_is_ledgered_with_its_cost(self):
        entry = self._record(self._attempt())
        self.assertTrue(entry["ok"])
        self.assertIsNotNone(entry["cost_usd"])
        self.assertEqual(entry["data_version"], "v1")
        self.assertEqual(entry["prompt_version"], PROMPT_VERSION)
        ledger = self.governor.ledger()
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["usage"]["deepseek-v4-pro"]["calls"], 4)

    def test_a_failed_run_is_ledgered_too(self):
        attempt = self._attempt(ok=False)
        attempt.failure = {"type": "TradingAgentsError", "message": "rate limited", "retryable": True}
        entry = self._record(attempt, rating=None)
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["failure"]["message"], "rate limited")
        totals = self.governor.spent_today(now=dt.datetime.now(dt.timezone.utc))
        self.assertEqual(totals["failedRuns"], 1)

    def test_reuse_finds_the_earlier_run_and_respects_the_window(self):
        self._record(self._attempt())
        found = self.governor.find_reuse(self.key)
        self.assertIsNotNone(found)
        self.assertEqual(found["runId"], "run-1")
        self.assertEqual(found["dataVersion"], "v1")
        # Outside the window the result is not reused.
        old = self.governor.find_reuse(
            self.key, now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=48)
        )
        self.assertIsNone(old)
        # A repaired dataset is a different key.
        repaired = reuse_key(venue_symbol="BTCUSDT", trade_date="2026-09-15",
                             config_fingerprint_value=self.fingerprint, data_version="v2")
        self.assertIsNone(self.governor.find_reuse(repaired))

    def test_a_reused_answer_reports_the_coverage_it_reused(self):
        # The reused receipt describes the same run as the original one: an empty
        # panel here would read as "no analyst reported" instead of "not re-run".
        self._record(self._attempt(), run_id="run-full")
        self.assertEqual(self.governor.find_reuse(self.key)["missingAnalysts"], [])
        self._record(self._attempt(), run_id="run-partial",
                     missing_analysts=["social"], rating="Hold")
        newest = self.governor.find_reuse(self.key)
        self.assertEqual(newest["runId"], "run-partial", "the newest answer is the one reused")
        self.assertEqual(newest["missingAnalysts"], ["social"])

    def test_a_failed_run_is_never_reused(self):
        attempt = self._attempt(ok=False)
        attempt.failure = {"type": "TradingAgentsError", "message": "boom"}
        self._record(attempt, rating=None)
        self.assertIsNone(self.governor.find_reuse(self.key))

    def test_todays_spend_sums_priced_runs(self):
        self._record(self._attempt())
        self._record(self._attempt(), run_id="run-2")
        totals = self.governor.spent_today(now=dt.datetime.now(dt.timezone.utc))
        self.assertEqual(totals["runs"], 2)
        self.assertGreater(totals["usd"], 0)

    def test_an_unpriced_day_cannot_be_budgeted(self):
        usage = {"mystery-model": TokenUsage(cache_miss=1_000, output=100)}
        attempt = RunAttempt(ok=True, usage=usage, cost=self.governor.price(usage, "deepseek", at=OFF_PEAK))
        self._record(attempt)
        state = self.governor.check_budget(now=dt.datetime.now(dt.timezone.utc))
        self.assertFalse(state["allowed"])
        self.assertIn("模型未定价", state["problems"][0])

    def test_the_single_run_limit_is_checked_after_pricing(self):
        cheap = self._attempt(usd_tokens=10_000)
        self.assertTrue(self.governor.check_attempt(cheap)["allowed"])
        huge_usage = {"deepseek-v4-pro": TokenUsage(cache_miss=5_000_000, output=1_000_000)}
        huge = RunAttempt(ok=True, usage=huge_usage, cost=self.governor.price(huge_usage, "deepseek", at=PEAK))
        state = self.governor.check_attempt(huge)
        self.assertFalse(state["allowed"])
        self.assertIn("单次上限", state["problems"][0])

    def test_a_run_that_failed_before_calling_a_model_is_not_priced_as_unknown(self):
        # The shape of the real failure: the worker died on import, so no model
        # was ever reached. The ledger must say "cost $0, no token data", the
        # single-run check must pass, and the day must stay budgetable.
        attempt = RunAttempt(ok=False, usage={}, duration_s=1.7)
        attempt.cost = self.governor.price({}, "deepseek", at=OFF_PEAK)
        attempt.failure = {"type": "TradingAgentsError", "message": "No module named 'tomli_w'"}
        entry = self._record(attempt, rating=None)
        self.assertEqual(entry["cost_usd"], 0.0)
        self.assertFalse(entry["usage_known"])
        self.assertTrue(self.governor.check_attempt(attempt)["allowed"])
        state = self.governor.check_budget(now=dt.datetime.now(dt.timezone.utc))
        self.assertTrue(state["allowed"], state["problems"])
        self.assertEqual(state["today"]["unpricedRuns"], 0)

    def test_the_token_ceiling_is_a_second_guard(self):
        governor = AgentCostGovernor(self.home, RunBudget(max_tokens_per_run=1_000))
        attempt = self._attempt(usd_tokens=10_000)
        state = governor.check_attempt(attempt)
        self.assertFalse(state["allowed"])
        self.assertIn("tokens 超过上限", state["problems"][0])

    def test_missing_analysts_are_recorded_not_hidden(self):
        entry = self._record(self._attempt(), missing_analysts=["social"])
        ledger = self.governor.ledger()
        self.assertEqual(ledger[0]["missing_analysts"], ["social"])

    def test_the_summary_names_the_models_and_their_spend(self):
        self._record(self._attempt())
        summary = self.governor.summary()
        self.assertEqual(summary["totals"]["runs"], 1)
        self.assertIn("deepseek-v4-pro", summary["byModel"])
        self.assertGreater(summary["byModel"]["deepseek-v4-pro"]["tokens"], 0)
        self.assertEqual(len(summary["recent"]), 1)
        self.assertEqual(summary["budget"]["dailyUsd"], 5.0)


class _Spec:
    venue_symbol = "NVDAUSDT"
    display_symbol = "NVDA"


def _reports(*present: str) -> dict[str, str]:
    every = {
        "market": "market_report",
        "social": "sentiment_report",
        "news": "news_report",
        "fundamentals": "fundamentals_report",
    }
    return {report: f"{report} body" for analyst, report in every.items() if analyst in present}


class AnalystDegradationTests(unittest.TestCase):
    """A panel that comes back incomplete is a bounded, recorded degradation.

    The graph can return with one analyst's report missing instead of raising.
    The rules here are the ones a reader relies on: the missing dimension is
    named, one repeat is allowed to recover it, and a conclusion reached without
    most of the panel is archived rather than published as a rating.
    """

    ANALYSTS = ["market", "social", "news", "fundamentals"]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.budget = RunBudget(per_run_usd=1.0, daily_usd=5.0, reuse_window_hours=24,
                                max_retries=1, max_analyst_retries=1, max_missing_analysts=1)
        self.governor = AgentCostGovernor(self.home, self.budget)
        reference = {"symbol": "NVDAUSDT", "available": True, "version": "v1",
                     "asOf": "2026-09-15", "stale": False, "complete": True}
        patcher = patch("quantdesk.tradingagents_runner.data_reference", return_value=reference)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, passes: list[dict]):
        """Drive governed_run with a scripted sequence of graph results."""
        calls: list[list[str]] = []

        def runner(*, analysts, timeout_seconds, callbacks):
            calls.append(list(analysts))
            body = passes[min(len(calls) - 1, len(passes) - 1)]
            if isinstance(body, Exception):
                raise body
            return {"rating": body.get("rating", "Hold"), "reports": body["reports"], "debates": {}}

        outcome = governed_run(
            governor=self.governor, profile=_Profile(), spec=_Spec(), trade_date="2026-09-15",
            analysts=self.ANALYSTS, runner=runner, run_id="run-1",
        )
        return outcome, calls

    def test_one_missing_report_is_published_with_the_gap_named(self):
        outcome, calls = self._run([
            {"reports": _reports("market", "social", "fundamentals")},
            {"reports": _reports("market", "social", "fundamentals")},
        ])
        self.assertEqual(len(calls), 2, "a missing report earns exactly one repeat")
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["rating"], "Hold")
        self.assertEqual(outcome["analystRetries"], 1)
        self.assertEqual(outcome["analystCoverage"]["missing"], ["news"])
        self.assertTrue(outcome["degraded"])
        self.assertTrue(any("news" in w for w in outcome["result"]["warnings"]))
        self.assertEqual(outcome["ledger"]["missing_analysts"], ["news"])

    def test_a_recovered_analyst_leaves_no_gap_behind(self):
        outcome, calls = self._run([
            {"reports": _reports("market", "social", "news")},
            {"reports": _reports(*self.ANALYSTS)},
        ])
        self.assertEqual(len(calls), 2)
        self.assertTrue(outcome["ok"])
        self.assertFalse(outcome["degraded"])
        self.assertEqual(outcome["analystCoverage"]["missing"], [])
        self.assertEqual(outcome["ledger"]["analyst_retries"], 1)

    def test_the_repeat_is_bounded_and_not_a_loop(self):
        outcome, calls = self._run([{"reports": _reports("market")}])
        self.assertEqual(len(calls), 2, "at most 1 + max_analyst_retries passes")

    def test_a_conclusion_without_most_of_the_panel_is_not_a_rating(self):
        outcome, calls = self._run([{"reports": _reports("market")}])
        self.assertFalse(outcome["ok"])
        self.assertIsNone(outcome["rating"], "the model's answer is archived, not published")
        self.assertEqual(outcome["failure"]["type"], "AnalystDegraded")
        self.assertIn("social", outcome["failure"]["message"])
        self.assertEqual(outcome["analystCoverage"]["missing"], ["social", "news", "fundamentals"])
        self.assertEqual(outcome["ledger"]["missing_analysts"], ["social", "news", "fundamentals"])
        self.assertEqual(outcome["ledger"]["rating"], None)

    def test_an_empty_panel_is_a_failure_not_a_degradation(self):
        # "Degraded" must not become a synonym for "failed": a run that produced
        # nothing is broken, and reading it as a thin answer would hide that.
        outcome, _ = self._run([{"reports": {}}])
        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["analystCoverage"]["failed"])
        self.assertFalse(outcome["analystCoverage"]["degraded"])
        self.assertFalse(outcome["degraded"])
        self.assertIsNone(outcome["rating"])
        self.assertFalse(self.governor.summary()["recent"][0]["degraded"])

    def test_expired_data_does_not_produce_a_normal_rating(self):
        # The model still answers, and the answer is still archived - but a
        # verdict on data that does not reach its trade date is not a rating, and
        # publishing it as one is the failure this rule exists to prevent.
        stale = {"symbol": "NVDAUSDT", "available": True, "version": "v-old",
                 "asOf": "2026-09-01", "stale": True, "staleByDays": 14,
                 "staleReason": "数据未覆盖研判日期", "complete": True}
        with patch("quantdesk.tradingagents_runner.data_reference", return_value=stale):
            outcome, _ = self._run([{"reports": _reports(*self.ANALYSTS), "rating": "Buy"}])
        self.assertIsNone(outcome["rating"], "no normal rating on expired data")
        self.assertEqual(outcome["modelRating"], "Buy", "the model's own answer is kept")
        self.assertTrue(outcome["staleness"]["stale"])
        self.assertEqual(outcome["staleness"]["asOf"], "2026-09-01")
        self.assertTrue(outcome["ok"], "the run itself succeeded; its verdict is what is withheld")
        self.assertIsNone(outcome["ledger"]["rating"])

    def test_the_repeat_stops_at_the_single_run_allowance(self):
        # The first pass already spent the run's whole allowance, so repeating it
        # would spend money the operator capped away.
        governor = AgentCostGovernor(self.home, RunBudget(per_run_usd=0.000001, daily_usd=5.0))
        calls: list[int] = []

        def runner(*, analysts, timeout_seconds, callbacks):
            calls.append(1)
            for callback in callbacks:
                callback.absorb("deepseek-v4-pro", {"cacheMiss": 1_000_000, "output": 100_000, "calls": 3})
            return {"rating": "Hold", "reports": _reports("market"), "debates": {}}

        outcome = governed_run(
            governor=governor, profile=_Profile(), spec=_Spec(), trade_date="2026-09-15",
            analysts=self.ANALYSTS, runner=runner, run_id="run-2",
        )
        self.assertEqual(len(calls), 1, "no repeat once the allowance is spent")
        self.assertEqual(outcome["analystRetries"], 0)


if __name__ == "__main__":
    unittest.main()
