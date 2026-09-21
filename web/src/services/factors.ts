/**
 * The factor library and the statistical validator.
 *
 * `GET /api/factors` answers from the enabled factor plugin or — when none is
 * enabled — from the catalogue the engine stored the last time one was, so a
 * missing plugin is a state the page describes rather than an error. The same
 * discipline as the result centre applies here: a number the engine did not
 * report stays missing and is printed as "—", never as a zero. The pure helpers
 * at the bottom are what the panel renders from, so the tables and the unit
 * tests agree on what 覆盖率 means.
 */

import { queuedTransferOf, type StudyOutcome } from "./study-queue";

/* ------------------------------------------------------------------- types */

export interface FactorDefinition {
  id: string;
  name: string;
  family: string;
  mode: string;
  sources: string[];
  requiredFields: string[];
  warmupBars: number;
  supportedTimeframes: string[];
  implementationVersion: string;
  formulaHash: string;
  description: string;
  provider: string;
  providerVersion: string;
}

export interface FactorCatalog {
  provider: string | null;
  providerVersion: string;
  /** `plugin`: asked of the enabled provider. `stored`: what it last reported. */
  source: string;
  available: boolean;
  factors: FactorDefinition[];
  warnings: string[];
}

export interface FactorSeriesValue {
  time: number;
  value: number | null;
}

export interface FactorSeries {
  factorId: string;
  values: FactorSeriesValue[];
  implementationVersion?: string;
}

export interface FactorComputeResult {
  available: boolean;
  provider: string | null;
  runId: number;
  symbol: string;
  interval: string;
  snapshotHash: string;
  /** How many plugin round trips the request was split into (output cap). */
  batches: number;
  bars: number;
  series: FactorSeries[];
  /** Bars that produced a value, per factor id. A missing id was not reported. */
  coverage: Record<string, number>;
  warnings: string[];
  provenance: Record<string, unknown>;
}

export interface FactorRunSummary {
  id: number;
  provider: string;
  symbol: string;
  interval: string;
  snapshotHash: string;
  factorIds: string[];
  status: string;
  bars: number;
  /** Series the run stored; the values themselves are only sent on request. */
  seriesCount: number;
  coverage: Record<string, number>;
  warnings: string[];
  error: string | null;
  durationMs: number | null;
  createdTs: number;
  /** `policy:backend:enforced|unenforced`, as recorded when the plugin answered. */
  sandbox: string;
}

export interface FactorRunDetail extends FactorRunSummary {
  parameters: Record<string, unknown>;
  series: FactorSeries[];
  /** False when the values were not asked for — an empty `series` is not "none". */
  valuesIncluded: boolean;
}

export interface FactorValidationVerdict {
  kind: string;
  verdict: string;
  statistic: number | null;
  pValue: number | null;
  threshold: number | null;
  provider: string;
  detail: string;
}

export interface MultipleTestingReading {
  trials: number | null;
  factorCount: number | null;
  parameterCombinations: number | null;
  deflatedSharpe: number | null;
  probabilityOfBacktestOverfitting: number | null;
  method: string;
  /**
   * False when the validator refused to compute a correction. The note says what
   * it was missing; the page shows it instead of implying the correction was made.
   */
  applied: boolean | null;
  note: string;
}

/**
 * The validator's own reading of the run.
 *
 * Only the blocks the page renders are typed; the inference sections stay as the
 * validator sent them because their numbers already travel in the verdict rows.
 */
export interface ValidationAnalysis {
  provider: string;
  runId: string;
  algorithmVersion: string;
  seed: number | null;
  samples: number | null;
  multipleTesting: MultipleTestingReading | null;
  warnings: string[];
  /** Set when the validator could not answer part of the question at all. */
  unavailable: string;
  pathRisk: Record<string, unknown> | null;
  bootstrap: Record<string, unknown> | null;
  randomization: Record<string, unknown> | null;
  tailLoss: Record<string, unknown> | null;
  equityPercentiles: Record<string, number[]>;
}

export interface ValidationReport {
  available: boolean;
  provider: string | null;
  runId: number | null;
  analysis: ValidationAnalysis | null;
  verdicts: FactorValidationVerdict[];
}

export interface FactorComputeRequest {
  symbol: string;
  interval: string;
  bars: number;
  factorIds?: string[];
  parameters?: Record<string, unknown>;
}

export interface FactorRunFilter {
  symbol?: string;
  limit?: number;
}

export interface ValidateRunOptions {
  seed?: number;
}

/* ------------------------------------------------------------------ labels */

/**
 * Families the provider ships. An unknown family is shown as the token it is —
 * a new plugin family must not be silently relabelled as something it is not.
 */
