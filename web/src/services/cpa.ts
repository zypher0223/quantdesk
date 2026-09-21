/**
 * CPA (Cycle of Price Action) — the phase reading, its parameters, and the rules a
 * page must not soften.
 *
 * The engine computes the phases; everything here is either a fetch or a pure
 * function over what came back. Three rules are enforced *here* rather than in a
 * component's markup, because markup is where they would quietly rot:
 *
 * * **a candidate is not a signal.** `cpaSignalOf` is the only thing that decides
 *   whether a phase record would open a position. An observation phase
 *   (`reversal_extension`, `exhaustion_extension`) can never be actionable, and a
 *   `status: "candidate"` record never is - the chart and the tables read this
 *   function, so neither can invent an entry the engine did not confirm.
 * * **insufficient data draws nothing.** `cpaOverlayPlan` returns `draw: false` with
 *   empty collections, so "no phases" cannot be re-rendered as "phase none".
 * * **the simplified position model is always on screen.** `simplePositionNotice`
 *   falls back to the engine's own wording when the catalogue has not loaded, so the
 *   caveat cannot disappear with a failed request.
 */

import { GatewayError } from "./api";
import type { StrategyParams } from "./backtest";

/* ------------------------------------------------------------------- types */

export type CpaPhaseId =
  | "none"
  | "reversal_extension"
  | "wedge_pop"
  | "ema_crossback"
  | "base_n_break"
  | "exhaustion_extension"
  | "wedge_drop"
  | "downside_ema_crossback"
  | "downside_base_n_break";

export type CpaStatus = "none" | "candidate" | "confirmed";
export type CpaDirection = "bullish" | "bearish" | "neutral";
export type CpaAssetClass = "stock" | "etf" | "crypto";

export interface CpaParameterSpec {
  key: string;
  label: string;
  type: "integer" | "number" | "boolean" | "string";
  default: unknown;
  minimum: number | null;
  maximum: number | null;
  unit: string;
  help: string;
  options?: string[];
}

export interface CpaPhaseInfo {
  id: CpaPhaseId;
  label: string;
  observation: boolean;
}

export interface CpaCatalog {
  id: string;
  name: string;
  parameterVersion: string;
  /** The engine's notice field; the name has changed once, so both are accepted. */
  simplePositionNotice?: string;
  intentPositionNotice?: string;
  positionModels?: string[];
  positionNoticeByModel?: Record<string, string>;
  attribution: string;
  observationPhases: string[];
  higherTimeframeMap: Record<string, string[]>;
  higherTimeframeNotice: string;
  phases: CpaPhaseInfo[];
  phaseLabels: Record<string, string>;
  parameters: CpaParameterSpec[];
  defaults: Record<string, Record<string, Record<string, unknown>>>;
  optionalFactorLayer: Record<string, string>;
  supportedIntervals: string[];
}

export interface CpaHigherView {
  interval: string;
  phase: string;
  trend: string;
  closedAt: number;
  available: boolean;
  reason: string;
}

export interface CpaPhaseRecord {
  time: number;
  phase: CpaPhaseId;
  status: CpaStatus;
  direction: CpaDirection;
  confidence: number;
  pivotPrice: number | null;
  invalidationPrice: number | null;
  setupLow: number | null;
  ema10: number | null;
  ema20: number | null;
  distanceAtr: number | null;
  volumeRatio: number | null;
  contractionScore: number | null;
  atr: number | null;
  cycle: string;
  higherTimeframe: CpaHigherView;
  backgroundTimeframe: CpaHigherView;
  reasons: string[];
  warnings: string[];
  checks: Record<string, boolean>;
  parameterVersion: string;
}

export interface CpaPhases {
  symbol: string;
  displaySymbol: string;
  interval: string;
  productType: string;
  snapshotHash: string;
  dataVersion: string;
  parameterVersion: string;
  parameters: Record<string, unknown>;
  higherIntervals: { management: string; background: string };
  bars: number;
  requestedBars?: number;
  storedBars?: number;
  insufficient: boolean;
  insufficientReason: string;
  counts: Record<string, number>;
  current: CpaPhaseRecord | null;
  records: CpaPhaseRecord[];
  warnings: string[];
  attribution: string;
}

export interface CpaSummary {
  symbol: string;
  displaySymbol: string;
  interval: string;
  current: CpaPhaseRecord | null;
  counts: Record<string, number>;
  insufficient: boolean;
  insufficientReason: string;
  parameterVersion: string;
  snapshotHash: string;
  higherIntervals: { management: string; background: string };
  warnings: string[];
  attribution: string;
}

/* ---------------------------------------------------------------- fetching */

async function getJson<T>(path: string, timeoutMs = 30_000): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { signal: AbortSignal.timeout(timeoutMs) });
  } catch (reason) {
    throw new GatewayError(`无法连接本地引擎：${reason instanceof Error ? reason.message : reason}`, 0);
  }
  if (!response.ok) {
    let message = `引擎返回 ${response.status}`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") message = body.detail;
    } catch {
      /* a non-JSON error body keeps the status line */
    }
    throw new GatewayError(message, response.status);
  }
  return (await response.json()) as T;
}

