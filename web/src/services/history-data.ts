export type BackfillStatus = "pending" | "running" | "paused" | "done" | "failed" | "unsupported" | "cancelled";

export interface BackfillTask {
  id: number; symbol: string; interval: string; dataKind: string; kindLabel: string;
  status: BackfillStatus; pages: number; pagesRemaining: number; progressPct: number;
  complete: boolean; estimateExhausted: boolean; rowsAvailable: number; attempts: number;
  failureAttempts: number; maxAttempts: number; failureKind: string; failureLabel: string;
  failure: string | null; reason: string | null; updatedTs: number;
}

export interface BackfillBoard {
  paused: boolean; workerRunning: boolean; workerError: string | null;
  concurrency: number; pagesPerMinute: number;
  summary: { total: number; finished: number; pending: number; byStatus: Record<string, number> };
  tasks: BackfillTask[];
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(typeof body?.detail === "string" ? body.detail : `历史数据服务返回 ${response.status}`);
  return body as T;
}

/** What a series can be formally studied over, per contract and family. */
export interface BacktestableRange {
  symbol: string;
  dataKind: string;
  interval: string;
  status: "ok" | "gapped" | "not_reached_listing" | "unsupported" | "no_data";
  reason: string;
  barsAvailable: number;
  usableBars: number;
  historyFromTs: number | null;
  historyToTs: number | null;
  usableFromTs: number | null;
  usableToTs: number | null;
  /** The *usable window* is unbroken. Null for event series (funding, OI, ladder). */
  gapFree: boolean | null;
  hasGaps: boolean;
  reachedListing: boolean | null;
  scanLimitReached: boolean;
}

export interface BacktestableRanges {
  rows: BacktestableRange[];
  byStatus: Record<string, number>;
  summary: { series: number; gapFree: number; reachedListing: number; unsupported: number; empty: number };
}

export const RANGE_STATUS: Record<string, string> = {
  ok: "可用于正式回测",
  gapped: "区间有缺口",
  not_reached_listing: "未回溯到上线",
  unsupported: "交易所不支持",
  no_data: "尚无数据",
};

export const fetchBackfillBoard = () => json<BackfillBoard>("/api/data/backfill/tasks");
export const fetchBacktestableRanges = (symbol?: string) =>
  json<BacktestableRanges>(`/api/data/backfill/ranges${symbol ? `?symbol=${encodeURIComponent(symbol)}` : ""}`);
export async function buildBackfillMatrix(): Promise<BackfillBoard> {
  const body = await json<{ board: BackfillBoard }>("/api/data/backfill/tasks", { method: "POST", body: JSON.stringify({}) });
  return body.board;
}
export const controlBackfillQueue = (action: "pause" | "resume") =>
  json<BackfillBoard>(`/api/data/backfill/${action}`, { method: "POST" });
export const controlBackfillTask = (id: number, action: "pause" | "resume" | "cancel" | "retry") =>
  json<BackfillTask>(`/api/data/backfill/tasks/${id}/${action}`, { method: "POST" });