const FAMILY_LABEL: Record<string, string> = {
  momentum: "动量",
  trend: "趋势",
  volatility: "波动率",
  distribution: "收益分布",
  oscillator: "摆动指标",
  liquidity: "流动性",
  structure: "市场结构",
  carry: "资金费收益",
  positioning: "持仓结构",
};

/** Verdict kinds the engine and the validator emit. */
const VERDICT_KIND_LABEL: Record<string, string> = {
  walk_forward: "滚动窗口验证",
  walk_forward_stability: "窗口参数稳定性",
  leakage: "未来数据检查",
  overfit: "过拟合检查",
  bootstrap: "自助重采样",
  randomization: "信号随机化",
  deflated_sharpe: "去偏夏普",
  pbo: "回测过拟合概率",
  path_risk: "路径风险",
  // 提示行：说明多重检验校正是否应用，不是一次通过/不通过的判定。
  multiple_testing_note: "多重检验校正说明",
};

export function factorFamilyLabel(family: string): string {
  const key = (family ?? "").trim();
  if (!key) return "—";
  return FAMILY_LABEL[key.toLowerCase()] ?? key;
}

export function verdictKindLabel(kind: string): string {
  const key = (kind ?? "").trim();
  if (!key) return "—";
  return VERDICT_KIND_LABEL[key] ?? key;
}

/* ---------------------------------------------------------------- coverage */

/** One factor's coverage over the window the run read. */
export interface FactorCoverageRow {
  factorId: string;
  /** Bars with a real value; null when the run did not report a countable number. */
  covered: number | null;
  bars: number;
  /** 0–100, or null when the count is unknown. A window of 0 bars covers nothing. */
  pct: number | null;
}

function windowBars(bars: number | null | undefined): number {
  return typeof bars === "number" && Number.isFinite(bars) && bars > 0 ? Math.floor(bars) : 0;
}

/**
 * Coverage as rows, in the order the engine reported the factors.
 *
 * Zero bars is not "unknown coverage": nothing was read, so nothing is covered
 * and the bar is drawn empty. A count that is not a number stays unknown so the
 * percent prints as "—" rather than as a zero the run never claimed.
 */
export function summariseCoverage(
  coverage: Record<string, number> | null | undefined,
  bars: number | null | undefined,
): FactorCoverageRow[] {
  const window = windowBars(bars);
  if (!coverage || typeof coverage !== "object" || Array.isArray(coverage)) return [];
  return Object.entries(coverage).map(([factorId, value]) => {
    const covered = typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : null;
    const pct = window <= 0 ? 0 : covered === null ? null : Math.min(100, (covered / window) * 100);
    return { factorId, covered, bars: window, pct };
  });
}

/**
 * The coverage one number can carry: what share of the requested values the run
 * actually produced. Null when the run reported no factors at all, because
 * "nothing to average" is not the same as "nothing was covered".
 */
export function factorCoveragePct(
  run: Pick<FactorRunSummary, "coverage" | "bars"> | null | undefined,
): number | null {
  const window = windowBars(run?.bars);
  if (window <= 0) return 0;
  const rows = summariseCoverage(run?.coverage, window);
  if (rows.length === 0) return null;
  let covered = 0;
  for (const row of rows) {
    // One unknown count would understate the total, so the total stays unknown.
    if (row.covered === null) return null;
    covered += row.covered;
  }
  return Math.min(100, (covered / (rows.length * window)) * 100);
}

/**
 * The best-covered factors, most covered first.
 *
 * Ties keep the engine's own order, an unknown percentage sorts last, and a
 * factor the run did not report can never appear — the list is never padded.
 */
export function topFactorsByCoverage(
  run: Pick<FactorRunSummary, "coverage" | "bars"> | null | undefined,
  count: number,
): FactorCoverageRow[] {
  if (!Number.isFinite(count) || count <= 0) return [];
  return summariseCoverage(run?.coverage, run?.bars)
    .map((row, index) => ({ row, index }))
    .sort((left, right) => {
      const a = left.row.pct ?? -1;
      const b = right.row.pct ?? -1;
      return a === b ? left.index - right.index : b - a;
    })
    .slice(0, Math.floor(count))
    .map((entry) => entry.row);
}

/**
 * What the validator said about its own coverage of the question, in its words.
 *
 * A refusal to correct for multiple testing is a result, not a footnote: the
 * page shows it next to the verdicts so a raw Sharpe is not read as a corrected
 * one.
 */