export async function fetchCpaCatalog(): Promise<CpaCatalog> {
  return await getJson<CpaCatalog>("/api/cpa/catalog");
}

export async function fetchCpaPhases(
  symbol: string,
  interval: string,
  options: { bars?: number; limit?: number; withBackground?: boolean } = {},
): Promise<CpaPhases> {
  const bars = options.bars ?? 600;
  const limit = options.limit ?? 400;
  const withBackground = options.withBackground ?? true;
  const query = new URLSearchParams({
    symbol,
    interval,
    bars: String(bars),
    limit: String(limit),
    withBackground: String(withBackground),
  });
  return await getJson<CpaPhases>(`/api/cpa/phases?${query.toString()}`);
}

export async function fetchCpaSummary(
  symbol: string,
  interval: string,
  bars = 600,
): Promise<CpaSummary> {
  const query = new URLSearchParams({ symbol, interval, bars: String(bars) });
  return await getJson<CpaSummary>(`/api/cpa/summary?${query.toString()}`);
}

/* ------------------------------------------------------------------ labels */

/**
 * The engine's own Chinese labels, kept as a fallback.
 *
 * A phase id is data and must never be rendered raw; when the catalogue request has
 * not answered yet the labels still have to read as Chinese, so the map is duplicated
 * here deliberately (`catalog.phaseLabels` wins whenever it is available).
 */
export const FALLBACK_PHASE_LABELS: Record<string, string> = {
  none: "无阶段",
  reversal_extension: "反转延伸（Reversal Extension）",
  wedge_pop: "楔形突破（Wedge Pop）",
  ema_crossback: "均线回踩（EMA Crossback）",
  base_n_break: "平台突破（Base n’ Break）",
  exhaustion_extension: "延伸衰竭（Exhaustion Extension）",
  wedge_drop: "楔形下跌（Wedge Drop）",
  downside_ema_crossback: "下行均线回踩（Downside EMA Crossback）",
  downside_base_n_break: "下行平台突破（Downside Base n’ Break）",
};

/** Short labels for a chart band or a legend, where the full name does not fit. */
export const SHORT_PHASE_LABELS: Record<string, string> = {
  none: "无阶段",
  reversal_extension: "反转延伸",
  wedge_pop: "楔形突破",
  ema_crossback: "均线回踩",
  base_n_break: "平台突破",
  exhaustion_extension: "延伸衰竭",
  wedge_drop: "楔形下跌",
  downside_ema_crossback: "下行回踩",
  downside_base_n_break: "下行突破",
};

export function phaseLabel(phase: string, catalog?: CpaCatalog | null, short = false): string {
  if (short && SHORT_PHASE_LABELS[phase]) return SHORT_PHASE_LABELS[phase];
  const fromCatalog = catalog?.phaseLabels?.[phase];
  return fromCatalog || FALLBACK_PHASE_LABELS[phase] || phase;
}

export function isObservationPhase(phase: string, catalog?: CpaCatalog | null): boolean {
  const fromCatalog = catalog?.phases?.find((item) => item.id === phase);
  if (fromCatalog) return fromCatalog.observation;
  return phase === "reversal_extension" || phase === "exhaustion_extension";
}

/** Which side a phase belongs to, read from the record when it is there. */
export function phaseDirection(record: Pick<CpaPhaseRecord, "direction" | "phase">): CpaDirection {
  if (record.direction === "bullish" || record.direction === "bearish") return record.direction;
  if (record.phase.startsWith("downside_") || record.phase === "wedge_drop") return "bearish";
  return "neutral";
}

export type CpaSignalKind = "none" | "observation" | "candidate" | "confirmed";

/**
 * What one phase record *is*, before anyone decides how to draw it.
 *
 * `observation` is reported as its own kind on purpose: the two observation phases
 * can be confirmed by the state machine and still never open a position, so mapping
 * them to "confirmed" and then subtracting them somewhere else is how a page ends up
 * drawing them as entries.
 */
export function cpaSignalKind(
  record: Pick<CpaPhaseRecord, "phase" | "status">,
  catalog?: CpaCatalog | null,
): CpaSignalKind {
  if (record.status === "none" || record.phase === "none") return "none";
  if (isObservationPhase(record.phase, catalog)) return "observation";
  if (record.status === "candidate") return "candidate";
  return "confirmed";
}

export interface CpaSignal {
  /** True only for a confirmed, non-observation phase: what would open a position. */
  actionable: boolean;
  kind: CpaSignalKind;
  side: "long" | "short" | null;
  /** Always safe to render: says *why* it is not actionable when it is not. */
  label: string;
}

/**
 * The one place that decides whether a record is a trade signal.
 *
 * The engine's backtest only opens on confirmed phases from `entryStages`, and the
 * chart must agree with the backtest rather than with a reader's optimism, so a
 * candidate gets `actionable: false` and a label that says so.
 */
