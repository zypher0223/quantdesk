/**
 * The result centre's batch logic: comparing, exporting, remembering a filter,
 * and telling a queued transfer from a finished study.
 *
 * Two failure modes are what these tests exist for. A CSV that mangles a value
 * containing a comma silently corrupts a spreadsheet, and a 202 transfer read as
 * a study payload prints a queue row as if it were a result. Both are invisible
 * on screen, so both are pinned here.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  comparisonRow,
  comparisonRows,
  compareEnabled,
  COMPARISON_LIMIT,
  csvCell,
  csvFileName,
  DEFAULT_RESULT_FILTER,
  readStoredFilter,
  RESULT_CSV_COLUMNS,
  RESULT_FILTER_STORAGE_KEY,
  resultsCsv,
  storeResultFilter,
  type FilterStorage,
} from "../src/services/results-batch.ts";
import {
  FACTOR_SCOPE,
  factorCoverageText,
  isFactorRun,
  queueStateText,
  readPruneReport,
  readRunSummary,
  sandboxLabel,
  sandboxState,
  type RunSummary,
} from "../src/services/runs.ts";
import { queuedNotice, queuedReason, queuedTransferOf } from "../src/services/study-queue.ts";
import { megabytesText } from "../src/services/run-figures.ts";

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

/** A factor run as the queue reports one: coverage, and no P&L at all. */
function factorRun(patch: Partial<RunSummary> = {}): RunSummary {
  return run({
    id: 21,
    kind: "factors",
    status: "done",
    symbol: "BTCUSDT",
    displaySymbol: "BTCUSDT",
    interval: "1h",
    strategyId: "",
    durationMs: 4_500,
    finishedTs: 1_800_000_100_000,
    headline: {
      scope: FACTOR_SCOPE,
      netReturnPct: null,
      maxDrawdownPct: null,
      trades: null,
      sharpe: null,
      profitFactor: null,
      winRatePct: null,
      factors: 6,
      coveredFactors: 4,
      medianCoverage: 1832,
      bars: 2000,
    },
    ...patch,
  });
}

/* -------------------------------------------------------------- CSV fields */

test("a CSV field is quoted exactly when it has to be", () => {
  assert.equal(csvCell("plain"), "plain");
  assert.equal(csvCell(""), "");
  assert.equal(csvCell(null), "");
  assert.equal(csvCell(undefined), "");
  assert.equal(csvCell("a,b"), '"a,b"', "逗号必须被引号包住");
  assert.equal(csvCell('say "hi"'), '"say ""hi"""', "内部引号要成对加倍");
  assert.equal(csvCell("line\nbreak"), '"line\nbreak"', "换行必须被引号包住");
  assert.equal(csvCell("cr\r\nlf"), '"cr\r\nlf"');
  assert.equal(csvCell("，"), "，", "中文逗号不是分隔符，不需要引号");
});

test("the exported CSV keeps one row per run and one column per field", () => {
  const plain = run({
    id: 7,
    status: "done",
    symbol: "BTCUSDT",
    displaySymbol: "BTCUSDT",
    headline: {
      scope: '样本外, 含"成本"',
      netReturnPct: 12.5,
      maxDrawdownPct: 4.25,
      trades: 31,
      sharpe: 1.4,
      profitFactor: 2.1,
      winRatePct: 58,
    },
    durationMs: 61_000,
    finishedTs: 1_800_000_100_000,
  });
  const csv = resultsCsv([plain, factorRun()]);
  const lines = csv.split("\n");
  assert.equal(lines.length, 3, "表头加两条记录");
  assert.equal(lines[0], RESULT_CSV_COLUMNS.join(","));
  assert.ok(lines[1].includes('"样本外, 含""成本"""'), "带逗号与引号的口径字段必须整体加引号");
  // The quoted comma must not create a 15th column: count fields, not commas.
  assert.equal(splitCsv(lines[1]).length, RESULT_CSV_COLUMNS.length);
  assert.equal(splitCsv(lines[2]).length, RESULT_CSV_COLUMNS.length);
  assert.ok(lines[1].includes("+12.50%"));
  assert.ok(lines[2].includes("因子 6 个 · 有值 4 · 中位覆盖 1832 根 / 2000 根"), "因子任务的请求摘要要进 CSV");
});

