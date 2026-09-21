/** Horizontal room reserved for Lightweight Charts' right-hand price scale. */
export const FIB_PRICE_SCALE_GUTTER_PX = 72;

/** Keep every level label to the left of the chart's price scale. */
export function fibonacciLevelLabelRight(width: number): number {
  return Math.max(48, width - FIB_PRICE_SCALE_GUTTER_PX);
}

/**
 * Place an anchor label on whichever side has room, and keep its baseline in
 * the visible plot. Long BTC prices near the right edge must never be clipped.
 */
export function fibonacciAnchorLabel(
  x: number,
  y: number,
  width: number,
  height: number,
): { labelX: number; labelY: number; textAnchor: "start" | "end" } {
  const safeRight = fibonacciLevelLabelRight(width);
  const roomOnRight = safeRight - x;
  const roomOnLeft = x;
  const placeOnLeft = roomOnRight < 104 && roomOnLeft >= roomOnRight;
  return {
    labelX: placeOnLeft ? x - 8 : x + 8,
    labelY: Math.max(12, Math.min(height - 6, y - 8)),
    textAnchor: placeOnLeft ? "end" : "start",
  };
}
