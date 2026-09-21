"""Stage 6: campaign-level DSR, proposal-dimension PBO, and the one-shot Gate-C verdict.

What these tests are guarding, in order of how easy it would be to get wrong:

* the DSR must refuse to produce a number when there is no trial count, no spread or
  no sample length. A deflated Sharpe built on a guessed `N` looks exactly like a
  real one in a report, so the only defence is that the unavailable path is asserted;
* the PBO must be measured on proposals, must be about 0.5 for pure noise, and must
  fall to zero when one proposal really is better. The noise case is asserted as an
  ensemble (twelve fixed seeds) because a *single* CSCV estimate at this matrix size
  has a wide sampling spread - that spread is a property of the estimator, not a
  defect, and hiding it behind one lucky seed would be dishonest;
* the test segment must not leak before the seal is broken, through either the stats
  endpoint or the verdict endpoint, even when the test-window run is already sitting
  in `backtest_runs`;
* the verdict must be written once, read back identically, and must never turn
  "we could not compute it" into a pass.

Everything runs against a temporary `QUANTDESK_HOME` (the session fixture in
`conftest.py` fails a test that opens the operator's live database), with synthetic
bars and synthetic equity curves so no market data and no network are involved.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from quantdesk import campaign_stats as cs
from quantdesk import campaigns
from quantdesk.agent_campaign import annualisation as orchestrator_annualisation
from quantdesk.api.server import app
from quantdesk.campaigns import CampaignError
from quantdesk.datahub.db import Database

WINDOWS = {
    "train": [1_600_000_000_000, 1_650_000_000_000],
    "validation": [1_650_000_000_001, 1_680_000_000_000],
    "test": [1_680_000_000_001, 1_700_000_000_000],
}
GATES = ("coverage", "finiteness", "dispersion", "predictive", "persistence", "cost", "redundancy")
STEP_MS = 4 * 3600 * 1000  # the campaign interval used throughout: 4h bars
INTERVAL = "4h"


def write_library(
    home: Path,
    *,
    group: str = "stock",
    interval: str = INTERVAL,
    horizon: int = 24,
    factor_ids: tuple[str, ...] = ("vibe.macd.hist", "vibe.rsi.reversal"),
) -> None:
    """A gate report in the shape `factor_library` reads, without running a scan."""
    root = home / "factor-library"
    root.mkdir(parents=True, exist_ok=True)
    evidence = []
    for factor_id in factor_ids:
        evidence.append({
            "factorId": factor_id,
            "family": "quantdesk",
            "passed": True,
            "symbols": ["AAPLUSDT", "MSFTUSDT"],
            "gates": [
                {
                    "name": name,
                    "passed": True,
                    "detail": "",
                    "metric": (
                        {"ic": 0.05, "pValue": 0.002, "signConsistency": 0.7}
                        if name == "predictive"
                        else {"netBps": 16.2} if name == "cost" else {}
                    ),
                }
                for name in GATES
            ],
        })
    (root / f"{group}-{interval}-h{horizon}.json").write_text(
        json.dumps({
            "group": group, "interval": interval, "horizonBars": horizon,
            "generatedAt": "2026-09-16T00:00:00Z", "symbols": ["AAPLUSDT", "MSFTUSDT"],
            "evidence": evidence,
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def curve(start: int, end: int, *, drift: float, vol: float, seed: int) -> list[dict]:
    """An equity curve sampled on the campaign's bar grid inside one window."""
    generator = random.Random(seed)
    equity = 100_000.0
    points = [{"time": start, "equity": equity}]
    stamp = start
    while stamp + STEP_MS <= end:
        stamp += STEP_MS
        equity *= math.exp(generator.gauss(drift, vol))
        points.append({"time": stamp, "equity": equity})
    return points


def insert_run(db: Database, run_id: int, points: list[dict], *, bars: int | None = None) -> str:
    """A stored run whose result carries an equity curve and nothing else."""
    db.execute(
        "INSERT INTO backtest_runs (id, kind, status, request_json, result_json, summary_json, "
        " queued_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?)",
        (
            run_id, "backtest", "done", "{}",
            json.dumps({"equity_curve": points, "bars": bars or len(points)}, ensure_ascii=False),
            json.dumps({"bars": bars or len(points)}),
            1, 1,
        ),
    )
    return str(run_id)


def noise_series(proposals: int, bars: int, seed: int, *, vol: float = 0.01) -> dict[str, list[float]]:
    generator = random.Random(seed)
    return {f"p{index}": [generator.gauss(0.0, vol) for _ in range(bars)] for index in range(proposals)}


def signal_series(
    proposals: int, bars: int, seed: int, *, edge: float = 0.003, at: int = 0, vol: float = 0.01
) -> dict[str, list[float]]:
    generator = random.Random(seed)
    return {
        f"p{index}": [generator.gauss(edge if index == at else 0.0, vol) for _ in range(bars)]
        for index in range(proposals)
    }


