/**
 * The result centre's pure logic.
 *
 * These helpers decide what the queue board claims: how many runs are active,
 * how long a run took, and — most importantly — when a metric is unknown. A
 * missing number must stay missing so the page can print "—"; a fabricated zero
 * would read as a real result. Tested without a DOM, like the market logic.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  artifactMediaLabel,
  artifactUrl,
  checkStatusLabel,
  equityPointsOf,
  errorKindLabel,
  executionModelOf,
  fetchRun,
  fetchRuns,
  filterRuns,
  formatBytes,
  formatDuration,
  headlineOf,
  isActiveStatus,
  parseGridInput,
  provenanceOf,
  readValidationStudy,
  resolveHeadline,
  runKindLabel,
  runStatusLabel,
  submitRun,
  summariseRuns,
  validationStudyBody,
  verdictLabel,
  type RunSummary,
  type ValidationStudyForm,
} from "../src/services/runs.ts";

function run(patch: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 1,
    kind: "backtest",
    status: "queued",
    label: "",
    symbol: null,
    displaySymbol: null,
    symbols: [],
    interval: "1h",
    strategyId: "ma_cross",
    strategyVersion: "1",
    progress: 0,
    progressLabel: "",
    stage: "",
    attempts: 1,
    queuedTs: 1_800_000_000_000,
    startedTs: null,
    finishedTs: null,
    durationMs: null,
    error: null,
    errorKind: "",
    headline: { netReturnPct: null, maxDrawdownPct: null, trades: null, sharpe: null, profitFactor: null, winRatePct: null },
    artifacts: [],
    verdicts: [],
    dataReady: null,
    degraded: null,
    missingData: [],
    ...patch,
  };
}

/* ------------------------------------------------------------- queue copy */

test("every queue state has Chinese copy and an unknown one is passed through", () => {
  assert.equal(runStatusLabel("queued"), "排队中");
  assert.equal(runStatusLabel("running"), "运行中");
  assert.equal(runStatusLabel("done"), "已完成");
  assert.equal(runStatusLabel("failed"), "失败");
  assert.equal(runStatusLabel("cancelled"), "已取消");
  assert.equal(runStatusLabel("paused"), "paused", "a status the UI does not know must not be relabelled");
});

test("study kinds and verdicts read as the study surfaces name them", () => {
  assert.equal(runKindLabel("backtest"), "回测");
  assert.equal(runKindLabel("validate"), "参数验证");
  assert.equal(runKindLabel("portfolio"), "组合回测");
  assert.equal(runKindLabel("sweep"), "sweep");
  assert.equal(verdictLabel("pass"), "通过");
  assert.equal(verdictLabel("warn"), "关注");
  assert.equal(verdictLabel("fail"), "不通过");
});

test("only queued and running count as active work", () => {
  assert.equal(isActiveStatus("queued"), true);
  assert.equal(isActiveStatus("running"), true);
  assert.equal(isActiveStatus("done"), false);
  assert.equal(isActiveStatus("failed"), false);
  assert.equal(isActiveStatus("cancelled"), false);
});

test("a board tally counts each status and reports whether anything is moving", () => {
  const tally = summariseRuns([
    run({ id: 1, status: "queued" }),
    run({ id: 2, status: "queued" }),
    run({ id: 3, status: "running" }),
    run({ id: 4, status: "done" }),
    run({ id: 5, status: "done" }),
    run({ id: 6, status: "done" }),
    run({ id: 7, status: "failed" }),
    run({ id: 8, status: "cancelled" }),
  ]);
  assert.deepEqual(tally, { queued: 2, running: 1, done: 3, failed: 1, cancelled: 1, total: 8, activeCount: 3, hasActive: true });
});

test("an empty or settled board polls nothing", () => {
  assert.deepEqual(summariseRuns([]), { queued: 0, running: 0, done: 0, failed: 0, cancelled: 0, total: 0, activeCount: 0, hasActive: false });
  const settled = summariseRuns([run({ status: "done" }), run({ status: "failed" })]);
  assert.equal(settled.hasActive, false);
  assert.equal(settled.activeCount, 0);
});

