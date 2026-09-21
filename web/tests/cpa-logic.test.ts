/**
 * CPA front-end rules, without a DOM.
 *
 * The four things a page must not get wrong are decided by the pure functions below,
 * so they are testable here: a candidate is never a signal, an insufficient sample
 * draws nothing, the parameter defaults come from the contract class and interval the
 * engine published, and the simplified-position notice is always available.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  FALLBACK_PHASE_LABELS,
  FALLBACK_SIMPLE_POSITION_NOTICE,
  assetClassOf,
  buildCpaGeometry,
  cpaAttachment,
  cpaEvidenceCard,
  cpaMarkerAt,
  cpaOverlayPlan,
  cpaResultView,
  cpaSignalKind,
  cpaSignalOf,
  higherTimeframeComparison,
  isObservationPhase,
  parameterDefaults,
  parameterRows,
  phaseDistribution,
  phaseLabel,
  simpleMovingAverage,
  simplePositionNotice,
  statusLabel,
  tradesByEntryPhase,
  warmupState,
  type CpaCatalog,
  type CpaPhaseRecord,
  type CpaPhases,
} from "../src/services/cpa.ts";

/* --------------------------------------------------------------- fixtures */

function record(overrides: Partial<CpaPhaseRecord> = {}): CpaPhaseRecord {
  return {
    time: 1_700_000_000_000,
    phase: "wedge_pop",
    status: "confirmed",
    direction: "bullish",
    confidence: 0.8,
    pivotPrice: 105.5,
    invalidationPrice: 98.25,
    setupLow: 99,
    ema10: 103,
    ema20: 101,
    distanceAtr: 1.2,
    volumeRatio: 1.6,
    contractionScore: 0.42,
    atr: 2.5,
    cycle: "upside",
    higherTimeframe: { interval: "4h", phase: "ema_crossback", trend: "bullish", closedAt: 1, available: true, reason: "" },
    backgroundTimeframe: { interval: "1d", phase: "base_n_break", trend: "bullish", closedAt: 1, available: true, reason: "" },
    reasons: ["收盘价突破枢轴 105.50", "成交量 1.60 倍于均量"],
    warnings: [],
    checks: { pivotBreak: true },
    parameterVersion: "cpa-qd/1.0.0",
    ...overrides,
  };
}

function phases(records: CpaPhaseRecord[], overrides: Partial<CpaPhases> = {}): CpaPhases {
  return {
    symbol: "BTCUSDT",
    displaySymbol: "BTC",
    interval: "1h",
    productType: "crypto",
    snapshotHash: "snap-1",
    dataVersion: "data-abc123",
    parameterVersion: "cpa-qd/1.0.0",
    parameters: { sideMode: "long_only" },
    higherIntervals: { management: "4h", background: "1d" },
    bars: 600,
    insufficient: false,
    insufficientReason: "",
    counts: {},
    current: records.at(-1) ?? null,
    records,
    warnings: [],
    attribution: "概念来源：Oliver Kell",
    ...overrides,
  };
}

