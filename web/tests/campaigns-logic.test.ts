/**
 * The campaign panel's rules, without a DOM.
 *
 * What matters here is the same thing the server guards: a reader must not be able to
 * see the sealed test segment, and an operator must not be offered a promotion the
 * engine will refuse. Both are decided by the pure functions below, so the page and
 * these tests cannot disagree about them.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  budgetView,
  statisticsRows,
  topSelection,
  verdictView,
  factorUsage,
  ledgerRows,
  promotionState,
  reportHead,
  sharpeSeries,
  statusLabel,
  statusTone,
  trialCells,
  trialsByProposal,
  visibleTrials,
  type Campaign,
  type CampaignDetail,
} from "../src/services/campaigns.ts";

function campaign(overrides: Partial<Campaign> = {}): Campaign {
  return {
    uid: "camp-1",
    provider: "vibe-backtest-lab",
    agentVersion: "vibe-lab-agent/1",
    mode: "deterministic_search",
    group: "stock",
    interval: "1h",
    horizonBars: 24,
    universe: ["AAPLUSDT", "MSFTUSDT"],
    factorSpace: [{ factorId: "vibe.macd.hist", tier: "validated", family: "quantdesk" }],
    hypothesis: "MACD 柱有跨截面动量",
    successCriteria: "验证段 Sharpe > 0",
    windows: {
      train: [1, 2],
      validation: [3, 4],
      test: [5, 6],
    },
    testSealed: true,
    testUnsealedTs: null,
    budget: { proposalsPerRound: 2, maxRounds: 2, roundDeadlineMs: 600_000 },
    status: "completed",
    stopReason: "已达轮数上限 2",
    roundsUsed: 2,
    trialsUsed: 4,
    proposalsUsed: 4,
    seed: 42,
    createdTs: 1,
    finishedTs: 2,
    ...overrides,
  };
}

function detail(overrides: Partial<CampaignDetail> = {}): CampaignDetail {
  const base: CampaignDetail = {
    campaign: campaign(),
    budgetState: { roundsUsed: 2, roundsLeft: 0, proposalsUsed: 4, trialsUsed: 4 },
    proposals: [
      {
        proposalId: "r01-p000",
        round: 1,
        kind: "parameter_set",
        factorIds: ["vibe.macd.hist"],
        parameters: { entryThreshold: 0 },
        hypothesis: "延续",
        expectedFailureMode: "成本",
        status: "evaluated",
        rejectReason: "",
      },
      {
        proposalId: "r02-p000",
        round: 2,
        kind: "parameter_set",
        factorIds: ["vibe.macd.hist", "vibe.rsi.14"],
        parameters: { entryThreshold: 0.5 },
        hypothesis: "收敛到最好者附近",
        expectedFailureMode: "过拟合",
        status: "evaluated",
        rejectReason: "",
      },
    ],
    trials: [
      { proposalId: "r01-p000", segment: "train", sharpe: -4.34, returnPct: -15.04, maxDrawdownPct: 22.1, trades: 631, verdict: "measured", reason: "" },
      { proposalId: "r01-p000", segment: "validation", sharpe: -3.06, returnPct: -19.21, maxDrawdownPct: 21.2, trades: 978, verdict: "measured", reason: "" },
      { proposalId: "r02-p000", segment: "validation", sharpe: null, returnPct: null, maxDrawdownPct: null, trades: null, verdict: "inconclusive", reason: "窗口内没有标的" },
    ],
    ledger: [
      { round: 0, entry: "rounds", amount: 0, limit: 2, breached: false, note: "预注册", createdTs: 1 },
      { round: 1, entry: "proposals", amount: 2, limit: 2, breached: false, note: "入库", createdTs: 2 },
      { round: 2, entry: "proposals", amount: 3, limit: 2, breached: true, note: "超预算", createdTs: 3 },
    ],
  };
  return { ...base, ...overrides };
}

describe("the sealed test segment", () => {
  it("is not visible while the campaign says it is sealed", () => {
    const withTest = detail({
      trials: [
        ...detail().trials,
        { proposalId: "r01-p000", segment: "test", sharpe: 9.9, returnPct: 99, maxDrawdownPct: 1, trades: 3, verdict: "measured", reason: "" },
      ],
    });
    assert.equal(visibleTrials(withTest).some((trial) => trial.segment === "test"), false);
    assert.equal(trialsByProposal(withTest).get("r01-p000")?.length, 2);
  });

  it("becomes visible only after the campaign records an unsealing", () => {
    const unsealed = detail({
      campaign: campaign({ testSealed: false, testUnsealedTs: 1_700_000_000_000 }),
      trials: [
        ...detail().trials,
        { proposalId: "r01-p000", segment: "test", sharpe: 0.4, returnPct: 2, maxDrawdownPct: 3, trades: 40, verdict: "measured", reason: "" },
      ],
    });
    assert.equal(visibleTrials(unsealed).some((trial) => trial.segment === "test"), true);
  });

  it("stays hidden when the flag and the timestamp disagree", () => {
    // Belt and braces: a payload claiming "not sealed" without an unsealing time is
    // treated as sealed, because the timestamp is the record of the act.
    const inconsistent = detail({ campaign: campaign({ testSealed: false, testUnsealedTs: null }) });
    assert.equal(visibleTrials(inconsistent).some((trial) => trial.segment === "test"), false);
  });
});

describe("promotion eligibility", () => {
  it("requires a named human, including the empty string", () => {
    // The empty case is the one a page gets wrong: a component that substitutes a
    // placeholder for an empty name ends up offering an action the server rejects.
    for (const name of ["", "   ", "\n"]) {
      const state = promotionState(campaign(), "r01-p000", detail(), name);
      assert.equal(state.eligible, false, JSON.stringify(name));
      assert.match(state.reason, /批准人/);
    }
  });

  it("requires a finished campaign", () => {
    const state = promotionState(campaign({ status: "running" }), "r01-p000", detail(), "zypher");
    assert.equal(state.eligible, false);
    assert.match(state.reason, /结束前不能晋升/);
  });

  it("requires a validation reading, not just a row", () => {
    const state = promotionState(campaign(), "r02-p000", detail(), "zypher");
    assert.equal(state.eligible, false);
    assert.match(state.reason, /没有 Sharpe 读数/);
    assert.match(state.reason, /inconclusive/);
  });

  it("allows a proposal that was actually measured", () => {
    const state = promotionState(campaign(), "r01-p000", detail(), "zypher");
    assert.equal(state.eligible, true);
  });
});

describe("budget and ledger", () => {
  it("counts breaches and derives the proposal ceiling from the round limit", () => {
    const view = budgetView(detail());
    assert.equal(view.roundsLimit, 2);
    assert.equal(view.proposalsLimit, 4);
    assert.equal(view.breaches, 1);
    assert.equal(view.roundsLeft, 0);
  });

  it("prints a percentage of the limit, and nothing when the entry has no limit", () => {
    // A `trials` entry records an observation, not a budget line, so the service
    // stores limit 0 for it - and a percentage of zero is unknown, not 0%.
    const withObservation = detail({
      ledger: [
        ...detail().ledger,
        { round: 1, entry: "trials", amount: 1, limit: 0, breached: false, note: "试验", createdTs: 4 },
      ],
    });
    const rows = ledgerRows(withObservation);
    assert.equal(rows[1].usedPct, 100);
    assert.equal(rows[0].usedPct, 0, "预注册那一行的上限是轮数上限，已用 0 轮");
    assert.equal(rows[3].usedPct, null, "没有上限的条目不给百分比");
  });
});

describe("figures", () => {
  it("builds one series per visible segment", () => {
    const { proposals, series } = sharpeSeries(detail());
    assert.deepEqual(proposals, ["r01-p000", "r02-p000"]);
    assert.deepEqual(Object.keys(series).sort(), ["train", "validation"]);
    assert.equal(series.validation.length, 2);
  });

  it("counts how often each factor was proposed", () => {
    assert.deepEqual(factorUsage(detail()), [
      { factorId: "vibe.macd.hist", count: 2 },
      { factorId: "vibe.rsi.14", count: 1 },
    ]);
  });

  it("prints an unmeasured Sharpe as a dash, never a zero", () => {
    const cells = trialCells(detail().trials[2]);
    assert.deepEqual(cells, ["r02-p000", "validation", "—", "—", "—", "—"]);
  });

  it("says a campaign has no measurable trials instead of implying a result", () => {
    const head = reportHead(detail({ trials: [detail().trials[2]] }));
    assert.match(head.headline, /没有可测量的试验/);
    assert.match(head.detail, /算不出来/);
  });

  it("reports the best and worst readings of a measured campaign", () => {
    const head = reportHead(detail());
    assert.match(head.headline, /r01-p000/);
    assert.match(head.detail, /测试段仍封存/);
    assert.equal(head.tone, "positive");
  });
});

describe("statistics and the verdict", () => {
  const available = {
    campaign: "camp-1",
    trials: 6,
    deflatedSharpe: {
      available: true,
      deflatedSharpe: 0.1353,
      expectedMaxSharpe: 0.0183,
      sharpeSpread: 0.0093,
      trials: 6,
      method: "bailey-lopez-de-prado-2014-dsr",
      approximations: [],
    },
    pbo: {
      available: true,
      pbo: 0.5714,
      method: "cscv-proposal-dimension",
      selectionFrequency: { "r01-p000": 0.9857, "r01-p001": 0.0143 },
    },
  };

  it("prints DSR and PBO with their method and the trial count", () => {
    const rows = statisticsRows(available);
    assert.equal(rows.length, 3);
    assert.equal(rows[0].value, "6");
    assert.equal(rows[1].value, "0.1353");
    assert.match(rows[1].note, /bailey-lopez-de-prado-2014-dsr/);
    assert.equal(rows[2].value, "0.5714");
    assert.match(rows[2].note, /r01-p000（99%）/);
  });

  it("prints the reason instead of a zero when a statistic is unavailable", () => {
    const rows = statisticsRows({
      campaign: "camp-1",
      trials: 2,
      deflatedSharpe: { available: false, deflatedSharpe: null, expectedMaxSharpe: null, sharpeSpread: null, trials: 0, reason: "没有试验次数 N" },
      pbo: { available: false, pbo: null, reason: "可用收益序列的提案只有 0 个" },
    });
    assert.equal(rows[1].value, "—");
    assert.match(rows[1].note, /没有试验次数 N/);
    assert.equal(rows[2].value, "—");
    assert.match(rows[2].note, /只有 0 个/);
  });

  it("says a verdict was not reached while the test segment is sealed", () => {
    const view = verdictView({ judged: false, campaign: "camp-1", testSealed: true, reason: "测试段尚未开封" });
    assert.equal(view.label, "测试段封存");
    assert.match(view.headline, /未开封/);
  });

  it("tones a failed verdict as a result rather than an error", () => {
    const view = verdictView({
      judged: true, campaign: "camp-1", testSealed: false, verdict: "fail",
      sharpe: -5.0239, deflatedSharpe: 0.0078, pbo: 0.5714, trials: 6,
      criteria: "testSharpe >= 0 且 pbo <= 0.5", approvedBy: "zypher",
      reason: "testSharpe >= 0 不满足（实际 -5.02386）",
    });
    assert.equal(view.label, "未通过");
    assert.equal(view.tone, "negative");
    assert.match(view.headline, /-5.0239/);
    assert.match(view.detail, /zypher/);
  });

  it("calls an unmeasurable verdict inconclusive, never a pass", () => {
    const view = verdictView({ judged: true, campaign: "camp-1", testSealed: false, verdict: "inconclusive", reason: "没有可用序列" });
    assert.equal(view.label, "证据不足");
    assert.equal(view.tone, "warning");
  });

  it("names the most frequently selected proposal, or nothing", () => {
    assert.equal(topSelection(undefined), "—");
    assert.equal(topSelection({ a: 0.2, b: 0.8 }), "b（80%）");
  });
});

describe("status labels", () => {
  it("names every terminal state in Chinese", () => {
    for (const status of ["preregistered", "running", "completed", "budget_limited", "compliance_blocked", "failed"]) {
      assert.notEqual(statusLabel(status), status);
    }
  });

  it("tones a budget stop as a warning rather than a failure", () => {
    assert.equal(statusTone("budget_limited"), "warning");
    assert.equal(statusTone("failed"), "negative");
    assert.equal(statusTone("completed"), "positive");
  });
});
