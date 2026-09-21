import { useCallback, useEffect, useState } from "react";
import { IconAlertTriangle, IconBrain, IconCheck, IconInfoCircle, IconLoader2, IconSend } from "@tabler/icons-react";
import type { Candle, Instrument, Timeframe } from "../data/market";
import { formatPrice } from "../lib/format";
import {
  fetchArchivedReports,
  fetchResearchReadiness,
  runResearch,
  type ExternalEvidenceSummary,
  type ArchivedReport,
  type ResearchReadiness,
  type ResearchResult,
  type TradingPlan,
} from "../services/research";
import {
  cancelTradingAgentsJob,
  enqueueTradingAgents,
  fetchTradingAgentsCosts,
  fetchTradingAgentsJob,
  fetchTradingAgentsJobs,
  fetchTradingAgentsReadiness,
  type TradingAgentsCostSummary,
  type TradingAgentsJob,
  type TradingAgentsReadiness,
  type TradingAgentsReceipt,
  type TradingAgentsResult,
} from "../services/tradingagents";

type ResearchMode = "agents" | "quick";

const REPORT_LABELS: Record<string, string> = {
  market_report: "市场技术分析师",
  sentiment_report: "情绪分析师",
  news_report: "新闻分析师",
  fundamentals_report: "基本面分析师",
  investment_plan: "研究经理综合方案",
  trader_investment_plan: "交易员执行方案",
  final_trade_decision: "风控委员会最终决策",
};

interface Props {
  instrument: Instrument | null;
  timeframe: Timeframe;
  candles: Candle[];
  source: string;
  /** Hand a plan to the paper-trading form. */
  onSendToPaper: (plan: TradingPlan) => void;
}