export function cpaSignalOf(
  record: Pick<CpaPhaseRecord, "phase" | "status" | "direction">,
  catalog?: CpaCatalog | null,
): CpaSignal {
  const kind = cpaSignalKind(record, catalog);
  const label = phaseLabel(record.phase, catalog, true);
  if (kind === "confirmed") {
    const direction = phaseDirection(record);
    return {
      actionable: true,
      kind,
      side: direction === "bearish" ? "short" : "long",
      label: `${label}（已确认）`,
    };
  }
  if (kind === "candidate") return { actionable: false, kind, side: null, label: `${label}（候选，未确认）` };
  if (kind === "observation") return { actionable: false, kind, side: null, label: `${label}（观察，不产生交易）` };
  return { actionable: false, kind, side: null, label: "无阶段" };
}

export function statusLabel(status: CpaStatus): string {
  if (status === "confirmed") return "已确认";
  if (status === "candidate") return "候选";
  return "无";
}

/* ------------------------------------------------------- the overlay plan */

export interface CpaBand {
  phase: CpaPhaseId;
  status: CpaStatus;
  from: number;
  to: number;
  label: string;
  tone: CpaDirection;
}

export interface CpaLevel {
  kind: "pivot" | "invalidation";
  price: number;
  from: number;
  to: number;
  label: string;
}

export interface CpaMarker {
  time: number;
  phase: CpaPhaseId;
  kind: CpaSignalKind;
  side: "long" | "short" | null;
  label: string;
  price: number;
}

export interface CpaLine {
  key: "ema10" | "ema20" | "sma50" | "sma200";
  label: string;
  points: Array<{ time: number; value: number }>;
}

export interface CpaOverlayPlan {
  /** The single switch every renderer reads. False means: draw nothing at all. */
  draw: boolean;
  reason: string;
  enabled: boolean;
  insufficient: boolean;
  insufficientReason: string;
  interval: string;
  bands: CpaBand[];
  levels: CpaLevel[];
  markers: CpaMarker[];
  lines: CpaLine[];
  /** Markers for the phase *changes* a reader can click for the evidence card. */
  anchors: CpaMarker[];
  latest: CpaPhaseRecord | null;
  parameterVersion: string;
  dataVersion: string;
}

/**
 * Whether the chart should ask the engine for phases at all.
 *
 * Off means *no request*, not "a request whose result is hidden": an overlay that
 * polls the engine while switched off would be a cost with no reader, and it is the
 * kind of thing that later turns into a badge that "helpfully" appears.
 */
export function shouldFetchCpa(args: {
  cpaEnabled: boolean;
  cpaOn: boolean;
  symbol: string;
}): boolean {
  return Boolean(args.cpaEnabled && args.cpaOn && args.symbol.trim().length > 0);
}

/**
 * Turn a phase series into what an overlay may draw.
 *
 * The plan is returned even when nothing is drawn, so a caller can show *why*
 * (`reason`) instead of an empty chart with no explanation.
 */
