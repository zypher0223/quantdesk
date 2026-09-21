import { INTERVAL_MS, type Candle, type Instrument, type MarketState, type StatusInfo, type Timeframe } from "../data/market";

/** Only closed candles ever reach analysis. The venue includes the forming bar. */
export function closedCandles(candles: Candle[], timeframe: Timeframe, now = Date.now()): Candle[] {
  const step = INTERVAL_MS[timeframe];
  return candles.filter((candle) => candle.time + step <= now);
}

/** When the next bar of this timeframe closes. */
export function barClosesAt(candles: Candle[], timeframe: Timeframe): number | null {
  if (!candles.length) return null;
  return candles.at(-1)!.time + INTERVAL_MS[timeframe];
}

/**
 * A live feed whose newest bar should already have closed is stale — the venue
 * moved on and our copy did not. Treated as a degraded read, not as fresh data.
 */
export function isStale(candles: Candle[], timeframe: Timeframe, now = Date.now()): boolean {
  const closes = barClosesAt(candles, timeframe);
  if (closes === null) return false;
  return now > closes + INTERVAL_MS[timeframe] * 1.5;
}

export interface StatusInput {
  state: MarketState;
  instrument: Instrument | null;
  degraded: boolean;
  indexError: string | null;
  now: number;
}

/** "3 分 12 秒" / "42 秒" — how long ago the last real message arrived. */
export function humanAge(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "未知";
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

/**
 * What the operator should believe right now.
 *
 * Order matters: a broken upstream link outranks any local freshness, because
 * frozen data that still looks live is the one failure a desk cannot see.
 */
export function deriveStatus({ state, instrument, degraded, indexError, now }: StatusInput): StatusInfo {
  if (indexError) return { level: "degraded", message: "无法读取固定合约池", detail: indexError };
  if (degraded) {
    return {
      level: "down",
      message: "本地行情网关未连接",
      detail: "请在 engine 目录运行 python -m quantdesk.cli serve（默认 127.0.0.1:8765），并确认 QUANTDESK_PROXY 指向可用代理",
    };
  }
  if (!instrument) return { level: "down", message: "正在连接行情网关" };

  const { feed } = state;
  if (feed && feed.state !== "connected") {
    const frozen = state.tickerFetchedAt ?? state.candlesFetchedAt;
    const age = frozen === null ? null : now - frozen;
    return {
      level: "partial",
      message: "行情已中断，显示最后真实数据",
      detail: `上游 ${
        feed.state === "stopped" ? "服务未运行" : feed.state === "starting" ? "正在建立连接" : "连接已断开"
      }${age === null ? "" : `；数据距今 ${humanAge(age)}`}${feed.lastError ? `；${feed.lastError}` : ""}`,
    };
  }
  const delay = feed?.lastMessageAgeMs ?? null;
  const exchangeGap = state.ticker?.updatedAt ? now - state.ticker.updatedAt : null;
  if ((delay !== null && delay > 90_000) || (exchangeGap !== null && exchangeGap > 90_000)) {
    const worst = Math.max(delay ?? 0, exchangeGap ?? 0);
    return {
      level: "partial",
      message: "行情延迟偏高",
      detail: `最近一条交易所消息距今 ${humanAge(worst)}${feed?.reconnects ? `；累计重连 ${feed.reconnects} 次` : ""}`,
    };
  }

  const { candles, ticker, resonance } = state.sources;
  if (candles === "demo") {
    return { level: "degraded", message: "演示数据", detail: state.errors.candles ?? "实时K线不可用，图表为合成数据，不可用于决策" };
  }
  if (candles === "upload") {
    return { level: "partial", message: "本地数据", detail: "当前K线来自上传文件；衍生品指标仍为交易所实时值" };
  }

  const missing = [
    ticker === "demo" ? "衍生品指标" : null,
    resonance === "demo" ? "多周期共振" : null,
  ].filter((item): item is string => item !== null);
  if (candles === "live" && missing.length) {
    return { level: "partial", message: "部分数据降级", detail: `${missing.join("与")}暂不可用，其余为 Bybit 实时数据` };
  }

  if (isStale(state.candles, state.timeframe, now)) {
    return {
      level: "partial",
      message: "数据可能滞后",
      detail: `最新已收盘K线为 ${new Date(barClosesAt(state.candles, state.timeframe)!).toLocaleString("zh-CN", { hour12: false })}，交易所可能已开出更新的一根`,
    };
  }

  if (candles === "live" && ticker === "live" && resonance === "live") {
    return {
      level: "live",
      message: "BYBIT 实时",
      detail: delay === null ? undefined : `交易所消息延迟 ${delay} ms`,
    };
  }
  return { level: "partial", message: "数据加载中" };
}
