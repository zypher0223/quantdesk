/**
 * The result centre's batch work: comparing runs, exporting them, and remembering
 * the operator's filter.
 *
 * Everything here is pure except the download itself, so the table, the CSV and
 * the unit tests all read the same row. The one rule that matters most: a run whose
 * metrics are null exports "—" and never a zero, because a fabricated number in a
 * spreadsheet outlives the page that invented it.
 */

import {
  RUN_KIND_LABEL,
  RUN_STATUS_LABEL,
  factorCoverageText,
  formatDuration,
  formatRunTime,
  runKindLabel,
  runStatusLabel,
  type RunKind,
  type RunStatus,
  type RunSummary,
} from "./runs";
import { countText, decimalText, drawdownText, percentText } from "./run-figures";

/** The comparison is a reading aid, not a report: six columns is already wide. */
export const COMPARISON_LIMIT = 6;

/** 2–6 runs can be compared; one is not a comparison and seven is not readable. */
export function compareEnabled(count: number): boolean {
  return Number.isFinite(count) && count >= 2 && count <= COMPARISON_LIMIT;
}

/* ------------------------------------------------------------ filter memory */

export const RESULT_FILTER_STORAGE_KEY = "quantdesk.resultCentre.filter";

/** The only three UI choices the result centre persists. */
export interface ResultCentreFilter {
  kind: RunKind | "all";
  status: RunStatus | "all";
  query: string;
}

export const DEFAULT_RESULT_FILTER: ResultCentreFilter = { kind: "all", status: "all", query: "" };

/** The slice of `localStorage` this module needs, so a test can supply its own. */
export interface FilterStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** The browser's store, or null when it is absent or refuses to be read. */
export function browserFilterStorage(): FilterStorage | null {
  try {
    if (typeof window === "undefined") return null;
    return window.localStorage ?? null;
  } catch {
    // Private mode and blocked-cookie settings throw on access rather than on use.
    return null;
  }
}

/**
 * The stored filter, or the default one.
 *
 * A hand-edited or stale value must never break the page: anything that is not one
 * of the engine's kinds, one of its statuses, or a string is dropped rather than
 * passed to the table as a filter that matches nothing.
 */
export function readStoredFilter(storage: FilterStorage | null | undefined): ResultCentreFilter {
  const fallback: ResultCentreFilter = { ...DEFAULT_RESULT_FILTER };
  if (!storage) return fallback;
  let raw: string | null = null;
  try {
    raw = storage.getItem(RESULT_FILTER_STORAGE_KEY);
  } catch {
    return fallback;
  }
  if (!raw) return fallback;
  let parsed: unknown = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return fallback;
  }
  const source = asRecord(parsed);
  if (!source) return fallback;
  const kind = typeof source.kind === "string" ? source.kind : "";
  const status = typeof source.status === "string" ? source.status : "";
  return {
    kind: Object.prototype.hasOwnProperty.call(RUN_KIND_LABEL, kind) ? (kind as RunKind) : "all",
    status: Object.prototype.hasOwnProperty.call(RUN_STATUS_LABEL, status) ? (status as RunStatus) : "all",
    query: typeof source.query === "string" ? source.query : "",
  };
}

/** Write exactly the three UI choices; the filter is never a place to keep data. */
export function storeResultFilter(storage: FilterStorage | null | undefined, filter: ResultCentreFilter): void {
  if (!storage) return;
  try {
    storage.setItem(RESULT_FILTER_STORAGE_KEY, JSON.stringify({ kind: filter.kind, status: filter.status, query: filter.query }));
  } catch {
    // A full or read-only store must not stop the operator from filtering.
  }
}

/* -------------------------------------------------------------- comparison */

/** One run as the comparison table and the CSV both print it. */
export interface ComparisonRow {
  id: number;
  idText: string;
  kind: string;
  symbol: string;
  interval: string;
  /** 标的 and 周期 as one cell for the table. */
  market: string;
  strategy: string;
  status: string;
  netReturn: string;
  maxDrawdown: string;
  trades: string;
  sharpe: string;
  /** A factor run's coverage, where a P&L column would only ever be "—". */
  coverage: string;
  scope: string;
  duration: string;
  completed: string;
}

export function comparisonRow(run: RunSummary): ComparisonRow {
  const headline = run.headline ?? null;
  const symbol = (run.displaySymbol ?? run.symbol ?? (run.symbols ?? []).join(" · ")) || "—";
  const interval = run.interval || "—";
  return {
    id: run.id,
    idText: `#${run.id}`,
    kind: runKindLabel(run.kind),
    symbol,
    interval,
    market: `${symbol} · ${interval}`,
    strategy: run.strategyId ? `${run.strategyId} v${run.strategyVersion || "—"}` : "—",
    status: runStatusLabel(run.status),
    // Every figure below stays "—" when the engine did not report it. A factor run
    // has no P&L at all, and the coverage column carries what it does have.
    netReturn: percentText(headline?.netReturnPct, true),
    maxDrawdown: drawdownText(headline?.maxDrawdownPct),
    trades: countText(headline?.trades),
    sharpe: decimalText(headline?.sharpe),
    coverage: factorCoverageText(run),
    scope: headline?.scope || "—",
    duration: formatDuration(run.durationMs),
    completed: formatRunTime(run.finishedTs),
  };
}

export function comparisonRows(runs: RunSummary[]): ComparisonRow[] {
  return runs.map(comparisonRow);
}

/* -------------------------------------------------------------------- CSV */

export const RESULT_CSV_COLUMNS = [
  "ID", "类型", "标的", "周期", "策略", "状态",
  "净收益", "最大回撤", "交易数", "夏普", "覆盖/请求摘要", "指标口径", "耗时", "完成时间",
] as const;

/**
 * One CSV field.
 *
 * A value containing a quote, a comma or a newline is quoted, and its own quotes
 * are doubled — the rule RFC 4180 states and the one every spreadsheet reads.
 */
export function csvCell(value: string | null | undefined): string {
  const text = value ?? "";
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** The selected runs as a CSV document, header first. */
export function resultsCsv(runs: RunSummary[]): string {
  const lines = [RESULT_CSV_COLUMNS.map((column) => csvCell(column)).join(",")];
  for (const row of comparisonRows(runs)) {
    lines.push([
      row.idText, row.kind, row.symbol, row.interval, row.strategy, row.status,
      row.netReturn, row.maxDrawdown, row.trades, row.sharpe, row.coverage, row.scope,
      row.duration, row.completed,
    ].map(csvCell).join(","));
  }
  return lines.join("\n");
}

/** `quantdesk-runs-20250916-0930.csv`, named after the moment it was taken. */
export function csvFileName(stamp: number): string {
  const at = new Date(Number.isFinite(stamp) ? stamp : Date.now());
  const pad = (value: number) => String(value).padStart(2, "0");
  const date = `${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}`;
  return `quantdesk-runs-${date}-${pad(at.getHours())}${pad(at.getMinutes())}.csv`;
}

/**
 * Hand the CSV to the browser as a download.
 *
 * The BOM is what makes Excel read the Chinese headers as UTF-8 instead of
 * mojibake; the object URL is revoked as soon as the click has been dispatched.
 */
export function downloadCsv(fileName: string, csv: string): void {
  const blob = new Blob([`\ufeff${csv}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** A parsed JSON object, or null for anything else (including arrays). */
function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}