class CampaignFixture(unittest.TestCase):
    """A temp home, a gated library, and a campaign with runs it can point at."""

    CRITERIA = "testSharpe >= 1.0 且 dsr >= 0.4"
    VALIDATION_SPECS = {
        "p0": {"drift": 0.00025, "vol": 0.01, "seed": 11},   # a real edge
        "p1": {"drift": 0.0, "vol": 0.01, "seed": 12},
        "p2": {"drift": 0.0, "vol": 0.01, "seed": 13},
        "p3": {"drift": 0.0, "vol": 0.01, "seed": 14},
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.db = Database(self.home / "quantdesk.db")
        write_library(self.home)
        self._previous_home = os.environ.get("QUANTDESK_HOME")
        os.environ["QUANTDESK_HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        self.uid = self.register()

    def _restore_home(self) -> None:
        if self._previous_home is None:
            os.environ.pop("QUANTDESK_HOME", None)
        else:
            os.environ["QUANTDESK_HOME"] = self._previous_home

    def register(self, **overrides) -> str:
        payload = {
            "group": "stock", "interval": INTERVAL, "horizon_bars": 24,
            "hypothesis": "MACD 柱在半导体的 4 小时线上有跨截面动量",
            "success_criteria": self.CRITERIA,
            "windows": WINDOWS, "provider": "vibe-backtest-lab", "home": self.home,
        }
        payload.update(overrides)
        return campaigns.preregister(self.db, **payload)["uid"]

    def propose(
        self, proposal_ids: tuple[str, ...] = ("p0", "p1", "p2", "p3"), *, round_number: int = 1
    ) -> None:
        campaigns.start_round(self.db, self.uid, round_number=round_number)
        campaigns.record_proposals(
            self.db, self.uid, round_number=round_number,
            proposals=[
                {
                    "proposalId": proposal_id,
                    "factorIds": ["vibe.macd.hist"],
                    "parameters": {"lookback": 12.0 + index},
                    "hypothesis": f"提案 {proposal_id} 的动量假设",
                }
                for index, proposal_id in enumerate(proposal_ids)
            ],
        )

    def validation_runs(self, *, run_base: int = 100) -> dict[str, dict]:
        """Store one validation run per proposal and record the trial that points at it."""
        existing = self.db.query(
            "SELECT COUNT(*) AS count FROM agent_proposals WHERE campaign_id="
            "(SELECT id FROM agent_campaigns WHERE campaign_uid=?)",
            (self.uid,),
        )[0]["count"]
        if not existing:
            self.propose()
        metrics: dict[str, dict] = {}
        for index, (proposal_id, spec) in enumerate(self.VALIDATION_SPECS.items()):
            points = curve(WINDOWS["validation"][0], WINDOWS["validation"][1], **spec)
            run_id = insert_run(self.db, run_base + index, points)
            measured = cs.segment_metrics(points, INTERVAL)
            metrics[proposal_id] = {**measured, "runId": run_id}
            campaigns.record_trial(
                self.db, self.uid, proposal_uid=proposal_id, segment="validation",
                run_id=run_id, sharpe=measured["sharpe"], return_pct=measured["returnPct"],
                max_drawdown_pct=measured["maxDrawdownPct"], trades=37,
            )
        return metrics

    def store_test_run(self, *, run_id: int = 9001, drift: float = 0.0004, seed: int = 42) -> tuple[str, dict]:
        points = curve(WINDOWS["test"][0], WINDOWS["test"][1], drift=drift, vol=0.01, seed=seed)
        stored = insert_run(self.db, run_id, points)
        return stored, cs.segment_metrics(points, INTERVAL)


# --------------------------------------------------------------------------------
# The statistics themselves
# --------------------------------------------------------------------------------


class DeflatedSharpeTests(unittest.TestCase):
    """DSR: the paper's formula, and the refusals that keep it honest."""

    TRIALS = [0.02 * index for index in range(1, 21)]  # twenty per-bar trial Sharpes

    def result(self, **overrides) -> dict:
        payload = {
            "observed_sharpe": 0.30,
            "trial_sharpes": self.TRIALS,
            "trials": 20,
            "sample_length": 500,
            "skew": 0.0,
            "kurtosis": 3.0,
        }
        payload.update(overrides)
        return cs.deflated_sharpe(**payload)

    def test_dsr_falls_as_the_trial_count_grows(self):
        numbers = [
            self.result(trials=count)["deflatedSharpe"] for count in (5, 20, 100, 400)
        ]
        for value in numbers:
            self.assertIsNotNone(value)
            self.assertGreater(value, 0.0)
            self.assertLess(value, 1.0)
        self.assertEqual(numbers, sorted(numbers, reverse=True))
        self.assertGreater(numbers[0], numbers[1])
        self.assertGreater(numbers[1], numbers[2])
        self.assertGreater(numbers[2], numbers[3])
        # Concrete numbers, not just "a float came back".
        self.assertAlmostEqual(numbers[0], 0.999741779, places=8)
        self.assertAlmostEqual(numbers[1], 0.949622298, places=8)
        self.assertAlmostEqual(numbers[2], 0.505012790, places=8)
        self.assertAlmostEqual(numbers[3], 0.122659098, places=8)

    def test_expected_maximum_sharpe_rises_with_the_trial_count(self):
        expected = [self.result(trials=count)["expectedMaxSharpe"] for count in (5, 20, 100, 400)]
        self.assertEqual(expected, sorted(expected))
        self.assertAlmostEqual(expected[0], 0.141109625, places=8)
        self.assertAlmostEqual(expected[3], 0.353166477, places=8)

    def test_the_expected_maximum_is_the_paper_expression(self):
        """SR0 = spread * [(1-g)*ppf(1-1/N) + g*ppf(1-1/(N*e))], checked by hand."""
        spread = cs._sample_stdev(self.TRIALS)
        count = 20
        by_hand = spread * (
            (1.0 - cs.EULER_MASCHERONI) * cs.normal_ppf(1.0 - 1.0 / count)
            + cs.EULER_MASCHERONI * cs.normal_ppf(1.0 - 1.0 / (count * math.e))
        )
        self.assertAlmostEqual(self.result()["expectedMaxSharpe"], by_hand, places=12)
        self.assertAlmostEqual(cs.normal_ppf(0.975), 1.959963985, places=8)

    def test_dsr_needs_n(self):
        for trials in (None, 0, 1):
            item = self.result(trials=trials)
            self.assertFalse(item["available"])
            self.assertIsNone(item["deflatedSharpe"])
            self.assertIsNone(item["expectedMaxSharpe"])
            self.assertIn("N", item["reason"])

    def test_dsr_needs_a_spread_of_trial_sharpes(self):
        for trial_sharpes in ([], [0.1], None):
            item = self.result(trial_sharpes=trial_sharpes)
            self.assertFalse(item["available"])
            self.assertIsNone(item["deflatedSharpe"])
            self.assertIn("离散度", item["reason"])
        item = self.result(trial_sharpes=[0.25, 0.25, 0.25])
        self.assertFalse(item["available"])
        self.assertIn("离散度为 0", item["reason"])

    def test_dsr_needs_the_observed_sharpe_and_a_sample_length(self):
        missing_observed = self.result(observed_sharpe=None)
        self.assertFalse(missing_observed["available"])
        self.assertIn("Sharpe", missing_observed["reason"])
        missing_length = self.result(sample_length=None)
        self.assertFalse(missing_length["available"])
        self.assertIn("T", missing_length["reason"])
        little = self.result(sample_length=1)
        self.assertFalse(little["available"])

    def test_fat_tails_lower_the_deflated_sharpe(self):
        normal = self.result(kurtosis=3.0)["deflatedSharpe"]
        fat = self.result(kurtosis=9.0)["deflatedSharpe"]
        self.assertLess(fat, normal)
        self.assertAlmostEqual(normal, 0.949622298, places=8)
        self.assertAlmostEqual(fat, 0.938762663, places=8)

    def test_normal_moments_are_what_a_missing_curve_falls_back_to(self):
        given = self.result(skew=0.0, kurtosis=3.0)
        fallback = self.result(skew=None, kurtosis=None)
        self.assertAlmostEqual(given["deflatedSharpe"], fallback["deflatedSharpe"], places=12)
        self.assertEqual(fallback["kurtosis"], 3.0)

    def test_kurtosis_is_not_in_excess(self):
        generator = random.Random(5)
        sample = [generator.gauss(0.0, 1.0) for _ in range(20_000)]
        self.assertAlmostEqual(cs._kurtosis(sample), 3.0, places=1)
        self.assertAlmostEqual(cs._skewness(sample), 0.0, places=1)
        self.assertLess(cs._kurtosis(sample), 3.5)

    def test_the_plugin_uses_the_excess_convention_and_says_so(self):
        """The frozen `vibe-factors` helper is excess kurtosis: our gamma4 is +3."""
        plugin = load_reference_plugin()
        if plugin is None:  # pragma: no cover - the plugin ships with the repo
            self.skipTest("vibe-factors plugin is not present")
        generator = random.Random(9)
        sample = [generator.gauss(0.0, 1.0) for _ in range(20_000)]
        self.assertAlmostEqual(cs._kurtosis(sample) - 3.0, plugin._kurtosis(sample), places=2)
        # ... and inside the variance term that is a different number, which is why the
        # engine does not reuse the plugin's convention.
        excess_term = (plugin._kurtosis(sample) - 1.0) / 4.0
        our_term = (cs._kurtosis(sample) - 1.0) / 4.0
        self.assertAlmostEqual(our_term - excess_term, 0.75, places=1)


class ProposalPboTests(unittest.TestCase):
    """CSCV at the proposal dimension: noise ~0.5, a real edge ~0, a trap ~1."""

    def test_pure_noise_averages_about_one_half(self):
        values = [
            cs.proposal_pbo(noise_series(12, 600, seed), blocks=8)["pbo"]
            for seed in range(1, 25)
        ]
        for value in values:
            self.assertIsNotNone(value)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        mean = sum(values) / len(values)
        # A single noise matrix is a wide estimate (the twelve-seed subset below ranges
        # from 0.31 to 1.0); the ensemble is the honest statement about the estimator.
        self.assertAlmostEqual(mean, 0.459524, places=5)
        self.assertLess(abs(mean - 0.5), 0.06)
        self.assertAlmostEqual(
            [round(value, 4) for value in values[:3]], [0.3857, 0.3143, 0.7], places=4
        )

    def test_a_real_edge_drives_pbo_to_zero(self):
        for position in (0, 5, 11):
            for seed in (1, 2, 3):
                item = cs.proposal_pbo(signal_series(12, 600, seed, at=position), blocks=8)
                self.assertTrue(item["available"])
                self.assertEqual(item["pbo"], 0.0)
                self.assertIn(f"p{position}", item["selectionFrequency"])
                self.assertEqual(item["selectionFrequency"][f"p{position}"], 1.0)
                self.assertGreater(item["medianOosRank"], 0.9)

    def test_a_strategy_that_only_worked_in_sample_is_flagged(self):
        """Good in the first half of the timeline, bad in the second: PBO = 1."""
        blocks, per_block = 8, 75
        series = {f"p{index}": [0.0] * (blocks * per_block) for index in range(4)}
        pattern = [0.02] * 4 + [-0.02] * 4
        series["p0"] = [pattern[index // per_block] for index in range(blocks * per_block)]
        item = cs.proposal_pbo(series, blocks=8)
        self.assertTrue(item["available"])
        self.assertEqual(item["pbo"], 1.0)
        self.assertEqual(item["combinations"], 70)

    def test_it_says_it_is_the_proposal_dimension(self):
        item = cs.proposal_pbo(noise_series(4, 320, 3), blocks=8)
        self.assertEqual(item["method"], "cscv-proposal-dimension")
        self.assertIn("提案维度", item["note"])
        self.assertIn("不是某个策略的参数网格", item["note"])
        self.assertNotIn("parameter", item["method"])

    def test_proposals_without_a_usable_series_are_counted_not_dropped(self):
        series = noise_series(4, 320, 4)
        series["p1"] = [0.0] * 3  # too short to say anything
        series["p2"] = [0.01] * 200 + [float("nan")] * 120
        item = cs.proposal_pbo(series, blocks=8)
        self.assertTrue(item["available"])
        self.assertEqual(item["proposals"], 2)
        self.assertEqual(set(item["skipped"]), {"p1", "p2"})
        self.assertIn("少于", item["skipped"]["p1"])
        self.assertIn("非有限值", item["skipped"]["p2"])

    def test_one_proposal_is_not_a_distribution(self):
        item = cs.proposal_pbo(noise_series(1, 320, 5))
        self.assertFalse(item["available"])
        self.assertIsNone(item["pbo"])
        self.assertIn("至少需要 2 个", item["reason"])
        item = cs.proposal_pbo({"p0": [0.01] * 8})
        self.assertFalse(item["available"])

    def test_it_matches_the_reference_cscv_in_the_factor_plugin(self):
        """The engine's PBO and the frozen plugin's CSCV agree on the same matrix."""
        plugin = load_reference_plugin()
        if plugin is None:  # pragma: no cover - the plugin ships with the repo
            self.skipTest("vibe-factors plugin is not present")
        series = noise_series(8, 480, 17)
        blocks = 8
        per_block = 480 // blocks
        matrix = [
            [sum(series[key][index * per_block:(index + 1) * per_block]) / per_block
             for index in range(blocks)]
            for key in sorted(series)
        ]
        reference = plugin.probability_of_overfitting(
            matrix, max_combinations=252, rng=random.Random(0)
        )
        mine = cs.proposal_pbo(series, blocks=blocks)
        self.assertTrue(reference["applied"])
        self.assertEqual(reference["combinations"], mine["combinations"])
        self.assertAlmostEqual(reference["probability"], mine["pbo"], places=12)


class AnnualisationParityTests(unittest.TestCase):
    def test_the_engine_and_the_orchestrator_agree_on_bars_per_year(self):
        for interval in ("15m", "1h", "4h", "1d", "3m"):
            self.assertEqual(cs.annualisation(interval), orchestrator_annualisation(interval))


class CriteriaTests(unittest.TestCase):
    """The pre-registered sentence is the contract; it must be read, not guessed."""

    EVIDENCE = {
        "segments": {
            "train": {"available": True, "sharpe": 0.4, "returnPct": 1.0,
                      "maxDrawdownPct": -9.0, "trades": 12, "source": "x"},
            "validation": {"available": True, "sharpe": 1.6, "returnPct": 7.5,
                           "maxDrawdownPct": -6.0, "trades": 31, "source": "x"},
            "test": {"available": True, "sharpe": 1.9, "returnPct": 9.9,
                     "maxDrawdownPct": -5.0, "trades": 44, "source": "x"},
        },
        "stats": {"trials": 12, "deflatedSharpe": 0.83, "pbo": 0.14},
    }

    def test_json_like_clauses_are_parsed(self):
        parsed = cs.parse_criteria("testSharpe>=1.0; pbo<=0.5; trials>=8")
        self.assertEqual(parsed["recognisedCount"], 3)
        self.assertEqual([item["metric"] for item in parsed["clauses"]],
                         ["sharpe", "pbo", "trials"])
        self.assertEqual([item["operator"] for item in parsed["clauses"]], [">=", "<=", ">="])
        self.assertEqual([item["segment"] for item in parsed["clauses"]], ["test", None, None])

    def test_chinese_prose_is_parsed_with_its_segment(self):
        parsed = cs.parse_criteria("验证段净收益 > 0 且 DSR > 0")
        self.assertEqual(parsed["unrecognised"], [])
        self.assertEqual(parsed["clauses"][0]["metric"], "returnPct")
        self.assertEqual(parsed["clauses"][0]["segment"], "validation")
        self.assertEqual(parsed["clauses"][1]["metric"], "deflatedSharpe")
        outcome = cs.evaluate_criteria(parsed, self.EVIDENCE, judged_segment="test")
        self.assertEqual(outcome["verdict"], "pass")
        self.assertEqual(len(outcome["outcomes"]), 2)

    def test_every_clause_must_hold(self):
        parsed = cs.parse_criteria("testSharpe >= 2.5 且 pbo <= 0.5")
        outcome = cs.evaluate_criteria(parsed, self.EVIDENCE, judged_segment="test")
        self.assertEqual(outcome["verdict"], "fail")
        self.assertEqual(outcome["failed"], ["testSharpe >= 2.5"])
        self.assertIn("1.9", outcome["reason"])

    def test_an_uncomputable_clause_is_inconclusive_never_a_pass(self):
        parsed = cs.parse_criteria("testSharpe >= 0.1 且 dsr >= 0.9")
        evidence = json.loads(json.dumps(self.EVIDENCE))
        evidence["stats"]["deflatedSharpe"] = None
        outcome = cs.evaluate_criteria(parsed, evidence, judged_segment="test")
        self.assertEqual(outcome["verdict"], "inconclusive")
        self.assertEqual(outcome["unevaluable"], ["dsr >= 0.9"])
        self.assertIn("DSR 不可用", outcome["reason"])

    def test_prose_without_numbers_is_inconclusive(self):
        parsed = cs.parse_criteria("表现要稳健，回撤要能接受")
        self.assertEqual(parsed["clauses"], [])
        self.assertEqual(len(parsed["unrecognised"]), 2)
        outcome = cs.evaluate_criteria(parsed, self.EVIDENCE, judged_segment="test")
        self.assertEqual(outcome["verdict"], "inconclusive")
        self.assertIn("没有可判定的数值条款", outcome["reason"])

    def test_a_failing_clause_outranks_an_unparseable_one(self):
        parsed = cs.parse_criteria("testSharpe >= 5.0 且 要稳健")
        outcome = cs.evaluate_criteria(parsed, self.EVIDENCE, judged_segment="test")
        self.assertEqual(outcome["verdict"], "fail")
        self.assertEqual(outcome["unrecognised"], ["要稳健"])


# --------------------------------------------------------------------------------
# Campaign-level statistics over stored runs
# --------------------------------------------------------------------------------


class CampaignStatsTests(CampaignFixture):
    def test_sealed_stats_use_only_the_validation_segment(self):
        metrics = self.validation_runs()
        # The test-window run already exists in the store; the seal, not the data, is
        # what keeps it out of these numbers.
        test_run, test_metrics = self.store_test_run()
        stats = cs.campaign_stats(self.db, self.uid)

        self.assertEqual(stats["observed"]["segment"], "validation")
        self.assertEqual(stats["selectedProposal"]["proposalId"], "p0")
        self.assertEqual(stats["selectedProposal"]["runId"], "100")
        self.assertAlmostEqual(
            stats["observed"]["sharpe"], metrics["p0"]["sharpe"], places=12
        )
        self.assertEqual(stats["trials"], 4)
        self.assertEqual(stats["trialsRecorded"], 4)
        self.assertEqual(stats["visibility"]["segments"], ["validation"])
        self.assertEqual(stats["visibility"]["testTrialsRecorded"], 0)

        # No test-segment value, run id or window boundary anywhere in the payload.
        payload = json.dumps(stats, ensure_ascii=False, default=str)
        self.assertNotIn(test_run, payload)
        self.assertNotIn(str(WINDOWS["test"][1]), payload)
        self.assertNotIn(f"{test_metrics['sharpe']:.6f}", payload)
        self.assertNotIn(f"{test_metrics['returnPct']:.6f}", payload)

    def test_sealed_stats_refuse_to_include_the_test_segment(self):
        self.validation_runs()
        with self.assertRaises(CampaignError) as raised:
            cs.campaign_stats(self.db, self.uid, include_test=True)
        self.assertEqual(raised.exception.status, 409)
        self.assertIn("开封", str(raised.exception))
        with self.assertRaises(CampaignError) as observed:
            cs.campaign_stats(self.db, self.uid, observed_segment="test")
        self.assertEqual(observed.exception.status, 409)

    def test_the_numbers_are_concrete(self):
        metrics = self.validation_runs()
        stats = cs.campaign_stats(self.db, self.uid)
        deflated = stats["deflatedSharpe"]
        pbo = stats["pbo"]

        self.assertAlmostEqual(metrics["p0"]["sharpe"], 2.215394016, places=8)
        self.assertAlmostEqual(metrics["p1"]["sharpe"], 1.653416324, places=8)
        self.assertAlmostEqual(metrics["p2"]["sharpe"], -0.364084957, places=8)
        self.assertTrue(deflated["available"])
        self.assertEqual(deflated["trials"], 4)
        self.assertAlmostEqual(deflated["sharpeSpread"], 0.023750734, places=8)
        self.assertAlmostEqual(deflated["expectedMaxSharpe"], 0.024988689, places=8)
        self.assertAlmostEqual(deflated["deflatedSharpe"], 0.846506652, places=8)
        self.assertEqual(deflated["sampleLength"], 2083)
        self.assertEqual(deflated["method"], "bailey-lopez-de-prado-2014-dsr")
        self.assertEqual(deflated["approximations"], [])
        self.assertIn("N=搜索阶段的尝试数", deflated["trialsBasis"])

        self.assertTrue(pbo["available"])
        # Four proposals where three are lucky noise: the validation edge does not
        # survive the block split, and the number says so.
        self.assertAlmostEqual(pbo["pbo"], 0.671428571, places=8)
        self.assertEqual(pbo["blocks"], 8)
        self.assertEqual(pbo["combinations"], 70)
        self.assertEqual(pbo["method"], "cscv-proposal-dimension")
        self.assertEqual(pbo["segment"], "validation")
        self.assertEqual(pbo["skipped"], {})
        self.assertAlmostEqual(pbo["selectionFrequency"]["p0"], 0.514285714, places=8)

    def test_a_trial_without_a_run_falls_back_and_says_so(self):
        """No stored curve: the Sharpe is known, the moments are not -> declared."""
        campaigns.start_round(self.db, self.uid, round_number=1)
        campaigns.record_proposals(self.db, self.uid, round_number=1, proposals=[
            {"proposalId": f"p{index}", "factorIds": ["vibe.macd.hist"],
             "hypothesis": f"h{index}"} for index in range(2)
        ])
        campaigns.record_trial(self.db, self.uid, proposal_uid="p0", segment="validation",
                               sharpe=1.4, return_pct=4.0, max_drawdown_pct=-3.0, trades=9)
        campaigns.record_trial(self.db, self.uid, proposal_uid="p1", segment="validation",
                               sharpe=0.6, return_pct=1.0, max_drawdown_pct=-5.0, trades=11)
        stats = cs.campaign_stats(self.db, self.uid)

        self.assertEqual(stats["observed"]["source"], "trial-record")
        self.assertIn("moments=normal-approximation", stats["approximations"])
        self.assertIn("sampleLength=window-approximation", stats["approximations"])
        self.assertEqual(stats["deflatedSharpe"]["kurtosis"], 3.0)
        self.assertEqual(stats["deflatedSharpe"]["method"],
                         "bailey-lopez-de-prado-2014-dsr+normal-moments+window-T")
        self.assertEqual(stats["deflatedSharpe"]["sampleLength"], 2083)
        self.assertTrue(stats["deflatedSharpe"]["available"])
        self.assertFalse(stats["pbo"]["available"])
        self.assertIn("至少需要 2 个", stats["pbo"]["reason"])

    def test_without_a_trial_count_there_is_no_dsr(self):
        campaigns.start_round(self.db, self.uid, round_number=1)
        campaigns.record_proposals(self.db, self.uid, round_number=1, proposals=[
            {"proposalId": "p0", "factorIds": ["vibe.macd.hist"], "hypothesis": "h"}
        ])
        campaigns.record_trial(self.db, self.uid, proposal_uid="p0", segment="validation",
                               sharpe=1.2, return_pct=3.0, max_drawdown_pct=-2.0, trades=4)
        stats = cs.campaign_stats(self.db, self.uid)
        self.assertFalse(stats["deflatedSharpe"]["available"])
        self.assertIsNone(stats["deflatedSharpe"]["deflatedSharpe"])
        self.assertIn("N", stats["deflatedSharpe"]["reason"])

    def test_curves_are_read_from_the_stored_run_and_differenced(self):
        metrics = self.validation_runs()
        curve_points, source = cs.stored_equity(self.db, "100")
        self.assertEqual(source, "backtest_runs.result_json")
        self.assertEqual(len(curve_points), 2084)
        returns, _ = cs.returns_of(curve_points)
        self.assertEqual(len(returns), 2083)
        self.assertAlmostEqual(
            cs.segment_metrics(curve_points, INTERVAL)["sharpe"], metrics["p0"]["sharpe"], places=9
        )

    def test_a_missing_run_is_a_reason_not_an_exception(self):
        points, source = cs.stored_equity(self.db, "4242")
        self.assertEqual(points, [])
        self.assertIn("不在 backtest_runs 里", source)

    def test_the_equity_artifact_is_the_fallback(self):
        points = curve(WINDOWS["validation"][0], WINDOWS["validation"][1],
                       drift=0.0, vol=0.01, seed=31)
        self.db.execute(
            "INSERT INTO backtest_runs (id, kind, status, request_json, result_json, "
            " queued_ts, updated_ts) VALUES (?,?,?,?,?,?,?)",
            (555, "backtest", "done", "{}", json.dumps({"metrics": {}}), 1, 1),
        )
        self.db.execute(
            "INSERT INTO backtest_artifacts (run_id, name, media_type, bytes, sha256, payload, "
            " created_ts) VALUES (?,?,?,?,?,?,?)",
            (555, "equity", "application/json", 0, "x",
             json.dumps({"equity_curve": points}), 1),
        )
        read, source = cs.stored_equity(self.db, "555")
        self.assertEqual(source, "backtest_artifacts.equity")
        self.assertEqual(len(read), len(points))


# --------------------------------------------------------------------------------
# Gate-C: unseal once, judge once, store the verdict
# --------------------------------------------------------------------------------


class GateCVerdictTests(CampaignFixture):
    def finish(self, status: str = "completed") -> None:
        campaigns.finish(self.db, self.uid, status=status, reason="预算用完")

    def test_pass_is_written_and_read_back(self):
        metrics = self.validation_runs()
        test_run, test_metrics = self.store_test_run()
        self.finish()
        verdict = cs.adjudicate(self.db, self.uid, approved_by="operator@desk",
                                proposal_uid="p0", run_id=test_run)

        self.assertEqual(verdict["verdict"], "pass")
        self.assertEqual(verdict["proposalId"], "p0")
        self.assertEqual(verdict["segment"], "test")
        self.assertEqual(verdict["criteria"], self.CRITERIA)
        self.assertEqual(verdict["approvedBy"], "operator@desk")
        self.assertEqual(verdict["window"], WINDOWS["test"])
        self.assertEqual(verdict["horizonBars"], 24)
        self.assertEqual(verdict["group"], "stock")
        self.assertEqual(verdict["interval"], "4h")
        self.assertAlmostEqual(verdict["sharpe"], test_metrics["sharpe"], places=9)
        self.assertAlmostEqual(verdict["returnPct"], test_metrics["returnPct"], places=9)
        self.assertAlmostEqual(verdict["maxDrawdownPct"], test_metrics["maxDrawdownPct"], places=9)
        self.assertTrue(verdict["deflatedSharpe"] is not None)
        self.assertAlmostEqual(verdict["pbo"], 0.671428571, places=8)
        self.assertEqual(verdict["trials"], 4)
        self.assertFalse(verdict["idempotent"])

        # Concrete OOS numbers, and the pre-registered sentence quoted verbatim.
        self.assertAlmostEqual(test_metrics["sharpe"], 1.206082052, places=8)
        self.assertAlmostEqual(test_metrics["returnPct"], 33.332589, places=5)
        self.assertAlmostEqual(test_metrics["maxDrawdownPct"], -38.468145, places=5)
        # The DSR at the gate deflates the *test* Sharpe (1.2061) by the effort the
        # search spent, not the validation one: 0.5117, still above the pre-registered
        # floor of 0.4.
        self.assertAlmostEqual(verdict["deflatedSharpe"], 0.511658401, places=8)
        self.assertAlmostEqual(verdict["pbo"], 0.671428571, places=8)
        self.assertIn(self.CRITERIA, verdict["evidence"]["criteria"]["sentence"])
        self.assertIn("测试段窗口", verdict["evidence"]["criteria"]["sentence"])
        self.assertEqual(verdict["evidence"]["criteria"]["outcome"]["verdict"], "pass")
        self.assertEqual(
            [item["status"] for item in verdict["evidence"]["criteria"]["outcome"]["outcomes"]],
            ["pass", "pass"],
        )
        self.assertEqual(verdict["evidence"]["judged"]["runId"], test_run)
        self.assertEqual(verdict["evidence"]["segments"]["validation"]["source"],
                         "backtest_runs.result_json")
        self.assertAlmostEqual(
            verdict["evidence"]["segments"]["validation"]["sharpe"],
            metrics["p0"]["sharpe"], places=9,
        )
        self.assertEqual(verdict["evidence"]["stats"]["method"]["pbo"], "cscv-proposal-dimension")

        stored = cs.stored_verdict(self.db, self.uid)
        self.assertEqual(stored["verdict"], "pass")
        self.assertEqual(stored["createdTs"], verdict["createdTs"])
        rows = self.db.query("SELECT * FROM agent_verdicts")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "pass")
        self.assertEqual(rows[0]["campaign_uid"], self.uid)
        self.assertEqual(rows[0]["segment"], "test")
        self.assertEqual(rows[0]["criteria"], self.CRITERIA)

    def test_the_verdict_deflates_the_test_sharpe_not_the_validation_one(self):
        self.validation_runs()
        test_run, test_metrics = self.store_test_run()
        self.finish()
        sealed = cs.campaign_stats(self.db, self.uid)["deflatedSharpe"]
        verdict = cs.adjudicate(self.db, self.uid, approved_by="operator@desk",
                                proposal_uid="p0", run_id=test_run)

        points, _ = cs.stored_equity(self.db, test_run)
        returns, _ = cs.returns_of(cs.curve_in_window(points, WINDOWS["test"]))
        trial_sharpes = [
            row["sharpe"] / math.sqrt(cs.annualisation(INTERVAL))
            for row in self.db.query(
                "SELECT sharpe FROM agent_trials WHERE segment IN ('train','validation') ORDER BY id")
            if row["sharpe"] is not None
        ]
        by_hand = cs.deflated_sharpe(
            observed_sharpe=cs.per_bar_sharpe(returns),
            trial_sharpes=trial_sharpes,
            trials=4,
            sample_length=len(returns),
            skew=cs._skewness(returns),
            kurtosis=cs._kurtosis(returns),
            annualisation_factor=cs.annualisation(INTERVAL),
        )
        self.assertAlmostEqual(verdict["deflatedSharpe"], by_hand["deflatedSharpe"], places=12)
        # The number moved because the observation moved: the gate deflates the
        # out-of-sample Sharpe, not the validation one that picked the proposal.
        self.assertNotAlmostEqual(verdict["deflatedSharpe"], sealed["deflatedSharpe"], places=3)
        self.assertAlmostEqual(sealed["deflatedSharpe"], 0.846506652, places=8)
        self.assertAlmostEqual(verdict["deflatedSharpe"], 0.511658401, places=8)
        self.assertAlmostEqual(test_metrics["sharpe"], 1.206082052, places=8)

    def test_stats_never_report_a_test_sharpe_as_the_validation_one(self):
        """A campaign with no validation trial must not relabel the test segment."""
        campaigns.start_round(self.db, self.uid, round_number=1)
        campaigns.record_proposals(self.db, self.uid, round_number=1, proposals=[
            {"proposalId": "p0", "factorIds": ["vibe.macd.hist"], "hypothesis": "h"}
        ])
        campaigns.record_trial(self.db, self.uid, proposal_uid="p0", segment="train",
                               verdict="no_evidence")
        campaigns.unseal_test(self.db, self.uid, approved_by="operator@desk")
        test_run, test_metrics = self.store_test_run()
        campaigns.record_trial(self.db, self.uid, proposal_uid="p0", segment="test",
                               run_id=test_run, sharpe=test_metrics["sharpe"],
                               return_pct=test_metrics["returnPct"],
                               max_drawdown_pct=test_metrics["maxDrawdownPct"], trades=3)

        default_view = cs.campaign_stats(self.db, self.uid, include_test=True)
        self.assertFalse(default_view["observed"]["available"])
        self.assertNotIn(f"{test_metrics['sharpe']:.6f}",
                         json.dumps(default_view, ensure_ascii=False, default=str))
        self.assertFalse(default_view["deflatedSharpe"]["available"])

        asked = cs.campaign_stats(self.db, self.uid, include_test=True, observed_segment="test")
        self.assertTrue(asked["observed"]["available"])
        self.assertEqual(asked["observed"]["segment"], "test")
        self.assertAlmostEqual(asked["observed"]["sharpe"], test_metrics["sharpe"], places=9)

    def test_the_verdict_is_idempotent_and_does_not_rerun_the_test_segment(self):
        self.validation_runs()
        test_run, _ = self.store_test_run()
        self.finish()
        first = cs.adjudicate(self.db, self.uid, approved_by="operator@desk",
                              proposal_uid="p0", run_id=test_run)
        reads: list[str] = []
        original = cs.stored_equity

        def counted(db, run_id):
            reads.append(str(run_id))
            return original(db, run_id)

        with patch.object(cs, "stored_equity", counted):
            second = cs.adjudicate(self.db, self.uid, approved_by="someone-else",
                                   proposal_uid="p0", run_id=test_run)
        self.assertEqual(reads, [])
        self.assertTrue(second["idempotent"])
        self.assertTrue(second["alreadyJudged"])
        self.assertEqual(second["createdTs"], first["createdTs"])
        self.assertEqual(second["verdict"], first["verdict"])
        self.assertEqual(second["approvedBy"], "operator@desk")
        self.assertEqual(second["criteria"], first["criteria"])
        self.assertEqual(
            self.db.query("SELECT COUNT(*) AS count FROM agent_verdicts")[0]["count"], 1
        )
        # The window is not reopened either: `unseal_test` would refuse a second time.
        with self.assertRaises(CampaignError):
            campaigns.unseal_test(self.db, self.uid, approved_by="operator@desk")

    def test_the_test_segment_is_judged_even_if_the_run_disappears(self):
        self.validation_runs()
        test_run, _ = self.store_test_run()
        self.finish()
        first = cs.adjudicate(self.db, self.uid, approved_by="operator@desk",
                              proposal_uid="p0", run_id=test_run)
        self.db.execute("DELETE FROM backtest_runs WHERE id=?", (int(test_run),))
        second = cs.adjudicate(self.db, self.uid, approved_by="operator@desk",
                               proposal_uid="p0", run_id=test_run)
        self.assertEqual(second["verdict"], first["verdict"])
        self.assertEqual(second["createdTs"], first["createdTs"])
        self.assertAlmostEqual(second["sharpe"], first["sharpe"], places=12)

    def test_a_failed_campaign_is_recorded_as_a_failure(self):
        self.uid = self.register(success_criteria="testSharpe >= 99 且 pbo <= 0.5")
        self.validation_runs()
        test_run, _ = self.store_test_run()
        self.finish(status="budget_limited")
        verdict = cs.adjudicate(self.db, self.uid, approved_by="operator@desk",
                                proposal_uid="p0", run_id=test_run)
        self.assertEqual(verdict["verdict"], "fail")
        self.assertIn("testSharpe >= 99", verdict["reason"])
        self.assertEqual(verdict["evidence"]["criteria"]["criteriaVerdict"], "fail")
        stored = cs.stored_verdict(self.db, self.uid)
        self.assertEqual(stored["verdict"], "fail")
        self.assertEqual(verdict["evidence"]["criteria"]["outcome"]["failed"],
                         ["testSharpe >= 99", "pbo <= 0.5"])
        # A recorded failure is a result, not an error state on the campaign.
        self.assertEqual(campaigns.get_campaign(self.db, self.uid)["status"], "budget_limited")

    def test_missing_test_evidence_is_inconclusive_not_a_pass(self):
        self.uid = self.register(success_criteria="testSharpe >= -99")
        self.validation_runs()
        self.finish()
        verdict = cs.adjudicate(self.db, self.uid, approved_by="operator@desk", proposal_uid="p0")
        self.assertEqual(verdict["verdict"], "inconclusive")
        self.assertIn("证据不足", verdict["reason"])
        self.assertIsNone(verdict["sharpe"])
        self.assertIsNone(verdict["runId"])
        self.assertEqual(cs.stored_verdict(self.db, self.uid)["verdict"], "inconclusive")

    def test_a_high_pbo_alone_fails_the_campaign(self):
        """The verdict is decided by the numbers, including the PBO it publishes."""
        self.uid = self.register(success_criteria="pbo <= 0.5")
        self.validation_runs()
        test_run, _ = self.store_test_run()
        self.finish()
        verdict = cs.adjudicate(self.db, self.uid, approved_by="operator@desk",
                                proposal_uid="p0", run_id=test_run)
        self.assertEqual(verdict["verdict"], "fail")
        self.assertAlmostEqual(verdict["pbo"], 0.671428571, places=8)
        self.assertIn("0.671429", verdict["reason"])
        self.assertIn("pbo <= 0.5", verdict["reason"])

    def test_more_trials_lower_the_campaign_dsr(self):
        """N comes from the campaign's own trial sheet, not from a parameter."""
        self.validation_runs()
        before = cs.campaign_stats(self.db, self.uid)["deflatedSharpe"]
        # Two more proposals with the same edge as p0: the observed Sharpe does not
        # move, so only the trial count and the spread can move the deflated number.
        self.propose(("p4", "p5"), round_number=2)
        for index, proposal_id in enumerate(("p4", "p5")):
            points = curve(WINDOWS["validation"][0], WINDOWS["validation"][1],
                           **self.VALIDATION_SPECS["p0"])
            run_id = insert_run(self.db, 500 + index, points)
            measured = cs.segment_metrics(points, INTERVAL)
            campaigns.record_trial(
                self.db, self.uid, proposal_uid=proposal_id, segment="validation",
                run_id=run_id, sharpe=measured["sharpe"], return_pct=measured["returnPct"],
                max_drawdown_pct=measured["maxDrawdownPct"], trades=37,
            )
        after = cs.campaign_stats(self.db, self.uid)["deflatedSharpe"]
        self.assertEqual(before["trials"], 4)
        self.assertEqual(after["trials"], 6)
        self.assertAlmostEqual(before["observedSharpe"], after["observedSharpe"], places=12)
        self.assertAlmostEqual(before["deflatedSharpe"], 0.846506652, places=8)
        self.assertAlmostEqual(after["deflatedSharpe"], 0.812926328, places=8)
        self.assertLess(after["deflatedSharpe"], before["deflatedSharpe"])

    def test_a_recorded_test_trial_is_used_when_no_run_is_given(self):
        self.validation_runs()
        test_run, test_metrics = self.store_test_run()
        # A still-running campaign may record the one test measurement, and the
        # judgement then reads it instead of asking for a run id.
        campaigns.unseal_test(self.db, self.uid, approved_by="operator@desk")
        campaigns.record_trial(self.db, self.uid, proposal_uid="p0", segment="test",
                               run_id=test_run, sharpe=test_metrics["sharpe"],
                               return_pct=test_metrics["returnPct"],
                               max_drawdown_pct=test_metrics["maxDrawdownPct"], trades=52)
        verdict = cs.adjudicate(self.db, self.uid, approved_by="operator@desk")
        self.assertEqual(verdict["proposalId"], "p0")
        self.assertEqual(verdict["verdict"], "pass")
        self.assertEqual(verdict["runId"], test_run)
        self.assertAlmostEqual(verdict["sharpe"], test_metrics["sharpe"], places=9)
        self.assertEqual(verdict["trades"], 52)

    def test_an_unparseable_criteria_string_cannot_pass(self):
        self.uid = self.register(success_criteria="表现要稳健")
        self.validation_runs()
        test_run, _ = self.store_test_run()
        self.finish()
        verdict = cs.adjudicate(self.db, self.uid, approved_by="operator@desk",
                                proposal_uid="p0", run_id=test_run)
        self.assertEqual(verdict["verdict"], "inconclusive")
        self.assertIn("没有可判定的数值条款", verdict["reason"])

    def test_a_campaign_without_validation_evidence_is_not_unsealed(self):
        self.propose()
        self.finish()
        with self.assertRaises(CampaignError) as raised:
            cs.adjudicate(self.db, self.uid, approved_by="operator@desk")
        self.assertEqual(raised.exception.status, 409)
        self.assertIn("验证段证据", str(raised.exception))
        self.assertIsNone(campaigns.get_campaign(self.db, self.uid)["testUnsealedTs"])
        self.assertEqual(cs.stored_verdict(self.db, self.uid), None)

    def test_an_unknown_proposal_is_refused(self):
        self.validation_runs()
        self.finish()
        with self.assertRaises(CampaignError) as raised:
            cs.adjudicate(self.db, self.uid, approved_by="operator@desk", proposal_uid="nope")
        self.assertEqual(raised.exception.status, 404)

    def test_an_unattributed_unsealing_is_refused(self):
        self.validation_runs()
        self.finish()
        with self.assertRaises(CampaignError) as raised:
            cs.adjudicate(self.db, self.uid, approved_by="   ")
        self.assertEqual(raised.exception.status, 422)

    def test_the_campaign_trial_sheet_gains_the_test_measurement(self):
        self.validation_runs()
        test_run, _ = self.store_test_run()
        # A campaign that is still running (not finished) takes the one test trial, so
        # the campaign's own sheet shows the out-of-sample measurement.
        verdict = cs.adjudicate(self.db, self.uid, approved_by="operator@desk",
                                proposal_uid="p0", run_id=test_run)
        self.assertEqual(verdict["verdict"], "pass")
        trials = campaigns.trials_for(self.db, self.uid, include_test=True)
        test_trials = [item for item in trials if item["segment"] == "test"]
        self.assertEqual(len(test_trials), 1)
        self.assertEqual(test_trials[0]["proposalId"], "p0")
        self.assertEqual(test_trials[0]["verdict"], "pass")
        # N still counts the search attempts, not the final measurement.
        stats = cs.campaign_stats(self.db, self.uid, include_test=True)
        self.assertEqual(stats["trials"], 4)
        self.assertEqual(stats["trialsRecorded"], 5)
        self.assertEqual(stats["visibility"]["testTrialsRecorded"], 1)
        self.assertEqual(stats["deflatedSharpe"]["trials"], 4)

    def test_a_finished_campaign_records_the_out_of_sample_measurement_once(self):
        """The search may not add trials after it ends; the test measurement is not the search.

        It happens after the search by design, exactly once, so it *is* recorded - the
        campaign's ledger should show the one out-of-sample run that the verdict rests
        on. What stays forbidden is a second one, and the search adding more.
        """
        self.validation_runs()
        test_run, _ = self.store_test_run()
        self.finish()
        verdict = cs.adjudicate(self.db, self.uid, approved_by="operator@desk",
                                proposal_uid="p0", run_id=test_run)
        self.assertEqual(verdict["verdict"], "pass")
        stats = cs.campaign_stats(self.db, self.uid, include_test=True)
        self.assertEqual(stats["trialsRecorded"], 5, "4 条搜索试验 + 1 条一次性测试段测量")
        self.assertEqual(stats["visibility"]["testTrialsRecorded"], 1)
        self.assertEqual(cs.stored_verdict(self.db, self.uid)["runId"], test_run)
        trial = self.db.query(
            "SELECT run_id, segment FROM agent_trials WHERE segment='test'"
        )
        self.assertEqual(len(trial), 1)
        self.assertEqual(str(trial[0]["run_id"]), str(test_run))
        # A second adjudication changes nothing: no third row, same verdict row.
        again = cs.adjudicate(self.db, self.uid, approved_by="someone@else",
                              proposal_uid="p0", run_id=test_run)
        self.assertTrue(again["idempotent"])
        self.assertEqual(
            self.db.query("SELECT COUNT(*) AS c FROM agent_trials WHERE segment='test'")[0]["c"], 1
        )


# --------------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------------


class CampaignStatsApiTests(CampaignFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        super().setUp()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_stats_are_readable_before_and_after_unsealing(self):
        metrics = self.validation_runs()
        test_run, test_metrics = self.store_test_run()

        sealed = await self.client.get(f"/api/campaigns/{self.uid}/stats")
        self.assertEqual(sealed.status_code, 200)
        body = sealed.json()
        self.assertTrue(body["testSealed"])
        self.assertEqual(body["observed"]["segment"], "validation")
        self.assertAlmostEqual(body["observed"]["sharpe"], metrics["p0"]["sharpe"], places=9)
        self.assertTrue(body["deflatedSharpe"]["available"])
        self.assertAlmostEqual(body["pbo"]["pbo"], 0.671428571, places=8)
        self.assertNotIn(test_run, sealed.text)
        self.assertNotIn(str(WINDOWS["test"][1]), sealed.text)
        self.assertNotIn(f"{test_metrics['sharpe']:.6f}", sealed.text)

        refused = await self.client.get(
            f"/api/campaigns/{self.uid}/stats", params={"includeTest": "true"}
        )
        self.assertEqual(refused.status_code, 409)
        self.assertIn("开封", refused.json()["detail"])

        verdict = await self.client.post(
            f"/api/campaigns/{self.uid}/verdict",
            json={"approvedBy": "operator@desk", "proposalId": "p0", "runId": test_run},
        )
        self.assertEqual(verdict.status_code, 200)
        self.assertEqual(verdict.json()["verdict"], "pass")

        after = await self.client.get(
            f"/api/campaigns/{self.uid}/stats", params={"includeTest": "true"}
        )
        self.assertEqual(after.status_code, 200)
        self.assertTrue(after.json()["visibility"]["includeTest"])
        self.assertEqual(after.json()["trialsRecorded"], 5)
        self.assertEqual(after.json()["visibility"]["testTrialsRecorded"], 1)
        # The default view is still the train+validation picture even after unsealing:
        # opening the window is not the same as making it the headline number.
        default_view = await self.client.get(f"/api/campaigns/{self.uid}/stats")
        self.assertEqual(default_view.status_code, 200)
        self.assertFalse(default_view.json()["visibility"]["includeTest"])
        self.assertEqual(default_view.json()["observed"]["segment"], "validation")
        # The search-phase statistics did not move when the seal came off.
        self.assertAlmostEqual(
            after.json()["deflatedSharpe"]["deflatedSharpe"],
            body["deflatedSharpe"]["deflatedSharpe"], places=12,
        )

    async def test_the_verdict_endpoint_explains_itself_when_sealed(self):
        self.validation_runs()
        response = await self.client.get(f"/api/campaigns/{self.uid}/verdict")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["judged"])
        self.assertTrue(body["testSealed"])
        self.assertIsNone(body["verdict"])
        self.assertIn("尚未开封", body["reason"])
        self.assertNotIn("sharpe", body)
        self.assertNotIn("pbo", body)

    async def test_the_verdict_endpoint_is_idempotent_and_reports_a_failure(self):
        self.uid = self.register(success_criteria="testSharpe >= 99")
        self.validation_runs()
        test_run, _ = self.store_test_run()
        first = await self.client.post(
            f"/api/campaigns/{self.uid}/verdict",
            json={"approvedBy": "operator@desk", "proposalId": "p0", "runId": test_run},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["verdict"], "fail")
        self.assertFalse(first.json()["idempotent"])

        second = await self.client.post(
            f"/api/campaigns/{self.uid}/verdict",
            json={"approvedBy": "operator@desk", "proposalId": "p0"},
        )
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["idempotent"])
        self.assertEqual(second.json()["verdict"], "fail")
        self.assertEqual(second.json()["createdTs"], first.json()["createdTs"])

        read_back = await self.client.get(f"/api/campaigns/{self.uid}/verdict")
        self.assertEqual(read_back.status_code, 200)
        body = read_back.json()
        self.assertTrue(body["judged"])
        self.assertEqual(body["verdict"], "fail")
        self.assertEqual(body["criteria"], "testSharpe >= 99")
        self.assertEqual(body["approvedBy"], "operator@desk")
        self.assertEqual(
            self.db.query("SELECT COUNT(*) AS count FROM agent_verdicts")[0]["count"], 1
        )

    async def test_the_verdict_endpoint_requires_an_approver(self):
        self.validation_runs()
        response = await self.client.post(f"/api/campaigns/{self.uid}/verdict", json={})
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(campaigns.get_campaign(self.db, self.uid)["testUnsealedTs"])

    async def test_an_unknown_campaign_is_a_404_on_both_endpoints(self):
        stats = await self.client.get("/api/campaigns/nope/stats")
        self.assertEqual(stats.status_code, 404)
        verdict = await self.client.get("/api/campaigns/nope/verdict")
        self.assertEqual(verdict.status_code, 404)


_REFERENCE_PLUGIN: dict = {}


def load_reference_plugin():
    """The frozen `vibe-factors` module, imported read-only for a cross-check."""
    if "plugin" in _REFERENCE_PLUGIN:
        return _REFERENCE_PLUGIN["plugin"]
    path = Path(__file__).resolve().parents[2] / "plugins" / "vibe-factors" / "plugin.py"
    plugin = None
    if path.exists():
        spec = importlib.util.spec_from_file_location("vibe_factors_reference", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = module
    _REFERENCE_PLUGIN["plugin"] = plugin
    return plugin
