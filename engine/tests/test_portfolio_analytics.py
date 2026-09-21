"""Portfolio analytics: what is sent, what is cached, and what is refused.

Two properties matter more than the maths, which lives at the provider:

* a paid computation is not repeated for an identical question, and never reused
  for a different portfolio, price snapshot or scenario;
* a failure is contained and recorded, and never overwrites the last good answer.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quantdesk.analytics import (
    PortfolioAnalyticsService,
    build_snapshot,
    input_hash,
    positions_hash,
)
from quantdesk.datahub.db import Database

CONFIG = {"cache_ttl_minutes": {"portfolio": 15, "scenario": 15}, "retention_days": {"analytics": 400}}

POSITIONS = [
    {"symbol": "NVDAUSDT", "side": "long", "quantity": 2, "entryPrice": 180, "markPrice": 184,
     "notional": 368, "margin": 73.6},
    {"symbol": "BTCUSDT", "side": "short", "quantity": 0.01, "entryPrice": 60000, "markPrice": 59000,
     "notional": 590, "margin": 59},
]
RETURNS = {symbol: [{"time": 1789000000000 + index * 3_600_000, "return": 0.001 * (index % 5)}
                    for index in range(60)]
           for symbol in ("NVDAUSDT", "BTCUSDT")}


class _Clock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


class SnapshotTests(unittest.TestCase):
    def test_the_snapshot_uses_quantdesk_codes_and_quantdesk_returns(self):
        snapshot = build_snapshot(
            positions=POSITIONS, returns=RETURNS, market_version="v1", equity=1000.0
        )
        self.assertEqual([item["symbol"] for item in snapshot.positions], ["NVDAUSDT", "BTCUSDT"])
        self.assertEqual(sorted(snapshot.returns), ["BTCUSDT", "NVDAUSDT"])
        self.assertEqual(snapshot.gross_exposure, 958.0)
        self.assertEqual(snapshot.net_exposure, 368.0 - 590.0)
        self.assertEqual(snapshot.margin_used, 73.6 + 59)

    def test_a_contract_outside_the_pool_is_named_and_excluded(self):
        snapshot = build_snapshot(
            positions=POSITIONS + [{"symbol": "DOGEUSDT", "side": "long", "quantity": 5, "markPrice": 1}],
            returns=RETURNS, market_version="v1", equity=1000.0,
        )
        self.assertEqual(len(snapshot.positions), 2)
        self.assertTrue(any("DOGEUSDT" in warning for warning in snapshot.warnings))

    def test_a_thin_return_history_is_flagged_rather_than_trusted(self):
        snapshot = build_snapshot(
            positions=POSITIONS, returns={"NVDAUSDT": RETURNS["NVDAUSDT"][:3], "BTCUSDT": RETURNS["BTCUSDT"]},
            market_version="v1", equity=1000.0,
        )
        self.assertTrue(any("NVDAUSDT" in warning and "样本不足" in warning for warning in snapshot.warnings))

    def test_the_hash_covers_positions_parameters_and_the_market_version(self):
        base = dict(kind="portfolio", positions=POSITIONS, parameters={"confidence": 0.95},
                    market_version="v1")
        self.assertEqual(input_hash(**base), input_hash(**base))
        self.assertNotEqual(input_hash(**base), input_hash(**{**base, "market_version": "v2"}))
        self.assertNotEqual(input_hash(**base), input_hash(**{**base, "parameters": {"confidence": 0.99}}))
        moved = [dict(POSITIONS[0], markPrice=190), POSITIONS[1]]
        self.assertNotEqual(input_hash(**base), input_hash(**{**base, "positions": moved}))
        self.assertNotEqual(positions_hash(POSITIONS), positions_hash(moved))


class _Provider:
    def __init__(self, results=None, error=None):
        self.results = results or {}
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, kind, params):
        self.calls.append((kind, params))
        if self.error is not None:
            raise self.error
        return self.results.get(kind, {"provider": "fincept-api", "requestId": "req-1", "metrics": {}})


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        self.snapshot = build_snapshot(
            positions=POSITIONS, returns=RETURNS, market_version="v1", equity=1000.0
        )

    def _service(self, provider, clock=None):
        return PortfolioAnalyticsService(self.db, provider, config=CONFIG, now=clock)

    def test_a_portfolio_call_sends_contract_codes_returns_and_the_snapshot_version(self):
        provider = _Provider({"portfolio": {"provider": "fincept-api", "requestId": "req-9",
                                           "metrics": {"var": -0.03}, "riskContributions": []}})
        outcome = self._service(provider).portfolio(self.snapshot)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.provider, "fincept-api")
        self.assertEqual(outcome.request_id, "req-9")
        kind, params = provider.calls[0]
        self.assertEqual(kind, "portfolio")
        self.assertEqual([item["symbol"] for item in params["positions"]], ["NVDAUSDT", "BTCUSDT"])
        self.assertEqual(params["marketSnapshotVersion"], "v1")
        self.assertIn("NVDAUSDT", params["returns"])
        self.assertNotIn("DOGEUSDT", json.dumps(params))

    def test_an_identical_question_is_answered_from_cache(self):
        provider = _Provider()
        service = self._service(provider)
        first = service.portfolio(self.snapshot)
        second = service.portfolio(self.snapshot)
        self.assertEqual(len(provider.calls), 1, "同一输入不得重复付费计算")
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(second.input_hash, first.input_hash)

    def test_a_moved_price_or_position_is_a_different_question(self):
        provider = _Provider()
        service = self._service(provider)
        service.portfolio(self.snapshot)
        moved = build_snapshot(
            positions=[dict(POSITIONS[0], markPrice=190), POSITIONS[1]],
            returns=RETURNS, market_version="v2", equity=1000.0,
        )
        service.portfolio(moved)
        self.assertEqual(len(provider.calls), 2, "快照变化后必须重新计算")

    def test_an_expired_cache_entry_is_recomputed(self):
        import datetime as dt

        clock = _Clock(dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.timezone.utc))
        provider = _Provider()
        service = self._service(provider, clock=clock)
        service.portfolio(self.snapshot)
        clock.moment = clock.moment + dt.timedelta(minutes=16)
        service.portfolio(self.snapshot)
        self.assertEqual(len(provider.calls), 2)

    def test_force_bypasses_the_cache(self):
        provider = _Provider()
        service = self._service(provider)
        service.portfolio(self.snapshot)
        service.portfolio(self.snapshot, force=True)
        self.assertEqual(len(provider.calls), 2)

    def test_a_result_records_the_market_version_it_was_computed_from(self):
        provider = _Provider()
        self._service(provider).portfolio(self.snapshot)
        rows = self.db.list_external_analytics(kind="portfolio")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_snapshot_version"], "v1")
        self.assertEqual(rows[0]["status"], "ok")
        self.assertTrue(rows[0]["input_hash"])
        self.assertEqual(json.loads(rows[0]["parameters_json"])["positions"], 2)

    def test_an_unavailable_provider_is_reported_and_recorded(self):
        provider = _Provider({"portfolio": {"unavailable": "未配置 FINCEPT_API_KEY"}})
        outcome = self._service(provider).portfolio(self.snapshot)
        self.assertFalse(outcome.ok)
        self.assertIn("FINCEPT_API_KEY", outcome.unavailable)
        rows = self.db.list_external_analytics(kind="portfolio")
        self.assertEqual(rows[0]["status"], "error")
        self.assertIsNone(rows[0]["result_json"])

    def test_a_provider_error_is_contained_and_leaves_the_run_usable(self):
        provider = _Provider(error=TimeoutError("上游超时"))
        outcome = self._service(provider).portfolio(self.snapshot)
        self.assertFalse(outcome.ok)
        self.assertIn("TimeoutError", outcome.error)
        self.assertEqual(outcome.market_version, "v1")

    def test_a_failure_does_not_overwrite_the_last_successful_result(self):
        good = _Provider({"portfolio": {"provider": "fincept-api", "requestId": "req-ok",
                                       "metrics": {"var": -0.02}}})
        service = self._service(good)
        service.portfolio(self.snapshot)
        # Same question, provider now down, cache bypassed on purpose.
        broken = PortfolioAnalyticsService(self.db, _Provider(error=RuntimeError("down")), config=CONFIG)
        broken.portfolio(self.snapshot, force=True)
        reusable = self.db.find_external_analytics(
            input_hash(kind="portfolio", positions=self.snapshot.positions,
                       parameters={"confidence": 0.95, "optimize": False}, market_version="v1"),
            kind="portfolio",
        )
        self.assertIsNotNone(reusable, "失败不得覆盖上一次成功结果")
        self.assertEqual(json.loads(reusable["result_json"])["requestId"], "req-ok")

    def test_a_scenario_resolves_its_rules_to_one_shock_per_contract(self):
        provider = _Provider({"scenario": {"provider": "fincept-api", "scenario": "semiconductor_shock",
                                          "equityChangePct": -4.2, "positions": []}})
        outcome = self._service(provider).scenario(self.snapshot, scenario_id="semiconductor_shock")
        self.assertTrue(outcome.ok)
        _, params = provider.calls[0]
        self.assertEqual(params["scenario"], "semiconductor_shock")
        self.assertEqual(params["shocks"]["NVDAUSDT"], -15.0)
        self.assertNotIn("BTCUSDT", params["shocks"], "加密资产不在半导体情景内")
        self.assertEqual(params["marketSnapshotVersion"], "v1")

    def test_a_custom_shock_is_sent_verbatim(self):
        provider = _Provider({"scenario": {"provider": "fincept-api", "scenario": "custom"}})
        self._service(provider).scenario(
            self.snapshot, scenario_id="all_equities_down", shocks={"symbol:BTCUSDT": -7.5}
        )
        _, params = provider.calls[0]
        self.assertEqual(params["shocks"], {"BTCUSDT": -7.5})

    def test_scenario_results_are_cached_per_scenario_and_snapshot(self):
        provider = _Provider({"scenario": {"provider": "fincept-api"}})
        service = self._service(provider)
        service.scenario(self.snapshot, scenario_id="crypto_selloff")
        service.scenario(self.snapshot, scenario_id="crypto_selloff")
        service.scenario(self.snapshot, scenario_id="all_equities_down")
        self.assertEqual(len(provider.calls), 2, "不同情景是不同的问题")

    def test_an_unknown_scenario_is_refused_locally(self):
        provider = _Provider()
        with self.assertRaises(KeyError):
            self._service(provider).scenario(self.snapshot, scenario_id="moon")
        self.assertEqual(provider.calls, [], "未知情景不应产生外部调用")

    def test_pruning_applies_the_configured_retention(self):
        import datetime as dt

        clock = _Clock(dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.timezone.utc))
        service = self._service(_Provider(), clock=clock)
        service.portfolio(self.snapshot)
        self.assertEqual(service.prune()["analytics"], 0)
        clock.moment = clock.moment + dt.timedelta(days=401)
        self.assertEqual(service.prune()["analytics"], 1)

    def test_no_result_can_place_an_order(self):
        # The analytics layer exposes reads only: there is no order, position or
        # account-mutating entry point on the service.
        service = self._service(_Provider())
        for name in ("place_order", "submit_order", "open_position", "close_position", "set_leverage"):
            self.assertFalse(hasattr(service, name), f"分析层不应有 {name}")


if __name__ == "__main__":
    unittest.main()
