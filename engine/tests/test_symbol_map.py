"""The one symbol map every external provider reads.

The map is the place where "which contract is which security" is decided. These
tests pin the rules the execution report calls out by name, because a mapping bug
does not look like a bug - it looks like a provider returning someone else's data.
"""

from __future__ import annotations

import unittest

from quantdesk.config.instruments import VENUE_SYMBOLS
from quantdesk.config.symbol_map import (
    MAPPINGS,
    SCENARIO_IDS,
    SCENARIOS,
    fincept_symbols,
    mapping_for,
    mapping_for_reference,
    mapping_payload,
    reference_symbols,
    resolve_shocks,
    scenario_for,
    shock_for,
    symbol_map_payload,
)


class MappingCoverageTests(unittest.TestCase):
    def test_every_contract_in_the_pool_has_exactly_one_mapping(self):
        self.assertEqual(set(MAPPINGS), set(VENUE_SYMBOLS))
        self.assertEqual(len(symbol_map_payload()), len(VENUE_SYMBOLS))

    def test_the_two_columns_that_are_not_free(self):
        # Fincept always risks the QuantDesk contract code, never a reference
        # ticker: the risk is taken on the contract we actually hold.
        self.assertEqual(fincept_symbols(), list(VENUE_SYMBOLS))
        for venue_symbol, mapping in MAPPINGS.items():
            self.assertEqual(mapping.fincept_symbol, venue_symbol)

    def test_reference_codes_are_public_securities_or_coins(self):
        self.assertIn("AAPL", reference_symbols())
        self.assertIn("BTC", reference_symbols())
        self.assertNotIn("AAPLUSDT", reference_symbols())
        self.assertNotIn("AMDSTOCKUSDT", reference_symbols())

    def test_an_unknown_contract_is_refused_with_a_reason(self):
        with self.assertRaisesRegex(KeyError, "不在固定合约池"):
            mapping_for("DOGEUSDT")
        with self.assertRaisesRegex(KeyError, "不在固定合约池"):
            mapping_payload("DOGEUSDT")


class ReportRuleTests(unittest.TestCase):
    """The specific rules the execution report names."""

    def test_amd_maps_from_the_tokenised_contract_to_the_share(self):
        self.assertEqual(mapping_for("AMDSTOCKUSDT").openbb_symbol, "AMD")
        self.assertEqual(mapping_for("AMDSTOCKUSDT").display_symbol, "AMD")

    def test_leveraged_etfs_are_marked_as_leveraged(self):
        self.assertTrue(mapping_for("SOXLUSDT").leveraged)
        self.assertTrue(mapping_for("SOXSUSDT").leveraged)
        self.assertEqual(mapping_for("SOXLUSDT").asset_class, "leveraged_etf")
        self.assertFalse(mapping_for("NVDAUSDT").leveraged)

    def test_names_whose_reference_may_not_exist_are_flagged_not_guessed(self):
        # SPCX and SKHY may have no record at the provider. The map says so, so a
        # failed lookup is reported as unavailable instead of being retried
        # against a lookalike ticker.
        self.assertTrue(mapping_for("SPCXUSDT").reference_may_be_unavailable)
        self.assertTrue(mapping_for("SKHYUSDT").reference_may_be_unavailable)
        self.assertFalse(mapping_for("AAPLUSDT").reference_may_be_unavailable)
        self.assertEqual(mapping_for("SPCXUSDT").openbb_symbol, "SPCX")

    def test_crypto_references_the_coin_not_the_perp(self):
        self.assertEqual(mapping_for("BTCUSDT").openbb_symbol, "BTC")
        self.assertEqual(mapping_for("ETHUSDT").openbb_symbol, "ETH")
        self.assertEqual(mapping_for("BTCUSDT").asset_class, "crypto")

    def test_a_reference_resolves_back_to_one_contract(self):
        self.assertEqual(mapping_for_reference("AMD").venue_symbol, "AMDSTOCKUSDT")
        self.assertEqual(mapping_for_reference("SOXL").venue_symbol, "SOXLUSDT")
        self.assertIsNone(mapping_for_reference("NOT_A_TICKER"))

    def test_the_payload_sent_to_a_plugin_is_the_mapping_plus_the_optional_flag(self):
        payload = mapping_payload("SPCXUSDT")
        self.assertEqual(payload["mapping"]["openbbSymbol"], "SPCX")
        self.assertTrue(payload["referenceOptional"])
        self.assertFalse(mapping_payload("AAPLUSDT")["referenceOptional"])


class ScenarioTests(unittest.TestCase):
    def test_the_report_scenarios_all_exist(self):
        for scenario_id in ("all_equities_down", "semiconductor_shock", "crypto_selloff",
                            "volatility_spike", "funding_anomaly"):
            self.assertIn(scenario_id, SCENARIO_IDS)
        self.assertEqual(len(SCENARIOS), len(SCENARIO_IDS))
        with self.assertRaisesRegex(KeyError, "未知情景"):
            scenario_for("moon")

    def test_an_equity_scenario_leaves_crypto_alone(self):
        resolved = resolve_shocks(scenario_for("all_equities_down")["shocks"])
        self.assertEqual(resolved["AAPLUSDT"], -10.0)
        self.assertEqual(resolved["SOXLUSDT"], -10.0)
        self.assertNotIn("BTCUSDT", resolved)

    def test_a_semiconductor_shock_names_its_members(self):
        # The pool files NVDA under 科技龙头, so the scenario names the chain
        # explicitly: a semiconductor shock that skips NVDA is not a shock.
        resolved = resolve_shocks(scenario_for("semiconductor_shock")["shocks"])
        self.assertEqual(resolved["NVDAUSDT"], -15.0)
        self.assertEqual(resolved["MUUSDT"], -15.0)
        self.assertEqual(resolved["SKHYUSDT"], -15.0)
        self.assertEqual(resolved["SOXLUSDT"], -15.0)
        self.assertEqual(resolved["AAPLUSDT"], -3.0, "链外股票只受资产类别冲击")

    def test_a_group_shock_still_works_for_groups_the_pool_defines(self):
        resolved = resolve_shocks({"group:半导体": -15.0, "assetClass:equity": -3.0})
        self.assertEqual(resolved["MUUSDT"], -15.0)
        self.assertEqual(resolved["AAPLUSDT"], -3.0)

    def test_an_explicit_symbol_shock_beats_every_rule(self):
        mapping = mapping_for("NVDAUSDT")
        self.assertEqual(shock_for(mapping, {"group:半导体": -15.0, "symbol:NVDAUSDT": -5.0}), -5.0)
        # NVDA is not in the pool's 半导体 display group, which is exactly why the
        # scenario names its members instead of trusting the group rule.
        self.assertIsNone(shock_for(mapping, {"group:半导体": -15.0}))
        self.assertEqual(shock_for(mapping_for("MUUSDT"), {"group:半导体": -15.0}), -15.0)
        self.assertEqual(shock_for(mapping, {"symbol:NVDAUSDT": -5.0, "assetClass:equity": -3.0}), -5.0)
        self.assertIsNone(shock_for(mapping, {"assetClass:crypto": -20.0}))

    def test_a_crypto_selloff_only_touches_crypto(self):
        resolved = resolve_shocks(scenario_for("crypto_selloff")["shocks"])
        self.assertEqual(set(resolved), {"BTCUSDT", "ETHUSDT"})

    def test_a_custom_shock_can_target_one_contract(self):
        resolved = resolve_shocks({"symbol:BTCUSDT": -7.5})
        self.assertEqual(resolved, {"BTCUSDT": -7.5})


if __name__ == "__main__":
    unittest.main()
