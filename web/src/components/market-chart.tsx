import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
  type BusinessDay,
  type IChartApi,
  type ISeriesApi,
  type Logical,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { INTERVAL_MS, type Candle, type Timeframe } from "../data/market";
import { markThinBars } from "../lib/series";
import { formatCandleRange, formatPrice } from "../lib/format";
import { fibonacciAnchorLabel, fibonacciLevelLabelRight } from "../lib/fibonacci-layout";
import { CpaOverlay, CpaPhaseCard, CpaStatusLine } from "./cpa-chart-overlay";
import {
  buildCpaGeometry,
  cpaMarkerAt,
  cpaOverlayPlan,
  fetchCpaCatalog,
  fetchCpaPhases,
  simpleMovingAverage,
  shouldFetchCpa,
  type CpaCatalog,
  type CpaMarker,
  type CpaPhaseRecord,
  type CpaPhases,
} from "../services/cpa";

const INK = "#45e0d0";
const DOWN = "#fb6b67";
const THIN_INK = "rgba(69, 224, 208, 0.48)";
const THIN_DOWN = "rgba(251, 107, 103, 0.48)";
const INITIAL_VISIBLE_BARS = 160;
const CHART_HEIGHT = 460;
const FIB_SETTING_KEY = "quantdesk.chart.fibonacci.enabled";
const MA_SETTING_KEY = "quantdesk.chart.moving-averages";
const MA_ENABLED_SETTING_KEY = "quantdesk.chart.moving-averages.enabled";
type MaPeriod = 5 | 30 | 60;
const MA_LINES: ReadonlyArray<{ period: MaPeriod; color: string; className: string }> = [
  { period: 5, color: "#f6b94c", className: "ma-5" },
  { period: 30, color: "#79a9ff", className: "ma-30" },
  { period: 60, color: "#c184ff", className: "ma-60" },
];
const FIB_LEVELS = [
  { ratio: 0, label: "0", color: "#718581" },
  { ratio: 0.236, label: "0.236", color: "#6aaea7" },
  { ratio: 0.382, label: "0.382", color: "#59c6bb" },
  { ratio: 0.5, label: "0.5", color: "#f6b94c" },
  { ratio: 0.618, label: "0.618", color: "#45e0d0" },
  { ratio: 0.786, label: "0.786", color: "#6aaea7" },
  { ratio: 1, label: "1", color: "#718581" },
] as const;

interface FibonacciAnchor {
  time: number;
  price: number;
}

interface Props {
  candles: Candle[];
  timeframe: Timeframe;
  symbol?: string;
  /** The still-forming bar from the realtime stream; drawn but never analysed. */
  formingCandle?: Candle | null;
  /**
   * Whether this chart offers the CPA overlay at all. Off unless a caller asks for
   * it, and - like Fibonacci - only ever an addition: when the switch is off the
   * overlay is not rendered and no CPA request is made.
   */
  cpaEnabled?: boolean;
}

function timestamp(time: number): UTCTimestamp {
  return Math.floor(time / 1000) as UTCTimestamp;
}

function epochSeconds(time: Time): number {
  if (typeof time === "number") return time;
  const day = time as BusinessDay;
  return Date.UTC(day.year, day.month - 1, day.day) / 1000;
}

