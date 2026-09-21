/**
 * The backtest result centre's contract.
 *
 * A study posted to `POST /api/backtest/runs` is queued and executed by the
 * engine's background worker instead of blocking the browser. This module owns
 * the request shapes and the pure helpers the workspace renders from, so the
 * table, the detail panel and the unit tests agree on what 进度、耗时 and
 * 头部指标 mean. Everything here is defensive about missing fields: a result
 * that has not produced a number yet must show "—", never a guessed value.
 */

import { countText } from "./run-figures";

export type RunStatus = "queued" | "running" | "done" | "failed" | "cancelled";
export type RunKind = "backtest" | "validate" | "portfolio" | "factors" | "campaign" | "cpa_ablation";
export type RunVerdict = "pass" | "warn" | "fail" | "info";

/** The engine's own kind tokens, for reading a run body defensively. */
export const RUN_KINDS: RunKind[] = ["backtest", "validate", "portfolio", "factors", "campaign", "cpa_ablation"];
export const RUN_STATUSES: RunStatus[] = ["queued", "running", "done", "failed", "cancelled"];

/**
 * The scope a factor run's headline carries.
 *
 * A factor run measures coverage, not profit: it has no net return and no
 * drawdown. The engine states that in `headline.scope`, and the page looks for
 * that sentence instead of assuming every row with null metrics is broken.
 */
export const FACTOR_SCOPE = "因子覆盖率（无盈亏指标）";

/** The six numbers the queue board advertises without loading a full result. */
export interface RunHeadline {
  /** Which part of the study these numbers describe (样本外测试段 / 组合合并账本 …). */
  scope?: string;
  netReturnPct: number | null;
  maxDrawdownPct: number | null;
  trades: number | null;
  sharpe: number | null;
  profitFactor: number | null;
  winRatePct: number | null;
  /** Factor runs only: how many factors the request named. */
  factors?: number | null;
  /** Factor runs only: how many of them produced at least one value. */
  coveredFactors?: number | null;
  /** Factor runs only: the median count of bars that produced a value. */
  medianCoverage?: number | null;
  /** Factor runs only: the bars the run read. */
  bars?: number | null;
}

export interface RunVerdictNote {
  kind: string;
  verdict: RunVerdict;
  detail: string;
  /** Which isolation the validator ran under, e.g. `preferred:sandbox-exec:unenforced`. */
  sandbox?: string;
}

export interface ValidationVerdict extends RunVerdictNote {
  statistic: number | null;
  pValue: number | null;
  threshold: number | null;
}

export interface ArtifactEntry {
  name: string;
  mediaType: string;
  bytes: number;
  sha256: string;
  createdTs: number;
}

export interface RunSummary {
  id: number;
  kind: RunKind;
  status: RunStatus;
  label: string;
  symbol: string | null;
  displaySymbol: string | null;
  symbols: string[];
  interval: string;
  strategyId: string;
  strategyVersion: string;
  progress: number;
  progressLabel: string;
  stage: string;
  attempts: number;
  queuedTs: number;
  startedTs: number | null;
  finishedTs: number | null;
  durationMs: number | null;
  error: string | null;
  errorKind: "" | "not_ready" | "invalid" | "internal" | "cancelled" | string;
  headline: RunHeadline;
  artifacts: string[];
  verdicts: RunVerdictNote[];
  dataReady: boolean | null;
  degraded: boolean | null;
  missingData: string[];
}

export interface RunDetail extends RunSummary {
  /** The exact submitted study body, so a result can be reproduced. */
  request: Record<string, unknown>;
  /** The full study payload, in the same shape the synchronous endpoints return. */
  result: Record<string, unknown> | null;
  artifactIndex: ArtifactEntry[];
  validation: ValidationVerdict[];
  /** The gate's verdict block: title / detail / action / checks. */
  readiness: Record<string, unknown> | null;
}

export interface RunQueue {
  workerRunning: boolean;
  activeRunId: number | null;
  concurrency: number;
  /** What the active run is, and how far it has come, when the worker reports it. */
  activeLabel?: string | null;
  activeProgress?: number | null;
  /** Why the worker is not running, when it failed to start. */
  workerError?: string | null;
}

export interface RunTally {
  queued: number;
  running: number;
  done: number;
  failed: number;
  cancelled: number;
  total: number;
  activeCount: number;
  hasActive: boolean;
}

export interface RunBoard {
  summary: RunTally;
  runs: RunSummary[];
  queue: RunQueue;
}

export interface RunFilter {
  status?: RunStatus | "all";
  kind?: RunKind | "all";
  /** Matches id, symbol, label and strategy id, case-insensitively. */
  query?: string;
  limit?: number;
}

export interface SubmitOutcome {
  run: RunSummary;
  deduplicated: boolean;
}

export interface ReadinessCheck {
  key: string;
  label: string;
  status: string;
  detail: string;
}