test("filters pass everything through on 'all' and narrow on a value", () => {
  const runs = [
    run({ id: 1, kind: "backtest", status: "done" }),
    run({ id: 2, kind: "validate", status: "running" }),
    run({ id: 3, kind: "validate", status: "failed" }),
  ];
  assert.equal(filterRuns(runs, {}).length, 3);
  assert.equal(filterRuns(runs, { status: "all", kind: "all" }).length, 3);
  assert.deepEqual(filterRuns(runs, { status: "failed" }).map((item) => item.id), [3]);
  assert.deepEqual(filterRuns(runs, { kind: "validate" }).map((item) => item.id), [2, 3]);
  assert.deepEqual(filterRuns(runs, { kind: "validate", status: "running" }).map((item) => item.id), [2]);
  assert.deepEqual(filterRuns(runs, { kind: "portfolio" }), []);
});

test("the text search matches id, symbol, label and strategy, and ignores case", () => {
  const runs = [
    run({ id: 11, symbol: "BTCUSDT", displaySymbol: "BTCUSDT", label: "参数搜索与滚动验证 BTCUSDT 1h", strategyId: "ma_cross" }),
    run({ id: 12, kind: "factors", symbol: "ETHUSDT", displaySymbol: "ETHUSDT", label: "因子覆盖", strategyId: "" }),
    run({ id: 13, kind: "portfolio", symbols: ["SOLUSDT", "BTCUSDT"], label: "组合", strategyId: "momentum_breakout" }),
  ];
  assert.deepEqual(filterRuns(runs, { query: "12" }).map((item) => item.id), [12], "编号可搜");
  assert.deepEqual(filterRuns(runs, { query: "ethusdt" }).map((item) => item.id), [12], "标的不区分大小写");
  assert.deepEqual(filterRuns(runs, { query: "滚动验证" }).map((item) => item.id), [11], "标签可搜");
  assert.deepEqual(filterRuns(runs, { query: "momentum" }).map((item) => item.id), [13], "策略可搜");
  assert.deepEqual(filterRuns(runs, { query: "BTCUSDT" }).map((item) => item.id), [11, 13], "多标的的成员也算匹配");
  assert.deepEqual(filterRuns(runs, { query: "  " }).map((item) => item.id), [11, 12, 13], "空白搜索等于没有搜索");
  assert.deepEqual(filterRuns(runs, { query: "nope" }), []);
  assert.deepEqual(filterRuns(runs, { query: "BTC", kind: "portfolio" }).map((item) => item.id), [13], "搜索与下拉一起生效");
});

/* -------------------------------------------------------------- durations */

test("elapsed time separates 'not finished' from 'took no time'", () => {
  assert.equal(formatDuration(null), "—");
  assert.equal(formatDuration(undefined), "—");
  assert.equal(formatDuration(Number.NaN), "—");
  assert.equal(formatDuration(0), "0 毫秒");
  assert.equal(formatDuration(480), "480 毫秒");
  assert.equal(formatDuration(1_200), "1.2 秒");
  assert.equal(formatDuration(45_000), "45.0 秒");
  assert.equal(formatDuration(192_000), "3 分 12 秒");
  assert.equal(formatDuration(7_500_000), "2 小时 5 分");
});

test("artifact sizes and payload types are described, not guessed", () => {
  assert.equal(formatBytes(null), "—");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(2048), "2.0 KB");
  assert.equal(formatBytes(3 * 1024 * 1024), "3.00 MB");
  assert.equal(artifactMediaLabel("application/json"), "JSON");
  assert.equal(artifactMediaLabel("text/csv; charset=utf-8"), "CSV");
  assert.equal(artifactMediaLabel(""), "—");
});

test("failure kinds are named the way the gate names them", () => {
  assert.equal(errorKindLabel("not_ready"), "数据未就绪");
  assert.equal(errorKindLabel("invalid"), "请求无效");
  assert.equal(errorKindLabel("internal"), "引擎内部错误");
  assert.equal(errorKindLabel("cancelled"), "已取消");
  assert.equal(errorKindLabel(""), "任务失败");
  assert.equal(errorKindLabel("weird"), "任务失败");
});

