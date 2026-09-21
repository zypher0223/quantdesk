import type { Candle, FeedStatus, MarketSnapshot, NormalizedCandle, Ticker, Timeframe } from "../data/market";
import { GatewayError } from "./api";

export interface MarketSnapshotQuery {
  symbol: string;
  interval?: Timeframe;
}

function numberOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** Drop null entries so "absent" never overwrites a value already on screen. */
function omitUndefined<T extends object>(source: T): T {
  return Object.fromEntries(Object.entries(source).filter(([, value]) => value !== undefined && value !== null)) as T;
}

/** The venue returns stored bars as `{ts, open, high, low, close, volume}`. */
export function toCandle(raw: NormalizedCandle | null | undefined): Candle | null {
  if (!raw) return null;
  const time = numberOrNull(raw.ts);
  const open = numberOrNull(raw.open);
  const high = numberOrNull(raw.high);
  const low = numberOrNull(raw.low);
  const close = numberOrNull(raw.close);
  if (time === null || open === null || high === null || low === null || close === null) return null;
  return {
    time,
    open,
    high,
    low,
    close,
    volume: numberOrNull(raw.volume) ?? 0,
    turnover: numberOrNull(raw.turnover),
  };
}

/**
 * Snapshot ticker -> UI ticker.
 *
 * Every field is read by name from the stored quote; nothing is derived from a
 * sibling value, so a missing field stays missing instead of becoming a
 * plausible-looking number.
 */
export function toTicker(raw: Record<string, unknown>): Ticker | null {
  const last = numberOrNull(raw.last_price);
  if (last === null) return null;
  const openInterest = numberOrNull(raw.open_interest);
  return {
    lastPrice: last,
    markPrice: numberOrNull(raw.mark_price) ?? last,
    indexPrice: numberOrNull(raw.index_price) ?? last,
    prevPrice24h: numberOrNull(raw.prev_price_24h),
    price24hPcnt: numberOrNull(raw.price_24h_pct),
    openInterest,
    openInterestValue: numberOrNull(raw.open_interest_value) ?? (openInterest !== null ? openInterest * last : null),
    volume24h: numberOrNull(raw.volume_24h),
    turnover24h: numberOrNull(raw.turnover_24h),
    fundingRate: numberOrNull(raw.funding_rate),
    fundingIntervalHour: numberOrNull(raw.funding_interval_hour),
    nextFundingTime: numberOrNull(raw.next_funding_time),
    updatedAt: numberOrNull(raw.received_ts) ?? Date.now(),
  };
}

/** Fill only the fields a delta actually carries; partial frames never blank data. */
export function mergeTicker(current: Ticker | null, patch: Partial<Ticker>): Ticker {
  const base: Ticker = current ?? {
    lastPrice: null,
    markPrice: null,
    indexPrice: null,
    prevPrice24h: null,
    price24hPcnt: null,
    openInterest: null,
    openInterestValue: null,
    volume24h: null,
    turnover24h: null,
    fundingRate: null,
    fundingIntervalHour: null,
    nextFundingTime: null,
    updatedAt: Date.now(),
  };
  const merged: Ticker = { ...base, updatedAt: Date.now() };
  for (const [key, value] of Object.entries(patch) as Array<[keyof Ticker, Ticker[keyof Ticker]]>) {
    if (key === "updatedAt") continue;
    if (value !== undefined) (merged[key] as Ticker[keyof Ticker]) = value;
  }
  return merged;
}

