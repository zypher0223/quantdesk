import type { Candle } from "../data/market";

interface Column {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  thin: boolean;
}

export interface ParsedSeries {
  candles: Candle[];
  thinFlags: boolean[];
}

/**
 * Off-hours bars carry a negligible fraction of session turnover. This mirrors
 * the engine's thin-session rule (close*volume vs a short rolling median) so the
 * chart can mark bars the analysis deliberately ignores.
 */
export function markThinBars(candles: Candle[], lookback = 20): boolean[] {
  const notional = candles.map((candle) => candle.close * candle.volume);
  return candles.map((_, index) => {
    const from = Math.max(0, index - lookback + 1);
    const window = notional.slice(from, index).filter((value) => value > 0).sort((a, b) => a - b);
    if (window.length < 5) return false;
    const median = window[Math.floor(window.length / 2)];
    return median > 0 ? notional[index] / median < 0.1 : false;
  });
}

export function buildColumns(candles: Candle[], lookback = 20): { columns: Column[]; thinFlags: boolean[] } {
  const thinFlags = markThinBars(candles, lookback);
  return {
    thinFlags,
    columns: candles.map((candle, index) => ({ ...candle, thin: thinFlags[index] ?? false })),
  };
}
