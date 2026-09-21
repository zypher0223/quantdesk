/**
 * What a study endpoint answered, and how to tell a queued transfer from a result.
 *
 * A study too large for one HTTP request is not refused: the engine moves the same
 * request to its run queue and answers 202 with the run it created. A caller that
 * only checked `response.ok` would hand that body to the report renderer and show
 * a queue row as if the study had produced numbers. So the transfer is its own
 * outcome, and the study payload is only reachable after narrowing on `queued`.
 *
 * `queuedTransferOf` is deliberately strict: it answers a transfer only for a 202
 * whose body says `queued: true` *and* names a run. `POST /api/backtest/runs`
 * answers 202 too, but with `{run, deduplicated}` — a submission, not a transfer —
 * and must not be mistaken for one.
 */

import { readRunSummary, type RunSummary } from "./runs";

export interface QueuedStudy {
  /** The queue row that will produce the result. Its metrics are not a result. */
  run: RunSummary;
  /** The engine's own sentence: how much work this was, and where it went. */
  reason: string;
  detail: string;
  syncCost: number | null;
  syncBudget: number | null;
}

/** Either the study ran inline, or it was transferred to the queue. */
export type StudyOutcome<T> =
  | ({ queued: true } & QueuedStudy)
  | { queued: false; study: T };

/** The line every calling panel shows for a transferred study. */
export function queuedNotice(queued: Pick<QueuedStudy, "run">): string {
  return `已转入后台（#${queued.run.id}），可在结果中心查看`;
}

/** The engine's explanation, when it sent one; empty when it did not. */
export function queuedReason(queued: Pick<QueuedStudy, "reason" | "detail">): string {
  const parts = [queued.reason, queued.detail].map((part) => (part ?? "").trim()).filter(Boolean);
  return parts.join(" · ");
}

export function queuedTransferOf(status: number, payload: unknown): QueuedStudy | null {
  if (status !== 202) return null;
  const body = asRecord(payload);
  if (!body || body.queued !== true) return null;
  // Without a run there is nothing to watch, so this is not a transfer the page
  // can present; the caller falls through to its normal error path.
  const run = readRunSummary(body.run);
  if (!run) return null;
  return {
    run,
    reason: text(body.reason),
    detail: text(body.detail),
    syncCost: finite(body.syncCost),
    syncBudget: finite(body.syncBudget),
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
