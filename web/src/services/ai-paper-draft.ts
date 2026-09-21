/**
 * The AI paper simulation's **edit draft**, kept apart from the server snapshot.
 *
 * The bug this separates: the page held one set of values that both the server and the
 * form wrote to. A background refresh overwrote whatever the operator had typed, and
 * "start" told the engine to run the rules it already had rather than the rules on
 * screen - so the page could show one configuration and the account could run another.
 *
 * Everything here is pure and DOM-free, so the node test runner covers the decisions
 * that matter: what the payload contains, whether the draft differs from the server,
 * whether the engine's receipt confirms what was sent, and how each is summarised.
 *
 * `initialCash` and `maxLeverage` stay **text** on purpose - they are raw input values,
 * and a half-typed field has to be representable. They are parsed where a number is
 * actually needed, which also means "100000.0" is not spuriously different from
 * 100000.
 */
import type { Instrument } from "../data/market";
import type {
  AiPaperConfigPayload, AiPaperHorizon, AiPaperProfile, AiPaperStyle,
} from "./ai-paper";

export interface AiPaperDraft {
  name: string;
  initialCash: string;
  maxLeverage: string;
  horizon: AiPaperHorizon;
  style: AiPaperStyle;
  fibOnly: boolean;
  symbols: string[];
}

export const STYLE_LABEL: Record<AiPaperStyle, string> = {
  conservative: "稳妥",
  aggressive: "激进",
  gambler: "赌徒",
};

export const HORIZON_LABEL: Record<AiPaperHorizon, string> = {
  short: "短线",
  swing: "中长线",
};

/** The draft a freshly read profile implies. */
export function draftFromProfile(profile: AiPaperProfile): AiPaperDraft {
  return {
    name: profile.name,
    initialCash: String(profile.initial_cash),
    maxLeverage: String(profile.max_leverage),
    horizon: profile.horizon,
    style: profile.style,
    fibOnly: profile.fib_only,
    symbols: [...profile.symbols],
  };
}

/** The request body for `PUT /config` and for the atomic `POST /start`. */
export function configPayload(draft: AiPaperDraft): AiPaperConfigPayload {
  return {
    name: draft.name.trim(),
    initialCash: Number(draft.initialCash),
    maxLeverage: Number(draft.maxLeverage),
    horizon: draft.horizon,
    style: draft.style,
    fibOnly: draft.fibOnly,
    symbols: [...draft.symbols],
  };
}

/** Symbol selection is a set: order and duplicates are not rule changes. */
function sameSymbolSet(left: readonly string[], right: readonly string[]): boolean {
  const a = new Set(left);
  const b = new Set(right);
  if (a.size !== b.size) return false;
  for (const item of a) if (!b.has(item)) return false;
  return true;
}

/** Names are compared trimmed because the payload trims them. */
const nameOf = (value: string) => value.trim();

/**
 * Does the draft differ from the server profile in any rule the engine stores?
 *
 * A whitespace-only name edit is not a rule change - the payload trims it, so flagging
 * it would leave "unsaved changes" showing forever after a save.
 */
export function isDraftDirty(draft: AiPaperDraft, profile: AiPaperProfile): boolean {
  return (
    nameOf(draft.name) !== nameOf(profile.name)
    || Number(draft.initialCash) !== profile.initial_cash
    || Number(draft.maxLeverage) !== profile.max_leverage
    || draft.horizon !== profile.horizon
    || draft.style !== profile.style
    || draft.fibOnly !== profile.fib_only
    || !sameSymbolSet(draft.symbols, profile.symbols)
  );
}

/**
 * What a server refresh should do to the draft in hand.
 *
 * "Unsaved" is measured against `baseline` - the profile the draft was last compared
 * with - and **not** against the incoming one. Comparing with the incoming profile
 * would call a change made elsewhere (another tab, the engine) the operator's unsaved
 * edit: the stale values would be pinned on screen, the badge would claim unsaved work
 * nobody did, and the next start would push those stale rules back over the change.
 * So: keep the draft when the operator really has edits, re-sync when they do not.
 */
export function reconcileDraft(
  current: AiPaperDraft, next: AiPaperProfile, baseline: AiPaperProfile,
): AiPaperDraft {
  return isDraftDirty(current, baseline) ? current : draftFromProfile(next);
}

/** Can this draft be sent at all? Mirrors the form's own `min` constraints. */
export function draftReady(draft: AiPaperDraft): boolean {
  const cash = Number(draft.initialCash);
  const leverage = Number(draft.maxLeverage);
  return (
    nameOf(draft.name).length > 0
    && Number.isFinite(cash) && cash > 0
    && Number.isFinite(leverage) && leverage > 0
    && draft.symbols.length > 0
  );
}

/**
 * Is a response still the one to apply?
 *
 * Two independent ways it can be stale: a newer request has started, or the operator
 * switched instance while this one was in flight. Both must discard it.
 */
export function responseIsCurrent(
  sequence: number, latest: number, requestedId: string, selectedId: string,
): boolean {
  return sequence === latest && requestedId === selectedId;
}

/** Venue symbol to the short form the UI shows (`SOXLUSDT` → `SOXL`). */
function shortSymbol(symbol: string): string {
  return symbol.endsWith("USDT") ? symbol.slice(0, -"USDT".length) : symbol;
}

