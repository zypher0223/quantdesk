export type InstrumentClass = "stock" | "etf" | "crypto";
export type RiskClass = "standard" | "sector" | "leveraged_etf" | "special" | "adr";
export type PoolKey = "stock" | "crypto";

export interface Instrument {
  displaySymbol: string;
  venueSymbol: string;
  name: string;
  group: string;
  productType: InstrumentClass;
  productLabel: string;
  riskClass: RiskClass;
  chartInterval: string;
  underlyingSymbol: string | null;
  pool: PoolKey;
  symbolMapped: boolean;
}

export interface Pool {
  key: PoolKey;
  label: string;
  count: number;
  symbols: string[];
}

export interface InstrumentIndex {
  timeframes: string[];
  resonanceMinBars: number;
  pools: Pool[];
  instruments: Instrument[];
  cacheTtlSeconds?: number;
  klineBars?: number;
}

/**
 * Canonical analysis timeframes. The engine is the source of truth for these, and
 * `1w` is fetchable there (crypto carries 5+ years of weekly bars). It is offered
 * here as a chart timeframe as well, because data nobody can open is data nobody
 * uses; signal eligibility and resonance still run on the four core timeframes.
 */
export const TIMEFRAMES = ["15m", "1h", "4h", "1d", "1w"] as const;
export type Timeframe = (typeof TIMEFRAMES)[number];

/** Candle length in ms — used to decide whether a bar has closed. */
export const INTERVAL_MS: Record<Timeframe, number> = {
  "15m": 900_000,
  "1h": 3_600_000,
  "4h": 14_400_000,
  "1d": 86_400_000,
  "1w": 604_800_000,
};

export const RISK_COPY: Record<RiskClass, string> = {
  standard: "标准合约风控",
  sector: "计入半导体组合敞口",
  leveraged_etf: "三倍杠杆 ETF，展期损耗需单独计入",
  special: "事件与合约状态监控",
  adr: "ADR 比例与时区校验",
};

export type Source = "live" | "upload" | "demo" | "none";

/** Where each piece of the snapshot came from — never inferred from a sibling value. */
export interface SnapshotSources {
  candles: Source;
  ticker: Source;
  resonance: Source;
}

export interface ResonanceFrame {
  interval: string;
  stance: "bull" | "bear" | "neutral";
  score: number | null;
  trend: number;
  momentum: number;
  volume: number;
  close: number | null;
  notes: {
    adx?: number | null;
    rsi?: number | null;
    vol_ratio?: number | null;
    session_thin?: boolean;
    activity_ratio?: number | null;
    insufficient_bars?: boolean;
  };
}

export interface ResonanceUnavailable {
  interval: string;
  bars: number;
  required: number;
}

export interface ResonanceResult {
  venueSymbol: string;
  displaySymbol: string;
  weights: Record<string, number>;
  barsRequested: number;
  minBars: number;
  computedAt: number;
  score: number | null;
  score_100: number | null;
  label: string;
  timeframes: ResonanceFrame[];
  unavailable: ResonanceUnavailable[];
}

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  turnover?: number | null;
}

export interface Ticker {
  lastPrice: number | null;
  markPrice: number | null;
  indexPrice: number | null;
  prevPrice24h: number | null;
  price24hPcnt: number | null;
  /** Base-coin (or contract) quantity, NOT a USDT amount. */
  openInterest: number | null;
  /** USDT notional of open interest — this is the number a desk should read. */
  openInterestValue: number | null;
  volume24h: number | null;
  turnover24h: number | null;
  fundingRate: number | null;
  fundingIntervalHour: number | null;
  nextFundingTime: number | null;
  updatedAt: number;
}

export type StatusLevel = "live" | "partial" | "degraded" | "down";

export interface StatusInfo {
  level: StatusLevel;
  message: string;
  detail?: string;
}

/**
 * Upstream market-data link, as reported by the in-process engine service.
 * `state` mirrors the Bybit WebSocket; `lastMessageAgeMs` is the data delay a
 * desk actually cares about, and it never hides behind a REST poll.
 */
export type FeedState = "stopped" | "starting" | "connected" | "degraded";

export interface FeedStreamStatus {
  name: string;
  state: string;
  topics: number;
  attempt: number;
  reconnects: number;
  messagesReceived: number;
  connectedAt: number | null;
  lastMessageAt: number | null;
  lastError: string | null;
}

export interface FeedStatus {
  state: FeedState;
  connected: boolean;
  reconnects: number;
  lastMessageAt: number | null;
  lastMessageAgeMs: number | null;
  lastSuccessAt: number | null;
  lastSuccessAgeMs: number | null;
  lastBackfillAt: number | null;
  lastError: string | null;
  proxyConfigured: boolean;
  streams: FeedStreamStatus[];
  /** Filled by the browser client, not by the engine. */
  streamState?: "idle" | "connecting" | "open" | "closed";
  reconnectsLocal?: number;
  lastEventAt?: number | null;
  lastEventType?: string | null;
  backlog?: number;
}

/** Provenance of one piece of the snapshot; never inferred from a sibling. */
export type SnapshotSource = "websocket" | "rest_backfill" | "sqlite" | "local_cache" | "demo";

/** One bar exactly as stored: venue field names, closed bars only. */
export interface NormalizedCandle {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  turnover: number | null;
}

export interface MarketSnapshot {
  venue: string;
  symbol: string;
  displaySymbol: string;
  interval: string | null;
  candle: NormalizedCandle | null;
  formingCandle: NormalizedCandle | null;
  ticker: Record<string, unknown>;
  source: string | null;
  exchangeTs: number | null;
  receivedTs: number | null;
  ageMs: number | null;
  stale: boolean;
  connection: FeedStatus;
}

export interface MarketState {
  instrument: Instrument | null;
  timeframe: Timeframe;
  candles: Candle[];
  ticker: Ticker | null;
  resonance: ResonanceResult | null;
  sources: SnapshotSources;
  errors: { candles?: string; ticker?: string; resonance?: string };
  loading: boolean;
  candlesFetchedAt: number | null;
  tickerFetchedAt: number | null;
  resonanceFetchedAt: number | null;
  status: StatusInfo;
  health: { ok: boolean; proxyConfigured: boolean } | null;
  hasData: boolean;
  /** Upstream link state and message delay; null until the first read. */
  feed: FeedStatus | null;
  /** True while the newest displayed bar is still forming (live intra-bar). */
  liveBar: boolean;
  /** The venue's forming bar; never part of the closed series analysis reads. */
  formingCandle: Candle | null;
}

export function emptyMarketState(): MarketState {
  return {
    instrument: null,
    timeframe: "1h",
    candles: [],
    ticker: null,
    resonance: null,
    sources: { candles: "none", ticker: "none", resonance: "none" },
    errors: {},
    loading: false,
    candlesFetchedAt: null,
    tickerFetchedAt: null,
    resonanceFetchedAt: null,
    status: { level: "down", message: "正在连接行情网关" },
    health: null,
    hasData: false,
    feed: null,
    liveBar: false,
    formingCandle: null,
  };
}

export function instrumentsByPool(index: InstrumentIndex | null): Array<{ pool: Pool; items: Instrument[] }> {
  if (!index) return [];
  return index.pools.map((pool) => ({
    pool,
    items: index.instruments.filter((item) => item.pool === pool.key),
  }));
}