const CATALOG: CpaCatalog = {
  id: "cpa_cycle",
  name: "Cycle of Price Action — QuantDesk 规则化适配版",
  parameterVersion: "cpa-qd/1.0.0",
  simplePositionNotice: "当前回测尚未模拟 CPA 分批建仓与分批减仓，收益结果属于单仓位简化版本。",
  attribution: "概念来源：Oliver Kell 公开描述的 Cycle of Price Action；阈值为 QuantDesk 研究参数。",
  observationPhases: ["reversal_extension", "exhaustion_extension"],
  higherTimeframeMap: { "1h": ["4h", "1d"] },
  higherTimeframeNotice: "低周期只读取当时已经收盘的高周期K线。",
  phases: [
    { id: "reversal_extension", label: "反转延伸（Reversal Extension）", observation: true },
    { id: "wedge_pop", label: "楔形突破（Wedge Pop）", observation: false },
    { id: "exhaustion_extension", label: "延伸衰竭（Exhaustion Extension）", observation: true },
    { id: "none", label: "无阶段", observation: false },
  ],
  phaseLabels: FALLBACK_PHASE_LABELS,
  parameters: [
    { key: "emaFast", label: "快线 EMA 周期", type: "integer", default: 10, minimum: 2, maximum: 100, unit: "根", help: "" },
    { key: "minBars", label: "最少K线数", type: "integer", default: 60, minimum: 30, maximum: 1000, unit: "根", help: "" },
    { key: "sideMode", label: "方向模式", type: "string", default: "long_only", minimum: null, maximum: null, unit: "", help: "", options: ["long_only", "symmetric"] },
    { key: "entryStages", label: "入场阶段", type: "string", default: ["wedge_pop", "ema_crossback", "base_n_break"], minimum: null, maximum: null, unit: "", help: "", options: ["wedge_pop", "ema_crossback", "base_n_break"] },
    { key: "exitOnExhaustion", label: "衰竭即退出", type: "boolean", default: true, minimum: null, maximum: null, unit: "", help: "" },
  ],
  defaults: {
    stock: { "1h": { emaFast: 10, minBars: 60, sideMode: "long_only", entryStages: ["wedge_pop"], exitOnExhaustion: true } },
    etf: { "1h": { emaFast: 10, minBars: 60, sideMode: "long_only", entryStages: ["wedge_pop"], exitOnExhaustion: true, extensionAtr: 1.8 } },
    crypto: { "1d": { emaFast: 10, minBars: 60, sideMode: "symmetric", entryStages: ["base_n_break"], exitOnExhaustion: false } },
  },
  optionalFactorLayer: { trend_strength: "vibe.trend_strength.24" },
  supportedIntervals: ["15m", "1h", "4h", "1d", "1w"],
};

// A projector consistent with its own canvas: the fixture's bar lands inside 0..800,
// which is what a real `timeToCoordinate` does for a bar the chart actually holds.
const PROJECTOR = {
  x: (time: number) => (time - 1_699_999_800_000) / 1000,
  y: (price: number) => 400 - (price - 90) * 2,
  width: 800,
  height: 460,
  timeRange: { from: 1_699_999_800_000, to: 1_700_001_000_000 },
};

/* ------------------------------------------------------------ 1. 中文名映射 */

describe("phase labels", () => {
  it("maps every engine phase id to Chinese, never rendering the raw id", () => {
    for (const id of [
      "none", "reversal_extension", "wedge_pop", "ema_crossback", "base_n_break",
      "exhaustion_extension", "wedge_drop", "downside_ema_crossback", "downside_base_n_break",
    ]) {
      const label = phaseLabel(id);
      assert.notEqual(label, id, `${id} 必须渲染成中文`);
      assert.match(label, /[\u4e00-\u9fa5]/, `${id} 的中文名里应有汉字`);
    }
  });

  it("prefers the catalogue's own labels and falls back to the built-in map", () => {
    const catalog = { ...CATALOG, phaseLabels: { wedge_pop: "楔形突破（自定义）" } } as CpaCatalog;
    assert.equal(phaseLabel("wedge_pop", catalog), "楔形突破（自定义）");
    assert.equal(phaseLabel("base_n_break", catalog), FALLBACK_PHASE_LABELS.base_n_break);
  });

  it("has a short label for chart bands and the full one for tables", () => {
    assert.equal(phaseLabel("downside_ema_crossback", CATALOG, true), "下行回踩");
    assert.match(phaseLabel("downside_ema_crossback", CATALOG), /Downside EMA Crossback/);
  });

  it("labels a status in Chinese", () => {
    assert.equal(statusLabel("confirmed"), "已确认");
    assert.equal(statusLabel("candidate"), "候选");
    assert.equal(statusLabel("none"), "无");
  });
});