/**
 * The rule revision, rendered only when the engine reported one.
 *
 * A page deployed ahead of the engine would otherwise print "修订 #undefined" at the
 * operator, and an unknown revision should read as unknown rather than as a number.
 */
export function revisionLabel(value: number | undefined): string {
  return typeof value === "number" ? `修订 #${value}` : "修订 —";
}

function nearlyEqual(left: number, right: number): boolean {
  return Number.isFinite(left) && Number.isFinite(right) && Math.abs(left - right) <= 1e-9;
}

/**
 * Compare what was sent against the profile the engine confirmed.
 *
 * An empty list means the engine is running exactly what the page showed. Anything
 * else is a rule the operator did not ask for, and the caller must not report success.
 *
 * `initialCash` is deliberately **not** compared: an account's initial cash is fixed
 * when it is created, so a server that keeps the existing figure is behaving correctly,
 * and reporting that as a mismatch would stop a profile for no reason. The fields here
 * are the ones the engine can and does change.
 */
export function receiptMismatch(
  sent: AiPaperConfigPayload, profile: AiPaperProfile,
): string[] {
  const problems: string[] = [];

  if (nameOf(sent.name) !== nameOf(profile.name)) {
    problems.push(`名称不一致：本次发送「${nameOf(sent.name) || "（空）"}」，服务端确认「${profile.name}」`);
  }
  if (sent.style !== profile.style) {
    problems.push(
      `风格不一致：本次发送 ${STYLE_LABEL[sent.style] ?? sent.style}，`
      + `服务端确认 ${STYLE_LABEL[profile.style] ?? profile.style}`,
    );
  }
  if (sent.horizon !== profile.horizon) {
    problems.push(
      `周期不一致：本次发送 ${HORIZON_LABEL[sent.horizon] ?? sent.horizon}，`
      + `服务端确认 ${HORIZON_LABEL[profile.horizon] ?? profile.horizon}`,
    );
  }
  if (!sameSymbolSet(sent.symbols, profile.symbols)) {
    const missing = sent.symbols.filter((symbol) => !profile.symbols.includes(symbol));
    const extra = profile.symbols.filter((symbol) => !sent.symbols.includes(symbol));
    const detail = [
      missing.length > 0 ? `缺少 ${missing.map(shortSymbol).join("、")}` : "",
      extra.length > 0 ? `多出 ${extra.map(shortSymbol).join("、")}` : "",
    ].filter(Boolean).join("；");
    problems.push(
      `合约集合不一致：本次发送 ${sent.symbols.length} 个，`
      + `服务端确认 ${profile.symbols.length} 个（${detail}）`,
    );
  }
  if (sent.fibOnly !== profile.fib_only) {
    problems.push(
      `仅斐波那契设置不一致：本次发送 ${sent.fibOnly ? "开启" : "关闭"}，`
      + `服务端确认 ${profile.fib_only ? "开启" : "关闭"}`,
    );
  }
  if (!nearlyEqual(sent.maxLeverage, profile.max_leverage)) {
    problems.push(
      `最大杠杆不一致：本次发送 ${formatLeverage(sent.maxLeverage)}，`
      + `服务端确认 ${formatLeverage(profile.max_leverage)}`,
    );
  }
  return problems;
}

function formatLeverage(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return `${Number.isInteger(value) ? value : Number(value.toFixed(2))}x`;
}

/**
 * The excluded part of a summary: which of the venue's contracts this run will *not*
 * trade. With no instrument index the pool is unknown, so nothing is claimed - saying
 * "全部合约" without knowing the pool would be a guess dressed as a fact.
 */
function exclusionClause(symbols: readonly string[], instruments: readonly Instrument[]): string | null {
  if (instruments.length === 0) return null;
  const selected = new Set(symbols);
  const excluded = instruments
    .filter((item) => !selected.has(item.venueSymbol))
    .map((item) => item.displaySymbol);
  return excluded.length === 0 ? "全部合约" : `已排除 ${excluded.join("、")}`;
}

function summaryLine(
  horizon: AiPaperHorizon, style: AiPaperStyle, symbols: readonly string[],
  fibOnly: boolean, leverage: number, instruments: readonly Instrument[],
): string {
  return [
    HORIZON_LABEL[horizon] ?? horizon,
    STYLE_LABEL[style] ?? style,
    `${symbols.length} 个合约`,
    exclusionClause(symbols, instruments),
    fibOnly ? "仅 Fib 回调" : null,
    `最大 ${formatLeverage(leverage)}`,
  ].filter((part): part is string => Boolean(part)).join(" · ");
}

/** What the engine is running right now - always the server profile, never the draft. */
export function runningSummary(profile: AiPaperProfile, instruments: readonly Instrument[] = []): string {
  return summaryLine(
    profile.horizon, profile.style, profile.symbols, profile.fib_only,
    profile.max_leverage, instruments,
  );
}

/** What pressing start would launch - the draft as it stands on screen. */
export function pendingSummary(draft: AiPaperDraft, instruments: readonly Instrument[] = []): string {
  return summaryLine(
    draft.horizon, draft.style, draft.symbols, draft.fibOnly,
    Number(draft.maxLeverage), instruments,
  );
}