export function cpaOverlayPlan(args: {
  enabled: boolean;
  phases: CpaPhases | null;
  interval: string;
  catalog?: CpaCatalog | null;
  /** Daily charts may add SMA50/SMA200; they are computed from the candles shown. */
  showLongSma?: boolean;
  /**
   * The bars on screen. A marker is placed on its own bar (just beyond that bar's
   * high or low); without them the plan falls back to the phase's own level, which can
   * sit outside the visible price scale.
   */
  candles?: Array<{ time: number; close: number; high?: number; low?: number }>;
}): CpaOverlayPlan {
  const { enabled, phases, interval, catalog, showLongSma, candles } = args;
  const empty: CpaOverlayPlan = {
    draw: false,
    reason: enabled ? "还没有阶段数据" : "CPA 周期开关未打开",
    enabled,
    insufficient: false,
    insufficientReason: "",
    interval,
    bands: [],
    levels: [],
    markers: [],
    lines: [],
    anchors: [],
    latest: null,
    parameterVersion: "",
    dataVersion: "",
  };
  if (!enabled || !phases) return empty;
  const base: CpaOverlayPlan = {
    ...empty,
    reason: "",
    insufficient: Boolean(phases.insufficient),
    insufficientReason: phases.insufficientReason ?? "",
    parameterVersion: phases.parameterVersion ?? "",
    dataVersion: phases.dataVersion ?? "",
    latest: phases.current ?? null,
  };
  if (phases.insufficient) {
    return {
      ...base,
      draw: false,
      reason: phases.insufficientReason
        ? `样本不足：${phases.insufficientReason}`
        : "样本不足，未输出阶段",
    };
  }
  const records = (phases.records ?? []).filter((record) => record && record.status !== "none");
  if (!records.length) {
    return { ...base, draw: false, reason: "该区间没有识别出任何阶段" };
  }

  const bands: CpaBand[] = [];
  for (const record of records) {
    const key = `${record.phase}:${record.status}`;
    const previous = bands.at(-1);
    if (previous && `${previous.phase}:${previous.status}` === key) {
      previous.to = record.time;
      continue;
    }
    bands.push({
      phase: record.phase,
      status: record.status,
      from: record.time,
      to: record.time,
      label: `${phaseLabel(record.phase, catalog, true)}·${statusLabel(record.status)}`,
      tone: phaseDirection(record),
    });
  }

  const levels: CpaLevel[] = [];
  for (const record of records) {
    if (record.status !== "confirmed") continue;
    if (record.pivotPrice !== null) {
      levels.push({
        kind: "pivot",
        price: record.pivotPrice,
        from: record.time,
        to: records.at(-1)?.time ?? record.time,
        label: "枢轴",
      });
    }
    if (record.invalidationPrice !== null) {
      levels.push({
        kind: "invalidation",
        price: record.invalidationPrice,
        from: record.time,
        to: records.at(-1)?.time ?? record.time,
        label: "结构失效",
      });
    }
  }

  const barByTime = new Map<number, { high?: number; low?: number }>();
  for (const candle of candles ?? []) {
    barByTime.set(candle.time, { high: candle.high, low: candle.low });
  }

  const markers: CpaMarker[] = [];
  const anchors: CpaMarker[] = [];
  let previousKind = "";
  for (const record of records) {
    const signal = cpaSignalOf(record, catalog);
    // On its own bar when the chart has that bar - a pivot level from twenty bars ago
    // can easily sit off the visible price scale, and a marker outside the plot is
    // either invisible or drawn over the page.
    const bar = barByTime.get(record.time);
    const onBar = signal.side === "short" ? bar?.high : bar?.low;
    const price = onBar ?? bar?.high ?? bar?.low ?? record.pivotPrice ?? record.ema20 ?? record.ema10 ?? null;
    if (signal.kind === "confirmed" || signal.kind === "candidate" || signal.kind === "observation") {
      const kind = signal.kind === "confirmed"
        ? (signal.side === "short" ? "short" : "long")
        : signal.kind === "observation" ? "observation" : "candidate";
      markers.push({
        time: record.time,
        phase: record.phase,
        kind: signal.kind,
        side: signal.side,
        label: signal.label,
        price: price ?? 0,
      });
      const dedupe = `${record.phase}:${kind}`;
      if (dedupe !== previousKind) {
        anchors.push({
          time: record.time,
          phase: record.phase,
          kind: signal.kind,
          side: signal.side,
          label: signal.label,
          price: price ?? 0,
        });
        previousKind = dedupe;
      }
    }
  }

  const line = (key: CpaLine["key"], label: string, pick: (record: CpaPhaseRecord) => number | null): CpaLine | null => {
    const points = records
      .map((record) => ({ time: record.time, value: pick(record) }))
      .filter((point): point is { time: number; value: number } =>
        typeof point.value === "number" && Number.isFinite(point.value));
    return points.length > 1 ? { key, label, points } : null;
  };
  const lines = [
    line("ema10", "EMA10", (record) => record.ema10),
    line("ema20", "EMA20", (record) => record.ema20),
  ].filter((item): item is CpaLine => item !== null);
  if (showLongSma && candles?.length) {
    // The engine's phase records carry the 10/20 EMA only, so the long backdrop is
    // computed here from the same closed candles the chart is drawing. It is a
    // moving average of what is on screen, not a second opinion about the phase.
    for (const period of [50, 200] as const) {
      const points = simpleMovingAverage(candles, period);
      if (points.length > 1) {
        lines.push({ key: period === 50 ? "sma50" : "sma200", label: `SMA${period}`, points });
      }
    }
  }

  return {
    ...base,
    draw: true,
    reason: "",
    bands,
    levels,
    markers,
    anchors,
    lines,
  };
}

/** A plain close-based moving average, aligned to the bars that produced it. */
export function simpleMovingAverage(
  candles: Array<{ time: number; close: number }>,
  period: number,
): Array<{ time: number; value: number }> {
  if (!Number.isFinite(period) || period < 2) return [];
  const out: Array<{ time: number; value: number }> = [];
  let sum = 0;
  for (let index = 0; index < candles.length; index += 1) {
    const close = Number(candles[index]?.close);
    if (!Number.isFinite(close)) return out;
    sum += close;
    if (index >= period) sum -= Number(candles[index - period].close);
    if (index >= period - 1) out.push({ time: candles[index].time, value: sum / period });
  }
  return out;
}

/* ---------------------------------------------------------------- geometry */

export interface CpaProjector {
  /** Bar time -> x coordinate, or null when the bar is outside the visible range. */
  x: (time: number) => number | null;
  /** Price -> y coordinate, or null when it cannot be projected. */
  y: (price: number) => number | null;
  width: number;
  height: number;
  /**
   * The bars the chart actually holds, in the same units as `x`.
   *
   * A chart of 300 candles asked for a 600-bar phase series: `timeToCoordinate`
   * extrapolates for times older than the series instead of returning null, which put
   * bands in a heap of two-pixel slivers at the left edge and markers at x = -2000.
   * Anything outside this range is dropped rather than drawn at a guessed position.
   */
  timeRange?: { from: number; to: number };
}

export interface CpaGeometry {
  bands: Array<{ x1: number; x2: number; label: string; tone: CpaDirection; status: CpaStatus }>;
  levels: Array<{ x1: number; x2: number; y: number; label: string; kind: CpaLevel["kind"] }>;
  markers: Array<{ x: number; y: number; marker: CpaMarker }>;
  anchors: Array<{ x: number; y: number; marker: CpaMarker }>;
  lines: Array<{ key: CpaLine["key"]; label: string; path: string }>;
}

