"""Risk model: venue tiers, mark-price liquidation, funding settlement, costs.

The numbers in the fixtures are the venue's own, captured from
`/v5/market/risk-limit` and `/v5/market/mark-price-kline`, so the arithmetic is
checked against a real ladder rather than a remembered constant.
"""

from __future__ import annotations

import unittest

from quantdesk.backtest import BacktestConfig, run_backtest
from quantdesk.risk import (
    RiskProfile,
    leverage_violation,
    liquidation_price,
    maintenance_margin_for,
    margin_ratio,
    position_risk,
    profile_from_rows,
    tier_rows_for_db,
)

# Captured from Bybit: BTCUSDT rungs 1-3 and SOXLUSDT rung 1.
BTC_TIERS = [
    {"id": 1, "riskLimitValue": "300000", "maintenanceMargin": "0.0033", "maxLeverage": "150.00", "isLowestRisk": 1},
    {"id": 2, "riskLimitValue": "800000", "maintenanceMargin": "0.005", "maxLeverage": "100.00"},
    {"id": 3, "riskLimitValue": "2000000", "maintenanceMargin": "0.01", "maxLeverage": "50.00"},
]
SOXL_TIERS = [
    {"id": 1, "riskLimitValue": "10000", "maintenanceMargin": "0.007", "maxLeverage": "100.00", "isLowestRisk": 1},
    {"id": 2, "riskLimitValue": "100000", "maintenanceMargin": "0.01", "maxLeverage": "50.00"},
]

HOUR = 3_600_000
START = 1_700_000_000_000


def profile(symbol: str, rows: list[dict]) -> RiskProfile:
    return RiskProfile.from_rows(symbol, rows, synced_at=START)


def bars(count: int = 120, price: float = 100.0, volume: float = 5_000.0, step: float = 0.001) -> list[dict]:
    """A slow sine so a moving-average rule actually trades."""
    import math

    out = []
    for index in range(count):
        value = price * (1 + math.sin(index / 9) * step * 6)
        out.append(
            {
                "ts": START + index * HOUR,
                "open": value,
                "high": value * 1.002,
                "low": value * 0.998,
                "close": value,
                "volume": volume,
            }
        )
    return out