/* --------------------------------------------------------------- headline */

test("the headline reads the queue's camelCase row", () => {
  const headline = headlineOf({ netReturnPct: 12.5, maxDrawdownPct: 4.25, trades: 31, sharpe: 1.4, profitFactor: 2.1, winRatePct: 58.06 });
  assert.deepEqual(headline, { netReturnPct: 12.5, maxDrawdownPct: 4.25, trades: 31, sharpe: 1.4, profitFactor: 2.1, winRatePct: 58.06 });
});

test("the headline falls back to the engine's snake_case study payload", () => {
  const headline = headlineOf({ net_return_pct: -3.5, max_drawdown_pct: 7, win_rate_pct: 41.2, profit_factor: null, trades: [{}, {}, {}] });
  assert.equal(headline.netReturnPct, -3.5);
  assert.equal(headline.maxDrawdownPct, 7);
  assert.equal(headline.winRatePct, 41.2);
  assert.equal(headline.trades, 3, "a trade list is counted, not shown as a number");
  assert.equal(headline.profitFactor, null, "a null profit factor is unknown, not zero");
  assert.equal(headline.sharpe, null);
});

test("metrics nested under the validation envelope are read too", () => {
  const headline = headlineOf({ metrics: { sharpe: 0.9, profitFactor: 1.25, trades: 12 } });
  assert.equal(headline.sharpe, 0.9);
  assert.equal(headline.profitFactor, 1.25);
  assert.equal(headline.trades, 12);
});

test("a missing headline field stays null instead of becoming zero", () => {
  assert.deepEqual(headlineOf(null), { netReturnPct: null, maxDrawdownPct: null, trades: null, sharpe: null, profitFactor: null, winRatePct: null });
  assert.deepEqual(headlineOf(undefined), { netReturnPct: null, maxDrawdownPct: null, trades: null, sharpe: null, profitFactor: null, winRatePct: null });
  assert.deepEqual(headlineOf({}), { netReturnPct: null, maxDrawdownPct: null, trades: null, sharpe: null, profitFactor: null, winRatePct: null });
  const invalid = headlineOf({ netReturnPct: "12.5", maxDrawdownPct: Number.NaN, trades: Number.POSITIVE_INFINITY });
  assert.equal(invalid.netReturnPct, null, "a stringified number is not a number");
  assert.equal(invalid.maxDrawdownPct, null);
  assert.equal(invalid.trades, null);
});

test("an empty trade list is a real zero, unlike a missing field", () => {
  const headline = headlineOf({ trades: [] });
  assert.equal(headline.trades, 0);
});

test("a null queue headline falls back to the result payload field by field", () => {
  const queueHeadline = { netReturnPct: 8, maxDrawdownPct: null, trades: null, sharpe: null, profitFactor: null, winRatePct: 50 };
  const resolved = resolveHeadline(queueHeadline, { net_return_pct: 1, max_drawdown_pct: 6.5, trades: [{}, {}], win_rate_pct: 99 });
  assert.equal(resolved.netReturnPct, 8, "the queue's own number wins");
  assert.equal(resolved.maxDrawdownPct, 6.5, "a null field is filled from the payload");
  assert.equal(resolved.trades, 2);
  assert.equal(resolved.winRatePct, 50, "a present field is never overwritten by the payload");
  assert.equal(resolved.sharpe, null);
});

test("without a queue headline the payload is the only source", () => {
  const resolved = resolveHeadline(null, { net_return_pct: -1 });
  assert.equal(resolved.netReturnPct, -1);
  assert.equal(resolved.maxDrawdownPct, null);
});

/* ------------------------------------------------------------ equity curve */

test("equity points accept either spelling and drop unusable entries", () => {
  const camel = equityPointsOf({ equityCurve: [{ time: 2, equity: 20 }, { time: 1, equity: 10 }, { time: 3, equity: null }] });
  assert.deepEqual(camel, [{ time: 1, equity: 10 }, { time: 2, equity: 20 }], "points are ordered by time and null equity is dropped");

  const snake = equityPointsOf({ equity_curve: [{ time: 5, equity: 50, marginRatio: null }] });
  assert.deepEqual(snake, [{ time: 5, equity: 50 }]);
});