/** What the run read, and what it could not read. */
export interface RunProvenance {
  severity: "formal" | "degraded";
  title: string;
  detail: string;
  action: string;
  missing: string[];
  impacts: string[];
  /** The checks that actually blocked the study, when the gate named them. */
  blocking: ReadinessCheck[];
  checks: ReadinessCheck[];
}

/* ------------------------------------------------------------------ labels */

export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  queued: "排队中",
  running: "运行中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export const RUN_KIND_LABEL: Record<RunKind, string> = {
  backtest: "回测",
  validate: "参数验证",
  portfolio: "组合回测",
  factors: "因子计算",
  campaign: "代理战役",
  cpa_ablation: "CPA 消融",
};

export const VERDICT_LABEL: Record<RunVerdict, string> = {
  pass: "通过",
  warn: "关注",
  fail: "不通过",
  // The validator's own tone for a reading that is context, not a test outcome
  // (路径风险 describes order sensitivity rather than passing or failing).
  info: "提示",
};

const ERROR_KIND_LABEL: Record<string, string> = {
  "": "任务失败",
  not_ready: "数据未就绪",
  invalid: "请求无效",
  internal: "引擎内部错误",
  cancelled: "已取消",
};

/** Gate checks speak in status tokens; the page speaks Chinese. */
const CHECK_STATUS_LABEL: Record<string, string> = {
  pass: "通过",
  ok: "通过",
  ready: "就绪",
  healthy: "正常",
  warn: "关注",
  degraded: "降级",
  partial: "不完整",
  skipped: "跳过",
  fail: "未通过",
  failed: "失败",
  blocked: "阻塞",
  error: "错误",
  critical: "严重",
  not_ready: "未就绪",
};

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABEL[status as RunStatus] ?? status;
}

export function checkStatusLabel(status: string): string {
  return CHECK_STATUS_LABEL[status.trim().toLowerCase()] ?? (status || "—");
}

export function runKindLabel(kind: string): string {
  return RUN_KIND_LABEL[kind as RunKind] ?? kind;
}

export function verdictLabel(verdict: string): string {
  return VERDICT_LABEL[verdict as RunVerdict] ?? verdict;
}

export function errorKindLabel(errorKind: string): string {
  return ERROR_KIND_LABEL[errorKind] ?? "任务失败";
}

export function isActiveStatus(status: string): boolean {
  return status === "queued" || status === "running";
}

/* --------------------------------------------------------------- isolation */

export const SANDBOX_LABEL = {
  enforced: "已强制隔离",
  unenforced: "未强制隔离",
  unknown: "未知",
} as const;

export type SandboxState = keyof typeof SANDBOX_LABEL;

/**
 * How a recorded isolation reads in Chinese.
 *
 * The engine records provenance as `policy:backend:enforced|unenforced` (for
 * example `preferred:sandbox-exec:unenforced`), and older rows may carry nothing
 * at all. Only the last token decides; anything else — including a policy name
 * the page has never seen — is reported as 未知 rather than guessed at.
 */
export function sandboxState(value: string | null | undefined): SandboxState {
  const token = (value ?? "").trim().toLowerCase();
  if (!token) return "unknown";
  const last = token.split(":").pop() ?? "";
  if (last === "enforced") return "enforced";
  if (last === "unenforced") return "unenforced";
  return "unknown";
}

export function sandboxLabel(value: string | null | undefined): string {
  return SANDBOX_LABEL[sandboxState(value)];
}

/* ------------------------------------------------------------- formatting */

/** Elapsed engine time. Null means "not finished yet", which is not zero. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  const seconds = Math.max(0, ms) / 1000;
  if (seconds < 1) return `${Math.round(Math.max(0, ms))} 毫秒`;
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  const whole = Math.round(seconds);
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes} 分 ${whole % 60} 秒`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function formatRunTime(ts: number | null | undefined): string {
  if (ts === null || ts === undefined || !Number.isFinite(ts) || ts <= 0) return "—";
  return new Date(ts).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Artifact payloads are served raw; the label only describes the encoding. */
export function artifactMediaLabel(mediaType: string): string {
  const type = mediaType.split(";")[0].trim().toLowerCase();
  if (type === "application/json") return "JSON";
  if (type === "text/csv") return "CSV";
  if (type === "text/plain") return "文本";
  return mediaType || "—";
}

/* ---------------------------------------------------------------- selectors */

export function summariseRuns(runs: RunSummary[]): RunTally {
  const tally: RunTally = { queued: 0, running: 0, done: 0, failed: 0, cancelled: 0, total: runs.length, activeCount: 0, hasActive: false };
  for (const run of runs) {
    if (run.status === "queued") tally.queued += 1;
    else if (run.status === "running") tally.running += 1;
    else if (run.status === "done") tally.done += 1;
    else if (run.status === "failed") tally.failed += 1;
    else if (run.status === "cancelled") tally.cancelled += 1;
  }
  tally.activeCount = tally.queued + tally.running;
  tally.hasActive = tally.activeCount > 0;
  return tally;
}

