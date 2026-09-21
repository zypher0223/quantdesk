/**
 * The factor library's pure logic.
 *
 * These helpers decide what the factor panel claims: which Chinese label a
 * verdict kind or a factor family gets, and — most importantly — how much of a
 * window a factor actually covered. A count the run did not report must stay
 * unknown so the table can print "—"; a fabricated 0% would read as a measured
 * result. Tested without a DOM, like the result centre's logic.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  computeFactors,
  factorCoveragePct,
  factorFamilyLabel,
  fetchFactorCatalog,
  fetchFactorRun,
  fetchFactorRuns,
  providerLabel,
  summariseCoverage,
  topFactorsByCoverage,
  validateRun,
  validationNotes,
  verdictKindLabel,
  type ValidationAnalysis,
  type ValidationReport,
} from "../src/services/factors.ts";

/* ------------------------------------------------------------------- stubs */

interface FetchCall {
  url: string;
  init: RequestInit | undefined;
}

function stubFetch(reply: (call: FetchCall) => { status?: number; body: unknown }): { calls: FetchCall[]; restore: () => void } {
  const original = globalThis.fetch;
  const calls: FetchCall[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const call: FetchCall = { url: String(input), init };
    calls.push(call);
    const { status = 200, body } = reply(call);
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  return { calls, restore: () => { globalThis.fetch = original; } };
}

function bodyOf(call: FetchCall): Record<string, unknown> {
  return JSON.parse(String(call.init?.body ?? "{}")) as Record<string, unknown>;
}

/** The live validator envelope, trimmed to the blocks the page reads. */
const VALIDATION_ENVELOPE = {
  available: true,
  provider: "vibe-factors",
  runId: 13,
  analysis: {
    provider: "vibe-factors",
    runId: "13",
    algorithmVersion: "vibe/1",
    seed: 42,
    samples: 1977,
    multipleTesting: {
      trials: 9,
      factorCount: 0,
      parameterCombinations: 9,
      deflatedSharpe: null,
      probabilityOfBacktestOverfitting: null,
      method: "deflated_sharpe + cscv_pbo",
      applied: false,
      note: "缺少多次尝试的 Sharpe 离散度，未做 Deflated Sharpe",
    },
    warnings: [],
    unavailable: "",
    pathRisk: { simulations: 400 },
    bootstrap: { resamples: 400 },
    randomization: { permutations: 400, pValue: 0.5187032419 },
    tailLoss: { low: -0.0087, high: -0.0026 },
    equityPercentiles: { p05: [84.99], p95: [110.8] },
  },
  verdicts: [
    { kind: "bootstrap", verdict: "warn", statistic: 0.3125, pValue: null, threshold: null, provider: "vibe-factors", detail: "移动分块 bootstrap：正 Sharpe 概率 31.2%" },
    { kind: "randomization", verdict: "fail", statistic: -0.695, pValue: 0.5187, threshold: 0.05, provider: "vibe-factors", detail: "信号随机化：p = 0.5187" },
    { kind: "path_risk", verdict: "info", statistic: 0.0, pValue: null, threshold: null, provider: "vibe-factors", detail: "路径风险：只说明顺序敏感度" },
  ],
};

/* ------------------------------------------------------------------ labels */

test("every verdict kind the engine and the validator emit is labelled in Chinese", () => {
  assert.equal(verdictKindLabel("walk_forward"), "滚动窗口验证");
  assert.equal(verdictKindLabel("walk_forward_stability"), "窗口参数稳定性");
  assert.equal(verdictKindLabel("leakage"), "未来数据检查");
  assert.equal(verdictKindLabel("overfit"), "过拟合检查");
  assert.equal(verdictKindLabel("bootstrap"), "自助重采样");
  assert.equal(verdictKindLabel("randomization"), "信号随机化");
  assert.equal(verdictKindLabel("deflated_sharpe"), "去偏夏普");
  assert.equal(verdictKindLabel("pbo"), "回测过拟合概率");
  assert.equal(verdictKindLabel("path_risk"), "路径风险");
});

test("an unknown verdict kind is shown raw, never relabelled", () => {
  assert.equal(verdictKindLabel("regime_shift"), "regime_shift");
  assert.equal(verdictKindLabel(""), "—");
});

test("factor families the provider ships are labelled and unknown ones pass through", () => {
  assert.equal(factorFamilyLabel("momentum"), "动量");
  assert.equal(factorFamilyLabel("trend"), "趋势");
  assert.equal(factorFamilyLabel("volatility"), "波动率");
  assert.equal(factorFamilyLabel("distribution"), "收益分布");
  assert.equal(factorFamilyLabel("oscillator"), "摆动指标");
  assert.equal(factorFamilyLabel("liquidity"), "流动性");
  assert.equal(factorFamilyLabel("structure"), "市场结构");
  assert.equal(factorFamilyLabel("carry"), "资金费收益");
  assert.equal(factorFamilyLabel("positioning"), "持仓结构");
  assert.equal(factorFamilyLabel("MOMENTUM"), "动量", "the token is matched case-insensitively");
  assert.equal(factorFamilyLabel("sentiment"), "sentiment", "a new plugin family is not guessed at");
  assert.equal(factorFamilyLabel(""), "—");
});

/* ---------------------------------------------------------------- coverage */

test("coverage rows keep the engine's order and report the share of the window", () => {
  const rows = summariseCoverage({ "vibe.rsi.14": 800, "vibe.atr.14": 200 }, 1000);
  assert.deepEqual(rows, [
    { factorId: "vibe.rsi.14", covered: 800, bars: 1000, pct: 80 },
    { factorId: "vibe.atr.14", covered: 200, bars: 1000, pct: 20 },
  ]);
});

test("coverage percentage is 0 when bars is 0, on both readings", () => {
  const rows = summariseCoverage({ "vibe.rsi.14": 0 }, 0);
  assert.deepEqual(rows, [{ factorId: "vibe.rsi.14", covered: 0, bars: 0, pct: 0 }]);
  assert.equal(factorCoveragePct({ coverage: { "vibe.rsi.14": 0 }, bars: 0 }), 0);
  assert.equal(factorCoveragePct({ coverage: { "vibe.rsi.14": 900 }, bars: 0 }), 0,
    "no window was read, so nothing is covered");
  assert.equal(summariseCoverage({ "vibe.rsi.14": 900 }, -5)[0].pct, 0);
  assert.equal(summariseCoverage({ "vibe.rsi.14": 900 }, Number.NaN)[0].pct, 0);
});

test("a count the run did not report stays unknown instead of becoming zero", () => {
  const rows = summariseCoverage({ a: null as unknown as number, b: -1, c: Number.NaN, d: 100 }, 400);
  assert.deepEqual(rows.map((row) => row.covered), [null, null, null, 100]);
  assert.deepEqual(rows.map((row) => row.pct), [null, null, null, 25]);
});

test("coverage never exceeds the window, and a missing record is not a factor", () => {
  assert.equal(summariseCoverage({ a: 5000 }, 1000)[0].pct, 100);
  assert.deepEqual(summariseCoverage(null, 1000), []);
  assert.deepEqual(summariseCoverage(undefined, 1000), []);
  assert.deepEqual(summariseCoverage({}, 1000), []);
  assert.deepEqual(summariseCoverage([1, 2] as unknown as Record<string, number>, 1000), []);
});

test("one number summarises a run's coverage, and it is unknown when nothing was reported", () => {
  assert.equal(factorCoveragePct({ coverage: { a: 400, b: 600 }, bars: 1000 }), 50);
  assert.equal(factorCoveragePct({ coverage: { a: 1000 }, bars: 1000 }), 100);
  assert.equal(factorCoveragePct({ coverage: {}, bars: 1000 }), null,
    "no factors were reported, which is not the same as 0% covered");
  assert.equal(factorCoveragePct(null), 0, "no run at all read no bars");
  assert.equal(factorCoveragePct(undefined), 0);
  assert.equal(factorCoveragePct({ coverage: { a: 400, b: null as unknown as number }, bars: 1000 }), null,
    "an unknown count would understate the total, so the total stays unknown");
});

test("top factors are ordered by coverage, ties keep the engine's order", () => {
  const run = { coverage: { a: 100, b: 900, c: 100, d: 500 }, bars: 1000 };
  assert.deepEqual(topFactorsByCoverage(run, 3).map((row) => row.factorId), ["b", "d", "a"]);
  assert.deepEqual(topFactorsByCoverage(run, 4).map((row) => row.pct), [90, 50, 10, 10],
    "a tie keeps the order the engine reported");
  assert.deepEqual(topFactorsByCoverage(run, 2).map((row) => row.factorId), ["b", "d"]);
});

test("top factors never invent a factor and never pad a short list", () => {
  const run = { coverage: { "vibe.rsi.14": 500 }, bars: 1000 };
  const rows = topFactorsByCoverage(run, 5);
  assert.equal(rows.length, 1, "there is no sixth factor to pad the list with");
  assert.deepEqual(rows.map((row) => row.factorId), ["vibe.rsi.14"]);
  assert.deepEqual(topFactorsByCoverage({ coverage: {}, bars: 1000 }, 3), []);
  assert.deepEqual(topFactorsByCoverage(null, 3), []);
});

test("an unknown coverage sorts last and a nonsensical limit asks for nothing", () => {
  const run = { coverage: { a: null as unknown as number, b: 100 }, bars: 1000 };
  assert.deepEqual(topFactorsByCoverage(run, 2).map((row) => row.factorId), ["b", "a"]);
  assert.deepEqual(topFactorsByCoverage(run, 0), []);
  assert.deepEqual(topFactorsByCoverage(run, -1), []);
  assert.deepEqual(topFactorsByCoverage(run, Number.NaN), []);
});

/* ------------------------------------------------------- validator notes */

function analysis(patch: Partial<ValidationAnalysis> = {}): ValidationAnalysis {
  return {
    provider: "vibe-factors",
    runId: "13",
    algorithmVersion: "vibe/1",
    seed: 42,
    samples: 1977,
    multipleTesting: null,
    warnings: [],
    unavailable: "",
    pathRisk: null,
    bootstrap: null,
    randomization: null,
    tailLoss: null,
    equityPercentiles: {},
    ...patch,
  };
}

test("the validator's refusal to correct for multiple testing is shown, in its own words", () => {
  const notes = validationNotes(analysis({
    multipleTesting: {
      trials: 9, factorCount: 0, parameterCombinations: 9, deflatedSharpe: null,
      probabilityOfBacktestOverfitting: null, method: "deflated_sharpe + cscv_pbo",
      applied: false, note: "缺少多次尝试的 Sharpe 离散度，未做 Deflated Sharpe",
    },
  }));
  assert.deepEqual(notes, ["缺少多次尝试的 Sharpe 离散度，未做 Deflated Sharpe"]);
});

test("a refusal without a note still says so, and an applied correction says nothing", () => {
  const silent = analysis({
    multipleTesting: {
      trials: 9, factorCount: 4, parameterCombinations: 9, deflatedSharpe: 0.4,
      probabilityOfBacktestOverfitting: 0.6, method: "deflated_sharpe + cscv_pbo", applied: false, note: "",
    },
  });
  assert.deepEqual(validationNotes(silent), ["验证器未应用多重检验校正，且未说明原因。"]);
  const applied = analysis({
    multipleTesting: {
      trials: 9, factorCount: 4, parameterCombinations: 9, deflatedSharpe: 0.4,
      probabilityOfBacktestOverfitting: 0.6, method: "deflated_sharpe + cscv_pbo", applied: true, note: "已校正",
    },
  });
  assert.deepEqual(validationNotes(applied), [], "an applied correction is not a caveat");
});

test("unavailable text and warnings travel with the analysis, once each", () => {
  const notes = validationNotes(analysis({
    unavailable: "路径风险需要交易记录，该结果没有交易",
    warnings: ["样本不足，尾部损失区间偏宽", "样本不足，尾部损失区间偏宽"],
  }));
  assert.deepEqual(notes, ["路径风险需要交易记录，该结果没有交易", "样本不足，尾部损失区间偏宽"]);
  assert.deepEqual(validationNotes(null), []);
  assert.deepEqual(validationNotes(undefined), []);
});

/* --------------------------------------------------- response shapes (fetch) */

test("validateRun posts to the run and unwraps the {available, verdicts, analysis} envelope", async () => {
  const stub = stubFetch(() => ({ body: VALIDATION_ENVELOPE }));
  try {
    const report: ValidationReport = await validateRun(13, { seed: 11 });
    assert.equal(stub.calls[0].url, "/api/factors/validate/13");
    assert.equal(stub.calls[0].init?.method, "POST");
    assert.deepEqual(bodyOf(stub.calls[0]), { seed: 11 });
    assert.equal(report.available, true);
    assert.equal(report.provider, "vibe-factors");
    assert.equal(report.runId, 13);
    assert.equal(report.verdicts.length, 3, "结论行来自 verdicts，而不是外壳");
    assert.equal(report.verdicts[0].kind, "bootstrap");
    assert.equal(report.verdicts[0].statistic, 0.3125);
    assert.equal(report.verdicts[2].verdict, "info");
    assert.equal(report.analysis?.multipleTesting?.applied, false, "analysis 必须被解开，页面要读它的自述");
    assert.equal(report.analysis?.multipleTesting?.note, "缺少多次尝试的 Sharpe 离散度，未做 Deflated Sharpe");
    assert.equal(report.analysis?.seed, 42);
    assert.deepEqual(validationNotes(report.analysis), ["缺少多次尝试的 Sharpe 离散度，未做 Deflated Sharpe"]);
  } finally {
    stub.restore();
  }
});

test("validateRun sends no seed when none was asked for, and refuses a shapeless envelope", async () => {
  const stub = stubFetch(() => ({ body: VALIDATION_ENVELOPE }));
  try {
    await validateRun(13);
    assert.deepEqual(bodyOf(stub.calls[0]), {}, "the engine's own default seed applies");
  } finally {
    stub.restore();
  }

  const broken = stubFetch(() => ({ body: { available: true, analysis: {} } }));
  try {
    await assert.rejects(() => validateRun(13), /verdicts/);
  } finally {
    broken.restore();
  }
});

test("a 409 with a plain sentence is shown as that sentence", async () => {
  const stub = stubFetch(() => ({
    status: 409,
    body: { detail: "没有启用的统计验证插件（capability: backtest_validator）" },
  }));
  try {
    await assert.rejects(() => validateRun(4), {
      message: "没有启用的统计验证插件（capability: backtest_validator）",
    });
  } finally {
    stub.restore();
  }
});

test("a 409 whose detail is a JSON document is joined into one sentence", async () => {
  const stub = stubFetch(() => ({
    status: 409,
    body: { detail: JSON.stringify({ title: "数据未就绪", detail: "该结果没有交易记录", action: "先补足历史数据" }) },
  }));
  try {
    await assert.rejects(() => validateRun(4), { message: "数据未就绪：该结果没有交易记录：先补足历史数据" });
  } finally {
    stub.restore();
  }
});

test("computeFactors posts the window and omits an empty factor list", async () => {
  const computed = {
    available: true, provider: "vibe-factors", runId: 3, symbol: "BTCUSDT", interval: "1h",
    snapshotHash: "f2ebc6ce3d66d687", batches: 6, bars: 3000,
    series: [{ factorId: "vibe.rsi.14", values: [{ time: 1, value: 55.2 }, { time: 2, value: null }] }],
    coverage: { "vibe.rsi.14": 2900 }, warnings: ["分 6 批计算（插件单次输出上限 1MB）"], provenance: { version: "v1" },
  };
  const all = stubFetch(() => ({ body: computed }));
  try {
    const outcome = await computeFactors({ symbol: " BTCUSDT ", interval: "1h", bars: 3000 });
    assert.equal(all.calls[0].url, "/api/factors/compute");
    assert.deepEqual(bodyOf(all.calls[0]), { symbol: "BTCUSDT", interval: "1h", bars: 3000 });
    assert.equal(outcome.queued, false, "能同步回答的窗口不进入后台");
    if (outcome.queued) throw new Error("同步窗口不应被转入后台");
    const result = outcome.study;
    assert.equal(result.runId, 3);
    assert.equal(result.batches, 6);
    assert.deepEqual(result.coverage, { "vibe.rsi.14": 2900 });
    assert.deepEqual(result.series[0].values, [{ time: 1, value: 55.2 }, { time: 2, value: null }],
      "a factor value the plugin did not produce stays null");
  } finally {
    all.restore();
  }

  const picked = stubFetch(() => ({ body: computed }));
  try {
    await computeFactors({ symbol: "BTCUSDT", interval: "4h", bars: 500, factorIds: ["vibe.rsi.14"] });
    assert.deepEqual(bodyOf(picked.calls[0]), { symbol: "BTCUSDT", interval: "4h", bars: 500, factorIds: ["vibe.rsi.14"] });
  } finally {
    picked.restore();
  }
});

test("a compute without a runId is refused instead of being labelled run #0", async () => {
  const stub = stubFetch(() => ({ body: { available: true, symbol: "BTCUSDT" } }));
  try {
    await assert.rejects(() => computeFactors({ symbol: "BTCUSDT", interval: "1h", bars: 100 }), /runId/);
  } finally {
    stub.restore();
  }
});

test("an over-budget compute comes back as a queued transfer, not as coverage", async () => {
  const stub = stubFetch(() => ({
    status: 202,
    body: {
      queued: true,
      run: { id: 41, kind: "factors", status: "queued", label: "因子计算 BTCUSDT 1h", progress: 0 },
      reason: "该研究预计 600,000 单位工作量，超过同步上限 60,000 单位",
      syncCost: 600_000,
      syncBudget: 60_000,
    },
  }));
  try {
    const outcome = await computeFactors({ symbol: "BTCUSDT", interval: "1h", bars: 20_000 });
    assert.equal(outcome.queued, true);
    if (!outcome.queued) throw new Error("202 必须被读成转入后台");
    assert.equal(outcome.run.id, 41);
    assert.equal(outcome.run.kind, "factors");
    assert.equal(outcome.syncCost, 600_000);
    assert.equal(outcome.syncBudget, 60_000);
    assert.ok(!("study" in outcome), "转入后台的结果里没有可渲染的覆盖率");
  } finally {
    stub.restore();
  }
});

test("the catalogue is asked for a refresh and keeps the engine's availability flag", async () => {
  const stub = stubFetch(() => ({
    body: {
      provider: "vibe-factors", providerVersion: "1.2.0", source: "plugin", available: true,
      factors: [{
        id: "vibe.rsi.14", name: "RSI（14）", family: "oscillator", mode: "time_series",
        sources: ["candles"], requiredFields: ["close"], warmupBars: 15, supportedTimeframes: ["15m", "1h"],
        implementationVersion: "1", formulaHash: "abc123def4567890", description: "相对强弱", provider: "vibe-factors",
        providerVersion: "1.2.0",
      }],
      warnings: [],
    },
  }));
  try {
    const catalog = await fetchFactorCatalog();
    assert.equal(stub.calls[0].url, "/api/factors?refresh=true");
    assert.equal(catalog.available, true);
    assert.equal(catalog.provider, "vibe-factors");
    assert.equal(catalog.providerVersion, "1.2.0");
    assert.equal(catalog.factors.length, 1);
    assert.equal(catalog.factors[0].warmupBars, 15);
    assert.deepEqual(catalog.factors[0].requiredFields, ["close"]);
  } finally {
    stub.restore();
  }

  const stored = stubFetch(() => ({
    body: { provider: "vibe-factors", source: "stored", available: false, factors: [], warnings: ["因子插件未响应：timeout"] },
  }));
  try {
    const catalog = await fetchFactorCatalog(false);
    assert.equal(stored.calls[0].url, "/api/factors?refresh=false");
    assert.equal(catalog.available, false, "the stored catalogue is not a live provider");
    assert.deepEqual(catalog.warnings, ["因子插件未响应：timeout"]);
  } finally {
    stored.restore();
  }
});

test("runs are read with their filter and unwrapped from the {runs} envelope", async () => {
  const stub = stubFetch(() => ({
    body: {
      runs: [{
        id: 3, provider: "vibe-factors", symbol: "BTCUSDT", interval: "1h", snapshotHash: "hash",
        factorIds: ["a", "b"], status: "done", bars: 3000, seriesCount: 2, coverage: { a: 3000, b: null },
        warnings: [], error: null, durationMs: 1234, createdTs: 1_800_000_000_000,
      }, { nonsense: true }],
    },
  }));
  try {
    const runs = await fetchFactorRuns({ symbol: "BTCUSDT", limit: 5 });
    assert.equal(stub.calls[0].url, "/api/factors/runs?symbol=BTCUSDT&limit=5");
    assert.equal(runs.length, 1, "a row without an id is not a run");
    assert.equal(runs[0].id, 3);
    assert.deepEqual(runs[0].factorIds, ["a", "b"]);
    assert.deepEqual(runs[0].coverage, { a: 3000 }, "a count that is not a number is not a count");
  } finally {
    stub.restore();
  }
});

test("one run's detail is unwrapped from {run} with its parameters and values", async () => {
  const stub = stubFetch(() => ({
    body: {
      run: {
        id: 5, provider: "vibe-factors", symbol: "ETHUSDT", interval: "4h", snapshotHash: "h",
        factorIds: ["a"], parameters: { window: 24 }, status: "done", bars: 500, seriesCount: 1,
        coverage: { a: 480 }, warnings: [], error: null, durationMs: 900, createdTs: 1,
        series: [{ factorId: "a", values: [{ time: 10, value: 1.5 }], implementationVersion: "1" }],
      },
    },
  }));
  try {
    const run = await fetchFactorRun(5);
    assert.equal(stub.calls[0].url, "/api/factors/runs/5");
    assert.equal(run.id, 5);
    assert.deepEqual(run.parameters, { window: 24 });
    assert.equal(run.series.length, 1);
    assert.deepEqual(run.series[0].values, [{ time: 10, value: 1.5 }]);
  } finally {
    stub.restore();
  }

  const missing = stubFetch(() => ({ body: {} }));
  try {
    await assert.rejects(() => fetchFactorRun(9), /run/);
  } finally {
    missing.restore();
  }
});

test("the provider chip does not repeat the provider name", () => {
  assert.equal(
    providerLabel({ provider: "vibe-factors", providerVersion: "vibe-factors/0.1.0", available: true }),
    "vibe-factors/0.1.0 · 插件已启用",
  );
  assert.equal(
    providerLabel({ provider: "vibe-factors", providerVersion: "0.2.0", available: false }),
    "vibe-factors v0.2.0 · 插件未启用",
  );
  assert.equal(
    providerLabel({ provider: null, providerVersion: "", available: false }),
    "无提供者 · 插件未启用",
  );
});