/* ------------------------------------------- 2. candidate 不得当作入场信号 */

describe("candidate is not an entry signal", () => {
  it("never makes a candidate actionable, whatever its direction", () => {
    for (const phase of ["wedge_pop", "ema_crossback", "base_n_break", "wedge_drop", "downside_base_n_break"]) {
      const signal = cpaSignalOf({ phase: phase as CpaPhaseRecord["phase"], status: "candidate", direction: "bullish" });
      assert.equal(signal.actionable, false, `${phase} 的候选态不能可交易`);
      assert.equal(signal.kind, "candidate");
      assert.equal(signal.side, null);
      assert.match(signal.label, /候选/);
    }
  });

  it("treats the two observation phases as observations even when confirmed", () => {
    for (const phase of ["reversal_extension", "exhaustion_extension"]) {
      const signal = cpaSignalOf({ phase: phase as CpaPhaseRecord["phase"], status: "confirmed", direction: "bullish" }, CATALOG);
      assert.equal(signal.kind, "observation");
      assert.equal(signal.actionable, false, `${phase} 是观察阶段，不能开仓`);
      assert.match(signal.label, /观察/);
    }
  });

  it("only a confirmed non-observation phase is actionable, and it names a side", () => {
    const long = cpaSignalOf({ phase: "wedge_pop", status: "confirmed", direction: "bullish" }, CATALOG);
    assert.equal(long.actionable, true);
    assert.equal(long.side, "long");
    const short = cpaSignalOf({ phase: "wedge_drop", status: "confirmed", direction: "bearish" }, CATALOG);
    assert.equal(short.actionable, true);
    assert.equal(short.side, "short");
  });

  it("classifies the kind from the record, before any rendering decision", () => {
    assert.equal(cpaSignalKind({ phase: "none", status: "none" }, CATALOG), "none");
    assert.equal(cpaSignalKind({ phase: "wedge_pop", status: "candidate" }, CATALOG), "candidate");
    assert.equal(cpaSignalKind({ phase: "wedge_pop", status: "confirmed" }, CATALOG), "confirmed");
    assert.equal(cpaSignalKind({ phase: "reversal_extension", status: "confirmed" }, CATALOG), "observation");
  });

  it("keeps candidates out of the plan's entry markers but keeps them visible", () => {
    const plan = cpaOverlayPlan({
      enabled: true,
      interval: "1h",
      catalog: CATALOG,
      phases: phases([record({ status: "candidate", phase: "wedge_pop" })]),
    });
    const confirmed = plan.markers.filter((marker) => marker.kind === "confirmed");
    assert.equal(confirmed.length, 0, "候选不能产生已确认标记");
    assert.equal(plan.markers.length, 1, "但候选本身要在图上可见（虚线/浅色）");
    assert.equal(plan.markers[0].kind, "candidate");
  });
});

/* ------------------------------------------------- 3. insufficient 不画阶段 */

describe("insufficient sample draws nothing", () => {
  it("returns a plan with no bands, levels, markers or lines", () => {
    const plan = cpaOverlayPlan({
      enabled: true,
      interval: "1h",
      catalog: CATALOG,
      phases: phases([record()], { insufficient: true, insufficientReason: "只有 42 根K线", records: [] }),
    });
    assert.equal(plan.draw, false);
    assert.equal(plan.insufficient, true);
    assert.deepEqual(plan.bands, []);
    assert.deepEqual(plan.levels, []);
    assert.deepEqual(plan.markers, []);
    assert.deepEqual(plan.lines, []);
    assert.match(plan.reason, /样本不足/);
    assert.match(plan.reason, /42/);
  });

  it("does not invent a phase when the engine returned no records", () => {
    const plan = cpaOverlayPlan({
      enabled: true,
      interval: "1h",
      catalog: CATALOG,
      phases: phases([], { records: [], current: null, bars: 600 }),
    });
    assert.equal(plan.draw, false);
    assert.equal(plan.markers.length, 0);
    assert.match(plan.reason, /没有识别出任何阶段/);
  });

  it("geometry is empty whenever the plan says not to draw", () => {
    const geometry = buildCpaGeometry(
      cpaOverlayPlan({ enabled: true, interval: "1h", phases: phases([record()], { insufficient: true }) }),
      PROJECTOR,
    );
    assert.deepEqual(geometry, { bands: [], levels: [], markers: [], anchors: [], lines: [] });
  });
});