export function filterRuns(runs: RunSummary[], filter: RunFilter): RunSummary[] {
  const status = filter.status ?? "all";
  const kind = filter.kind ?? "all";
  const query = (filter.query ?? "").trim().toLowerCase();
  return runs.filter((run) => {
    if (status !== "all" && run.status !== status) return false;
    if (kind !== "all" && run.kind !== kind) return false;
    if (!query) return true;
    // The four things an operator has in hand when looking for a run: its number,
    // the contract, the label they typed, and the strategy that produced it.
    const haystack = [
      String(run.id),
      run.symbol ?? "",
      run.displaySymbol ?? "",
      ...(run.symbols ?? []),
      run.label,
      run.strategyId,
    ];
    return haystack.some((field) => field.toLowerCase().includes(query));
  });
}

/* ------------------------------------------------------------- factor runs */

/** A factor run measures coverage; its P&L columns are null by construction. */
export function isFactorRun(run: Pick<RunSummary, "kind" | "headline"> | null | undefined): boolean {
  if (!run) return false;
  if (run.kind === "factors") return true;
  if (run.headline?.scope === FACTOR_SCOPE) return true;
  // No scope, but the coverage numbers are there: an older factor row still reads
  // as a factor run instead of as six dashes with no explanation.
  return run.headline?.factors != null && run.headline.netReturnPct == null;
}

/**
 * A factor run's coverage in one line.
 *
 * The numbers the engine reports are counts of bars, so they are printed as
 * counts; a part the run did not report is left out instead of being shown as
 * zero. A run that is not a factor run has no coverage line at all.
 */
export function factorCoverageText(run: Pick<RunSummary, "kind" | "headline"> | null | undefined): string {
  if (!isFactorRun(run)) return "—";
  const headline = run?.headline ?? null;
  const factors = finiteNumber(headline?.factors);
  const covered = finiteNumber(headline?.coveredFactors);
  const median = finiteNumber(headline?.medianCoverage);
  const bars = finiteNumber(headline?.bars);
  const parts: string[] = [];
  if (factors !== null) parts.push(`因子 ${countText(factors)} 个`);
  if (covered !== null) parts.push(`有值 ${countText(covered)}`);
  if (median !== null) parts.push(`中位覆盖 ${countText(median)} 根${bars !== null ? ` / ${countText(bars)} 根` : ""}`);
  else if (bars !== null) parts.push(`窗口 ${countText(bars)} 根`);
  return parts.length > 0 ? parts.join(" · ") : "覆盖率未报告";
}

/* ------------------------------------------------------------ result reads */

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function numberIn(source: Record<string, unknown> | null, ...keys: string[]): number | null {
  if (!source) return null;
  for (const key of keys) {
    const value = finiteNumber(source[key]);
    if (value !== null) return value;
  }
  return null;
}

function textIn(source: Record<string, unknown> | null, key: string): string {
  const value = source?.[key];
  return typeof value === "string" ? value : "";
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}

function emptyHeadline(): RunHeadline {
  return { netReturnPct: null, maxDrawdownPct: null, trades: null, sharpe: null, profitFactor: null, winRatePct: null };
}

/**
 * The headline row a result implies, when the server did not precompute one.
 *
 * Both spellings are accepted on purpose: the study payloads are the engine's
 * snake_case documents, while the queue contract is camelCase. A field that is
 * absent stays null so the page can print "—" instead of a fabricated number.
 */
export function headlineOf(result: Record<string, unknown> | null | undefined): RunHeadline {
  const payload = asRecord(result);
  if (!payload) return emptyHeadline();
  const metrics = asRecord(payload.metrics);
  const trades = payload.trades;
  const headline: RunHeadline = {
    netReturnPct: numberIn(payload, "netReturnPct", "net_return_pct") ?? numberIn(metrics, "netReturnPct", "net_return_pct"),
    maxDrawdownPct: numberIn(payload, "maxDrawdownPct", "max_drawdown_pct") ?? numberIn(metrics, "maxDrawdownPct", "max_drawdown_pct"),
    trades: Array.isArray(trades) ? trades.length : numberIn(payload, "trades", "tradeCount", "trade_count") ?? numberIn(metrics, "trades", "trade_count"),
    sharpe: numberIn(payload, "sharpe", "sharpeRatio", "sharpe_ratio") ?? numberIn(metrics, "sharpe", "sharpeRatio", "sharpe_ratio"),
    profitFactor: numberIn(payload, "profitFactor", "profit_factor") ?? numberIn(metrics, "profitFactor", "profit_factor"),
    winRatePct: numberIn(payload, "winRatePct", "win_rate_pct") ?? numberIn(metrics, "winRatePct", "win_rate_pct"),
  };
  // A factor result carries no P&L at all; its headline is the coverage of the
  // window it read. These keys are added only when the payload really has such
  // numbers, so an ordinary backtest keeps the six-field shape it always had.
  const coverage = asRecord(payload.coverage);
  if (coverage || payload.factorRunId !== undefined || payload.factors !== undefined) {
    const counts = Object.values(coverage ?? {})
      .filter((value): value is number => typeof value === "number" && Number.isFinite(value) && value > 0)
      .sort((left, right) => left - right);
    headline.factors = numberIn(payload, "factors") ?? (coverage ? Object.keys(coverage).length : null);
    headline.coveredFactors = numberIn(payload, "coveredFactors") ?? (coverage ? counts.length : null);
    headline.medianCoverage = numberIn(payload, "medianCoverage") ?? (counts.length ? counts[Math.floor(counts.length / 2)] : null);
    headline.bars = numberIn(payload, "bars");
  }
  return headline;
}

