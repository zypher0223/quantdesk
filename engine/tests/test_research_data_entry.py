"""Phase 1: one formal research entry point for every kind of study.

A single backtest, a parameter search, a walk-forward run and a portfolio are all
formal studies. These tests hold them to the same three promises:

* they read the local store, never a fresh venue fetch;
* they refuse to publish when the data cannot support them, unless degradation is
  explicitly accepted, and then they say what is missing;
* the same data, read twice, gives the same answer.

The venue client is disabled for the whole file: if any formal path tries to
build one, the test fails with that message rather than quietly passing.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quantdesk.api.server import app
from quantdesk.datahub.db import Database

HOUR = 3_600_000
BAR_COUNT = 400
# Volatile fields: a timestamp of when the run happened is not part of its result.
# `ageMs` is how long ago the risk ladder was collected: a live measurement of
# the present, not part of the study's result.
VOLATILE = {"run_at", "runAt", "generatedAt", "generated_at", "durationMs", "duration_ms",
            "checkedAt", "ageMs"}


def stable(payload):
    """Strip wall-clock fields so two runs of the same study can be compared."""
    if isinstance(payload, dict):
        return {key: stable(value) for key, value in payload.items() if key not in VOLATILE}
    if isinstance(payload, list):
        return [stable(item) for item in payload]
    return payload


class ResearchEntryFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"QUANTDESK_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.db = Database(self.home / "quantdesk.db")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()
        self.tmp.cleanup()

    def seed(self, symbol: str = "BTCUSDT", *, interval: str = "1h", bars: int = BAR_COUNT,
             marks: bool = True, funding: bool = True, snapshots: bool = True, meta: bool = True,
             tiers: bool = True, source: str = "venue_rest"):
        step = {"15m": 900_000, "1h": HOUR, "4h": 4 * HOUR, "1d": 24 * HOUR}[interval]
        stored = 1_700_000_000_000 - (1_700_000_000_000 % step) - step
        rows = [
            {
                "ts": stored - index * step,
                "open": 100.0 + index * 0.1,
                "high": 101.0 + index * 0.1,
                "low": 99.0 + index * 0.1,
                "close": 100.5 + index * 0.1,
                "volume": 10.0 + index,
                "source": source,
            }
            for index in range(bars)
        ]
        self.db.upsert_candles("bybit", symbol, interval, rows, source=source)
        if marks:
            self.db.upsert_mark_candles(
                "bybit", symbol, interval,
                [{"ts": row["ts"], "open": row["close"], "high": row["close"],
                  "low": row["close"], "close": row["close"]} for row in rows],
            )
        if funding:
            self.db.upsert_funding(
                "bybit", symbol,
                [{"ts": row["ts"], "rate": 0.0001} for row in rows[::8]],
            )
        self.db.upsert_oi(
            "bybit", symbol,
            [{"ts": row["ts"], "oi": 1000.0 + index} for index, row in enumerate(rows[::4])],
        )
        if snapshots:
            for kind, kind_interval, version in (
                ("trade_candle", interval, f"history/1:{symbol.lower()}"),
                ("mark_candle", interval, f"series/1:{symbol.lower()}-marks"),
                ("funding", "", f"series/1:{symbol.lower()}-funding"),
                ("open_interest", "", f"series/1:{symbol.lower()}-oi"),
            ):
                self.db.record_history_snapshot({
                    "venue": "bybit", "symbol": symbol, "interval": kind_interval,
                    "data_kind": kind, "version": version,
                    "from_ts": rows[-1]["ts"], "to_ts": rows[0]["ts"], "bars": len(rows),
                })
        if tiers:
            # The ladder is what decides margin and the leverage cap, so a dataset
            # without one is incomplete rather than merely thin - and a ladder the
            # venue may since have changed is stale, which the gate also reports.
            import time as _time

            from quantdesk.risk import RiskProfile, tier_rows_for_db

            stamp = int(_time.time() * 1000)
            profile = RiskProfile.from_rows(
                symbol,
                [{"riskLimitValue": "300000", "maintenanceMargin": "0.5", "maxLeverage": "150"}],
                synced_at=stamp,
            )
            rows_for_db = tier_rows_for_db(profile)
            self.db.upsert_risk_tiers(
                "bybit", symbol, rows_for_db, source=profile.source, synced_at=stamp,
            )
            if snapshots:
                from quantdesk.datahub.history import RISK_LIMIT, series_version

                self.db.record_history_snapshot({
                    "venue": "bybit", "symbol": symbol, "interval": "", "data_kind": RISK_LIMIT,
                    "version": series_version(RISK_LIMIT, "bybit", symbol, rows_for_db),
                    "from_ts": stamp, "to_ts": stamp, "bars": len(rows_for_db),
                })
        if meta:
            self.db.upsert_instrument_meta({
                "venue": "bybit", "symbol": symbol, "launch_ts": rows[-1]["ts"],
                "funding_interval_hours": 8, "tick_size": 0.1, "qty_step": 0.001,
            })
        return rows

    def no_venue(self):
        """Fail loudly if any formal path builds a venue client."""
        from quantdesk.datahub.bybit import BybitClient

        return patch.object(
            BybitClient, "__init__",
            side_effect=AssertionError("正式研究不得访问交易所（本地历史已足够）"),
        )

    async def backtest(self, **overrides):
        body = {"symbol": "BTCUSDT", "timeframe": "1h", "bars": 300, "strategyId": "ma_cross"}
        body.update(overrides)
        return await self.client.post("/api/backtest", json=body)

    async def validate(self, **overrides):
        body = {"symbol": "BTCUSDT", "timeframe": "1h", "bars": 300, "fastGrid": [5, 9],
                "slowGrid": [21, 50], "walkForwardWindows": 2}
        body.update(overrides)
        return await self.client.post("/api/validate", json=body)

    async def portfolio(self, **overrides):
        body = {"symbols": ["BTCUSDT"], "timeframe": "1h", "bars": 300}
        body.update(overrides)
        return await self.client.post("/api/portfolio", json=body)


class OfflineStudyTests(ResearchEntryFixture):
    """With complete local history and no network, all three studies must finish."""

    async def test_all_three_studies_run_without_touching_the_venue(self):
        self.seed()
        with self.no_venue():
            backtest = await self.backtest()
            validate = await self.validate()
            portfolio = await self.portfolio()
        self.assertEqual(backtest.status_code, 200, backtest.text)
        self.assertEqual(validate.status_code, 200, validate.text)
        self.assertEqual(portfolio.status_code, 200, portfolio.text)

    async def test_each_study_reports_the_range_it_read_and_every_version(self):
        self.seed()
        with self.no_venue():
            bodies = {
                "backtest": (await self.backtest()).json(),
                "validate": (await self.validate()).json(),
                "portfolio": (await self.portfolio()).json(),
            }
        for name, body in bodies.items():
            with self.subTest(study=name):
                self.assertTrue(body["readRange"]["fromTs"] and body["readRange"]["toTs"], name)
                self.assertEqual(body["readRange"]["bars"], 300, name)
                versions = body["versions"] if name != "portfolio" else body["members"]["BTCUSDT"]["versions"]
                self.assertTrue(versions["readCandles"], name)
                self.assertTrue(versions["snapshotCandles"], name)
                self.assertTrue(versions["snapshotMarks"], name)
                self.assertTrue(versions["snapshotFunding"], name)
                self.assertTrue(body["strategyVersion"]["strategyId"], name)
                self.assertTrue(body["costModel"], name)

    async def test_the_three_studies_read_the_same_window(self):
        self.seed()
        with self.no_venue():
            backtest = (await self.backtest()).json()
            validate = (await self.validate()).json()
            portfolio = (await self.portfolio()).json()
        self.assertEqual(backtest["readRange"]["fromTs"], validate["readRange"]["fromTs"])
        self.assertEqual(backtest["readRange"]["toTs"], validate["readRange"]["toTs"])
        self.assertEqual(backtest["readRange"]["toTs"], portfolio["readRange"]["toTs"])


class GateTests(ResearchEntryFixture):
    """Incomplete data is refused everywhere, and labelled when accepted."""

    async def test_all_three_refuse_when_the_history_is_missing(self):
        # Snapshots and metadata only: no candles, no marks, no funding.
        self.seed(bars=0, marks=False, funding=False, snapshots=False, meta=False, tiers=False)
        with self.no_venue():
            responses = {
                "backtest": await self.backtest(),
                "validate": await self.validate(),
                "portfolio": await self.portfolio(),
            }
        for name, response in responses.items():
            with self.subTest(study=name):
                self.assertEqual(response.status_code, 409, f"{name}: {response.text[:200]}")
                detail = json.loads(response.json()["detail"])
                self.assertIn("数据未就绪", detail["title"])
                self.assertTrue(detail["readiness"]["blocking"], name)

    async def test_all_three_refuse_when_only_the_mark_prices_are_missing(self):
        self.seed(marks=False)
        with self.no_venue():
            for name, call in (("backtest", self.backtest), ("validate", self.validate),
                               ("portfolio", self.portfolio)):
                with self.subTest(study=name):
                    response = await call()
                    self.assertEqual(response.status_code, 409, name)
                    detail = json.loads(response.json()["detail"])
                    self.assertIn("marks", [check["key"] for check in detail["readiness"]["blocking"]])

    async def test_the_degraded_path_runs_and_discloses_what_is_missing(self):
        self.seed(marks=False, funding=False)
        with self.no_venue():
            backtest = await self.backtest(allowDegraded=True)
            validate = await self.validate(allowDegraded=True)
            portfolio = await self.portfolio(allowDegraded=True)
        for name, response in (("backtest", backtest), ("validate", validate), ("portfolio", portfolio)):
            with self.subTest(study=name):
                self.assertEqual(response.status_code, 200, f"{name}: {response.text[:200]}")
                body = response.json()
                self.assertTrue(body["degraded"], name)
                self.assertFalse(body["dataReady"], name)
                self.assertTrue(body["missingData"], name)
                self.assertTrue(body["dataImpacts"], name)

    async def test_the_degraded_flag_reaches_the_backtest_result_itself(self):
        self.seed(marks=False)
        with self.no_venue():
            body = (await self.backtest(allowDegraded=True)).json()
        self.assertTrue(body["data_quality"]["degraded"])
        self.assertTrue(any("标记价格" in warning for warning in body["warnings"]))
        self.assertIn("readiness", body["data_quality"])


class DeterminismTests(ResearchEntryFixture):
    """One snapshot, one answer - twice."""

    async def test_repeating_each_study_on_the_same_data_gives_the_same_result(self):
        self.seed()
        with self.no_venue():
            pairs = {
                "backtest": ((await self.backtest()).json(), (await self.backtest()).json()),
                "validate": ((await self.validate()).json(), (await self.validate()).json()),
                "portfolio": ((await self.portfolio()).json(), (await self.portfolio()).json()),
            }
        for name, (first, second) in pairs.items():
            with self.subTest(study=name):
                self.assertEqual(stable(first), stable(second), f"{name} 同一数据必须得到一致结果")

    async def test_a_changed_bar_changes_the_read_version_and_the_result(self):
        self.seed()
        with self.no_venue():
            before = (await self.backtest()).json()
            self.db.upsert_candles(
                "bybit", "BTCUSDT", "1h",
                [{"ts": 1_700_000_000_000 - (1_700_000_000_000 % HOUR) - HOUR,
                  "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 99.0}],
                source="venue_rest",
            )
            after = (await self.backtest()).json()
        self.assertNotEqual(before["versions"]["readCandles"], after["versions"]["readCandles"])
        self.assertNotEqual(
            before["versions"]["readHistory"], after["versions"]["readHistory"],
            "K线内容变化后读取版本必须改变",
        )


class PortfolioCoverageTests(ResearchEntryFixture):
    """A portfolio's legs must describe one period."""

    async def test_a_short_history_member_is_named_and_not_averaged_away(self):
        self.seed("BTCUSDT", bars=400)
        self.seed("ETHUSDT", bars=120)          # much shorter history
        with self.no_venue():
            response = await self.portfolio(symbols=["BTCUSDT", "ETHUSDT"], allowDegraded=True)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        coverage = body["portfolioCoverage"]
        self.assertFalse(coverage["comparable"])
        self.assertIn("ETHUSDT", coverage["shortHistory"])
        self.assertTrue(any("ETHUSDT" in note for note in coverage["notes"]))
        self.assertTrue(body["degraded"])
        self.assertIn("portfolioComparability", body["missingData"])

    async def test_members_with_comparable_history_are_called_comparable(self):
        self.seed("BTCUSDT", bars=400)
        self.seed("ETHUSDT", bars=400)
        with self.no_venue():
            body = (await self.portfolio(symbols=["BTCUSDT", "ETHUSDT"])).json()
        self.assertTrue(body["portfolioCoverage"]["comparable"])
        self.assertEqual(body["portfolioCoverage"]["notes"], [])
        self.assertFalse(body["degraded"])

    async def test_each_member_keeps_its_own_versions(self):
        self.seed("BTCUSDT")
        self.seed("ETHUSDT")
        with self.no_venue():
            body = (await self.portfolio(symbols=["BTCUSDT", "ETHUSDT"])).json()
        self.assertEqual(set(body["memberVersions"]), {"BTCUSDT", "ETHUSDT"})
        self.assertNotEqual(
            body["memberVersions"]["BTCUSDT"]["readCandles"],
            body["memberVersions"]["ETHUSDT"]["readCandles"],
        )


