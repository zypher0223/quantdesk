/**
 * Agent campaigns: the pre-registered search, and the rules a page must not soften.
 *
 * Three rules are enforced here rather than left to a component's good manners:
 *
 * * **the test segment is invisible until it is unsealed** - `visibleTrials` never
 *   returns a `test` row unless the campaign says it was unsealed, so a table that
 *   renders what it is given cannot leak it;
 * * **promotion needs evidence** - `promotionState` mirrors the server's guard: a
 *   proposal with no validation reading is not promotable, and the button says why
 *   instead of being quietly disabled;
 * * **a missing figure is not a zero** - every metric goes through the same
 *   formatters the result centre uses, so an unmeasured Sharpe prints as "—".
 */

import { GatewayError } from "./api";
import { countText, decimalText, drawdownText, percentText } from "./run-figures";

/* ------------------------------------------------------------------- types */

export type CampaignStatus =
  | "preregistered"
  | "running"
  | "completed"
  | "budget_limited"
  | "compliance_blocked"
  | "failed";

export interface CampaignWindow {
  train: [number, number];
  validation: [number, number];
  test: [number, number];
}

export interface Campaign {
  uid: string;
  provider: string;
  agentVersion: string;
  mode: string;
  group: string;
  interval: string;
  horizonBars: number;
  universe: string[];
  factorSpace: Array<{ factorId: string; tier: string; family: string }>;
  hypothesis: string;
  successCriteria: string;
  windows: CampaignWindow;
  testSealed: boolean;
  testUnsealedTs: number | null;
  budget: { proposalsPerRound?: number; maxRounds?: number; roundDeadlineMs?: number };
  status: CampaignStatus;
  stopReason: string;
  roundsUsed: number;
  trialsUsed: number;
  proposalsUsed: number;
  seed: number;
  createdTs: number;
  finishedTs: number | null;
}

export interface CampaignProposal {
  proposalId: string;
  round: number;
  kind: string;
  factorIds: string[];
  parameters: Record<string, number>;
  hypothesis: string;
  expectedFailureMode: string;
  status: string;
  rejectReason: string;
}

export interface CampaignTrial {
  proposalId: string;
  segment: string;
  sharpe: number | null;
  returnPct: number | null;
  maxDrawdownPct: number | null;
  trades: number | null;
  verdict: string;
  reason: string;
  factorIds?: string[];
}

export interface LedgerEntry {
  round: number;
  entry: string;
  amount: number;
  limit: number;
  breached: boolean;
  note: string;
  createdTs: number;
}

export interface CampaignDetail {
  campaign: Campaign;
  budgetState: { roundsUsed: number; roundsLeft: number; proposalsUsed: number; trialsUsed: number };
  proposals: CampaignProposal[];
  trials: CampaignTrial[];
  ledger: LedgerEntry[];
}

/* ----------------------------------------------------------------- requests */

async function send(path: string, init?: RequestInit & { timeoutMs?: number }): Promise<any> {
  const { timeoutMs = 60_000, ...rest } = init ?? {};
  let response: Response;
  try {
    response = await fetch(path, { ...rest, signal: AbortSignal.timeout(timeoutMs) });
  } catch (reason) {
    throw new GatewayError(`无法连接本地引擎：${reason instanceof Error ? reason.message : reason}`, 0);
  }
  if (!response.ok) {
    let message = `引擎返回 ${response.status}`;
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") message = body.detail;
    } catch {
      /* a non-JSON error body keeps the status line */
    }
    throw new GatewayError(message, response.status);
  }
  return response.json();
}

export async function listCampaigns(limit = 50): Promise<Campaign[]> {
  const payload = await send(`/api/campaigns?limit=${limit}`);
  return (payload.campaigns ?? []) as Campaign[];
}

export async function campaignDetail(uid: string, includeTest = false): Promise<CampaignDetail> {
  return (await send(
    `/api/campaigns/${encodeURIComponent(uid)}?includeTest=${includeTest ? "true" : "false"}`,
  )) as CampaignDetail;
}

export interface NewCampaign {
  group: string;
  interval: string;
  horizonBars: number;
  hypothesis: string;
  successCriteria: string;
  windows: CampaignWindow;
  budget?: { proposalsPerRound?: number; maxRounds?: number; roundDeadlineMs?: number };
  provider?: string;
  seed?: number;
}

export async function createCampaign(body: NewCampaign): Promise<Campaign> {
  return (await send("/api/campaigns", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })) as Campaign;
}

export async function runRound(uid: string): Promise<{ run: { id: number; status: string }; status: string }> {
  return await send(`/api/campaigns/${encodeURIComponent(uid)}/run`, { method: "POST" });
}

