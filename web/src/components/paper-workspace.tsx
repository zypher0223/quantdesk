import { useCallback, useEffect, useMemo, useState } from "react";
import { IconAlertTriangle, IconCheck, IconRefresh, IconX } from "@tabler/icons-react";
import type { Instrument } from "../data/market";
import { compactUsdt, formatPrice, relativeAge } from "../lib/format";
import type { TradingPlan } from "../services/research";
import {
  fetchExternalStatus,
  fetchScenarios,
  runPortfolioRisk,
  runScenario,
  type AnalyticsOutcome,
  type ExternalScenario,
  type ExternalStatus,
} from "../services/external";
import {
  closePosition,
  fetchJournal,
  fetchPaperAccount,
  journalExportUrl,
  openPosition,
  resetPaper,
  savePositionNote,
  type JournalResponse,
  type PaperAccount,
} from "../services/paper";
import { AiPaperWorkspace } from "./ai-paper-workspace";

type Feed = "positions" | "journal" | "risk" | "ai";

export function PaperWorkspace({
  instrument,
  prefill,
  onPrefillConsumed,
}: {
  instrument: Instrument | null;
  /** A plan handed over from the research workspace, used to prefill the order form. */
  prefill?: TradingPlan | null;
  onPrefillConsumed?: () => void;
}) {
  const [feed, setFeed] = useState<Feed>("positions");
  useEffect(() => {
    if (prefill) setFeed("positions");
  }, [prefill]);
  return (
    <div className="paper-workspace">
      <header className="paper-heading">
        <div>
          <h2>模拟交易与交易日志</h2>
          <p>持仓按交易所标记价估值，费用与资金费从模拟现金中扣除。成交后写入只可追加的日志，条目带内容哈希，改动可被发现。</p>
        </div>
        <div className="paper-tabs" role="tablist">
          <button type="button" role="tab" aria-selected={feed === "positions"} className={feed === "positions" ? "active" : ""} onClick={() => setFeed("positions")}>持仓与账户</button>
          <button type="button" role="tab" aria-selected={feed === "journal"} className={feed === "journal" ? "active" : ""} onClick={() => setFeed("journal")}>交易日志</button>
          <button type="button" role="tab" aria-selected={feed === "risk"} className={feed === "risk" ? "active" : ""} onClick={() => setFeed("risk")}>组合风险</button>
          <button type="button" role="tab" aria-selected={feed === "ai"} className={feed === "ai" ? "active" : ""} onClick={() => setFeed("ai")}>AI 自动模拟</button>
        </div>
      </header>
      {feed === "positions" ? (
        <PositionsPanel instrument={instrument} prefill={prefill} onPrefillConsumed={onPrefillConsumed} />
      ) : feed === "journal" ? (
        <JournalPanel />
      ) : feed === "risk" ? (
        <PortfolioRiskPanel />
      ) : (
        <AiPaperWorkspace />
      )}
    </div>
  );
}

