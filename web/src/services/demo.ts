import { INTERVAL_MS, type Candle, type Instrument, type ResonanceResult, type Ticker, type Timeframe } from "../data/market";

/**
 * Labelled fallback data. This is only ever used when the gateway cannot be
 * reached, and every surface that renders it must say so — the prices are
 * synthetic and anchored to the instrument so a demo chart still looks like
 * the instrument it claims to be.
 */
const ANCHORS: Record<string, number> = {
  BTCUSDT: 77_000,
  ETHUSDT: 2_600,
  AAPLUSDT: 333,
  MSFTUSDT: 510,
  GOOGLUSDT: 245,
  AMZNUSDT: 230,
  NVDAUSDT: 178,
  METAUSDT: 640,
  TSLAUSDT: 395,
  SNDKUSDT: 88,
  MUUSDT: 155,
  AMDSTOCKUSDT: 517,
  NBISUSDT: 92,
  SPCXUSDT: 150,
  SKHYUSDT: 210,
  SOXLUSDT: 122,
  SOXSUSDT: 18,
};

function anchorFor(venueSymbol: string): number {
  const known = ANCHORS[venueSymbol];
  if (known) return known;
  const seed = [...venueSymbol].reduce((sum, char) => sum + char.charCodeAt(0), 0);
  return 40 + (seed % 260);
}

/** Deterministic pseudo-random in [0,1) so the demo state is stable across renders. */
function noise(index: number, seed: number): number {
  const value = Math.sin((index + 1) * 12.9898 + seed * 78.233) * 43758.5453;
  return value - Math.floor(value);
}

export function demoCandles(instrument: Instrument, timeframe: Timeframe, count = 180): Candle[] {
  const step = INTERVAL_MS[timeframe];
  const seed = [...instrument.venueSymbol].reduce((sum, char) => sum + char.charCodeAt(0), 0);
  // End on a closed bar so demo data follows the same closed-candle rule as live data.
  const lastClose = Math.floor(Date.now() / step) * step;
  const perBar = instrument.productType === "crypto" ? 0.006 : 0.0025;
  // Start a few percent away from the anchor so the series is not flat.
  let price = anchorFor(instrument.venueSymbol) * (1 + (noise(0, seed) - 0.5) * 0.08);

  return Array.from({ length: count }, (_, index) => {
    const open = price;
    const shock = (noise(index, seed) - 0.5) * 2 * perBar;
    const swing = Math.sin((index + seed) / 21) * perBar * 0.35;
    const close = Math.max(0.01, open * (1 + shock + swing));
    const spread = price * perBar * (0.3 + noise(index + 99, seed) * 0.7);
    // Thin prints outside the cash session, same shape the venue produces.
    const inSession = Math.sin(index / 6.5) > -0.35;
    const volume = Math.round((inSession ? 4_000 : 120) * (0.5 + noise(index + 7, seed)));
    price = close;
    return {
      time: lastClose - (count - index) * step,
      open,
      high: Math.max(open, close) + spread,
      low: Math.max(0.005, Math.min(open, close) - spread),
      close,
      volume,
      turnover: volume * close,
    };
  });
}

export function demoTicker(instrument: Instrument, candles: Candle[]): Ticker {
  const last = candles.at(-1)?.close ?? anchorFor(instrument.venueSymbol);
  const previous = candles.at(-Math.min(candles.length, 96))?.close ?? last;
  const seed = [...instrument.venueSymbol].reduce((sum, char) => sum + char.charCodeAt(0), 0);
  const openInterest = 5_000 + (seed % 40) * 1_800;
  return {
    lastPrice: last,
    markPrice: last * 0.9998,
    indexPrice: last * 1.0003,
    prevPrice24h: previous,
    price24hPcnt: previous ? last / previous - 1 : null,
    openInterest,
    openInterestValue: openInterest * last,
    volume24h: 120_000 + (seed % 90) * 4_000,
    turnover24h: openInterest * last * 22,
    fundingRate: instrument.productType === "crypto" ? 0.0001 : 0,
    fundingIntervalHour: 8,
    nextFundingTime: null,
    updatedAt: Date.now(),
  };
}

/** Demo radar values. Clearly separated from engine output at every call site. */
export function demoResonance(instrument: Instrument): ResonanceResult {
  const seed = [...instrument.venueSymbol].reduce((sum, char) => sum + char.charCodeAt(0), 0);
  const weights: Record<string, number> = { "1d": 0.4, "4h": 0.3, "1h": 0.2, "15m": 0.1 };
  const frames = ["15m", "1h", "4h", "1d"].map((interval, position) => {
    const value = noise(position, seed) * 2 - 1;
    const score = Math.round(value * 3) / 3;
    return {
      interval,
      stance: score > 0.2 ? ("bull" as const) : score < -0.2 ? ("bear" as const) : ("neutral" as const),
      score,
      trend: Math.sign(score),
      momentum: Math.sign(score),
      volume: 0,
      close: anchorFor(instrument.venueSymbol),
      notes: { session_thin: instrument.productType !== "crypto" },
    };
  });
  const score = frames.reduce((sum, frame) => sum + frame.score * (weights[frame.interval] ?? 0), 0);
  return {
    venueSymbol: instrument.venueSymbol,
    displaySymbol: instrument.displaySymbol,
    weights,
    barsRequested: 0,
    minBars: 220,
    computedAt: Date.now(),
    score,
    score_100: Math.round((score + 1) * 50),
    label: score > 0.6 ? "强共振看多" : score > 0.2 ? "偏多" : score < -0.6 ? "强共振看空" : score < -0.2 ? "偏空" : "中性",
    timeframes: frames,
    unavailable: [],
  };
}