export function MarketChart({ candles, timeframe, symbol = "", formingCandle = null, cpaEnabled = false }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const maSeriesRef = useRef(new Map<MaPeriod, ISeriesApi<"Line">>());
  const candleMapRef = useRef(new Map<number, Candle>());
  const orderedRef = useRef<Candle[]>([]);
  const scopeRef = useRef("");
  const rangeBarCountRef = useRef(0);
  const [hover, setHover] = useState<Candle | null>(null);
  // Off by default and not persisted: an overlay that turns itself on would change
  // what a returning reader sees without them asking.
  const [cpaOn, setCpaOn] = useState(false);
  const [cpaCatalog, setCpaCatalog] = useState<CpaCatalog | null>(null);
  const [cpaPhases, setCpaPhases] = useState<CpaPhases | null>(null);
  const [cpaError, setCpaError] = useState("");
  const [cpaSelected, setCpaSelected] = useState<CpaPhaseRecord | null>(null);
  const [cpaLongSma, setCpaLongSma] = useState(false);
  const [maEnabled, setMaEnabled] = useState(() => {
    try {
      return window.localStorage.getItem(MA_ENABLED_SETTING_KEY) !== "false";
    } catch {
      return true;
    }
  });
  const [maVisible, setMaVisible] = useState<Record<MaPeriod, boolean>>(() => {
    const fallback = { 5: true, 30: true, 60: true };
    try {
      const stored = JSON.parse(window.localStorage.getItem(MA_SETTING_KEY) ?? "null") as Partial<Record<MaPeriod, boolean>> | null;
      return stored
        ? { 5: stored[5] !== false, 30: stored[30] !== false, 60: stored[60] !== false }
        : fallback;
    } catch {
      return fallback;
    }
  });
  const [fibEnabled, setFibEnabled] = useState(() => {
    try {
      return window.localStorage.getItem(FIB_SETTING_KEY) === "true";
    } catch {
      return false;
    }
  });
  const [fibSelecting, setFibSelecting] = useState(false);
  const [fibAnchors, setFibAnchors] = useState<FibonacciAnchor[]>([]);
  const [overlayVersion, setOverlayVersion] = useState(0);
  const fibEnabledRef = useRef(fibEnabled);
  const fibSelectingRef = useRef(false);
  const fibAnchorsRef = useRef<FibonacciAnchor[]>([]);

  const ordered = useMemo(() => {
    const unique = new Map<number, Candle>();
    for (const candle of candles) unique.set(candle.time, candle);
    return [...unique.values()].sort((left, right) => left.time - right.time);
  }, [candles]);
  const thin = useMemo(() => markThinBars(ordered), [ordered]);
  const movingAverages = useMemo(
    () => new Map(MA_LINES.map(({ period }) => [period, simpleMovingAverage(ordered, period)])),
    [ordered],
  );
  const hasCandles = ordered.length > 0;

  useEffect(() => {
    try {
      window.localStorage.setItem(MA_SETTING_KEY, JSON.stringify(maVisible));
      window.localStorage.setItem(MA_ENABLED_SETTING_KEY, String(maEnabled));
    } catch {
      // The moving averages remain usable when browser storage is unavailable.
    }
    for (const { period } of MA_LINES) {
      maSeriesRef.current.get(period)?.applyOptions({ visible: maEnabled && maVisible[period] });
    }
  }, [maEnabled, maVisible, hasCandles, timeframe]);

  useEffect(() => {
    fibEnabledRef.current = fibEnabled;
    try {
      window.localStorage.setItem(FIB_SETTING_KEY, String(fibEnabled));
    } catch {
      // The drawing tool still works when browser storage is unavailable.
    }
  }, [fibEnabled]);

  useEffect(() => {
    fibAnchorsRef.current = [];
    setFibAnchors([]);
    fibSelectingRef.current = fibEnabledRef.current;
    setFibSelecting(fibEnabledRef.current);
  }, [symbol, timeframe]);

  useEffect(() => {
    if (!cpaEnabled || cpaCatalog) return;
    let active = true;
    void fetchCpaCatalog()
      .then((catalog) => { if (active) setCpaCatalog(catalog); })
      .catch(() => { if (active) setCpaCatalog(null); });
    return () => { active = false; };
  }, [cpaEnabled, cpaCatalog]);

  // The phase series is fetched only while the overlay is on: an off switch must not
  // cost a request, let alone change the chart.
  useEffect(() => {
    if (!shouldFetchCpa({ cpaEnabled, cpaOn, symbol })) {
      setCpaPhases(null);
      setCpaError("");
      setCpaSelected(null);
      return;
    }
    let active = true;
    setCpaError("");
    void fetchCpaPhases(symbol, timeframe, { bars: Math.max(600, candles.length), limit: 600 })
      .then((payload) => { if (active) setCpaPhases(payload); })
      .catch((reason) => {
        if (!active) return;
        setCpaPhases(null);
        setCpaError(reason instanceof Error ? reason.message : "读取 CPA 阶段失败");
      });
    return () => { active = false; };
  }, [cpaEnabled, cpaOn, symbol, timeframe, candles.length]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const chart = createChart(container, {
      width: container.clientWidth,
      height: CHART_HEIGHT,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#718581",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 10,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "#172324", style: 1 },
        horzLines: { color: "#172324", style: 1 },
      },
      rightPriceScale: {
        borderColor: "#263334",
        scaleMargins: { top: 0.08, bottom: 0.24 },
      },
      timeScale: {
        borderColor: "#263334",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 4,
        barSpacing: 6,
        minBarSpacing: 2,
        lockVisibleTimeRangeOnResize: true,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#4d6462", width: 1, style: 2, labelBackgroundColor: "#183e3b" },
        horzLine: { color: "#4d6462", width: 1, style: 2, labelBackgroundColor: "#183e3b" },
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
      localization: {
        locale: "zh-CN",
        timeFormatter: (time: Time) => {
          const value = new Date(epochSeconds(time) * 1000);
          return value.toLocaleString("zh-CN", {
            timeZone: "Asia/Shanghai",
            month: "2-digit",
            day: "2-digit",
            hour: timeframe === "1d" ? undefined : "2-digit",
            minute: timeframe === "1d" ? undefined : "2-digit",
            hour12: false,
          });
        },
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: INK,
      downColor: DOWN,
      borderUpColor: INK,
      borderDownColor: DOWN,
      wickUpColor: INK,
      wickDownColor: DOWN,
      priceLineVisible: false,
      lastValueVisible: true,
    });
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: "",
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat: { type: "volume" },
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    maSeriesRef.current = new Map(
      MA_LINES.map(({ period, color }) => [
        period,
        chart.addSeries(LineSeries, {
          color,
          lineWidth: period === 5 ? 2 : 1,
          visible: maEnabled && maVisible[period],
          crosshairMarkerVisible: false,
          priceLineVisible: false,
          lastValueVisible: false,
          title: `MA${period}`,
        }),
      ]),
    );

    const onCrosshairMove = (param: { time?: Time }) => {
      if (param.time === undefined) {
        setHover(null);
        return;
      }
      setHover(candleMapRef.current.get(epochSeconds(param.time)) ?? null);
    };
    chart.subscribeCrosshairMove(onCrosshairMove);

    const refreshOverlay = () => setOverlayVersion((current) => current + 1);
    chart.timeScale().subscribeVisibleLogicalRangeChange(refreshOverlay);

    const resize = () => {
      if (!container.clientWidth) return;
      chart.applyOptions({ width: container.clientWidth, height: CHART_HEIGHT });
      refreshOverlay();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);

    chartRef.current = chart;
    candleSeriesRef.current = candleSeries;
    volumeSeriesRef.current = volumeSeries;

    return () => {
      observer.disconnect();
      chart.unsubscribeCrosshairMove(onCrosshairMove);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(refreshOverlay);
      chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      maSeriesRef.current.clear();
    };
  }, [hasCandles, timeframe]);

  useEffect(() => {
    const chart = chartRef.current;
    const candleSeries = candleSeriesRef.current;
    const volumeSeries = volumeSeriesRef.current;
    if (!chart || !candleSeries || !volumeSeries) return;

    orderedRef.current = ordered;
    candleMapRef.current = new Map(ordered.map((candle) => [timestamp(candle.time) as number, candle]));
    candleSeries.setData(
      ordered.map((candle, index) => ({
        time: timestamp(candle.time),
        open: candle.open,
        high: candle.high,
        low: candle.low,
        close: candle.close,
        color: thin[index] ? (candle.close >= candle.open ? THIN_INK : THIN_DOWN) : undefined,
        borderColor: thin[index] ? (candle.close >= candle.open ? THIN_INK : THIN_DOWN) : undefined,
        wickColor: thin[index] ? (candle.close >= candle.open ? THIN_INK : THIN_DOWN) : undefined,
      })),
    );
    volumeSeries.setData(
      ordered.map((candle, index) => ({
        time: timestamp(candle.time),
        value: candle.volume,
        color: thin[index]
          ? candle.close >= candle.open ? "rgba(69, 224, 208, 0.16)" : "rgba(251, 107, 103, 0.16)"
          : candle.close >= candle.open ? "rgba(69, 224, 208, 0.32)" : "rgba(251, 107, 103, 0.32)",
      })),
    );
    for (const { period } of MA_LINES) {
      maSeriesRef.current.get(period)?.setData(
        (movingAverages.get(period) ?? []).map((point) => ({
          time: timestamp(point.time),
          value: point.value,
        })),
      );
    }
    setOverlayVersion((current) => current + 1);

    const scope = `${symbol}:${timeframe}`;
    const scopeChanged = scopeRef.current !== scope || !scopeRef.current;
    // The local snapshot normally paints one closed bar before the 300-bar REST
    // window arrives.  If the view is initialised from that first bar only,
    // lightweight-charts preserves a five-bar viewport while the full series is
    // appended and every recent analytical overlay ends up off screen.  Reframe
    // once when history arrives as a material jump.  Ordinary one-bar realtime
    // additions do not satisfy this condition, so a reader's manual zoom survives.
    const historyArrived = !scopeChanged && ordered.length > rangeBarCountRef.current + 10;
    if (scopeChanged || historyArrived) {
      scopeRef.current = scope;
      const to = ordered.length + 3;
      const from = Math.max(-1, ordered.length - INITIAL_VISIBLE_BARS);
      chart.timeScale().setVisibleLogicalRange({ from: from as Logical, to: to as Logical });
      setHover(null);
    }
    rangeBarCountRef.current = ordered.length;

    (window as unknown as Record<string, unknown>).__qdChart = {
      timeframe,
      intervalMs: INTERVAL_MS[timeframe],
      bars: ordered,
      originalBarCount: ordered.length,
      movingAverages: Object.fromEntries(MA_LINES.map(({ period }) => [period, movingAverages.get(period) ?? []])),
      coordinateForIndex: (index: number) => chart.timeScale().logicalToCoordinate(index as Logical),
      visibleRange: () => chart.timeScale().getVisibleLogicalRange(),
    };
  }, [movingAverages, ordered, symbol, thin, timeframe]);

  /**
   * The venue's forming bar, streamed over the local socket. It is drawn only
   * here: `candles` stays the closed series that analysis and overlays read.
   */
  useEffect(() => {
    const candleSeries = candleSeriesRef.current;
    const volumeSeries = volumeSeriesRef.current;
    if (!candleSeries || !volumeSeries || !formingCandle) return;
    const last = orderedRef.current.at(-1);
    if (last && formingCandle.time <= last.time) return;
    candleSeries.update({
      time: timestamp(formingCandle.time),
      open: formingCandle.open,
      high: formingCandle.high,
      low: formingCandle.low,
      close: formingCandle.close,
      color: formingCandle.close >= formingCandle.open ? "rgba(69, 224, 208, 0.55)" : "rgba(251, 107, 103, 0.55)",
      borderColor: formingCandle.close >= formingCandle.open ? THIN_INK : THIN_DOWN,
      wickColor: formingCandle.close >= formingCandle.open ? THIN_INK : THIN_DOWN,
    });
    volumeSeries.update({
      time: timestamp(formingCandle.time),
      value: formingCandle.volume,
      color: formingCandle.close >= formingCandle.open ? "rgba(69, 224, 208, 0.18)" : "rgba(251, 107, 103, 0.18)",
    });
  }, [formingCandle]);

  const zoom = (factor: number) => {
    const scale = chartRef.current?.timeScale();
    const range = scale?.getVisibleLogicalRange();
    if (!scale || !range) return;
    const center = (range.from + range.to) / 2;
    const span = Math.max(20, Math.min(ordered.length + 12, (range.to - range.from) * factor));
    scale.setVisibleLogicalRange({
      from: (center - span / 2) as Logical,
      to: (center + span / 2) as Logical,
    });
  };

  const toggleFibonacci = () => {
    setFibEnabled((current) => {
      const next = !current;
      fibEnabledRef.current = next;
      const selecting = next && fibAnchorsRef.current.length < 2;
      fibSelectingRef.current = selecting;
      setFibSelecting(selecting);
      return next;
    });
  };

  const restartFibonacci = () => {
    fibAnchorsRef.current = [];
    setFibAnchors([]);
    fibSelectingRef.current = true;
    setFibSelecting(true);
  };

  const clearFibonacci = () => {
    fibAnchorsRef.current = [];
    setFibAnchors([]);
    fibSelectingRef.current = false;
    setFibSelecting(false);
  };

  const selectFibonacciPoint = (event: ReactMouseEvent<HTMLDivElement>) => {
    const chart = chartRef.current;
    const series = candleSeriesRef.current;
    const container = containerRef.current;
    if (!chart || !series || !container || !fibEnabledRef.current || !fibSelectingRef.current) return;
    const bounds = container.getBoundingClientRect();
    const x = event.clientX - bounds.left;
    const y = event.clientY - bounds.top;
    if (x < 0 || x > chart.timeScale().width() || y < 0 || y > CHART_HEIGHT) return;
    const selectedTime = chart.timeScale().coordinateToTime(x);
    const logical = chart.timeScale().coordinateToLogical(x);
    const byTime = selectedTime === null ? undefined : candleMapRef.current.get(epochSeconds(selectedTime));
    const logicalIndex = logical === null ? -1 : Math.round(Number(logical));
    const candle = byTime ?? orderedRef.current[logicalIndex];
    if (!candle) return;
    const highY = series.priceToCoordinate(candle.high);
    const lowY = series.priceToCoordinate(candle.low);
    if (highY === null || lowY === null) return;
    const anchor = {
      time: candle.time,
      price: Math.abs(y - Number(highY)) <= Math.abs(y - Number(lowY)) ? candle.high : candle.low,
    };
    setFibAnchors((current) => {
      const next = current.length === 0 ? [anchor] : [current[0], anchor];
      fibAnchorsRef.current = next;
      if (next.length === 2) {
        fibSelectingRef.current = false;
        setFibSelecting(false);
      }
      return next;
    });
  };

  const active = hover;
  const activeIndex = active ? ordered.findIndex((item) => item.time === active.time) : -1;
  const first = ordered[0];
  const last = ordered.at(-1);
  const maReadoutTime = active?.time ?? last?.time;
  const maReadout = new Map(
    MA_LINES.map(({ period }) => [
      period,
      (movingAverages.get(period) ?? []).find((point) => point.time === maReadoutTime)?.value ?? null,
    ]),
  );
  const fibonacci = buildFibonacciGeometry(chartRef.current, candleSeriesRef.current, containerRef.current, fibAnchors, overlayVersion);

  /**
   * The CPA plan: `draw` is false whenever the switch is off, the series is missing
   * or the engine reported an insufficient sample, and the overlay renders nothing in
   * that case. Nothing here reads or writes the Fibonacci anchors.
   */
  const cpaPlan = cpaOverlayPlan({
    enabled: cpaEnabled && cpaOn,
    phases: cpaPhases,
    interval: timeframe,
    catalog: cpaCatalog,
    showLongSma: cpaLongSma && timeframe === "1d",
    candles: ordered.map((candle) => ({
      time: candle.time, close: candle.close, high: candle.high, low: candle.low,
    })),
  });
  const cpaGeometry = useMemo(
    () =>
      buildCpaGeometry(cpaPlan, {
        x: (time) => {
          const chart = chartRef.current;
          if (!chart) return null;
          const coordinate = chart.timeScale().timeToCoordinate(timestamp(time));
          return coordinate === null ? null : Number(coordinate);
        },
        y: (price) => {
          const series = candleSeriesRef.current;
          if (!series) return null;
          const coordinate = series.priceToCoordinate(price);
          return coordinate === null ? null : Number(coordinate);
        },
        width: containerRef.current?.clientWidth ?? 0,
        height: CHART_HEIGHT,
        // The phase series can be longer than the candles on screen; projecting a bar
        // the chart does not hold is what produced markers at x = -2000.
        timeRange: ordered.length
          ? { from: ordered[0].time, to: ordered[ordered.length - 1].time }
          : undefined,
      }),
    // `overlayVersion` is bumped by the chart on pan/zoom/resize, exactly as the
    // Fibonacci overlay uses it, so both follow the viewport the same way.
    [cpaPlan, overlayVersion],
  );

  const selectCpaMarker = (marker: CpaMarker) => {
    const record = (cpaPhases?.records ?? []).find((item) => item.time === marker.time);
    setCpaSelected(record ?? null);
  };

  const toggleCpa = () => {
    setCpaOn((current) => {
      const next = !current;
      if (!next) {
        setCpaPhases(null);
        setCpaSelected(null);
      }
      return next;
    });
  };

  /**
   * A click anywhere on the chart opens the nearest phase card when it is close to a
   * marker. The markers themselves carry their own handler; this is the fallback for a
   * click that lands a few pixels off, and it is inert while the overlay is off.
   */
  const onCpaClick = (event: ReactMouseEvent<HTMLDivElement>) => {
    if (!cpaPlan.draw) return;
    const container = containerRef.current;
    if (!container) return;
    const bounds = container.getBoundingClientRect();
    const marker = cpaMarkerAt(cpaGeometry.anchors, event.clientX - bounds.left, event.clientY - bounds.top);
    if (marker) setCpaSelected((cpaPhases?.records ?? []).find((item) => item.time === marker.time) ?? null);
  };

  if (!ordered.length) return <div className="chart-empty">等待行情数据…</div>;

  return (
    <div className="chart-shell" onClick={cpaEnabled && cpaOn ? onCpaClick : undefined}>
      <div className="chart-toolbar" aria-label="K线图控制">
        <span>原始K线 {ordered.length}</span>
        <button type="button" onClick={() => zoom(1.35)} aria-label="缩小K线图">−</button>
        <button type="button" onClick={() => zoom(0.74)} aria-label="放大K线图">＋</button>
        <button type="button" onClick={() => chartRef.current?.timeScale().scrollToRealTime()}>回到最新</button>
        <button
          type="button"
          className={maEnabled ? "ma-toggle active" : "ma-toggle"}
          aria-pressed={maEnabled}
          aria-label={`${maEnabled ? "关闭" : "开启"} MA5、MA30、MA60 均线`}
          title="同时显示或隐藏 MA5、MA30、MA60；各周期的独立选择会保留"
          onClick={(event) => {
            event.stopPropagation();
            setMaEnabled((current) => !current);
          }}
        >
          MA {maEnabled ? "开" : "关"}
        </button>
        <button type="button" className={fibEnabled ? "fib-toggle active" : "fib-toggle"} aria-pressed={fibEnabled} onClick={toggleFibonacci}>
          斐波那契 {fibEnabled ? "开" : "关"}
        </button>
        {fibEnabled && <button type="button" onClick={restartFibonacci}>重选</button>}
        {fibEnabled && fibAnchors.length > 0 && <button type="button" onClick={clearFibonacci}>清除</button>}
        {cpaEnabled && (
          <>
            <button
              type="button"
              className={cpaOn ? "cpa-toggle active" : "cpa-toggle"}
              aria-pressed={cpaOn}
              onClick={toggleCpa}
              title="显示 CPA 价格周期阶段（默认关闭）"
            >
              CPA 周期 {cpaOn ? "开" : "关"}
            </button>
            {cpaOn && timeframe === "1d" && (
              <label className="cpa-sma-toggle">
                <input
                  type="checkbox"
                  checked={cpaLongSma}
                  onChange={(event) => setCpaLongSma(event.target.checked)}
                />
                SMA50/200
              </label>
            )}
          </>
        )}
      </div>
      {maEnabled && <div className="ma-legend" aria-label="移动平均线">
        {MA_LINES.map(({ period, className }) => {
          const value = maReadout.get(period) ?? null;
          return (
            <button
              key={period}
              type="button"
              className={`${className}${maVisible[period] ? " active" : ""}`}
              aria-pressed={maVisible[period]}
              aria-label={`${maVisible[period] ? "隐藏" : "显示"} MA${period} 均线`}
              onClick={(event) => {
                event.stopPropagation();
                setMaVisible((current) => ({ ...current, [period]: !current[period] }));
              }}
            >
              <i aria-hidden="true" />
              <span>MA{period}</span>
              <strong>{maVisible[period] && value !== null ? formatPrice(value) : "—"}</strong>
            </button>
          );
        })}
      </div>}
      <div
        ref={containerRef}
        className="tradingview-chart"
        role="img"
        aria-label={`K线图 ${timeframe}，${ordered.length} 根原始已收盘K线，区间 ${formatCandleRange(first.time, timeframe, ordered[1]?.time)} 至 ${formatCandleRange(last!.time, timeframe)}，最新收盘 ${formatPrice(last!.close)}，均线 ${maEnabled ? MA_LINES.filter(({ period }) => maVisible[period]).map(({ period }) => `MA${period}`).join("、") || "全部关闭" : "全部关闭"}`}
      />
      {fibEnabled && fibSelecting && <div className="fib-hit-area" aria-hidden="true" onClick={selectFibonacciPoint} />}
      {fibEnabled && fibonacci && (
        <svg className="fib-overlay" viewBox={`0 0 ${fibonacci.width} ${CHART_HEIGHT}`} preserveAspectRatio="none" aria-hidden="true">
          {fibonacci.levels.map((level) => (
            <g key={level.label}>
              <line className="fib-level" x1={level.x1} x2={level.x2} y1={level.y} y2={level.y} stroke={level.color} />
              <text className="fib-level-label" x={level.labelX} y={Math.max(12, level.y - 5)} textAnchor="end" fill={level.color}>{level.label} · {formatPrice(level.price)}</text>
            </g>
          ))}
          {fibonacci.anchors.length === 2 && <line className="fib-guide" x1={fibonacci.anchors[0].x} y1={fibonacci.anchors[0].y} x2={fibonacci.anchors[1].x} y2={fibonacci.anchors[1].y} />}
          {fibonacci.anchors.map((anchor, index) => (
            <g key={`${anchor.x}-${anchor.y}`}>
              <circle cx={anchor.x} cy={anchor.y} r="4" />
              <text className="fib-anchor-label" x={anchor.labelX} y={anchor.labelY} textAnchor={anchor.textAnchor}>{index === 0 ? "A" : "B"} · {formatPrice(anchor.price)}</text>
            </g>
          ))}
        </svg>
      )}
      {fibEnabled && fibSelecting && (
        <div className="fib-instruction" role="status">
          {fibAnchors.length === 0 ? "在价格图中点击一根K线，吸附高点或低点作为 A" : "再点击一根K线，吸附高点或低点作为 B"}
        </div>
      )}
      {/*
        The CPA layer is a sibling of the Fibonacci overlay, never a replacement: the
        two can be on at once, and with the switch off this block renders nothing.
      */}
      {cpaEnabled && cpaOn && (
        <div className="cpa-layer">
          <CpaOverlay
            plan={cpaPlan}
            geometry={cpaGeometry}
            width={containerRef.current?.clientWidth ?? 0}
            height={CHART_HEIGHT}
            onSelect={selectCpaMarker}
          />
          {cpaSelected && (
            <CpaPhaseCard
              record={cpaSelected}
              catalog={cpaCatalog}
              dataVersion={cpaPhases?.dataVersion ?? ""}
              higherIntervals={
                cpaPhases?.higherIntervals ?? { management: "", background: "" }
              }
              onClose={() => setCpaSelected(null)}
            />
          )}
        </div>
      )}
      {cpaEnabled && (cpaOn || cpaError) && <CpaStatusLine plan={cpaPlan} error={cpaError} />}
      <div className="chart-readout" aria-live="polite">
        {active ? (
          <>
            <span className="readout-time">{formatCandleRange(active.time, timeframe, ordered[activeIndex + 1]?.time)}</span>
            <span className="readout-high">最高 {formatPrice(active.high)}</span>
            <span className="readout-low">最低 {formatPrice(active.low)}</span>
            <span>开 {formatPrice(active.open)}</span>
            <span>收 {formatPrice(active.close)}</span>
            <span>量 {active.volume.toLocaleString("en-US", { maximumFractionDigits: 0 })}</span>
            {thin[activeIndex] && <em>休市空 bar，量能票在引擎中停用</em>}
          </>
        ) : (
          <>
            <span className="readout-time">{formatCandleRange(first.time, timeframe, ordered[1]?.time)} → {formatCandleRange(last!.time, timeframe)}</span>
            <span>每根图形对应 1 根 {timeframe} K线</span>
            <em>拖动平移 · 滚轮或双指缩放 · 十字光标查看 OHLCV</em>
          </>
        )}
      </div>
      <a className="chart-attribution" href="https://www.tradingview.com/" target="_blank" rel="noreferrer">
        Lightweight Charts™ by TradingView
      </a>
    </div>
  );
}