export async function fetchMarketSnapshot(query: MarketSnapshotQuery, signal?: AbortSignal): Promise<MarketSnapshot> {
  const params = new URLSearchParams({ symbol: query.symbol });
  if (query.interval) params.set("interval", query.interval);
  let response: Response;
  try {
    response = await fetch(`/api/market/snapshot?${params}`, { signal: signal ?? AbortSignal.timeout(8_000) });
  } catch (reason) {
    const detail = reason instanceof Error ? reason.message : String(reason);
    throw new GatewayError(`无法连接本地行情网关（127.0.0.1:8765）：${detail}`, 0);
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new GatewayError(typeof body?.detail === "string" ? body.detail : `行情快照返回 ${response.status}`, response.status);
  }
  if (!body || typeof body.symbol !== "string") throw new GatewayError("行情快照结构异常", 502);
  return body as MarketSnapshot;
}

export async function fetchFeedStatus(signal?: AbortSignal): Promise<FeedStatus> {
  const response = await fetch("/api/market/state", { signal: signal ?? AbortSignal.timeout(8_000) });
  const body = await response.json().catch(() => null);
  if (!response.ok || !body?.connection) throw new GatewayError("行情服务状态不可用", response.status || 502);
  return body.connection as FeedStatus;
}

/* ------------------------------------------------------------------ stream */

export type StreamEvent =
  | { kind: "snapshot"; snapshot: MarketSnapshot }
  | { kind: "ticker"; symbol: string; patch: Partial<Ticker>; exchangeTs: number | null; receivedTs: number | null; observationKey: string | null }
  | { kind: "candle"; symbol: string; interval: string; openTs: number; closed: boolean; candle: Candle; observationKey: string | null; source: string | null }
  | { kind: "connection"; state: string; detail: string; reconnects: number; feed: FeedStatus | null }
  | { kind: "backfill"; rows: number; symbols: number; errors: string[]; feed: FeedStatus | null }
  | { kind: "error"; message: string };

export interface StreamHandlers {
  onEvent: (event: StreamEvent) => void;
}

const STREAM_BASE_DELAY_MS = 500;
const STREAM_MAX_DELAY_MS = 15_000;

export interface LiveFeed {
  close: () => void;
  /** True while the socket is open. */
  readonly open: boolean;
  readonly backlog: number;
}

/**
 * Browser side of `WS /api/market/stream`.
 *
 * The engine is the only component that talks to Bybit; this socket carries
 * normalized deltas for the fixed universe. It reconnects on its own with
 * exponential backoff and re-reads the snapshot afterwards, so a window that
 * opened while the socket was down is filled from the engine's own state.
 */
