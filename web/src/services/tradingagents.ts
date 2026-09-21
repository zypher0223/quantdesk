export interface TradingAgentsReadiness {
  ready: boolean;
  runtime: { ready: boolean; version?: string | null; reason?: string | null; commit: string };
  profile: string;
  provider: string;
  models: { deep: string; quick: string };
  credential: { environmentVariable: string; present: boolean; issue?: string | null };
  target?: { symbol: string; asset_type: "stock" | "crypto"; analysts: string[]; fundamental_symbol?: string | null } | null;
  reason?: string | null;
}

export interface TradingAgentsResult {
  ok: boolean;
  runId: string;
  symbol: string;
  venue_symbol: string;
  display_symbol: string;
  trade_date: string;
  asset_type: "stock" | "crypto";
  analysts: string[];
  rating: string;
  requires_review: boolean;
  profile: string;
  reports: Record<string, string>;
  debates: Record<string, unknown>;
  meta: {
    provider?: string;
    deep_model?: string;
    quick_model?: string;
    debate_rounds?: number;
    risk_rounds?: number;
    market_data_source?: string;
    market_symbol?: string;
    fundamental_symbol?: string | null;
    duration_seconds?: number;
    generated_at?: string;
  };
  warnings?: string[];
  /** The external evidence this run read, in the same shape the research page uses. */
  externalEvidence?: TradingAgentsExternalEvidence | null;
}

export interface TradingAgentsCost {
  usd: number | null;
  usage: Record<string, { cacheHit: number; cacheMiss: number; output: number; total: number; calls: number }>;
  detail: {
    usd: number | null;
    peak?: boolean | null;
    unpriced: string[];
    models: Array<{ model: string; usd: number | null }>;
  } | null;
  budget: {
    allowed: boolean;
    problems: string[];
    dailyLimitUsd: number | null;
    perRunLimitUsd: number | null;
    spentTodayUsd: number | null;
    spentRunUsd: number | null;
    remainingTodayUsd: number | null;
    tokens: number;
  } | null;
}

export interface TradingAgentsDataProvenance {
  symbol: string;
  displaySymbol?: string;
  available: boolean;
  interval?: string;
  asOf?: string;
  tradeDate?: string;
  bars?: number;
  complete?: boolean;
  missingInSession?: number;
  sources?: Record<string, number>;
  stale?: boolean;
  staleByDays?: number;
  staleReason?: string;
  version?: string | null;
  reason?: string;
}

export interface TradingAgentsCoverage {
  requested: string[];
  covered: string[];
  missing: string[];
  complete: boolean;
  degraded: boolean;
  failed: boolean;
}

/** What one governed run left behind: the report and its receipt. */
/** Mirrors the research page's evidence summary: one shape, two research paths. */
export interface TradingAgentsExternalEvidence {
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
  records?: Array<{
    key: string; label: string; provider: string; endpoint: string; topic: string;
    asOf: string; publishedAt: string; observedAt: string; source: string;
  }>;
}

export interface TradingAgentsReceipt {
  runId: string;
  cost: TradingAgentsCost | null;
  data: TradingAgentsDataProvenance | null;
  analystCoverage: TradingAgentsCoverage | null;
  staleness: { stale: boolean; reason?: string; asOf?: string; tradeDate?: string } | null;
  durationS: number | null;
  retries: number | null;
  analystRetries: number | null;
  degraded: boolean | null;
  reused: boolean;
  failure: { type?: string; message?: string } | null;
  externalEvidence?: TradingAgentsExternalEvidence | null;
}

export interface TradingAgentsCostSummary {
  windowDays: number;
  totals: { runs: number; usd: number; unpricedRuns: number; failedRuns: number };
  today: { runs: number; usd: number; unpricedRuns: number; failedRuns: number };
  byModel: Record<string, { tokens: number; calls: number; usd: number | null }>;
  budgetState: { allowed: boolean; problems: string[]; remainingTodayUsd: number | null };
}

export type TradingAgentsJobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface TradingAgentsJob {
  id: string;
  venue_symbol: string;
  trade_date: string;
  analysts: string[];
  profile: string;
  status: TradingAgentsJobStatus;
  progress: string | null;
  result: (TradingAgentsResult & Partial<TradingAgentsReceipt>) | null;
  error: string | null;
  cancel_requested: boolean;
  created_ts: number;
  started_ts: number | null;
  finished_ts: number | null;
}

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const { timeoutMs = 1_900_000, ...rest } = init ?? {};
  let response: Response;
  try {
    response = await fetch(path, { ...rest, signal: AbortSignal.timeout(timeoutMs) });
  } catch (reason) {
    throw new Error(`无法连接 QuantDesk 引擎：${reason instanceof Error ? reason.message : String(reason)}`);
  }
  const text = await response.text();
  let payload: unknown;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const raw = (payload as { detail?: unknown } | null)?.detail;
    if (typeof raw === "string") {
      try {
        const detail = JSON.parse(raw) as { title?: string; detail?: string };
        throw new Error(`${detail.title ?? "TradingAgents 运行失败"}：${detail.detail ?? raw}`);
      } catch (reason) {
        if (reason instanceof SyntaxError) throw new Error(raw);
        throw reason;
      }
    }
    throw new Error(`TradingAgents 接口返回 ${response.status}`);
  }
  return payload as T;
}

export function fetchTradingAgentsReadiness(symbol: string): Promise<TradingAgentsReadiness> {
  return request(`/api/tradingagents/readiness?symbol=${encodeURIComponent(symbol)}`, { timeoutMs: 20_000 });
}

export function runTradingAgents(payload: { symbol: string; tradeDate: string }): Promise<TradingAgentsResult> {
  return request("/api/tradingagents/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function enqueueTradingAgents(payload: { symbol: string; tradeDate: string }): Promise<{ jobId: string; status: "queued" }> {
  return request("/api/tradingagents/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    timeoutMs: 30_000,
  });
}

export function fetchTradingAgentsJob(jobId: string): Promise<TradingAgentsJob> {
  return request(`/api/tradingagents/jobs/${encodeURIComponent(jobId)}`, { timeoutMs: 15_000 });
}

export function fetchTradingAgentsJobs(limit = 30): Promise<{ jobs: TradingAgentsJob[] }> {
  return request(`/api/tradingagents/jobs?limit=${limit}`, { timeoutMs: 15_000 });
}

/** What the paid research has spent, and what is left of today's allowance. */
export function fetchTradingAgentsCosts(days = 7): Promise<TradingAgentsCostSummary> {
  return request(`/api/tradingagents/costs?days=${days}`, { timeoutMs: 15_000 });
}

export function cancelTradingAgentsJob(jobId: string): Promise<TradingAgentsJob> {
  return request(`/api/tradingagents/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    timeoutMs: 15_000,
  });
}