test("a result without a curve draws nothing", () => {
  assert.deepEqual(equityPointsOf(null), []);
  assert.deepEqual(equityPointsOf({}), []);
  assert.deepEqual(equityPointsOf({ equityCurve: "nope" }), []);
  assert.deepEqual(equityPointsOf({ equityCurve: [{ equity: 10 }] }), []);
});

/* -------------------------------------------------------------- provenance */

test("an unready run is reported as degraded with what is missing", () => {
  const provenance = provenanceOf(
    run({ status: "failed", errorKind: "not_ready", dataReady: false, degraded: null, missingData: ["funding"] }),
    { title: "数据未就绪", detail: "资金费历史不足", action: "先回填资金费", checks: [{ key: "funding", label: "资金费", status: "fail", detail: "0 条" }] },
    { data_impacts: ["未计入资金费，收益偏高"] },
  );
  assert.equal(provenance.severity, "degraded");
  assert.equal(provenance.title, "数据未就绪");
  assert.equal(provenance.detail, "资金费历史不足");
  assert.equal(provenance.action, "先回填资金费");
  assert.deepEqual(provenance.missing, ["funding"]);
  assert.deepEqual(provenance.impacts, ["未计入资金费，收益偏高"]);
  assert.deepEqual(provenance.checks, [{ key: "funding", label: "资金费", status: "fail", detail: "0 条" }]);
});

test("missing data and impacts from every source are merged once", () => {
  const provenance = provenanceOf(
    run({ degraded: true, dataReady: false, missingData: ["funding"] }),
    { missing: ["funding", "open_interest"], impacts: ["杠杆档位缺失"] },
    { missing_data: ["open_interest"], dataImpacts: ["杠杆档位缺失"] },
  );
  assert.deepEqual(provenance.missing, ["funding", "open_interest"]);
  assert.deepEqual(provenance.impacts, ["杠杆档位缺失"]);
});

test("a complete, formal run is not flagged as degraded", () => {
  const provenance = provenanceOf(run({ status: "done", dataReady: true, degraded: false }), null, null);
  assert.equal(provenance.severity, "formal");
  assert.equal(provenance.title, "数据就绪（正式口径）");
  assert.deepEqual(provenance.missing, []);
  assert.deepEqual(provenance.checks, []);
});

test("a queued run with nothing known yet is not flagged", () => {
  const provenance = provenanceOf(run({ status: "queued" }), null, null);
  assert.equal(provenance.severity, "formal", "nothing is known yet, so the page shows no banner");
  assert.equal(provenance.title, "数据就绪（正式口径）");
});

test("the study envelope's gate verdict is read without a title of its own", () => {
  const provenance = provenanceOf(
    run({ status: "done", dataReady: false, degraded: false }),
    {
      ok: false,
      degraded: false,
      missing: ["funding", "open_interest"],
      impacts: ["未计入资金费，收益偏高"],
      blocking: [{ key: "funding", label: "资金费", status: "fail", detail: "0 条" }],
      checks: [{ key: "bars", label: "K线", status: "ok", detail: "800 根" }],
    },
    null,
  );
  assert.equal(provenance.severity, "degraded");
  assert.equal(provenance.title, "数据未就绪", "a gate verdict without a title still needs one");
  assert.deepEqual(provenance.missing, ["funding", "open_interest"]);
  assert.deepEqual(provenance.impacts, ["未计入资金费，收益偏高"]);
  assert.deepEqual(provenance.blocking.map((item) => item.label), ["资金费"]);
  assert.deepEqual(provenance.checks.map((item) => item.label), ["K线"]);
});

test("a degraded but admissible run is labelled as degraded, not blocked", () => {
  const provenance = provenanceOf(
    run({ status: "done", dataReady: true, degraded: true }),
    { ok: true, degraded: true, missing: [], impacts: ["使用标记价格作为成交价"] },
    null,
  );
  assert.equal(provenance.severity, "degraded");
  assert.equal(provenance.title, "数据降级运行");
  assert.deepEqual(provenance.blocking, []);
  assert.deepEqual(provenance.impacts, ["使用标记价格作为成交价"]);
});

