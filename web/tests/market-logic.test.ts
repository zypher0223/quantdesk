/**
 * Browser market-data logic: delta merging, provenance and stale handling.
 *
 * These are the rules that decide what an operator sees, so they are tested
 * without a DOM: a late frame must not move a price backwards, a partial frame
 * must not blank a field it did not carry, and a symbol switch must not let the
 * previous subscription repaint the chart.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { MARKET_UI_REFRESH_MS, mergeCandle } from "../src/hooks/use-market.ts";
import { deriveStatus, humanAge } from "../src/lib/snapshot.ts";
import { fibonacciAnchorLabel, fibonacciLevelLabelRight } from "../src/lib/fibonacci-layout.ts";
import { decodeFrame, mergeTicker, toCandle, toTicker } from "../src/services/market.ts";
import { emptyMarketState, type Candle, type MarketState } from "../src/data/market.ts";

function bar(time: number, close: number): Candle {
  return { time, open: close - 1, high: close + 1, low: close - 2, close, volume: 10 };
}

test("live market deltas paint on a one-second cadence", () => {
  assert.equal(MARKET_UI_REFRESH_MS, 1_000);
});

test("Fibonacci labels stay left of the price scale and flip inward at the edge", () => {
  assert.equal(fibonacciLevelLabelRight(1_000), 928);
  assert.deepEqual(fibonacciAnchorLabel(950, 6, 1_000, 460), {
    labelX: 942,
    labelY: 12,
    textAnchor: "end",
  });
  assert.deepEqual(fibonacciAnchorLabel(300, 200, 1_000, 460), {
    labelX: 308,
    labelY: 192,
    textAnchor: "start",
  });
});

test("a newer closed bar is appended and the same bar is replaced in place", () => {
  const series = [bar(1_000, 10), bar(2_000, 11)];
  const appended = mergeCandle(series, bar(3_000, 12));
  assert.equal(appended.changed, true);
  assert.equal(appended.candles.length, 3);
  assert.equal(appended.candles.at(-1)?.close, 12);

  const replaced = mergeCandle(appended.candles, { ...bar(3_000, 9), high: 20 });
  assert.equal(replaced.changed, true);
  assert.equal(replaced.candles.length, 3);
  assert.equal(replaced.candles.at(-1)?.close, 9);
  assert.equal(replaced.candles.at(-1)?.high, 20);
});

test("an identical replay changes nothing", () => {
  const series = [bar(1_000, 10), bar(2_000, 11)];
  const replay = mergeCandle(series, bar(2_000, 11));
  assert.equal(replay.changed, false);
  assert.equal(replay.candles, series);
});

test("a bar older than the displayed series is discarded", () => {
  const series = [bar(2_000, 11), bar(3_000, 12)];
  const late = mergeCandle(series, bar(1_000, 10));
  assert.equal(late.changed, false);
  assert.equal(late.candles, series);
});

test("a bar already in the middle of the series is replaced, never appended", () => {
  // The snapshot, a REST backfill and the stream can all deliver the same bar;
  // appending it would put two bars with one open time on the chart.
  const series = [bar(1_000, 10), bar(2_000, 11), bar(3_000, 12)];
  const replay = mergeCandle(series, { ...bar(2_000, 99), high: 120 });
  assert.equal(replay.changed, true);
  assert.equal(replay.candles.length, 3);
  assert.equal(replay.candles[1].close, 99);
  assert.equal(replay.candles[1].high, 120);
  assert.equal(new Set(replay.candles.map((candle) => candle.time)).size, 3);
});

test("snapshot ticker keeps venue numbers and never invents a price", () => {
  const ticker = toTicker({
    last_price: 77785.9,
    mark_price: 77780,
    index_price: 77790.1,
    funding_rate: -0.00000757,
    funding_interval_hour: 8,
    open_interest: 53524.596,
    open_interest_value: 4138248787.28,
    price_24h_pct: 0.00555,
    volume_24h: 72172.63,
    turnover_24h: 5609628158.77,
    received_ts: 1_789_443_832_010,
  });
  assert.ok(ticker);
  assert.equal(ticker.lastPrice, 77785.9);
  assert.equal(ticker.markPrice, 77780);
  assert.equal(ticker.openInterest, 53524.596);
  assert.equal(ticker.openInterestValue, 4138248787.28);
  assert.equal(ticker.updatedAt, 1_789_443_832_010);

  assert.equal(toTicker({ mark_price: 1 }), null, "a quote without a price is not a quote");
  assert.equal(toTicker({ last_price: "" }), null);
});

test("open interest notional falls back to base quantity times price", () => {
  const ticker = toTicker({ last_price: 100, open_interest: 5 });
  assert.ok(ticker);
  assert.equal(ticker.openInterestValue, 500);
});

test("a partial ticker delta never blanks a field it did not carry", () => {
  const current = toTicker({ last_price: 100, mark_price: 101, open_interest_value: 900 });
  assert.ok(current);
  const merged = mergeTicker(current, { lastPrice: 102 });
  assert.equal(merged.lastPrice, 102);
  assert.equal(merged.markPrice, 101, "mark price must survive a last-price-only frame");
  assert.equal(merged.openInterestValue, 900);
});

test("intervals and missing prices are dropped by the frame decoder", () => {
  const candle = decodeFrame(
    { kind: "candle", symbol: "BTCUSDT", interval: "15m", openTs: 1_700_000_000_000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 3, closed: true },
    0,
  );
  assert.equal(candle?.kind, "candle");
  if (candle?.kind !== "candle") return;
  assert.equal(candle.closed, true);
  assert.equal(candle.candle.close, 1.5);

  assert.equal(decodeFrame({ kind: "candle", symbol: "BTCUSDT" }, 0), null, "a candle frame without an interval is unusable");
  assert.equal(decodeFrame({ kind: "ticker", symbol: "BTCUSDT" }, 0), null, "an empty ticker frame must not clear the quote");
  assert.equal(decodeFrame({ kind: "heartbeat", ts: 1 }, 0), null);
});

/* ------------------------------------------------------------- status copy */

