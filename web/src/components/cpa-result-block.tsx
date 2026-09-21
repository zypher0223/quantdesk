import { IconAlertTriangle, IconInfoCircle } from "@tabler/icons-react";

import {
  cpaAttachment,
  cpaResultView,
  type CpaResultView,
  type PhaseTradeRow,
} from "../services/cpa";
import { artifactUrl } from "../services/runs";

/**
 * The CPA block of a finished run.
 *
 * Rendered only when the payload actually carries a phase series, and always as a
 * whole: the distribution, the per-stage attribution, the versions and the simplified
 * -position notice come from one `cpaResultView`, so a page cannot show the numbers
 * while dropping the caveat that says what they are not.
 */

function money(value: number): string {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function percent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function TradeRows({ rows }: { rows: PhaseTradeRow[] }) {
  if (!rows.length) return <p className="backtest-hint">该结果没有成交记录，无法按入场阶段拆分。</p>;
  return (
    <div className="backtest-table-wrap">
      <table>
        <thead>
          <tr>
            <th>入场阶段</th><th>交易</th><th>净盈亏</th><th>平均收益</th><th>最差一笔</th><th>强平</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.phase}>
              <td title={row.phase}>{row.label}</td>
              <td>{row.trades}</td>
              <td className={row.netPnl >= 0 ? "positive" : "negative"}>{money(row.netPnl)}</td>
              <td className={(row.avgReturnPct ?? 0) >= 0 ? "positive" : "negative"}>{percent(row.avgReturnPct)}</td>
              <td className="negative">{percent(row.worstReturnPct)}</td>
              <td className={row.liquidations ? "negative" : undefined}>{row.liquidations || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function CpaResultBlock({
  view,
  runId,
  artifacts,
}: {
  view: CpaResultView;
  runId: number;
  artifacts: string[];
}) {
  const attachment = cpaAttachment(artifacts) ?? view.attachment;
  const confirmedTotal = view.distribution.reduce((sum, row) => sum + row.confirmed, 0);
  const candidateTotal = view.distribution.reduce((sum, row) => sum + row.candidate, 0);
  const observationTotal = view.distribution.reduce((sum, row) => sum + row.observation, 0);

  return (
    <section className="cpa-result" aria-label="CPA 阶段结果">
      <div className="run-section-heading">
        <h3>CPA 阶段</h3>
        <span>
          {view.phases.bars} 根 · 规则版本 {view.parameterVersion || "—"}
          {view.higherIntervals.length ? ` · 高周期 ${view.higherIntervals.join("/")}` : ""}
        </span>
      </div>

      <p className="cpa-notice" role="note">
        <IconInfoCircle size={13} /> {view.notice}
      </p>

      {view.phases.insufficient && (
        <p className="cpa-warn" role="status">
          <IconAlertTriangle size={13} /> 样本不足：{view.phases.insufficientReason || "引擎未说明原因"}
          （本次运行没有可用的阶段结论）
        </p>
      )}

      <div className="cpa-counts">
        <span>已确认 {confirmedTotal}</span>
        <span>候选 {candidateTotal}（不产生交易）</span>
        <span>观察 {observationTotal}（不产生交易）</span>
      </div>

      {view.distribution.length > 0 && (
        <div className="backtest-table-wrap">
          <table>
            <thead>
              <tr><th>阶段</th><th>已确认</th><th>候选</th><th>观察</th><th>合计</th></tr>
            </thead>
            <tbody>
              {view.distribution.map((row) => (
                <tr key={row.phase}>
                  <td title={row.phase}>{row.label}</td>
                  <td>{row.confirmed || "—"}</td>
                  <td>{row.candidate || "—"}</td>
                  <td>{row.observation || "—"}</td>
                  <td>{row.total}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4 className="cpa-subheading">按入场阶段拆分的成交</h4>
      <TradeRows rows={view.tradeRows} />

      <h4 className="cpa-subheading">高周期过滤前后对比</h4>
      {view.comparison.available ? (
        <div className="backtest-table-wrap">
          <table>
            <thead><tr><th>指标</th><th>开启高周期过滤</th><th>关闭</th></tr></thead>
            <tbody>
              {view.comparison.rows.map((row) => (
                <tr key={row.label}>
                  <td>{row.label}</td>
                  <td>{row.withFilter === null ? "—" : row.withFilter}</td>
                  <td>{row.withoutFilter === null ? "—" : row.withoutFilter}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="backtest-hint">{view.comparison.reason}</p>
      )}

      <dl className="cpa-versions">
        <div><dt>规则版本</dt><dd>{view.parameterVersion || "—"}</dd></div>
        <div><dt>数据版本</dt><dd>{view.dataVersion ? view.dataVersion.slice(0, 16) : "—"}</dd></div>
        <div><dt>阶段样本</dt><dd>{view.phases.bars} 根</dd></div>
        <div>
          <dt>阶段附件</dt>
          <dd>
            {attachment
              ? <a href={artifactUrl(runId, attachment)} target="_blank" rel="noreferrer">{attachment}</a>
              : "未产出"}
          </dd>
        </div>
      </dl>

      {view.warnings.length > 0 && (
        <ul className="backtest-warnings">
          {view.warnings.map((warning, index) => (
            <li key={index}><IconAlertTriangle size={13} />{warning}</li>
          ))}
        </ul>
      )}

      {Object.keys(view.parameters).length > 0 && (
        <details className="cpa-run-parameters">
          <summary>本次运行使用的参数</summary>
          <pre>{JSON.stringify(view.parameters, null, 2)}</pre>
        </details>
      )}
    </section>
  );
}

/** The block, or nothing at all when the run is not a CPA run. */
export function CpaResultSection({
  result,
  runId,
  artifacts,
}: {
  result: Record<string, unknown> | null | undefined;
  runId: number;
  artifacts: string[];
}) {
  const view = cpaResultView(result);
  if (!view) return null;
  return <CpaResultBlock view={view} runId={runId} artifacts={artifacts} />;
}
