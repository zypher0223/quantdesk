/**
 * The AI paper simulation's draft/refresh/receipt rules, without a DOM.
 *
 * These cover the front-end half of the "the page showed one configuration and the
 * account ran another" defect: what a save actually sends, whether a background
 * refresh may overwrite typing, and whether the engine's receipt confirms the rules the
 * page displayed. The component itself is not rendered here - the decisions it makes
 * are delegated to the pure functions below, which is what these tests pin down.
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { Instrument } from "../src/data/market.ts";
import type { AiPaperProfile } from "../src/services/ai-paper.ts";
import {
  configPayload, draftFromProfile, draftReady, isDraftDirty, pendingSummary,
  receiptMismatch, reconcileDraft, responseIsCurrent, revisionLabel, runningSummary,
  type AiPaperDraft,
} from "../src/services/ai-paper-draft.ts";

/* --------------------------------------------------------------- fixtures */

// The venue's real 17-contract universe: 15 stock-class (the two leveraged ETFs
// included) plus the two crypto pairs.
const POOL: Array<[string, string, "stock" | "crypto"]> = [
  ["AAPLUSDT", "AAPL", "stock"], ["MSFTUSDT", "MSFT", "stock"],
  ["GOOGLUSDT", "GOOGL", "stock"], ["AMZNUSDT", "AMZN", "stock"],
  ["NVDAUSDT", "NVDA", "stock"], ["METAUSDT", "META", "stock"],
  ["TSLAUSDT", "TSLA", "stock"], ["SNDKUSDT", "SNDK", "stock"],
  ["MUUSDT", "MU", "stock"], ["AMDSTOCKUSDT", "AMD", "stock"],
  ["NBISUSDT", "NBIS", "stock"], ["SPCXUSDT", "SPCX", "stock"],
  ["SKHYUSDT", "SKHY", "stock"], ["SOXLUSDT", "SOXL", "stock"],
  ["SOXSUSDT", "SOXS", "stock"],
  ["BTCUSDT", "BTC", "crypto"], ["ETHUSDT", "ETH", "crypto"],
];

const ALL_SYMBOLS = POOL.map(([venueSymbol]) => venueSymbol);

function instrument([venueSymbol, displaySymbol, pool]: [string, string, "stock" | "crypto"]): Instrument {
  return {
    displaySymbol, venueSymbol, name: `${displaySymbol} 现货`, group: "",
    productType: pool === "crypto" ? "crypto" : "stock", productLabel: "",
    riskClass: "standard", chartInterval: "", underlyingSymbol: null,
    pool, symbolMapped: true,
  };
}

const INSTRUMENTS = POOL.map(instrument);

function profile(overrides: Partial<AiPaperProfile> = {}): AiPaperProfile {
  return {
    id: "default", name: "主模拟", enabled: false, initial_cash: 100_000,
    max_leverage: 8, horizon: "swing", style: "conservative", fib_only: false,
    symbols: [...ALL_SYMBOLS], model_role: "council", last_cycle_ts: null,
    last_bar_ts: null, last_error: null, config_revision: 4,
    ...overrides,
  };
}

function draft(overrides: Partial<AiPaperDraft> = {}): AiPaperDraft {
  return { ...draftFromProfile(profile()), ...overrides };
}

const without = (...symbols: string[]) =>
  ALL_SYMBOLS.filter((symbol) => !symbols.includes(symbol));

/* ------------------------------------------------------- what a save sends */

describe("the payload is the draft, not the server profile", () => {
  it("sends short + aggressive with the two leveraged ETFs removed", () => {
    const server = profile({ horizon: "swing", style: "conservative" });
    const edited = draft({
      ...draftFromProfile(server), horizon: "short", style: "aggressive",
      symbols: without("SOXLUSDT", "SOXSUSDT"),
    });

    const payload = configPayload(edited);

    assert.equal(payload.horizon, "short");
    assert.equal(payload.style, "aggressive");
    assert.equal(payload.symbols.length, 15);
    assert.ok(!payload.symbols.includes("SOXLUSDT"), "SOXL 已移除");
    assert.ok(!payload.symbols.includes("SOXSUSDT"), "SOXS 已移除");
    // The page must never fall back to what the engine already had.
    assert.notDeepEqual(payload.symbols, server.symbols);
    assert.notEqual(payload.horizon, server.horizon);
  });

  it("trims the name and parses the numeric fields the form holds as text", () => {
    const payload = configPayload(draft({ name: "  夜盘模拟  ", initialCash: "250000.0", maxLeverage: "3.5" }));
    assert.equal(payload.name, "夜盘模拟");
    assert.equal(payload.initialCash, 250_000);
    assert.equal(payload.maxLeverage, 3.5);
  });

  it("copies the symbol list so a later edit cannot mutate what was sent", () => {
    const edited = draft({ symbols: ["BTCUSDT", "ETHUSDT"] });
    const payload = configPayload(edited);
    edited.symbols.push("NVDAUSDT");
    assert.deepEqual(payload.symbols, ["BTCUSDT", "ETHUSDT"]);
  });
});

