from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quantdesk.alerts import AlertEngine, AlertRuleError
from quantdesk.api.server import app
from quantdesk.datahub.db import Database


def seed_candles(db: Database, symbol: str = "BTCUSDT", *, close: float = 110.0) -> int:
    now = int(time.time() * 1000)
    steps = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
    last_by_frame = {
        timeframe: (now // step) * step - step - (step if timeframe == "1h" else 0)
        for timeframe, step in steps.items()
    }
    for timeframe, step in steps.items():
        last_ts = last_by_frame[timeframe]
        rows = []
        for index in range(240):
            price = close - (239 - index) * 0.01
            rows.append(
                {
                    "ts": last_ts - (239 - index) * step,
                    "open": price - 0.2,
                    "high": price + 0.5,
                    "low": price - 0.5,
                    "close": price,
                    "volume": 100 + index,
                }
            )
        db.upsert_candles("bybit", symbol, timeframe, rows)
    return last_by_frame["1h"]


class AlertEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.engine = AlertEngine(self.home)
        self.last_ts = seed_candles(self.engine.db)

    def create_price_rule(self):
        return self.engine.create_rule(
            {
                "name": "BTC 突破",
                "venue_symbol": "BTCUSDT",
                "condition_type": "price_above",
                "timeframe": "1h",
                "threshold": 100,
                "cooldown_seconds": 60,
                "enabled": True,
            }
        )

    def append_1h(self, close: float, offset: int = 1):
        self.engine.db.upsert_candles(
            "bybit", "BTCUSDT", "1h",
            [{"ts": self.last_ts + 3_600_000 * offset, "open": close - 1, "high": close + 1, "low": close - 2, "close": close, "volume": 400}],
        )

    def test_trigger_is_persisted_and_same_observation_is_deduplicated(self):
        rule = self.create_price_rule()
        first = self.engine.evaluate_symbol("BTC")
        second = self.engine.evaluate_symbol("BTCUSDT")

        self.assertEqual(len(first["triggered"]), 1)
        self.assertEqual(second["triggered"], [])
        self.assertEqual(len(self.engine.list_events()), 1)
        updated = self.engine.get_rule(rule["id"])
        self.assertTrue(updated["lastCondition"])
        self.assertEqual(updated["lastObservedAt"], self.last_ts)

    def test_new_observation_can_retrigger_after_cooldown(self):
        rule = self.create_price_rule()
        self.engine.evaluate_symbol("BTCUSDT")
        self.engine.db.execute(
            "UPDATE alert_rules SET last_triggered_ts=? WHERE id=?",
            (int(time.time() * 1000) - 120_000, rule["id"]),
        )
        self.engine.db.upsert_candles(
            "bybit",
            "BTCUSDT",
            "1h",
            [{"ts": self.last_ts + 3_600_000, "open": 111, "high": 113, "low": 110, "close": 112, "volume": 400}],
        )
        result = self.engine.evaluate_symbol("BTCUSDT")
        self.assertEqual(len(result["triggered"]), 1)
        self.assertEqual(len(self.engine.list_events()), 2)

    def test_disabled_rule_is_not_evaluated(self):
        rule = self.create_price_rule()
        self.engine.update_rule(
            rule["id"],
            {
                "name": rule["name"], "venue_symbol": rule["venueSymbol"],
                "condition_type": rule["conditionType"], "timeframe": rule["timeframe"],
                "threshold": rule["threshold"], "cooldown_seconds": 60, "enabled": False,
            },
        )
        result = self.engine.evaluate_symbol("BTCUSDT")
        self.assertEqual(result["evaluated"], 0)

    def test_simultaneous_engine_instances_do_not_duplicate_a_trigger(self):
        self.create_price_rule()
        other = AlertEngine(self.home)
        results = []

        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(engine.evaluate_symbol, "BTCUSDT") for engine in (self.engine, other)]
            results = [future.result() for future in futures]

        self.assertEqual(sum(len(result["triggered"]) for result in results), 1)
        self.assertEqual(len(self.engine.list_events()), 1)

    def test_invalid_rule_is_rejected(self):
        with self.assertRaises(AlertRuleError):
            self.engine.create_rule(
                {
                    "name": "bad", "venue_symbol": "DOGEUSDT", "condition_type": "price_above",
                    "timeframe": "1h", "threshold": 1, "cooldown_seconds": 60,
                }
            )

    def test_compound_rule_requires_every_condition(self):
        rule = self.engine.create_rule(
            {
                "name": "价格与放量", "venue_symbol": "BTCUSDT",
                "conditions": [
                    {"condition_type": "price_above", "timeframe": "1h", "threshold": 100},
                    {"condition_type": "volume_ratio_above", "timeframe": "1h", "threshold": 1},
                ],
                "cooldown_seconds": 60,
            }
        )
        result = self.engine.evaluate_symbol("BTCUSDT")
        self.assertEqual(len(rule["conditions"]), 2)
        self.assertEqual(len(result["triggered"]), 1)
        self.assertEqual(result["triggered"][0]["conditionType"], "compound")

    def test_crossing_waits_for_a_real_transition(self):
        self.engine.create_rule(
            {
                "name": "真正上穿", "venue_symbol": "BTCUSDT", "condition_type": "price_cross_above",
                "timeframe": "1h", "threshold": 111, "cooldown_seconds": 60,
            }
        )
        self.assertEqual(self.engine.evaluate_symbol("BTCUSDT")["triggered"], [])
        self.append_1h(112)
        self.assertEqual(len(self.engine.evaluate_symbol("BTCUSDT")["triggered"]), 1)

    def test_crossing_rejects_consecutive_confirmation(self):
        with self.assertRaisesRegex(AlertRuleError, "只能使用 1 次确认"):
            self.engine.create_rule(
                {
                    "name": "错误的交叉确认", "venue_symbol": "BTCUSDT",
                    "condition_type": "price_cross_above", "timeframe": "1h",
                    "threshold": 111, "cooldown_seconds": 60, "confirmation_count": 2,
                }
            )

    def test_confirmation_count_uses_distinct_observations(self):
        self.engine.create_rule(
            {
                "name": "两次确认", "venue_symbol": "BTCUSDT", "condition_type": "price_above",
                "timeframe": "1h", "threshold": 100, "cooldown_seconds": 60, "confirmation_count": 2,
            }
        )
        self.assertEqual(self.engine.evaluate_symbol("BTCUSDT")["triggered"], [])
        self.assertEqual(self.engine.evaluate_symbol("BTCUSDT")["triggered"], [])
        self.append_1h(112)
        self.assertEqual(len(self.engine.evaluate_symbol("BTCUSDT")["triggered"]), 1)

    def test_hysteresis_must_rearm_before_retrigger(self):
        rule = self.engine.create_rule(
            {
                "name": "迟滞", "venue_symbol": "BTCUSDT", "condition_type": "price_above",
                "timeframe": "1h", "threshold": 100, "cooldown_seconds": 60, "hysteresis": 5,
            }
        )
        self.assertEqual(len(self.engine.evaluate_symbol("BTCUSDT")["triggered"]), 1)
        self.engine.db.execute("UPDATE alert_rules SET last_triggered_ts=? WHERE id=?", (0, rule["id"]))
        self.append_1h(110, 1)
        self.assertEqual(self.engine.evaluate_symbol("BTCUSDT")["triggered"], [])
        self.append_1h(94, 2)
        self.engine.evaluate_symbol("BTCUSDT")
        self.append_1h(106, 3)
        self.assertEqual(len(self.engine.evaluate_symbol("BTCUSDT")["triggered"]), 1)

    def test_quiet_window_suppresses_delivery(self):
        self.engine.create_rule(
            {
                "name": "全日静默", "venue_symbol": "BTCUSDT", "condition_type": "price_above",
                "timeframe": "1h", "threshold": 100, "cooldown_seconds": 60,
                "quiet_start": "00:00", "quiet_end": "00:00",
            }
        )
        self.assertEqual(self.engine.evaluate_symbol("BTCUSDT")["triggered"], [])

    def test_daily_limit_suppresses_additional_events(self):
        rule = self.engine.create_rule(
            {
                "name": "每日一次", "venue_symbol": "BTCUSDT", "condition_type": "price_above",
                "timeframe": "1h", "threshold": 100, "cooldown_seconds": 60, "daily_limit": 1,
            }
        )
        self.assertEqual(len(self.engine.evaluate_symbol("BTCUSDT")["triggered"]), 1)
        self.engine.db.execute("UPDATE alert_rules SET last_triggered_ts=? WHERE id=?", (0, rule["id"]))
        self.append_1h(112)
        self.assertEqual(self.engine.evaluate_symbol("BTCUSDT")["triggered"], [])

    def test_stale_required_frame_blocks_signal(self):
        self.create_price_rule()
        self.engine.db.execute("DELETE FROM candles WHERE symbol='BTCUSDT' AND interval='1h'")
        result = self.engine.evaluate_symbol("BTCUSDT")
        self.assertEqual(result["triggered"], [])
        self.assertEqual(len(result["blocked"]), 1)


class AlertApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp.name}, clear=False)
        self.env.start()
        seed_candles(Database(Path(self.tmp.name) / "quantdesk.db"))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()
        self.tmp.cleanup()

    async def test_rule_crud_and_manual_evaluation(self):
        catalog = await self.client.get("/api/alerts")
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(len(catalog.json()["instruments"]), 17)

        created = await self.client.post(
            "/api/alerts/rules",
            json={
                "name": "BTC 价格", "venueSymbol": "BTCUSDT", "conditionType": "price_above",
                "timeframe": "1h", "threshold": 100, "cooldownSeconds": 60, "enabled": True,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        rule = created.json()
        evaluated = await self.client.post("/api/alerts/evaluate", params={"symbol": "BTC"})
        self.assertEqual(len(evaluated.json()["triggered"]), 1)

        disabled = await self.client.put(
            f"/api/alerts/rules/{rule['id']}",
            json={
                "name": rule["name"], "venueSymbol": rule["venueSymbol"],
                "conditionType": rule["conditionType"], "timeframe": rule["timeframe"],
                "threshold": rule["threshold"], "cooldownSeconds": 60, "enabled": False,
            },
        )
        self.assertFalse(disabled.json()["enabled"])
        removed = await self.client.delete(f"/api/alerts/rules/{rule['id']}")
        self.assertTrue(removed.json()["removed"])
        final = await self.client.get("/api/alerts")
        self.assertEqual(final.json()["rules"], [])
        self.assertEqual(len(final.json()["events"]), 1)

    async def test_strategy_alert_can_be_created_from_backtest_configuration(self):
        created = await self.client.post(
            "/api/alerts/from-strategy",
            json={
                "venueSymbol": "BTCUSDT", "timeframe": "1h", "strategyId": "ma_cross",
                "strategyParameters": {"fastPeriod": 9, "slowPeriod": 21}, "signalDirection": "any",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        rule = created.json()
        self.assertEqual(rule["conditions"][0]["strategyId"], "ma_cross")
        self.assertEqual(rule["conditions"][0]["conditionType"], "strategy_signal")


if __name__ == "__main__":
    unittest.main()
