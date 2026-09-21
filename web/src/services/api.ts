import type { Candle, InstrumentIndex, ResonanceResult, Ticker, Timeframe } from "../data/market";

const INTERVAL_CODE: Record<string, string> = { "15m": "15", "1h": "60", "4h": "240", "1d": "D", "1w": "W" };

export class GatewayError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "GatewayError";
  }

  /** The local gateway never answered — it is not running or not reachable. */
  get unreachable(): boolean {
    return this.status === 0;
  }
}

async function request(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<any> {
  const { timeoutMs = 30_000, ...rest } = init ?? {};
  const signal = AbortSignal.timeout(timeoutMs);
  let response: Response;
  try {
    response = await fetch(path, { ...rest, signal });
  } catch (reason) {
    const detail = reason instanceof Error ? reason.message : String(reason);
    throw new GatewayError(`无法连接本地行情网关（127.0.0.1:8765）：${detail}`, 0);
  }
  if (!response.ok) {
    let message = `行情网关返回 ${response.status}`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") message = body.detail;
    } catch {
      /* the gateway did not send JSON; keep the status-based message */
    }
    throw new GatewayError(message, response.status);
  }
  return response.json();
}

export async function fetchInstrumentIndex(signal?: AbortSignal): Promise<InstrumentIndex> {
  return request("/api/instruments", { signal });
}

export async function fetchHealth(): Promise<{ ok: boolean; proxyConfigured: boolean }> {
  const body = await request("/health", { timeoutMs: 5_000 });
  return { ok: Boolean(body?.ok), proxyConfigured: Boolean(body?.proxyConfigured) };
}

export async function fetchCandles(venueSymbol: string, timeframe: Timeframe, limit = 300, signal?: AbortSignal): Promise<Candle[]> {
  const query = new URLSearchParams({
    category: "linear",
    symbol: venueSymbol,
    interval: INTERVAL_CODE[timeframe] ?? "60",
    limit: String(limit),
  });
  const body = await request(`/bybit/v5/market/kline?${query}`, { signal });
  const rows = body?.result?.list as string[][] | undefined;
  if (!Array.isArray(rows)) throw new GatewayError("行情返回结构异常", 502);
  // The venue returns newest-first and includes the still-forming bar.
  return rows
    .map((row) => ({
      time: Number(row[0]),
      open: Number(row[1]),
      high: Number(row[2]),
      low: Number(row[3]),
      close: Number(row[4]),
      volume: Number(row[5]),
      turnover: row[6] === undefined ? null : Number(row[6]),
    }))
    .sort((a, b) => a.time - b.time);
}

function numberOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export async function fetchTicker(venueSymbol: string, signal?: AbortSignal): Promise<Ticker> {
  const query = new URLSearchParams({ category: "linear", symbol: venueSymbol });
  const body = await request(`/bybit/v5/market/tickers?${query}`, { signal });
  const row = body?.result?.list?.[0];
  if (!row) throw new GatewayError("该合约暂无行情", 502);
  const last = numberOrNull(row.lastPrice);
  if (last === null) throw new GatewayError("该合约暂无有效成交价", 502);
  const openInterest = numberOrNull(row.openInterest);
  return {
    lastPrice: last,
    markPrice: numberOrNull(row.markPrice) ?? last,
    indexPrice: numberOrNull(row.indexPrice) ?? last,
    prevPrice24h: numberOrNull(row.prevPrice24h),
    price24hPcnt: numberOrNull(row.price24hPcnt),
    openInterest,
    // Prefer the venue's own USDT notional over openInterest, which is quoted
    // in base coin (contracts for stock perps) and is off by orders of magnitude.
    openInterestValue: numberOrNull(row.openInterestValue) ?? (openInterest !== null ? openInterest * last : null),
    volume24h: numberOrNull(row.volume24h),
    turnover24h: numberOrNull(row.turnover24h),
    fundingRate: numberOrNull(row.fundingRate),
    fundingIntervalHour: numberOrNull(row.fundingIntervalHour),
    nextFundingTime: numberOrNull(row.nextFundingTime),
    updatedAt: Date.now(),
  };
}

export async function fetchResonance(venueSymbol: string, bars = 300, signal?: AbortSignal): Promise<ResonanceResult> {
  const query = new URLSearchParams({ symbol: venueSymbol, bars: String(bars) });
  const body = await request(`/api/resonance?${query}`, { signal, timeoutMs: 60_000 });
  if (!body || !Array.isArray(body.timeframes)) throw new GatewayError("共振引擎返回结构异常", 502);
  return body as ResonanceResult;
}

export interface FundingPoint {
  ts: number;
  rate: number;
}

export async function fetchFundingHistory(venueSymbol: string, limit = 24, signal?: AbortSignal): Promise<{ list: FundingPoint[]; allZero: boolean }> {
  const query = new URLSearchParams({ symbol: venueSymbol, limit: String(limit) });
  const body = await request(`/bybit/v5/market/funding/history?${query}`, { signal });
  return { list: Array.isArray(body?.list) ? body.list : [], allZero: Boolean(body?.allZero) };
}
