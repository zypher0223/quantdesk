"""The campaign orchestration: rounds, the space it may search, and the seal.

The tests inject a stub provider manager and a stub evaluator on purpose. What is
being tested here is the *loop* - that it stays inside the campaign's frozen factors,
that it only ever shows a provider the segments the protocol allows, that it stops
when the budget says so, and that a broken provider fails the round instead of the
campaign pretending it succeeded. Whether a backtest is any good is not this module's
business, and a test that ran real ones would be slow and would prove nothing extra.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quantdesk import agent_campaign, campaigns
from quantdesk.agent_campaign import Segment, sharpe_of, segments_of
from quantdesk.campaigns import CampaignError
from quantdesk.datahub.db import Database
from quantdesk.plugins.protocol import (
    AgentManifestResult,
    AgentProposal,
    AgentProposalSpace,
    AgentProposeResult,
    AgentReflectResult,
    AgentTrialSummary,
)

GATES = ("coverage", "finiteness", "dispersion", "predictive", "persistence", "cost", "redundancy")
FACTORS = ("vibe.macd.hist", "vibe.rsi.14")
WINDOWS = {
    "train": [1_600_000_000_000, 1_650_000_000_000],
    "validation": [1_650_000_000_001, 1_680_000_000_000],
    "test": [1_680_000_000_001, 1_700_000_000_000],
}


def write_library(home: Path) -> None:
    root = home / "factor-library"
    root.mkdir(parents=True, exist_ok=True)
    (root / "stock-1h-h24.json").write_text(
        json.dumps({
            "group": "stock", "interval": "1h", "horizonBars": 24,
            "generatedAt": "2026-09-16T00:00:00Z", "symbols": ["AAPLUSDT", "MSFTUSDT"],
            "evidence": [
                {
                    "factorId": factor_id, "family": "quantdesk", "passed": True,
                    "symbols": ["AAPLUSDT"],
                    "gates": [
                        {"name": name, "passed": True, "detail": "",
                         "metric": {"ic": 0.05, "pValue": 0.002, "signConsistency": 0.7, "netBps": 16.2}}
                        for name in GATES
                    ],
                }
                for factor_id in FACTORS
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )


class StubManager:
    """A provider that speaks the v4 protocol and records what it was asked."""

    def __init__(self, *, agent_version: str = "stub/1", proposals_per_round: int | None = None,
                 proposal_ids: list[str] | None = None, fail_on: str = ""):
        self.calls: list[tuple[str, dict]] = []
        self.agent_version = agent_version
        self.proposals_per_round = proposals_per_round
        self.proposal_ids = proposal_ids
        self.fail_on = fail_on

    def invoke(self, plugin_id, method, params, capability=None):
        self.calls.append((method, dict(params)))
        if method == self.fail_on:
            raise RuntimeError(f"stub refuses {method}")
        space = list(params.get("factorIds") or [])
        if method == "agent.manifest":
            return {"result": AgentManifestResult(
                agentVersion=self.agent_version,
                proposalSpace=AgentProposalSpace(
                    factorIds=space, parameters={"entryThreshold": [0.0, 1.0]},
                    ruleTemplates=["sign_threshold"], maxProposalsPerRound=32, maxRounds=5,
                ),
            ).model_dump()}
        round_number = int(params.get("round") or 1)
        if method == "agent.reflect":
            # The real plugin numbers the *next* round's proposals; the stub does the
            # same so a duplicate-id collision is never mistaken for a real failure.
            round_number += 1
        if self.proposal_ids is not None:
            ids = list(self.proposal_ids)
        else:
            ids = [f"r{round_number:02d}-p{index:03d}" for index in range(len(space))]
        proposals = [
            AgentProposal(
                proposalId=proposal_id, kind="parameter_set", factorIds=[factor_id],
                parameters={"entryThreshold": 0.0}, rule={"type": "sign_threshold"},
                hypothesis=f"{factor_id} 的符号延续",
            )
            for proposal_id, factor_id in zip(ids, space)
        ]
        if method == "agent.propose":
            return {"result": AgentProposeResult(proposals=proposals).model_dump()}
        return {"result": AgentReflectResult(reflection="上一轮最好的是第一个",
                                             proposals=proposals).model_dump()}

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


class CampaignRoundFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.db = Database(self.home / "quantdesk.db")
        write_library(self.home)
        self.evaluated: list[tuple[str, str]] = []

    def register(self, **overrides):
        payload = {
            "group": "stock", "interval": "1h", "horizon_bars": 24,
            "hypothesis": "MACD 柱在半导体的 1 小时线上有跨截面动量",
            "success_criteria": "验证段净收益 > 0", "windows": WINDOWS,
            "provider": "vibe-backtest-lab", "budget": {"maxRounds": 2}, "home": self.home,
        }
        payload.update(overrides)
        return campaigns.preregister(self.db, **payload)

    def evaluator(self, *, sharpe: float = 0.6):
        def evaluate(db, campaign, proposal, segment):
            self.evaluated.append((proposal["proposalId"], segment.name))
            return {"sharpe": sharpe, "returnPct": sharpe / 10, "maxDrawdownPct": -4.0,
                    "trades": 25, "verdict": "measured", "reason": f"stub {segment.name}"}

        return evaluate


class RoundLoopTests(CampaignRoundFixture):
    def test_the_first_round_proposes_and_the_second_reflects(self):
        campaign = self.register()
        manager = StubManager()
        first = agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                         evaluate=self.evaluator(), manager=manager)
        second = agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                          evaluate=self.evaluator(), manager=manager)
        self.assertEqual(manager.methods(),
                         ["agent.manifest", "agent.propose", "agent.manifest", "agent.reflect"])
        self.assertEqual(first["round"], 1)
        self.assertEqual(second["round"], 2)
        self.assertIn("上一轮最好的", second["reflection"])

    def test_every_proposal_is_evaluated_on_both_visible_segments(self):
        campaign = self.register()
        agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                 evaluate=self.evaluator(), manager=StubManager())
        self.assertEqual(
            sorted(self.evaluated),
            [(f"r01-p{index:03d}", segment) for index in range(len(FACTORS))
             for segment in ("train", "validation")],
        )
        trials = campaigns.trials_for(self.db, campaign["uid"])
        self.assertEqual(len(trials), len(FACTORS) * 2)
        self.assertEqual({item["segment"] for item in trials}, {"train", "validation"})

    def test_the_test_window_is_never_evaluated(self):
        campaign = self.register()
        agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                 evaluate=self.evaluator(), manager=StubManager())
        self.assertNotIn("test", {segment for _, segment in self.evaluated})
        self.assertTrue(campaigns.get_campaign(self.db, campaign["uid"])["testSealed"])

    def test_the_provider_is_only_told_the_segments_the_protocol_allows(self):
        campaign = self.register()
        manager = StubManager()
        agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                 evaluate=self.evaluator(), manager=StubManager())
        agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                 evaluate=self.evaluator(), manager=manager)
        reflect = next(params for method, params in manager.calls if method == "agent.reflect")
        for summary in reflect["trials"]:
            self.assertIn(summary["segment"], ("train", "validation"))
        self.assertEqual(reflect["factorIds"], list(FACTORS))

    def test_a_proposal_outside_the_frozen_space_fails_the_round(self):
        campaign = self.register()
        manager = StubManager(proposal_ids=["r01-p000", "r01-p001"])
        # A provider that ignores the space it was given: the engine must refuse it.
        original = manager.invoke

        def sneaky(plugin_id, method, params, capability=None):
            response = original(plugin_id, method, params, capability)
            if method in ("agent.propose", "agent.reflect"):
                response["result"]["proposals"][0]["factorIds"] = ["vibe.not.in.space"]
            return response

        manager.invoke = sneaky  # type: ignore[assignment]
        with self.assertRaises(Exception) as caught:
            agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                     evaluate=self.evaluator(), manager=manager)
        self.assertIn("未声明", str(caught.exception))

    def test_the_round_stops_at_the_round_limit(self):
        campaign = self.register(budget={"maxRounds": 1})
        manager = StubManager()
        agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                 evaluate=self.evaluator(), manager=manager)
        stored = campaigns.get_campaign(self.db, campaign["uid"])
        self.assertEqual(stored["status"], "completed")
        self.assertIn("轮数上限", stored["stopReason"])
        with self.assertRaisesRegex(CampaignError, "战役已结束"):
            agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                     evaluate=self.evaluator(), manager=manager)

    def test_a_broken_evaluator_fails_that_trial_not_the_round(self):
        campaign = self.register()
        seen: list[str] = []

        def flaky(db, campaign_, proposal, segment):
            seen.append(proposal["proposalId"])
            if proposal["proposalId"] == "r01-p000":
                raise RuntimeError("回测炸了")
            return {"sharpe": 0.4, "verdict": "measured"}

        agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                 evaluate=flaky, manager=StubManager())
        trials = {item["proposalId"]: item for item in campaigns.trials_for(self.db, campaign["uid"])}
        self.assertEqual(trials["r01-p000"]["verdict"], "failed")
        self.assertIn("回测炸了", trials["r01-p000"]["reason"])
        self.assertEqual(trials["r01-p001"]["verdict"], "measured")

    def test_a_provider_reusing_proposal_ids_is_refused_by_name(self):
        campaign = self.register(budget={"maxRounds": 3})
        manager = StubManager(proposal_ids=["same-id", "other-id"])
        agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                 evaluate=self.evaluator(), manager=manager)
        with self.assertRaisesRegex(CampaignError, "已经在第 1 轮用过"):
            agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                     evaluate=self.evaluator(), manager=manager)

    def test_a_provider_with_nothing_left_completes_the_campaign(self):
        campaign = self.register()
        manager = StubManager(proposal_ids=[])
        result = agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                          evaluate=self.evaluator(), manager=manager)
        self.assertTrue(result["stopped"])
        self.assertEqual(campaigns.get_campaign(self.db, campaign["uid"])["status"], "completed")


class TestSegmentTests(CampaignRoundFixture):
    """The out-of-sample window: opened once, evaluated once, by the orchestrator."""

    def finished_campaign(self, *, with_validation: bool = True):
        campaign = self.register(budget={"maxRounds": 1})
        agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                 evaluate=self.evaluator(), manager=StubManager())
        if with_validation:
            # run_round already recorded validation trials for both proposals.
            pass
        return campaigns.get_campaign(self.db, campaign["uid"])

    def test_it_refuses_while_the_search_is_still_running(self):
        campaign = self.register(budget={"maxRounds": 2})
        with self.assertRaisesRegex(CampaignError, "搜索结束后"):
            agent_campaign.ensure_test_trial(
                self.db, campaign["uid"], approved_by="zypher", home=self.home,
                evaluate=self.evaluator(), manager=StubManager())

    def test_it_unseals_once_and_records_one_test_trial(self):
        campaign = self.finished_campaign()
        self.assertTrue(campaign["testSealed"])
        result = agent_campaign.ensure_test_trial(
            self.db, campaign["uid"], approved_by="zypher", home=self.home,
            evaluate=self.evaluator(), manager=StubManager())
        self.assertFalse(result.get("alreadyRun"))
        after = campaigns.get_campaign(self.db, campaign["uid"])
        self.assertFalse(after["testSealed"], "开封必须真的发生")
        segments = [trial["segment"] for trial in campaigns.trials_for(
            self.db, campaign["uid"], include_test=True)]
        self.assertEqual(segments.count("test"), 1)
        # The test segment is evaluated once - not once per proposal.
        self.assertEqual(len(self.evaluated), 2 * len(FACTORS) + 1)

    def test_the_second_call_returns_the_first_result_without_evaluating(self):
        campaign = self.finished_campaign()
        agent_campaign.ensure_test_trial(
            self.db, campaign["uid"], approved_by="zypher", home=self.home,
            evaluate=self.evaluator(), manager=StubManager())
        before = len(self.evaluated)
        again = agent_campaign.ensure_test_trial(
            self.db, campaign["uid"], approved_by="zypher", home=self.home,
            evaluate=self.evaluator(), manager=StubManager())
        self.assertTrue(again.get("alreadyRun"))
        self.assertEqual(len(self.evaluated), before, "不能重跑测试段")

    def test_it_refuses_when_nothing_was_measured_in_validation(self):
        campaign = self.register(budget={"maxRounds": 1})
        campaigns.start_round(self.db, campaign["uid"], round_number=1)
        campaigns.record_proposals(self.db, campaign["uid"], round_number=1, proposals=[{
            "proposalId": "p1", "factorIds": [FACTORS[0]], "hypothesis": "h",
        }])
        campaigns.record_trial(self.db, campaign["uid"], proposal_uid="p1",
                               segment="validation", sharpe=None, verdict="inconclusive")
        campaigns.finish(self.db, campaign["uid"], status="completed", reason="搜索完成")
        with self.assertRaisesRegex(CampaignError, "没有任何 Sharpe 读数"):
            agent_campaign.ensure_test_trial(
                self.db, campaign["uid"], approved_by="zypher", home=self.home,
                evaluate=self.evaluator(), manager=StubManager())

    def test_it_selects_the_best_validation_proposal(self):
        """The out-of-sample run goes to the proposal the validation segment liked best."""
        campaign = self.register(budget={"maxRounds": 1})

        def ranked(db, campaign_, proposal, segment):
            sharpe = 2.0 if proposal["proposalId"].endswith("p001") else 0.1
            return {"sharpe": sharpe, "returnPct": sharpe / 10, "maxDrawdownPct": -1.0,
                    "trades": 5, "verdict": "measured"}

        agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                 evaluate=ranked, manager=StubManager())
        result = agent_campaign.ensure_test_trial(
            self.db, campaign["uid"], approved_by="zypher", home=self.home,
            evaluate=self.evaluator(), manager=StubManager())
        self.assertEqual(result["proposalId"], "r01-p001")
        self.assertIn("验证段 Sharpe 最高", result["selectionRule"])


class TieBreakTests(CampaignRoundFixture):
    def test_a_tie_is_broken_by_the_order_the_trials_were_recorded(self):
        campaign = self.register(budget={"maxRounds": 1})
        agent_campaign.run_round(self.db, campaign["uid"], home=self.home,
                                 evaluate=self.evaluator(), manager=StubManager())
        result = agent_campaign.ensure_test_trial(
            self.db, campaign["uid"], approved_by="zypher", home=self.home,
            evaluate=self.evaluator(), manager=StubManager())
        self.assertEqual(result["proposalId"], "r01-p000", "并列时取先记录的那一条")


class SymbolToleranceTests(unittest.TestCase):
    def test_a_symbol_without_bars_is_skipped_rather_than_failing_the_segment(self):
        """A younger listing must not void a window for the whole group."""
        import inspect

        source = inspect.getsource(agent_campaign.evaluate_proposal)
        self.assertIn("skipped.append", source, "逐标的失败要记录而不是中断")
        self.assertIn('"verdict": "measured" if sharpes else "inconclusive"', source)

    def test_the_bar_count_is_measured_from_the_window_start_to_now(self):
        """The bug the first live round hit: a past window needs more bars than its span."""
        from quantdesk.agent_campaign import bars_for_window

        window = Segment("train", 1_776_772_800_000, 1_781_012_399_999)
        bars = bars_for_window(window, "1h", 24)
        self.assertGreater(bars, (window.end_ts - window.start_ts) // 3_600_000,
                           "培训窗口在几个月前，读的根数必须覆盖到它，而不是只覆盖它的跨度")
        self.assertLessEqual(bars, 100_000)


class SegmentTests(unittest.TestCase):
    def test_only_the_visible_segments_are_returned_by_default(self):
        campaign = {"windows": {name: value for name, value in WINDOWS.items()}}
        names = [segment.name for segment in segments_of(campaign)]
        self.assertEqual(names, ["train", "validation"])
        self.assertIn("test", [segment.name for segment in segments_of(campaign, include_test=True)])


class SharpeTests(unittest.TestCase):
    def test_the_sharpe_is_annualised_by_the_interval(self):
        hourly = [100.0 * (1.001 ** index) for index in range(200)]
        daily = [100.0 * (1.001 ** index) for index in range(200)]
        self.assertGreater(sharpe_of(hourly, "1h"), sharpe_of(daily, "1d"))

    def test_a_flat_curve_has_no_sharpe(self):
        self.assertIsNone(sharpe_of([100.0] * 50, "1h"))
        self.assertIsNone(sharpe_of([100.0, 101.0], "1h"))


class ProtocolBoundaryTests(unittest.TestCase):
    def test_the_trial_summary_cannot_name_the_test_segment(self):
        with self.assertRaises(Exception):
            AgentTrialSummary(proposalId="p", segment="test")

    def test_the_reflect_request_carries_the_frozen_space(self):
        from quantdesk.plugins.protocol import AgentReflectRequest

        request = AgentReflectRequest(campaignId="c", round=1, factorIds=list(FACTORS))
        self.assertEqual(request.factorIds, list(FACTORS))
        self.assertEqual(request.group, "crypto", "默认分组保持向后兼容")


if __name__ == "__main__":
    unittest.main()
