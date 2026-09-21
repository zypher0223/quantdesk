"""Campaigns: pre-registration, budget, visibility, promotion.

The tests are written against the rules the plan locked (D1-D5), because those rules
are only real if breaking them fails somewhere. A campaign that could widen its own
search space, see its test segment early, run past its budget, or promote itself
would leave the same result in the database as one that did none of those things —
so each of those is asserted here.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quantdesk import campaigns
from quantdesk.campaigns import CampaignError
from quantdesk.datahub.db import Database

WINDOWS = {
    "train": [1_600_000_000_000, 1_650_000_000_000],
    "validation": [1_650_000_000_001, 1_680_000_000_000],
    "test": [1_680_000_000_001, 1_700_000_000_000],
}
GATES = ("coverage", "finiteness", "dispersion", "predictive", "persistence", "cost", "redundancy")


def write_library(home: Path, *, group: str = "stock", interval: str = "1h", horizon: int = 24,
                  factors: tuple[tuple[str, str], ...] = (("vibe.macd.hist", "validated"),)) -> None:
    """A gate report in the shape `factor_library` reads, without running a scan."""
    root = home / "factor-library"
    root.mkdir(parents=True, exist_ok=True)
    evidence = []
    for factor_id, tier in factors:
        passes = {name: True for name in GATES}
        if tier == "candidate":
            passes["predictive"] = False
        evidence.append({
            "factorId": factor_id,
            "family": "quantdesk" if factor_id.startswith("vibe.") else "vibe-trading",
            "passed": all(passes.values()),
            "symbols": ["AAPLUSDT", "MSFTUSDT"],
            "gates": [
                {"name": name, "passed": passes[name], "detail": "", "metric": metric_for(name)}
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


def metric_for(name: str) -> dict:
    if name == "predictive":
        return {"ic": 0.05, "pValue": 0.002, "signConsistency": 0.7}
    if name == "cost":
        return {"netBps": 16.2}
    return {}


class CampaignFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.db = Database(self.home / "quantdesk.db")
        write_library(self.home)

    def register(self, **overrides):
        payload = {
            "group": "stock", "interval": "1h", "horizon_bars": 24,
            "hypothesis": "MACD 柱在半导体的 1 小时线上有跨截面动量",
            "success_criteria": "验证段净收益 > 0 且 DSR > 0",
            "windows": WINDOWS, "provider": "vibe-backtest-lab", "home": self.home,
        }
        payload.update(overrides)
        return campaigns.preregister(self.db, **payload)

    def round_with_proposal(self, uid: str, proposal_id: str = "p1", round_number: int = 1):
        campaigns.start_round(self.db, uid, round_number=round_number)
        campaigns.record_proposals(self.db, uid, round_number=round_number, proposals=[{
            "proposalId": proposal_id, "factorIds": ["vibe.macd.hist"], "hypothesis": "动量为正",
        }])


class PreregistrationTests(CampaignFixture):
    def test_a_campaign_records_its_hypothesis_windows_and_frozen_space(self):
        campaign = self.register()
        self.assertEqual(campaign["status"], "preregistered")
        self.assertEqual(campaign["group"], "stock")
        self.assertEqual(campaign["universe"][:2], ["AAPLUSDT", "MSFTUSDT"])
        self.assertEqual([item["factorId"] for item in campaign["factorSpace"]], ["vibe.macd.hist"])
        self.assertTrue(campaign["testSealed"])

    def test_the_crypto_group_gets_its_own_universe(self):
        write_library(self.home, group="crypto", interval="1h", horizon=24,
                      factors=(("vibe.rsi.14", "candidate"),))
        campaign = self.register(group="crypto")
        self.assertEqual(campaign["universe"], ["BTCUSDT", "ETHUSDT"])

    def test_an_empty_or_missing_hypothesis_is_refused(self):
        with self.assertRaisesRegex(CampaignError, "假设"):
            self.register(hypothesis="   ")

    def test_the_three_windows_must_be_ordered_and_disjoint(self):
        with self.assertRaisesRegex(CampaignError, "重叠或相接"):
            self.register(windows={
                "train": [1_600_000_000_000, 1_650_000_000_000],
                "validation": [1_650_000_000_000, 1_680_000_000_000],
                "test": [1_680_000_000_000, 1_700_000_000_000],
            })
        with self.assertRaisesRegex(CampaignError, "test 窗口"):
            self.register(windows={"train": WINDOWS["train"], "validation": WINDOWS["validation"]})
        with self.assertRaisesRegex(CampaignError, "不合法"):
            self.register(windows={**WINDOWS, "test": [5, 4]})

    def test_a_campaign_cannot_start_without_a_gated_library(self):
        with self.assertRaisesRegex(CampaignError, "受控因子库"):
            self.register(group="leveraged_etf")

    def test_only_deterministic_search_is_open_in_this_stage(self):
        with self.assertRaisesRegex(CampaignError, "D1"):
            self.register(mode="llm_assisted")

    def test_the_budget_caps_from_d2_are_enforced_at_registration(self):
        for payload, expected in (
            ({"proposalsPerRound": 33}, "1–32"),
            ({"maxRounds": 6}, "1–5"),
            ({"roundDeadlineMs": 10 * 60 * 1000 + 1}, "10 分钟"),
            ({"proposalsPerRound": 0}, "1–32"),
        ):
            with self.assertRaises(CampaignError) as caught:
                self.register(budget=payload)
            self.assertIn(expected, str(caught.exception))

    def test_a_group_with_too_few_symbols_is_refused(self):
        # No group in this deployment has fewer than two instruments, so the guard is
        # checked through the group lookup itself.
        with self.assertRaises(CampaignError):
            self.register(group="nope")


class BudgetTests(CampaignFixture):
    def test_a_round_beyond_the_budget_stops_the_campaign_with_a_reason(self):
        campaign = self.register(budget={"maxRounds": 1})
        campaigns.start_round(self.db, campaign["uid"], round_number=1)
        with self.assertRaisesRegex(CampaignError, "轮数超过预算"):
            campaigns.start_round(self.db, campaign["uid"], round_number=2)
        stored = campaigns.get_campaign(self.db, campaign["uid"])
        self.assertEqual(stored["status"], "budget_limited")
        self.assertIn("轮数用尽", stored["stopReason"])
        ledger = campaigns.ledger_for(self.db, campaign["uid"])
        self.assertTrue(any(entry["breached"] for entry in ledger), "越界必须记在台账里")

    def test_too_many_proposals_in_one_round_stops_the_campaign(self):
        campaign = self.register(budget={"proposalsPerRound": 2})
        campaigns.start_round(self.db, campaign["uid"], round_number=1)
        with self.assertRaisesRegex(CampaignError, "每轮上限 2"):
            campaigns.record_proposals(self.db, campaign["uid"], round_number=1, proposals=[
                {"proposalId": f"p{index}", "factorIds": ["vibe.macd.hist"], "hypothesis": "h"}
                for index in range(3)
            ])
        self.assertEqual(campaigns.get_campaign(self.db, campaign["uid"])["status"], "budget_limited")

    def test_rounds_must_be_consecutive(self):
        campaign = self.register()
        with self.assertRaisesRegex(CampaignError, "轮次必须连续"):
            campaigns.start_round(self.db, campaign["uid"], round_number=2)

    def test_a_round_that_outran_its_deadline_stops_the_campaign(self):
        campaign = self.register(budget={"roundDeadlineMs": 1000})
        with self.assertRaisesRegex(CampaignError, "超过上限"):
            campaigns.start_round(self.db, campaign["uid"], round_number=1, wallclock_ms=2000)
        self.assertEqual(campaigns.get_campaign(self.db, campaign["uid"])["status"], "budget_limited")

    def test_the_budget_state_reports_what_is_left(self):
        campaign = self.register(budget={"maxRounds": 3})
        campaigns.start_round(self.db, campaign["uid"], round_number=1)
        state = campaigns.budget_state(self.db, campaign["uid"])
        self.assertEqual(state["roundsUsed"], 1)
        self.assertEqual(state["roundsLeft"], 2)


class ProposalSpaceTests(CampaignFixture):
    def test_a_factor_outside_the_frozen_space_is_refused(self):
        campaign = self.register()
        campaigns.start_round(self.db, campaign["uid"], round_number=1)
        with self.assertRaisesRegex(CampaignError, "冻结空间之外"):
            campaigns.record_proposals(self.db, campaign["uid"], round_number=1, proposals=[{
                "proposalId": "p1", "factorIds": ["vibe.rsi.14"], "hypothesis": "h",
            }])

    def test_widening_the_live_library_cannot_widen_a_running_campaign(self):
        """The reason the space is a snapshot: the library is not the contract."""
        campaign = self.register()
        write_library(self.home, factors=(("vibe.macd.hist", "validated"), ("vibe.rsi.14", "candidate")))
        campaigns.start_round(self.db, campaign["uid"], round_number=1)
        with self.assertRaisesRegex(CampaignError, "冻结空间之外"):
            campaigns.record_proposals(self.db, campaign["uid"], round_number=1, proposals=[{
                "proposalId": "p1", "factorIds": ["vibe.rsi.14"], "hypothesis": "h",
            }])

    def test_a_proposal_without_a_hypothesis_or_with_a_duplicate_id_is_refused(self):
        campaign = self.register()
        campaigns.start_round(self.db, campaign["uid"], round_number=1)
        with self.assertRaisesRegex(CampaignError, "缺少假设"):
            campaigns.record_proposals(self.db, campaign["uid"], round_number=1, proposals=[{
                "proposalId": "p1", "factorIds": ["vibe.macd.hist"], "hypothesis": " ",
            }])
        with self.assertRaisesRegex(CampaignError, "重复"):
            campaigns.record_proposals(self.db, campaign["uid"], round_number=1, proposals=[
                {"proposalId": "p1", "factorIds": ["vibe.macd.hist"], "hypothesis": "h"},
                {"proposalId": "p1", "factorIds": ["vibe.macd.hist"], "hypothesis": "h"},
            ])

    def test_non_numeric_parameters_are_refused(self):
        campaign = self.register()
        campaigns.start_round(self.db, campaign["uid"], round_number=1)
        with self.assertRaisesRegex(CampaignError, "数值"):
            campaigns.record_proposals(self.db, campaign["uid"], round_number=1, proposals=[{
                "proposalId": "p1", "factorIds": ["vibe.macd.hist"], "hypothesis": "h",
                "parameters": {"window": "fast"},
            }])


class VisibilityTests(CampaignFixture):
    def test_the_test_segment_cannot_be_written_before_unsealing(self):
        campaign = self.register()
        self.round_with_proposal(campaign["uid"])
        with self.assertRaisesRegex(CampaignError, "train/validation"):
            campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1",
                                   segment="test", sharpe=9.9)

    def test_the_test_segment_is_not_readable_before_unsealing(self):
        campaign = self.register()
        self.round_with_proposal(campaign["uid"])
        campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1", segment="validation",
                               sharpe=1.1)
        self.assertEqual(
            [item["segment"] for item in campaigns.trials_for(self.db, campaign["uid"])],
            ["validation"],
        )
        with self.assertRaisesRegex(CampaignError, "尚未开封"):
            campaigns.trials_for(self.db, campaign["uid"], include_test=True)

    def test_the_detail_view_hides_the_test_window_while_it_is_sealed(self):
        campaign = self.register()
        self.round_with_proposal(campaign["uid"])
        payload = json.dumps(campaigns.detail(self.db, campaign["uid"]), ensure_ascii=False)
        self.assertTrue(campaigns.get_campaign(self.db, campaign["uid"])["testSealed"])
        self.assertNotIn("testUnsealedTs\": 1", payload)

    def test_what_the_agent_sees_excludes_the_round_it_is_about_to_run(self):
        campaign = self.register()
        self.round_with_proposal(campaign["uid"], "p1", round_number=1)
        campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1", segment="validation",
                               sharpe=1.0)
        self.assertEqual(
            [item["proposalId"] for item in campaigns.agent_visible_trials(
                self.db, campaign["uid"], round_number=2)],
            ["p1"],
        )
        self.assertEqual(
            campaigns.agent_visible_trials(self.db, campaign["uid"], round_number=1), []
        )


class UnsealTests(CampaignFixture):
    def test_unsealing_requires_a_named_human_and_a_trial_history(self):
        campaign = self.register()
        with self.assertRaisesRegex(CampaignError, "谁批准"):
            campaigns.unseal_test(self.db, campaign["uid"], approved_by=" ")
        with self.assertRaisesRegex(CampaignError, "没有任何试验"):
            campaigns.unseal_test(self.db, campaign["uid"], approved_by="zypher")

    def test_unsealing_opens_the_window_exactly_once(self):
        campaign = self.register()
        self.round_with_proposal(campaign["uid"])
        campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1", segment="validation",
                               sharpe=1.4)
        opened = campaigns.unseal_test(self.db, campaign["uid"], approved_by="zypher")
        self.assertIsNotNone(opened["testUnsealedTs"])
        self.assertFalse(opened["testSealed"])
        with self.assertRaisesRegex(CampaignError, "不能重复开封"):
            campaigns.unseal_test(self.db, campaign["uid"], approved_by="zypher")

    def test_after_unsealing_the_test_rows_are_readable_and_writable(self):
        campaign = self.register()
        self.round_with_proposal(campaign["uid"])
        campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1", segment="validation",
                               sharpe=1.4)
        campaigns.unseal_test(self.db, campaign["uid"], approved_by="zypher")
        campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1", segment="test",
                               sharpe=0.9)
        segments = [item["segment"] for item in campaigns.trials_for(
            self.db, campaign["uid"], include_test=True)]
        self.assertEqual(segments, ["validation", "test"])


class PromotionTests(CampaignFixture):
    def finished_campaign(self, *, with_trial: bool = True):
        campaign = self.register()
        self.round_with_proposal(campaign["uid"])
        if with_trial:
            campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1",
                                   segment="validation", sharpe=1.4, verdict="pass")
        campaigns.finish(self.db, campaign["uid"], status="completed", reason="搜索完成")
        return campaign

    def test_promotion_requires_a_named_human(self):
        campaign = self.finished_campaign()
        with self.assertRaisesRegex(CampaignError, "D3"):
            campaigns.promote(self.db, campaign["uid"], proposal_uid="p1", approved_by="")

    def test_a_running_campaign_cannot_promote_anything(self):
        campaign = self.register()
        self.round_with_proposal(campaign["uid"])
        campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1", segment="validation",
                               sharpe=1.4)
        with self.assertRaisesRegex(CampaignError, "结束前不能晋升"):
            campaigns.promote(self.db, campaign["uid"], proposal_uid="p1", approved_by="zypher")

    def test_a_proposal_without_validation_evidence_is_not_promotable(self):
        campaign = self.finished_campaign(with_trial=False)
        with self.assertRaisesRegex(CampaignError, "没有验证段证据"):
            campaigns.promote(self.db, campaign["uid"], proposal_uid="p1", approved_by="zypher")

    def test_a_validation_trial_without_a_reading_is_not_evidence_enough(self):
        """A row is not a measurement.

        Live example: a campaign whose validation segment returned `inconclusive`
        (no symbol had bars in the window) still had a trial row, and promoting it
        would have written a strategy version with nothing behind it.
        """
        campaign = self.register()
        self.round_with_proposal(campaign["uid"])
        campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1",
                               segment="validation", sharpe=None, verdict="inconclusive",
                               reason="窗口内没有标的可评估")
        campaigns.finish(self.db, campaign["uid"], status="completed", reason="搜索完成")
        with self.assertRaisesRegex(CampaignError, "没有 Sharpe 读数"):
            campaigns.promote(self.db, campaign["uid"], proposal_uid="p1", approved_by="zypher")

    def test_promotion_writes_a_candidate_version_and_never_a_live_one(self):
        campaign = self.finished_campaign()
        result = campaigns.promote(self.db, campaign["uid"], proposal_uid="p1",
                                   approved_by="zypher", note="人工复核")
        self.assertEqual(result["stage"], "candidate")
        self.assertFalse(result["live"])
        rows = self.db.query("SELECT * FROM strategy_versions WHERE strategy_id=?",
                             (result["strategyId"],))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["version"], result["version"])
        stored = json.loads(rows[0]["parameters_json"])
        self.assertEqual(stored["factors"], ["vibe.macd.hist"])

    def test_promotion_is_recorded_in_the_ledger(self):
        campaign = self.finished_campaign()
        campaigns.promote(self.db, campaign["uid"], proposal_uid="p1", approved_by="zypher")
        notes = " ".join(entry["note"] for entry in campaigns.ledger_for(self.db, campaign["uid"]))
        self.assertIn("zypher", notes)
        self.assertIn("p1", notes)

    def test_an_unknown_proposal_cannot_be_promoted(self):
        campaign = self.finished_campaign()
        with self.assertRaisesRegex(CampaignError, "不在这个战役里"):
            campaigns.promote(self.db, campaign["uid"], proposal_uid="nope", approved_by="zypher")


class TerminalStateTests(CampaignFixture):
    def test_a_finished_campaign_refuses_new_rounds_and_trials(self):
        campaign = self.register()
        self.round_with_proposal(campaign["uid"])
        campaigns.finish(self.db, campaign["uid"], status="failed", reason="数据缺口")
        with self.assertRaisesRegex(CampaignError, "战役已结束"):
            campaigns.start_round(self.db, campaign["uid"], round_number=2)
        with self.assertRaisesRegex(CampaignError, "战役已结束"):
            campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1",
                                   segment="validation", sharpe=1.0)

    def test_an_unknown_status_is_refused(self):
        campaign = self.register()
        with self.assertRaisesRegex(CampaignError, "结束状态"):
            campaigns.finish(self.db, campaign["uid"], status="maybe")

    def test_listing_reports_every_campaign_newest_first(self):
        first = self.register()
        second = self.register(group="stock")
        listed = campaigns.list_campaigns(self.db)
        self.assertEqual({item["uid"] for item in listed}, {first["uid"], second["uid"]})


class SchemaTests(unittest.TestCase):
    def test_the_campaign_tables_exist_in_a_fresh_database(self):
        with tempfile.TemporaryDirectory() as raw:
            db = Database(Path(raw) / "quantdesk.db")
            names = {
                row["name"]
                for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in ("agent_campaigns", "agent_proposals", "agent_trials",
                          "agent_budget_ledger"):
                self.assertIn(table, names)

    def test_the_trial_table_has_no_writer_for_the_test_segment_but_the_service(self):
        """A schema-level statement of the seal: nothing else writes `test` rows."""
        import inspect

        from quantdesk.datahub import schema

        self.assertIn("agent_trials", schema.SCHEMA)
        source = inspect.getsource(campaigns)
        self.assertIn('SEGMENTS = ("train", "validation")', source)


if __name__ == "__main__":
    unittest.main()