/**
 * Project the plan onto pixels. Pure: the projector is injected, so this is tested
 * with a stub instead of a chart.
 */
export function buildCpaGeometry(plan: CpaOverlayPlan, project: CpaProjector): CpaGeometry {
  if (!plan.draw) return { bands: [], levels: [], markers: [], anchors: [], lines: [] };
  const inRange = (time: number) =>
    !project.timeRange || (time >= project.timeRange.from && time <= project.timeRange.to);
  /** x for a time the chart holds; null when it is absent or off the plot. */
  const projectX = (time: number): number | null => {
    if (!inRange(time)) return null;
    const value = project.x(time);
    if (value === null || !Number.isFinite(value)) return null;
    return value < -0.5 || value > project.width + 0.5 ? null : value;
  };
  /**
   * y for a price on the visible price scale; null when it is off it.
   *
   * Live bug: only `x` was range-checked, so a pivot price outside the current price
   * range projected to cy = -214 and the marker was drawn over the page header.
   */
  const projectY = (price: number): number | null => {
    const value = project.y(price);
    if (value === null || !Number.isFinite(value)) return null;
    return value < -0.5 || value > project.height + 0.5 ? null : value;
  };

  const bands = plan.bands
    .map((band) => {
      // Only the ends that exist are projected: a band that starts before the chart
      // begins is clamped to the left edge, and one entirely outside is dropped.
      const startsInside = inRange(band.from);
      const endsInside = inRange(band.to);
      if (!startsInside && !endsInside) {
        if (band.from < (project.timeRange?.from ?? 0) && band.to < (project.timeRange?.from ?? 0)) {
          return null;
        }
      }
      const x1 = startsInside ? projectX(band.from) : band.from < (project.timeRange?.from ?? 0) ? 0 : projectX(band.to);
      const x2 = endsInside ? projectX(band.to) : band.to > (project.timeRange?.to ?? 0) ? project.width : projectX(band.from);
      if (x1 === null && x2 === null) return null;
      const left = x1 ?? 0;
      const right = x2 ?? project.width;
      if (right < 0 || left > project.width) return null;
      return {
        x1: Math.max(0, Math.min(left, right)),
        x2: Math.min(project.width, Math.max(left, right) + 6),
        label: band.label,
        tone: band.tone,
        status: band.status,
      };
    })
    .filter((item): item is CpaGeometry["bands"][number] => item !== null);

  const levels = plan.levels
    .map((level) => {
      const y = projectY(level.price);
      if (y === null) return null;
      const startsInside = inRange(level.from);
      const endsInside = inRange(level.to);
      if (!startsInside && !endsInside) return null;
      const from = startsInside ? projectX(level.from) : 0;
      const to = endsInside ? projectX(level.to) : project.width;
      if (from === null && to === null) return null;
      return {
        x1: Math.max(0, from ?? 0),
        x2: Math.min(project.width, to ?? project.width),
        y,
        label: `${level.label} ${level.price.toLocaleString("en-US", { maximumFractionDigits: 4 })}`,
        kind: level.kind,
      };
    })
    .filter((item): item is CpaGeometry["levels"][number] => item !== null);

  const project_marker = (marker: CpaMarker) => {
    // Neither off-chart bars nor off-scale prices are drawn at a guessed position.
    const x = projectX(marker.time);
    const y = projectY(marker.price);
    if (x === null || y === null) return null;
    return { x, y, marker };
  };
  const markers = plan.markers.map(project_marker).filter((item): item is { x: number; y: number; marker: CpaMarker } => item !== null);
  const anchors = plan.anchors.map(project_marker).filter((item): item is { x: number; y: number; marker: CpaMarker } => item !== null);
  const lines = plan.lines
    .map((line) => {
      const segments: string[] = [];
      for (const point of line.points) {
        const x = projectX(point.time);
        const y = projectY(point.value);
        if (x === null || y === null) continue;
        segments.push(`${segments.length === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`);
      }
      return segments.length > 1 ? { key: line.key, label: line.label, path: segments.join(" ") } : null;
    })
    .filter((item): item is { key: CpaLine["key"]; label: string; path: string } => item !== null);

  return { bands, levels, markers, anchors, lines };
}

/** The marker a click landed on, if any - what opens the evidence card. */
export function cpaMarkerAt(
  anchors: CpaGeometry["anchors"],
  x: number,
  y: number,
  radius = 12,
): CpaMarker | null {
  let best: { distance: number; marker: CpaMarker } | null = null;
  for (const anchor of anchors) {
    const distance = Math.hypot(anchor.x - x, anchor.y - y);
    if (distance <= radius && (!best || distance < best.distance)) {
      best = { distance, marker: anchor.marker };
    }
  }
  return best?.marker ?? null;
}

/* -------------------------------------------------------------- parameters */

export const CPA_PARAMETER_GROUPS: Array<{ title: string; keys: string[] }> = [
  { title: "均线与背景", keys: ["emaFast", "emaSlow", "longSma", "backdropSma"] },
  {
    title: "延伸与收缩",
    keys: ["extensionAtr", "exhaustionAtr", "contractionWindow", "contractionThreshold"],
  },
  { title: "结构与量能", keys: ["pivotLookback", "volumeWindow", "volumeConfirm", "downsideVolumeConfirm", "crossbackToleranceAtr"] },
  { title: "行为与入场", keys: ["sideMode", "entryStages", "exitOnExhaustion", "requireHigherTimeframe", "exhaustionTrailAtr", "minBars"] },
];