/* ------------------------------------------------ a refresh may not eat edits */

describe("a background refresh never overwrites unsaved edits", () => {
  it("keeps a dirty draft and re-syncs only a clean one", () => {
    const server = profile();
    const edited = draft({ style: "aggressive", horizon: "short" });

    // What the badge reads: the draft differs from what the engine has.
    assert.equal(isDraftDirty(edited, server), true);
    // What an untouched draft reads.
    assert.equal(isDraftDirty(draftFromProfile(server), server), false);

    // A refresh applies `draftFromProfile`; reconcile is what decides, with the profile
    // the draft was last compared against as the baseline.
    assert.deepEqual(reconcileDraft(edited, server, server), edited, "脏草稿必须原样保留");
    assert.deepEqual(
      reconcileDraft(draftFromProfile(server), server, server),
      draftFromProfile(server),
    );
  });

  it("re-syncs to a profile that changed elsewhere when the operator had no edits", () => {
    const server = profile();
    const moved = profile({ name: "改名了", horizon: "short", symbols: ["BTCUSDT"], config_revision: 5 });
    assert.deepEqual(reconcileDraft(draftFromProfile(server), moved, server), draftFromProfile(moved));
  });

  it("does not mistake a server-side change for the operator's unsaved edit", () => {
    // The baseline is the profile the draft was last synced with, not the incoming one.
    // Comparing with the incoming profile would pin stale rules on screen and let the
    // next start push them back over a change made somewhere else.
    const server = profile();
    const moved = profile({ name: "别处改过", symbols: ["BTCUSDT"], config_revision: 9 });
    const untouched = draftFromProfile(server);

    assert.equal(isDraftDirty(untouched, moved), true, "相对新配置当然不同");
    assert.deepEqual(reconcileDraft(untouched, moved, server), draftFromProfile(moved),
      "没有未保存修改时必须采用服务端的新配置");

    // A draft the operator really did edit is kept even when the server moved too.
    const edited = draft({ style: "aggressive" });
    assert.deepEqual(reconcileDraft(edited, moved, server), edited);
  });

  it("still keeps the dirty draft when the server profile moved meanwhile", () => {
    const server = profile();
    const edited = draft({ symbols: without("SOXLUSDT", "SOXSUSDT") });
    const moved = profile({ name: "别处改过", max_leverage: 3, config_revision: 9 });
    assert.deepEqual(reconcileDraft(edited, moved, server), edited);
  });

  it("does not call a reordered or whitespace-only difference a rule change", () => {
    const server = profile();
    assert.equal(isDraftDirty(draft({ symbols: [...server.symbols].reverse() }), server), false);
    assert.equal(isDraftDirty(draft({ name: " 主模拟 " }), server), false);
    assert.equal(isDraftDirty(draft({ initialCash: "100000.0" }), server), false);
  });

  it("does notice a genuinely removed symbol", () => {
    const server = profile();
    assert.equal(isDraftDirty(draft({ symbols: without("SOXLUSDT") }), server), true);
    // And a duplicate is not a way to look equal while selecting something else.
    assert.equal(isDraftDirty(draft({ symbols: [...without("SOXLUSDT", "SOXSUSDT"), "BTCUSDT"] }), server), true);
  });
});

/* --------------------------------------------------- the engine's receipt */

describe("receiptMismatch", () => {
  it("is empty when the engine confirmed exactly what was sent", () => {
    const payload = configPayload(draft({ horizon: "short", style: "aggressive", symbols: without("SOXSUSDT") }));
    const confirmed = profile({
      name: "主模拟", horizon: "short", style: "aggressive", fib_only: false,
      max_leverage: 8, symbols: without("SOXSUSDT"),
    });
    assert.deepEqual(receiptMismatch(payload, confirmed), []);
  });

  it("ignores symbol order, which is not a rule", () => {
    const payload = configPayload(draft({ symbols: ["BTCUSDT", "ETHUSDT", "NVDAUSDT"] }));
    const confirmed = profile({ symbols: ["NVDAUSDT", "BTCUSDT", "ETHUSDT"] });
    assert.deepEqual(receiptMismatch(payload, confirmed), []);
  });

  it("names every field that differs", () => {
    const payload = configPayload(draft({ horizon: "short", style: "aggressive", symbols: without("SOXLUSDT", "SOXSUSDT") }));
    const confirmed = profile({
      name: "另一个名字", horizon: "swing", style: "gambler", fib_only: true,
      max_leverage: 3, symbols: [...without("SOXLUSDT", "SOXSUSDT"), "EXTRAUSDT"],
    });

    const problems = receiptMismatch(payload, confirmed);
    const text = problems.join("\n");

    assert.equal(problems.length, 6, "每个不一致字段各一条");
    assert.match(text, /名称不一致/);
    assert.match(text, /风格不一致/);
    assert.match(text, /周期不一致/);
    assert.match(text, /合约集合不一致/);
    assert.match(text, /仅斐波那契设置不一致/);
    assert.match(text, /最大杠杆不一致/);
  });

  it("names the symbols that went missing and the ones that appeared", () => {
    const payload = configPayload(draft({ symbols: without("SOXLUSDT", "SOXSUSDT") }));
    const confirmed = profile({ symbols: [...without("SOXLUSDT", "SOXSUSDT", "NVDAUSDT"), "EXTRAUSDT"] });
    const text = receiptMismatch(payload, confirmed).join("\n");
    assert.match(text, /缺少 NVDA/);
    assert.match(text, /多出 EXTRA/);
    assert.match(text, /本次发送 15 个，服务端确认 15 个/);
  });

  it("does not report a mismatch for the initial cash it cannot change", () => {
    // An account's initial cash is fixed when it is created, so a server that keeps the
    // existing figure is right; reporting it would stop a profile for no reason.
    const payload = { ...configPayload(draft()), initialCash: 999_999 };
    assert.deepEqual(receiptMismatch(payload, profile({ initial_cash: 100_000 })), []);
  });
});

