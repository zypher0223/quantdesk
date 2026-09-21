import type { Candle, Timeframe } from "../data/market";

export interface EvidenceItem {
  key: string;
  label: string;
  value: unknown;
  source: string;
  observed_at: number;
  note: string;
}

export interface PriceStructure {
  last_close: number;
  atr: number | null;
  atr_source: string;
  recent_high: number;
  recent_low: number;
  recent_range_position_pct: number | null;
  swing_highs: number[];
  swing_lows: number[];
  bars: number;
  interval?: string;
  source?: "live" | "upload" | "engine_fetch";
}

export interface TradingPlan {
  direction?: "long" | "short" | "wait" | string;
  entry?: number;
  entry_zone?: number[];
  stop_loss?: number;
  take_profit_1?: number;
  take_profit_2?: number;
  risk_reward?: number;
  position_size_pct?: number;
  timeframe?: string;
  rationale?: string;
  trigger?: string | null;
  valid_until?: string;
}

export interface PlanCheck {
  present: boolean;
  ok: boolean;
  direction?: string;
  levels?: Record<string, number> | null;
  entryZone?: number[] | null;
  computed: {
    riskPerUnit?: number | null;
    rewardPerUnit?: number | null;
    riskReward?: number | null;
    riskPct?: number | null;
    rewardPct?: number | null;
    stopAtrMultiple?: number | null;
  };
  problems: string[];
  notes: string[];
}

export interface ReportValidation {
  unsupportedNumbers: Array<{ where: string; number: number; text: string }>;
  directives: string[];
  citedKeys: string[];
  unknownCitedKeys: string[];
  plan: PlanCheck;
  verified: boolean;
  archiveError?: string;
}

export interface ResearchReport {
  headline?: string;
  confidence?: string;
  facts?: Array<{ statement: string; evidence?: string[] }>;
  inferences?: Array<{ statement: string; basis?: string[]; confidence?: string }>;
  trading_plan?: TradingPlan;
  scenarios?: Array<{ name: string; condition: string; implication: string }>;
  invalidation?: string[];
  missing_evidence?: string[];
  data_caveats?: string[];
}

/** The external side of a research run: what was used, what was missing, and why. */
export interface ExternalEvidenceSummary {
  enabled: boolean;
  usable: number;
  topics: string[];
  providers: string[];
  cacheHits: number;
  calls: number;
  degraded: boolean;
  unavailable: Array<{ topic?: string; provider?: string; reason?: string }>;
  rejected: Array<{ topic?: string; provider?: string; reason?: string; basis?: string }>;
  errors: Array<{ topic?: string; provider?: string; reason?: string }>;
  appendix?: string;
  records?: Array<{
    key: string;
    label: string;
    provider: string;
    endpoint: string;
    topic: string;
    asOf: string;
    publishedAt: string;
    observedAt: string;
    source: string;
  }>;
}

export interface ResearchResult {
  ok: boolean;
  reportId: number | null;
  profile: string;
  model: string;
  latencySeconds: number;
  usage: Record<string, number | null>;
  report: ResearchReport | null;
  markdown: string;
  validation: ReportValidation;
  evidence: EvidenceItem[];
  externalEvidence?: ExternalEvidenceSummary | null;
  priceStructure: PriceStructure;
  missing: string[];
  builtAt: number;
  disclaimer: string;
}

export interface ResearchReadiness {
  ready: boolean;
  role: string;
  profile?: { name: string; provider: string; models: { deep: string; quick: string; vision: string } };
  reason?: string;
  action?: string;
}

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  // A reasoning model has been observed at 145s for one pass; leave headroom.
  const { timeoutMs = 420_000, ...rest } = init ?? {};
  let response: Response;
  try {
    response = await fetch(path, { ...rest, signal: AbortSignal.timeout(timeoutMs) });
  } catch (reason) {
    throw new Error(`无法连接本地引擎：${reason instanceof Error ? reason.message : String(reason)}`);
  }
  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    // Provider failures arrive as a JSON error body with a title and an action.
    const detail = (payload as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") {
      try {
        const parsed = JSON.parse(detail) as { title?: string; detail?: string; action?: string };
        if (parsed.title) throw new Error(`${parsed.title}：${parsed.detail ?? ""}${parsed.action ? `（${parsed.action}）` : ""}`);
      } catch {
        throw new Error(detail);
      }
    }
    throw new Error(`引擎返回 ${response.status}`);
  }
  return payload as T;
}

export function fetchResearchReadiness(): Promise<ResearchReadiness> {
  return request<ResearchReadiness>("/api/research/readiness", { timeoutMs: 10_000 });
}

/** Run a research pass. `candles` are the ones on screen, so the plan anchors to them. */
export function runResearch(payload: {
  symbol: string;
  timeframe: Timeframe;
  source: "live" | "upload" | "demo";
  candles?: Candle[];
  focus?: string;
  news?: string;
  includeBacktest?: boolean;
}): Promise<ResearchResult> {
  const body = {
    symbol: payload.symbol,
    timeframe: payload.timeframe,
    source: payload.source,
    includeBacktest: payload.includeBacktest ?? true,
    focus: payload.focus || null,
    news: payload.news || "",
    candles: payload.candles?.length
      ? payload.candles.map((bar) => ({ time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: bar.volume }))
      : null,
  };
  return request<ResearchResult>("/api/research", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export interface ArchivedReport {
  id: number;
  symbol: string;
  tradeDate: string;
  headline: string | null;
  ok: boolean;
  durationSeconds: number | null;
  createdAt: number;
  model: string | null;
  profile: string | null;
  verified: boolean;
}

export function fetchArchivedReports(limit = 20): Promise<{ reports: ArchivedReport[] }> {
  return request<{ reports: ArchivedReport[] }>(`/api/research/reports?limit=${limit}`, { timeoutMs: 15_000 });
}

export function fetchArchivedReport(id: number): Promise<{ markdown: string; headline: string | null }> {
  return request<{ markdown: string; headline: string | null }>(`/api/research/reports/${id}`, { timeoutMs: 15_000 });
}