/* ------------------------------------------- 4. 关闭开关不影响现有图表 */

describe("the overlay is off unless it is switched on", () => {
  it("draws nothing at all when disabled, even with a full phase series", () => {
    const plan = cpaOverlayPlan({
      enabled: false,
      interval: "1h",
      catalog: CATALOG,
      phases: phases([record(), record({ time: 1_700_000_600_000, phase: "base_n_break" })]),
    });
    assert.equal(plan.draw, false);
    assert.equal(plan.bands.length, 0);
    assert.equal(plan.markers.length, 0);
    assert.equal(plan.lines.length, 0);
    assert.match(plan.reason, /开关未打开/);
  });

  it("draws nothing when there is no payload either", () => {
    const plan = cpaOverlayPlan({ enabled: true, interval: "1h", phases: null });
    assert.equal(plan.draw, false);
    assert.equal(plan.markers.length, 0);
  });

  it("only adds to what is already there once it is on", () => {
    // Two records, because a line needs two points to exist at all.
    const series = phases([
      record({ time: 1_700_000_000_000, ema10: 103, ema20: 101 }),
      record({ time: 1_700_000_600_000, ema10: 104, ema20: 102, phase: "base_n_break" }),
    ]);
    const off = cpaOverlayPlan({ enabled: false, interval: "1h", catalog: CATALOG, phases: series });
    const on = cpaOverlayPlan({ enabled: true, interval: "1h", catalog: CATALOG, phases: series });
    assert.deepEqual(buildCpaGeometry(off, PROJECTOR), { bands: [], levels: [], markers: [], anchors: [], lines: [] });
    assert.equal(on.draw, true);
    assert.ok(on.bands.length >= 1);
    assert.ok(on.levels.some((level) => level.kind === "pivot"));
    assert.ok(on.levels.some((level) => level.kind === "invalidation"));
    assert.ok(on.lines.some((line) => line.key === "ema10"));
    assert.ok(on.lines.some((line) => line.key === "ema20"));
  });

  it("adds SMA50/SMA200 only when the daily chart asks for them", () => {
    const candles = Array.from({ length: 260 }, (_, index) => ({ time: 1_700_000_000_000 + index * 86_400_000, close: 100 + index }));
    const without = cpaOverlayPlan({ enabled: true, interval: "1d", catalog: CATALOG, phases: phases([record()]), candles });
    assert.equal(without.lines.some((line) => line.key === "sma50"), false);
    const withSma = cpaOverlayPlan({ enabled: true, interval: "1d", catalog: CATALOG, phases: phases([record()]), candles, showLongSma: true });
    assert.equal(withSma.lines.some((line) => line.key === "sma50"), true);
    assert.equal(withSma.lines.some((line) => line.key === "sma200"), true);
  });

  it("computes a moving average from the closes it is given", () => {
    const points = simpleMovingAverage([{ time: 1, close: 10 }, { time: 2, close: 20 }, { time: 3, close: 30 }], 2);
    assert.deepEqual(points, [{ time: 2, value: 15 }, { time: 3, value: 25 }]);
    assert.deepEqual(simpleMovingAverage([{ time: 1, close: 10 }], 2), []);
  });

  it("places a marker on its own bar when the chart has that bar", () => {
    const at = 1_700_000_000_000;
    const plan = cpaOverlayPlan({
      enabled: true,
      interval: "1h",
      catalog: CATALOG,
      phases: phases([record({ time: at, phase: "wedge_pop", direction: "bullish", pivotPrice: 40 })]),
      candles: [{ time: at, close: 100, high: 108, low: 96 }],
    });
    // The pivot (40) is far below the bar; the marker belongs on the bar it confirmed.
    assert.equal(plan.markers[0].price, 96, "偏多标记落在该根K线的低点");
    const shortPlan = cpaOverlayPlan({
      enabled: true,
      interval: "1h",
      catalog: CATALOG,
      phases: phases([record({ time: at, phase: "wedge_drop", direction: "bearish", pivotPrice: 400 })]),
      candles: [{ time: at, close: 100, high: 108, low: 96 }],
    });
    assert.equal(shortPlan.markers[0].price, 108, "偏空标记落在该根K线的高点");
  });

  it("drops anything the chart does not hold instead of drawing it at a guessed x", () => {
    // Live bug: a 600-bar phase series on a 299-bar chart made `timeToCoordinate`
    // extrapolate, so bands piled up as 2px slivers and markers sat at x = -2000.
    const outside = record({ time: 1_600_000_000_000 });
    const plan = cpaOverlayPlan({ enabled: true, interval: "1h", catalog: CATALOG, phases: phases([outside]) });
    const geometry = buildCpaGeometry(plan, PROJECTOR);
    assert.deepEqual(geometry.markers, [], "视图之外的标记不应被投影");
    assert.deepEqual(geometry.bands, [], "视图之外的阶段带不应被压成左侧细条");
  });

  it("clamps a band that begins before the chart and returns to the left edge", () => {
    const plan = cpaOverlayPlan({
      enabled: true,
      interval: "1h",
      catalog: CATALOG,
      phases: phases([
        record({ time: 1_600_000_000_000, phase: "wedge_pop" }),
        record({ time: 1_699_999_900_000, phase: "wedge_pop" }),
      ]),
    });
    const geometry = buildCpaGeometry(plan, { ...PROJECTOR, timeRange: { from: 1_699_999_900_000, to: 1_700_001_000_000 } });
    assert.equal(geometry.bands.length, 1);
    assert.equal(geometry.bands[0].x1, 0, "跨过左边界要贴左边缘，而不是被丢掉");
    assert.ok(geometry.bands[0].x2 > 0);
  });

  it("picks the nearest clickable marker and ignores clicks in open space", () => {
    const plan = cpaOverlayPlan({ enabled: true, interval: "1h", catalog: CATALOG, phases: phases([record()]) });
    const geometry = buildCpaGeometry(plan, PROJECTOR);
    assert.ok(geometry.anchors.length >= 1);
    const target = geometry.anchors[0];
    assert.equal(cpaMarkerAt(geometry.anchors, target.x + 3, target.y + 3)?.time, target.marker.time);
    assert.equal(cpaMarkerAt(geometry.anchors, target.x + 400, target.y + 400), null);
  });
});