export function assetClassOf(productType: string | null | undefined): CpaAssetClass {
  if (productType === "etf" || productType === "crypto") return productType;
  return "stock";
}

/**
 * The defaults for one contract class and interval, straight from the catalogue.
 *
 * The engine publishes a table per asset class and interval, so a leveraged ETF gets
 * its own expansion thresholds instead of whatever the form last held.
 */
/**
 * A value the engine's request model will accept.
 *
 * Anything else (a nested object, `null`) is dropped rather than forwarded: a study
 * body that cannot be serialised into the request would fail far from here, and the
 * catalogue is not a trusted shape - it comes over HTTP.
 */
function toStrategyValue(value: unknown): string | number | boolean | string[] | undefined {
  if (typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
    return value as string[];
  }
  return undefined;
}

export function parameterDefaults(
  catalog: CpaCatalog | null,
  productType: string | null | undefined,
  interval: string,
): StrategyParams {
  const assetClass = assetClassOf(productType);
  const byInterval = catalog?.defaults?.[assetClass] ?? {};
  const source = byInterval[interval] ?? byInterval["1h"] ?? {};
  const out: StrategyParams = {};
  for (const [key, raw] of Object.entries(source)) {
    const value = toStrategyValue(raw);
    if (value !== undefined) out[key] = value;
  }
  for (const spec of catalog?.parameters ?? []) {
    if (spec.key in out) continue;
    const value = toStrategyValue(spec.default);
    if (value !== undefined) out[spec.key] = value;
  }
  return out;
}

export interface CpaParameterRow {
  spec: CpaParameterSpec;
  value: unknown;
  /** Where the value came from, so a preset is never mistaken for an edit. */
  origin: "defaults" | "spec";
}

export function parameterRows(
  catalog: CpaCatalog | null,
  productType: string | null | undefined,
  interval: string,
): CpaParameterRow[] {
  const defaults = parameterDefaults(catalog, productType, interval);
  return (catalog?.parameters ?? []).map((spec) => ({
    spec,
    value: spec.key in defaults ? defaults[spec.key] : spec.default,
    origin: spec.key in defaults ? "defaults" : "spec",
  }));
}

export function parameterControls(catalog: CpaCatalog | null): {
  positionModel?: CpaParameterSpec;
  sideMode?: CpaParameterSpec;
  entryStages?: CpaParameterSpec;
  exitOnExhaustion?: CpaParameterSpec;
  requireHigherTimeframe?: CpaParameterSpec;
} {
  const find = (key: string) => catalog?.parameters?.find((spec) => spec.key === key);
  return {
    positionModel: find("positionModel"),
    sideMode: find("sideMode"),
    entryStages: find("entryStages"),
    exitOnExhaustion: find("exitOnExhaustion"),
    requireHigherTimeframe: find("requireHigherTimeframe"),
  };
}

export interface WarmupState {
  ok: boolean;
  required: number;
  available: number;
  reason: string;
}

/**
 * Whether the bars in hand satisfy the CPA warmup, judged by the engine's `minBars`.
 *
 * Reported before a run rather than discovered in a failed one: a CPA backtest on
 * sixty bars of a 15m chart is not a smaller version of the result, it is a
 * different question.
 */
export function warmupState(
  catalog: CpaCatalog | null,
  interval: string,
  availableBars: number,
  productType?: string | null,
): WarmupState {
  const defaults = parameterDefaults(catalog, productType ?? "stock", interval);
  const raw = defaults["minBars"];
  const required = typeof raw === "number" && Number.isFinite(raw) ? raw : 60;
  if (!Number.isFinite(availableBars)) {
    return { ok: false, required, available: 0, reason: "无法确认K线数量" };
  }
  if (availableBars >= required + 3) return { ok: true, required, available: availableBars, reason: "" };
  return {
    ok: false,
    required,
    available: availableBars,
    reason: `CPA 需要至少 ${required} 根K线（外加 3 根用于确认），当前 ${availableBars} 根`,
  };
}

/**
 * The caveat that must always be visible for a CPA run.
 *
 * It falls back to the engine's own sentence when the catalogue request failed: a
 * missing network call must not remove a disclosure.
 */
export const FALLBACK_SIMPLE_POSITION_NOTICE =
  "当前回测尚未模拟 CPA 分批建仓与分批减仓，收益结果属于单仓位简化版本。";

export function positionNotice(catalog: CpaCatalog | null, model: string): string {
  const fromMap = catalog?.positionNoticeByModel?.[model]?.trim();
  if (fromMap) return fromMap;
  if (model === "intent") {
    const intent = catalog?.intentPositionNotice?.trim();
    if (intent) return intent;
  }
  const simple = catalog?.simplePositionNotice?.trim();
  const legacy = catalog?.positionNoticeByModel ? "" : catalog?.intentPositionNotice?.trim();
  return simple || legacy || FALLBACK_SIMPLE_POSITION_NOTICE;
}

