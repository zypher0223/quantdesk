import { useCallback, useEffect, useMemo, useState } from "react";
import {
  IconAlertTriangle,
  IconCheck,
  IconLock,
  IconLockOpen,
  IconPlayerPlay,
  IconRefresh,
  IconRosetteDiscountCheck,
} from "@tabler/icons-react";

import { GatewayError } from "../services/api";
import {
  budgetView,
  campaignDetail,
  campaignStats,
  campaignVerdict,
  judgeCampaign,
  statisticsRows,
  verdictView,
  createCampaign,
  factorUsage,
  finishCampaign,
  ledgerRows,
  listCampaigns,
  promoteProposal,
  promotionState,
  reportHead,
  runRound,
  sharpeSeries,
  statusLabel,
  statusTone,
  trialCells,
  unsealTest,
  visibleTrials,
  type Campaign,
  type CampaignDetail,
  type CampaignStats,
  type CampaignVerdict,
  type CampaignWindow,
} from "../services/campaigns";

/**
 * Agent campaigns: what the search is doing, and the two acts only a human may take.
 *
 * The page is built around three statements it must never soften:
 *
 * * a campaign is a *pre-registered* search, so the hypothesis, the three windows and
 *   the budget are shown as they were written down - not as they look after the fact;
 * * the test segment is sealed until someone opens it once, and the page renders the
 *   seal (and refuses to draw a test figure) rather than trusting the reader to
 *   remember;
 * * promotion is a human act. The button is enabled only when the engine would accept
 *   it, and when it is not, the page says which condition is missing.
 */

const POLL_MS = 3_000;
const INTERVALS = ["15m", "1h", "4h", "1d"];
const GROUPS: Array<{ key: string; label: string }> = [
  { key: "stock", label: "股票组（15 只，剔除杠杆 ETF）" },
  { key: "leveraged_etf", label: "杠杆 ETF（SOXL/SOXS，单独验证）" },
  { key: "crypto", label: "加密组（BTC/ETH）" },
];

function dayStart(daysAgo: number): string {
  const date = new Date(Date.now() - daysAgo * 86_400_000);
  return date.toISOString().slice(0, 10);
}

/** A window picker that always produces the engine's `[start, end]` millisecond pair. */
function defaultWindows(): { train: string; validation: string; test: string; split: number } {
  return { train: dayStart(150), validation: dayStart(100), test: dayStart(50), split: 50 };
}

function windowsFrom(picker: { train: string; validation: string; test: string; split: number }): CampaignWindow {
  const stamp = (value: string) => Date.parse(`${value}T00:00:00Z`);
  const trainStart = stamp(picker.train);
  const validationStart = stamp(picker.validation);
  const testStart = stamp(picker.test);
  const now = Date.now();
  // The windows must not touch: a bar carries its opening timestamp, so an end that
  // equals the next start would put the same bar in two segments.
  return {
    train: [trainStart, validationStart - 1],
    validation: [validationStart, testStart - 1],
    test: [testStart, now],
  };
}

function when(ts: number | null | undefined): string {
  if (!ts) return "—";
  const date = new Date(ts);
  return `${date.toISOString().slice(0, 10)}`;
}

function ms(value: number): string {
  if (value < 1000) return `${Math.round(value)} ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(1)} s`;
  return `${(value / 60_000).toFixed(1)} min`;
}

