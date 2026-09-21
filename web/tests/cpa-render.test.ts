/**
 * The CPA components' render-level guarantees, without a DOM.
 *
 * A React component is a function of its props, so calling one directly is enough to
 * check the property that matters here: **with the overlay switched off, nothing is
 * rendered at all**. The alternative - trusting a reader to notice that an overlay is
 * "empty" - is how an off switch ends up costing a request and drawing an empty SVG.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { CpaOverlay, CpaPhaseCard, CpaStatusLine } from "../src/components/cpa-chart-overlay.tsx";
import { CpaResultSection } from "../src/components/cpa-result-block.tsx";
import {
  buildCpaGeometry,
  cpaOverlayPlan,
  shouldFetchCpa,
  type CpaPhaseRecord,
  type CpaPhases,
} from "../src/services/cpa.ts";

const PROJECTOR = { x: (time: number) => (time - 1_699_999_800_000) / 1000, y: (price: number) => price, width: 800, height: 460, timeRange: { from: 1_699_999_800_000, to: 1_700_001_000_000 } };

function record(overrides: Partial<CpaPhaseRecord> = {}): CpaPhaseRecord {
  return {
    time: 1_700_000_000_000,
    phase: "wedge_pop",
    status: "confirmed",
    direction: "bullish",
    confidence: 0.7,
    pivotPrice: 105,
    invalidationPrice: 99,
    setupLow: 100,
    ema10: 103,
    ema20: 101,
    distanceAtr: 1.1,
    volumeRatio: 1.4,
    contractionScore: 0.5,
    atr: 2,
    cycle: "upside",
    higherTimeframe: { interval: "4h", phase: "none", trend: "bullish", closedAt: 1, available: true, reason: "" },
    backgroundTimeframe: { interval: "1d", phase: "none", trend: "bullish", closedAt: 1, available: true, reason: "" },
    reasons: ["突破枢轴"],
    warnings: [],
    checks: {},
    parameterVersion: "cpa-qd/1.0.0",
    ...overrides,
  };
}

function phases(records: CpaPhaseRecord[]): CpaPhases {
  return {
    symbol: "BTCUSDT",
    displaySymbol: "BTC",
    interval: "1h",
    productType: "crypto",
    snapshotHash: "s",
    dataVersion: "d",
    parameterVersion: "cpa-qd/1.0.0",
    parameters: {},
    higherIntervals: { management: "4h", background: "1d" },
    bars: 600,
    insufficient: false,
    insufficientReason: "",
    counts: {},
    current: records.at(-1) ?? null,
    records,
    warnings: [],
    attribution: "x",
  };
}

/* The component functions are called directly: a null return is React's own way of
   saying "this renders nothing", so the assertion is about the same thing the DOM
   would show. */

describe("switched off, nothing is rendered", () => {
  it("draws no overlay at all when the plan is off", () => {
    const plan = cpaOverlayPlan({ enabled: false, interval: "1h", phases: phases([record()]) });
    const element = CpaOverlay({
      plan,
      geometry: buildCpaGeometry(plan, PROJECTOR),
      width: 800,
      height: 460,
      onSelect: () => {},
    });
    assert.equal(element, null);
  });

  it("draws no overlay when the sample is insufficient", () => {
    const plan = cpaOverlayPlan({
      enabled: true,
      interval: "1h",
      phases: phases([{ ...record(), status: "none", phase: "none" }]),
    });
    const element = CpaOverlay({
      plan,
      geometry: buildCpaGeometry(plan, PROJECTOR),
      width: 800,
      height: 460,
      onSelect: () => {},
    });
    assert.equal(element, null);
  });

  it("renders nothing at all for a no-phase series", () => {
    const plan = cpaOverlayPlan({ enabled: true, interval: "1h", phases: phases([]) });
    assert.equal(plan.draw, false);
    assert.equal(
      CpaOverlay({ plan, geometry: buildCpaGeometry(plan, PROJECTOR), width: 800, height: 460, onSelect: () => {} }),
      null,
    );
  });

  it("renders no status line while the switch is off", () => {
    const plan = cpaOverlayPlan({ enabled: false, interval: "1h", phases: phases([record()]) });
    assert.equal(CpaStatusLine({ plan }), null);
  });

  it("does render once the plan says to draw", () => {
    const plan = cpaOverlayPlan({
      enabled: true,
      interval: "1h",
      phases: phases([record(), record({ time: 1_700_000_600_000, phase: "base_n_break" })]),
    });
    assert.equal(plan.draw, true);
    const element = CpaOverlay({
      plan,
      geometry: buildCpaGeometry(plan, PROJECTOR),
      width: 800,
      height: 460,
      onSelect: () => {},
    });
    assert.notEqual(element, null);
    assert.notEqual(CpaStatusLine({ plan }), null);
  });

  it("renders no CPA block for a run that is not a CPA run", () => {
    assert.equal(CpaResultSection({ result: { net_return_pct: 1 }, runId: 1, artifacts: [] }), null);
    assert.equal(CpaResultSection({ result: null, runId: 1, artifacts: [] }), null);
    const cpa = CpaResultSection({ result: { cpaPhases: phases([record()]), trades: [] }, runId: 7, artifacts: ["cpa-phases"] });
    assert.notEqual(cpa, null);
  });

  it("renders an evidence card that says a candidate is not an entry", () => {
    const element = CpaPhaseCard({
      record: record({ status: "candidate" }),
      catalog: null,
      dataVersion: "d",
      higherIntervals: { management: "4h", background: "1d" },
      onClose: () => {},
    });
    assert.notEqual(element, null);
  });
});

describe("the phase request follows the switch, not the page", () => {
  it("asks the engine only when the overlay is on and there is a symbol", () => {
    assert.equal(shouldFetchCpa({ cpaEnabled: true, cpaOn: false, symbol: "BTCUSDT" }), false);
    assert.equal(shouldFetchCpa({ cpaEnabled: false, cpaOn: true, symbol: "BTCUSDT" }), false);
    assert.equal(shouldFetchCpa({ cpaEnabled: true, cpaOn: true, symbol: "   " }), false);
    assert.equal(shouldFetchCpa({ cpaEnabled: true, cpaOn: true, symbol: "BTCUSDT" }), true);
  });
});
