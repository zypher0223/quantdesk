/** External repository plugin catalog and lifecycle. */

export type PluginCapability =
  | "data_provider"
  | "strategy"
  | "research_tool"
  | "notifier"
  // v2: portfolio analytics (Fincept)
  | "analytics"
  // v3: factor research and backtest validation
  | "factor_provider"
  | "backtest_validator"
  // v4: strategy proposal/agent capability
  | "strategy_agent";

/** Protocol versions the engine accepts. A plugin declares the one it speaks. */
export const SUPPORTED_PLUGIN_API_VERSIONS = ["1", "2", "3", "4"] as const;

export interface PluginInfo {
  id: string;
  name: string;
  version: string;
  api_version: string;
  description: string;
  homepage: string;
  capabilities: PluginCapability[];
  command: string[];
  timeout_seconds: number;
  required_env: string[];
  network: boolean;
  requirements_file: string;
  required_executables: string[];
  path: string;
  enabled: boolean;
  origin: { kind?: string; source?: string; ref?: string; commit?: string };
  dependencies: PluginDependencyStatus;
  sandbox: PluginSandboxStatus;
  valid: true;
}

export interface PluginSandboxStatus {
  policy: "required" | "preferred" | "off";
  backend: string | null;
  available: boolean;
  enforced: boolean;
  detail: string;
}

export interface PluginDependencyStatus {
  declared: boolean;
  requirementsFile: string | null;
  requirementsSha256: string | null;
  installedSha256: string | null;
  runtimePath: string;
  missingExecutables: string[];
  needsInstall: boolean;
  ready: boolean;
  problems: string[];
  sandbox: PluginSandboxStatus;
}

export interface PluginCatalog {
  apiVersion: string;
  supportedCapabilities: PluginCapability[];
  supportedApiVersions?: string[];
  pluginRoot: string;
  homeWritable: boolean;
  sandbox: PluginSandboxStatus;
  plugins: PluginInfo[];
  invalid: Array<{ id: string; path: string; error: string }>;
}

export interface PluginHealth {
  pluginId: string;
  method: string;
  latencyMs: number;
  result: { ok?: boolean; message?: string; [key: string]: unknown };
}

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const { timeoutMs = 60_000, ...rest } = init ?? {};
  let response: Response;
  try {
    response = await fetch(path, { ...rest, signal: AbortSignal.timeout(timeoutMs) });
  } catch (reason) {
    throw new Error(`无法连接插件服务：${reason instanceof Error ? reason.message : String(reason)}`);
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
    throw new Error(typeof detail === "string" ? detail : `插件服务返回 ${response.status}`);
  }
  return payload as T;
}

export function fetchPlugins(): Promise<PluginCatalog> {
  return request<PluginCatalog>("/api/plugins");
}

export function installPlugin(source: string, ref?: string): Promise<PluginInfo> {
  return request<PluginInfo>("/api/plugins/install", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source, ref: ref?.trim() || null }),
    timeoutMs: 200_000,
  });
}

export function setPluginEnabled(pluginId: string, enabled: boolean): Promise<PluginInfo> {
  return request<PluginInfo>(`/api/plugins/${encodeURIComponent(pluginId)}/enabled`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
}

export function checkPlugin(pluginId: string): Promise<PluginHealth> {
  return request<PluginHealth>(`/api/plugins/${encodeURIComponent(pluginId)}/health`, {
    method: "POST",
  });
}

export function updatePlugin(pluginId: string, ref?: string): Promise<{ plugin: PluginInfo; previousVersion: string; changed: boolean; reviewRequired: boolean }> {
  return request(`/api/plugins/${encodeURIComponent(pluginId)}/update`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref: ref?.trim() || null }),
    timeoutMs: 200_000,
  });
}

export function installPluginDependencies(pluginId: string): Promise<PluginDependencyStatus> {
  return request(`/api/plugins/${encodeURIComponent(pluginId)}/dependencies/install`, {
    method: "POST",
    timeoutMs: 700_000,
  });
}

export function uninstallPlugin(pluginId: string, purgeData = false): Promise<{ id: string; removed: boolean; dataRemoved: boolean; cleanupPending: boolean }> {
  return request(`/api/plugins/${encodeURIComponent(pluginId)}?purgeData=${purgeData ? "true" : "false"}`, {
    method: "DELETE",
  });
}