/** The queue's own headline wins; a null field falls back to the result payload. */
export function resolveHeadline(headline: RunHeadline | null | undefined, result: Record<string, unknown> | null | undefined): RunHeadline {
  const derived = headlineOf(result);
  if (!headline) return derived;
  const resolved: RunHeadline = {
    netReturnPct: finiteNumber(headline.netReturnPct) ?? derived.netReturnPct,
    maxDrawdownPct: finiteNumber(headline.maxDrawdownPct) ?? derived.maxDrawdownPct,
    trades: finiteNumber(headline.trades) ?? derived.trades,
    sharpe: finiteNumber(headline.sharpe) ?? derived.sharpe,
    profitFactor: finiteNumber(headline.profitFactor) ?? derived.profitFactor,
    winRatePct: finiteNumber(headline.winRatePct) ?? derived.winRatePct,
  };
  // A factor headline's coverage fields travel the same way as the six P&L
  // numbers, so a queue row that only knows one side does not erase the other.
  const scope = headline.scope || derived.scope;
  if (scope) resolved.scope = scope;
  const factors = finiteNumber(headline.factors) ?? derived.factors;
  const coveredFactors = finiteNumber(headline.coveredFactors) ?? derived.coveredFactors;
  const medianCoverage = finiteNumber(headline.medianCoverage) ?? derived.medianCoverage;
  const bars = finiteNumber(headline.bars) ?? derived.bars;
  if (factors != null) resolved.factors = factors;
  if (coveredFactors != null) resolved.coveredFactors = coveredFactors;
  if (medianCoverage != null) resolved.medianCoverage = medianCoverage;
  if (bars != null) resolved.bars = bars;
  return resolved;
}

/** `{time, equity}` points, accepting the engine's snake_case curve as well. */
export function equityPointsOf(result: Record<string, unknown> | null | undefined): Array<{ time: number; equity: number }> {
  const payload = asRecord(result);
  const selected = asRecord(payload?.selectedRun);
  // A single backtest and a portfolio carry the curve at the top level; a
  // validation run's own curve belongs to the parameters it selected, and says so
  // in the panel rather than being drawn as if it were the out-of-sample result.
  const raw = payload?.equityCurve ?? payload?.equity_curve ?? selected?.equityCurve;
  if (!Array.isArray(raw)) return [];
  const points: Array<{ time: number; equity: number }> = [];
  for (const entry of raw) {
    const point = asRecord(entry);
    const time = finiteNumber(point?.time);
    const equity = finiteNumber(point?.equity);
    if (time === null || equity === null) continue;
    points.push({ time, equity });
  }
  points.sort((left, right) => left.time - right.time);
  return points;
}

/* ------------------------------------------------------- execution model */

/** How a study turned signals into fills, as the engine states it. */
export interface ExecutionModel {
  /** When a signal became a fill, in one sentence. */
  fillRule: string;
  signalBar: string;
  fillPrice: string;
  /** Bars between the signal and the fill; 0 means the next open. */
  latencyBars: number | null;
  slippageModel: string;
  slippageBps: number | null;
  impactCoefficient: number | null;
  /** Share of a bar's volume an order may take, 0–1. */
  maxParticipation: number | null;
  partialFill: string;
  unfilledOrders: number | null;
  unfilledNotional: number | null;
  feeTiming: string;
  fundingTiming: string;
  liquidationBasis: string;
  thinBarPolicy: string;
  /** What the model deliberately does not simulate, in the engine's words. */
  simplifications: string[];
}

/**
 * The execution assumptions a result carries.
 *
 * The engine states them once under `execution_model`; the same numbers also sit
 * in `costModel` and `data_quality`, and a field the dedicated block omits is
 * read from there rather than defaulted. A result that states nothing at all
 * returns null, so the page renders no block instead of an invented one.
 */
