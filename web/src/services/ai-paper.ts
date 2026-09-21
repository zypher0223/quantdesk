import type { JournalEntry, PaperAccount, PositionView } from "./paper";

export type AiPaperStyle = "conservative" | "aggressive" | "gambler";
export type AiPaperHorizon = "short" | "swing";

export interface AiPaperProfile {
  id: string;
  name: string;
  enabled: boolean;
  initial_cash: number;
  max_leverage: number;
  horizon: AiPaperHorizon;
  style: AiPaperStyle;
  fib_only: boolean;
  symbols: string[];
  model_role: string;
  last_cycle_ts: number | null;
  last_bar_ts: number | null;
  last_error: string | null;
  /** Monotonic per profile; +1 for every real rule change the engine accepts. */
  config_revision: number;
}

export interface AiPaperDecision {
  id: number;
  cycle_ts: number;
  model_profile: string | null;
  model: string | null;
  action: "hold" | "open" | "close";
  symbol: string | null;
  side: "long" | "short" | null;
  leverage: number | null;
  notional: number | null;
  confidence: number | null;
  reason: string;
  lesson_applied: string;
  status: string;
  error: string | null;
  position_id: number | null;
  evidence?: {
    fibOnly?: boolean;
    evidenceUsed?: string[];
    agentsConsulted?: string[];
    agentConflicts?: string[];
    simulation?: AiSimulationCondition;
  };
}

export interface AiSimulationCondition {
  id: string;
  name: string;
  style: AiPaperStyle;
  styleLabel: string;
  horizon: AiPaperHorizon;
  horizonLabel: string;
  fibOnly: boolean;
  entryLabel: string;
  maxLeverage: number;
  /**
   * The rule revision this position (or decision) was taken under, and the symbol
   * set that revision covered. A profile can be re-configured while it runs, so a
   * position opened earlier must still say which rules it actually came from.
   */
  configRevision: number;
  symbols: string[];
}

export interface AiPaperProfileSummary {
  profile: AiPaperProfile;
  metrics: AiPaperSnapshot["metrics"];
  equity: number;
  openPositions: number;
}

export type AiPaperConfigPayload = {
  name: string;
  initialCash: number;
  maxLeverage: number;
  horizon: AiPaperHorizon;
  style: AiPaperStyle;
  fibOnly: boolean;
  symbols: string[];
};

export interface AiPaperSnapshot {
  simulationOnly: true;
  profile: AiPaperProfile;
  policy: {
    label: string;
    max_leverage: number;
    risk_fraction: number;
    max_notional_fraction: number;
    max_open_positions: number;
    min_confidence: number;
  };
  feePolicy: {
    benchmark: string;
    tier: string;
    product: string;
    fillType: "taker";
    takerFeeBps: number;
    feeRatePct: number;
    chargedOn: Array<"open" | "close">;
    formula: string;
    effectiveDate: string | null;
  };
  account: PaperAccount;
  metrics: {
    closedTrades: number;
    wins: number;
    losses: number;
    winRate: number;
    returnPct: number;
    netPnl: number;
    realizedNetPnl: number;
    maxDrawdownPct: number;
  };
  journal: JournalEntry[];
  decisions: AiPaperDecision[];
  memory: { path: string; updatedAt: number | null; content: string; modelIndependent: true };
}

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const { timeoutMs = 180_000, ...rest } = init ?? {};
  let response: Response;
  try {
    response = await fetch(path, { ...rest, signal: AbortSignal.timeout(timeoutMs) });
  } catch (reason) {
    throw new Error(`无法连接本地引擎：${reason instanceof Error ? reason.message : String(reason)}`);
  }
  const text = await response.text();
  let body: unknown = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = null; }
  if (!response.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    throw new Error(typeof detail === "string" ? detail : `引擎返回 ${response.status}`);
  }
  return body as T;
}

const profilePath = (id: string) => `/api/ai-paper/profiles/${encodeURIComponent(id)}`;

export const fetchAiPaperProfiles = () => request<{ profiles: AiPaperProfileSummary[] }>("/api/ai-paper/profiles");
export const fetchAiPaper = (id = "default") => request<AiPaperSnapshot>(profilePath(id));
export const createAiPaper = (payload: AiPaperConfigPayload) =>
  request<AiPaperSnapshot>("/api/ai-paper/profiles", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
export const saveAiPaperConfig = (id: string, payload: AiPaperConfigPayload) =>
  request<AiPaperProfile>(`${profilePath(id)}/config`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
/**
 * Start a profile.
 *
 * With a payload the call is **atomic**: the engine saves these rules, starts the
 * profile, and answers with the full snapshot it will actually run - so the page never
 * has to save, then start, then hope the two agreed. With no payload it stays the
 * legacy call that only flips the switch and answers with the profile.
 */
export function startAiPaper(id: string): Promise<AiPaperProfile>;
export function startAiPaper(id: string, payload: AiPaperConfigPayload): Promise<AiPaperSnapshot>;
export function startAiPaper(id: string, payload?: AiPaperConfigPayload): Promise<AiPaperProfile | AiPaperSnapshot> {
  if (!payload) return request<AiPaperProfile>(`${profilePath(id)}/start`, { method: "POST" });
  return request<AiPaperSnapshot>(`${profilePath(id)}/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}
export const stopAiPaper = (id: string) => request<AiPaperProfile>(`${profilePath(id)}/stop`, { method: "POST" });
export const runAiPaperNow = (id: string) => request<Record<string, unknown>>(`${profilePath(id)}/run`, { method: "POST", timeoutMs: 300_000 });
export const closeAiPaperPosition = (profileId: string, positionId: number) => request(`${profilePath(profileId)}/positions/${positionId}/close`, { method: "POST" });
export const resetAiPaper = (id: string) => request<{ ok: boolean }>(`${profilePath(id)}/reset?confirm=true`, { method: "POST" });
export const deleteAiPaper = (id: string) => request<{ ok: boolean }>(`${profilePath(id)}?confirm=true`, { method: "DELETE" });
export const aiPaperExportUrl = (id: string, format: "json" | "csv" | "md") => `${profilePath(id)}/export?format=${format}`;