export function simplePositionNotice(catalog: CpaCatalog | null): string {
  // `intentPositionNotice` is the newer spelling: the engine's CPA module renamed the
  // constant while this page was being written, and a rename must not silently drop a
  // disclosure back to older wording.
  return positionNotice(catalog, "single");
}

export function attributionText(catalog: CpaCatalog | null): string {
  const text = catalog?.attribution?.trim();
  return text && text.length > 0
    ? text
    : "概念来源：Oliver Kell 公开描述的 Cycle of Price Action；阈值为 QuantDesk 研究参数。";
}

/* ------------------------------------------------------- result rendering */

export interface PhaseDistributionRow {
  phase: string;
  label: string;
  confirmed: number;
  candidate: number;
  observation: number;
  total: number;
}

export function phaseDistribution(phases: CpaPhases | null | undefined): PhaseDistributionRow[] {
  const records = phases?.records ?? [];
  const rows = new Map<string, PhaseDistributionRow>();
  for (const record of records) {
    if (record.status === "none" || record.phase === "none") continue;
    const row = rows.get(record.phase) ?? {
      phase: record.phase,
      label: phaseLabel(record.phase),
      confirmed: 0,
      candidate: 0,
      observation: 0,
      total: 0,
    };
    const observation = isObservationPhase(record.phase);
    if (observation) row.observation += 1;
    else if (record.status === "confirmed") row.confirmed += 1;
    else row.candidate += 1;
    row.total += 1;
    rows.set(record.phase, row);
  }
  return [...rows.values()].sort((left, right) => right.total - left.total || left.phase.localeCompare(right.phase));
}

export interface TradeLike {
  entry_time: number;
  exit_time?: number;
  net_pnl: number;
  return_pct?: number;
  liquidated?: boolean;
}

export interface PhaseTradeRow {
  phase: string;
  label: string;
  trades: number;
  netPnl: number;
  avgReturnPct: number | null;
  worstReturnPct: number | null;
  liquidations: number;
}

/**
 * Group trades by the phase that was in force when they opened.
 *
 * The engine's trades carry no phase tag, so the join is by entry time against the
 * attached phase series: the last record at or before the entry. A trade whose entry
 * predates the series is reported as `unmatched` instead of being dropped, because a
 * silently missing trade would make the table disagree with the headline count.
 */
export function tradesByEntryPhase(
  trades: TradeLike[] | null | undefined,
  records: CpaPhaseRecord[] | null | undefined,
): PhaseTradeRow[] {
  const series = [...(records ?? [])].sort((left, right) => left.time - right.time);
  const rows = new Map<string, PhaseTradeRow>();
  for (const trade of trades ?? []) {
    let phase = "unmatched";
    for (const record of series) {
      if (record.time <= trade.entry_time && record.status !== "none" && record.phase !== "none") {
        phase = record.phase;
      } else if (record.time > trade.entry_time) {
        break;
      }
    }
    const row = rows.get(phase) ?? {
      phase,
      label: phase === "unmatched" ? "未匹配到阶段" : phaseLabel(phase),
      trades: 0,
      netPnl: 0,
      avgReturnPct: null,
      worstReturnPct: null,
      liquidations: 0,
    };
    row.trades += 1;
    row.netPnl += Number(trade.net_pnl ?? 0);
    if (typeof trade.return_pct === "number" && Number.isFinite(trade.return_pct)) {
      const sum = (row.avgReturnPct ?? 0) * (row.trades - 1) + trade.return_pct;
      row.avgReturnPct = sum / row.trades;
      row.worstReturnPct = row.worstReturnPct === null
        ? trade.return_pct
        : Math.min(row.worstReturnPct, trade.return_pct);
    }
    if (trade.liquidated) row.liquidations += 1;
    rows.set(phase, row);
  }
  return [...rows.values()].sort((left, right) => right.trades - left.trades);
}

export interface HigherTimeframeComparison {
  available: boolean;
  reason: string;
  rows: Array<{ label: string; withFilter: number | null; withoutFilter: number | null }>;
}

/**
 * The filter comparison, only when the payload actually carries one.
 *
 * The engine does not currently publish a with/without comparison, so this returns
 * `available: false` with a reason. Inventing a "before" line by re-running the
 * strategy in the browser would be a fabricated measurement, which is worse than an
 * absent one.
 */
export function higherTimeframeComparison(result: Record<string, unknown> | null | undefined): HigherTimeframeComparison {
  const payload = (result ?? {}) as Record<string, unknown>;
  const meta = (payload.cpaMeta ?? {}) as Record<string, unknown>;
  const candidates = [meta.filterComparison, meta.higherTimeframeComparison, payload.cpaFilterComparison];
  const found = candidates.find((item) => item && typeof item === "object") as Record<string, unknown> | undefined;
  if (!found) {
    return {
      available: false,
      reason: "引擎未提供高周期过滤前后对比（仅在有该字段时展示，不会在本地重算）",
      rows: [],
    };
  }
  const number = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : null);
  const withFilter = (found.withFilter ?? {}) as Record<string, unknown>;
  const withoutFilter = (found.withoutFilter ?? {}) as Record<string, unknown>;
  return {
    available: true,
    reason: "",
    rows: [
      { label: "净收益 %", withFilter: number(withFilter.netReturnPct), withoutFilter: number(withoutFilter.netReturnPct) },
      { label: "最大回撤 %", withFilter: number(withFilter.maxDrawdownPct), withoutFilter: number(withoutFilter.maxDrawdownPct) },
      { label: "交易次数", withFilter: number(withFilter.trades), withoutFilter: number(withoutFilter.trades) },
      { label: "Sharpe", withFilter: number(withFilter.sharpe), withoutFilter: number(withoutFilter.sharpe) },
    ],
  };
}

