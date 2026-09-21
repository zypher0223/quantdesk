import { IconX } from "@tabler/icons-react";
import { comparisonRows, COMPARISON_LIMIT } from "../services/results-batch";
import type { RunSummary } from "../services/runs";

/**
 * The selected runs side by side.
 *
 * A comparison is only readable because every cell means the same thing in every
 * row: the same scope, the same unit, the same "—" when the engine reported
 * nothing. A factor run has no P&L at all, so its coverage takes the metric
 * columns' place rather than six dashes pretending to be a result.
 */
export function RunComparison({ runs, onClose }: { runs: RunSummary[]; onClose: () => void }) {
  const shown = runs.slice(0, COMPARISON_LIMIT);
  const rows = comparisonRows(shown);
  return <section className="run-comparison" aria-label="回测结果对比">
    <div className="run-section-heading">
      <h3>结果对比</h3>
      <span>
        {rows.length} 项
        {runs.length > shown.length ? ` · 仅显示前 ${COMPARISON_LIMIT} 项` : ""}
        {" · "}指标口径随研究类型不同
      </span>
      <button type="button" className="run-comparison-close" title="关闭对比" onClick={onClose}><IconX size={13} />关闭</button>
    </div>
    <div className="history-table-wrap">
      <table className="history-table run-comparison-table">
        <thead>
          <tr>
            <th>ID</th><th>类型</th><th>标的 / 周期</th><th>策略</th><th>状态</th>
            <th>净收益</th><th>最大回撤</th><th>交易数</th><th>夏普</th><th>覆盖</th>
            <th>指标口径</th><th>耗时</th><th>完成时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <td><strong>{row.idText}</strong></td>
              <td>{row.kind}</td>
              <td>{row.market}</td>
              <td>{row.strategy}</td>
              <td>{row.status}</td>
              <td>{row.netReturn}</td>
              <td>{row.maxDrawdown}</td>
              <td>{row.trades}</td>
              <td>{row.sharpe}</td>
              <td>{row.coverage}</td>
              <td title={row.scope}>{row.scope}</td>
              <td>{row.duration}</td>
              <td>{row.completed}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
    <p className="backtest-hint">
      对比只读取列表已报告的数字，缺失项显示「—」；导出 CSV 使用同一组列。
    </p>
  </section>;
}