/** The panel's figures, drawn as inline SVG so the page carries no chart dependency. */
function BarFigure({
  title,
  caption,
  rows,
  format,
}: {
  title: string;
  caption: string;
  rows: Array<{ label: string; value: number; tone?: "positive" | "negative" }>;
  format: (value: number) => string;
}) {
  const max = Math.max(1, ...rows.map((row) => Math.abs(row.value)));
  return (
    <figure className="rounded-lg border border-white/10 bg-black/20 p-3">
      <figcaption className="mb-2 text-xs uppercase tracking-wide text-neutral-400">{title}</figcaption>
      {rows.length === 0 ? (
        <p className="py-4 text-center text-xs text-neutral-500">没有可画的读数</p>
      ) : (
        <div className="space-y-1">
          {rows.map((row) => {
            const width = (Math.abs(row.value) / max) * 100;
            const negative = row.value < 0;
            return (
              <div key={row.label} className="flex items-center gap-2 text-xs">
                <span className="w-28 shrink-0 truncate text-neutral-400" title={row.label}>
                  {row.label}
                </span>
                <span className="relative h-3 flex-1 rounded-sm bg-white/5">
                  <span
                    className={`absolute inset-y-0 rounded-sm ${
                      negative ? "bg-rose-500/70" : "bg-emerald-500/70"
                    }`}
                    style={{ width: `${width}%`, left: negative ? `${100 - width}%` : 0 }}
                  />
                </span>
                <span className={`w-20 shrink-0 text-right ${negative ? "text-rose-300" : "text-emerald-300"}`}>
                  {format(row.value)}
                </span>
              </div>
            );
          })}
        </div>
      )}
      <p className="mt-2 text-[11px] leading-relaxed text-neutral-500">{caption}</p>
    </figure>
  );
}

/** Trial Sharpe per proposal, one mini-panel per visible segment. */
function SharpeFigure({ detail }: { detail: CampaignDetail }) {
  const { series } = sharpeSeries(detail);
  const segments = Object.keys(series).sort();
  const rows = segments.flatMap((segment) =>
    series[segment].map((point) => ({
      label: `${point.label} · ${segment}`,
      value: point.value,
      tone: point.tone,
    })),
  );
  return (
    <BarFigure
      title="各提案的 Sharpe（按分段）"
      caption="测试段在开封之前不会出现在这张图里；没有读数的提案按 0 画，并在表里显示为「—」。"
      rows={rows}
      format={(value) => value.toFixed(2)}
    />
  );
}

function DrawdownFigure({ detail }: { detail: CampaignDetail }) {
  const rows = visibleTrials(detail)
    .filter((trial) => trial.maxDrawdownPct != null)
    .map((trial) => ({
      label: `${trial.proposalId} · ${trial.segment}`,
      value: -Math.abs(trial.maxDrawdownPct ?? 0),
      tone: "negative" as const,
    }));
  return (
    <BarFigure
      title="最大回撤"
      caption="只画引擎报了的回撤；没报的留空，不按 0 处理。"
      rows={rows}
      format={(value) => `${value.toFixed(2)}%`}
    />
  );
}

function BudgetFigure({ detail }: { detail: CampaignDetail }) {
  const view = budgetView(detail);
  const rows = [
    { label: "轮次", value: view.roundsUsed, tone: "positive" as const },
    { label: "提案", value: view.proposalsUsed, tone: "positive" as const },
    { label: "试验", value: view.trialsUsed, tone: "positive" as const },
  ];
  return (
    <BarFigure
      title="预算使用（轮次/提案/试验）"
      caption={`上限：${view.roundsLimit} 轮 × ${detail.campaign.budget?.proposalsPerRound ?? "—"} 个提案／轮；越界 ${view.breaches} 次（决策 D2）。`}
      rows={rows}
      format={(value) => String(Math.round(value))}
    />
  );
}

function LedgerFigure({ detail }: { detail: CampaignDetail }) {
  const rows = ledgerRows(detail).slice(-8).map((entry) => ({
    label: `第${entry.round}轮 ${entry.entry}`,
    value: entry.usedPct ?? 0,
    tone: entry.breached ? ("negative" as const) : ("positive" as const),
  }));
  return (
    <BarFigure
      title="预算台账（最近 8 条）"
      caption="每条越界都会留档；红色表示这一次真的越了界。"
      rows={rows}
      format={(value) => `${value.toFixed(0)}%`}
    />
  );
}