export function validationNotes(analysis: ValidationAnalysis | null | undefined): string[] {
  if (!analysis) return [];
  const notes: string[] = [];
  const push = (text: string) => {
    const trimmed = text.trim();
    if (trimmed && !notes.includes(trimmed)) notes.push(trimmed);
  };
  push(analysis.unavailable);
  if (analysis.multipleTesting?.applied === false) {
    push(analysis.multipleTesting.note || "验证器未应用多重检验校正，且未说明原因。");
  }
  for (const warning of analysis.warnings) push(warning);
  return notes;
}

/* ------------------------------------------------------------- normalising */

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.length > 0);
}

/** Counts only: an id the engine did not count stays out of the record. */
function coverageMap(value: unknown): Record<string, number> {
  const source = asRecord(value);
  if (!source) return {};
  const counts: Record<string, number> = {};
  for (const [key, raw] of Object.entries(source)) {
    const count = finiteNumber(raw);
    if (count !== null && count >= 0) counts[key] = count;
  }
  return counts;
}

function percentileMap(value: unknown): Record<string, number[]> {
  const source = asRecord(value);
  if (!source) return {};
  const series: Record<string, number[]> = {};
  for (const [key, raw] of Object.entries(source)) {
    if (Array.isArray(raw)) series[key] = raw.filter((item): item is number => typeof item === "number" && Number.isFinite(item));
  }
  return series;
}

function readDefinition(value: unknown): FactorDefinition | null {
  const item = asRecord(value);
  const id = text(item?.id);
  if (!item || !id) return null;
  return {
    id,
    name: text(item.name),
    family: text(item.family),
    mode: text(item.mode),
    sources: stringList(item.sources),
    requiredFields: stringList(item.requiredFields),
    warmupBars: finiteNumber(item.warmupBars) ?? 0,
    supportedTimeframes: stringList(item.supportedTimeframes),
    implementationVersion: text(item.implementationVersion),
    formulaHash: text(item.formulaHash),
    description: text(item.description),
    provider: text(item.provider),
    providerVersion: text(item.providerVersion),
  };
}

function readSeries(value: unknown): FactorSeries[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): FactorSeries[] => {
    const item = asRecord(entry);
    const factorId = text(item?.factorId);
    if (!item || !factorId) return [];
    const values = Array.isArray(item.values)
      ? item.values.flatMap((point): FactorSeriesValue[] => {
        const record = asRecord(point);
        const time = finiteNumber(record?.time);
        if (!record || time === null) return [];
        return [{ time, value: finiteNumber(record.value) }];
      })
      : [];
    return [{ factorId, values, implementationVersion: text(item.implementationVersion) }];
  });
}

function readRunSummary(value: unknown): FactorRunSummary | null {
  const run = asRecord(value);
  const id = finiteNumber(run?.id);
  if (!run || id === null) return null;
  return {
    id,
    provider: text(run.provider),
    symbol: text(run.symbol),
    interval: text(run.interval),
    snapshotHash: text(run.snapshotHash),
    factorIds: stringList(run.factorIds),
    status: text(run.status),
    bars: finiteNumber(run.bars) ?? 0,
    seriesCount: finiteNumber(run.seriesCount) ?? 0,
    coverage: coverageMap(run.coverage),
    warnings: stringList(run.warnings),
    error: typeof run.error === "string" && run.error ? run.error : null,
    durationMs: finiteNumber(run.durationMs),
    createdTs: finiteNumber(run.createdTs) ?? 0,
    // Rows written before the column existed carry nothing; "未知" is then the
    // honest label rather than an assumption of isolation.
    sandbox: text(run.sandbox),
  };
}

function readAnalysis(value: unknown): ValidationAnalysis | null {
  const block = asRecord(value);
  if (!block) return null;
  const multiple = asRecord(block.multipleTesting);
  return {
    provider: text(block.provider),
    runId: text(block.runId),
    algorithmVersion: text(block.algorithmVersion),
    seed: finiteNumber(block.seed),
    samples: finiteNumber(block.samples),
    multipleTesting: multiple
      ? {
        trials: finiteNumber(multiple.trials),
        factorCount: finiteNumber(multiple.factorCount),
        parameterCombinations: finiteNumber(multiple.parameterCombinations),
        deflatedSharpe: finiteNumber(multiple.deflatedSharpe),
        probabilityOfBacktestOverfitting: finiteNumber(multiple.probabilityOfBacktestOverfitting),
        method: text(multiple.method),
        applied: typeof multiple.applied === "boolean" ? multiple.applied : null,
        note: text(multiple.note),
      }
      : null,
    warnings: stringList(block.warnings),
    unavailable: text(block.unavailable),
    pathRisk: asRecord(block.pathRisk),
    bootstrap: asRecord(block.bootstrap),
    randomization: asRecord(block.randomization),
    tailLoss: asRecord(block.tailLoss),
    equityPercentiles: percentileMap(block.equityPercentiles),
  };
}

