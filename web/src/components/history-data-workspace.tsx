import { useCallback, useEffect, useMemo, useState } from "react";
import { IconAlertTriangle, IconPlayerPause, IconPlayerPlay, IconRefresh, IconRestore, IconX } from "@tabler/icons-react";
import { buildBackfillMatrix, controlBackfillQueue, controlBackfillTask, fetchBackfillBoard, fetchBacktestableRanges, RANGE_STATUS, type BackfillBoard, type BackfillTask, type BacktestableRanges } from "../services/history-data";

const STATUS: Record<string, string> = { pending: "等待", running: "回填中", paused: "已暂停", done: "完成", failed: "失败", unsupported: "不支持", cancelled: "已取消" };

export function HistoryDataWorkspace() {
  const [board, setBoard] = useState<BackfillBoard | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState("all");
  const [status, setStatus] = useState("all");
  const [ranges, setRanges] = useState<BacktestableRanges | null>(null);
  const loadRanges = useCallback(async () => {
    try { setRanges(await fetchBacktestableRanges()); }
    catch { /* the board still works without the range table */ }
  }, []);
  const load = useCallback(async (quiet = false) => {
    if (!quiet) setBusy(true);
    try { setBoard(await fetchBackfillBoard()); setError(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "读取历史任务失败"); }
    finally { if (!quiet) setBusy(false); }
  }, []);
  useEffect(() => { void load(); const timer = window.setInterval(() => void load(true), 5_000); return () => window.clearInterval(timer); }, [load]);
  // The range table reads bar history, so it refreshes on a slower cadence than
  // the task board: a five-second poll should not rescan every series.
  useEffect(() => { void loadRanges(); const timer = window.setInterval(() => void loadRanges(), 30_000); return () => window.clearInterval(timer); }, [loadRanges]);
  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try { await action(); await load(true); setError(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败"); }
    finally { setBusy(false); }
  };
  const tasks = useMemo(() => (board?.tasks ?? []).filter((task) => (kind === "all" || task.dataKind === kind) && (status === "all" || task.status === status)), [board, kind, status]);
  const finished = board?.summary.finished ?? 0;
  const total = board?.summary.total ?? 0;
  return <section className="history-workspace">
    <header className="history-heading"><div><h2>历史数据</h2><p>交易所数据分族回填、版本覆盖与失败恢复</p></div><div className="history-actions">
      <button type="button" onClick={() => void run(buildBackfillMatrix)} disabled={busy}>建立完整矩阵</button>
      <button type="button" onClick={() => void run(() => controlBackfillQueue(board?.paused ? "resume" : "pause"))} disabled={busy || !board}>{board?.paused ? <IconPlayerPlay size={14} /> : <IconPlayerPause size={14} />}{board?.paused ? "继续全部" : "暂停全部"}</button>
      <button type="button" onClick={() => void load()} disabled={busy}><IconRefresh className={busy ? "spin" : ""} size={14} />刷新</button>
    </div></header>
    {error && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{error}</p>}
    <div className="history-overview"><div><span>任务进度</span><strong>{finished}/{total || "—"}</strong><i><b style={{ width: total ? `${Math.round(finished / total * 100)}%` : "0%" }} /></i></div><div><span>后台执行器</span><strong className={board?.workerRunning ? "positive" : "negative"}>{board?.workerRunning ? "运行中" : "未启动"}</strong><small>{board?.concurrency ?? 0} 并发 · {board?.pagesPerMinute ?? 0} 页/分钟</small></div><div><span>待处理</span><strong>{board?.summary.pending ?? 0}</strong><small>{board?.workerError || (board?.paused ? "全局暂停" : "自动续传")}</small></div></div>
    <div className="history-filters"><label>数据族<select value={kind} onChange={(event) => setKind(event.target.value)}><option value="all">全部</option><option value="trade_candle">成交 K 线</option><option value="mark_candle">标记价格</option><option value="funding">资金费率</option><option value="open_interest">持仓量</option><option value="risk_limit">风险档位</option></select></label><label>状态<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">全部</option>{Object.entries(STATUS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><span>显示 {tasks.length} 项</span></div>
    {!board ? <div className="monitoring-loading">正在读取任务队列…</div> : total === 0 ? <div className="history-empty"><strong>尚未建立历史数据矩阵</strong><span>建立后会生成 68 个成交 K 线、68 个标记价格和各合约衍生数据任务。</span></div> : <div className="history-table-wrap"><table className="history-table"><thead><tr><th>合约</th><th>数据</th><th>状态</th><th>进度</th><th>可用记录</th><th>运行</th><th>说明</th><th /></tr></thead><tbody>{tasks.map((task) => <TaskRow key={task.id} task={task} run={run} />)}</tbody></table></div>}
    {ranges && <RangeTable ranges={ranges} />}
  </section>;
}

/**
 * What can be backtested right now, per contract and family. "Gap-free" and
 * "reached the listing date" are separate columns on purpose: a study can run in
 * a continuous window that does not go back to the contract's first day.
 */
function RangeTable({ ranges }: { ranges: BacktestableRanges }) {
  const [only, setOnly] = useState("all");
  const rows = ranges.rows.filter((row) => only === "all" || row.status === only);
  const fmt = (ts: number | null) => (ts ? new Date(ts).toLocaleString("zh-CN", { hour12: false }) : "—");
  return <section className="history-ranges">
    <div className="history-filters">
      <strong>可用于回测区间</strong>
      <span>{ranges.summary.series} 个数据族 · 连续 {ranges.summary.gapFree} · 已到上线 {ranges.summary.reachedListing} · 不支持 {ranges.summary.unsupported} · 无数据 {ranges.summary.empty}</span>
      <label>状态<select value={only} onChange={(event) => setOnly(event.target.value)}>
        <option value="all">全部</option>
        {Object.entries(RANGE_STATUS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <span>显示 {rows.length} 项</span>
    </div>
    <div className="history-table-wrap"><table className="history-table">
      <thead><tr><th>合约</th><th>数据</th><th>周期</th><th>可用于回测</th><th>唯一记录</th><th>已到上线</th><th>区间无缺口</th><th>说明</th></tr></thead>
      <tbody>{rows.map((row) => <tr key={`${row.symbol}-${row.dataKind}-${row.interval}`}>
        <td><strong>{row.symbol}</strong></td>
        <td>{row.dataKind}</td>
        <td>{row.interval || "全周期"}</td>
        <td>
          <em className={`history-status range-${row.status}`}>{RANGE_STATUS[row.status] ?? row.status}</em>
          <small>{row.usableFromTs ? `${fmt(row.usableFromTs)} → ${fmt(row.usableToTs)}` : "—"}</small>
        </td>
        <td>{row.barsAvailable.toLocaleString("zh-CN")}{row.usableBars !== row.barsAvailable ? `（可用 ${row.usableBars.toLocaleString("zh-CN")}）` : ""}</td>
        <td>{row.reachedListing === null ? "未知" : row.reachedListing ? "是" : "否"}</td>
        <td>{row.gapFree === null ? "不适用" : row.gapFree ? "是" : "否"}</td>
        <td title={row.reason}>{row.reason || (row.hasGaps ? "历史中存在缺口，可用区间为最新连续段" : "—")}</td>
      </tr>)}</tbody>
    </table></div>
  </section>;
}

function TaskRow({ task, run }: { task: BackfillTask; run: (action: () => Promise<unknown>) => Promise<void> }) {
  const detail = task.failureLabel || task.failure || task.reason || (task.estimateExhausted ? "页数估算已用尽，仍会继续" : "—");
  return <tr><td><strong>{task.symbol}</strong><span>{task.interval || "全周期"}</span></td><td>{task.kindLabel}</td><td><em className={`history-status status-${task.status}`}>{STATUS[task.status] ?? task.status}</em></td><td><span>{task.progressPct.toFixed(0)}%</span><small>{task.pages} 页 · 余 {task.pagesRemaining}</small></td><td>{task.rowsAvailable.toLocaleString("zh-CN")}</td><td>{task.attempts} 次{task.failureAttempts ? ` · 连败 ${task.failureAttempts}` : ""}</td><td title={detail}>{detail}</td><td><div className="task-actions">
    {(task.status === "pending" || task.status === "running") && <button type="button" title="暂停" onClick={() => void run(() => controlBackfillTask(task.id, "pause"))}><IconPlayerPause size={13} /></button>}
    {task.status === "paused" && <button type="button" title="继续" onClick={() => void run(() => controlBackfillTask(task.id, "resume"))}><IconPlayerPlay size={13} /></button>}
    {(task.status === "failed" || task.status === "cancelled") && <button type="button" title="重试" onClick={() => void run(() => controlBackfillTask(task.id, "retry"))}><IconRestore size={13} /></button>}
    {!(["done", "unsupported", "cancelled"] as string[]).includes(task.status) && <button type="button" title="取消" onClick={() => void run(() => controlBackfillTask(task.id, "cancel"))}><IconX size={13} /></button>}
  </div></td></tr>;
}
