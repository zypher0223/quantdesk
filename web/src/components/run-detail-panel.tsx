import { useState } from "react";
import { IconAlertTriangle, IconExternalLink, IconShieldCheck } from "@tabler/icons-react";
import { formatPrice } from "../lib/format";
import { CpaResultSection } from "./cpa-result-block";
import { FactorPanel } from "./factor-panel";
import { SandboxChip } from "./sandbox-chip";
import { validateRun, validationNotes, verdictKindLabel } from "../services/factors";
import { countText, decimalText, drawdownText, percentText, progressOf, toneOf } from "../services/run-figures";
import {
  artifactMediaLabel,
  artifactUrl,
  checkStatusLabel,
  equityPointsOf,
  errorKindLabel,
  executionModelOf,
  formatBytes,
  formatDuration,
  formatRunTime,
  isFactorRun,
  provenanceOf,
  resolveHeadline,
  runKindLabel,
  runStatusLabel,
  verdictLabel,
  type ExecutionModel,
  type RunDetail,
  type RunProvenance,
  type ValidationVerdict,
} from "../services/runs";

/**
 * One run's full record.
 *
 * Every block is the engine's own answer, printed as written: the gate's verdict,
 * the execution assumptions, the verdict rows and the artifacts. A figure the run
 * did not report prints as "—", and a factor run — which has no P&L at all —
 * shows its coverage instead of a grid of dashes.
 */