function PositionsPanel({
  instrument,
  prefill,
  onPrefillConsumed,
}: {
  instrument: Instrument | null;
  prefill?: TradingPlan | null;
  onPrefillConsumed?: () => void;
}) {
  const [account, setAccount] = useState<PaperAccount | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [side, setSide] = useState<"long" | "short">("long");
  const [notional, setNotional] = useState("1000");
  const [leverage, setLeverage] = useState("1");
  const [stopLoss, setStopLoss] = useState("");
  const [takeProfit1, setTakeProfit1] = useState("");
  const [takeProfit2, setTakeProfit2] = useState("");
  const [rationale, setRationale] = useState("");
  const [noteDrafts, setNoteDrafts] = useState<Record<number, string>>({});
  const [fromPlan, setFromPlan] = useState(false);

  // Adopt a plan only after the account is loaded: position_size_pct is a
  // percentage of current equity, not a percentage of a hard-coded account.
  useEffect(() => {
    if (!prefill || !account) return;
    setSide(prefill.direction === "short" ? "short" : "long");
    const size = prefill.position_size_pct && prefill.position_size_pct > 0 ? prefill.position_size_pct : 10;
    setNotional(String(Math.max(1, Math.round(account.equity * size / 100))));
    setStopLoss(prefill.stop_loss ? String(prefill.stop_loss) : "");
    setTakeProfit1(prefill.take_profit_1 ? String(prefill.take_profit_1) : "");
    setTakeProfit2(prefill.take_profit_2 ? String(prefill.take_profit_2) : "");
    setRationale(
      [
        prefill.rationale ? `计划理由：${prefill.rationale}` : null,
        prefill.entry ? `入场 ${prefill.entry}` : null,
        prefill.stop_loss ? `止损 ${prefill.stop_loss}` : null,
        prefill.take_profit_1 ? `止盈1 ${prefill.take_profit_1}` : null,
        prefill.take_profit_2 ? `止盈2 ${prefill.take_profit_2}` : null,
        prefill.valid_until ? `有效期 ${prefill.valid_until}` : null,
      ]
        .filter(Boolean)
        .join("；"),
    );
    setFromPlan(true);
    onPrefillConsumed?.();
  }, [account, prefill, onPrefillConsumed]);

  const load = useCallback(async () => {
    try {
      setAccount(await fetchPaperAccount());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取账户失败");
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [load]);

  const act = async (action: () => Promise<string>) => {
    setBusy(true);
    setNotice(null);
    try {
      setNotice(await action());
      await load();
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const riskTone = (position: NonNullable<PaperAccount>["positions"][number]) => {
    if (position.distance_to_liq_pct === null) return "";
    if (position.distance_to_liq_pct < 5) return "risk-high";
    if (position.distance_to_liq_pct < 15) return "risk-mid";
    return "";
  };

  if (error) {
    return (
      <section className="coming-workspace">
        <IconAlertTriangle size={28} />
        <h2>模拟账户不可用</h2>
        <p>{error}</p>
        <div className="coming-context"><span>需要</span><strong>本地网关</strong><span>请先运行 python -m quantdesk.cli serve</span></div>
      </section>
    );
  }

  return (
    <div className="paper-layout">
      <section className="paper-form">
        <h3>开仓（模拟）</h3>
        <p>按交易所标记价加滑点成交，立即计算强平价与保证金占用。</p>
        {fromPlan && <p className="paper-from-plan"><IconCheck size={13} />已按当前权益和研判计划预填仓位与保护价</p>}
        <label>标的<input value={instrument?.displaySymbol ?? "—"} readOnly /></label>
        <div className="backtest-field-row">
          <label>方向<select value={side} onChange={(event) => setSide(event.target.value as "long" | "short")}><option value="long">做多</option><option value="short">做空</option></select></label>
          <label>杠杆（x）<input type="number" min="1" max="100" value={leverage} onChange={(event) => setLeverage(event.target.value)} /></label>
        </div>
        <label>名义额（USDT）<input type="number" min="1" step="100" value={notional} onChange={(event) => setNotional(event.target.value)} /></label>
        <div className="paper-protection">
          <strong>保护条件（可选）</strong>
          <label>止损价<input type="number" min="0" step="any" value={stopLoss} placeholder="触发后全部平仓" onChange={(event) => setStopLoss(event.target.value)} /></label>
          <div className="backtest-field-row">
            <label>止盈1<input type="number" min="0" step="any" value={takeProfit1} placeholder="先平 50%" onChange={(event) => setTakeProfit1(event.target.value)} /></label>
            <label>止盈2<input type="number" min="0" step="any" value={takeProfit2} placeholder="平掉剩余" onChange={(event) => setTakeProfit2(event.target.value)} /></label>
          </div>
          <small>只填止盈1时会全部平仓；同时填两个目标时，止盈1先平一半。</small>
        </div>
        <label>开仓理由<textarea rows={3} maxLength={300} value={rationale} placeholder="为什么开这一仓，失效条件是什么" onChange={(event) => setRationale(event.target.value)} /></label>
        <button
          type="button"
          className="analysis-run"
          disabled={!instrument || busy}
          onClick={() =>
            act(async () => {
              if (!instrument) return "请先选择标的";
              const view = await openPosition({
                symbol: instrument.venueSymbol,
                side,
                notional: Number(notional),
                leverage: Number(leverage),
                rationale,
                stopLoss: stopLoss ? Number(stopLoss) : undefined,
                takeProfit1: takeProfit1 ? Number(takeProfit1) : undefined,
                takeProfit2: takeProfit2 ? Number(takeProfit2) : undefined,
              });
              setRationale("");
              setStopLoss("");
              setTakeProfit1("");
              setTakeProfit2("");
              setFromPlan(false);
              return `已开仓 ${view.display_symbol} ${view.side === "long" ? "多" : "空"} ${view.qty} @ ${formatPrice(view.entry_price)}，强平价 ${formatPrice(view.liq_price)}`;
            })
          }
        >
          模拟开仓
        </button>
        <button
          type="button"
          className="paper-reset"
          disabled={busy || !account || account.positions.length > 0}
          onClick={() => act(async () => { await resetPaper(); return "模拟账户已重置"; })}
        >
          重置账户
        </button>
        {notice && <p className="paper-notice" role="status">{notice}</p>}
      </section>

      <section className="paper-body">
        {account && (
          <div className="paper-stats">
            <Stat label="权益" value={`${compactUsdt(account.equity)}`} />
            <Stat label="可用保证金" value={`${compactUsdt(account.free_margin)}`} />
            <Stat label="已占用保证金" value={`${compactUsdt(account.margin_used)}`} />
            <Stat label="未实现盈亏" value={`${account.unrealized_pnl >= 0 ? "+" : ""}${compactUsdt(account.unrealized_pnl)}`} tone={account.unrealized_pnl >= 0 ? "positive" : "negative"} />
            <Stat label="已实现盈亏" value={`${account.realized_pnl >= 0 ? "+" : ""}${compactUsdt(account.realized_pnl)}`} tone={account.realized_pnl >= 0 ? "positive" : "negative"} />
            <Stat label="手续费累计" value={compactUsdt(account.fees_paid)} />
            <Stat label="资金费 付出/收取" value={`${compactUsdt(account.funding_paid)} / ${compactUsdt(account.funding_received)}`} />
            <Stat label="标记价更新" value={relativeAge(account.mark_prices_at)} />
          </div>
        )}
        {account && account.warnings.length > 0 && (
          <ul className="backtest-warnings">
            {account.warnings.map((warning, index) => <li key={index}><IconAlertTriangle size={13} />{warning}</li>)}
          </ul>
        )}

        <div className="paper-positions">
          <div className="paper-positions-heading">
            <h3>持仓 {account ? `(${account.positions.length})` : ""}</h3>
            <button type="button" onClick={() => void load()} disabled={busy}><IconRefresh size={14} />刷新标记价</button>
          </div>
          {!account || account.positions.length === 0 ? (
            <p className="backtest-hint">当前没有模拟持仓。</p>
          ) : (
            <div className="paper-cards">
              {account.positions.map((position) => (
                <article key={position.id} className={`paper-card ${riskTone(position)}`}>
                  <header>
                    <div>
                      <strong>{position.display_symbol}</strong>
                      <span className={position.side === "long" ? "positive" : "negative"}>{position.side === "long" ? "多" : "空"} · {position.leverage}x</span>
                    </div>
                    <span className="paper-card-pnl">
                      {position.unrealized_pnl === null ? "—" : `${position.unrealized_pnl >= 0 ? "+" : ""}${compactUsdt(position.unrealized_pnl)}`}
                      {position.unrealized_pct !== null && <em>{position.unrealized_pct >= 0 ? "+" : ""}{position.unrealized_pct.toFixed(2)}%</em>}
                    </span>
                  </header>
                  <dl>
                    <div><dt>入场价</dt><dd>{formatPrice(position.entry_price)}</dd></div>
                    <div><dt>标记价</dt><dd>{formatPrice(position.mark_price)}</dd></div>
                    <div><dt>数量</dt><dd>{position.qty.toLocaleString("en-US", { maximumFractionDigits: 4 })}</dd></div>
                    <div><dt>名义额</dt><dd>{compactUsdt(position.notional)}</dd></div>
                    <div><dt>保证金</dt><dd>{compactUsdt(position.margin)}</dd></div>
                    <div><dt>强平价</dt><dd>{formatPrice(position.liq_price)}</dd></div>
                    <div><dt>距强平</dt><dd>{position.distance_to_liq_pct === null ? "—" : `${position.distance_to_liq_pct.toFixed(2)}%`}</dd></div>
                    <div><dt>保证金率</dt><dd>{position.margin_ratio === null ? "—" : `${(position.margin_ratio * 100).toFixed(2)}%`}</dd></div>
                  </dl>
                  {position.protective_orders.length > 0 && (
                    <div className="paper-orders" aria-label="生效中的保护条件">
                      {position.protective_orders.map((order) => (
                        <span key={order.id}>
                          {order.type === "stop_loss" ? "止损" : order.type === "take_profit_1" ? "止盈1" : "止盈2"}
                          <strong>{formatPrice(order.trigger_price)}</strong>
                          {order.close_fraction < 1 ? <em>平 {Math.round(order.close_fraction * 100)}%</em> : <em>全部</em>}
                        </span>
                      ))}
                    </div>
                  )}
                  <label className="paper-note">
                    理由（可随时修改，平仓时写入日志）
                    <textarea
                      rows={2}
                      maxLength={500}
                      value={noteDrafts[position.id] ?? position.notes}
                      onChange={(event) => setNoteDrafts((current) => ({ ...current, [position.id]: event.target.value }))}
                      onBlur={() => {
                        const draft = noteDrafts[position.id];
                        if (draft === undefined || draft === position.notes) return;
                        void act(async () => {
                          await savePositionNote(position.id, draft);
                          return "理由已保存";
                        });
                      }}
                    />
                  </label>
                  <button
                    type="button"
                    className="paper-close"
                    disabled={busy}
                    onClick={() => act(async () => {
                      const result = await closePosition(position.id);
                      return `已平仓 ${position.display_symbol}，净盈亏 ${Number(result.net_pnl) >= 0 ? "+" : ""}${compactUsdt(Number(result.net_pnl))} USDT，已写入日志`;
                    })}
                  >
                    <IconX size={14} />平仓并写入日志
                  </button>
                </article>
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

function JournalPanel() {
  const [data, setData] = useState<JournalResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setData(await fetchJournal());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取日志失败");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const totals = useMemo(() => {
    const entries = data?.entries ?? [];
    return {
      net: entries.reduce((sum, entry) => sum + entry.net_pnl, 0),
      fees: entries.reduce((sum, entry) => sum + entry.fees, 0),
      funding: entries.reduce((sum, entry) => sum + entry.funding_paid, 0),
      wins: entries.filter((entry) => entry.net_pnl > 0).length,
    };
  }, [data]);

  if (error) {
    return <section className="coming-workspace"><IconAlertTriangle size={28} /><h2>日志不可用</h2><p>{error}</p></section>;
  }

  return (
    <div className="journal-panel">
      <div className="journal-toolbar">
        <div className={`journal-integrity ${data?.integrity.intact ? "ok" : "broken"}`}>
          {data?.integrity.intact ? <IconCheck size={14} /> : <IconAlertTriangle size={14} />}
          <span>
            {data
              ? data.integrity.intact
                ? `${data.integrity.entries} 条记录，哈希校验通过 · 只可追加`
                : `哈希校验失败：第 ${data.integrity.tampered.join(", ")} 条被改动`
              : "正在校验…"}
          </span>
        </div>
        <div className="journal-actions">
          <a href={journalExportUrl("csv")} download>导出 CSV</a>
          <a href={journalExportUrl("json")} download>导出 JSON</a>
          <button type="button" onClick={() => void load()} disabled={busy}>
            <IconRefresh size={14} className={busy ? "spin" : ""} />刷新
          </button>
        </div>
      </div>

      {data && data.entries.length > 0 && (
        <div className="paper-stats">
          <Stat label="净盈亏合计" value={`${totals.net >= 0 ? "+" : ""}${compactUsdt(totals.net)}`} tone={totals.net >= 0 ? "positive" : "negative"} />
          <Stat label="手续费合计" value={compactUsdt(totals.fees)} />
          <Stat label="资金费合计" value={compactUsdt(totals.funding)} />
          <Stat label="盈利笔数" value={`${totals.wins} / ${data.entries.length}`} />
        </div>
      )}

      {!data || data.entries.length === 0 ? (
        <p className="backtest-hint">日志为空。在「持仓与账户」里平掉一笔模拟持仓后会在这里留下不可修改的记录。</p>
      ) : (
        <div className="backtest-table-wrap">
          <table>
            <thead>
              <tr><th>平仓时间</th><th>标的</th><th>方向</th><th>数量</th><th>入场价</th><th>出场价</th><th>杠杆</th><th>资金费</th><th>手续费</th><th>净盈亏</th><th>理由</th><th>哈希</th></tr>
            </thead>
            <tbody>
              {data.entries.map((entry) => (
                <tr key={entry.id}>
                  <td>{new Date(entry.closed_ts).toLocaleString("zh-CN", { hour12: false })}</td>
                  <td>{entry.symbol}</td>
                  <td className={entry.side === "long" ? "positive" : "negative"}>{entry.side === "long" ? "多" : "空"}</td>
                  <td>{entry.qty.toLocaleString("en-US", { maximumFractionDigits: 4 })}</td>
                  <td>{formatPrice(entry.entry_price)}</td>
                  <td>{formatPrice(entry.exit_price)}</td>
                  <td>{entry.leverage}x</td>
                  <td>{compactUsdt(entry.funding_paid)}</td>
                  <td>{compactUsdt(entry.fees)}</td>
                  <td className={entry.net_pnl >= 0 ? "positive" : "negative"}>{entry.net_pnl >= 0 ? "+" : ""}{compactUsdt(entry.net_pnl)}</td>
                  <td className="journal-rationale">{entry.rationale || "—"}</td>
                  <td className="journal-hash" title={entry.entry_hash ?? ""}>{entry.entry_hash ? `${entry.entry_hash.slice(0, 8)}…` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "positive" | "negative" }) {
  return <div className="backtest-stat"><span>{label}</span><strong className={tone}>{value}</strong></div>;
}

/**
 * Portfolio risk, computed by the external analytics provider over QuantDesk's own
 * positions and returns. Read-only by construction: this panel can display a risk
 * figure and a stress result, and has no path to open, close or resize anything.
 */
function PortfolioRiskPanel() {
  const [status, setStatus] = useState<ExternalStatus | null>(null);
  const [outcome, setOutcome] = useState<AnalyticsOutcome | null>(null);
  const [scenarios, setScenarios] = useState<ExternalScenario[]>([]);
  const [scenario, setScenario] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void fetchExternalStatus()
      .then((value) => { if (active) setStatus(value); })
      .catch(() => { if (active) setStatus(null); });
    void fetchScenarios()
      .then(({ scenarios: list }) => {
        if (!active) return;
        setScenarios(list);
        setScenario((current) => current || list[0]?.id || "");
      })
      .catch(() => undefined);
    return () => { active = false; };
  }, []);

  const runRisk = async () => {
    setBusy("risk");
    setError(null);
    try {
      setOutcome(await runPortfolioRisk({}));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "组合风险计算失败");
    } finally {
      setBusy(null);
    }
  };

  const runShock = async () => {
    if (!scenario) return;
    setBusy("scenario");
    setError(null);
    try {
      setOutcome(await runScenario({ scenario }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "压力测试失败");
    } finally {
      setBusy(null);
    }
  };

  const enabled = status?.external.finceptEnabled ?? false;
  const snapshot = outcome?.snapshot ?? null;
  const metrics = (outcome?.result?.metrics ?? {}) as Record<string, number | null>;
  const contributions = (outcome?.result?.riskContributions ?? []) as Array<{ symbol: string; value: number; percentage: number }>;
  const correlation = outcome?.result?.correlation as { symbols: string[]; matrix: number[][] } | null | undefined;
  const positionRows = (outcome?.result?.positions ?? []) as Array<{ symbol: string; pnl: number; pnlPct?: number | null }>;

  return (
    <section className="risk-workspace">
      <div className="paper-section-heading">
        <h3>组合风险（Fincept 计算，QuantDesk 持仓与收益率）</h3>
        <span>{status ? (enabled ? `已启用 · ${status.plugins.analytics.pluginId ?? "未安装插件"}` : "未启用") : "读取状态中…"}</span>
      </div>
      {!enabled && (
        <p className="paper-notice">Fincept 组合分析未启用：风险数字需要外部计算服务。设置页可开启；QuantDesk 自有的保证金、强平与限额检查不受影响。</p>
      )}
      {enabled && (
        <div className="risk-scenarios">
          <button type="button" disabled={busy !== null} onClick={() => void runRisk()} className={outcome?.kind === "portfolio" ? "active" : ""}>
            组合风险（VaR / 波动率 / 风险贡献）
          </button>
          {scenarios.map((item) => (
            <button
              key={item.id}
              type="button"
              title={item.description}
              disabled={busy !== null}
              className={outcome?.kind === "scenario" && outcome.result?.scenario === item.id ? "active" : ""}
              onClick={() => { setScenario(item.id); void runShock(); }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
      {busy && <p className="paper-notice">正在计算…（{busy === "risk" ? "组合风险" : "压力测试"}）</p>}
      {error && <p className="paper-error">{error}</p>}
      {outcome && !outcome.ok && (
        <p className="paper-error">
          {outcome.unavailable || outcome.error || "外部分析不可用"}
          {outcome.result ? "" : "；本地保证金与限额检查仍然有效"}
        </p>
      )}
      {outcome?.ok && (
        <>
          <p className="paper-notice">
            {outcome.cached ? "命中缓存（未重复计费）" : `provider ${outcome.provider}`} · 市场快照 {outcome.marketVersion?.slice(0, 16) || "—"} · 耗时 {outcome.durationMs}ms
            {outcome.advisory ? ` · ${outcome.advisory}` : ""}
          </p>
          {snapshot && (
            <div className="risk-grid">
              <RiskCard label="总名义敞口" value={compactUsdt(snapshot.totalNotional)} note={`${snapshot.positions.length} 个持仓`} />
              <RiskCard label="多空净敞口" value={compactUsdt(snapshot.netExposure)} note={snapshot.netExposure >= 0 ? "净多" : "净空"} />
              <RiskCard label="保证金占用" value={compactUsdt(snapshot.marginUsed)} note={`权益 ${compactUsdt(snapshot.equity)}`} />
              <RiskCard label="组合波动率" value={fmtMetric(metrics.volatility, true)} note="年化，由外部服务计算" />
              <RiskCard label="VaR" value={fmtMetric(metrics.var, true)} note="给定置信度下的单期损失" />
              <RiskCard label="CVaR" value={fmtMetric(metrics.cvar, true)} note="尾部平均损失" />
              <RiskCard label="最大回撤" value={fmtMetric(metrics.maxDrawdown, true)} note="样本区间内" />
              {outcome.kind === "scenario" && (
                <>
                  <RiskCard label="权益变化" value={fmtMetric(outcome.result?.equityChange as number)} note={`${fmtMetric(outcome.result?.equityChangePct as number, true)}`} />
                  <RiskCard label="保证金使用率" value={`${fmtMetric(outcome.result?.marginUsageBefore as number, true)} → ${fmtMetric(outcome.result?.marginUsageAfter as number, true)}`} note="情景后" />
                  <RiskCard
                    label="账户风险限制"
                    value={outcome.result?.breachesAccountRisk ? "触发" : "未触发"}
                    note={(outcome.result?.breachedLimits as string[] | undefined)?.join("、") || "未触及"}
                  />
                </>
              )}
            </div>
          )}
          {contributions.length > 0 && (
            <div className="risk-contrib">
              <h4>风险贡献</h4>
              <table>
                <thead><tr><th>合约</th><th>贡献</th><th>占比</th><th /></tr></thead>
                <tbody>
                  {contributions.map((item) => (
                    <tr key={item.symbol}>
                      <td>{item.symbol}</td>
                      <td>{fmtMetric(item.value)}</td>
                      <td>{fmtMetric(item.percentage, true)}</td>
                      <td><span className="risk-bar"><i style={{ width: `${Math.min(100, Math.abs(item.percentage))}%` }} /></span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {positionRows.length > 0 && (
            <div className="risk-contrib">
              <h4>单仓损失（情景）</h4>
              <table>
                <thead><tr><th>合约</th><th>盈亏</th><th>幅度</th></tr></thead>
                <tbody>
                  {positionRows.map((row) => (
                    <tr key={row.symbol}><td>{row.symbol}</td><td>{fmtMetric(row.pnl)}</td><td>{fmtMetric(row.pnlPct, true)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {correlation && correlation.matrix?.length > 0 && (
            <div className="risk-contrib">
              <h4>相关性矩阵</h4>
              <table>
                <thead>
                  <tr><th />{correlation.symbols.map((symbol) => <th key={symbol}>{symbol}</th>)}</tr>
                </thead>
                <tbody>
                  {correlation.matrix.map((row, index) => (
                    <tr key={correlation.symbols[index] ?? index}>
                      <td>{correlation.symbols[index] ?? index}</td>
                      {row.map((value, column) => <td key={column}>{value.toFixed(2)}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      {(snapshot?.warnings?.length ?? 0) > 0 && (
        <ul className="paper-warnings">
          {snapshot?.warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      )}
      <p className="analysis-disclaimer">
        <IconAlertTriangle size={14} />
        外部风险结果只用于风险提示，不改变 QuantDesk 本地开仓与风控限制，也不具备下单能力。
      </p>
    </section>
  );
}

function RiskCard({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="risk-card">
      <span>{label}</span>
      <strong>{value}</strong>
      {note && <em>{note}</em>}
    </div>
  );
}

function fmtMetric(value: number | null | undefined, ratio = false): string {
  if (value == null || Number.isNaN(value)) return "—";
  if (ratio) return `${(value * 100).toFixed(2)}%`;
  return value.toFixed(4);
}