function readVerdicts(value: unknown): FactorValidationVerdict[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): FactorValidationVerdict[] => {
    const row = asRecord(entry);
    const kind = text(row?.kind);
    if (!row || !kind) return [];
    return [{
      kind,
      verdict: text(row.verdict),
      statistic: finiteNumber(row.statistic),
      pValue: finiteNumber(row.pValue),
      threshold: finiteNumber(row.threshold),
      provider: text(row.provider),
      detail: text(row.detail),
    }];
  });
}

function readValidationReport(payload: unknown): ValidationReport {
  const envelope = asRecord(payload);
  if (!envelope) throw new Error("统计验证返回结构异常");
  if (!Array.isArray(envelope.verdicts)) throw new Error("统计验证返回缺少 verdicts 字段");
  return {
    available: envelope.available === true,
    provider: typeof envelope.provider === "string" ? envelope.provider : null,
    runId: finiteNumber(envelope.runId),
    analysis: readAnalysis(envelope.analysis),
    verdicts: readVerdicts(envelope.verdicts),
  };
}

/* -------------------------------------------------------------------- HTTP */

const READ_TIMEOUT_MS = 30_000;
/** A factor set is computed in batches by the plugin; a large window is slow. */
const COMPUTE_TIMEOUT_MS = 300_000;
/** 400 bootstrap/randomization passes over a full curve take a while. */
const VALIDATE_TIMEOUT_MS = 300_000;

/**
 * The engine reads `detail` on every failure, and a 409 carries a JSON document
 * inside that string when a gate wrote it; unwrap it before showing the operator
 * a join of title / detail / action.
 */
function describeFailure(payload: unknown, status: number, label: string): string {
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
      const message = item ? text(item.msg) : "";
      return message ? [message] : [];
    });
    if (parts.length) return parts.join("；");
  }
  return `${label}返回 ${status}`;
}

async function requestJson(method: string, path: string, body: unknown, timeoutMs: number): Promise<{ status: number; payload: unknown }> {
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
  const raw = await response.text();
  let payload: unknown = null;
  try {
    payload = raw ? JSON.parse(raw) : null;
  } catch {
    payload = null;
  }
  return { status: response.status, payload };
}

async function send<T>(method: string, path: string, body: unknown, timeoutMs: number, label: string): Promise<T> {
  const { status, payload } = await requestJson(method, path, body, timeoutMs);
  if (status < 200 || status >= 300) throw new Error(describeFailure(payload, status, label));
  return payload as T;
}

/** Read-only calls follow the same surface: `detail`, then a status line. */
async function json<T>(path: string, label: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { signal: AbortSignal.timeout(READ_TIMEOUT_MS) });
  } catch (reason) {
    throw new Error(`无法连接本地引擎：${reason instanceof Error ? reason.message : String(reason)}`);
  }
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  if (!response.ok) throw new Error(typeof body?.detail === "string" ? body.detail : `${label}返回 ${response.status}`);
  if (!body) throw new Error(`${label}返回结构异常`);
  return body as T;
}

/* ------------------------------------------------------------------- calls */

/** The factor library. `refresh` asks the plugin and re-records what it says. */
export async function fetchFactorCatalog(refresh = true): Promise<FactorCatalog> {
  const body = await json<Record<string, unknown>>(`/api/factors?refresh=${refresh ? "true" : "false"}`, "因子目录");
  const provider = typeof body.provider === "string" && body.provider ? body.provider : null;
  const factors = Array.isArray(body.factors)
    ? body.factors.flatMap((entry) => {
      const definition = readDefinition(entry);
      return definition ? [definition] : [];
    })
    : [];
  return {
    provider,
    providerVersion: text(body.providerVersion),
    source: text(body.source) || (provider ? "plugin" : "stored"),
    // The engine's own flag: false means the catalogue below was read from the
    // store because no plugin is enabled. Warnings can accompany either state,
    // so they are reported as they arrived and never inferred from this flag.
    available: body.available === true,
    factors,
    warnings: stringList(body.warnings),
  };
}

/**
 * Compute a factor matrix over one contract's stored history.
 *
 * A window too wide to answer inline is transferred to the run queue, and the
 * outcome says so: the caller shows the run it can watch instead of reading the
 * coverage of a computation that has not happened yet.
 */