export function executionModelOf(result: Record<string, unknown> | null | undefined): ExecutionModel | null {
  const payload = asRecord(result);
  if (!payload) return null;
  // A validation run's executed backtest is its selected parameter set, exactly
  // as its equity curve is; the top level of such a study carries only the cost
  // model, so both places are read before anything is called missing.
  const selected = asRecord(payload.selectedRun) ?? asRecord(payload.selected_run);
  const block = asRecord(payload.execution_model) ?? asRecord(payload.executionModel)
    ?? asRecord(selected?.execution_model) ?? asRecord(selected?.executionModel);
  const cost = asRecord(payload.costModel) ?? asRecord(payload.cost_model);
  const quality = asRecord(payload.data_quality) ?? asRecord(payload.dataQuality)
    ?? asRecord(selected?.data_quality) ?? asRecord(selected?.dataQuality);
  const model: ExecutionModel = {
    fillRule: textIn(block, "fillRule"),
    signalBar: textIn(block, "signalBar"),
    fillPrice: textIn(block, "fillPrice"),
    latencyBars: numberIn(block, "latencyBars") ?? numberIn(cost, "latencyBars") ?? numberIn(quality, "latencyBars"),
    slippageModel: textIn(block, "slippageModel") || textIn(cost, "slippageModel"),
    slippageBps: numberIn(block, "slippageBps") ?? numberIn(cost, "slippageBps"),
    impactCoefficient: numberIn(block, "impactCoefficient") ?? numberIn(cost, "impactCoefficient"),
    maxParticipation: numberIn(block, "maxParticipation") ?? numberIn(cost, "maxParticipation") ?? numberIn(quality, "maxParticipation"),
    partialFill: textIn(block, "partialFill") || textIn(cost, "partialFill") || textIn(quality, "partialFill"),
    unfilledOrders: numberIn(block, "unfilledOrders") ?? numberIn(quality, "unfilledOrders"),
    unfilledNotional: numberIn(block, "unfilledNotional") ?? numberIn(quality, "unfilledNotional"),
    feeTiming: textIn(block, "feeTiming"),
    fundingTiming: textIn(block, "fundingTiming"),
    liquidationBasis: textIn(block, "liquidationBasis"),
    thinBarPolicy: textIn(block, "thinBarPolicy") || textIn(block, "fillOnThin"),
    simplifications: stringList(block?.simplifications),
  };
  // A block that says nothing is not a model: an older result must not render an
  // empty one, so the whole block is dropped unless something was actually stated.
  const stated = model.fillRule !== "" || model.fillPrice !== "" || model.slippageModel !== ""
    || model.partialFill !== "" || model.simplifications.length > 0
    || model.latencyBars !== null || model.maxParticipation !== null;
  return stated ? model : null;
}

/**
 * What the run read, and what it could not.
 *
 * Two shapes reach this function: the queue's own verdict block (title / detail
 * / action) and the study envelope's gate verdict (`ok`, `blocking`, `checks`,
 * `missing`, `impacts`). Both are read here so the page never has to interpret
 * `errorKind` on its own, and a title is only invented when the block has none.
 */
export function provenanceOf(
  run: Pick<RunSummary, "degraded" | "dataReady" | "errorKind" | "missingData">,
  readiness: Record<string, unknown> | null | undefined,
  result: Record<string, unknown> | null | undefined,
): RunProvenance {
  const block = asRecord(readiness);
  const payload = asRecord(result);
  const missing = unique([
    ...(run.missingData ?? []),
    ...stringList(block?.missing),
    ...stringList(payload?.missingData ?? payload?.missing_data),
  ]);
  const impacts = unique([
    ...stringList(block?.impacts),
    ...stringList(payload?.dataImpacts ?? payload?.data_impacts),
  ]);
  const blocking = checkList(block?.blocking);
  const checks = checkList(block?.checks);
  const notReady = run.errorKind === "not_ready" || run.dataReady === false || block?.ok === false || blocking.length > 0;
  const degraded = notReady || run.degraded === true || block?.degraded === true || missing.length > 0;
  const inferredTitle = notReady
    ? "数据未就绪"
    : degraded
      ? (run.degraded === true || block?.degraded === true ? "数据降级运行" : "数据不完整")
      : "数据就绪（正式口径）";
  return {
    severity: degraded ? "degraded" : "formal",
    title: textIn(block, "title") || inferredTitle,
    detail: textIn(block, "detail"),
    action: textIn(block, "action"),
    missing,
    impacts,
    blocking,
    checks,
  };
}

function checkList(value: unknown): ReadinessCheck[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): ReadinessCheck[] => {
    const check = asRecord(entry);
    if (!check) return [];
    return [{ key: textIn(check, "key"), label: textIn(check, "label"), status: textIn(check, "status"), detail: textIn(check, "detail") }];
  });
}

/* ------------------------------------------------------------ queue reads */

