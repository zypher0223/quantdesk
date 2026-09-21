import type { Candle, Instrument, Timeframe } from "../data/market";
import { queuedTransferOf, type StudyOutcome } from "./study-queue";

/**
 * One strategy's parameters as the engine accepts them.
 *
 * The engine's request model is a free-form dict, and CPA's `entryStages` is a list of
 * phase ids, so the type has to admit an array; anything outside this union would not
 * survive `JSON.stringify` into a study request anyway.
 */
export type StrategyParams = Record<string, number | string | boolean | string[]>;

export interface BacktestConfig {
  strategyId: string;
  strategyParams: StrategyParams;
  fastPeriod: number;
  slowPeriod: number;
  direction: "both" | "long";
  initialCapital: number;
  allocationPct: number;
  feeBps: number | null;
  slippageBps: number | null;
  leverage: number;
  maintenanceMarginRate: number;
  includeFunding: boolean;
  includeLiquidation: boolean;
  fillOnThin: "skip" | "allow";
}

export interface BacktestTrade {
  direction: "多" | "空";
  entry_time: number;
  exit_time: number;
  entry_price: number;
  exit_price: number;
  quantity: number;
  notional: number;
  gross_pnl: number;
  funding_paid: number;
  fees: number;
  net_pnl: number;
  return_pct: number;
  bars_held: number;
  exit_reason: string;
  liquidated: boolean;
  thin_entry: boolean;
  /** Which venue risk rung this trade was margined in, when a ladder was used. */
  risk_tier_id: number | null;
  maintenance_margin_rate: number | null;
}

export interface RiskTier {
  tierId: number;
  maxLeverage: number;
  maintenanceMarginRate: number;
  riskLimitValue: number;
  mmDeduction: number;
  lowestRisk: boolean;
}

export interface RiskProfile {
  venueSymbol: string;
  source: string;
  syncedAt: number | null;
  tiers: RiskTier[];
  maxLeverage: number | null;
  minMaintenanceMarginRate: number | null;
}

export interface BacktestRisk {
  tiered: boolean;
  maxLeverageAllowed: number | null;
  minMaintenanceMarginRate: number | null;
  liquidations: Array<{
    time: number;
    tierId: number | null;
    maintenanceMarginRate: number;
    maintenanceMargin: number;
    liqPrice: number;
    loss: number;
  }>;
  fundingSettlements: Array<{ ts: number; rate: number; price: number; cost: number }>;
  tradedTiers: number[];
}

/** The gate's verdict, as every formal study reports it. */
export interface StudyReadiness {
  symbol: string;
  interval: string;
  fromTs: number;
  toTs: number;
  ok: boolean;
  degraded: boolean;
  proxyData: boolean;
  blocking: Array<{ key: string; label: string; detail: string; status: string }>;
  checks: Array<{ key: string; label: string; status: string; detail: string; values: Record<string, unknown> }>;
  versions: Record<string, string>;
  missing: string[];
  impacts: string[];
}

/** What a study read, and on what assumptions: present on every formal result. */
export interface StudyEnvelope {
  readRange: { interval: string; fromTs: number; toTs: number; bars: number; expectedBars: number };
  versions: Record<string, string | null>;
  readiness: StudyReadiness | null;
  dataReady: boolean;
  degraded: boolean;
  missingData: string[];
  dataImpacts: string[];
  costModel: Record<string, unknown>;
  strategyVersion: Record<string, unknown>;
}

export interface BacktestResult extends StudyEnvelope {
  config: Record<string, unknown>;
  instrument: Record<string, unknown>;
  initial_capital: number;
  final_equity: number;
  net_return_pct: number;
  max_drawdown_pct: number;
  win_rate_pct: number;
  profit_factor: number | null;
  total_fees: number;
  total_funding: number;
  trades: BacktestTrade[];
  equity_curve: Array<{ time: number; equity: number; marginRatio: number | null }>;
  warnings: string[];
  assumptions: string[];
  data_quality: Record<string, number | string | null>;
  /** Present when the venue ladder was used or an empty profile was returned. */
  riskProfile?: RiskProfile | null;
  risk?: BacktestRisk;
}