/* ---------------------------------------------- 5. 参数默认值按类别与周期取 */

describe("parameter defaults come from the catalogue's class and interval", () => {
  it("maps the instrument class onto the engine's asset classes", () => {
    assert.equal(assetClassOf("stock"), "stock");
    assert.equal(assetClassOf("etf"), "etf");
    assert.equal(assetClassOf("crypto"), "crypto");
    assert.equal(assetClassOf(null), "stock");
  });

  it("takes the class-and-interval preset, not one global default", () => {
    const stock = parameterDefaults(CATALOG, "stock", "1h");
    const crypto = parameterDefaults(CATALOG, "crypto", "1d");
    assert.deepEqual(stock.entryStages, ["wedge_pop"]);
    assert.deepEqual(crypto.entryStages, ["base_n_break"]);
    assert.equal(crypto.sideMode, "symmetric");
    assert.equal(stock.sideMode, "long_only");
    assert.equal(parameterDefaults(CATALOG, "etf", "1h").extensionAtr, 1.8);
  });

  it("falls back to the specs when the class or interval is not published", () => {
    const unknown = parameterDefaults(CATALOG, "stock", "1w");
    assert.equal(unknown.minBars, 60);
    assert.equal(unknown.exitOnExhaustion, true);
  });

  it("drops values the request model cannot carry instead of forwarding them", () => {
    const odd = {
      ...CATALOG,
      defaults: {
        stock: { "1h": { emaFast: 12, nested: { a: 1 }, nothing: null } },
      },
    } as unknown as CpaCatalog;
    const values = parameterDefaults(odd, "stock", "1h");
    assert.equal(values.emaFast, 12);
    assert.equal("nested" in values, false);
    assert.equal("nothing" in values, false);
  });

  it("reports where each row's value came from", () => {
    const rows = parameterRows(CATALOG, "crypto", "1d");
    const sideMode = rows.find((row) => row.spec.key === "sideMode");
    assert.equal(sideMode?.origin, "defaults");
    assert.equal(sideMode?.value, "symmetric");
    const emaFast = rows.find((row) => row.spec.key === "emaFast");
    assert.equal(emaFast?.origin, "defaults");
  });

  it("carries the label, unit and range through for the form to render", () => {
    const spec = CATALOG.parameters.find((item) => item.key === "emaFast");
    assert.equal(spec?.label, "快线 EMA 周期");
    assert.equal(spec?.unit, "根");
    assert.equal(spec?.minimum, 2);
    assert.equal(spec?.maximum, 100);
  });

  it("judges the CPA warmup from the engine's minBars", () => {
    assert.equal(warmupState(CATALOG, "1h", 600, "stock").ok, true);
    const short = warmupState(CATALOG, "1h", 40, "stock");
    assert.equal(short.ok, false);
    assert.equal(short.required, 60);
    assert.match(short.reason, /60/);
  });
});