/* ------------------------------------------------------------- summaries */

describe("summaries", () => {
  it("names the excluded contracts and the counts for what is about to start", () => {
    const line = pendingSummary(draft({ horizon: "short", style: "aggressive", symbols: without("SOXLUSDT", "SOXSUSDT") }), INSTRUMENTS);
    assert.match(line, /^短线 · 激进 · 15 个合约/);
    assert.match(line, /已排除 SOXL、SOXS/);
    assert.match(line, /最大 8x$/);
  });

  it("says 全部合约 when the whole pool is selected", () => {
    const line = pendingSummary(draft({ symbols: [...ALL_SYMBOLS] }), INSTRUMENTS);
    assert.match(line, /17 个合约 · 全部合约/);
    assert.doesNotMatch(line, /已排除/);
  });

  it("claims nothing about the pool when the instrument index has not loaded", () => {
    const line = pendingSummary(draft({ symbols: without("SOXLUSDT") }), []);
    assert.doesNotMatch(line, /已排除|全部合约/);
    assert.match(line, /16 个合约/);
  });

  it("marks the Fib-only mode and formats a fractional leverage", () => {
    const line = pendingSummary(draft({ fibOnly: true, maxLeverage: "2.5", symbols: ["BTCUSDT"] }), INSTRUMENTS);
    assert.match(line, /仅 Fib 回调/);
    assert.match(line, /最大 2\.5x/);
  });

  it("summarises what is RUNNING from the server profile, never from the draft", () => {
    const server = profile({ horizon: "swing", style: "conservative", fib_only: true, max_leverage: 3 });
    const edited = draft({ horizon: "short", style: "gambler", symbols: ["BTCUSDT"] });
    const line = runningSummary(server, INSTRUMENTS);
    assert.match(line, /^中长线 · 稳妥/);
    assert.match(line, /17 个合约 · 全部合约/);
    assert.match(line, /仅 Fib 回调/);
    assert.match(line, /最大 3x$/);
    assert.doesNotMatch(line, /赌徒|短线/);
    // The draft is a different configuration; the running line must not borrow it.
    assert.notEqual(line, pendingSummary(edited, INSTRUMENTS));
  });
});

/* ------------------------------------------------- other pure decisions */

describe("draft readiness", () => {
  it("refuses a draft that cannot be sent", () => {
    assert.equal(draftReady(draft()), true);
    assert.equal(draftReady(draft({ name: "   " })), false);
    assert.equal(draftReady(draft({ initialCash: "" })), false);
    assert.equal(draftReady(draft({ initialCash: "0" })), false);
    assert.equal(draftReady(draft({ maxLeverage: "" })), false);
    assert.equal(draftReady(draft({ maxLeverage: "0" })), false);
    assert.equal(draftReady(draft({ symbols: [] })), false);
  });
});

describe("an engine that has not reported a revision", () => {
  it("renders the revision as unknown instead of the word undefined", () => {
    // Verified against the running engine while the backend half was still landing:
    // its profile had no `config_revision` and its decision evidence had no
    // `configRevision`, so these are the values the page really receives in between.
    assert.equal(revisionLabel(4), "修订 #4");
    assert.equal(revisionLabel(0), "修订 #0");
    assert.equal(revisionLabel(undefined), "修订 —");
    assert.doesNotMatch(revisionLabel(undefined), /undefined/u);
  });
});

describe("stale responses", () => {
  it("accepts only the newest request for the instance still selected", () => {
    assert.equal(responseIsCurrent(7, 7, "default", "default"), true);
    assert.equal(responseIsCurrent(6, 7, "default", "default"), false, "更新的请求已开始");
    assert.equal(responseIsCurrent(7, 7, "default", "night"), false, "已切换到别的实例");
    assert.equal(responseIsCurrent(6, 7, "default", "night"), false);
  });
});