test("the CSV file name is stamped with the moment it was taken", () => {
  const stamp = new Date(2025, 8, 16, 9, 30).getTime();
  assert.equal(csvFileName(stamp), "quantdesk-runs-20250916-0930.csv");
});

/* -------------------------------------------------------------- comparison */

test("a comparison row prints a missing metric as a dash, never a zero", () => {
  const row = comparisonRow(run({ id: 3, status: "running" }));
  assert.equal(row.idText, "#3");
  assert.equal(row.kind, "回测");
  assert.equal(row.market, "— · 1h");
  assert.equal(row.status, "运行中");
  assert.equal(row.netReturn, "—");
  assert.equal(row.maxDrawdown, "—");
  assert.equal(row.trades, "—");
  assert.equal(row.sharpe, "—");
  assert.equal(row.scope, "—", "口径未报告时不能编造整段回测");
  assert.equal(row.duration, "—");
  assert.equal(row.completed, "—");
  assert.equal(row.coverage, "—", "非因子任务没有覆盖列");
});

test("a finished run shows its own numbers and completion time", () => {
  const row = comparisonRow(run({
    id: 9,
    status: "done",
    symbol: "ETHUSDT",
    displaySymbol: "ETHUSDT",
    interval: "4h",
    strategyId: "ma_cross",
    strategyVersion: "2",
    durationMs: 61_000,
    finishedTs: 1_800_000_100_000,
    headline: {
      scope: "样本外测试段",
      netReturnPct: -3.5,
      maxDrawdownPct: 7,
      trades: 12,
      sharpe: 0.9,
      profitFactor: null,
      winRatePct: 41.2,
    },
  }));
  assert.equal(row.market, "ETHUSDT · 4h");
  assert.equal(row.strategy, "ma_cross v2");
  assert.equal(row.netReturn, "-3.50%");
  assert.equal(row.maxDrawdown, "-7.00%");
  assert.equal(row.trades, "12");
  assert.equal(row.sharpe, "0.90");
  assert.equal(row.scope, "样本外测试段");
  assert.equal(row.duration, "1 分 1 秒");
  assert.notEqual(row.completed, "—");
});

test("a factor run shows coverage where the P&L columns are empty", () => {
  const row = comparisonRow(factorRun());
  assert.equal(row.netReturn, "—");
  assert.equal(row.trades, "—");
  assert.equal(row.scope, FACTOR_SCOPE);
  assert.equal(row.coverage, "因子 6 个 · 有值 4 · 中位覆盖 1832 根 / 2000 根");
  assert.ok(isFactorRun(factorRun()));
  assert.equal(isFactorRun(run()), false);
});

test("a factor run without coverage numbers says so instead of showing dashes", () => {
  const bare = factorRun({
    headline: { scope: FACTOR_SCOPE, netReturnPct: null, maxDrawdownPct: null, trades: null, sharpe: null, profitFactor: null, winRatePct: null },
  });
  assert.equal(factorCoverageText(bare), "覆盖率未报告");
  assert.equal(comparisonRow(bare).coverage, "覆盖率未报告");
});

test("comparison rows keep the order they were given and the batch cap is 2–6", () => {
  const rows = comparisonRows([run({ id: 5 }), run({ id: 4 }), factorRun()]);
  assert.deepEqual(rows.map((row) => row.id), [5, 4, 21]);
  assert.equal(COMPARISON_LIMIT, 6);
  assert.equal(compareEnabled(0), false);
  assert.equal(compareEnabled(1), false, "一项不构成对比");
  assert.equal(compareEnabled(2), true);
  assert.equal(compareEnabled(6), true);
  assert.equal(compareEnabled(7), false, "超过六项会读不出来");
  assert.equal(compareEnabled(Number.NaN), false);
});

/* ------------------------------------------------------- queued transfers */