/* ------------------------------------------- 6. 简化仓位提示必然出现 */

describe("the simplified-position notice is always available", () => {
  it("uses the catalogue's wording when it is there", () => {
    assert.equal(simplePositionNotice(CATALOG), CATALOG.simplePositionNotice);
  });

  it("accepts the engine's newer field name for the same notice", () => {
    const renamed = { ...CATALOG, simplePositionNotice: undefined, intentPositionNotice: "分批减仓尚未模拟（新字段名）。" } as CpaCatalog;
    assert.equal(simplePositionNotice(renamed), "分批减仓尚未模拟（新字段名）。");
  });

  it("falls back to the engine's sentence when the catalogue did not load", () => {
    for (const catalog of [null, { ...CATALOG, simplePositionNotice: "" } as CpaCatalog]) {
      const notice = simplePositionNotice(catalog);
      assert.match(notice, /分批建仓|单仓位简化版本/);
    }
    assert.match(FALLBACK_SIMPLE_POSITION_NOTICE, /单仓位简化版本/);
  });

  it("always carries the notice into a run's view", () => {
    const view = cpaResultView({
      cpaPhases: phases([record()]),
      trades: [],
    });
    assert.ok(view);
    assert.match(view.notice, /单仓位简化版本/);
  });

  it("never produces a view for a run without phases", () => {
    assert.equal(cpaResultView({ net_return_pct: 12 }), null);
    assert.equal(cpaResultView(null), null);
  });
});

/* ----------------------------------------------------------- 结果渲染数据 */

