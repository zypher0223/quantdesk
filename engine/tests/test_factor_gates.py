"""The seven gates, and the library they decide.

Two things are being protected here. First, that each gate actually refuses what it
claims to refuse — a gate that always passes is worse than no gate, because the
library then looks vetted when it is not. Second, that the *tier* a factor lands in
is derived from its evidence rather than from a list someone maintained by hand:
the whole point of `factor_library.tier_of` is that adding a factor to the proposal
space requires evidence, not an edit.
"""

from __future__ import annotations

import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from quantdesk.factor_gates import (
    GATE_NAMES,
    GateThresholds,
    block_bootstrap_p_value,
    evaluate,
    forward_returns,
    gate_coverage,
    gate_cost,
    gate_dispersion,
    gate_finiteness,
    gate_persistence,
    gate_predictive,
    gate_redundancy,
    group_symbols,
    lag_one_autocorrelation,
    spearman,
)
from quantdesk.factor_library import (
    ALL_GATES,
    HYGIENE_GATES,
    entries_of,
    library_for,
    load_library_report,
    save_report,
    summarise,
    tier_of,
)


def walk(bars: int, seed: int, *, drift: float = 0.0, noise: float = 0.01) -> list[float]:
    rng = random.Random(seed)
    price = 100.0
    out = []
    for _ in range(bars):
        price *= 1 + drift + rng.gauss(0.0, noise)
        out.append(price)
    return out


def evidence_with(**gates: bool) -> dict:
    """A gate record with the named gates passing and the rest failing."""
    return {
        "factorId": "vibe.test",
        "family": "quantdesk",
        "gates": [
            {"name": name, "passed": bool(gates.get(name, False)), "detail": "", "metric": {}}
            for name in ALL_GATES
        ],
    }


class SpearmanTests(unittest.TestCase):
    def test_a_perfect_rank_agreement_is_one(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0, places=12)

    def test_a_reversed_order_is_minus_one(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [40, 30, 20, 10]), -1.0, places=12)

    def test_ties_are_averaged_rather_than_broken_by_position(self):
        # [1, 2, 2, 3] against a strictly increasing series: the two tied values must
        # get the same rank, so the correlation is below 1 but well above 0.
        value = spearman([1, 2, 2, 3], [1, 2, 3, 4])
        self.assertIsNotNone(value)
        self.assertTrue(0.8 < value < 1.0, value)

    def test_a_constant_series_has_no_rank_correlation(self):
        self.assertIsNone(spearman([1, 1, 1, 1], [1, 2, 3, 4]))

    def test_too_few_points_is_not_a_correlation(self):
        self.assertIsNone(spearman([1, 2], [2, 1]))