export const DEFAULT_BACKTEST_CONFIG: BacktestConfig = {
  strategyId: "ma_cross",
  strategyParams: { fastPeriod: 9, slowPeriod: 21 },
  fastPeriod: 9,
  slowPeriod: 21,
  direction: "both",
  initialCapital: 10_000,
  allocationPct: 50,
  feeBps: null,
  slippageBps: null,
  leverage: 1,
  maintenanceMarginRate: 0.005,
  includeFunding: true,
  includeLiquidation: true,
  fillOnThin: "skip",
};

export interface StrategyParameter {
  key: string;
  label: string;
  type: "integer" | "number" | "boolean" | "select";
  default: number | string | boolean;
  minimum?: number | null;
  maximum?: number | null;
  options?: string[];
}

export interface StrategyInfo {
  id: string;
  name: string;
  description: string;
  source: "builtin" | "plugin";
  plugin_id?: string | null;
  parameters: StrategyParameter[];
}

export async function fetchStrategies(): Promise<{ strategies: StrategyInfo[]; pluginErrors: Array<{ pluginId: string; error: string }> }> {
  const response = await fetch("/api/strategies", { signal: AbortSignal.timeout(30_000) });
  if (!response.ok) throw new Error(`策略目录返回 ${response.status}`);
  return response.json();
}

/**
 * The exact study body the engine accepts.
 *
 * Both the synchronous run and a queued `submitRun("backtest", …)` submit this
 * object, so a background task reproduces the foreground run byte for byte.
 */
export function backtestRequestBody(
  instrument: Instrument,
  timeframe: Timeframe,
  config: BacktestConfig,
  candles?: Candle[],
): Record<string, unknown> {
  const useUploaded = Boolean(candles && candles.length >= 30);
  return {
    symbol: instrument.venueSymbol,
    timeframe,
    ...config,
    ...(useUploaded ? { candles: candles!.map((bar) => ({ time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: bar.volume })) } : {}),
  };
}

/**
 * Run the rule on the engine.
 *
 * The engine owns the algorithm so the CLI and this page cannot drift apart;
 * the browser only supplies parameters and, when the user loaded a file, the
 * candles themselves. A study too large to answer inline is transferred to the
 * run queue instead of being refused, and the outcome says so — the caller must
 * never render that as a finished backtest.
 */
export async function runBacktest(
  instrument: Instrument,
  timeframe: Timeframe,
  config: BacktestConfig,
  candles?: Candle[],
): Promise<StudyOutcome<BacktestResult>> {
  // One error path for every study: a 409 carries the gate's verdict, so the page
  // can say which data is missing instead of showing a raw JSON body.
  return post<BacktestResult>("/api/backtest", backtestRequestBody(instrument, timeframe, config, candles), 120_000);
}

/** Parameter search plus walk-forward, over the same gated local window. */
export async function runValidation(body: Record<string, unknown>): Promise<StudyOutcome<StudyEnvelope & Record<string, unknown>>> {
  return post("/api/validate", body, 600_000);
}

/** One strategy across several contracts, each read over the common window. */
export async function runPortfolio(body: Record<string, unknown>): Promise<StudyOutcome<StudyEnvelope & Record<string, unknown>>> {
  return post("/api/portfolio", body, 600_000);
}

async function post<T>(path: string, body: Record<string, unknown>, timeoutMs: number): Promise<StudyOutcome<T>> {
  let response: Response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
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
  // 202 with a run means the engine queued this study: the answer is a transfer,
  // not a result, and is reported as such before anything else looks at it.
  const queued = queuedTransferOf(response.status, payload);
  if (queued) return { queued: true, ...queued };
  if (!response.ok) {
    // A 409 carries the gate's verdict: say what is missing, not just "failed".
    const detail = (payload as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") {
      try {
        const parsed = JSON.parse(detail) as { title?: string; detail?: string; action?: string };
        throw new Error([parsed.title ?? "数据未就绪", parsed.detail, parsed.action].filter(Boolean).join("："));
      } catch (reason) {
        if (reason instanceof Error && reason.message !== "Unexpected end of JSON input") throw reason;
        throw new Error(detail);
      }
    }
    throw new Error(`引擎返回 ${response.status}`);
  }
  return { queued: false, study: payload as T };
}

export const EXIT_REASON_LABEL: Record<string, string> = {
  signal: "信号平仓",
  liquidation: "强制平仓",
  end_of_data: "数据末尾结算",
  manual: "手动平仓",
};
