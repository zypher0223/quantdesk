export interface SchedulerStatus {
  running: boolean;
  market: {
    status: "idle" | "running" | "error";
    lastRunAt: number | null;
    lastSuccessAt: number | null;
    lastError: string | null;
    lastResult: {
      symbol: string;
      candles: Record<string, number>;
      funding: number;
      openInterest: number;
      alerts?: { evaluated: number; triggered: number; unavailable: number; blocked: number };
    } | null;
  };
  dailyTradingAgents: { status: string; lastRunAt: number | null; lastError: string | null; queued: string[] };
  alerts: {
    status: "idle" | "running" | "error";
    lastRunAt: number | null;
    lastError: string | null;
    lastResult: { symbol: string; evaluated: number; triggered: number; unavailable: number; blocked: number } | null;
  };
  config: {
    marketCollectionEnabled: boolean;
    marketSymbolIntervalSec: number;
    marketBackfillBars: number;
    dailyTradingAgentsEnabled: boolean;
    dailyTradingAgentsTime: string;
    dailyTradingAgentsSymbols: string[];
  };
  nextMarketSymbol: string;
}

export type SchedulerConfig = SchedulerStatus["config"];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, signal: AbortSignal.timeout(180_000) });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(typeof body?.detail === "string" ? body.detail : `调度器返回 ${response.status}`);
  return body as T;
}

export function fetchSchedulerStatus(): Promise<SchedulerStatus> {
  return request("/api/scheduler/status");
}

export function runMarketCollection(symbol?: string): Promise<SchedulerStatus["market"]["lastResult"]> {
  const query = symbol ? `?symbol=${encodeURIComponent(symbol)}` : "";
  return request(`/api/scheduler/market/run${query}`, { method: "POST" });
}

export function saveSchedulerConfig(config: SchedulerConfig): Promise<SchedulerStatus> {
  return request("/api/scheduler/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}