export async function computeFactors(request: FactorComputeRequest): Promise<StudyOutcome<FactorComputeResult>> {
  const body: Record<string, unknown> = {
    symbol: request.symbol.trim(),
    interval: request.interval,
    bars: Math.max(1, Math.floor(request.bars)),
  };
  if (request.factorIds?.length) body.factorIds = request.factorIds;
  if (request.parameters && Object.keys(request.parameters).length) body.parameters = request.parameters;
  const { status, payload } = await requestJson("POST", "/api/factors/compute", body, COMPUTE_TIMEOUT_MS);
  const queued = queuedTransferOf(status, payload);
  if (queued) return { queued: true, ...queued };
  if (status < 200 || status >= 300) throw new Error(describeFailure(payload, status, "因子计算"));
  const result = asRecord(payload);
  if (!result) throw new Error("因子计算返回结构异常");
  const runId = finiteNumber(result.runId);
  if (runId === null) throw new Error("因子计算返回缺少 runId，无法记录本次计算");
  return {
    queued: false,
    study: {
      available: result.available === true,
      provider: typeof result.provider === "string" ? result.provider : null,
      runId,
      symbol: text(result.symbol),
      interval: text(result.interval),
      snapshotHash: text(result.snapshotHash),
      batches: finiteNumber(result.batches) ?? 0,
      bars: finiteNumber(result.bars) ?? 0,
      series: readSeries(result.series),
      coverage: coverageMap(result.coverage),
      warnings: stringList(result.warnings),
      provenance: asRecord(result.provenance) ?? {},
    },
  };
}

/** Recent factor computations, newest first, without their values. */
export async function fetchFactorRuns(filter: FactorRunFilter = {}): Promise<FactorRunSummary[]> {
  const params = new URLSearchParams();
  const symbol = filter.symbol?.trim();
  if (symbol) params.set("symbol", symbol);
  if (filter.limit && filter.limit > 0) params.set("limit", String(Math.floor(filter.limit)));
  const query = params.toString();
  const body = await json<{ runs?: unknown }>(`/api/factors/runs${query ? `?${query}` : ""}`, "因子任务");
  const rows = Array.isArray(body.runs) ? body.runs : [];
  return rows.flatMap((entry) => {
    const run = readRunSummary(entry);
    return run ? [run] : [];
  });
}

/**
 * One factor computation.
 *
 * The values are opt-in: a wide run stores megabytes of them, and a caller that
 * only wants the coverage should not pay to transfer them. `valuesIncluded` says
 * which answer arrived, so an empty `series` is never read as "no values exist".
 */
export async function fetchFactorRun(id: number, options: { values?: boolean } = {}): Promise<FactorRunDetail> {
  const query = options.values ? "?values=true" : "";
  const body = await json<{ run?: unknown }>(`/api/factors/runs/${Math.floor(id)}${query}`, "因子任务");
  const run = readRunSummary(body.run);
  if (!run) throw new Error(`因子任务 #${id} 的响应缺少 run 字段`);
  const detail = asRecord(body.run);
  return {
    ...run,
    parameters: asRecord(detail?.parameters) ?? {},
    series: readSeries(detail?.series),
    valuesIncluded: detail?.valuesIncluded === true,
  };
}

/**
 * Ask the validator for a statistical reading of a finished run.
 *
 * A 409 here is the engine saying "no validator plugin, or nothing to read yet"
 * — the caller shows that sentence and leaves the result itself untouched.
 */
export async function validateRun(runId: number, options: ValidateRunOptions = {}): Promise<ValidationReport> {
  const body: Record<string, unknown> = {};
  const seed = finiteNumber(options.seed);
  if (seed !== null) body.seed = Math.max(0, Math.floor(seed));
  const payload = await send<unknown>("POST", `/api/factors/validate/${Math.floor(runId)}`, body, VALIDATE_TIMEOUT_MS, "统计验证");
  return readValidationReport(payload);
}

/**
 * The provider chip's text.
 *
 * `providerVersion` already carries the provider's name (`vibe-factors/0.1.0`),
 * so prefixing the id as well produced "vibe-factors vvibe-factors/0.1.0". The
 * version is shown as-is when it already names the provider, and joined when it
 * does not.
 */
export function providerLabel(catalog: { provider: string | null; providerVersion: string; available: boolean }): string {
  const provider = catalog.provider ?? "无提供者";
  const version = catalog.providerVersion.trim();
  const named = version && !version.toLowerCase().startsWith(provider.toLowerCase());
  const left = named ? `${provider} v${version}` : (version || provider);
  return `${left} · ${catalog.available ? "插件已启用" : "插件未启用"}`;
}
