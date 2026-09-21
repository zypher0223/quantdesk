/**
 * How a study's numbers are printed.
 *
 * One rule governs every function here: a figure the engine did not report stays
 * "—". A missing drawdown is not 0.00%, and an unreported trade count is not 0 —
 * printing either would turn "we don't know" into a measurement. The result
 * centre, the comparison table and the export all read their cells from here so
 * they cannot disagree about what a missing number looks like.
 */

/** Signed percentage, e.g. `+12.50%`. `signed` adds the `+` for a positive value. */
export function percentText(value: number | null | undefined, signed = false): string {
  if (!isNumber(value)) return "—";
  return `${signed && value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

/** A drawdown is printed as a loss even when the engine stored it unsigned. */
export function drawdownText(value: number | null | undefined): string {
  if (!isNumber(value)) return "—";
  return `-${Math.abs(value).toFixed(2)}%`;
}

export function countText(value: number | null | undefined): string {
  if (!isNumber(value)) return "—";
  return String(Math.round(value));
}

export function decimalText(value: number | null | undefined, digits = 2): string {
  if (!isNumber(value)) return "—";
  return value.toFixed(digits);
}

/** The colour a figure deserves: unknown is neither good nor bad. */
export function toneOf(value: number | null | undefined): "positive" | "negative" | undefined {
  if (!isNumber(value)) return undefined;
  return value >= 0 ? "positive" : "negative";
}

/** A progress bar stays inside 0–100 even if the engine reports nonsense. */
export function progressOf(value: number | null | undefined): number {
  if (!isNumber(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

/**
 * Bytes as MB, the unit the retention policy is discussed in.
 *
 * Below a megabyte the MB figure would round to 0.0 and read as "nothing", so the
 * exact byte count is shown instead.
 */
export function megabytesText(bytes: number | null | undefined): string {
  if (!isNumber(bytes) || bytes < 0) return "—";
  if (bytes < 1_000_000) return `${Math.round(bytes)} B`;
  return `${(bytes / 1_000_000).toFixed(2)} MB`;
}

function isNumber(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}