test("gate check statuses are shown in Chinese and never default to a failure", () => {
  assert.equal(checkStatusLabel("pass"), "通过");
  assert.equal(checkStatusLabel("OK"), "通过");
  assert.equal(checkStatusLabel("warn"), "关注");
  assert.equal(checkStatusLabel("fail"), "未通过");
  assert.equal(checkStatusLabel(""), "—");
  assert.equal(checkStatusLabel("something_new"), "something_new", "an unknown token is shown as-is, not translated");
});

/* --------------------------------------------------------------- artifacts */

test("artifact links are encoded and stay inside the run", () => {
  assert.equal(artifactUrl(7, "trades.csv"), "/api/backtest/runs/7/artifacts/trades.csv");
  assert.equal(artifactUrl(12, "净值 曲线.json"), "/api/backtest/runs/12/artifacts/%E5%87%80%E5%80%BC%20%E6%9B%B2%E7%BA%BF.json");
  assert.equal(artifactUrl(3, "a/b.csv"), "/api/backtest/runs/3/artifacts/a%2Fb.csv");
});

/* --------------------------------------------------- response shapes (fetch) */

test("fetchRun unwraps the {run} envelope the endpoint answers with", async () => {
  const original = globalThis.fetch;
  const seen: string[] = [];
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    seen.push(String(input));
    return new Response(JSON.stringify({ run: { id: 10, status: "cancelled", progress: 13.8 } }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
  try {
    const run = await fetchRun(10);
    assert.equal(seen[0], "/api/backtest/runs/10");
    assert.equal(run.id, 10, "详情必须直接给出任务本身，而不是 {run} 外壳");
    assert.equal(run.status, "cancelled");
  } finally {
    globalThis.fetch = original;
  }
});

test("fetchRun refuses an envelope without a run instead of rendering blanks", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({}), { status: 200 })) as typeof fetch;
  try {
    await assert.rejects(() => fetchRun(3), /run/);
  } finally {
    globalThis.fetch = original;
  }
});

test("fetchRuns keeps the board envelope the list endpoint answers with", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({
    summary: { queued: 0, running: 1, done: 2, failed: 0, cancelled: 0, total: 3 },
    queue: { workerRunning: true, activeRunId: 4, concurrency: 1 },
    runs: [{ id: 4, status: "running" }],
  }), { status: 200 })) as typeof fetch;
  try {
    const board = await fetchRuns({ limit: 5 });
    assert.equal(board.runs.length, 1);
    assert.equal(board.queue.activeRunId, 4);
    assert.equal(board.summary.running, 1);
  } finally {
    globalThis.fetch = original;
  }
});

test("a validation run's curve is drawn from the parameters it selected", () => {
  const points = equityPointsOf({
    parameterSearch: { best: { parameters: { fastPeriod: 9 } } },
    selectedRun: { equityCurve: [{ time: 2, equity: 102 }, { time: 1, equity: 100 }] },
  });
  assert.deepEqual(points, [{ time: 1, equity: 100 }, { time: 2, equity: 102 }],
    "没有顶层曲线时用所选参数的整段曲线，并按时间排序");
  const top = equityPointsOf({ equityCurve: [{ time: 5, equity: 1 }], selectedRun: { equityCurve: [{ time: 1, equity: 9 }] } });
  assert.deepEqual(top, [{ time: 5, equity: 1 }], "顶层曲线优先");
});

/* --------------------------------------------------------- execution model */

