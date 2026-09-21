import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  TIMEFRAMES,
  emptyMarketState,
  type Candle,
  type FeedStatus,
  type Instrument,
  type InstrumentIndex,
  type MarketState,
  type Source,
  type Timeframe,
} from "../data/market";
import { barClosesAt, closedCandles, deriveStatus } from "../lib/snapshot";
import { GatewayError, fetchCandles, fetchHealth, fetchInstrumentIndex, fetchResonance } from "../services/api";
import { fetchFeedStatus, fetchMarketSnapshot, mergeTicker, openMarketStream, toCandle, toTicker, type LiveFeed, type StreamEvent } from "../services/market";
import { demoCandles, demoResonance } from "../services/demo";

/** How long a fetched slice stays fresh before a symbol/timeframe revisit refetches it. */
const CANDLE_TTL_MS = 60_000;
const RESONANCE_TTL_MS = 5 * 60_000;
const DEFAULT_TIMEFRAME: Timeframe = "1h";

const KLINE_BARS = 300;
/** EMA200 + ADX(14) need this much history before a stance means anything. */
const RESONANCE_MIN_BARS = 220;
/** Health is a status read, not a data fetch: it never triggers a reload. */
const HEALTH_INTERVAL_MS = 60_000;
/**
 * Paint venue deltas once per second.
 *
 * Bybit remains the source of truth and continues to push over WebSocket. We
 * only coalesce bursts in the browser; polling REST every second would add
 * rate-limit risk and would be slower than the exchange stream.
 */
export const MARKET_UI_REFRESH_MS = 1_000;

interface CacheSlice {
  candles?: { value: Candle[]; at: number };
  ticker?: { value: MarketState["ticker"]; at: number };
  resonance?: { value: MarketState["resonance"]; at: number };
}

type Cache = Record<string, CacheSlice>;

function keyOf(timeframe: Timeframe, venueSymbol: string): string {
  return `${timeframe}:${venueSymbol}`;
}

function isFresh(fetchedAt: number | null, ttl: number, now: number): boolean {
  return fetchedAt !== null && now - fetchedAt < ttl;
}

/**
 * Insert or replace one bar by its open time.
 *
 * The identity of a bar is its open time, so an incoming bar is matched before
 * it is appended: the live stream, a REST backfill and a snapshot read can all
 * deliver the same bar, and the series must never hold it twice. A bar older
 * than the whole series is dropped — after a symbol switch, a late frame from
 * the previous subscription must not touch the chart.
 */
export function mergeCandle(candles: Candle[], incoming: Candle): { candles: Candle[]; changed: boolean } {
  const index = candles.findIndex((candle) => candle.time === incoming.time);
  if (index === -1) {
    const last = candles.at(-1);
    if (last && incoming.time < last.time) return { candles, changed: false };
    return { candles: [...candles, incoming], changed: true };
  }
  const existing = candles[index];
  if (
    existing.open === incoming.open &&
    existing.high === incoming.high &&
    existing.low === incoming.low &&
    existing.close === incoming.close &&
    existing.volume === incoming.volume
  ) {
    return { candles, changed: false };
  }
  const next = candles.slice();
  next[index] = incoming;
  return { candles: next, changed: true };
}

export interface MarketController {
  state: MarketState;
  index: InstrumentIndex | null;
  indexError: string | null;
  degraded: boolean;
  select: (instrument: Instrument) => void;
  setTimeframe: (timeframe: Timeframe) => void;
  refresh: (options?: { silent?: boolean }) => void;
  applyUpload: (candles: Candle[], label: string) => void;
  clearUpload: () => void;
  uploadLabel: string | null;
  /** ms since epoch when the currently loaded bar closes, or null when unknown. */
  barClosesAt: number | null;
  /** Browser-side view of the local realtime socket. */
  stream: { open: boolean; state: FeedStatus["streamState"]; backlog: number; reconnects: number };
}