export function openMarketStream(handlers: StreamHandlers): LiveFeed {
  const url = new URL("/api/market/stream", window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  let socket: WebSocket | null = null;
  let closed = false;
  let attempt = 0;
  let reconnects = 0;
  let backlog = 0;
  let timer: number | null = null;

  const emit = handlers.onEvent;

  const scheduleReconnect = () => {
    if (closed) return;
    const delay = Math.min(STREAM_MAX_DELAY_MS, STREAM_BASE_DELAY_MS * 2 ** attempt) * (0.7 + Math.random() * 0.6);
    attempt += 1;
    reconnects += 1;
    emit({ kind: "connection", state: "reconnecting", detail: "", reconnects, feed: null });
    timer = window.setTimeout(connect, delay);
  };

  const connect = () => {
    if (closed) return;
    try {
      socket = new WebSocket(url.toString());
    } catch (reason) {
      emit({ kind: "error", message: reason instanceof Error ? reason.message : String(reason) });
      scheduleReconnect();
      return;
    }    socket.onopen = () => {
      attempt = 0;
      backlog = 0;
      emit({ kind: "connection", state: "connected", detail: "", reconnects, feed: null });
    };
    socket.onmessage = (message) => {
      // Frames arriving while an earlier one is still being applied are the
      // backlog a desk watches; a throwing handler must not kill the socket.
      backlog += 1;
      try {
        const frame = JSON.parse(String(message.data));
        const event = decodeFrame(frame, reconnects);
        if (event) emit(event);
      } catch (reason) {
        emit({ kind: "error", message: reason instanceof Error ? reason.message : String(reason) });
      } finally {
        backlog = Math.max(0, backlog - 1);
      }
    };
    socket.onerror = () => {
      /* onclose always follows; the message there is what matters. */
    };
    socket.onclose = (event) => {
      socket = null;
      if (closed) return;
      emit({ kind: "connection", state: "disconnected", detail: event.reason || "", reconnects, feed: null });
      scheduleReconnect();
    };
  };

  // The first connection is deferred by one turn of the event loop so this
  // function always returns before any event is delivered: a caller that reads
  // the handle from inside its own event handler never sees an uninitialised one.
  timer = window.setTimeout(connect, 0);

  return {
    close: () => {
      closed = true;
      if (timer !== null) window.clearTimeout(timer);
      socket?.close();
      socket = null;
    },
    get open() {
      return socket?.readyState === WebSocket.OPEN;
    },
    get backlog() {
      return backlog;
    },
  };
}

export function decodeFrame(frame: any, reconnects: number): StreamEvent | null {
  if (!frame || typeof frame !== "object") return null;
  const symbol = typeof frame.symbol === "string" ? frame.symbol : null;
  const exchangeTs = numberOrNull(frame.exchangeTs);
  const receivedTs = numberOrNull(frame.receivedTs);
  switch (frame.kind) {
    case "ticker": {
      if (!symbol) return null;
      const openInterest = numberOrNull(frame.openInterest);
      // Only fields the frame actually carries may overwrite the displayed
      // quote; a partial frame must not blank data it did not include.
      const patch: Partial<Ticker> = omitUndefined({
        lastPrice: numberOrNull(frame.lastPrice),
        markPrice: numberOrNull(frame.markPrice),
        indexPrice: numberOrNull(frame.indexPrice),
        price24hPcnt: numberOrNull(frame.price24hPct),
        openInterest,
        openInterestValue: numberOrNull(frame.openInterestValue),
        volume24h: numberOrNull(frame.volume24h),
        turnover24h: numberOrNull(frame.turnover24h),
        fundingRate: numberOrNull(frame.fundingRate),
        fundingIntervalHour: numberOrNull(frame.fundingIntervalHour),
        nextFundingTime: numberOrNull(frame.nextFundingTime),
      });
      if (Object.keys(patch).length === 0) return null;
      return {
        kind: "ticker",
        symbol,
        patch,
        exchangeTs,
        receivedTs,
        observationKey: typeof frame.observationKey === "string" ? frame.observationKey : null,
      };
    }
    case "candle": {
      if (!symbol || typeof frame.interval !== "string") return null;
      const candle = toCandle({
        ts: numberOrNull(frame.openTs) ?? 0,
        open: numberOrNull(frame.open) ?? 0,
        high: numberOrNull(frame.high) ?? 0,
        low: numberOrNull(frame.low) ?? 0,
        close: numberOrNull(frame.close) ?? 0,
        volume: numberOrNull(frame.volume) ?? 0,
        turnover: numberOrNull(frame.turnover),
      });
      if (!candle) return null;
      return {
        kind: "candle",
        symbol,
        interval: frame.interval,
        openTs: candle.time,
        closed: Boolean(frame.closed),
        candle,
        observationKey: typeof frame.observationKey === "string" ? frame.observationKey : null,
        source: typeof frame.source === "string" ? frame.source : null,
      };
    }
    case "connection":
      return {
        kind: "connection",
        state: typeof frame.state === "string" ? frame.state : "unknown",
        detail: typeof frame.detail === "string" ? frame.detail : "",
        reconnects,
        feed: null,
      };
    case "backfill":
      return {
        kind: "backfill",
        rows: numberOrNull(frame.rows) ?? 0,
        symbols: numberOrNull(frame.symbols) ?? 0,
        errors: Array.isArray(frame.errors) ? frame.errors.filter((item: unknown): item is string => typeof item === "string") : [],
        feed: null,
      };
    case "heartbeat":
      return null;
    default:
      return null;
  }
}