test("the execution model is read from the engine's snake_case block", () => {
  const model = executionModelOf({
    execution_model: {
      fillRule: "收盘确认交叉，下一根K线开盘成交",
      signalBar: "已收盘K线",
      fillPrice: "延迟后那根K线的开盘价加滑点",
      latencyBars: 0,
      slippageModel: "fixed",
      slippageBps: 5,
      impactCoefficient: 0.1,
      maxParticipation: 1,
      partialFill: "ignore",
      unfilledOrders: 0,
      unfilledNotional: 0,
      feeTiming: "开平各计一次",
      fundingTiming: "按交易所结算时间用当时标记价计收",
      liquidationBasis: "逐仓，维持保证金按风险档位",
      thinBarPolicy: "skip",
      simplifications: ["不建模排队位置、盘口深度与下单被拒"],
    },
  });
  assert.equal(model?.fillRule, "收盘确认交叉，下一根K线开盘成交");
  assert.equal(model?.latencyBars, 0, "0 根是即时下一根开盘，不是未知");
  assert.equal(model?.slippageBps, 5);
  assert.equal(model?.maxParticipation, 1);
  assert.equal(model?.partialFill, "ignore");
  assert.equal(model?.thinBarPolicy, "skip");
  assert.deepEqual(model?.simplifications, ["不建模排队位置、盘口深度与下单被拒"]);
});

test("the camelCase spelling is read too, and missing numbers fall back to costModel or data_quality", () => {
  const model = executionModelOf({
    executionModel: { fillRule: "下一根开盘成交", latencyBars: 2 },
    data_quality: { latencyBars: 1, partialFill: "allow", maxParticipation: 0.25, unfilledOrders: 3, unfilledNotional: 120.5 },
  });
  assert.equal(model?.fillRule, "下一根开盘成交");
  assert.equal(model?.latencyBars, 2, "the dedicated block wins over the quality summary");
  assert.equal(model?.partialFill, "allow");
  assert.equal(model?.maxParticipation, 0.25);
  assert.equal(model?.unfilledOrders, 3);
  assert.equal(model?.unfilledNotional, 120.5);
});

test("a validation run's execution model is read from the parameters it selected", () => {
  const model = executionModelOf({
    costModel: { latencyBars: 1, partialFill: "ignore" },
    selectedRun: { execution_model: { fillRule: "收盘确认交叉，下一根K线开盘成交", latencyBars: 0, simplifications: ["不建模排队位置"] } },
  });
  assert.equal(model?.fillRule, "收盘确认交叉，下一根K线开盘成交");
  assert.equal(model?.latencyBars, 0, "所选参数自己的执行模型优先于外层成本模型");
  assert.deepEqual(model?.simplifications, ["不建模排队位置"]);
});

test("a result that states no execution assumptions renders no block", () => {
  assert.equal(executionModelOf(null), null);
  assert.equal(executionModelOf(undefined), null);
  assert.equal(executionModelOf({}), null);
  assert.equal(executionModelOf({ execution_model: {} }), null, "空块不是执行模型");
  assert.equal(executionModelOf({ data_quality: { bars: 300, thinBars: 0 } }), null, "K线计数不是执行模型");
});

test("an execution number the result does not state stays unknown instead of becoming zero", () => {
  const model = executionModelOf({ execution_model: { fillRule: "下一根开盘成交" } });
  assert.equal(model?.latencyBars, null);
  assert.equal(model?.slippageBps, null);
  assert.equal(model?.maxParticipation, null);
  assert.equal(model?.unfilledOrders, null);
  assert.equal(model?.impactCoefficient, null);
  assert.equal(model?.partialFill, "");
  assert.deepEqual(model?.simplifications, []);
});

/* ---------------------------------------------------------- heavy studies */

test("a parameter grid is parsed from commas, spaces and full-width commas", () => {
  assert.deepEqual(parseGridInput("5,9,20"), [5, 9, 20]);
  assert.deepEqual(parseGridInput(" 5，9  20 "), [5, 9, 20]);
  assert.deepEqual(parseGridInput("7"), [7]);
  assert.deepEqual(parseGridInput("1,2,3,4,5,6,7,8"), [1, 2, 3, 4, 5, 6, 7, 8]);
});