function buildFibonacciGeometry(
  chart: IChartApi | null,
  series: ISeriesApi<"Candlestick"> | null,
  container: HTMLDivElement | null,
  anchors: FibonacciAnchor[],
  _version: number,
) {
  if (!chart || !series || !container || anchors.length === 0) return null;
  const width = container.clientWidth;
  const coordinates = anchors.map((anchor) => ({
    x: chart.timeScale().timeToCoordinate(timestamp(anchor.time)),
    y: series.priceToCoordinate(anchor.price),
    price: anchor.price,
  }));
  if (coordinates.some((point) => point.x === null || point.y === null)) return null;
  const projected = (coordinates as Array<{ x: number; y: number; price: number }>).map((point) => ({
    ...point,
    ...fibonacciAnchorLabel(point.x, point.y, width, CHART_HEIGHT),
  }));
  if (anchors.length < 2) return { width, anchors: projected, levels: [] };
  const start = anchors[0];
  const end = anchors[1];
  const canvasRight = Math.max(48, width - 8);
  const labelRight = fibonacciLevelLabelRight(width);
  const rawLineStart = Math.max(0, Math.min(projected[0].x, projected[1].x));
  const lineStart = Math.min(rawLineStart, Math.max(0, canvasRight - 24));
  const lineEnd = Math.min(canvasRight, Math.max(lineStart + 24, labelRight));
  const levels = FIB_LEVELS.map((level) => {
    const price = start.price + (end.price - start.price) * level.ratio;
    return {
      ...level,
      price,
      x1: lineStart,
      x2: lineEnd,
      labelX: Math.min(labelRight, lineEnd - 5),
      y: Number(series.priceToCoordinate(price)),
    };
  }).filter((level) => Number.isFinite(level.y));
  return { width, anchors: projected, levels };
}