export function ResearchWorkspace({ instrument, timeframe, candles, source, onSendToPaper }: Props) {
  const [mode, setMode] = useState<ResearchMode>("agents");
  const [readiness, setReadiness] = useState<ResearchReadiness | null>(null);
  const [agentsReadiness, setAgentsReadiness] = useState<TradingAgentsReadiness | null>(null);
  const [agentsResult, setAgentsResult] = useState<TradingAgentsResult | null>(null);
  const [agentsJob, setAgentsJob] = useState<TradingAgentsJob | null>(null);
  const [agentsCost, setAgentsCost] = useState<TradingAgentsCostSummary | null>(null);
  const [tradeDate, setTradeDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [focus, setFocus] = useState("");
  const [news, setNews] = useState("");
  const [includeBacktest, setIncludeBacktest] = useState(true);
  const [result, setResult] = useState<ResearchResult | null>(null);
  const [archived, setArchived] = useState<ArchivedReport[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadReadiness = useCallback(async () => {
    try {
      setReadiness(await fetchResearchReadiness());
    } catch {
      setReadiness(null);
    }
  }, []);

  const loadArchived = useCallback(async () => {
    try {
      setArchived((await fetchArchivedReports()).reports);
    } catch {
      setArchived([]);
    }
  }, []);

  useEffect(() => {
    void loadReadiness();
    void loadArchived();
  }, [loadReadiness, loadArchived]);

  useEffect(() => {
    let active = true;
    setAgentsReadiness(null);
    if (!instrument) return () => { active = false; };
    void fetchTradingAgentsReadiness(instrument.venueSymbol)
      .then((value) => { if (active) setAgentsReadiness(value); })
      .catch(() => { if (active) setAgentsReadiness(null); });
    void fetchTradingAgentsJobs(20)
      .then(({ jobs }) => {
        if (!active) return;
        const activeJob = jobs.find((job) => job.venue_symbol === instrument.venueSymbol && ["queued", "running"].includes(job.status));
        if (activeJob) setAgentsJob(activeJob);
      })
      .catch(() => undefined);
    return () => { active = false; };
  }, [instrument]);

  useEffect(() => {
    if (!agentsJob || !["queued", "running"].includes(agentsJob.status)) return;
    let active = true;
    const poll = async () => {
      try {
        const next = await fetchTradingAgentsJob(agentsJob.id);
        if (!active) return;
        setAgentsJob(next);
        if (next.status === "succeeded" && next.result) {
          setAgentsResult(next.result);
          setError(null);
          // The run just spent money; refresh the day's total so the figure the
          // operator sees is the one the engine will enforce.
          void fetchTradingAgentsCosts()
            .then((value) => { if (active) setAgentsCost(value); })
            .catch(() => undefined);
        } else if (next.status === "failed") {
          setAgentsResult(null);
          setError(next.error || "TradingAgents 任务失败");
        } else if (next.status === "cancelled") {
          setAgentsResult(null);
          setError("TradingAgents 任务已取消");
        }
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "读取任务状态失败");
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [agentsJob?.id, agentsJob?.status]);

  const run = async () => {
    if (!instrument) return;
    setBusy(true);
    setError(null);
    try {
      if (mode === "agents") {
        const submitted = await enqueueTradingAgents({ symbol: instrument.venueSymbol, tradeDate });
        const job = await fetchTradingAgentsJob(submitted.jobId);
        setAgentsJob(job);
        setAgentsResult(null);
        setResult(null);
        return;
      }
      const outcome = await runResearch({
        symbol: instrument.venueSymbol,
        timeframe,
        source: source === "upload" ? "upload" : source === "demo" ? "demo" : "live",
        candles,
        focus,
        news,
        includeBacktest,
      });
      setResult(outcome);
      setAgentsResult(null);
      void loadArchived();
    } catch (reason) {
      setResult(null);
      setAgentsResult(null);
      setError(reason instanceof Error ? reason.message : "研判失败");
    } finally {
      setBusy(false);
    }
  };

  const plan = result?.report?.trading_plan;
  const planCheck = result?.validation.plan;
  const blocked = mode === "agents"
    ? agentsReadiness && !agentsReadiness.ready
    : readiness && !readiness.ready;
  const blockedReason = mode === "agents" ? agentsReadiness?.reason : readiness?.reason;
  const demoBlocked = mode === "quick" && source === "demo";
  const agentsWorking = agentsJob?.status === "queued" || agentsJob?.status === "running";

  return (
    <div className="research-workspace">
      <header className="research-heading">
        <div>
          <h2>{mode === "agents" ? "TradingAgents 多智能体研判" : "快速模型研判"}</h2>
          <p>
            {mode === "agents"
              ? "运行真正的 TradingAgentsGraph：分析师取数后，由多空研究员辩论、交易员形成方案，再经激进、中性、保守风控辩论给出最终决策。"
              : "引擎用当前图表K线、四周期立场、衍生品与回测摘要，执行一次证据约束的快速模型研判。"}
          </p>
        </div>
        <span className="research-scope">
          {instrument ? `${instrument.displaySymbol} · ${timeframe} · ${candles.length} 根` : "等待合约池"}
        </span>
      </header>

      {blocked && (
        <p className="research-blocked" role="status">
          <IconAlertTriangle size={15} />
          <span>{blockedReason}</span>
          {mode === "quick" && readiness?.action && <em>{readiness.action}</em>}
        </p>
      )}
      {demoBlocked && (
        <p className="research-blocked" role="status">
          <IconAlertTriangle size={15} />
          <span>演示数据不能用于深度研判，请连接 Bybit 或上传真实历史K线。</span>
        </p>
      )}

      <div className="research-layout">
        <section className="research-form">
          <div className="research-mode" role="group" aria-label="研判模式">
            <button type="button" className={mode === "agents" ? "active" : ""} onClick={() => setMode("agents")}>多智能体</button>
            <button type="button" className={mode === "quick" ? "active" : ""} onClick={() => setMode("quick")}>快速研判</button>
          </div>

          {mode === "agents" ? (
            <>
              <label>研判日期
                <input type="date" value={tradeDate} max={new Date().toISOString().slice(0, 10)} onChange={(event) => setTradeDate(event.target.value)} />
              </label>
              <div className="agents-route">
                <span>分析代码</span><strong>{agentsReadiness?.target?.symbol ?? "—"}</strong>
                <span>资产类型</span><strong>{agentsReadiness?.target?.asset_type === "crypto" ? "加密资产" : "基础证券"}</strong>
                {agentsReadiness?.target?.fundamental_symbol && <><span>基本面代码</span><strong>{agentsReadiness.target.fundamental_symbol}</strong></>}
                <span>智能体</span><strong>{agentsReadiness?.target?.analysts?.join(" / ") ?? "正在检查"}</strong>
              </div>
            </>
          ) : (
            <>
              <label>额外关注点（可选）
                <textarea rows={2} maxLength={300} value={focus} placeholder="例如：关注 521.6 上方的突破是否有效" onChange={(event) => setFocus(event.target.value)} />
              </label>
              <label>新闻或公告（可选，会标注为外部输入）
                <textarea rows={3} maxLength={2000} value={news} placeholder="粘贴与标的相关的公开消息" onChange={(event) => setNews(event.target.value)} />
              </label>
              <label className="research-check">
                <input type="checkbox" checked={includeBacktest} onChange={(event) => setIncludeBacktest(event.target.checked)} />
                附上规则回测摘要作为证据
              </label>
            </>
          )}

          <button type="button" className="analysis-run" onClick={run} disabled={busy || (mode === "agents" && agentsWorking) || !instrument || Boolean(blocked) || demoBlocked}>
            {busy || (mode === "agents" && agentsWorking) ? <IconLoader2 size={16} className="spin" /> : <IconBrain size={16} />}
            {mode === "agents" && agentsWorking ? (agentsJob?.progress || "任务处理中…") : busy ? "研判中…" : "开始研判"}
          </button>
          {mode === "agents" && agentsWorking && (
            <div className="agents-job-state" role="status">
              <span>任务 {agentsJob.id.slice(0, 8)}</span>
              <strong>{agentsJob.status === "queued" ? "排队中" : "运行中"}</strong>
              <button type="button" onClick={() => void cancelTradingAgentsJob(agentsJob.id).then(setAgentsJob)}>取消</button>
            </div>
          )}

          <p className="research-source-note">
            {mode === "agents"
              ? "多智能体会独立拉取日线市场、新闻与基本面资料。代币化股票使用 Bybit 合约行情，BTC/ETH 使用 Hyperliquid 公共行情桥；全程不会下单。"
              : `价格结构取样自当前图表（${source === "upload" ? "上传文件" : source === "demo" ? "演示数据" : "Bybit 实时"}，共 ${candles.length} 根）。`}
          </p>

          {error && <p className="analysis-error" role="alert">{error}</p>}

          {mode === "quick" && archived.length > 0 && (
            <div className="research-archive">
              <h4>历史报告</h4>
              <ul>
                {archived.slice(0, 6).map((item) => (
                  <li key={item.id}>
                    <span className={item.verified ? "archive-verified" : "archive-unverified"}>
                      {item.verified ? <IconCheck size={11} /> : <IconAlertTriangle size={11} />}
                    </span>
                    <strong>{item.symbol.replace("USDT", "")}</strong>
                    <span className="archive-headline">{item.headline ?? "—"}</span>
                    <time>{new Date(item.createdAt).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}</time>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>

        <section className="research-output">
          {mode === "agents" && agentsResult ? (
            <>
              <TradingAgentsOutput result={agentsResult} />
              <AgentsReceipt result={agentsResult} cost={agentsCost} />
            </>
          ) : !result ? (
            <div className="backtest-empty">
              <strong>{mode === "agents" ? "运行后生成完整多智能体报告" : "运行后生成研判与交易计划"}</strong>
              <span>{mode === "agents" ? "结果会保存分析师报告、两轮辩论状态、最终评级与运行元数据。" : "报告会落库保存，含所用 profile、模型、证据键与校验结果。"}</span>
            </div>
          ) : (
            <>
              <div className="analysis-meta">
                <span>profile {result.profile}</span>
                <span>model {result.model}</span>
                <span>{result.latencySeconds}s</span>
                {result.usage?.total_tokens != null && <span>{result.usage.total_tokens} tokens</span>}
                {/* The engine already folds the plan check into `verified`; the
                    UI must not re-decide it, or the two can disagree. */}
                <span
                  data-verified={String(result.validation.verified)}
                  data-plan-ok={String(planCheck?.ok ?? false)}
                  className={result.validation.verified ? "confidence-high" : "confidence-low"}
                >
                  {result.validation.verified ? "点位与数值校验通过" : "有待核对项"}
                </span>
              </div>

              {result.externalEvidence && <ExternalEvidencePanel value={result.externalEvidence} />}

              {plan && (
                <section className={`plan-card ${planCheck?.ok ? "" : "plan-card-bad"}`}>
                  <header>
                    <h3>
                      交易计划
                      <em className={`plan-direction plan-${plan.direction ?? "wait"}`}>
                        {plan.direction === "long" ? "做多" : plan.direction === "short" ? "做空" : "等待触发"}
                      </em>
                    </h3>
                    <button type="button" className="plan-send" onClick={() => onSendToPaper(plan)}>
                      <IconSend size={13} />送到模拟盘
                    </button>
                  </header>
                  <div className="plan-levels">
                    <div className="plan-level plan-stop">
                      <span>止损</span>
                      <strong>{formatPrice(plan.stop_loss)}</strong>
                      <em>{planCheck?.computed.riskPct != null ? `风险 ${planCheck.computed.riskPct.toFixed(2)}%` : "—"}</em>
                    </div>
                    <div className="plan-level plan-entry">
                      <span>入场</span>
                      <strong>{formatPrice(plan.entry)}</strong>
                      <em>{plan.entry_zone?.length === 2 ? `${formatPrice(plan.entry_zone[0])} – ${formatPrice(plan.entry_zone[1])}` : "—"}</em>
                    </div>
                    <div className="plan-level plan-tp">
                      <span>止盈 1</span>
                      <strong>{formatPrice(plan.take_profit_1)}</strong>
                      <em>{planCheck?.computed.rewardPct != null ? `空间 ${planCheck.computed.rewardPct.toFixed(2)}%` : "—"}</em>
                    </div>
                    <div className="plan-level plan-tp">
                      <span>止盈 2</span>
                      <strong>{formatPrice(plan.take_profit_2)}</strong>
                      <em>{plan.timeframe ? `${plan.timeframe} 周期` : "—"}</em>
                    </div>
                  </div>
                  <dl className="plan-facts">
                    <div>
                      <dt>盈亏比（引擎重算）</dt>
                      <dd>{planCheck?.computed.riskReward ?? "—"}
                        {plan.risk_reward != null && planCheck?.computed.riskReward != null && Math.abs(plan.risk_reward - planCheck.computed.riskReward) > 0.15 && (
                          <em className="plan-mismatch">模型自报 {plan.risk_reward}</em>
                        )}
                      </dd>
                    </div>
                    <div><dt>止损距离 / ATR</dt><dd>{planCheck?.computed.stopAtrMultiple ?? "—"}</dd></div>
                    <div><dt>建议仓位</dt><dd>{plan.position_size_pct != null ? `${plan.position_size_pct}%` : "—"}</dd></div>
                    <div><dt>有效期</dt><dd>{plan.valid_until ?? "—"}</dd></div>
                  </dl>
                  {plan.rationale && <p className="plan-rationale">{plan.rationale}</p>}
                  {plan.trigger && <p className="plan-trigger">触发条件：{plan.trigger}</p>}
                  {planCheck && planCheck.problems.length > 0 && (
                    <ul className="plan-problems">
                      {planCheck.problems.map((problem, index) => <li key={index}><IconAlertTriangle size={12} />{problem}</li>)}
                    </ul>
                  )}
                  {planCheck && planCheck.notes.length > 0 && (
                    <ul className="plan-notes">
                      {planCheck.notes.map((note, index) => <li key={index}>{note}</li>)}
                    </ul>
                  )}
                </section>
              )}

              {result.report?.headline && (
                <p className="research-headline"><strong>{result.report.headline}</strong>{result.report.confidence && <em>置信度 {result.report.confidence}</em>}</p>
              )}

              {result.report?.facts && result.report.facts.length > 0 && (
                <section className="analysis-block">
                  <h3>事实（来自证据包）</h3>
                  <ul>
                    {result.report.facts.map((fact, index) => (
                      <li key={index}>{fact.statement}<small>{fact.evidence?.join(" · ")}</small></li>
                    ))}
                  </ul>
                </section>
              )}

              {result.report?.inferences && result.report.inferences.length > 0 && (
                <section className="analysis-block">
                  <h3>推断</h3>
                  <ul>
                    {result.report.inferences.map((inference, index) => (
                      <li key={index}>{inference.statement}<small>依据 {inference.basis?.join(" · ")}｜置信度 {inference.confidence ?? "—"}</small></li>
                    ))}
                  </ul>
                </section>
              )}

              {result.report?.scenarios && result.report.scenarios.length > 0 && (
                <section className="analysis-block">
                  <h3>情形</h3>
                  <ul>
                    {result.report.scenarios.map((scenario, index) => (
                      <li key={index}><strong>{scenario.name}</strong>：{scenario.condition} → {scenario.implication}</li>
                    ))}
                  </ul>
                </section>
              )}

              {result.report?.invalidation && result.report.invalidation.length > 0 && (
                <section className="analysis-block">
                  <h3>失效条件</h3>
                  <ul>{result.report.invalidation.map((line, index) => <li key={index}>{line}</li>)}</ul>
                </section>
              )}

              <section className="analysis-block analysis-uncertain">
                <h3><IconAlertTriangle size={14} />无法确认 / 数据缺口</h3>
                <ul>
                  {[...(result.report?.missing_evidence ?? []), ...(result.report?.data_caveats ?? []), ...result.missing].map((line, index) => (
                    <li key={index}>{line}</li>
                  ))}
                  {!result.missing.length && !(result.report?.missing_evidence ?? []).length && <li>无</li>}
                </ul>
              </section>

              <details className="audit-panel">
                <summary>证据与校验（{result.evidence.length} 条可引用证据）</summary>
                <div className="audit-grid">
                  <div>
                    <h4>价格结构</h4>
                    <p>来源：{result.priceStructure.source === "live" || result.priceStructure.source === "upload" ? "当前图表" : "引擎独立取数"} · {result.priceStructure.interval ?? timeframe} · {result.priceStructure.bars} 根</p>
                    <p>ATR {formatPrice(result.priceStructure.atr)} · 近20根 {formatPrice(result.priceStructure.recent_low)} – {formatPrice(result.priceStructure.recent_high)} · 位于区间 {result.priceStructure.recent_range_position_pct?.toFixed(1)}%</p>
                    <p>摆动低点 {result.priceStructure.swing_lows.slice(-4).map((v) => formatPrice(v)).join(" / ") || "—"}</p>
                    <p>摆动高点 {result.priceStructure.swing_highs.slice(-4).map((v) => formatPrice(v)).join(" / ") || "—"}</p>
                  </div>
                  <div>
                    <h4>校验结果</h4>
                    <p>引用键 {result.validation.citedKeys.length} 个，无效 {result.validation.unknownCitedKeys.length} 个</p>
                    <p>无法定位的数值 {result.validation.unsupportedNumbers.length} 个</p>
                    <p>越权指令用语 {result.validation.directives.length} 处</p>
                    <p>计划自洽：{planCheck?.ok ? "是" : "否"}</p>
                    {result.validation.unsupportedNumbers.length > 0 && (
                      <ul className="audit-list">
                        {result.validation.unsupportedNumbers.slice(0, 8).map((item, index) => (
                          <li key={index}>{item.where}：{item.number}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
                <div className="audit-evidence">
                  {result.evidence.map((item) => (
                    <div key={item.key}>
                      <code>{item.key}</code>
                      <span>{item.label}</span>
                      <strong>{typeof item.value === "object" ? JSON.stringify(item.value) : String(item.value)}</strong>
                      <em>{item.source}</em>
                    </div>
                  ))}
                </div>
              </details>

              <p className="analysis-disclaimer"><IconInfoCircle size={14} />{result.disclaimer}</p>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

/**
 * The external evidence behind a research run: providers, publish times, sources,
 * and the reasons anything is missing. A verdict without this is a claim; with it,
 * it is checkable.
 */
function ExternalEvidencePanel({ value }: { value: ExternalEvidenceSummary }) {
  if (!value.enabled) {
    return (
      <section className="external-evidence">
        <header><strong>外部证据</strong><span>未启用（设置页可开启 OpenBB 研究）</span></header>
      </section>
    );
  }
  const gaps = [...(value.unavailable || []), ...(value.rejected || []), ...(value.errors || [])];
  return (
    <section className={`external-evidence ${value.degraded ? "external-degraded" : ""}`}>
      <header>
        <strong>外部证据（OpenBB）</strong>
        <span>
          可用 {value.usable} 条 · Provider {value.providers.join("、") || "—"} · 缓存命中 {value.cacheHits}/{value.calls}
          {value.degraded ? " · 证据降级" : ""}
        </span>
      </header>
      {value.records && value.records.length > 0 && (
        <table>
          <thead>
            <tr><th>主题</th><th>读数</th><th>Provider</th><th>数据时间</th><th>发布时间</th><th>来源</th></tr>
          </thead>
          <tbody>
            {value.records.map((row) => (
              <tr key={row.key}>
                <td>{row.topic}</td>
                <td>{row.label}</td>
                <td>{row.provider}</td>
                <td>{row.asOf || "—"}</td>
                <td>{row.publishedAt || "未提供"}</td>
                <td>{row.source?.startsWith("http") ? <a href={row.source} target="_blank" rel="noreferrer">链接</a> : row.source || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {gaps.length > 0 && (
        <>
          <p>缺失或不可用（{gaps.length} 项）：</p>
          <ul>
            {gaps.map((item, index) => (
              <li key={`${item.topic}-${index}`}>
                {item.topic || "未知主题"}：{item.reason || "无原因"}
              </li>
            ))}
          </ul>
        </>
      )}
      {value.degraded && <p>证据降级：外部数据不完整，本报告置信度应相应下调。</p>}
    </section>
  );
}

function TradingAgentsOutput({ result }: { result: TradingAgentsResult }) {
  return (
    <div className="agents-output">
      <div className="analysis-meta">
        <span>profile {result.profile}</span>
        <span>{result.meta.provider ?? "—"}</span>
        <span>{result.meta.deep_model ?? "—"}</span>
        <span>{result.meta.duration_seconds ?? "—"}s</span>
        <span className={result.requires_review ? "confidence-low" : "confidence-high"}>评级 {result.rating}</span>
      </div>
      <section className="agents-summary">
        <span>分析标的</span><strong>{result.display_symbol} → {result.symbol}</strong>
        <span>研判日期</span><strong>{result.trade_date}</strong>
        <span>数据来源</span><strong>{result.meta.market_data_source ?? "—"}</strong>
        {result.meta.fundamental_symbol && <><span>基本面标的</span><strong>{result.meta.fundamental_symbol}</strong></>}
        <span>运行智能体</span><strong>{result.analysts.join(" / ")}</strong>
      </section>
      {Object.entries(result.reports).map(([key, content]) => (
        <section className={`agent-report ${key === "final_trade_decision" ? "agent-report-final" : ""}`} key={key}>
          <h3>{REPORT_LABELS[key] ?? key}</h3>
          <div className="agent-report-body">{content}</div>
        </section>
      ))}
      <details className="audit-panel">
        <summary>查看研究与风控辩论原始状态</summary>
        <pre className="agents-debate">{JSON.stringify(result.debates, null, 2)}</pre>
      </details>
      {result.externalEvidence && <ExternalEvidencePanel value={result.externalEvidence} />}
      {result.warnings?.map((warning) => <p className="analysis-error" key={warning}>{warning}</p>)}
      <p className="analysis-disclaimer"><IconInfoCircle size={14} />多智能体输出用于研究与模拟验证，不会触发实盘交易。</p>
    </div>
  );
}

function formatUsd(value: number | null | undefined): string {
  if (value == null) return "—";
  return `$${value.toFixed(4)}`;
}

/**
 * The receipt for a paid run: what it cost, which data it read, which analysts
 * actually reported, and whether the answer is allowed to count as a rating.
 * Shown next to the report because a verdict without its provenance is a claim,
 * not evidence.
 */
function AgentsReceipt({ result, cost }: { result: TradingAgentsResult & Partial<TradingAgentsReceipt>; cost: TradingAgentsCostSummary | null }) {
  const receipt = result.cost ?? null;
  const usage = Object.entries(receipt?.usage ?? {});
  const coverage = result.analystCoverage ?? null;
  const data = result.data ?? null;
  const tokens = usage.reduce((sum, [, item]) => sum + (item.total ?? 0), 0);
  return (
    <section className="agents-receipt">
      <header>
        <strong>运行回执</strong>
        <span>
          {receipt?.usd == null ? "费用未知" : `${formatUsd(receipt.usd)} · ${tokens} tokens`}
          {cost?.today ? ` · 今日累计 ${formatUsd(cost.today.usd)}` : ""}
          {cost?.budgetState?.remainingTodayUsd != null ? ` · 剩余 ${formatUsd(cost.budgetState.remainingTodayUsd)}` : ""}
        </span>
      </header>
      <div className="agents-receipt-grid">
        <span>运行编号</span><strong>{result.runId}</strong>
        <span>耗时 / 重试</span>
        <strong>
          {result.durationS != null ? `${result.durationS.toFixed(1)}s` : "—"}
          {` · 失败重试 ${result.retries ?? 0} · 分析师重试 ${result.analystRetries ?? 0}`}
        </strong>
        <span>数据时间</span>
        <strong>
          {data?.asOf ?? "—"}
          {data?.version ? ` · ${data.version}` : ""}
          {data?.bars != null ? ` · ${data.bars} 根` : ""}
        </strong>
        <span>数据状态</span>
        <strong className={data?.stale ? "confidence-low" : "confidence-high"}>
          {data?.available === false
            ? data.reason ?? "本地没有可用历史"
            : data?.stale
              ? `${data.staleReason ?? "数据过期"}（差 ${data.staleByDays ?? 0} 天），不作为正常评级`
              : data?.complete === false
                ? `交易时段内缺 ${data.missingInSession ?? 0} 根K线`
                : "已覆盖研判日期"}
        </strong>
        <span>分析师覆盖</span>
        <strong className={coverage && !coverage.complete ? "confidence-low" : "confidence-high"}>
          {coverage ? `${coverage.covered.length}/${coverage.requested.length}` : "—"}
          {coverage?.missing.length ? ` · 缺失 ${coverage.missing.join("、")}（未参与结论）` : ""}
        </strong>
        <span>模型与费用</span>
        <strong>
          {usage.length === 0
            ? "—"
            : usage.map(([model, item]) => `${model} ${item.calls} 次 ${item.total} tokens`).join(" / ")}
        </strong>
      </div>
      {receipt?.budget && !receipt.budget.allowed && (
        <p className="analysis-error">费用闸门：{receipt.budget.problems.join("；")}</p>
      )}
      {result.failure?.message && <p className="analysis-error">失败原因：{result.failure.message}</p>}
      {result.reused && <p className="analysis-note"><IconInfoCircle size={14} />命中复用窗口内的同配置结果，本次未产生费用。</p>}
    </section>
  );
}