/**
 * One queue row, read defensively.
 *
 * The list endpoint is trusted to send the contract, but a 202 transfer's body is
 * built on the way out of a submit: a field the page needs must not be able to
 * make the notice unrenderable. An id is the one thing a run cannot be without,
 * so a body without one is not a run and this returns null.
 */
export function readRunSummary(value: unknown): RunSummary | null {
  const source = asRecord(value);
  const id = finiteNumber(source?.id);
  if (!source || id === null) return null;
  const kind = textIn(source, "kind");
  const status = textIn(source, "status");
  return {
    id: Math.floor(id),
    kind: (RUN_KINDS.includes(kind as RunKind) ? kind : "backtest") as RunKind,
    status: (RUN_STATUSES.includes(status as RunStatus) ? status : "queued") as RunStatus,
    label: textIn(source, "label"),
    symbol: textIn(source, "symbol") || null,
    displaySymbol: textIn(source, "displaySymbol") || null,
    symbols: stringList(source.symbols),
    interval: textIn(source, "interval"),
    strategyId: textIn(source, "strategyId"),
    strategyVersion: textIn(source, "strategyVersion"),
    progress: numberIn(source, "progress") ?? 0,
    progressLabel: textIn(source, "progressLabel"),
    stage: textIn(source, "stage"),
    attempts: numberIn(source, "attempts") ?? 1,
    queuedTs: numberIn(source, "queuedTs") ?? 0,
    startedTs: numberIn(source, "startedTs"),
    finishedTs: numberIn(source, "finishedTs"),
    durationMs: numberIn(source, "durationMs"),
    error: textIn(source, "error") || null,
    errorKind: textIn(source, "errorKind"),
    headline: headlineOf(asRecord(source.headline)),
    artifacts: stringList(source.artifacts),
    verdicts: readVerdictNotes(source.verdicts),
    dataReady: typeof source.dataReady === "boolean" ? source.dataReady : null,
    degraded: typeof source.degraded === "boolean" ? source.degraded : null,
    missingData: stringList(source.missingData),
  };
}

/** Verdict rows as the queue list sends them, sandbox included when recorded. */
export function readVerdictNotes(value: unknown): RunVerdictNote[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): RunVerdictNote[] => {
    const row = asRecord(entry);
    const kind = textIn(row, "kind");
    if (!row || !kind) return [];
    const verdict = textIn(row, "verdict");
    const sandbox = textIn(row, "sandbox");
    return [{
      kind,
      verdict: (verdict || "info") as RunVerdict,
      detail: textIn(row, "detail"),
      ...(sandbox ? { sandbox } : {}),
    }];
  });
}

/**
 * The queue's own state in one line, including what the active run is doing now.
 *
 * `activeLabel` and `activeProgress` describe the run the worker picked up; when
 * the engine does not send them the sentence simply omits that clause instead of
 * printing a placeholder for work the page cannot see.
 */
export function queueStateText(queue: RunQueue | null | undefined): string {
  if (!queue) return "执行器状态未知";
  const parts = [queue.workerRunning ? "执行器运行中" : "执行器未启动"];
  if (queue.activeRunId != null) {
    const label = (queue.activeLabel ?? "").trim();
    const progress = finiteNumber(queue.activeProgress);
    parts.push(`当前 #${queue.activeRunId}${label ? ` ${label}` : ""}${progress === null ? "" : ` ${Math.round(progress)}%`}`);
  }
  if (finiteNumber(queue.concurrency) !== null) parts.push(`${queue.concurrency} 并发`);
  return parts.join(" · ");
}

/* -------------------------------------------------------- heavy studies */

/** The engine's own bounds for a queued parameter search. */
export const STUDY_BARS_MIN = 30;
export const STUDY_BARS_MAX = 5_000;
export const STUDY_GRID_MAX = 8;
export const STUDY_WINDOWS_MAX = 12;

/** The parameter search and walk-forward a queued `validate` run submits. */
export interface ValidationStudyInput {
  symbol: string;
  interval: string;
  bars: number;
  fastGrid: number[];
  slowGrid: number[];
  walkForwardWindows: number;
  allowDegraded: boolean;
}

/** One study as the form holds it: every field still text, as typed. */
export interface ValidationStudyForm {
  symbol: string;
  interval: string;
  bars: string;
  fastGrid: string;
  slowGrid: string;
  walkForwardWindows: string;
  allowDegraded: boolean;
}

export type ValidationStudyRead =
  | { ok: true; input: ValidationStudyInput }
  | { ok: false; error: string };

/**
 * A comma-separated integer grid.
 *
 * Null when it is empty, malformed or longer than the engine accepts — the
 * caller says which of those is wrong in Chinese; this function only judges.
 */
export function parseGridInput(text: string): number[] | null {
  const parts = text.split(/[,，\s]+/).map((part) => part.trim()).filter((part) => part.length > 0);
  if (parts.length === 0 || parts.length > STUDY_GRID_MAX) return null;
  const values: number[] = [];
  for (const part of parts) {
    if (!/^\d+$/.test(part)) return null;
    const value = Number(part);
    if (!Number.isSafeInteger(value) || value < 1) return null;
    values.push(value);
  }
  return values;
}