export function useMarket(): MarketController {
  const [index, setIndex] = useState<InstrumentIndex | null>(null);
  const [indexError, setIndexError] = useState<string | null>(null);
  const [degraded, setDegraded] = useState(false);
  const [instrument, setInstrument] = useState<Instrument | null>(null);
  const [timeframe, setTimeframeState] = useState<Timeframe>(DEFAULT_TIMEFRAME);
  const [snapshot, setSnapshot] = useState<MarketState>(emptyMarketState);
  const [uploadLabel, setUploadLabel] = useState<string | null>(null);
  const [freshnessTick, setFreshnessTick] = useState(0);
  const [streamView, setStreamView] = useState<MarketController["stream"]>({ open: false, state: "idle", backlog: 0, reconnects: 0 });

  const cacheRef = useRef<Cache>({});
  const generationRef = useRef(0);
  const uploadRef = useRef<{ venueSymbol: string; timeframe: Timeframe } | null>(null);
  const streamRef = useRef<LiveFeed | null>(null);
  const scopeRef = useRef<{ venueSymbol: string; timeframe: Timeframe } | null>(null);
  /** The instrument the loader last resolved, readable from the stream callback. */
  const instrumentRef = useRef<Instrument | null>(null);
  /** Socket reconnect counter, so only a real reconnect triggers a gap patch. */
  const reconnectCountRef = useRef<number | null>(null);
  const pendingTickerRef = useRef<Extract<StreamEvent, { kind: "ticker" }> | null>(null);
  const pendingFormingCandleRef = useRef<Extract<StreamEvent, { kind: "candle" }> | null>(null);
  /** Bumped by the stream on (re)connect and by a finished REST backfill. */
  const [recoveries, setRecoveries] = useState(0);

  const patch = useCallback((updater: (current: MarketState) => MarketState) => {
    setSnapshot((current) => updater(current));
  }, []);

  /**
   * Read one snapshot and fold it into the page.
   *
   * Used both for the first paint (local SQLite data, shown before the venue is
   * asked for anything) and to close the gap after the local socket reconnects.
   * It is deliberately cheap: one request, one bar, no history refetch.
   */
  const runSnapshotLoad = useCallback(
    async (generation: number, key: string, target: Instrument, frame: Timeframe, uploaded: boolean, append: boolean): Promise<void> => {
      try {
        const payload = await fetchMarketSnapshot({ symbol: target.venueSymbol, interval: frame });
        if (generation !== generationRef.current) return;
        const ticker = toTicker(payload.ticker ?? {});
        const closedBar = toCandle(payload.candle);
        const forming = toCandle(payload.formingCandle);
        const at = payload.receivedTs ?? Date.now();
        patch((current) => {
          const merged = closedBar && !uploaded ? mergeCandle(current.candles, closedBar) : { candles: current.candles, changed: false };
          const nextTicker = ticker ?? current.ticker;
          return {
            ...current,
            candles: merged.candles,
            candlesFetchedAt: merged.changed ? at : current.candlesFetchedAt,
            ticker: nextTicker,
            tickerFetchedAt: ticker ? at : current.tickerFetchedAt,
            formingCandle: forming ?? current.formingCandle,
            feed: payload.connection ? { ...(current.feed ?? payload.connection), ...payload.connection } : current.feed,
            hasData: current.hasData || Boolean(closedBar),
            sources: {
              ...current.sources,
              // Provenance is per part: the bar may come from SQLite while the
              // quote inside the same frame came from the stream.
              candles: merged.changed && current.sources.candles === "none" ? "live" : current.sources.candles,
              ticker: nextTicker && current.sources.ticker === "none" ? "live" : current.sources.ticker,
            },
          };
        });
        // Always write the slice, even when this key has none yet: on a first
        // visit the snapshot resolves before the history fetch creates it, and a
        // live quote that is never cached is replaced by "none" on the next load.
        const slice = cacheRef.current[key] ?? {};
        cacheRef.current[key] = {
          ...slice,
          ticker: ticker ? { value: ticker, at: Date.now() } : slice.ticker,
          // The bar extends the cached series, or — when no series has been
          // fetched yet — becomes one, marked stale so the history still loads.
          candles: closedBar
            ? { value: mergeCandle(slice.candles?.value ?? [], closedBar).candles, at: slice.candles?.at ?? 0 }
            : slice.candles,
        };
        if (append && closedBar) {
          // A gap patch must not wait for the deferred reload: the reconnect is
          // exactly when the newest bar is most likely to be missing.
          const cached = cacheRef.current[key] ?? {};
          cacheRef.current[key] = {
            ...cached,
            candles: { value: mergeCandle(cached.candles?.value ?? [], closedBar).candles, at: Date.now() },
          };
        }
      } catch (reason: unknown) {
        if (generation !== generationRef.current) return;
        const message = describe(reason);
        if (reason instanceof GatewayError && reason.unreachable) setDegraded(true);
        patch((current) => ({ ...current, errors: { ...current.errors, ticker: current.errors.ticker ?? message } }));
      }
    },
    [patch],
  );

  /**
   * After a local socket reconnect, close the gap with one snapshot read.
   *
   * This patches the current view instead of reloading it, so the history
   * request already in flight for the same symbol is not thrown away.
   */
  const patchGap = useCallback(async () => {
    const scope = scopeRef.current;
    const target = instrumentRef.current;
    if (!scope || !target || scope.venueSymbol !== target.venueSymbol) return;
    if (uploadRef.current) return;
    await runSnapshotLoad(generationRef.current, keyOf(scope.timeframe, scope.venueSymbol), target, scope.timeframe, false, true);
  }, [runSnapshotLoad]);

  const applyFeed = useCallback(
    (feed: FeedStatus | null) => {
      if (!feed) return;
      patch((current) => ({ ...current, feed: { ...(current.feed ?? feed), ...feed } }));
    },
    [patch],
  );

  /** Apply the newest exchange deltas in one render-sized batch. */
  const flushMarketDeltas = useCallback(() => {
    const scope = scopeRef.current;
    const ticker = pendingTickerRef.current;
    const forming = pendingFormingCandleRef.current;
    pendingTickerRef.current = null;
    pendingFormingCandleRef.current = null;

    if (scope && ticker && ticker.symbol === scope.venueSymbol) {
      const at = Date.now();
      patch((current) => ({
        ...current,
        ticker: mergeTicker(current.ticker, ticker.patch),
        tickerFetchedAt: at,
        sources: { ...current.sources, ticker: uploadRef.current ? current.sources.ticker : "live" },
        errors: { ...current.errors, ticker: undefined },
      }));
      const key = keyOf(scope.timeframe, scope.venueSymbol);
      const slice = cacheRef.current[key] ?? {};
      cacheRef.current[key] = {
        ...slice,
        ticker: { value: mergeTicker(slice.ticker?.value ?? null, ticker.patch), at },
      };
    }

    if (
      scope &&
      forming &&
      forming.symbol === scope.venueSymbol &&
      forming.interval === scope.timeframe &&
      !forming.closed
    ) {
      patch((current) => ({
        ...current,
        liveBar: true,
        formingCandle: forming.candle,
      }));
    }
  }, [patch]);

  /* ---------------------------------------------------------------- index */
  useEffect(() => {
    let active = true;
    fetchInstrumentIndex()
      .then((payload) => {
        if (!active || !Array.isArray(payload?.instruments) || payload.instruments.length === 0) return;
        setIndex(payload);
        setIndexError(null);
        setInstrument((current) => current ?? payload.instruments[0]);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setIndexError(reason instanceof Error ? reason.message : "无法读取固定合约池");
        setDegraded(true);
      });
    return () => {
      active = false;
    };
  }, []);

  /* --------------------------------------------------------------- stream */
  useEffect(() => {
    const feed = openMarketStream({
      onEvent: (event: StreamEvent) => {
        const scope = scopeRef.current;
        switch (event.kind) {
          case "connection": {
            setStreamView((current) => ({
              open: event.state === "connected",
              state: event.state === "connected" ? "open" : event.state === "disconnected" ? "closed" : "connecting",
              backlog: feed.backlog,
              reconnects: event.reconnects,
            }));
            patch((current) => ({
              ...current,
              feed: current.feed
                ? {
                    ...current.feed,
                    streamState: event.state === "connected" ? "open" : event.state === "disconnected" ? "closed" : "connecting",
                    reconnectsLocal: event.reconnects,
                  }
                : current.feed,
            }));
            if (event.state !== "connected") break;
            if (reconnectCountRef.current === null) {
              // The first open is not a gap: the history fetch is already running.
              reconnectCountRef.current = event.reconnects;
              break;
            }
            if (event.reconnects === reconnectCountRef.current) break;
            reconnectCountRef.current = event.reconnects;
            // The socket was down for a while, so the newest closed bar may be
            // missing. The engine holds the authoritative bar, so the gap is
            // closed by re-reading the snapshot -- never by refetching the whole
            // history, which would burst requests at the venue on every flap.
            void patchGap();
            break;
          }
          case "backfill": {
            // The engine just reconciled a window over REST; its stored bars
            // changed, so the cached history is worth re-reading once.
            if (event.rows > 0) setRecoveries((value) => value + 1);
            break;
          }
          case "ticker": {
            if (!scope || event.symbol !== scope.venueSymbol) return;
            // Bybit may push faster than the screen needs to paint. Merge every
            // partial field so the next one-second flush shows the latest full
            // quote without dropping mark price, OI, funding or 24h totals.
            const pending = pendingTickerRef.current;
            pendingTickerRef.current = pending && pending.symbol === event.symbol
              ? { ...event, patch: { ...pending.patch, ...event.patch } }
              : event;
            break;
          }
          case "candle": {
            if (!scope || event.symbol !== scope.venueSymbol || event.interval !== scope.timeframe) return;
            if (!event.closed) {
              // Forming bars follow the same one-second paint cadence as price.
              // They remain display-only and never enter analysis or storage.
              pendingFormingCandleRef.current = event;
              return;
            }
            // Never let a queued unconfirmed update resurrect a bar after the
            // exchange has confirmed it closed.
            const pendingForming = pendingFormingCandleRef.current;
            if (
              pendingForming?.symbol === event.symbol &&
              pendingForming.interval === event.interval &&
              pendingForming.openTs === event.openTs
            ) {
              pendingFormingCandleRef.current = null;
            }
            const upload = uploadRef.current;
            if (upload) return;
            patch((current) => {
              const merged = mergeCandle(current.candles, event.candle);
              return {
                ...current,
                candles: merged.candles,
                liveBar: false,
                formingCandle: null,
                candlesFetchedAt: Date.now(),
                sources: { ...current.sources, candles: current.sources.candles === "demo" ? "live" : current.sources.candles },
                hasData: true,
              };
            });
            const key = keyOf(scope.timeframe, scope.venueSymbol);
            const slice = cacheRef.current[key] ?? {};
            cacheRef.current[key] = {
              ...slice,
              candles: { value: mergeCandle(slice.candles?.value ?? [], event.candle).candles, at: Date.now() },
            };
            break;
          }
          default:
            break;
        }
      },
    });
    streamRef.current = feed;
    return () => {
      feed.close();
      streamRef.current = null;
    };
  }, [patch, patchGap]);

  /* --------------------------------------------------------------- loader */
  const load = useCallback(
    async (target: Instrument, frame: Timeframe, options: { silent?: boolean } = {}) => {
      const generation = ++generationRef.current;
      const key = keyOf(frame, target.venueSymbol);
      const cached = cacheRef.current[key] ?? {};
      const now = Date.now();
      const upload = uploadRef.current;
      scopeRef.current = { venueSymbol: target.venueSymbol, timeframe: frame };
      instrumentRef.current = target;

      if (upload && (upload.venueSymbol !== target.venueSymbol || upload.timeframe !== frame)) {
        uploadRef.current = null;
        setUploadLabel(null);
      }
      const uploaded = upload && upload.venueSymbol === target.venueSymbol && upload.timeframe === frame;
      const candleSource: Source = uploaded ? "upload" : cached.candles && isFresh(cached.candles.at, CANDLE_TTL_MS, now) ? "live" : "none";

      patch((current) => ({
        ...current,
        instrument: target,
        timeframe: frame,
        candles: uploaded ? current.candles : (cached.candles?.value ?? []),
        ticker: cached.ticker?.value ?? null,
        resonance: cached.resonance?.value ?? null,
        candlesFetchedAt: cached.candles?.at ?? null,
        tickerFetchedAt: cached.ticker?.at ?? null,
        resonanceFetchedAt: cached.resonance?.at ?? null,
        formingCandle: null,
        liveBar: false,
        sources: {
          ...current.sources,
          candles: candleSource,
          ticker: cached.ticker ? "live" : "none",
          resonance: cached.resonance ? "live" : "none",
        },
        errors: {},
        loading: true,
        hasData: uploaded || Boolean(cached.candles),
      }));

      const live = (promise: Promise<void>) => promise.catch(() => undefined);
      const applies = () => generation === generationRef.current;

      // A locally restored snapshot decides the first paint, so the page never
      // waits for a venue round trip to show the last real quote.
      const restoreTask = live(runSnapshotLoad(generation, key, target, frame, Boolean(uploaded), false));

      const blockingTasks: Array<Promise<void>> = [restoreTask];

      // History: the snapshot carries one closed bar. The chart needs 300, and
      // this is the only path that still asks the venue for a window of bars.
      if (!uploaded && candleSource !== "live") {
        blockingTasks.push(
          live(
            fetchCandles(target.venueSymbol, frame, KLINE_BARS)
              .then((candles) => {
                const closed = closedCandles(candles, frame);
                if (!applies()) return;
                cacheRef.current[key] = { ...cacheRef.current[key], candles: { value: closed, at: Date.now() } };
                patch((current) => ({
                  ...current,
                  candles: closed,
                  candlesFetchedAt: Date.now(),
                  sources: { ...current.sources, candles: "live" },
                  errors: { ...current.errors, candles: undefined },
                  hasData: true,
                }));
              })
              .catch((reason: unknown) => {
                const message = describe(reason);
                if (!applies()) return;
                if (reason instanceof GatewayError && reason.unreachable) setDegraded(true);
                patch((current) => ({
                  ...current,
                  // Real history is never replaced by generated bars: a restored
                  // or cached series stays on screen and is reported as stale.
                  candles: current.candles.length ? current.candles : demoCandles(target, frame),
                  sources: { ...current.sources, candles: current.candles.length ? current.sources.candles : "demo" },
                  errors: { ...current.errors, candles: message },
                  hasData: true,
                }));
              }),
          ),
        );
      }

      // The quote comes from the restored snapshot above, or from the realtime
      // socket. There is no third REST read: a second request for the same data
      // would only add latency to the first paint.

      if (!isFresh(cached.resonance?.at ?? null, RESONANCE_TTL_MS, now)) {
        // Resonance is based on closed bars and changes far less often than the
        // ticker. Refresh it in the background so four-period analysis never
        // holds the K-line refresh spinner open.
        void live(
          fetchResonance(target.venueSymbol, KLINE_BARS)
            .then((result) => {
              if (!applies()) return;
              cacheRef.current[key] = { ...cacheRef.current[key], resonance: { value: result, at: Date.now() } };
              patch((current) => ({
                ...current,
                resonance: result,
                resonanceFetchedAt: Date.now(),
                sources: { ...current.sources, resonance: "live" },
                errors: { ...current.errors, resonance: undefined },
              }));
            })
            .catch((reason: unknown) => {
              const message = describe(reason);
              if (!applies()) return;
              patch((current) => ({
                ...current,
                resonance: current.resonance ?? demoResonance(target),
                sources: { ...current.sources, resonance: current.resonance ? current.sources.resonance : "demo" },
                errors: { ...current.errors, resonance: message },
              }));
            }),
        );
      }

      await Promise.all(blockingTasks);
      if (!applies()) return;
      patch((current) => ({ ...current, loading: false }));
      void options;
    },
    [patch],
  );

  useEffect(() => {
    if (!instrument) return;
    void load(instrument, timeframe);
  }, [instrument, timeframe, load]);

  /* A REST backfill may have moved the market without us. */
  useEffect(() => {
    if (recoveries === 0 || !instrument) return;
    markSeriesStale();
    void load(instrument, timeframe, { silent: true });
    // Only a new recovery signal should re-run this; the loader is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recoveries]);

  /* -------------------------------------------------------------- health */
  useEffect(() => {
    let active = true;
    const probe = () =>
      Promise.all([
        fetchHealth()
          .then((health) => {
            if (active) {
              setDegraded(!health.ok);
              patch((current) => ({ ...current, health: health }));
            }
          })
          .catch(() => {
            if (!active) return;
            setDegraded(true);
            patch((current) => ({ ...current, health: { ok: false, proxyConfigured: false } }));
          }),
        fetchFeedStatus()
          .then((feed) => {
            if (active) applyFeed(feed);
          })
          .catch(() => undefined),
      ]).then(() => undefined);
    void probe();
    const timer = window.setInterval(probe, HEALTH_INTERVAL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [applyFeed, patch]);

  /* ------------------------------------- manual refresh (no periodic poll) */
  const refresh = useCallback(
    (options: { silent?: boolean } = {}) => {
      if (!instrument) return;
      const key = keyOf(timeframe, instrument.venueSymbol);
      const slice = cacheRef.current[key] ?? {};
      if (!options.silent) {
        // A manual refresh always forces candles and ticker, while a valid
        // five-minute resonance result can be kept because it only uses closed
        // 15m/1h/4h/1d bars.
        cacheRef.current[key] = {
          resonance: slice.resonance && isFresh(slice.resonance.at, RESONANCE_TTL_MS, Date.now())
            ? slice.resonance
            : undefined,
        };
      } else {
        // A silent refresh is what a tab refocus or a reconnect uses. It must not
        // throw the series away: the loader restores from cache and only the
        // lapsed slices are fetched.
        cacheRef.current[key] = slice;
      }
      void load(instrument, timeframe, options);
    },
    [instrument, timeframe, load],
  );

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible" && instrument) refresh({ silent: true });
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [instrument, refresh]);

  /* Paint the newest venue deltas and keep "updated N seconds ago" honest. */
  useEffect(() => {
    const timer = window.setInterval(() => {
      flushMarketDeltas();
      setFreshnessTick((value) => value + 1);
      const feed = streamRef.current;
      if (feed) setStreamView((current) => (current.backlog === feed.backlog && current.open === feed.open ? current : { ...current, open: feed.open, backlog: feed.backlog }));
    }, MARKET_UI_REFRESH_MS);
    return () => {
      window.clearInterval(timer);
      pendingTickerRef.current = null;
      pendingFormingCandleRef.current = null;
    };
  }, [flushMarketDeltas]);

  /**
   * After the engine reconciled bars over REST, the cached series may be missing
   * more than the newest bar. Only the slice that actually changed is marked
   * stale, so the reload refetches one symbol's history instead of the pool's.
   *
   * The current symbol is read from the ref rather than from state: this runs
   * from a deferred effect, and a closure over `instrument` can still hold the
   * symbol the operator has already switched away from.
   */
  const markSeriesStale = useCallback(() => {
    const scope = scopeRef.current;
    if (!scope) return;
    const key = keyOf(scope.timeframe, scope.venueSymbol);
    const slice = cacheRef.current[key];
    if (slice?.candles) cacheRef.current[key] = { ...slice, candles: { ...slice.candles, at: 0 } };
  }, []);

  const applyUpload = useCallback(
    (candles: Candle[], label: string) => {
      if (!instrument) return;
      // Invalidate every request that started before the upload. Otherwise a
      // delayed live response can replace the file while the UI still says upload.
      generationRef.current += 1;
      uploadRef.current = { venueSymbol: instrument.venueSymbol, timeframe };
      setUploadLabel(label);
      patch((current) => ({
        ...current,
        candles,
        sources: { ...current.sources, candles: "upload" },
        errors: { ...current.errors, candles: undefined },
        candlesFetchedAt: Date.now(),
        hasData: true,
        loading: false,
      }));
    },
    [instrument, timeframe, patch],
  );

  const clearUpload = useCallback(() => {
    uploadRef.current = null;
    setUploadLabel(null);
    if (instrument) {
      cacheRef.current[keyOf(timeframe, instrument.venueSymbol)] = {};
      void load(instrument, timeframe);
    }
  }, [instrument, timeframe, load]);

  const select = useCallback((next: Instrument) => setInstrument(next), []);
  const setTimeframe = useCallback((next: Timeframe) => setTimeframeState(next), []);

  const state = useMemo<MarketState>(() => {
    const now = Date.now();
    const scoped = snapshot.instrument === instrument && snapshot.timeframe === timeframe;
    return {
      ...snapshot,
      candles: scoped ? snapshot.candles : [],
      status: deriveStatus({ state: snapshot, instrument, degraded, indexError, now }),
    };
  }, [snapshot, instrument, timeframe, degraded, indexError, freshnessTick]);

  return {
    state,
    index,
    indexError,
    degraded,
    select,
    setTimeframe,
    refresh,
    applyUpload,
    clearUpload,
    uploadLabel,
    barClosesAt: barClosesAt(state.candles, state.timeframe),
    stream: streamView,
  };
}

function describe(reason: unknown): string {
  if (reason instanceof GatewayError) return reason.message;
  if (reason instanceof Error) return reason.message;
  return "未知错误";
}

export const MARKET_TIMEFRAMES = TIMEFRAMES;