class TierTests(unittest.TestCase):
    def test_ladder_reads_the_rung_a_position_sits_in(self):
        btc = profile("BTCUSDT", BTC_TIERS)
        self.assertEqual(btc.max_leverage, 150.0)
        self.assertAlmostEqual(btc.min_maintenance_margin_rate, 0.0033)
        self.assertEqual(btc.tier_for(200_000).tier_id, 1)
        self.assertEqual(btc.tier_for(300_000).tier_id, 1)
        self.assertEqual(btc.tier_for(300_001).tier_id, 2)
        self.assertEqual(btc.tier_for(900_000).tier_id, 3, "past every rung the strictest one applies")

    def test_a_rung_is_only_eligible_when_it_allows_that_leverage(self):
        btc = profile("BTCUSDT", BTC_TIERS)
        # 500k notional at 120x: rung 2 covers the size but caps leverage at 100x,
        # so the position is margined in rung 3 and cannot use 120x there either.
        self.assertEqual(btc.tier_for(500_000, leverage=120).tier_id, 3)
        self.assertFalse(btc.leverage_allowed(500_000, 120))
        self.assertTrue(btc.leverage_allowed(500_000, 50))
        self.assertEqual(leverage_violation(btc, 500_000, 120) is not None, True)

    def test_maintenance_margin_is_the_rung_rate_minus_its_deduction(self):
        btc = profile("BTCUSDT", BTC_TIERS)
        tier = btc.tier_for(200_000)
        self.assertAlmostEqual(maintenance_margin_for(tier, 200_000, 0.005), 660.0, places=6)
        # A published deduction reduces the requirement; the constant does not.
        deducting = RiskProfile.from_rows(
            "BTCUSDT", [{"id": 1, "riskLimitValue": "1000000", "maintenanceMargin": "0.01",
                         "mmDeduction": "500", "maxLeverage": "50"}]
        )
        self.assertAlmostEqual(maintenance_margin_for(deducting.tiers[0], 200_000, 0.005), 1_500.0, places=6)

    def test_a_missing_ladder_reports_the_fallback_instead_of_a_tier(self):
        empty = RiskProfile(venue_symbol="BTCUSDT", tiers=())
        self.assertIsNone(empty.max_leverage)
        self.assertIsNone(empty.tier_for(1_000))
        risk = position_risk(direction=1, entry_price=100.0, quantity=1.0, leverage=10.0, profile=empty)
        self.assertTrue(risk.warnings, "a fallback rate must be disclosed")
        self.assertAlmostEqual(risk.maintenance_margin_rate, 0.005)

    def test_rows_survive_a_database_round_trip(self):
        btc = profile("BTCUSDT", BTC_TIERS)
        restored = profile_from_rows(
            "BTCUSDT",
            [
                {
                    "tier_id": tier.tier_id,
                    "risk_limit_value": tier.risk_limit_value,
                    "maintenance_margin_rate": tier.maintenance_margin_rate,
                    "mm_deduction": tier.mm_deduction,
                    "max_leverage": tier.max_leverage,
                    "initial_margin_rate": tier.initial_margin_rate,
                    "lowest_risk": tier.lowest_risk,
                    "source": "bybit:risk-limit",
                    "synced_at": START,
                }
                for tier in btc.tiers
            ],
        )
        self.assertEqual([t.tier_id for t in restored.tiers], [1, 2, 3])
        self.assertEqual(restored.source, "bybit:risk-limit")
        self.assertEqual(restored.synced_at, START)
        self.assertEqual(len(tier_rows_for_db(btc)), 3)


class LiquidationTests(unittest.TestCase):
    def test_the_tiered_formula_matches_the_closed_form(self):
        # Long:  (qty x entry - margin + deduction) / (qty x (1 - mmr))
        # Short: (qty x entry + margin + deduction) / (qty x (1 + mmr))
        for entry, qty, leverage, mmr in ((100.0, 1.0, 20.0, 0.0033), (500.0, 2.0, 100.0, 0.005)):
            margin = qty * entry / leverage
            expected_long = (qty * entry - margin) / (qty * (1 - mmr))
            expected_short = (qty * entry + margin) / (qty * (1 + mmr))
            self.assertAlmostEqual(liquidation_price(1, entry, qty, margin, mmr), expected_long, places=8)
            self.assertAlmostEqual(liquidation_price(-1, entry, qty, margin, mmr), expected_short, places=8)

    def test_a_one_x_long_cannot_be_liquidated_but_a_one_x_short_can(self):
        # The old leverage-only approximation claimed both could be.
        self.assertIsNone(liquidation_price(1, 100.0, 1.0, 100.0, 0.0033))
        short = liquidation_price(-1, 100.0, 1.0, 100.0, 0.0033)
        self.assertIsNotNone(short)
        self.assertAlmostEqual(short, (100 + 100) / (1 + 0.0033), places=6)

    def test_a_stricter_rung_liquidates_earlier(self):
        btc = profile("BTCUSDT", BTC_TIERS)
        small = position_risk(direction=1, entry_price=77_000.0, quantity=2.0, leverage=20.0, profile=btc)
        large = position_risk(direction=1, entry_price=77_000.0, quantity=40.0, leverage=20.0, profile=btc)
        self.assertEqual(small.tier.tier_id, 1)
        self.assertEqual(large.tier.tier_id, 3)
        self.assertGreater(large.liq_price, small.liq_price, "the stricter tier must liquidate at a higher price")
        self.assertGreater(large.maintenance_margin_rate, small.maintenance_margin_rate)

    def test_legacy_positional_call_still_answers(self):
        # The CLI and any older caller use (direction, entry, leverage, mmr).
        from quantdesk.backtest import liquidation_price as legacy

        self.assertAlmostEqual(legacy(1, 100.0, 20, 0.0033) or 0.0, 95.31453797531854, places=8)

    def test_margin_ratio_uses_the_position_own_requirement(self):
        self.assertAlmostEqual(margin_ratio(10_000, 1_000, 0.005), 0.05, places=8)
        self.assertIsNone(margin_ratio(10_000, 0.0, 0.005))