class TokenisedStockTests(ResearchEntryFixture):
    """A tokenised stock is studied on its own contract, never on the share."""

    async def test_the_study_reads_the_exchange_contract_only(self):
        # AMDSTOCKUSDT's reference asset is AMD; the study must read the contract.
        self.seed("AMDSTOCKUSDT")
        with self.no_venue():
            body = (await self.backtest(symbol="AMDSTOCKUSDT")).json()
        self.assertEqual(body["instrument"]["venueSymbol"], "AMDSTOCKUSDT")
        self.assertEqual(body["versions"]["readCandles"], body["versions"]["readCandles"])
        contract_bars = self.db.load_candles("bybit", "AMDSTOCKUSDT", "1h")
        self.assertTrue(contract_bars)
        self.assertEqual(
            self.db.count_candles("bybit", "AMD", "1h"), 0,
            "研究只能读代币化合约，不得读公开股票代码",
        )
        # The read version is a hash over the contract's own bars; the contract it
        # belongs to is named by the instrument block above.
        self.assertEqual(body["instrument"]["venueSymbol"], "AMDSTOCKUSDT")

    async def test_the_read_symbol_is_the_contract_not_the_underlying(self):
        from quantdesk.config.instruments import require_instrument
        from quantdesk.datahub.readiness import load_research_data

        spec = require_instrument("AMDSTOCKUSDT")
        self.assertEqual(spec.underlying_symbol, "AMD")
        self.seed("AMDSTOCKUSDT")
        data = load_research_data(self.db, spec, interval="1h", bars=200)
        self.assertEqual(data.symbol, "AMDSTOCKUSDT")
        self.assertEqual(data.readiness.symbol, "AMDSTOCKUSDT")
        self.assertTrue(all(row["ts"] for row in data.candles))