class BootstrapTests(unittest.TestCase):
    def test_noise_does_not_produce_a_significant_p_value(self):
        rng = random.Random(7)
        pairs = [(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(800)]
        ic, p_value = block_bootstrap_p_value(pairs, resamples=120, block=9)
        self.assertIsNotNone(ic)
        self.assertLess(abs(ic), 0.12)
        self.assertGreater(p_value, 0.10, "纯噪声不应显著")

    def test_a_real_association_is_detected(self):
        rng = random.Random(11)
        pairs = []
        for _ in range(800):
            value = rng.gauss(0, 1)
            pairs.append((value, value * 0.5 + rng.gauss(0, 1)))
        ic, p_value = block_bootstrap_p_value(pairs, resamples=120, block=9)
        self.assertGreater(ic, 0.3)
        self.assertLess(p_value, 0.05)

    def test_independent_series_are_not_significant(self):
        """Two unrelated series must come back insignificant.

        Note what this does *not* claim: a factor that is itself a random-walk level
        can correlate with anything, because two independent trends correlate by
        construction. That is a property of the input, not of the test - which is why
        the gates run on factors that vary rather than on raw price levels.
        """
        rng = random.Random(3)
        pairs = [(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(1200)]
        ic, p_value = block_bootstrap_p_value(pairs, resamples=120, block=11)
        self.assertIsNotNone(p_value)
        self.assertLess(abs(ic), 0.1)
        self.assertGreater(p_value, 0.05)

    def test_a_short_series_gets_no_p_value_rather_than_a_made_up_one(self):
        ic, p_value = block_bootstrap_p_value([(1.0, 1.0)] * 10, resamples=50, block=3)
        self.assertIsNone(ic)
        self.assertIsNone(p_value)


class ForwardReturnTests(unittest.TestCase):
    def test_forward_returns_are_market_neutral(self):
        closes = {"A": [100.0, 110.0, 121.0], "B": [100.0, 100.0, 100.0]}
        forwards = forward_returns(closes, 1)
        # A rose 10% and B did not: the excess must sum to zero across the group.
        self.assertAlmostEqual(forwards["A"][0] + forwards["B"][0], 0.0, places=12)
        self.assertGreater(forwards["A"][0], 0.0)
        self.assertLess(forwards["B"][0], 0.0)

    def test_the_tail_has_no_forward_return(self):
        forwards = forward_returns({"A": [100.0, 101.0, 102.0]}, 1)
        self.assertIsNone(forwards["A"][-1])
        self.assertIsNotNone(forwards["A"][0])


class IndividualGateTests(unittest.TestCase):
    def setUp(self):
        self.limits = GateThresholds()

    def test_coverage_counts_points_and_refuses_a_thin_symbol(self):
        series = {"A": [1.0] * 200, "B": [None] * 200}
        outcome = gate_coverage(series, self.limits)
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.metric["qualifying"], ["A"])

    def test_finiteness_refuses_an_exploding_value(self):
        series = {"A": [1.0] * 100 + [1e12], "B": [1.0] * 101}
        outcome = gate_finiteness(series, self.limits)
        self.assertFalse(outcome.passed)

    def test_dispersion_refuses_a_constant_column(self):
        outcome = gate_dispersion({"A": [0.5] * 500}, self.limits)
        self.assertFalse(outcome.passed)
        self.assertEqual(outcome.metric["distinct"], 1)
        self.assertEqual(outcome.metric["iqr"], 0.0)
        self.assertIn("100.0%", outcome.detail)

    def test_dispersion_refuses_a_few_repeated_values(self):
        values = [1.0 if index % 2 else 2.0 for index in range(500)]
        outcome = gate_dispersion({"A": values}, self.limits)
        self.assertFalse(outcome.passed)
        self.assertGreater(outcome.metric["tieFraction"], 0.4)

    def test_persistence_refuses_a_sign_flipping_series(self):
        values = [1.0 if index % 2 else -1.0 for index in range(400)]
        outcome = gate_persistence({"A": values}, self.limits)
        self.assertFalse(outcome.passed)

    def test_persistence_accepts_a_slow_moving_series(self):
        values = [math.sin(index / 200.0) for index in range(400)]
        outcome = gate_persistence({"A": values}, self.limits)
        self.assertTrue(outcome.passed, outcome.detail)

    def test_predictive_refuses_pure_noise_on_the_configurations_the_scans_use(self):
        """Measured false-positive rate, not an anecdote about one seed.

        On one symbol the gate lets roughly one noise factor in five through, which
        is why the coverage gate demands at least two symbols before this gate is
        even reached. On the two- and thirteen-symbol shapes the real scans use, the
        sign-consistency requirement is what does the work: over these fixed seeds
        no noise factor passes at all.
        """
        passed: dict[int, int] = {}
        for symbols in (1, 2, 13):
            hits = 0
            for seed in range(12):
                rng = random.Random(seed)
                closes, series = {}, {}
                for index in range(symbols):
                    price, prices, values = 100.0, [], []
                    for _ in range(900):
                        values.append(rng.gauss(0, 1))
                        price *= 1 + rng.gauss(0, 0.01)
                        prices.append(price)
                    closes[f"S{index}"] = prices
                    series[f"S{index}"] = values
                outcome = gate_predictive(
                    series, forward_returns(closes, 1), horizon=1, thresholds=self.limits
                )
                hits += 1 if outcome.passed else 0
            passed[symbols] = hits
        self.assertEqual(passed[2], 0, f"2 标的的噪声因子不应通过：{passed}")
        self.assertEqual(passed[13], 0, f"13 标的的噪声因子不应通过：{passed}")
        self.assertGreater(passed[1], 0, "单标的下闸门确实偏松——覆盖闸门要求至少 2 个标的")

    def test_predictive_accepts_a_factor_that_knows_the_next_return(self):
        """A signal built from the next bar's return must be found, not missed."""
        rng = random.Random(9)
        closes = [100.0]
        for _ in range(900):
            closes.append(closes[-1] * (1 + rng.gauss(0, 0.01)))
        values = [closes[index + 1] / closes[index] - 1.0 for index in range(len(closes) - 1)]
        values.append(0.0)
        outcome = gate_predictive({"A": values}, {"A": forward_returns({"A": closes}, 1)["A"]},
                                  horizon=1, thresholds=self.limits)
        self.assertTrue(outcome.passed, outcome.detail)
        self.assertGreater(outcome.metric["ic"], 0.5)
        self.assertLess(outcome.metric["pValue"], 0.01)

    def test_cost_refuses_a_book_that_pays_more_in_fees_than_it_earns(self):
        rng = random.Random(13)
        closes = [100.0]
        for _ in range(600):
            closes.append(closes[-1] * (1 + rng.gauss(0, 0.005)))
        # A factor that flips every bar: the edge cannot cover the turnover.
        values = [1.0 if index % 2 else -1.0 for index in range(len(closes))]
        outcome = gate_cost({"A": values, "B": [1.0] * len(closes)}, 
                            forward_returns({"A": closes, "B": closes}, 1),
                            horizon=1, thresholds=GateThresholds(cost_bps=11.0))
        self.assertFalse(outcome.passed)
        self.assertGreater(outcome.metric["costBps"], 0.0)

    def test_cost_gives_no_credit_for_being_long_the_market(self):
        """The bug this gate was rewritten for: an always-long book is not a view."""
        closes = [100.0 * (1.002 ** index) for index in range(600)]
        values = [1.0] * 600  # always positive on both symbols
        outcome = gate_cost({"A": values, "B": values},
                            forward_returns({"A": closes, "B": closes}, 1),
                            horizon=1, thresholds=GateThresholds(cost_bps=11.0))
        self.assertFalse(outcome.passed, "单边做多不应通过成本闸门")
        self.assertAlmostEqual(outcome.metric["grossBps"], 0.0, places=6)

    def test_redundancy_refuses_a_copy_and_allows_a_stranger(self):
        values = [math.sin(index / 50.0) for index in range(400)]
        limits = self.limits
        copy = gate_redundancy({"A": values}, {"other": {"A": values}}, limits)
        self.assertFalse(copy.passed)
        self.assertGreater(copy.metric["maxAbsCorrelation"], 0.99)
        stranger = gate_redundancy(
            {"A": values}, {"other": {"A": [-value for value in values]}}, limits
        )
        self.assertFalse(stranger.passed, "完全反相关也是同一份信息")
        independent = gate_redundancy(
            {"A": values},
            {"other": {"A": [math.sin(index / 7.0) for index in range(400)]}},
            limits,
        )
        self.assertTrue(independent.passed, independent.detail)

    def test_the_first_empty_library_accepts_everything(self):
        outcome = gate_redundancy({"A": [1.0, 2.0, 3.0]}, {}, self.limits)
        self.assertTrue(outcome.passed)


class EvaluateTests(unittest.TestCase):
    def test_all_seven_gates_are_always_reported(self):
        evidence = evaluate(
            "vibe.test", family="quantdesk", group="crypto", interval="1d", horizon=1,
            series={"A": [1.0] * 10}, forwards={"A": [0.0] * 10},
        )
        self.assertEqual([gate.name for gate in evidence.gates], list(GATE_NAMES))
        self.assertFalse(evidence.passed)

    def test_a_gate_that_cannot_be_evaluated_says_so_instead_of_passing(self):
        evidence = evaluate(
            "vibe.test", family="quantdesk", group="crypto", interval="1d", horizon=1,
            series={"A": [None] * 100}, forwards={"A": [None] * 100},
        )
        predictive = evidence.gate("predictive")
        self.assertFalse(predictive.passed)
        self.assertIn("前置闸门", predictive.detail)


class LibraryTierTests(unittest.TestCase):
    def test_a_fully_passing_factor_is_validated(self):
        self.assertEqual(tier_of(evidence_with(**{name: True for name in ALL_GATES})), "validated")

    def test_a_factor_missing_only_the_predictive_gate_is_a_candidate(self):
        passes = {name: True for name in HYGIENE_GATES}
        self.assertEqual(tier_of(evidence_with(**passes)), "candidate")

    def test_a_factor_that_cannot_be_traded_is_in_no_tier(self):
        passes = {name: True for name in HYGIENE_GATES}
        passes["cost"] = False
        self.assertIsNone(tier_of(evidence_with(**passes)))

    def test_a_redundant_factor_is_in_no_tier(self):
        passes = {name: True for name in ALL_GATES}
        passes["redundancy"] = False
        self.assertIsNone(tier_of(evidence_with(**passes)))

    def test_entries_put_validated_factors_first(self):
        report = {
            "group": "crypto", "interval": "1d", "horizonBars": 1, "generatedAt": "2026-01-01T00:00:00Z",
            "evidence": [
                {**evidence_with(**{name: True for name in HYGIENE_GATES}), "factorId": "candidate"},
                {**evidence_with(**{name: True for name in ALL_GATES}), "factorId": "validated"},
                {**evidence_with(), "factorId": "rejected"},
            ],
        }
        entries = entries_of(report)
        self.assertEqual([item["factorId"] for item in entries], ["validated", "candidate"])
        self.assertEqual(entries[0]["tier"], "validated")

    def test_a_report_round_trips_through_the_library_directory(self):
        report = {
            "group": "stock", "interval": "1h", "horizonBars": 24, "generatedAt": "2026-01-01T00:00:00Z",
            "evidence": [{**evidence_with(**{name: True for name in ALL_GATES}), "factorId": "vibe.macd.hist"}],
        }
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            path = save_report(report, home)
            self.assertTrue(path.is_file())
            merged = load_library_report(home)
            self.assertIsNotNone(merged)
            self.assertEqual(len(merged["scans"]), 1)
            entries = library_for("stock", "1h", home=home)
            self.assertEqual([item["factorId"] for item in entries], ["vibe.macd.hist"])
            self.assertIn("已验证 1", summarise(merged))

    def test_the_newest_report_wins_for_the_same_scan(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            base = {"group": "crypto", "interval": "1d", "horizonBars": 1, "evidence": []}
            save_report({**base, "generatedAt": "2026-01-01T00:00:00Z"}, home)
            save_report({**base, "generatedAt": "2026-02-01T00:00:00Z",
                         "evidence": [{**evidence_with(**{name: True for name in ALL_GATES}),
                                       "factorId": "newer"}]}, home)
            entries = library_for("crypto", "1d", home=home)
            self.assertEqual([item["factorId"] for item in entries], ["newer"])

    def test_an_empty_library_is_an_empty_list_not_an_error(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(library_for("stock", "1h", home=Path(raw)), [])
            self.assertIsNone(load_library_report(Path(raw)))


class GroupTests(unittest.TestCase):
    def test_the_groups_match_the_declared_universe(self):
        stock = group_symbols("stock")
        leveraged = group_symbols("leveraged_etf")
        crypto = group_symbols("crypto")
        self.assertEqual(len(stock) + len(leveraged) + len(crypto), 17)
        # D5: the leveraged ETFs are never in the stock cross-section.
        self.assertNotIn("SOXLUSDT", stock)
        self.assertNotIn("SOXSUSDT", stock)
        self.assertEqual(leveraged, ["SOXLUSDT", "SOXSUSDT"])
        self.assertEqual(crypto, ["BTCUSDT", "ETHUSDT"])

    def test_an_unknown_group_is_refused(self):
        with self.assertRaises(ValueError):
            group_symbols("everything")


class ShippedLibraryTests(unittest.TestCase):
    def test_the_scans_on_this_machine_are_inside_the_library_directory(self):
        """If scans have been run, their reports must parse and carry evidence."""
        from quantdesk.config.settings import quantdesk_home

        merged = load_library_report(quantdesk_home())
        if merged is None:
            self.skipTest("本机还没有跑过闸门扫描")
        for scan in merged["scans"]:
            self.assertIn(scan["group"], {"stock", "leveraged_etf", "crypto"})
            self.assertTrue(scan["evidence"])
            for evidence in scan["evidence"]:
                self.assertEqual([gate["name"] for gate in evidence["gates"]], list(GATE_NAMES))
                json.dumps(evidence, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