/**
 * The study a form implies, or the reason it cannot be submitted.
 *
 * The limits are the engine's (`ValidationRequest`), checked here so nonsense is
 * answered in the form instead of by a 422 after a round trip.
 */
export function readValidationStudy(form: ValidationStudyForm): ValidationStudyRead {
  const symbol = form.symbol.trim();
  if (!symbol) return { ok: false, error: "请先选择或填写合约代码。" };
  const bars = Number(form.bars);
  if (!Number.isInteger(bars) || bars < STUDY_BARS_MIN || bars > STUDY_BARS_MAX) {
    return { ok: false, error: `K 线根数需为 ${STUDY_BARS_MIN} 到 ${STUDY_BARS_MAX} 之间的整数。` };
  }
  const fastGrid = parseGridInput(form.fastGrid);
  if (!fastGrid) return { ok: false, error: `快线周期需为 1 到 ${STUDY_GRID_MAX} 个逗号分隔的正整数，例如 5,9,20。` };
  const slowGrid = parseGridInput(form.slowGrid);
  if (!slowGrid) return { ok: false, error: `慢线周期需为 1 到 ${STUDY_GRID_MAX} 个逗号分隔的正整数，例如 21,50,100。` };
  const walkForwardWindows = Number(form.walkForwardWindows);
  if (!Number.isInteger(walkForwardWindows) || walkForwardWindows < 1 || walkForwardWindows > STUDY_WINDOWS_MAX) {
    return { ok: false, error: `滚动窗口数需为 1 到 ${STUDY_WINDOWS_MAX} 之间的整数。` };
  }
  return {
    ok: true,
    input: { symbol, interval: form.interval, bars, fastGrid, slowGrid, walkForwardWindows, allowDegraded: form.allowDegraded },
  };
}

/** The exact body `POST /api/backtest/runs` queues as a `validate` study. */
export function validationStudyBody(input: ValidationStudyInput): Record<string, unknown> {
  return {
    symbol: input.symbol.trim(),
    timeframe: input.interval,
    bars: Math.floor(input.bars),
    strategyId: "ma_cross",
    fastGrid: [...input.fastGrid],
    slowGrid: [...input.slowGrid],
    walkForwardWindows: Math.floor(input.walkForwardWindows),
    allowDegraded: input.allowDegraded,
  };
}

/* -------------------------------------------------------------------- HTTP */

export function artifactUrl(id: number, name: string): string {
  return `/api/backtest/runs/${id}/artifacts/${encodeURIComponent(name)}`;
}

/**
 * The engine reads `detail` on every failure; a 409 carries the gate's verdict
 * as a JSON document inside that string, so unwrap it before showing the
 * operator a join of title / detail / action.
 */
function describeFailure(payload: unknown, status: number): string {
  const detail = asRecord(payload)?.detail;
  if (typeof detail === "string") {
    try {
      const parsed = asRecord(JSON.parse(detail));
      const joined = [parsed?.title, parsed?.detail, parsed?.action]
        .filter((part): part is string => typeof part === "string" && part.length > 0)
        .join("：");
      return joined || detail;
    } catch {
      return detail;
    }
  }
  if (Array.isArray(detail)) {
    const parts = detail.flatMap((entry) => {
      const item = asRecord(entry);
      const message = item ? textIn(item, "msg") : "";
      return message ? [message] : [];
    });
    if (parts.length) return parts.join("；");
  }
  return `回测结果中心返回 ${status}`;
}

async function send<T>(method: string, path: string, body: unknown, timeoutMs: number): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (reason) {
    throw new Error(`无法连接本地引擎：${reason instanceof Error ? reason.message : String(reason)}`);
  }
  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) throw new Error(describeFailure(payload, response.status));
  return payload as T;
}

/** Read-only board calls follow the history-data surface: `detail`, then a status line. */
async function json<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { signal: AbortSignal.timeout(30_000) });
  } catch (reason) {
    throw new Error(`无法连接本地引擎：${reason instanceof Error ? reason.message : String(reason)}`);
  }
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  if (!response.ok) throw new Error(typeof body?.detail === "string" ? body.detail : `回测结果中心返回 ${response.status}`);
  return body as T;
}

/** Queue a study. The engine deduplicates an identical, still-active request. */
export function submitRun(kind: RunKind, request: Record<string, unknown>, label?: string): Promise<SubmitOutcome> {
  const body: Record<string, unknown> = { kind, request };
  const trimmed = label?.trim();
  if (trimmed) body.label = trimmed;
  return send<SubmitOutcome>("POST", "/api/backtest/runs", body, 30_000);
}