export function RunDetailPanel({ run, validateBusy, analysisNotes, onValidate }: {
  run: RunDetail;
  validateBusy: boolean;
  analysisNotes: string[];
  onValidate: () => void;
}) {
  const [factorsOpen, setFactorsOpen] = useState(false);
  const provenance = provenanceOf(run, run.readiness, run.result);
  const headline = resolveHeadline(run.headline, run.result);
  const points = equityPointsOf(run.result);
  const execution = executionModelOf(run.result);
  const factorRun = isFactorRun(run);
  const ablationRun = run.kind === "cpa_ablation";
  const verdicts: Array<ValidationVerdict & { kind: string }> = run.validation?.length
    ? run.validation
    : (run.verdicts ?? []).map((note) => ({ ...note, statistic: null, pValue: null, threshold: null }));
  const withStatistics = verdicts.some((row) => row.statistic !== null || row.pValue !== null || row.threshold !== null);
  // The isolation column appears only when the engine recorded one: an older
  // verdict set has nothing to say about sandboxing, and a column of 未知 would
  // imply it had been asked.
  const withSandbox = verdicts.some((row) => (row.sandbox ?? "").trim().length > 0);
  const artifacts = run.artifactIndex?.length
    ? run.artifactIndex.map((entry) => ({ name: entry.name, mediaType: entry.mediaType, bytes: entry.bytes as number | null, sha256: entry.sha256, createdTs: entry.createdTs as number | null }))
    : (run.artifacts ?? []).map((name) => ({ name, mediaType: "", bytes: null, sha256: "", createdTs: null }));

  return <>
    <header className="run-detail-heading">
      <div>
        <h3>#{run.id} · {runKindLabel(run.kind)} · {run.displaySymbol ?? run.symbol ?? "—"}</h3>
        <p>
          {run.label || run.strategyId} · {run.interval || "—"} · 策略 {run.strategyId} v{run.strategyVersion || "—"}
          {" · "}提交 {formatRunTime(run.queuedTs)}
          {run.startedTs ? ` · 开始 ${formatRunTime(run.startedTs)}` : ""}
          {run.finishedTs ? ` · 结束 ${formatRunTime(run.finishedTs)}` : ""}
        </p>
      </div>
      <div className="run-detail-actions">
        <div className="run-detail-status">
          <em className={`history-status status-${run.status}`}>{runStatusLabel(run.status)}</em>
          <span>{progressOf(run.progress).toFixed(0)}% · {run.progressLabel || run.stage || "—"}</span>
          <small>{run.attempts > 0 ? `尝试 ${run.attempts} 次 · ` : ""}耗时 {formatDuration(run.durationMs)}</small>
        </div>
        {run.status === "done" && <button
          className="run-validate"
          type="button"
          title="对该结果做统计验证（路径风险、自助重采样、随机化、多重检验校正）"
          disabled={validateBusy}
          onClick={onValidate}
        >
          <IconShieldCheck size={14} />{validateBusy ? "统计验证中…" : "统计验证"}
        </button>}
      </div>
    </header>

    {run.error && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{errorKindLabel(run.errorKind)}：{run.error}</p>}

    {provenance.severity === "degraded" && <ProvenanceBanner provenance={provenance} />}

    <div className="run-headline-scope">
      指标口径：{run.headline?.scope || (factorRun ? "因子覆盖率（无盈亏指标）" : run.kind === "validate" ? "样本外" : run.kind === "portfolio" ? "组合合并账本" : "整段回测")}
    </div>

    {ablationRun
      ? <p className="backtest-hint">该任务比较同一资产组的 CPA 策略变体；完整指标见下方消融对比。</p>
      : factorRun
      ? <div className="backtest-stats">
        <Stat label="因子数" value={countText(headline.factors)} />
        <Stat label="有值因子" value={countText(headline.coveredFactors)} />
        <Stat label="中位覆盖" value={barsText(headline.medianCoverage)} />
        <Stat label="窗口K线" value={barsText(headline.bars)} />
      </div>
      : <div className="backtest-stats">
        <Stat label="净收益" value={percentText(headline.netReturnPct, true)} tone={toneOf(headline.netReturnPct)} />
        <Stat label="最大回撤" value={drawdownText(headline.maxDrawdownPct)} tone={headline.maxDrawdownPct === null ? undefined : "negative"} />
        <Stat label="交易次数" value={countText(headline.trades)} />
        <Stat label="夏普" value={decimalText(headline.sharpe)} />
        <Stat label="盈亏比" value={decimalText(headline.profitFactor)} />
        <Stat label="胜率" value={percentText(headline.winRatePct)} />
      </div>}

    {!ablationRun && <section className="backtest-chart-panel">
      {factorRun
        ? <><div><h3>净值曲线</h3><span>因子任务只产出覆盖率</span></div><p className="backtest-hint">因子计算没有盈亏，也没有可绘制的净值曲线；覆盖数字见上方与因子库面板。</p></>
        : points.length < 2
          ? <><div><h3>净值曲线</h3><span>{run.result ? "该结果未包含净值曲线" : "任务尚未产出结果"}</span></div><p className="backtest-hint">没有可绘制的净值点。</p></>
          : <EquityCurve points={points} />}
    </section>}

    {execution && !factorRun && !ablationRun && <ExecutionModelBlock model={execution} />}

    <section className="run-verdicts">
      <div className="run-section-heading"><h3>验证结论</h3><span>{verdicts.length} 项</span></div>
      {analysisNotes.length > 0 && <ul className="run-validation-notes">
        {analysisNotes.map((note) => <li key={note}>{note}</li>)}
      </ul>}
      {verdicts.length === 0
        ? <p className="backtest-hint">该任务没有验证结论。</p>
        : <div className="backtest-table-wrap">
          <table>
            <thead>
              <tr>
                <th>项目</th><th>结论</th>{withSandbox && <th>隔离</th>}
                {withStatistics && <><th>统计量</th><th>p 值</th><th>阈值</th></>}<th>说明</th>
              </tr>
            </thead>
            <tbody>
              {verdicts.map((row, index) => (
                <tr key={`${row.kind}-${index}`}>
                  <td title={row.kind}>{verdictKindLabel(row.kind)}</td>
                  <td><em className={`run-verdict verdict-${row.verdict}`}>{verdictLabel(row.verdict)}</em></td>
                  {withSandbox && <td><SandboxChip value={row.sandbox} /></td>}
                  {withStatistics && <>
                    <td>{decimalText(row.statistic, 4)}</td>
                    <td>{decimalText(row.pValue, 4)}</td>
                    <td>{decimalText(row.threshold, 4)}</td>
                  </>}
                  <td title={row.detail}>{row.detail || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>}
    </section>

    {ablationRun && <CpaAblationResult result={run.result} />}

    {/* CPA runs carry a phase series; every other run renders nothing here. */}
    <CpaResultSection
      result={run.result}
      runId={run.id}
      artifacts={artifacts.map((entry) => entry.name)}
    />

    <section className="run-artifacts">
      <div className="run-section-heading"><h3>结果文件</h3><span>{artifacts.length} 个</span></div>
      {artifacts.length === 0
        ? <p className="backtest-hint">该任务没有产出文件。</p>
        : <ul>
          {artifacts.map((entry) => (
            <li key={entry.name}>
              <a href={artifactUrl(run.id, entry.name)} target="_blank" rel="noreferrer">
                {entry.name}<IconExternalLink size={12} />
              </a>
              <span>
                {artifactMediaLabel(entry.mediaType)} · {formatBytes(entry.bytes)}
                {entry.sha256 ? ` · sha256 ${entry.sha256.slice(0, 12)}…` : ""}
                {entry.createdTs ? ` · ${formatRunTime(entry.createdTs)}` : ""}
              </span>
            </li>
          ))}
        </ul>}
    </section>

    <details className="run-request">
      <summary>提交参数</summary>
      <pre>{JSON.stringify(run.request ?? {}, null, 2)}</pre>
    </details>

    {/* The library is asked for nothing until it is opened: no polling, and no
        catalogue read for an operator who only wanted the study's numbers. */}
    <details className="run-factor-library" onToggle={(event) => setFactorsOpen(event.currentTarget.open)}>
      <summary>
        <span className="run-section-heading"><h3>因子库</h3><span>目录、计算与覆盖率</span></span>
      </summary>
      {factorsOpen && <FactorPanel />}
    </details>
  </>;
}

function CpaAblationResult({ result }: { result: Record<string, unknown> | null }) {
  const rows = Array.isArray(result?.table)
    ? result.table.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    : [];
  const pbo = result?.pbo && typeof result.pbo === "object"
    ? result.pbo as Record<string, unknown>
    : null;
  const number = (value: unknown, digits = 3) =>
    typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
  return <section className="run-verdicts">
    <div className="run-section-heading">
      <h3>CPA 消融对比</h3>
      <span>{rows.length} 个变体 · PBO {number(pbo?.pbo)}</span>
    </div>
    <p className="backtest-hint">
      统计维度：策略变体；组内合约先按每根K线收益等权合并。收益为负不等于运行失败。
    </p>
    {rows.length === 0
      ? <p className="backtest-hint">该任务尚未产出消融表。</p>
      : <div className="backtest-table-wrap"><table>
        <thead><tr>
          <th>变体</th><th>交易</th><th>均值收益</th><th>最差回撤</th>
          <th>均值 Sharpe</th><th>DSR</th><th>PBO</th>
        </tr></thead>
        <tbody>{rows.map((row, index) => {
          const summary = row.summary && typeof row.summary === "object"
            ? row.summary as Record<string, unknown>
            : {};
          return <tr key={String(row.variant ?? index)}>
            <td title={String(row.note ?? "")}>{String(row.label ?? row.variant ?? "—")}</td>
            <td>{number(summary.trades, 0)}</td>
            <td>{number(summary.meanReturnPct, 2)}%</td>
            <td>{number(summary.worstDrawdownPct, 2)}%</td>
            <td>{number(summary.meanSharpe)}</td>
            <td title={String(row.dsrReason ?? "")}>{number(row.deflatedSharpe)}</td>
            <td title={String(row.pboReason ?? "")}>{number(row.pbo)}</td>
          </tr>;
        })}</tbody>
      </table></div>}
  </section>;
}


/**
 * The gate's verdict, printed as the engine wrote it: what was missing, what it
 * costs, and what to do about it. Never softened — a degraded study must not
 * look formal.
 */
function ProvenanceBanner({ provenance }: { provenance: RunProvenance }) {
  return <section className={`study-provenance study-${provenance.severity}`}>
    <header>
      <strong>{provenance.title}</strong>
      <span>{provenance.missing.length ? `缺失 ${provenance.missing.length} 项数据` : "数据口径完整"}</span>
    </header>
    {provenance.detail && <p className="study-degraded-note">{provenance.detail}</p>}
    {provenance.action && <p className="study-degraded-note">建议：{provenance.action}</p>}
    {(provenance.blocking.length > 0 || provenance.missing.length > 0 || provenance.impacts.length > 0) && <div className="study-gaps">
      {provenance.blocking.length > 0 && <ul>
        {provenance.blocking.map((item) => <li key={item.key || item.label}>阻塞：{item.label || item.key || "未命名检查"}{item.detail ? ` — ${item.detail}` : ""}</li>)}
      </ul>}
      {provenance.missing.length > 0 && <ul>{provenance.missing.map((item) => <li key={item}>缺失：{item}</li>)}</ul>}
      {provenance.impacts.length > 0 && <ul>{provenance.impacts.map((item) => <li key={item}>影响：{item}</li>)}</ul>}
    </div>}
    {provenance.checks.length > 0 && <details className="run-checks">
      <summary><IconShieldCheck size={12} />门禁检查 {provenance.checks.length} 项</summary>
      <ul>{provenance.checks.map((check) => (
        <li key={check.key || check.label}>
          <em className={`run-verdict verdict-${verdictTone(check.status)}`}>{checkStatusLabel(check.status)}</em>
          <strong>{check.label || check.key}</strong>
          <span>{check.detail || "—"}</span>
        </li>
      ))}</ul>
    </details>}
  </section>;
}

/** The curve is drawn by hand: sampled to ~90 points, then one SVG path. */
function EquityCurve({ points }: { points: Array<{ time: number; equity: number }> }) {
  const sampled = points.filter((_, index, all) => index === 0 || index === all.length - 1 || index % Math.max(1, Math.floor(all.length / 90)) === 0);
  const values = sampled.map((point) => point.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(max - min, 1);
  const line = sampled.map((point, index) => `${index === 0 ? "M" : "L"} ${(index / Math.max(1, sampled.length - 1)) * 1000} ${250 - ((point.equity - min) / range) * 220}`).join(" ");
  const last = sampled[sampled.length - 1];
  return <>
    <div><h3>净值曲线</h3><span>{formatRunTime(sampled[0].time)} → {formatRunTime(last.time)} · 期末 {formatPrice(last.equity)} USDT</span></div>
    <svg viewBox="0 0 1000 270" preserveAspectRatio="none" role="img" aria-label="回测账户净值曲线">
      <line x1="0" x2="1000" y1="250" y2="250" />
      <path d={line} />
    </svg>
  </>;
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "positive" | "negative" }) {
  return <div className="backtest-stat"><span>{label}</span><strong className={tone}>{value}</strong></div>;
}

/** A bar count with its unit; an unreported count stays a bare "—". */
function barsText(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? "—" : `${countText(value)} 根`;
}

/**
 * How the study turned signals into fills.
 *
 * The engine states these assumptions once per result; the page prints them as
 * written. The unfilled row appears only when something actually went unfilled —
 * a row of zeros would read as a measurement. A number the result does not carry
 * prints as "—".
 */
function ExecutionModelBlock({ model }: { model: ExecutionModel }) {
  const unfilled = unfilledText(model);
  return <section className="run-execution">
    <div className="run-section-heading"><h3>执行模型</h3><span>信号如何变成成交</span></div>
    <dl>
      {model.fillRule && <div><dt>成交规则</dt><dd>{model.fillRule}</dd></div>}
      {model.signalBar && <div><dt>信号K线</dt><dd>{model.signalBar}</dd></div>}
      {model.fillPrice && <div><dt>成交价</dt><dd>{model.fillPrice}</dd></div>}
      <div><dt>延迟</dt><dd>{latencyText(model)}</dd></div>
      <div><dt>滑点与冲击</dt><dd>{slippageText(model)}</dd></div>
      <div><dt>参与率与截断</dt><dd>{participationText(model)}</dd></div>
      {unfilled && <div><dt>未成交</dt><dd>{unfilled}</dd></div>}
      {model.feeTiming && <div><dt>费用</dt><dd>{model.feeTiming}</dd></div>}
      {model.fundingTiming && <div><dt>资金费</dt><dd>{model.fundingTiming}</dd></div>}
      {model.liquidationBasis && <div><dt>强平基准</dt><dd>{model.liquidationBasis}</dd></div>}
      {model.thinBarPolicy && <div><dt>休市空K线</dt><dd>{THIN_BAR_LABEL[model.thinBarPolicy] ?? model.thinBarPolicy}</dd></div>}
    </dl>
    {model.simplifications.length > 0 && <div className="study-gaps">
      <p>本模型未建模：</p>
      <ul>{model.simplifications.map((item) => <li key={item}>{item}</li>)}</ul>
    </div>}
  </section>;
}

/** The engine's partial-fill policies, in the operator's language. */
const PARTIAL_FILL_LABEL: Record<string, string> = {
  ignore: "不建模部分成交，超额部分按整笔处理",
  allow: "允许部分成交，剩余部分留给后续K线",
  reject: "超过参与率上限的部分直接拒单",
};

const THIN_BAR_LABEL: Record<string, string> = {
  skip: "不建仓（默认，避免无深度成交）",
  allow: "按价格成交并标注警告",
};

function latencyText(model: ExecutionModel): string {
  if (model.latencyBars === null) return "—";
  return model.latencyBars === 0 ? "0 根（信号确认后的下一根K线成交）" : `${model.latencyBars} 根K线`;
}

function slippageText(model: ExecutionModel): string {
  const parts: string[] = [];
  if (model.slippageModel) parts.push(model.slippageModel);
  if (model.slippageBps !== null) parts.push(`${model.slippageBps} bps`);
  if (model.impactCoefficient !== null) parts.push(`冲击系数 ${model.impactCoefficient}`);
  return parts.length > 0 ? parts.join(" · ") : "—";
}

function participationText(model: ExecutionModel): string {
  const parts: string[] = [];
  if (model.maxParticipation !== null) parts.push(`单根K线最多吃掉成交量的 ${(model.maxParticipation * 100).toFixed(0)}%`);
  if (model.partialFill) parts.push(PARTIAL_FILL_LABEL[model.partialFill] ?? model.partialFill);
  return parts.length > 0 ? parts.join(" · ") : "—";
}

/** Null when nothing was left unfilled: a real zero is not worth a row. */
function unfilledText(model: ExecutionModel): string | null {
  const orders = model.unfilledOrders ?? 0;
  const notional = model.unfilledNotional ?? 0;
  if (orders <= 0 && notional <= 0) return null;
  return `${countText(model.unfilledOrders)} 笔 · ${model.unfilledNotional === null ? "—" : `${formatPrice(model.unfilledNotional)} USDT`}`;
}

/** Gate checks use their own status vocabulary; map it onto the verdict tones. */
function verdictTone(status: string): "pass" | "warn" | "fail" {
  const value = status.trim().toLowerCase();
  if (value === "pass" || value === "ok" || value === "ready" || value === "healthy") return "pass";
  if (value === "fail" || value === "failed" || value === "blocked" || value === "error" || value === "critical" || value === "not_ready") return "fail";
  // An unrecognised status is not evidence of failure.
  return "warn";
}
