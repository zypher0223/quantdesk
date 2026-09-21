/** LLM settings and chart-screenshot analysis — all through the local gateway. */

export interface LlmErrorBody {
  kind: string;
  title: string;
  detail: string;
  action: string;
  status: number | null;
}

export interface LlmProfile {
  name: string;
  provider: string;
  baseUrl: string;
  apiKeyEnv: string;
  apiKeyCandidates: string[];
  hasKey: boolean;
  /** Present when a credential exists but is obviously unusable (e.g. a pasted placeholder). */
  credentialIssue: string | null;
  keyPresent: boolean;
  proxy: string;
  supportsVision: boolean;
  supportsJsonMode: boolean;
  models: { deep: string; quick: string; vision: string };
  /** Ceiling covering reasoning + answer; 0 means the driver default. */
  maxTokens: number;
}

export interface LlmSettings {
  profiles: LlmProfile[];
  roles: Record<string, string>;
  roleLabels: Record<string, string>;
  knownRoles: string[];
  configuredKeys: Record<string, boolean>;
  keysFile: string;
  /** False when the gateway's config directory cannot be written. */
  homeWritable: boolean;
}

export interface LlmTestResult {
  ok: boolean;
  profile: string;
  /** The name that was asked for; may differ from `model` if the provider substituted. */
  requestedModel?: string;
  model: string;
  substituted?: boolean;
  warning?: string | null;
  latencySeconds?: number;
  usage?: Record<string, number | null>;
  reply?: string;
  error?: LlmErrorBody;
}

export interface LlmStatus {
  roles: Array<{
    role: string;
    label: string;
    profile: string | null;
    ready: boolean;
    supportsVision: boolean;
    credentialIssue?: string | null;
  }>;
  visionProfile: string | null;
  canAnalyzeCharts: boolean;
}

export interface ChartLevel {
  price?: string;
  kind?: string;
  note?: string;
}

export interface ChartStructure {
  instrument?: { visible?: boolean; value?: string };
  timeframe?: { visible?: boolean; value?: string };
  trend?: string;
  structure?: string[];
  levels?: ChartLevel[];
  patterns?: string[];
  indicators_visible?: string[];
  uncertain?: string[];
  confidence?: string;
}

export interface ChartAnalysis {
  ok: boolean;
  profile: string;
  model: string;
  latencySeconds: number;
  usage: Record<string, number | null>;
  structured: ChartStructure | null;
  raw: string;
  imagePath: string;
  symbol: string | null;
  timeframe: string | null;
  disclaimer: string;
}

async function jsonRequest<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const { timeoutMs = 30_000, ...rest } = init ?? {};
  let response: Response;
  try {
    response = await fetch(path, { ...rest, signal: AbortSignal.timeout(timeoutMs) });
  } catch (reason) {
    throw new Error(`无法连接本地网关：${reason instanceof Error ? reason.message : String(reason)}`);
  }
  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!response.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    throw new Error(typeof detail === "string" ? detail : `网关返回 ${response.status}`);
  }
  return body as T;
}

export function fetchLlmSettings(): Promise<LlmSettings> {
  return jsonRequest<LlmSettings>("/api/llm/settings");
}

export function fetchLlmStatus(): Promise<LlmStatus> {
  return jsonRequest<LlmStatus>("/api/llm/status");
}

export function saveLlmKeys(updates: Record<string, string>): Promise<{ saved: string[]; keysFile: string }> {
  return jsonRequest("/api/llm/keys", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
}

export function saveLlmRoles(roles: Record<string, string>): Promise<{ roles: Record<string, string> }> {
  return jsonRequest("/api/llm/roles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(roles),
  });
}

export function testLlmProfile(name: string, model?: string): Promise<LlmTestResult> {
  return jsonRequest<LlmTestResult>(`/api/llm/profiles/${encodeURIComponent(name)}/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: model ?? null }),
    timeoutMs: 120_000,
  });
}

/** Persist the model names a profile uses. */
export function saveModelConfig(
  name: string,
  models: { deepModel?: string; quickModel?: string; visionModel?: string; maxTokens?: number },
): Promise<{ profile: string; models: { deep: string; quick: string; vision: string }; maxTokens: number; rolesUsingIt: string[] }> {
  return jsonRequest(`/api/llm/profiles/${encodeURIComponent(name)}/models-config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(models),
  });
}

export function listLlmModels(name: string): Promise<{ ok: boolean; models: string[]; error?: LlmErrorBody }> {
  return jsonRequest(`/api/llm/profiles/${encodeURIComponent(name)}/models`, {
    method: "POST",
    timeoutMs: 60_000,
  });
}

/** Read a screenshot with a vision-capable profile. The image never leaves the local gateway. */
export function analyzeChart(payload: {
  imageBase64: string;
  mimeType?: string;
  fileName?: string;
  symbol?: string;
  timeframe?: string;
  note?: string;
}): Promise<ChartAnalysis> {
  return jsonRequest<ChartAnalysis>("/api/llm/analyze-chart", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    timeoutMs: 180_000,
  });
}

/** FileReader gives a data URL; the gateway accepts that form directly. */
export function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error("读取图片失败"));
    reader.readAsDataURL(file);
  });
}
