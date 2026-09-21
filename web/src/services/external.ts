/** External research (OpenBB) and analytics (Fincept) status, evidence and risk. */

export interface ExternalProviderStatus {
  capability: "research_tool" | "analytics";
  configured: boolean;
  pluginId: string | null;
  installed: string[];
  enabled: string[];
  invalid: Array<{ pluginId?: string; error: string }>;
  healthy: boolean | null;
  runtime: {
    transport?: string;
    runtimeReady?: boolean;
    credentialPresent?: boolean;
    baseUrl?: string;
    note?: string;
    topics?: string[];
    endpoints?: Record<string, string>;
  } | null;
  error: string;
}

export interface ExternalEvidenceRow {
  symbol: string;
  topic: string;
  provider: string;
  status: "ok" | "unavailable" | "rejected" | "error";
  asOf: string | null;
  publishedAt: string | null;
  observedAt: string;
  warning: string | null;
  updatedTs: number;
}

export interface ExternalStatus {
  external: {
    openbbEnabled: boolean;
    finceptEnabled: boolean;
    timeoutSeconds: number | null;
    maxRetries: number | null;
    backoffBaseSeconds: number | null;
    maxBackoffSeconds: number | null;
    requestsPerMinute: number | null;
    cacheTtlMinutes: Record<string, number>;
    retentionDays: Record<string, number>;
  };
  providers: {
    openbb: Record<string, string>;
    fallbacks: Record<string, string[]>;
    pointInTime: { default?: boolean; exempt?: string[] };
  };
  plugins: { research: ExternalProviderStatus; analytics: ExternalProviderStatus };
  evidence: {
    rows: number;
    cacheHitRate: number | null;
    byProvider: Array<{ provider: string; status: string; total: number; newest: number }>;
    recent: ExternalEvidenceRow[];
  };
  analytics: {
    calls: number;
    rate: { ok: number; error: number; averageMs: number | null; samples: number; lastError: { at: number; error: string } | null };
    recent: Array<{
      kind: string;
      provider: string;
      status: string;
      marketVersion: string | null;
      durationMs: number | null;
      error: string | null;
      updatedTs: number;
    }>;
  };
  licences: Record<string, { name: string; licence: string; mode: string; note: string }>;
  checkedAt: number;
}

export interface ExternalEvidenceDetail {
  symbol: string;
  mapping: { mapping: Record<string, unknown>; referenceOptional: boolean };
  providers: Record<string, string>;
  pointInTime: { default?: boolean; exempt?: string[] };
  evidence: Array<{
    topic: string;
    provider: string;
    endpoint: string;
    status: string;
    asOf: string | null;
    publishedAt: string | null;
    observedAt: string;
    expiresAt: string | null;
    source: string | null;
    contentHash: string | null;
    pointInTime: boolean;
    warning: string | null;
    stale: boolean;
  }>;
}

export interface ExternalScenario {
  id: string;
  label: string;
  description: string;
}

export interface PortfolioSnapshotView {
  asOf: string;
  baseCurrency: string;
  marketVersion: string;
  equity: number;
  totalNotional: number;
  grossExposure: number;
  netExposure: number;
  marginUsed: number;
  warnings: string[];
  positions: Array<{ symbol: string; group: string; side: string; notional: number; margin: number }>;
}

export interface AnalyticsOutcome {
  ok: boolean;
  kind: "portfolio" | "scenario";
  provider: string;
  result: Record<string, any> | null;
  cached: boolean;
  unavailable: string;
  error: string;
  requestId: string;
  durationMs: number;
  marketVersion: string;
  inputHash: string;
  warnings: string[];
  snapshot: PortfolioSnapshotView | null;
  advisory?: string;
}

export interface ProviderTestResult {
  capability: string;
  enabled: boolean;
  plugin: ExternalProviderStatus;
  paid: boolean;
  called: boolean;
  outcome: string;
  note?: string;
}

async function jsonRequest<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const { timeoutMs = 30_000, ...rest } = init ?? {};
  let response: Response;
  try {
    response = await fetch(path, { ...rest, signal: AbortSignal.timeout(timeoutMs) });
  } catch (reason) {
    throw new Error(`无法连接 QuantDesk 引擎：${reason instanceof Error ? reason.message : String(reason)}`);
  }
  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const detail = (payload as { detail?: unknown } | null)?.detail;
    throw new Error(typeof detail === "string" ? detail : `外部接口返回 ${response.status}`);
  }
  return payload as T;
}

export function fetchExternalStatus(): Promise<ExternalStatus> {
  return jsonRequest<ExternalStatus>("/api/external/status", { timeoutMs: 20_000 });
}

export function saveExternalSettings(payload: {
  openbbEnabled?: boolean;
  finceptEnabled?: boolean;
  timeoutSeconds?: number;
  maxRetries?: number;
  requestsPerMinute?: number;
  cacheTtlMinutes?: Record<string, number>;
  retentionDays?: Record<string, number>;
  providers?: Record<string, string>;
  fallbacks?: Record<string, string[]>;
}): Promise<{ saved: boolean; configFile: string; status: ExternalStatus }> {
  return jsonRequest("/api/external/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function testExternalProvider(payload: {
  capability: "research_tool" | "analytics";
  topic?: string;
  symbol?: string;
  allowPaid?: boolean;
}): Promise<ProviderTestResult> {
  return jsonRequest("/api/external/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    timeoutMs: 90_000,
  });
}

export function fetchExternalEvidence(symbol: string): Promise<ExternalEvidenceDetail> {
  return jsonRequest<ExternalEvidenceDetail>(
    `/api/external/evidence?symbol=${encodeURIComponent(symbol)}`,
    { timeoutMs: 20_000 },
  );
}

export function fetchScenarios(): Promise<{ scenarios: ExternalScenario[] }> {
  return jsonRequest("/api/external/scenarios", { timeoutMs: 15_000 });
}

export function runPortfolioRisk(payload: {
  confidence?: number;
  optimize?: boolean;
  force?: boolean;
}): Promise<AnalyticsOutcome> {
  return jsonRequest<AnalyticsOutcome>("/api/external/portfolio-risk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    timeoutMs: 120_000,
  });
}

export function runScenario(payload: {
  scenario: string;
  confidence?: number;
  shocks?: Record<string, number>;
  force?: boolean;
}): Promise<AnalyticsOutcome> {
  return jsonRequest<AnalyticsOutcome>("/api/external/scenario", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    timeoutMs: 120_000,
  });
}