function FactorUsageFigure({ detail }: { detail: CampaignDetail }) {
  const rows = factorUsage(detail).slice(0, 10).map((item) => ({
    label: item.factorId,
    value: item.count,
    tone: "positive" as const,
  }));
  return (
    <BarFigure
      title="因子使用次数"
      caption="冻结空间里的因子被提案了多少次：一边倒说明搜索没有真正展开。"
      rows={rows}
      format={(value) => String(Math.round(value))}
    />
  );
}

function ReturnScatterFigure({ detail }: { detail: CampaignDetail }) {
  const trials = visibleTrials(detail).filter(
    (trial) => trial.returnPct != null && trial.maxDrawdownPct != null,
  );
  const maxReturn = Math.max(1, ...trials.map((trial) => Math.abs(trial.returnPct ?? 0)));
  const maxDrawdown = Math.max(1, ...trials.map((trial) => Math.abs(trial.maxDrawdownPct ?? 0)));
  return (
    <figure className="rounded-lg border border-white/10 bg-black/20 p-3">
      <figcaption className="mb-2 text-xs uppercase tracking-wide text-neutral-400">收益 × 回撤</figcaption>
      {trials.length === 0 ? (
        <p className="py-4 text-center text-xs text-neutral-500">没有同时报了收益与回撤的读数</p>
      ) : (
        <svg viewBox="0 0 220 140" className="h-40 w-full">
          <line x1="10" y1="120" x2="210" y2="120" stroke="rgba(255,255,255,0.2)" />
          <line x1="110" y1="10" x2="110" y2="120" stroke="rgba(255,255,255,0.2)" />
          {trials.map((trial) => {
            const x = 110 + ((trial.returnPct ?? 0) / maxReturn) * 95;
            const y = 120 - (Math.abs(trial.maxDrawdownPct ?? 0) / maxDrawdown) * 105;
            const positive = (trial.returnPct ?? 0) >= 0;
            return (
              <circle
                key={`${trial.proposalId}-${trial.segment}`}
                cx={x}
                cy={y}
                r="4"
                fill={positive ? "rgba(16,185,129,0.85)" : "rgba(244,63,94,0.85)"}
              >
                <title>{`${trial.proposalId} · ${trial.segment}：收益 ${trial.returnPct?.toFixed(2)}%，回撤 ${trial.maxDrawdownPct?.toFixed(2)}%`}</title>
              </circle>
            );
          })}
          <text x="112" y="132" fill="rgba(255,255,255,0.45)" fontSize="7">
            收益 →
          </text>
          <text x="12" y="14" fill="rgba(255,255,255,0.45)" fontSize="7">
            ↑ 回撤越大越靠上
          </text>
        </svg>
      )}
      <p className="mt-2 text-[11px] leading-relaxed text-neutral-500">
        每个点是一个提案在一个分段上的读数；右上方的点意味着「赚得多但中间亏得也深」。
      </p>
    </figure>
  );
}

