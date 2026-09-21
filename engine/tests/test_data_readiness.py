"""The data readiness gate, and the acceptance list this phase is judged by.

The gate exists so a formal backtest cannot quietly run on half a dataset. These
tests check both halves of that: what is refused, and what a deliberately degraded
run is obliged to disclose.
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
from quantdesk.datahub.readiness import BLOCKING, DEGRADED, OK, assess

HOUR = 3_600_000
NOW = 1_700_000_000_000 - (1_700_000_000_000 % HOUR)
FROM = NOW - 200 * HOUR


def bars(count: int, *, start: int = FROM, step: int = HOUR, source: str = "venue_rest") -> list[dict]:
    return [
        {"ts": start + index * step, "open": 100.0 + index, "high": 101.0 + index,
         "low": 99.0 + index, "close": 100.5 + index, "volume": 10.0 + index, "source": source}
        for index in range(count)
    ]


class ReadinessGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")
        self.collector_kwargs = dict(venue="bybit", symbol="BTCUSDT", interval="1h",
                                     from_ts=FROM, to_ts=NOW)

    def _assess(self, **overrides):
        return assess(self.db, **{**self.collector_kwargs, **overrides})

    def _seed(self, *, candles=True, marks=True, funding=True, snapshot=True, oi=False,
              candle_source="venue_rest", meta=True):
        if candles:
            self.db.upsert_candles("bybit", "BTCUSDT", "1h", bars(201), source=candle_source)
        if marks:
            self.db.upsert_mark_candles("bybit", "BTCUSDT", "1h",
                                        [{"ts": row["ts"], "open": 1, "high": 1, "low": 1, "close": 1}
                                         for row in bars(201)])
        if funding:
            self.db.upsert_funding("bybit", "BTCUSDT",
                                   [{"ts": FROM + index * 8 * HOUR, "rate": 0.0001} for index in range(25)])
        if oi:
            self.db.upsert_oi("bybit", "BTCUSDT",
                              [{"ts": FROM + index * HOUR, "oi": 100.0} for index in range(201)])
        if snapshot:
            self.db.record_history_snapshot({
                "venue": "bybit", "symbol": "BTCUSDT", "interval": "1h", "data_kind": "trade_candle",
                "version": "history/1:abc", "from_ts": FROM, "to_ts": NOW, "bars": 201,
            })
            self.db.record_history_snapshot({
                "venue": "bybit", "symbol": "BTCUSDT", "interval": "1h", "data_kind": "mark_candle",
                "version": "series/1:marks", "from_ts": FROM, "to_ts": NOW, "bars": 201,
            })
            self.db.record_history_snapshot({
                "venue": "bybit", "symbol": "BTCUSDT", "data_kind": "funding",
                "version": "series/1:funding", "from_ts": FROM, "to_ts": NOW, "bars": 25,
            })
        if meta:
            self.db.upsert_instrument_meta({
                "venue": "bybit", "symbol": "BTCUSDT", "launch_ts": FROM, "funding_interval_hours": 8,
            })

    def test_an_empty_store_blocks_on_the_load_bearing_inputs(self):
        readiness = self._assess()
        self.assertFalse(readiness.ok)
        self.assertEqual({check.key for check in readiness.blocking}, {"candles", "marks", "snapshot"})
        for key in ("candles", "marks", "snapshot"):
            self.assertIn(key, readiness.missing)

    def test_a_complete_store_passes_and_names_every_version(self):
        self._seed()
        readiness = self._assess()
        self.assertTrue(readiness.ok, [c.detail for c in readiness.blocking])
        self.assertEqual(readiness.versions["snapshotCandles"], "history/1:abc")
        self.assertEqual(readiness.versions["snapshotMarks"], "series/1:marks")
        self.assertEqual(readiness.versions["snapshotFunding"], "series/1:funding")
        self.assertEqual(readiness.versions["listing"], str(FROM))

    def test_missing_marks_block_because_liquidation_depends_on_them(self):
        self._seed(marks=False)
        readiness = self._assess()
        self.assertFalse(readiness.ok)
        self.assertEqual([check.key for check in readiness.blocking], ["marks"])
        self.assertTrue(any("强平" in impact for impact in readiness.impacts))

    def test_funding_with_a_middle_gap_is_degraded(self):
        self._seed()
        self.db.execute(
            "DELETE FROM funding WHERE venue=? AND symbol=? AND ts BETWEEN ? AND ?",
            ("bybit", "BTCUSDT", FROM + 60 * HOUR, FROM + 120 * HOUR),
        )
        readiness = self._assess()
        funding = next(check for check in readiness.checks if check.key == "funding")
        self.assertEqual(funding.status, DEGRADED)
        self.assertGreater(funding.values["maxGapMs"], 8 * HOUR)

    def test_open_interest_is_checked_in_the_requested_window_and_interval(self):
        self._seed()
        self.db.upsert_oi("bybit", "BTCUSDT", [{"ts": FROM - index * HOUR, "oi": 1.0} for index in range(200)], interval="1h")
        readiness = assess(
            self.db, venue="bybit", symbol="BTCUSDT", interval="1h", from_ts=FROM,
            to_ts=NOW, needs_open_interest=True, oi_interval="1h",
        )
        oi = next(check for check in readiness.checks if check.key == "open_interest")
        self.assertEqual(oi.status, DEGRADED)
        self.assertEqual(oi.values["points"], 1)

    def test_an_unsupported_funding_series_is_degraded_with_its_reason(self):
        self._seed(funding=False)
        self.db.upsert_backfill_state({
            "venue": "bybit", "symbol": "BTCUSDT", "interval": "", "data_kind": "funding",
            "status": "unsupported", "reason": "交易所元数据未给出资金费结算周期",
        })
        readiness = self._assess()
        self.assertTrue(readiness.ok, "资金费缺失是降级项，不是阻断项")
        self.assertTrue(readiness.degraded)
        funding = next(check for check in readiness.checks if check.key == "funding")
        self.assertEqual(funding.status, DEGRADED)
        self.assertIn("资金费结算周期", funding.detail)

    def test_open_interest_is_only_checked_when_a_factor_needs_it(self):
        self._seed(oi=False)
        without = self._assess(needs_open_interest=False)
        with_need = self._assess(needs_open_interest=True)
        self.assertEqual(next(c for c in without.checks if c.key == "open_interest").status, OK)
        self.assertEqual(next(c for c in with_need.checks if c.key == "open_interest").status, DEGRADED)

    def test_proxy_data_is_named_and_never_called_a_contract_backtest(self):
        self._seed(candles=False)
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", bars(201, source="local_derived"),
                               source="local_derived")
        readiness = self._assess()
        self.assertTrue(readiness.proxy_data)
        provenance = next(check for check in readiness.checks if check.key == "provenance")
        self.assertEqual(provenance.status, DEGRADED)
        self.assertTrue(any("不能称为该合约的合约回测" in impact for impact in readiness.impacts))

    def test_a_history_that_has_not_reached_the_listing_is_reported(self):
        self._seed(meta=False)
        self.db.upsert_instrument_meta({
            "venue": "bybit", "symbol": "BTCUSDT", "launch_ts": FROM - 5000 * HOUR,
            "funding_interval_hours": 8,
        })
        readiness = self._assess()
        listing = next(check for check in readiness.checks if check.key == "listing")
        self.assertEqual(listing.status, DEGRADED)
        self.assertIn("尚未回溯到上市日期", " ".join(readiness.impacts))


class BacktestGateTests(unittest.IsolatedAsyncioTestCase):
    """The gate as the backtest endpoint applies it."""

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

    def _seed(self, *, marks=True, snapshot=True, funding=True):
        step = HOUR
        stored = 1_700_000_000_000 - (1_700_000_000_000 % step) - step
        rows = [
            {"ts": stored - index * step, "open": 100.0 + index, "high": 101.0 + index,
             "low": 99.0 + index, "close": 100.5 + index, "volume": 10.0 + index, "source": "venue_rest"}
            for index in range(400)
        ]
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows)
        if marks:
            self.db.upsert_mark_candles("bybit", "BTCUSDT", "1h",
                                        [{"ts": row["ts"], "open": 1, "high": 1, "low": 1, "close": 1}
                                         for row in rows])
        if funding:
            self.db.upsert_funding("bybit", "BTCUSDT",
                                   [{"ts": row["ts"], "rate": 0.0001} for row in rows[::8]])
        self.db.upsert_risk_tiers("bybit", "BTCUSDT", [{
            "tier_id": 1, "risk_limit_value": 1_000_000,
            "maintenance_margin_rate": 0.005, "max_leverage": 100,
        }])
        if snapshot:
            for kind, interval, version in (
                ("trade_candle", "1h", "history/1:abc"),
                ("mark_candle", "1h", "series/1:marks"),
                ("funding", "", "series/1:funding"),
                ("risk_limit", "", "series/1:risk"),
            ):
                self.db.record_history_snapshot({
                    "venue": "bybit", "symbol": "BTCUSDT", "interval": interval, "data_kind": kind,
                    "version": version, "from_ts": rows[-1]["ts"], "to_ts": rows[0]["ts"],
                    "bars": len(rows),
                })
        self.db.upsert_instrument_meta({
            "venue": "bybit", "symbol": "BTCUSDT", "launch_ts": rows[-1]["ts"],
        })
        return stored

    async def test_a_formal_run_on_an_empty_store_is_refused_with_a_readiness_report(self):
        response = await self.client.post("/api/backtest", json={"symbol": "BTCUSDT", "bars": 300})
        self.assertEqual(response.status_code, 409, response.text)
        detail = json.loads(response.json()["detail"])
        self.assertIn("数据未就绪", detail["title"])
        self.assertIn("readiness", detail)
        self.assertTrue(detail["readiness"]["blocking"])

    async def test_the_degraded_path_runs_and_discloses_what_is_missing(self):
        self._seed(marks=False)
        response = await self.client.post(
            "/api/backtest", json={"symbol": "BTCUSDT", "bars": 300, "allowDegraded": True}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["degraded"])
        self.assertIn("marks", body["missingData"])
        self.assertTrue(body["dataImpacts"])
        self.assertFalse(body["dataReady"])

    async def test_a_ready_run_passes_and_carries_the_input_versions(self):
        self._seed()
        response = await self.client.post("/api/backtest", json={"symbol": "BTCUSDT", "bars": 300})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["dataReady"])
        versions = body["versions"]
        # The pinnned snapshot per series, plus the version of the window read.
        self.assertEqual(versions["snapshotCandles"], "history/1:abc")
        self.assertTrue(versions.get("snapshotMarks"))
        self.assertTrue(versions.get("snapshotFunding"))
        self.assertTrue(versions.get("readMarks"))
        self.assertTrue(versions.get("readFunding"))
        self.assertTrue(versions["readCandles"], "读取窗口本身也要有版本号")
        self.assertEqual(body["snapshotVersion"], versions["readHistory"])


class AcceptanceTests(unittest.TestCase):
    """The nine conditions this phase is accepted against, in one place."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(Path(self._tmp.name) / "quantdesk.db")

    def test_1_repeated_backfill_does_not_add_unique_rows(self):
        rows = bars(50)
        first = self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows)
        second = self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows)
        self.assertEqual(first.inserted, 50)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.unchanged, 50)
        self.assertEqual(self.db.count_candles("bybit", "BTCUSDT", "1h"), 50)

    def test_2_overlapping_pages_do_not_inflate_bars_available(self):
        rows = bars(100)
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows[:60])
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", rows[40:])
        self.assertEqual(self.db.count_candles("bybit", "BTCUSDT", "1h"), 100)

    def test_3_the_legacy_backfill_source_is_gone_and_understood(self):
        # New writes use the venue source plus an ingestion mode.
        self.db.upsert_candles("bybit", "BTCUSDT", "1h", bars(3), source="venue_rest",
                               ingestion_mode="backfill")
        row = self.db.load_candles("bybit", "BTCUSDT", "1h")[0]
        self.assertEqual(row["source"], "venue_rest")
        self.assertEqual(row["ingestion_mode"], "backfill")
        self.assertIn("venue_rest", Database.SOURCE_RANKS)

    def test_5_every_data_family_has_its_own_snapshot_row(self):
        for kind, interval, version in (
            ("trade_candle", "1h", "history/1:a"), ("mark_candle", "1h", "series/1:b"),
            ("funding", "", "series/1:c"), ("open_interest", "", "series/1:d"),
        ):
            self.db.record_history_snapshot({
                "venue": "bybit", "symbol": "BTCUSDT", "interval": interval, "data_kind": kind,
                "version": version, "from_ts": 1, "to_ts": 2, "bars": 5,
            })
        rows = self.db.list_history_snapshots(symbol="BTCUSDT")
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["data_kind"] for row in rows},
                         {"trade_candle", "mark_candle", "funding", "open_interest"})

    def test_7_a_backtest_result_can_cite_every_input_version(self):
        from quantdesk.datahub.history import FUNDING, MARK_CANDLE, OPEN_INTEREST, series_version

        self.db.upsert_candles("bybit", "BTCUSDT", "1h", bars(10))
        self.db.upsert_mark_candles("bybit", "BTCUSDT", "1h",
                                    [{"ts": row["ts"], "open": 1, "high": 1, "low": 1, "close": 1}
                                     for row in bars(10)])
        self.db.upsert_funding("bybit", "BTCUSDT", [{"ts": FROM, "rate": 0.0001}])
        self.db.upsert_oi("bybit", "BTCUSDT", [{"ts": FROM, "oi": 1.0}])
        versions = {
            "marks": series_version(MARK_CANDLE, "bybit", "BTCUSDT",
                                    self.db.load_mark_candles("bybit", "BTCUSDT", "1h"), interval="1h"),
            "funding": series_version(FUNDING, "bybit", "BTCUSDT", self.db.load_funding("bybit", "BTCUSDT")),
            "openInterest": series_version(OPEN_INTEREST, "bybit", "BTCUSDT",
                                           self.db.load_oi("bybit", "BTCUSDT")),
        }
        for name, version in versions.items():
            self.assertTrue(version.startswith("series/1:"), name)
            self.assertNotEqual(version, versions.get("marks") if name != "marks" else "")


if __name__ == "__main__":
    unittest.main()