test("a grid that is empty, malformed or longer than the engine accepts is refused", () => {
  assert.equal(parseGridInput(""), null);
  assert.equal(parseGridInput("   "), null);
  assert.equal(parseGridInput("5,a,9"), null);
  assert.equal(parseGridInput("5.5"), null);
  assert.equal(parseGridInput("-3"), null);
  assert.equal(parseGridInput("0"), null, "周期为 0 不是参数");
  assert.equal(parseGridInput("1,2,3,4,5,6,7,8,9"), null, "引擎最多接受 8 个值");
});

test("the heavy study form is answered in Chinese before anything is submitted", () => {
  const base: ValidationStudyForm = {
    symbol: "BTCUSDT", interval: "1h", bars: "2000",
    fastGrid: "5,9,20", slowGrid: "21,50,100", walkForwardWindows: "4", allowDegraded: false,
  };
  const ok = readValidationStudy(base);
  assert.equal(ok.ok, true);
  assert.deepEqual(ok.ok ? ok.input : null, {
    symbol: "BTCUSDT", interval: "1h", bars: 2000,
    fastGrid: [5, 9, 20], slowGrid: [21, 50, 100], walkForwardWindows: 4, allowDegraded: false,
  });

  const cases: Array<[Partial<ValidationStudyForm>, string]> = [
    [{ symbol: "   " }, "请先选择或填写合约代码。"],
    [{ bars: "10" }, "K 线根数需为 30 到 5000 之间的整数。"],
    [{ bars: "6000" }, "K 线根数需为 30 到 5000 之间的整数。"],
    [{ bars: "很多" }, "K 线根数需为 30 到 5000 之间的整数。"],
    [{ bars: "2000.5" }, "K 线根数需为 30 到 5000 之间的整数。"],
    [{ fastGrid: "" }, "快线周期需为 1 到 8 个逗号分隔的正整数，例如 5,9,20。"],
    [{ slowGrid: "21,x" }, "慢线周期需为 1 到 8 个逗号分隔的正整数，例如 21,50,100。"],
    [{ walkForwardWindows: "0" }, "滚动窗口数需为 1 到 12 之间的整数。"],
    [{ walkForwardWindows: "13" }, "滚动窗口数需为 1 到 12 之间的整数。"],
  ];
  for (const [patch, message] of cases) {
    const read = readValidationStudy({ ...base, ...patch });
    assert.equal(read.ok, false, `${JSON.stringify(patch)} 必须被拒绝`);
    assert.equal(read.ok ? "" : read.error, message);
  }
});

test("the queued study body is the engine's validate contract", () => {
  const body = validationStudyBody({
    symbol: " BTCUSDT ", interval: "4h", bars: 2000,
    fastGrid: [5, 9, 20], slowGrid: [21, 50, 100], walkForwardWindows: 4, allowDegraded: true,
  });
  assert.deepEqual(body, {
    symbol: "BTCUSDT", timeframe: "4h", bars: 2000, strategyId: "ma_cross",
    fastGrid: [5, 9, 20], slowGrid: [21, 50, 100], walkForwardWindows: 4, allowDegraded: true,
  });
});

test("a heavy study is queued as a validate run, under its own label", async () => {
  const original = globalThis.fetch;
  const seen: Array<{ url: string; body: string }> = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    seen.push({ url: String(input), body: String(init?.body ?? "") });
    return new Response(JSON.stringify({ run: { id: 21, status: "queued" }, deduplicated: true }), { status: 200 });
  }) as typeof fetch;
  try {
    const body = validationStudyBody({
      symbol: "BTCUSDT", interval: "1h", bars: 2000,
      fastGrid: [5, 9, 20], slowGrid: [21, 50, 100], walkForwardWindows: 4, allowDegraded: false,
    });
    const outcome = await submitRun("validate", body, "参数搜索与滚动验证 BTCUSDT 1h");
    assert.equal(seen[0].url, "/api/backtest/runs");
    assert.deepEqual(JSON.parse(seen[0].body), {
      kind: "validate", request: body, label: "参数搜索与滚动验证 BTCUSDT 1h",
    });
    assert.equal(outcome.run.id, 21);
    assert.equal(outcome.deduplicated, true, "同一个仍在执行的任务不会被重复排队");
  } finally {
    globalThis.fetch = original;
  }
});