export async function promoteProposal(
  uid: string,
  proposalId: string,
  approvedBy: string,
  note = "",
): Promise<any> {
  return await send(`/api/campaigns/${encodeURIComponent(uid)}/promote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ proposalId, approvedBy, note }),
  });
}

export async function unsealTest(uid: string, approvedBy: string): Promise<Campaign> {
  return (await send(`/api/campaigns/${encodeURIComponent(uid)}/unseal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approvedBy }),
  })) as Campaign;
}

export async function finishCampaign(uid: string, status: string, reason: string): Promise<Campaign> {
  return (await send(`/api/campaigns/${encodeURIComponent(uid)}/finish`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, reason }),
  })) as Campaign;
}

export interface CampaignStats {
  campaign: string;
  observed?: { segment: string; sharpe?: number | null; runId?: string | null };
  selectedProposal?: { proposalId: string; segment: string; sharpeAnnualised?: number | null };
  trials?: number;
  trialsRecorded?: number;
  visibility?: { segments?: string[]; testTrialsRecorded?: number };
  deflatedSharpe?: {
    available: boolean;
    deflatedSharpe: number | null;
    expectedMaxSharpe: number | null;
    sharpeSpread: number | null;
    trials: number;
    method?: string;
    reason?: string;
    approximations?: string[];
  };
  pbo?: {
    available: boolean;
    pbo: number | null;
    method?: string;
    reason?: string;
    selectionFrequency?: Record<string, number>;
    skipped?: Record<string, string>;
  };
}

export interface CampaignVerdict {
  judged: boolean;
  campaign: string;
  testSealed: boolean;
  proposalId?: string;
  segment?: string;
  sharpe?: number | null;
  returnPct?: number | null;
  maxDrawdownPct?: number | null;
  trades?: number | null;
  deflatedSharpe?: number | null;
  expectedMaxSharpe?: number | null;
  pbo?: number | null;
  trials?: number | null;
  verdict?: string | null;
  reason?: string;
  criteria?: string;
  approvedBy?: string;
  idempotent?: boolean;
}

export async function campaignStats(uid: string, includeTest = false): Promise<CampaignStats> {
  return (await send(
    `/api/campaigns/${encodeURIComponent(uid)}/stats?includeTest=${includeTest ? "true" : "false"}`,
  )) as CampaignStats;
}

export async function campaignVerdict(uid: string): Promise<CampaignVerdict> {
  return (await send(`/api/campaigns/${encodeURIComponent(uid)}/verdict`)) as CampaignVerdict;
}

export async function judgeCampaign(
  uid: string,
  approvedBy: string,
  proposalId = "",
): Promise<CampaignVerdict> {
  return (await send(`/api/campaigns/${encodeURIComponent(uid)}/verdict`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approvedBy, proposalId }),
  })) as CampaignVerdict;
}

/* ------------------------------------------------------------ pure helpers */