class MarkAndFundingTests(unittest.TestCase):
    def test_liquidation_reads_the_mark_extreme_not_the_last_trade(self):
        candles = bars(120)
        # The trade prints never touch the liquidation level, but the mark does.
        marks = [{"ts": bar["ts"], "open": bar["close"], "high": bar["close"] * 1.5,
                  "low": bar["close"] * 0.5, "close": bar["close"]} for bar in candles]
        config = BacktestConfig(leverage=20, allocation_pct=100, direction="long", include_funding=False)
        without = run_backtest(candles, config, interval="1h")
        with_marks = run_backtest(candles, config, marks=marks, interval="1h")
        self.assertEqual([t.liquidated for t in without.trades], [False] * len(without.trades))
        self.assertTrue(any(t.liquidated for t in with_marks.trades), "the mark extreme must trigger liquidation")
        self.assertTrue(with_marks.risk["liquidations"])
        self.assertEqual(with_marks.data_quality["markSource"], "bybit:mark-price-kline")
        self.assertEqual(without.data_quality["markSource"], "bar_close_fallback")

    def test_funding_settles_on_the_mark_at_the_venue_timestamp(self):
        candles = bars(72, volume=5_000.0)
        # One settlement mid-sample, at a price the bar close never shows.
        settle_ts = START + 40 * HOUR
        funding = [{"ts": settle_ts, "rate": 0.001}]
        marks = [{"ts": bar["ts"], "open": bar["close"], "high": bar["close"],
                  "low": bar["close"], "close": bar["close"] * 1.2} for bar in candles]
        config = BacktestConfig(leverage=5, allocation_pct=50, direction="both", include_liquidation=False)
        result = run_backtest(candles, config, funding=funding, marks=marks, interval="1h")
        settlements = result.risk["fundingSettlements"]
        self.assertTrue(settlements, "the settlement must be recorded")
        entry = settlements[0]
        self.assertEqual(entry["ts"], settle_ts)
        self.assertAlmostEqual(entry["rate"], 0.001)
        # The mark at that bar, not the trade close.
        expected_price = next(row["close"] for row in marks if row["ts"] == settle_ts)
        self.assertAlmostEqual(entry["price"], expected_price, places=6)
        self.assertAlmostEqual(result.total_funding, entry["cost"], places=6)

    def test_without_a_mark_series_the_fallback_is_declared(self):
        candles = bars(72)
        funding = [{"ts": START + 40 * HOUR, "rate": 0.001}]
        config = BacktestConfig(leverage=5, allocation_pct=50)
        result = run_backtest(candles, config, funding=funding, interval="1h")
        self.assertEqual(result.risk["fundingSettlements"][0]["ts"], START + 40 * HOUR)
        self.assertTrue(any("标记价" in warning for warning in result.warnings))


class SlippageModelTests(unittest.TestCase):
    def test_participation_adds_impact_over_the_fixed_model(self):
        candles = bars(120, volume=1_000.0)
        base = dict(leverage=5, allocation_pct=100, direction="both", include_funding=False, include_liquidation=False)
        fixed = run_backtest(candles, BacktestConfig(**base, slippage_model="fixed"), interval="1h")
        impact = run_backtest(
            candles,
            BacktestConfig(**base, slippage_model="participation", impact_coefficient=0.2),
            interval="1h",
        )
        self.assertTrue(fixed.trades and impact.trades)
        # This fixture opens on a downward cross, so the first trade is a short:
        # impact makes the fill worse, which for a short means selling lower.
        self.assertLess(impact.trades[0].entry_price, fixed.trades[0].entry_price, "impact must worsen the fill")
        self.assertEqual(impact.trades[0].direction, "空")
        self.assertTrue(any("参与率" in warning for warning in impact.warnings))
        self.assertFalse(any("参与率" in warning for warning in fixed.warnings))

    def test_an_unknown_slippage_model_is_rejected(self):
        with self.assertRaises(ValueError):
            run_backtest(bars(60), BacktestConfig(slippage_model="magic"), interval="1h")


