import type { StrategyParams } from "./backtest";
export interface AlertCondition {
  id: string;
  label: string;
  unit: string;
  operator: "above" | "below";
  timeframe: boolean;
  strategy?: boolean;
}

export interface AlertRuleCondition {
  id?: string;
  conditionType: string;
  timeframe: string | null;
  threshold: number | null;
  strategyId?: string | null;
  strategyParameters?: StrategyParams;
  signalDirection?: "any" | "long" | "short";
  lastMetric?: number | null;
  lastObservedAt?: number | null;
  lastMet?: boolean;
}

export interface AlertRule {
  id: string;
  name: string;
  venueSymbol: string;
  conditionType: string;
  timeframe: string | null;
  threshold: number;
  conditions: AlertRuleCondition[];
  cooldownSeconds: number;
  enabled: boolean;
  severity: "info" | "warning" | "critical";
  quietStart: string | null;
  quietEnd: string | null;
  timezone: string;
  dailyLimit: number;
  confirmationCount: number;
  consecutiveCount: number;
  hysteresis: number;
  armed: boolean;
  lastCondition: boolean;
  lastMetric: number | null;
  lastObservedAt: number | null;
  lastEvaluatedAt: number | null;
  lastTriggeredAt: number | null;
  createdAt: number;
  updatedAt: number;
}

export interface AlertEvent {
  id: string;
  ruleId: string;
  venueSymbol: string;
  conditionType: string;
  timeframe: string | null;
  metric: number;
  threshold: number;
  observedAt: number;
  triggeredAt: number;
  title: string;
  message: string;
  notificationResults: Array<{ pluginId?: string; ok?: boolean; error?: string }>;
}

export interface AlertCatalog {
  conditions: AlertCondition[];
  instruments: Array<{ venueSymbol: string; displaySymbol: string; name: string }>;
  rules: AlertRule[];
  events: AlertEvent[];
}

export interface AlertRuleInput {
  name: string;
  venueSymbol: string;
  conditions: AlertRuleCondition[];
  cooldownSeconds: number;
  enabled: boolean;
  severity: "info" | "warning" | "critical";
  quietStart: string | null;
  quietEnd: string | null;
  timezone: string;
  dailyLimit: number;
  confirmationCount: number;
  hysteresis: number;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, signal: AbortSignal.timeout(180_000) });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(typeof body?.detail === "string" ? body.detail : `告警服务返回 ${response.status}`);
  return body as T;
}

export function fetchAlerts(): Promise<AlertCatalog> {
  return request("/api/alerts");
}

export function createAlertRule(input: AlertRuleInput): Promise<AlertRule> {
  return request("/api/alerts/rules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function updateAlertRule(rule: AlertRule, changes: Partial<AlertRuleInput>): Promise<AlertRule> {
  const input: AlertRuleInput = {
    name: rule.name,
    venueSymbol: rule.venueSymbol,
    conditions: rule.conditions,
    cooldownSeconds: rule.cooldownSeconds,
    enabled: rule.enabled,
    severity: rule.severity,
    quietStart: rule.quietStart,
    quietEnd: rule.quietEnd,
    timezone: rule.timezone,
    dailyLimit: rule.dailyLimit,
    confirmationCount: rule.confirmationCount,
    hysteresis: rule.hysteresis,
    ...changes,
  };
  return request(`/api/alerts/rules/${encodeURIComponent(rule.id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function deleteAlertRule(ruleId: string): Promise<{ id: string; removed: boolean }> {
  return request(`/api/alerts/rules/${encodeURIComponent(ruleId)}`, { method: "DELETE" });
}

export function evaluateAlerts(symbol: string): Promise<{ evaluated: number; triggered: AlertEvent[]; unavailable: string[]; blocked: Array<{ ruleId: string; reasons: string[] }> }> {
  return request(`/api/alerts/evaluate?symbol=${encodeURIComponent(symbol)}`, { method: "POST" });
}

export function createStrategyAlert(input: {
  venueSymbol: string;
  timeframe: string;
  strategyId: string;
  strategyParameters: StrategyParams;
  signalDirection?: "any" | "long" | "short";
  name?: string;
}): Promise<AlertRule> {
  return request("/api/alerts/from-strategy", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}