export interface CpaResultView {
  phases: CpaPhases;
  distribution: PhaseDistributionRow[];
  tradeRows: PhaseTradeRow[];
  comparison: HigherTimeframeComparison;
  parameterVersion: string;
  dataVersion: string;
  higherIntervals: string[];
  warnings: string[];
  notice: string;
  attachment: string | null;
  parameters: Record<string, unknown>;
}

export const CPA_ATTACHMENT_NAME = "cpa-phases";

export function cpaAttachment(artifacts: string[] | null | undefined): string | null {
  const names = artifacts ?? [];
  return names.find((name) => name === CPA_ATTACHMENT_NAME)
    ?? names.find((name) => name.replace(/\.[a-z]+$/i, "") === CPA_ATTACHMENT_NAME)
    ?? null;
}

/**
 * Everything a result page shows for a CPA run, or `null` when the run is not one.
 *
 * It is a single function so the panel cannot show half a CPA block (a distribution
 * without the caveat, say) by taking one field and forgetting another.
 */
export function cpaResultView(result: Record<string, unknown> | null | undefined): CpaResultView | null {
  const payload = (result ?? {}) as Record<string, unknown>;
  const phases = payload.cpaPhases as CpaPhases | undefined;
  const meta = (payload.cpaMeta ?? {}) as Record<string, unknown>;
  if (!phases || typeof phases !== "object") return null;
  const trades = Array.isArray(payload.trades) ? (payload.trades as TradeLike[]) : [];
  const higher = (meta.higherIntervals ?? phases.higherIntervals) as unknown;
  const higherIntervals = Array.isArray(higher)
    ? higher.filter((item): item is string => typeof item === "string" && item.length > 0)
    : typeof higher === "object" && higher !== null
      ? Object.values(higher as Record<string, unknown>).filter((item): item is string => typeof item === "string" && item.length > 0)
      : [];
  const metaWarnings = Array.isArray(meta.warnings) ? (meta.warnings as string[]) : [];
  return {
    phases,
    distribution: phaseDistribution(phases),
    tradeRows: tradesByEntryPhase(trades, phases.records),
    comparison: higherTimeframeComparison(payload),
    parameterVersion: String(meta.parameterVersion ?? phases.parameterVersion ?? ""),
    dataVersion: String(meta.dataVersion ?? phases.dataVersion ?? ""),
    higherIntervals,
    warnings: [...new Set([...metaWarnings, ...(phases.warnings ?? [])])],
    notice: typeof meta.simplePositionNotice === "string" && meta.simplePositionNotice.trim()
      ? meta.simplePositionNotice
      : FALLBACK_SIMPLE_POSITION_NOTICE,
    attachment: cpaAttachment(Array.isArray(payload.artifacts) ? (payload.artifacts as string[]) : []),
    parameters: (phases.parameters ?? {}) as Record<string, unknown>,
  };
}

/** The evidence card for one clicked marker. */
export interface CpaEvidenceCard {
  title: string;
  time: number;
  phase: string;
  status: CpaStatus;
  direction: CpaDirection;
  signal: CpaSignal;
  confidence: number;
  higherTimeframe: CpaHigherView;
  backgroundTimeframe: CpaHigherView;
  volumeRatio: number | null;
  contractionScore: number | null;
  distanceAtr: number | null;
  pivotPrice: number | null;
  invalidationPrice: number | null;
  parameterVersion: string;
  timeframes: { management: string; background: string };
  reasons: string[];
  warnings: string[];
}

export function cpaEvidenceCard(
  record: CpaPhaseRecord,
  options: { catalog?: CpaCatalog | null; dataVersion?: string; higherIntervals?: { management: string; background: string } } = {},
): CpaEvidenceCard {
  const signal = cpaSignalOf(record, options.catalog);
  return {
    title: signal.label,
    time: record.time,
    phase: record.phase,
    status: record.status,
    direction: phaseDirection(record),
    signal,
    confidence: record.confidence,
    higherTimeframe: record.higherTimeframe,
    backgroundTimeframe: record.backgroundTimeframe,
    volumeRatio: record.volumeRatio,
    contractionScore: record.contractionScore,
    distanceAtr: record.distanceAtr,
    pivotPrice: record.pivotPrice,
    invalidationPrice: record.invalidationPrice,
    parameterVersion: record.parameterVersion,
    timeframes: options.higherIntervals ?? { management: "", background: "" },
    reasons: record.reasons ?? [],
    warnings: record.warnings ?? [],
  };
}