export function AgentCampaignWorkspace() {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [detail, setDetail] = useState<CampaignDetail | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [approvedBy, setApprovedBy] = useState("");
  const [promotionNote, setPromotionNote] = useState("");
  const [promotingId, setPromotingId] = useState("");
  const [picker, setPicker] = useState(defaultWindows);
  const [stats, setStats] = useState<CampaignStats | null>(null);
  const [verdict, setVerdict] = useState<CampaignVerdict | null>(null);
  const [form, setForm] = useState({
    group: "stock",
    interval: "1h",
    horizonBars: 24,
    hypothesis: "",
    successCriteria: "",
    proposalsPerRound: 4,
    maxRounds: 2,
  });

  const loadList = useCallback(async () => {
    try {
      const list = await listCampaigns();
      setCampaigns(list);
      setError("");
      setSelected((current) => current || list[0]?.uid || "");
    } catch (reason) {
      setError(reason instanceof GatewayError ? reason.message : String(reason));
    }
  }, []);

  const loadDetail = useCallback(
    async (uid: string) => {
      if (!uid) {
        setDetail(null);
        return;
      }
      try {
        // `includeTest` stays false: the server refuses it while the window is
        // sealed, and the page has no business asking before then.
        setDetail(await campaignDetail(uid, false));
        // Statistics are asked for with the same discipline; a sealed campaign can
        // only have computed them from train/validation, so that is what is drawn.
        setStats(await campaignStats(uid, false));
        setVerdict(await campaignVerdict(uid));
      } catch (reason) {
        setDetail(null);
        setStats(null);
        setVerdict(null);
        setError(reason instanceof GatewayError ? reason.message : String(reason));
      }
    },
    [],
  );

  useEffect(() => {
    void loadList();
  }, [loadList]);

  useEffect(() => {
    void loadDetail(selected);
  }, [selected, loadDetail]);

  const running = detail?.campaign.status === "running" || detail?.campaign.status === "preregistered";
  useEffect(() => {
    if (!running || !selected) return;
    const timer = setInterval(() => void loadDetail(selected), POLL_MS);
    return () => clearInterval(timer);
  }, [running, selected, loadDetail]);

  const act = useCallback(
    async (label: string, action: () => Promise<unknown>) => {
      setBusy(true);
      setNotice("");
      try {
        await action();
        setNotice(label);
        await loadList();
        await loadDetail(selected);
      } catch (reason) {
        setError(reason instanceof GatewayError ? reason.message : String(reason));
      } finally {
        setBusy(false);
      }
    },
    [loadDetail, loadList, selected],
  );

  const head = useMemo(() => (detail ? reportHead(detail) : null), [detail]);
  const promotion = useMemo(() => {
    if (!detail || !promotingId) return null;
    return promotionState(detail.campaign, promotingId, detail, approvedBy);
  }, [detail, promotingId, approvedBy]);

  const submit = () =>
    act("战役已预注册", async () => {
      const created = await createCampaign({
        group: form.group,
        interval: form.interval,
        horizonBars: form.horizonBars,
        hypothesis: form.hypothesis,
        successCriteria: form.successCriteria,
        windows: windowsFrom(picker),
        budget: {
          proposalsPerRound: form.proposalsPerRound,
          maxRounds: form.maxRounds,
          roundDeadlineMs: 600_000,
        },
      });
      setSelected(created.uid);
    });

  return (
    <div className="space-y-4">
      <header className="rounded-xl border border-white/10 bg-white/5 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-medium text-neutral-100">代理战役</h2>
            <p className="mt-1 max-w-3xl text-xs leading-relaxed text-neutral-400">
              代理只能在预注册时冻结的因子空间里提案，只看得到训练段与验证段的读数；测试段由人工一次性开封
              （Gate-C），晋升只能由人点击（决策 D3），代理不能改代码、不联网、不下单。
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              value={approvedBy}
              onChange={(event) => setApprovedBy(event.target.value)}
              placeholder="批准人（晋升/开封必须署名）"
              className="w-48 rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-neutral-100"
            />
            <button
              type="button"
              onClick={() => void loadList()}
              className="flex items-center gap-1 rounded border border-white/10 px-2 py-1 text-xs text-neutral-200 hover:bg-white/10"
            >
              <IconRefresh size={14} /> 刷新
            </button>
          </div>
        </div>
        {error && (
          <p className="mt-3 flex items-center gap-2 rounded border border-rose-500/30 bg-rose-500/10 p-2 text-xs text-rose-200">
            <IconAlertTriangle size={14} /> {error}
          </p>
        )}
        {notice && (
          <p className="mt-3 flex items-center gap-2 rounded border border-emerald-500/30 bg-emerald-500/10 p-2 text-xs text-emerald-200">
            <IconCheck size={14} /> {notice}
          </p>
        )}
      </header>

      <section className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <div className="space-y-4">
          <div className="rounded-xl border border-white/10 bg-white/5 p-3">
            <h3 className="mb-2 text-sm font-medium text-neutral-200">预注册一个新战役</h3>
            <div className="space-y-2 text-xs">
              <label className="block text-neutral-400">
                分组（D5：股票组不含杠杆 ETF）
                <select
                  value={form.group}
                  onChange={(event) => setForm({ ...form, group: event.target.value })}
                  className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-neutral-100"
                >
                  {GROUPS.map((group) => (
                    <option key={group.key} value={group.key}>
                      {group.label}
                    </option>
                  ))}
                </select>
              </label>
              <div className="flex gap-2">
                <label className="flex-1 text-neutral-400">
                  周期
                  <select
                    value={form.interval}
                    onChange={(event) => setForm({ ...form, interval: event.target.value })}
                    className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-neutral-100"
                  >
                    {INTERVALS.map((interval) => (
                      <option key={interval} value={interval}>
                        {interval}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex-1 text-neutral-400">
                  视野（bar）
                  <input
                    type="number"
                    min={1}
                    value={form.horizonBars}
                    onChange={(event) => setForm({ ...form, horizonBars: Number(event.target.value) })}
                    className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-neutral-100"
                  />
                </label>
              </div>
              <label className="block text-neutral-400">
                假设（预注册，事后不能改）
                <textarea
                  value={form.hypothesis}
                  onChange={(event) => setForm({ ...form, hypothesis: event.target.value })}
                  rows={2}
                  className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-neutral-100"
                />
              </label>
              <label className="block text-neutral-400">
                成功判据（开封后照此判决）
                <textarea
                  value={form.successCriteria}
                  onChange={(event) => setForm({ ...form, successCriteria: event.target.value })}
                  rows={2}
                  className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-neutral-100"
                />
              </label>
              <div className="grid grid-cols-3 gap-2">
                <label className="text-neutral-400">
                  训练起
                  <input
                    type="date"
                    value={picker.train}
                    onChange={(event) => setPicker({ ...picker, train: event.target.value })}
                    className="mt-1 w-full rounded border border-white/10 bg-black/30 px-1 py-1 text-neutral-100"
                  />
                </label>
                <label className="text-neutral-400">
                  验证起
                  <input
                    type="date"
                    value={picker.validation}
                    onChange={(event) => setPicker({ ...picker, validation: event.target.value })}
                    className="mt-1 w-full rounded border border-white/10 bg-black/30 px-1 py-1 text-neutral-100"
                  />
                </label>
                <label className="text-neutral-400">
                  测试起
                  <input
                    type="date"
                    value={picker.test}
                    onChange={(event) => setPicker({ ...picker, test: event.target.value })}
                    className="mt-1 w-full rounded border border-white/10 bg-black/30 px-1 py-1 text-neutral-100"
                  />
                </label>
              </div>
              <div className="flex gap-2">
                <label className="flex-1 text-neutral-400">
                  每轮提案 ≤32
                  <input
                    type="number"
                    min={1}
                    max={32}
                    value={form.proposalsPerRound}
                    onChange={(event) => setForm({ ...form, proposalsPerRound: Number(event.target.value) })}
                    className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-neutral-100"
                  />
                </label>
                <label className="flex-1 text-neutral-400">
                  轮数 ≤5
                  <input
                    type="number"
                    min={1}
                    max={5}
                    value={form.maxRounds}
                    onChange={(event) => setForm({ ...form, maxRounds: Number(event.target.value) })}
                    className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-neutral-100"
                  />
                </label>
              </div>
              <button
                type="button"
                disabled={busy || !form.hypothesis.trim() || !form.successCriteria.trim()}
                onClick={() => void submit()}
                className="w-full rounded bg-sky-500/80 px-2 py-1.5 text-xs font-medium text-white disabled:opacity-40"
              >
                预注册
              </button>
              <p className="text-[11px] leading-relaxed text-neutral-500">
                没有通过七道闸门的受控因子库时，引擎会拒绝开战役——没有证据的因子空间不允许搜索。
              </p>
            </div>
          </div>

          <div className="rounded-xl border border-white/10 bg-white/5 p-3">
            <h3 className="mb-2 text-sm font-medium text-neutral-200">战役列表</h3>
            <div className="space-y-1">
              {campaigns.length === 0 && <p className="text-xs text-neutral-500">还没有战役</p>}
              {campaigns.map((item) => (
                <button
                  key={item.uid}
                  type="button"
                  onClick={() => setSelected(item.uid)}
                  className={`w-full rounded border px-2 py-1 text-left text-xs ${
                    selected === item.uid
                      ? "border-sky-400/40 bg-sky-500/10 text-neutral-100"
                      : "border-white/10 text-neutral-300 hover:bg-white/5"
                  }`}
                >
                  <span className="font-mono">{item.uid.slice(0, 14)}</span> · {statusLabel(item.status)}
                  <span className="block text-[11px] text-neutral-500">
                    {item.group}/{item.interval} · 轮 {item.roundsUsed}/{item.budget?.maxRounds ?? "—"} · 试验{" "}
                    {item.trialsUsed}
                  </span>
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="space-y-4">
          {!detail && <p className="rounded-xl border border-white/10 bg-white/5 p-4 text-xs text-neutral-400">选择一个战役查看详情。</p>}
          {detail && head && (
            <>
              <div className="rounded-xl border border-white/10 bg-white/5 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-medium text-neutral-100">
                    {head.headline}
                    <span
                      className={`ml-2 rounded px-1.5 py-0.5 text-[11px] ${
                        statusTone(detail.campaign.status) === "positive"
                          ? "bg-emerald-500/15 text-emerald-200"
                          : statusTone(detail.campaign.status) === "negative"
                            ? "bg-rose-500/15 text-rose-200"
                            : "bg-amber-500/15 text-amber-200"
                      }`}
                    >
                      {statusLabel(detail.campaign.status)}
                    </span>
                  </h3>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={busy || !["preregistered", "running"].includes(detail.campaign.status)}
                      onClick={() => void act("已排队一轮", () => runRound(detail.campaign.uid))}
                      className="flex items-center gap-1 rounded border border-white/10 px-2 py-1 text-xs text-neutral-200 hover:bg-white/10 disabled:opacity-40"
                    >
                      <IconPlayerPlay size={14} /> 跑一轮
                    </button>
                    <button
                      type="button"
                      disabled={busy || detail.campaign.testSealed === false}
                      onClick={() =>
                        void act("测试段已开封（一次性）", () => unsealOnce(detail.campaign.uid, approvedBy))
                      }
                      className="flex items-center gap-1 rounded border border-white/10 px-2 py-1 text-xs text-neutral-200 hover:bg-white/10 disabled:opacity-40"
                    >
                      {detail.campaign.testSealed ? <IconLock size={14} /> : <IconLockOpen size={14} />}
                      {detail.campaign.testSealed ? "一次性开封测试段" : "测试段已开封"}
                    </button>
                    <button
                      type="button"
                      disabled={busy || !["preregistered", "running"].includes(detail.campaign.status)}
                      onClick={() =>
                        void act("战役已结束", () =>
                          finishCampaign(detail.campaign.uid, "completed", "人工结束"),
                        )
                      }
                      className="rounded border border-white/10 px-2 py-1 text-xs text-neutral-200 hover:bg-white/10 disabled:opacity-40"
                    >
                      结束
                    </button>
                  </div>
                </div>
                <p className="mt-2 text-xs text-neutral-400">{head.detail}</p>
                <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] text-neutral-400 md:grid-cols-4">
                  <div>
                    <dt className="text-neutral-500">假设</dt>
                    <dd className="text-neutral-200">{detail.campaign.hypothesis}</dd>
                  </div>
                  <div>
                    <dt className="text-neutral-500">成功判据</dt>
                    <dd className="text-neutral-200">{detail.campaign.successCriteria}</dd>
                  </div>
                  <div>
                    <dt className="text-neutral-500">窗口</dt>
                    <dd className="text-neutral-200">
                      训练 {when(detail.campaign.windows.train[0])}–{when(detail.campaign.windows.train[1])}
                      <br />
                      验证 {when(detail.campaign.windows.validation[0])}–{when(detail.campaign.windows.validation[1])}
                      <br />
                      测试 {when(detail.campaign.windows.test[0])}–{when(detail.campaign.windows.test[1])}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-neutral-500">冻结因子空间</dt>
                    <dd className="text-neutral-200">
                      {detail.campaign.factorSpace.map((item) => (
                        <span key={item.factorId} className="mr-1 inline-block rounded bg-white/5 px-1">
                          {item.factorId}
                          <span className="text-neutral-500"> · {item.tier}</span>
                        </span>
                      ))}
                    </dd>
                  </div>
                </dl>
                {detail.campaign.testSealed ? (
                  <p className="mt-3 flex items-center gap-2 rounded border border-amber-500/30 bg-amber-500/10 p-2 text-[11px] text-amber-200">
                    <IconLock size={13} /> 测试段仍然封存：表中不会出现测试段读数，引擎也会拒绝读取
                    （Gate-C）。开封需要署名，且只能开一次。
                  </p>
                ) : (
                  <p className="mt-3 flex items-center gap-2 rounded border border-emerald-500/30 bg-emerald-500/10 p-2 text-[11px] text-emerald-200">
                    <IconLockOpen size={13} /> 测试段已于 {when(detail.campaign.testUnsealedTs)} 开封一次，
                    不会再开第二次。
                  </p>
                )}
              </div>

              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                <SharpeFigure detail={detail} />
                <DrawdownFigure detail={detail} />
                <ReturnScatterFigure detail={detail} />
                <BudgetFigure detail={detail} />
                <LedgerFigure detail={detail} />
                <FactorUsageFigure detail={detail} />
              </div>

              <div className="rounded-xl border border-white/10 bg-white/5 p-3">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-medium text-neutral-200">多重检验校正与判决（Gate-C）</h3>
                  <button
                    type="button"
                    disabled={busy || !approvedBy.trim() || !["completed", "budget_limited", "failed", "compliance_blocked"].includes(detail.campaign.status)}
                    title={
                      !approvedBy.trim()
                        ? "开封与判决都要署名"
                        : detail.campaign.testSealed
                          ? "开封测试段并做一次性判决"
                          : "已有判决时重复调用不会重跑测试段"
                    }
                    onClick={() =>
                      void act("判决已写入", () => judgeCampaign(detail.campaign.uid, approvedBy))
                    }
                    className="rounded border border-sky-400/30 bg-sky-500/10 px-2 py-1 text-xs text-sky-200 disabled:opacity-40"
                  >
                    开封并判决（一次性）
                  </button>
                </div>
                <div className="grid gap-2 md:grid-cols-3">
                  {statisticsRows(stats).map((row) => (
                    <div key={row.label} className="rounded border border-white/10 p-2">
                      <p className="text-[11px] text-neutral-500">{row.label}</p>
                      <p className="text-sm text-neutral-100">{row.value}</p>
                      <p className="mt-1 text-[11px] leading-relaxed text-neutral-500">{row.note}</p>
                    </div>
                  ))}
                  {!stats && <p className="text-xs text-neutral-500">统计接口还没有回答</p>}
                </div>
                {verdict && (
                  <div
                    className={`mt-3 rounded border p-2 text-xs ${
                      verdictView(verdict).tone === "positive"
                        ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-100"
                        : verdictView(verdict).tone === "negative"
                          ? "border-rose-500/30 bg-rose-500/10 text-rose-100"
                          : "border-amber-500/30 bg-amber-500/10 text-amber-100"
                    }`}
                  >
                    <p className="font-medium">
                      {verdictView(verdict).label}｜{verdictView(verdict).headline}
                    </p>
                    <p className="mt-1 leading-relaxed opacity-90">{verdictView(verdict).detail}</p>
                  </div>
                )}
                <p className="mt-2 text-[11px] leading-relaxed text-neutral-500">
                  判决只在开封后进行一次：把预注册的判据逐条对照测试段的读数，写进
                  `agent_verdicts`。重复调用返回同一条记录，不会重跑测试段——所以测试段始终是样本外。
                </p>
              </div>

              <div className="rounded-xl border border-white/10 bg-white/5 p-3">
                <h3 className="mb-2 text-sm font-medium text-neutral-200">试验读数</h3>
                <div className="overflow-x-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="text-neutral-500">
                      <tr>
                        {["提案", "分段", "Sharpe", "收益", "回撤", "交易"].map((column) => (
                          <th key={column} className="py-1 pr-3 font-normal">
                            {column}
                          </th>
                        ))}
                        <th className="py-1 font-normal">说明</th>
                      </tr>
                    </thead>
                    <tbody className="text-neutral-200">
                      {visibleTrials(detail).map((trial) => {
                        const cells = trialCells(trial);
                        return (
                          <tr key={`${trial.proposalId}-${trial.segment}`} className="border-t border-white/5">
                            {cells.map((cell, index) => (
                              <td key={index} className="py-1 pr-3">
                                {cell}
                              </td>
                            ))}
                            <td className="py-1 text-neutral-400">{trial.reason || trial.verdict}</td>
                          </tr>
                        );
                      })}
                      {visibleTrials(detail).length === 0 && (
                        <tr>
                          <td colSpan={7} className="py-2 text-center text-neutral-500">
                            还没有读数
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="rounded-xl border border-white/10 bg-white/5 p-3">
                <h3 className="mb-2 text-sm font-medium text-neutral-200">提案与人工晋升</h3>
                <div className="space-y-2">
                  {detail.proposals.map((proposal) => {
                    // No placeholder for an empty name: substituting one made the
                    // button look armed while the engine would have refused it.
                    const state = promotionState(
                      detail.campaign,
                      proposal.proposalId,
                      detail,
                      approvedBy,
                    );
                    return (
                      <div
                        key={proposal.proposalId}
                        className="flex flex-wrap items-center justify-between gap-2 rounded border border-white/10 p-2 text-xs"
                      >
                        <div className="min-w-0">
                          <p className="font-mono text-neutral-200">
                            {proposal.proposalId} · 第 {proposal.round} 轮 · {proposal.kind}
                          </p>
                          <p className="truncate text-neutral-400">{proposal.hypothesis}</p>
                          <p className="text-[11px] text-neutral-500">
                            {proposal.factorIds.join("、")}｜参数{" "}
                            {Object.entries(proposal.parameters)
                              .map(([key, value]) => `${key}=${value}`)
                              .join(", ") || "—"}
                          </p>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="text-[11px] text-neutral-500">{state.reason}</span>
                          <button
                            type="button"
                            disabled={busy || !state.eligible}
                            title={state.reason}
                            onClick={() =>
                              void act(`已晋升 ${proposal.proposalId} 为候选`, () =>
                                promoteProposal(
                                  detail.campaign.uid,
                                  proposal.proposalId,
                                  approvedBy,
                                  promotionNote,
                                ),
                              )
                            }
                            className="flex items-center gap-1 rounded border border-emerald-400/30 bg-emerald-500/10 px-2 py-1 text-emerald-200 disabled:opacity-40"
                          >
                            <IconRosetteDiscountCheck size={14} /> 晋升为候选
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
                <input
                  value={promotionNote}
                  onChange={(event) => setPromotionNote(event.target.value)}
                  placeholder="晋升备注（写进台账）"
                  className="mt-2 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-neutral-100"
                />
                <p className="mt-2 text-[11px] leading-relaxed text-neutral-500">
                  晋升只写一个标记为 candidate 的策略版本，不会进入任何自动执行；没有验证段读数的提案会被
                  引擎直接拒绝，这里的按钮也会说明缺什么。
                </p>
              </div>
            </>
          )}
        </div>
      </section>
    </div>
  );
}

async function unsealOnce(uid: string, approvedBy: string) {
  return unsealTest(uid, approvedBy);
}