export function fetchRuns(filter: RunFilter = {}): Promise<RunBoard> {
  const params = new URLSearchParams();
  if (filter.status && filter.status !== "all") params.set("status", filter.status);
  if (filter.kind && filter.kind !== "all") params.set("kind", filter.kind);
  if (filter.limit && filter.limit > 0) params.set("limit", String(Math.floor(filter.limit)));
  const query = params.toString();
  return json<RunBoard>(`/api/backtest/runs${query ? `?${query}` : ""}`);
}

export async function fetchRun(id: number): Promise<RunDetail> {
  // The endpoint answers `{run: …}`, like the other detail routes; unwrap it here
  // so every caller gets the run itself and none of them has to remember which
  // shape this one uses.
  const body = await json<{ run: RunDetail }>(`/api/backtest/runs/${id}`);
  if (!body || typeof body !== "object" || !("run" in body) || !body.run) {
    throw new Error(`任务 #${id} 的响应缺少 run 字段`);
  }
  return body.run;
}

export function cancelRun(id: number): Promise<RunSummary> {
  return send<{ run: RunSummary }>("POST", `/api/backtest/runs/${id}/cancel`, undefined, 30_000).then((body) => body.run);
}

export function retryRun(id: number): Promise<RunSummary> {
  return send<{ run: RunSummary }>("POST", `/api/backtest/runs/${id}/retry`, undefined, 30_000).then((body) => body.run);
}

/** Rejected with a 409 while the run is still active. */
export function deleteRun(id: number): Promise<{ deleted: true; id: number }> {
  return send<{ deleted: true; id: number }>("DELETE", `/api/backtest/runs/${id}`, undefined, 30_000);
}

/* ---------------------------------------------------------- record cleanup */

/** The retention policy the engine applies, as `[retention]` in config.toml. */
export interface PrunePolicy {
  keepRuns: number;
  keepDays: number;
}

/** What a prune did, or what it would do. */
export interface PruneReport {
  policy: PrunePolicy;
  /** The run ids the policy selects. On a dry run this is the plan, not a result. */
  runs: number[];
  factorRuns: number[];
  bytes: number;
  deletedRuns: number;
  deletedFactorRuns: number;
  dryRun: boolean;
  /** Present only after a real prune: the queue's tally once it finished. */
  remaining: RunTally | null;
}

/**
 * Apply the retention policy, or report what applying it would do.
 *
 * The UI calls this twice by design: `dry_run=true` shows the policy and what it
 * would free, and only an explicit confirmation sends `dry_run=false`. Nothing
 * here runs on a timer, and nothing prunes as a side effect of looking.
 */
export async function pruneRuns(dryRun: boolean): Promise<PruneReport> {
  const payload = await send<unknown>(
    "POST",
    `/api/backtest/maintenance/prune?dry_run=${dryRun ? "true" : "false"}`,
    undefined,
    120_000,
  );
  return readPruneReport(payload, dryRun);
}

/**
 * The prune body, read defensively.
 *
 * A dry run answers the plan alone — no `deleted*` counters at all — so the
 * counters default to 0 and `dryRun` is taken from the request when the body does
 * not state it. The plan itself is never turned into a deletion count.
 */
export function readPruneReport(payload: unknown, dryRun: boolean): PruneReport {
  const body = asRecord(payload);
  if (!body) throw new Error("记录清理接口返回结构异常");
  const policy = asRecord(body.policy);
  const remaining = asRecord(body.remaining);
  return {
    policy: {
      keepRuns: numberIn(policy, "keepRuns") ?? 0,
      keepDays: numberIn(policy, "keepDays") ?? 0,
    },
    runs: integerList(body.runs),
    factorRuns: integerList(body.factorRuns),
    bytes: numberIn(body, "bytes") ?? 0,
    deletedRuns: numberIn(body, "deletedRuns") ?? 0,
    deletedFactorRuns: numberIn(body, "deletedFactorRuns") ?? 0,
    dryRun: typeof body.dryRun === "boolean" ? body.dryRun : dryRun,
    remaining: remaining ? summariseTally(remaining) : null,
  };
}

function integerList(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    const id = finiteNumber(entry);
    return id === null ? [] : [Math.floor(id)];
  });
}

/** The queue's own counters, read as they arrive rather than recomputed. */
function summariseTally(source: Record<string, unknown>): RunTally {
  const tally: RunTally = {
    queued: numberIn(source, "queued") ?? 0,
    running: numberIn(source, "running") ?? 0,
    done: numberIn(source, "done") ?? 0,
    failed: numberIn(source, "failed") ?? 0,
    cancelled: numberIn(source, "cancelled") ?? 0,
    total: numberIn(source, "total") ?? 0,
    activeCount: numberIn(source, "activeCount") ?? 0,
    hasActive: source.hasActive === true,
  };
  if (numberIn(source, "hasActive") === null) tally.hasActive = tally.queued + tally.running > 0;
  return tally;
}
