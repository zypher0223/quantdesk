export type QualityState = "healthy" | "degraded" | "critical";

export interface FrameQuality {
  timeframe: "15m" | "1h" | "4h" | "1d";
  status: "fresh" | "delayed" | "stale" | "missing";
  statusLabel: string;
  signalEligible: boolean;
  bars: number;
  latestOpenAt: number | null;
  expectedOpenAt: number;
  lagBars: number | null;
  gaps: number;
  recentGap: boolean;
  duplicates: number;
  invalidBars: number;
  priceOutliers: number;
  volumeOutliers: number;
  recentPriceOutlier: boolean;
  recentVolumeOutlier: boolean;
  issueCount: number;
}

export interface InstrumentQuality {
  venueSymbol: string;
  displaySymbol: string;
  name: string;
  state: QualityState;
  signalEligible: boolean;
  blockingReasons: string[];
  issueCount: number;
  frames: FrameQuality[];
  checkedAt: number;
}

export interface MonitoringOverview {
  summary: { healthy: number; degraded: number; critical: number; total: number; signalEligible: number };
  instruments: InstrumentQuality[];
  provider: {
    windowHours: number;
    total: number;
    failed: number;
    retried: number;
    rateLimited: number;
    averageLatencyMs: number;
    recent: Array<{
      provider: string;
      operation: string;
      symbol: string | null;
      status: string;
      http_status: number | null;
      attempt: number;
      duration_ms: number;
      error: string | null;
      created_ts: number;
    }>;
  };
  components: Record<string, Record<string, unknown> | null>;
  checkedAt: number;
}

/**
 * Live market link as reported by the in-process engine service. It is a real
 * WebSocket state, not a REST poll: `lastMessageAgeMs` is the number a desk
 * watches when deciding whether a quote is still trustworthy.
 */
export interface MarketDataComponent {
  status?: string;
  running?: boolean;
  startedAt?: number | null;
  stoppedAt?: number | null;
  symbols?: number;
  intervals?: string[];
  restoredSnapshots?: number;
  closedEvents?: number;
  duplicateClosed?: number;
  backfills?: number;
  lastBackfillAt?: number | null;
  backfillErrors?: string[];
  staleAfterMs?: number;
  /** The rotation heartbeat while the collector is in derivatives-only mode. */
  lastDerivativesAt?: number | null;
  lastDerivativesSymbol?: string | null;
  /** Present on the scheduler component: how the rotation is configured. */
  config?: Record<string, unknown>;
  connection?: {
    state?: string;
    connected?: boolean;
    reconnects?: number;
    lastMessageAt?: number | null;
    lastMessageAgeMs?: number | null;
    lastSuccessAt?: number | null;
    lastSuccessAgeMs?: number | null;
    lastError?: string | null;
    proxyConfigured?: boolean;
    streams?: Array<{
      name: string;
      state: string;
      topics: number;
      attempt: number;
      reconnects: number;
      messagesReceived: number;
      lastMessageAt: number | null;
      lastError: string | null;
    }>;
  };
}

export async function fetchMonitoring(): Promise<MonitoringOverview> {
  const response = await fetch("/api/monitoring", { signal: AbortSignal.timeout(30_000) });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(typeof body?.detail === "string" ? body.detail : `监控服务返回 ${response.status}`);
  return body as MonitoringOverview;
}