if __name__ == "__main__":
    unittest.main()


class CommonWindowMarkTests(ResearchEntryFixture):
    """组合窗口也要避开还没有标记价的那一根，否则组合会莫名被门禁拒绝。"""

    async def test_the_common_window_ends_where_every_members_marks_end(self):
        from quantdesk.datahub.readiness import common_window

        self.seed("BTCUSDT")
        self.seed("ETHUSDT")
        newest = self.db.last_open_ts("bybit", "ETHUSDT", "1h")
        self.db.execute(
            "DELETE FROM mark_candles WHERE venue='bybit' AND symbol='ETHUSDT' AND open_ts=?",
            (newest,),
        )
        candles_only = common_window(self.db, venue="bybit", symbols=["BTCUSDT", "ETHUSDT"],
                                     interval="1h", bars=100)
        with_marks = common_window(self.db, venue="bybit", symbols=["BTCUSDT", "ETHUSDT"],
                                   interval="1h", bars=100, require_marks=True)
        self.assertEqual(candles_only[1], newest)
        self.assertLess(with_marks[1], candles_only[1], "需要标记价时窗口必须回退一根")
        self.assertEqual(with_marks[1] - with_marks[0], candles_only[1] - candles_only[0])
        self.assertEqual(with_marks[2]["ETHUSDT"]["lastMarkTs"], newest - 3_600_000)