class ResultProvenanceTests(unittest.TestCase):
    def test_the_result_carries_the_ladder_and_mark_provenance(self):
        candles = bars(120)
        btc = profile("BTCUSDT", BTC_TIERS)
        marks = [{"ts": bar["ts"], "open": bar["close"], "high": bar["close"], "low": bar["close"], "close": bar["close"]}
                 for bar in candles]
        config = BacktestConfig(leverage=10, allocation_pct=100, direction="both", include_funding=False)
        result = run_backtest(candles, config, marks=marks, risk_profile=btc, interval="1h")
        quality = result.data_quality
        self.assertEqual(quality["riskTiers"], 3)
        self.assertEqual(quality["riskSource"], "bybit:risk-limit")
        self.assertEqual(quality["maxLeverageAllowed"], 150.0)
        self.assertEqual(quality["markBars"], len(marks))
        self.assertTrue(result.risk["tiered"])
        tiered = [t for t in result.trades if t.risk_tier_id is not None]
        self.assertTrue(tiered, "each trade must record the rung it was margined in")
        self.assertTrue(all(t.maintenance_margin_rate for t in tiered))

    def test_a_leverage_above_the_rung_cap_is_reported(self):
        candles = bars(120)
        btc = profile("BTCUSDT", BTC_TIERS)
        config = BacktestConfig(leverage=120, allocation_pct=100, initial_capital=1_000_000, direction="both",
                               include_funding=False, include_liquidation=False)
        result = run_backtest(candles, config, risk_profile=btc, interval="1h")
        self.assertTrue(any("档位上限" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()


class FiveContractComparisonTests(unittest.TestCase):
    """The comparison the requirement asks for, on the venue's real ladders.

    Each contract is run through the same strategy and costs so the only thing
    that varies is what the venue actually publishes for it: the maintenance
    margin rate of the rung the position sits in and the leverage that rung
    allows. The ladders below were read from `/v5/market/risk-limit`.
    """

    LADDERS = {
        "BTCUSDT": BTC_TIERS,
        "ETHUSDT": BTC_TIERS,
        # A single name carries its own ladder: a 200k order already sits in the
        # 1% rung, while BTC's 200k sits in the 0.33% rung.
        "AAPLUSDT": [
            {"id": 1, "riskLimitValue": "5000", "maintenanceMargin": "0.005", "maxLeverage": "100"},
            {"id": 2, "riskLimitValue": "10000", "maintenanceMargin": "0.0067", "maxLeverage": "50"},
            {"id": 3, "riskLimitValue": "50000", "maintenanceMargin": "0.01", "maxLeverage": "10"},
            {"id": 4, "riskLimitValue": "2000000", "maintenanceMargin": "0.015", "maxLeverage": "5"},
        ],
        "NVDAUSDT": [
            {"id": 1, "riskLimitValue": "5000", "maintenanceMargin": "0.005", "maxLeverage": "100"},
            {"id": 2, "riskLimitValue": "10000", "maintenanceMargin": "0.0067", "maxLeverage": "50"},
            {"id": 3, "riskLimitValue": "50000", "maintenanceMargin": "0.01", "maxLeverage": "10"},
            {"id": 4, "riskLimitValue": "2000000", "maintenanceMargin": "0.015", "maxLeverage": "5"},
        ],
        # The leveraged ETF's first rung is the most expensive of the five.
        "SOXLUSDT": [
            {"id": 1, "riskLimitValue": "10000", "maintenanceMargin": "0.007", "maxLeverage": "100"},
            {"id": 2, "riskLimitValue": "100000", "maintenanceMargin": "0.01", "maxLeverage": "50"},
            {"id": 3, "riskLimitValue": "500000", "maintenanceMargin": "0.015", "maxLeverage": "10"},
            {"id": 4, "riskLimitValue": "2000000", "maintenanceMargin": "0.02", "maxLeverage": "5"},
        ],
    }

    def test_every_contract_is_margined_by_its_own_ladder(self):
        candles = bars(300, price=100.0, volume=20_000.0, step=0.004)
        marks = [
            {"ts": bar["ts"], "open": bar["close"], "high": bar["close"] * 1.01,
             "low": bar["close"] * 0.99, "close": bar["close"]}
            for bar in candles
        ]
        funding = [{"ts": START + 50 * HOUR, "rate": 0.0001}, {"ts": START + 200 * HOUR, "rate": -0.0002}]
        seen: dict[str, dict] = {}
        for symbol, raw in self.LADDERS.items():
            config = BacktestConfig(
                leverage=20,
                allocation_pct=100,
                initial_capital=100_000,
                direction="both",
                include_funding=True,
                include_liquidation=True,
                slippage_model="participation",
            )
            result = run_backtest(
                candles,
                config,
                funding=funding,
                marks=marks,
                risk_profile=profile(symbol, raw),
                interval="1h",
            )
            rungs = {entry["tierId"] for entry in result.risk["liquidations"]}
            broken = [w for w in result.warnings if "档位上限" in w]
            # Rung 1 covers 300k on BTC and 5k on the single names, so the same
            # 2M order lands in a different rung per contract, which is exactly
            # what a single constant rate cannot express.
            traded = [tier for tier in (result.risk["tradedTiers"] or []) if tier is not None]
            self.assertTrue(traded, f"{symbol} must record the rung it traded in")
            self.assertGreater(
                max(traded), 1, f"{symbol}: a 2M notional must cross out of the smallest rung"
            )
            self.assertTrue(result.data_quality["riskTiers"] >= 2)
            self.assertEqual(result.data_quality["markSource"], "bybit:mark-price-kline")
            self.assertTrue(result.risk["fundingSettlements"], f"{symbol} funding must settle")
            rates = {
                tier["tierId"]: tier["maintenanceMarginRate"]
                for tier in profile(symbol, raw).as_dict()["tiers"]
            }
            tier_ids = [t.risk_tier_id for t in result.trades if t.risk_tier_id is not None]
            seen[symbol] = {
                "tierIds": sorted(set(tier_ids)),
                "maxTierRate": max(rates[tier] for tier in tier_ids) if tier_ids else None,
                "liquidations": len(result.risk["liquidations"]),
                "rungsLiquidated": sorted(rungs),
                "breaches": broken,
            }

        self.assertEqual(len(seen), 5)
        # The same 1.8M order with the same 20x is allowed on BTC/ETH (their
        # 1.8M rung permits 50x) and refused on the single names and SOXL (their
        # rung permits 10x). A single global cap cannot express that difference.
        for symbol in ("AAPLUSDT", "NVDAUSDT", "SOXLUSDT"):
            self.assertTrue(seen[symbol]["breaches"], f"{symbol} must report the leverage breach")
            self.assertIn("档位", seen[symbol]["breaches"][0])
        for symbol in ("BTCUSDT", "ETHUSDT"):
            self.assertFalse(
                seen[symbol]["breaches"],
                f"{symbol}'s 1.8M rung allows 50x, so 20x must not be flagged",
            )
        # At one notional the five contracts must not share one rate: BTC/ETH sit
        # in their 0.33% rung, the single names in their 0.5% rung, SOXL in its
        # 0.7% rung. A single configured constant cannot express that.
        notional = 2_000.0
        rates = {}
        for symbol, raw in self.LADDERS.items():
            tier = profile(symbol, raw).tier_for(notional)
            self.assertIsNotNone(tier, symbol)
            rates[symbol] = tier.maintenance_margin_rate
        self.assertGreaterEqual(len(set(rates.values())), 3, f"rates must differ per contract: {rates}")
        self.assertLess(rates["BTCUSDT"], rates["AAPLUSDT"], "the single name is the stricter rung")
        self.assertLess(rates["AAPLUSDT"], rates["SOXLUSDT"], "the leveraged ETF is strictest of all")
        self.assertEqual(rates["BTCUSDT"], rates["ETHUSDT"], "the two majors share a ladder")
        # A larger order never lands in a cheaper rung of the same contract, which
        # is why one contract cannot carry one rate either.
        for symbol, raw in self.LADDERS.items():
            ladder = profile(symbol, raw)
            big = ladder.tier_for(1_800_000.0)
            self.assertIsNotNone(big, symbol)
            self.assertGreaterEqual(
                big.maintenance_margin_rate,
                rates[symbol],
                f"{symbol}: a 1.8M order must not be cheaper than a 200k one",
            )
        # And the realised runs must have used rungs from those same ladders.
        for symbol, data in seen.items():
            self.assertTrue(data["tierIds"], f"{symbol} recorded no rung")
            ladder = profile(symbol, self.LADDERS[symbol])
            known = {tier.tier_id: tier.maintenance_margin_rate for tier in ladder.tiers}
            for tier_id in data["tierIds"]:
                self.assertIn(tier_id, known, f"{symbol} traded a rung that is not in its ladder")

    def test_a_fixed_rate_would_misprice_every_stock_perp(self):
        # The rung AAPL/NVDA sit in charges 0.67%, the SOXL rung 0.7%, and the
        # first prototype charged all of them 0.5%.
        for symbol in ("AAPLUSDT", "NVDAUSDT", "SOXLUSDT"):
            ladder = profile(symbol, self.LADDERS[symbol])
            tier = ladder.tier_for(8_000)
            self.assertIsNotNone(tier)
            self.assertGreater(tier.maintenance_margin_rate, 0.005)
            tiered = position_risk(direction=1, entry_price=100.0, quantity=80.0, leverage=20.0, profile=ladder)
            constant = position_risk(
                direction=1, entry_price=100.0, quantity=80.0, leverage=20.0, profile=None,
                fallback_maintenance_rate=0.005,
            )
            self.assertGreater(
                tiered.liq_price,
                constant.liq_price,
                f"{symbol}: the venue's rung liquidates earlier than the 0.5% constant",
            )

    def test_the_ladder_moves_the_liquidation_price_at_the_same_size(self):
        """Same contract, same size, same leverage: only the margin rule differs."""
        for symbol in ("NVDAUSDT", "SOXLUSDT"):
            ladder = profile(symbol, self.LADDERS[symbol])
            notional = 60_000.0
            entry, leverage = 100.0, 20.0
            quantity = notional / entry
            tiered = position_risk(
                direction=1, entry_price=entry, quantity=quantity, leverage=leverage,
                profile=ladder, reference_notional=notional,
            )
            constant = position_risk(
                direction=1, entry_price=entry, quantity=quantity, leverage=leverage,
                profile=None, fallback_maintenance_rate=0.005,
            )
            self.assertGreater(tiered.maintenance_margin_rate, 0.005)
            self.assertGreater(
                tiered.liq_price,
                constant.liq_price,
                f"{symbol}: the venue's rung must liquidate nearer than the 0.5% constant",
            )
            self.assertGreater(tiered.maintenance_margin, constant.maintenance_margin)
            # And the difference is material, not a rounding artefact.
            self.assertGreater((tiered.liq_price - constant.liq_price) / entry * 100, 0.4)