describe("result rendering", () => {
  it("separates confirmed phases from candidates and observations", () => {
    const rows = phaseDistribution(phases([
      record({ phase: "wedge_pop", status: "confirmed" }),
      record({ phase: "wedge_pop", status: "candidate" }),
      record({ phase: "reversal_extension", status: "confirmed" }),
      record({ phase: "base_n_break", status: "confirmed" }),
    ]));
    const wedge = rows.find((row) => row.phase === "wedge_pop");
    assert.equal(wedge?.confirmed, 1);
    assert.equal(wedge?.candidate, 1);
    const reversal = rows.find((row) => row.phase === "reversal_extension");
    assert.equal(reversal?.confirmed, 0, "观察阶段不计入已确认");
    assert.equal(reversal?.observation, 1);
  });

  it("attributes a trade to the phase in force at its entry, not to the newest one", () => {
    const series = [
      record({ time: 1000, phase: "wedge_pop", status: "confirmed" }),
      record({ time: 2000, phase: "base_n_break", status: "confirmed" }),
    ];
    const rows = tradesByEntryPhase([
      { entry_time: 1500, net_pnl: 10, return_pct: 1, liquidated: false },
      { entry_time: 2500, net_pnl: -4, return_pct: -0.4, liquidated: true },
    ], series);
    const wedge = rows.find((row) => row.phase === "wedge_pop");
    const base = rows.find((row) => row.phase === "base_n_break");
    assert.equal(wedge?.trades, 1);
    assert.equal(wedge?.netPnl, 10);
    assert.equal(base?.trades, 1);
    assert.equal(base?.liquidations, 1);
    assert.equal(base?.worstReturnPct, -0.4);
  });

  it("keeps a trade that predates the series instead of dropping it", () => {
    const rows = tradesByEntryPhase([{ entry_time: 10, net_pnl: 3 }], [record({ time: 1000 })]);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].phase, "unmatched");
    assert.match(rows[0].label, /未匹配/);
    assert.equal(rows[0].trades, 1);
  });

  it("says the filter comparison is unavailable rather than inventing one", () => {
    const missing = higherTimeframeComparison({ cpaMeta: { parameterVersion: "v1" } });
    assert.equal(missing.available, false);
    assert.match(missing.reason, /未提供/);
  });

  it("reads a filter comparison when the payload does carry one", () => {
    const comparison = higherTimeframeComparison({
      cpaMeta: { filterComparison: { withFilter: { netReturnPct: 12, trades: 8 }, withoutFilter: { netReturnPct: 5, trades: 21 } } },
    });
    assert.equal(comparison.available, true);
    const row = comparison.rows.find((item) => item.label === "净收益 %");
    assert.equal(row?.withFilter, 12);
    assert.equal(row?.withoutFilter, 5);
  });

  it("finds the cpa-phases attachment under either spelling", () => {
    assert.equal(cpaAttachment(["equity", "cpa-phases"]), "cpa-phases");
    assert.equal(cpaAttachment(["cpa-phases.json"]), "cpa-phases.json");
    assert.equal(cpaAttachment(["equity"]), null);
    assert.equal(cpaAttachment(undefined), null);
  });

  it("builds the evidence card with the numbers a reader checks", () => {
    const card = cpaEvidenceCard(record(), { catalog: CATALOG, dataVersion: "data-abc", higherIntervals: { management: "4h", background: "1d" } });
    assert.equal(card.status, "confirmed");
    assert.equal(card.signal.actionable, true);
    assert.equal(card.volumeRatio, 1.6);
    assert.equal(card.contractionScore, 0.42);
    assert.equal(card.pivotPrice, 105.5);
    assert.equal(card.invalidationPrice, 98.25);
    assert.equal(card.parameterVersion, "cpa-qd/1.0.0");
    assert.equal(card.timeframes.management, "4h");
    assert.equal(card.reasons.length, 2);
  });

  it("flags a candidate card as not actionable", () => {
    const card = cpaEvidenceCard(record({ status: "candidate" }), { catalog: CATALOG });
    assert.equal(card.signal.actionable, false);
    assert.match(card.title, /候选/);
  });

  it("marks observation phases without needing the catalogue", () => {
    assert.equal(isObservationPhase("exhaustion_extension"), true);
    assert.equal(isObservationPhase("wedge_pop"), false);
  });
});
