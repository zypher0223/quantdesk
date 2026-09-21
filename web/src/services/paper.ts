/** Paper trading, positions, and the append-only journal. */

export interface PositionView {
  id: number;
  venue: string;
  symbol: string;
  display_symbol: string;
  side: "long" | "short";
  qty: number;
  entry_price: number;
  leverage: number;
  liq_price: number | null;
  mark_price: number | null;
  notional: number;
  margin: number;
  unrealized_pnl: number | null;
  unrealized_pct: number | null;
  margin_ratio: number | null;
  distance_to_liq_pct: number | null;
  opened_ts: number;
  notes: string;
  fees_paid: number;
  protective_orders: Array<{
    id: number;
    type: "stop_loss" | "take_profit_1" | "take_profit_2";
    trigger_price: number;
    close_fraction: number;
    status: "open" | "filled" | "canceled";
  }>;
}

export interface PaperAccount {
  cash: number;
  initial_cash: number;
  realized_pnl: number;
  unrealized_pnl: number;
  fees_paid: number;
  funding_paid: number;
  funding_received: number;
  funding_net: number;
  equity: number;
  margin_used: number;
  free_margin: number;
  positions: PositionView[];
  warnings: string[];
  mark_prices_at: number | null;
}

export interface JournalEntry {
  id: number;
  position_id: number | null;
  opened_ts: number;
  closed_ts: number;
  venue: string;
  symbol: string;
  side: "long" | "short";
  qty: number;
  entry_price: number;
  exit_price: number;
  leverage: number;
  liq_price: number | null;
  gross_pnl: number;
  funding_paid: number;
  fees: number;
  net_pnl: number;
  exit_reason: string;
  rationale: string | null;
  source: string;
  entry_hash: string | null;
}

export interface JournalResponse {
  entries: JournalEntry[];
  integrity: { entries: number; tampered: number[]; intact: boolean };
  immutable: boolean;
}

async function request<T>(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<T> {
  const { timeoutMs = 60_000, ...rest } = init ?? {};
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
    const detail = (payload as { detail?: unknown } | null)?.detail;
    throw new Error(typeof detail === "string" ? detail : `引擎返回 ${response.status}`);
  }
  return payload as T;
}

export function fetchPaperAccount(): Promise<PaperAccount> {
  return request<PaperAccount>("/api/paper/account");
}

export function openPosition(payload: {
  symbol: string;
  side: "long" | "short";
  notional?: number;
  qty?: number;
  leverage: number;
  rationale?: string;
  stopLoss?: number;
  takeProfit1?: number;
  takeProfit2?: number;
}): Promise<PositionView> {
  return request<PositionView>("/api/paper/positions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function closePosition(positionId: number, reason = "manual"): Promise<Record<string, number | string>> {
  return request(`/api/paper/positions/${positionId}/close`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
}

export function savePositionNote(positionId: number, notes: string): Promise<{ position_id: number; notes: string }> {
  return request(`/api/paper/positions/${positionId}/note`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes }),
  });
}

export function fetchJournal(symbol?: string, limit = 200): Promise<JournalResponse> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (symbol) query.set("symbol", symbol);
  return request<JournalResponse>(`/api/journal?${query}`);
}

/** CSV/JSON export of the immutable journal, including its integrity report. */
export function journalExportUrl(format: "json" | "csv", symbol?: string): string {
  const query = new URLSearchParams({ format });
  if (symbol) query.set("symbol", symbol);
  return `/api/journal/export?${query}`;
}

export function resetPaper(): Promise<{ ok: boolean; cash: number }> {
  return request("/api/paper/reset", { method: "POST" });
}

export const EXIT_REASON_LABEL: Record<string, string> = {
  manual: "手动平仓",
  signal: "信号平仓",
  liquidation: "强制平仓",
  stop_loss: "止损触发",
  take_profit_1: "止盈1触发",
  take_profit_2: "止盈2触发",
  end_of_data: "数据末尾",
};