const NOW = 1_800_000_000_000;

function stateWith(patch: Partial<MarketState>): MarketState {
  return {
    ...emptyMarketState(),
    instrument: {
      displaySymbol: "BTC",
      venueSymbol: "BTCUSDT",
      name: "Bitcoin",
      group: "crypto",
      productType: "crypto",
      productLabel: "加密永续",
      riskClass: "standard",
      chartInterval: "15m",
      underlyingSymbol: null,
      pool: "crypto",
      symbolMapped: true,
    },
    timeframe: "15m",
    sources: { candles: "live", ticker: "live", resonance: "live" },
    candles: [{ time: NOW - 900_000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 1 }],
    tickerFetchedAt: NOW - 5_000,
    candlesFetchedAt: NOW - 5_000,
    ...patch,
  };
}

test("a dropped upstream link is reported even though local data is fresh", () => {
  const state = stateWith({
    feed: {
      state: "degraded",
      connected: false,
      reconnects: 3,
      lastMessageAt: NOW - 120_000,
      lastMessageAgeMs: 120_000,
      lastSuccessAt: NOW - 120_000,
      lastSuccessAgeMs: 120_000,
      lastBackfillAt: null,
      lastError: "proxy refused connection",
      proxyConfigured: true,
      streams: [],
    },
    ticker: {
      lastPrice: 100,
      markPrice: 100,
      indexPrice: 100,
      prevPrice24h: null,
      price24hPcnt: null,
      openInterest: null,
      openInterestValue: null,
      volume24h: null,
      turnover24h: null,
      fundingRate: null,
      fundingIntervalHour: null,
      nextFundingTime: null,
      updatedAt: NOW - 120_000,
    },
  });
  const status = deriveStatus({ state, instrument: state.instrument, degraded: false, indexError: null, now: NOW });
  assert.equal(status.level, "partial");
  assert.match(status.message, /行情已中断/);
  assert.match(status.detail ?? "", /最后真实数据|数据距今/);
  assert.match(status.detail ?? "", /proxy refused/);
});

test("a live link with a fresh quote reads as live and states the delay", () => {
  const state = stateWith({
    feed: {
      state: "connected",
      connected: true,
      reconnects: 0,
      lastMessageAt: NOW - 300,
      lastMessageAgeMs: 300,
      lastSuccessAt: NOW - 300,
      lastSuccessAgeMs: 300,
      lastBackfillAt: NOW - 10_000,
      lastError: null,
      proxyConfigured: true,
      streams: [],
    },
    ticker: {
      lastPrice: 100,
      markPrice: 100,
      indexPrice: 100,
      prevPrice24h: null,
      price24hPcnt: null,
      openInterest: null,
      openInterestValue: null,
      volume24h: null,
      turnover24h: null,
      fundingRate: null,
      fundingIntervalHour: null,
      nextFundingTime: null,
      updatedAt: NOW - 300,
    },
  });
  const status = deriveStatus({ state, instrument: state.instrument, degraded: false, indexError: null, now: NOW });
  assert.equal(status.level, "live");
  assert.match(status.detail ?? "", /300 ms/);
});

test("a connected link with an old message is called out as delayed, not live", () => {
  const state = stateWith({
    feed: {
      state: "connected",
      connected: true,
      reconnects: 1,
      lastMessageAt: NOW - 120_000,
      lastMessageAgeMs: 120_000,
      lastSuccessAt: NOW - 120_000,
      lastSuccessAgeMs: 120_000,
      lastBackfillAt: null,
      lastError: null,
      proxyConfigured: true,
      streams: [],
    },
  });
  const status = deriveStatus({ state, instrument: state.instrument, degraded: false, indexError: null, now: NOW });
  assert.equal(status.level, "partial");
  assert.match(status.message, /延迟偏高/);
});

test("human ages stay readable", () => {
  assert.equal(humanAge(12_000), "12 秒");
  assert.equal(humanAge(192_000), "3 分 12 秒");
  assert.equal(humanAge(7_500_000), "2 小时 5 分");
  assert.equal(humanAge(null), "未知");
});