const STATUS_LABELS: Record<string, string> = {
  preregistered: "已预注册",
  running: "搜索中",
  completed: "已完成",
  budget_limited: "预算用尽而停止",
  compliance_blocked: "合规拦截",
  failed: "失败",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function statusTone(status: string): "positive" | "negative" | "warning" | undefined {
  if (status === "completed") return "positive";
  if (status === "failed" || status === "compliance_blocked") return "negative";
  if (status === "budget_limited") return "warning";
  return undefined;
}

/** The test window may only be *shown* once the campaign says it was unsealed. */
export function visibleTrials(detail: CampaignDetail): CampaignTrial[] {
  const unsealed = detail.campaign.testUnsealedTs != null && !detail.campaign.testSealed;
  return (detail.trials ?? []).filter((trial) => unsealed || trial.segment !== "test");
}

export function trialsByProposal(detail: CampaignDetail): Map<string, CampaignTrial[]> {
  const grouped = new Map<string, CampaignTrial[]>();
  for (const trial of visibleTrials(detail)) {
    const list = grouped.get(trial.proposalId) ?? [];
    list.push(trial);
    grouped.set(trial.proposalId, list);
  }
  return grouped;
}

export interface PromotionState {
  eligible: boolean;
  /** Why the action is unavailable - shown to the operator, never a bare disable. */
  reason: string;
}

/**
 * Whether a proposal may be sent for promotion, and what is missing if not.
 *
 * The server refuses a promotion without a validation reading; saying so here keeps
 * the page from offering an action that is going to fail. `approvedBy` is checked
 * too: D3 says a human promotes, so an empty name is not a promotion.
 */
export function promotionState(
  campaign: Campaign,
  proposalId: string,
  detail: CampaignDetail,
  approvedBy: string,
): PromotionState {
  if (!approvedBy.trim()) return { eligible: false, reason: "需要填写批准人（D3：只有人能晋升）" };
  if (!["completed", "budget_limited", "failed", "compliance_blocked"].includes(campaign.status)) {
    return { eligible: false, reason: `战役还在「${statusLabel(campaign.status)}」，结束前不能晋升` };
  }
  const validation = visibleTrials(detail).find(
    (trial) => trial.proposalId === proposalId && trial.segment === "validation",
  );
  if (!validation) return { eligible: false, reason: "该提案没有验证段试验记录" };
  if (validation.sharpe == null) {
    return {
      eligible: false,
      reason: `验证段没有 Sharpe 读数（verdict=${validation.verdict || "空"}）：先把候选跑出可测量的结果`,
    };
  }
  return { eligible: true, reason: "晋升为候选版本（不进任何自动执行）" };
}

export interface BudgetView {
  roundsUsed: number;
  roundsLimit: number;
  roundsLeft: number;
  proposalsUsed: number;
  proposalsLimit: number;
  trialsUsed: number;
  breaches: number;
}

export function budgetView(detail: CampaignDetail): BudgetView {
  const budget = detail.campaign.budget ?? {};
  const roundsLimit = Number(budget.maxRounds ?? 0);
  const proposalsLimit = Number(budget.proposalsPerRound ?? 0) * (roundsLimit || 0);
  return {
    roundsUsed: detail.budgetState?.roundsUsed ?? detail.campaign.roundsUsed,
    roundsLimit,
    roundsLeft: detail.budgetState?.roundsLeft ?? Math.max(0, roundsLimit - detail.campaign.roundsUsed),
    proposalsUsed: detail.campaign.proposalsUsed,
    proposalsLimit,
    trialsUsed: detail.campaign.trialsUsed,
    breaches: (detail.ledger ?? []).filter((entry) => entry.breached).length,
  };
}

export interface SeriesPoint {
  label: string;
  value: number;
  tone: "positive" | "negative";
}

/** Trial Sharpes per proposal, one series per segment - the panel's main figure. */
export function sharpeSeries(detail: CampaignDetail): { proposals: string[]; series: Record<string, SeriesPoint[]> } {
  const trials = visibleTrials(detail);
  const proposals = Array.from(new Set(trials.map((trial) => trial.proposalId))).sort();
  const segments = Array.from(new Set(trials.map((trial) => trial.segment))).sort();
  const series: Record<string, SeriesPoint[]> = {};
  for (const segment of segments) {
    series[segment] = proposals.map((proposalId) => {
      const trial = trials.find((item) => item.proposalId === proposalId && item.segment === segment);
      const value = trial?.sharpe;
      return {
        label: proposalId,
        value: typeof value === "number" ? value : 0,
        tone: typeof value === "number" && value >= 0 ? "positive" : "negative",
      };
    });
  }
  return { proposals, series };
}

/** How often each factor appears across the round's proposals. */
export function factorUsage(detail: CampaignDetail): Array<{ factorId: string; count: number }> {
  const counts = new Map<string, number>();
  for (const proposal of detail.proposals ?? []) {
    for (const factorId of proposal.factorIds ?? []) {
      counts.set(factorId, (counts.get(factorId) ?? 0) + 1);
    }
  }
  return Array.from(counts.entries())
    .map(([factorId, count]) => ({ factorId, count }))
    .sort((left, right) => right.count - left.count || left.factorId.localeCompare(right.factorId));
}

/** Ledger rows with the percentage of the limit each one consumed. */
export function ledgerRows(detail: CampaignDetail): Array<LedgerEntry & { usedPct: number | null }> {
  return (detail.ledger ?? []).map((entry) => ({
    ...entry,
    usedPct: entry.limit > 0 ? Math.min(100, (entry.amount / entry.limit) * 100) : null,
  }));
}

export interface ReportHead {
  headline: string;
  detail: string;
  tone: "positive" | "negative" | "warning" | undefined;
}

/**
 * The one-line summary a reader sees first.
 *
 * It reports the *state of the search*, not a claim about profitability: a campaign
 * whose best trial lost money says so, and a campaign with no readings says that
 * instead of implying a result.
 */
export function reportHead(detail: CampaignDetail): ReportHead {
  const campaign = detail.campaign;
  const trials = visibleTrials(detail);
  const measured = trials.filter((trial) => trial.sharpe != null);
  const label = statusLabel(campaign.status);
  if (!measured.length) {
    return {
      headline: `${label}：没有可测量的试验`,
      detail: trials.length
        ? `${trials.length} 条试验都没有 Sharpe 读数，页面不把「算不出来」写成结果`
        : "还没有试验记录",
      tone: statusTone(campaign.status) ?? "warning",
    };
  }
  const best = measured.reduce((top, item) =>
    (item.sharpe ?? -Infinity) > (top.sharpe ?? -Infinity) ? item : top,
  );
  const worst = measured.reduce((bottom, item) =>
    (item.sharpe ?? Infinity) < (bottom.sharpe ?? Infinity) ? item : bottom,
  );
  return {
    headline: `${label}：${measured.length} 条读数，最好 ${best.proposalId} Sharpe ${decimalText(best.sharpe)}`,
    detail:
      `最差 ${worst.proposalId} Sharpe ${decimalText(worst.sharpe)}` +
      `｜轮次 ${campaign.roundsUsed}/${campaign.budget?.maxRounds ?? "—"}` +
      `｜测试段${campaign.testSealed ? "仍封存" : "已开封"}`,
    tone: statusTone(campaign.status),
  };
}

export interface StatRow {
  label: string;
  value: string;
  note: string;
}

/**
 * DSR and PBO as the page shows them.
 *
 * An unavailable statistic prints its *reason*, never a zero: "PBO could not be
 * computed because no proposal has a stored curve" is a finding, and `0.00` would be
 * a lie about a number nobody produced.
 */
export function statisticsRows(stats: CampaignStats | null): StatRow[] {
  if (!stats) return [];
  const dsr = stats.deflatedSharpe;
  const pbo = stats.pbo;
  const rows: StatRow[] = [
    {
      label: "试验次数 N",
      value: stats.trials == null ? "—" : String(stats.trials),
      note: "多重检验的修正量由尝试次数决定；N 来自战役自己的试验表",
    },
    {
      label: "收缩后 Sharpe（DSR）",
      value: dsr?.available && dsr.deflatedSharpe != null ? decimalText(dsr.deflatedSharpe, 4) : "—",
      note: dsr?.available
        ? `方法 ${dsr.method ?? "—"}；期望最大 Sharpe ${decimalText(dsr.expectedMaxSharpe, 4)}；离散度 ${decimalText(dsr.sharpeSpread, 4)}`
        : dsr?.reason || "DSR 不可用",
    },
    {
      label: "回测过拟合概率（PBO）",
      value: pbo?.available && pbo.pbo != null ? decimalText(pbo.pbo, 4) : "—",
      note: pbo?.available
        ? `${pbo.method ?? "—"}；样本内被选中最多的提案 ${topSelection(pbo.selectionFrequency)}`
        : pbo?.reason || "PBO 不可用",
    },
  ];
  if (dsr?.approximations?.length) {
    rows[1].note += `；近似：${dsr.approximations.join("、")}`;
  }
  if (pbo?.skipped && Object.keys(pbo.skipped).length) {
    rows[2].note += `；跳过 ${Object.keys(pbo.skipped).length} 个提案`;
  }
  return rows;
}

export function topSelection(frequency?: Record<string, number>): string {
  const entries = Object.entries(frequency ?? {}).sort((left, right) => right[1] - left[1]);
  if (!entries.length) return "—";
  return `${entries[0][0]}（${(entries[0][1] * 100).toFixed(0)}%）`;
}

export interface VerdictView {
  label: string;
  tone: "positive" | "negative" | "warning" | undefined;
  headline: string;
  detail: string;
}

export function verdictView(verdict: CampaignVerdict | null): VerdictView {
  if (!verdict) {
    return { label: "无法读取", tone: "warning", headline: "判决不可用", detail: "" };
  }
  if (!verdict.judged) {
    return {
      label: verdict.testSealed ? "测试段封存" : "待判决",
      tone: "warning",
      headline: verdict.testSealed ? "还没有判决：测试段仍未开封" : "测试段已开封，等待判决",
      detail: verdict.reason ?? "",
    };
  }
  const tone =
    verdict.verdict === "pass" ? "positive" : verdict.verdict === "fail" ? "negative" : "warning";
  return {
    label:
      verdict.verdict === "pass" ? "通过" : verdict.verdict === "fail" ? "未通过" : "证据不足",
    tone,
    headline:
      `判决 ${verdict.verdict ?? "—"}：样本外 Sharpe ${decimalText(verdict.sharpe, 4)}` +
      `，DSR ${decimalText(verdict.deflatedSharpe, 4)}，PBO ${decimalText(verdict.pbo, 4)}` +
      `（N=${verdict.trials ?? "—"}）`,
    detail: `${verdict.reason ?? ""}｜判据：${verdict.criteria ?? "—"}｜批准人：${verdict.approvedBy ?? "—"}`,
  };
}

/** A cell for one trial metric, printed the way the result centre prints figures. */
export function trialCells(trial: CampaignTrial): string[] {
  return [
    trial.proposalId,
    trial.segment,
    trial.sharpe == null ? "—" : decimalText(trial.sharpe),
    percentText(trial.returnPct, true),
    drawdownText(trial.maxDrawdownPct),
    countText(trial.trades),
  ];
}