const QUEUED_202 = {
  queued: true,
  run: { id: 12, kind: "factors", status: "queued", label: "因子计算 BTCUSDT 1h", progress: 0 },
  reason: "该研究预计 600,000 单位工作量，超过同步上限 60,000 单位，已自动转入后台队列",
  detail: "结果与进度在结果中心查看；同一请求不会重复排队",
  syncCost: 600_000,
  syncBudget: 60_000,
};

test("a 202 transfer is recognised, and an inline answer is not mistaken for one", () => {
  const queued = queuedTransferOf(202, QUEUED_202);
  assert.ok(queued, "202 + queued:true + run 就是一次转入后台");
  assert.equal(queued.run.id, 12);
  assert.equal(queued.run.kind, "factors");
  assert.equal(queued.run.status, "queued");
  assert.equal(queued.syncCost, 600_000);
  assert.equal(queued.syncBudget, 60_000);
  assert.equal(queuedNotice(queued), "已转入后台（#12），可在结果中心查看");
  assert.ok(queuedReason(queued).startsWith("该研究预计 600,000 单位工作量"));

  assert.equal(queuedTransferOf(200, { net_return_pct: 12 }), null, "同步结果不是转入后台");
  assert.equal(queuedTransferOf(200, QUEUED_202), null, "状态码不是 202 就不能当作转入后台");
  assert.equal(queuedTransferOf(202, { run: { id: 12 }, deduplicated: false }), null, "提交任务的 202 不是转入后台");
  assert.equal(queuedTransferOf(202, { queued: true }), null, "没有 run 就没有可等待的任务");
  assert.equal(queuedTransferOf(202, { queued: true, run: { status: "queued" } }), null, "run 没有 id 就不是一条任务");
  assert.equal(queuedTransferOf(409, { detail: "太大" }), null);
  assert.equal(queuedTransferOf(202, null), null);
});

test("the queue row inside a transfer body is read defensively", () => {
  const summary = readRunSummary({ id: 12, kind: "factors", status: "queued", headline: { scope: FACTOR_SCOPE, factors: 6 } });
  assert.ok(summary);
  assert.equal(summary.id, 12);
  assert.equal(summary.label, "", "没报告的字段留空，不猜");
  assert.equal(summary.attempts, 1);
  assert.equal(summary.headline.factors, 6);
  assert.equal(readRunSummary({ kind: "backtest" }), null);
  assert.equal(readRunSummary(null), null);
});

/* --------------------------------------------------------- filter memory */

class FakeStorage implements FilterStorage {
  private readonly data = new Map<string, string>();
  getItem(key: string): string | null {
    return this.data.has(key) ? this.data.get(key)! : null;
  }
  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
  keys(): string[] {
    return [...this.data.keys()];
  }
  values(): string[] {
    return [...this.data.values()];
  }
}

test("the filter survives a round trip and stores nothing but the three choices", () => {
  const storage = new FakeStorage();
  assert.deepEqual(readStoredFilter(storage), DEFAULT_RESULT_FILTER, "空存储给出默认筛选");

  storeResultFilter(storage, { kind: "factors", status: "done", query: "BTC" });
  assert.deepEqual(readStoredFilter(storage), { kind: "factors", status: "done", query: "BTC" });
  assert.deepEqual(storage.keys(), [RESULT_FILTER_STORAGE_KEY], "只写一个键");
  assert.deepEqual(Object.keys(JSON.parse(storage.values()[0])).sort(), ["kind", "query", "status"]);
});

test("a corrupt or hostile filter store falls back instead of breaking the page", () => {
  const storage = new FakeStorage();
  storage.setItem(RESULT_FILTER_STORAGE_KEY, "{not json");
  assert.deepEqual(readStoredFilter(storage), DEFAULT_RESULT_FILTER);

  storage.setItem(RESULT_FILTER_STORAGE_KEY, JSON.stringify({ kind: "nope", status: "weird", query: 5, secret: "x" }));
  assert.deepEqual(readStoredFilter(storage), { kind: "all", status: "all", query: "" }, "未知取值一律回到全部");

  const hostile: FilterStorage = {
    getItem() { throw new Error("blocked"); },
    setItem() { throw new Error("full"); },
  };
  assert.deepEqual(readStoredFilter(hostile), DEFAULT_RESULT_FILTER);
  storeResultFilter(hostile, { kind: "all", status: "all", query: "" });
  assert.deepEqual(readStoredFilter(null), DEFAULT_RESULT_FILTER);
});

/* ------------------------------------------------------------ sandbox copy */

test("a recorded sandbox reads as enforced, unenforced or unknown", () => {
  assert.equal(sandboxLabel("required:bwrap:enforced"), "已强制隔离");
  assert.equal(sandboxLabel("preferred:sandbox-exec:unenforced"), "未强制隔离");
  assert.equal(sandboxLabel("required:none:enforced"), "已强制隔离");
  assert.equal(sandboxLabel("ENFORCED"), "已强制隔离");
  assert.equal(sandboxLabel(""), "未知");
  assert.equal(sandboxLabel(null), "未知");
  assert.equal(sandboxLabel(undefined), "未知");
  assert.equal(sandboxLabel("unknown"), "未知");
  assert.equal(sandboxLabel("required:bwrap"), "未知", "只写了策略与后端时不能猜隔离结果");
  assert.equal(sandboxState("preferred:sandbox-exec:unenforced"), "unenforced");
  assert.equal(sandboxState("off:none:unenforced"), "unenforced");
});

/* ------------------------------------------------------- queue and cleanup */

test("the queue line says what the active run is and how far it has come", () => {
  assert.equal(queueStateText(null), "执行器状态未知");
  assert.equal(queueStateText({ workerRunning: false, activeRunId: null, concurrency: 1 }), "执行器未启动 · 1 并发");
  assert.equal(
    queueStateText({ workerRunning: true, activeRunId: 9, activeLabel: "参数搜索与滚动验证 BTCUSDT 1h", activeProgress: 42.6, concurrency: 2 }),
    "执行器运行中 · 当前 #9 参数搜索与滚动验证 BTCUSDT 1h 43% · 2 并发",
  );
  assert.equal(
    queueStateText({ workerRunning: true, activeRunId: 9, concurrency: 1 }),
    "执行器运行中 · 当前 #9 · 1 并发",
    "引擎没报告标签与进度时就不写这两段",
  );
});

test("a dry-run plan is never read as a deletion", () => {
  const plan = readPruneReport({
    policy: { keepRuns: 20, keepDays: 7 },
    runs: [1, 2, 3],
    factorRuns: [8],
    bytes: 2_500_000,
  }, true);
  assert.deepEqual(plan.policy, { keepRuns: 20, keepDays: 7 });
  assert.deepEqual(plan.runs, [1, 2, 3]);
  assert.deepEqual(plan.factorRuns, [8]);
  assert.equal(plan.deletedRuns, 0, "计划不是删除");
  assert.equal(plan.deletedFactorRuns, 0);
  assert.equal(plan.dryRun, true);
  assert.equal(plan.remaining, null);
  assert.equal(megabytesText(plan.bytes), "2.50 MB");

  const applied = readPruneReport({
    policy: { keepRuns: 20, keepDays: 7 },
    runs: [1, 2, 3],
    factorRuns: [8],
    bytes: 2_500_000,
    deletedRuns: 3,
    deletedFactorRuns: 1,
    dryRun: false,
    remaining: { queued: 1, running: 0, done: 4, failed: 0, cancelled: 0, total: 5, activeCount: 1, hasActive: true },
  }, false);
  assert.equal(applied.deletedRuns, 3);
  assert.equal(applied.deletedFactorRuns, 1);
  assert.equal(applied.dryRun, false);
  assert.equal(applied.remaining?.total, 5);
  assert.equal(applied.remaining?.hasActive, true);
  assert.equal(megabytesText(500), "500 B");
  assert.equal(megabytesText(null), "—");
});

/** A minimal RFC 4180 field splitter, used only to prove the export is parseable. */
function splitCsv(line: string): string[] {
  const fields: string[] = [];
  let current = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (quoted) {
      if (char === '"' && line[index + 1] === '"') { current += '"'; index += 1; }
      else if (char === '"') quoted = false;
      else current += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") { fields.push(current); current = ""; }
    else current += char;
  }
  fields.push(current);
  return fields;
}
