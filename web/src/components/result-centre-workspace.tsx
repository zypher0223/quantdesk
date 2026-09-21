import { useCallback, useEffect, useMemo, useState } from "react";
import { IconAlertTriangle, IconColumns, IconRefresh, IconRestore, IconTableExport, IconTrash, IconX } from "@tabler/icons-react";
import { RunComparison } from "./run-comparison";
import { RunDetailPanel } from "./run-detail-panel";
import { RunMaintenancePanel } from "./run-maintenance";
import { ValidationSubmitForm } from "./validation-submit-form";
import { validateRun, validationNotes } from "../services/factors";
import { countText, drawdownText, percentText, progressOf, toneOf } from "../services/run-figures";
import {
  browserFilterStorage,
  compareEnabled,
  COMPARISON_LIMIT,
  csvFileName,
  downloadCsv,
  readStoredFilter,
  resultsCsv,
  storeResultFilter,
  type ResultCentreFilter,
} from "../services/results-batch";
import {
  cancelRun,
  deleteRun,
  fetchRun,
  fetchRuns,
  filterRuns,
  formatDuration,
  formatRunTime,
  isActiveStatus,
  isFactorRun,
  queueStateText,
  retryRun,
  runKindLabel,
  runStatusLabel,
  summariseRuns,
  RUN_KIND_LABEL,
  RUN_STATUS_LABEL,
  type RunBoard,
  type RunDetail,
  type RunKind,
  type RunStatus,
  type RunSummary,
} from "../services/runs";

/** An active queue is worth watching closely; a settled one is not worth polling. */
const POLL_MS = 2_000;

export function ResultCentreWorkspace() {
  const [board, setBoard] = useState<RunBoard | null>(null);
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState(false);
  // The three filter choices are the only thing this page remembers between
  // visits; the selection is deliberately not persisted, since a batch export
  // must always be a decision made in this sitting.
  const [filter, setFilter] = useState<ResultCentreFilter>(() => readStoredFilter(browserFilterStorage()));
  const [picked, setPicked] = useState<number[]>([]);
  const [compareOpen, setCompareOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [validateBusy, setValidateBusy] = useState(false);
  // What the validator said about its own reading of the selected run. It is not
  // part of the run detail, so it lives beside it and is cleared with it.
  const [analysisNotes, setAnalysisNotes] = useState<string[]>([]);
  const [studyOpen, setStudyOpen] = useState(false);

  // Restored on mount and written on every change; nothing else is stored.
  useEffect(() => { storeResultFilter(browserFilterStorage(), filter); }, [filter]);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setBusy(true);
    try {
      setBoard(await fetchRuns({ limit: 200 }));
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取回测任务失败");
    } finally {
      if (!quiet) setBusy(false);
    }
  }, []);

  const loadDetail = useCallback(async (id: number) => {
    try {
      setDetail(await fetchRun(id));
      setDetailError("");
    } catch (reason) {
      setDetailError(reason instanceof Error ? reason.message : "读取任务详情失败");
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    // Switching rows must not leave the previous run's numbers on screen.
    setDetail(null);
    setDetailError("");
    setAnalysisNotes([]);
    if (selectedId === null) return;
    void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  const tally = useMemo(() => summariseRuns(board?.runs ?? []), [board]);
  const queue = board?.queue ?? null;
  const rows = useMemo(() => filterRuns(board?.runs ?? [], filter), [board, filter]);

  // The selection survives polling and filtering; the comparison reads the runs
  // that are still on the board, so a run deleted elsewhere simply drops out.
  const pickedRuns = useMemo(() => (board?.runs ?? []).filter((run) => picked.includes(run.id)), [board, picked]);
  const visibleIds = useMemo(() => rows.map((run) => run.id), [rows]);
  const allVisiblePicked = visibleIds.length > 0 && visibleIds.every((id) => picked.includes(id));
  const canCompare = compareEnabled(pickedRuns.length);

  // A selection that shrank below two, or grew past the cap, quietly closes the
  // comparison instead of leaving a stale table on screen.
  useEffect(() => { if (!canCompare) setCompareOpen(false); }, [canCompare]);

  // A run deleted here or in another tab must not stay counted as selected.
  useEffect(() => {
    if (!board) return;
    const live = new Set(board.runs.map((run) => run.id));
    setPicked((current) => (current.some((id) => !live.has(id)) ? current.filter((id) => live.has(id)) : current));
  }, [board]);

  const togglePick = (id: number) => {
    setPicked((current) => (current.includes(id) ? current.filter((value) => value !== id) : [...current, id]));
  };

  const toggleAllVisible = () => {
    setPicked((current) => allVisiblePicked
      ? current.filter((id) => !visibleIds.includes(id))
      : [...new Set([...current, ...visibleIds])]);
  };

  const exportCsv = () => {
    if (pickedRuns.length === 0) return;
    downloadCsv(csvFileName(Date.now()), resultsCsv(pickedRuns));
  };

  // One timer for the whole surface: it disappears the moment nothing is active,
  // and it is cleared on unmount so a closed workspace stops hitting the engine.
  useEffect(() => {
    if (!tally.hasActive) return;
    const timer = window.setInterval(() => {
      void load(true);
      if (selectedId !== null) void loadDetail(selectedId);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [tally.hasActive, selectedId, load, loadDetail]);

  const act = async (action: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await action();
      setActionError("");
      await load(true);
      if (selectedId !== null) await loadDetail(selectedId);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const removeRun = async (run: RunSummary) => {
    if (!window.confirm(`删除任务 #${run.id}？该任务的提交参数与结果文件会一并删除。`)) return;
    const wasSelected = selectedId === run.id;
    setBusy(true);
    try {
      await deleteRun(run.id);
      if (wasSelected) setSelectedId(null);
      setActionError("");
      await load(true);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : "删除失败");
    } finally {
      setBusy(false);
    }
  };

  /**
   * Ask the validator to read a finished run, then reload the detail so its new
   * verdict rows are on screen. A 409 ("no validator plugin", "no result yet") is
   * the engine's own sentence, not a crash: it goes to the banner above the panel.
   */
  const validate = async (id: number) => {
    setValidateBusy(true);
    try {
      const report = await validateRun(id);
      setAnalysisNotes(validationNotes(report.analysis));
      setDetailError("");
      await loadDetail(id);
    } catch (reason) {
      setAnalysisNotes([]);
      setDetailError(reason instanceof Error ? reason.message : "统计验证失败");
    } finally {
      setValidateBusy(false);
    }
  };

  return <section className="history-workspace results-workspace">
    <header className="history-heading">
      <div>
        <h2>回测结果中心</h2>
        <p>后台排队执行的历史研究任务、可复现的结果文件与验证结论</p>
      </div>
      <div className="history-actions">
        <span className="run-queue" title={queue?.workerError || "后台执行器状态"}>
          <i className={queue?.workerRunning ? "queue-dot queue-live" : "queue-dot"} />
          {queueStateText(queue)}
        </span>
        <button type="button" onClick={() => void load()} disabled={busy}><IconRefresh className={busy ? "spin" : ""} size={14} />刷新</button>
      </div>
    </header>

    {error && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{error}</p>}
    {actionError && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{actionError}</p>}

    <div className="history-overview runs-overview">
      <div><span>排队</span><strong>{tally.queued}</strong><small>等待执行器领取</small></div>
      <div>
        <span>运行中</span><strong>{tally.running}</strong>
        <small title={queue?.activeLabel ?? undefined}>{queue?.activeRunId != null ? `当前 #${queue.activeRunId}${queue.activeProgress != null ? ` ${Math.round(queue.activeProgress)}%` : ""}` : "当前空闲"}</small>
      </div>
      <div><span>已完成</span><strong>{tally.done}</strong><small>已产出结果</small></div>
      <div><span>失败</span><strong>{tally.failed}</strong><small>{tally.cancelled ? `另有 ${tally.cancelled} 个已取消` : "无取消任务"}</small></div>
    </div>

    <div className="history-filters">
      <label>类型
        <select value={filter.kind} onChange={(event) => setFilter((current) => ({ ...current, kind: event.target.value as RunKind | "all" }))}>
          <option value="all">全部</option>
          {Object.entries(RUN_KIND_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label>状态
        <select value={filter.status} onChange={(event) => setFilter((current) => ({ ...current, status: event.target.value as RunStatus | "all" }))}>
          <option value="all">全部</option>
          {Object.entries(RUN_STATUS_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label className="history-search">搜索
        <input
          type="search"
          value={filter.query}
          placeholder="ID / 标的 / 标签 / 策略"
          title="匹配任务编号、标的、标签与策略"
          onChange={(event) => setFilter((current) => ({ ...current, query: event.target.value }))}
        />
      </label>
      <span>显示 {rows.length} / {tally.total} 项{tally.hasActive ? " · 每 2 秒自动刷新" : ""}</span>
    </div>

    {picked.length > 0 && <div className="run-selection-bar">
      <strong aria-live="polite">已选 {picked.length} 项</strong>
      <button
        type="button"
        title={canCompare ? "并排对比所选任务" : `对比需要选择 2 到 ${COMPARISON_LIMIT} 项`}
        disabled={!canCompare}
        onClick={() => setCompareOpen((open) => !open)}
      >
        <IconColumns size={14} />{compareOpen ? "收起对比" : "对比"}
      </button>
      <button type="button" title="按同一组列导出所选任务" onClick={exportCsv}>
        <IconTableExport size={14} />导出 CSV
      </button>
      <button type="button" onClick={() => { setPicked([]); setCompareOpen(false); }}>清除选择</button>
      {picked.length > COMPARISON_LIMIT
        ? <small>对比最多 {COMPARISON_LIMIT} 项，导出不受限制</small>
        : picked.length === 1
          ? <small>再选一项即可对比</small>
          : null}
    </div>}


    {!board
      ? <div className="monitoring-loading">正在读取回测任务…</div>
      : tally.total === 0
        ? <div className="history-empty"><strong>还没有后台任务</strong><span>在「策略回测」中用「后台运行」提交，任务会在这里排队、执行并保留完整结果。</span></div>
        : <div className="history-table-wrap">
          <table className="history-table runs-table">
            <thead>
              <tr>
                <th className="runs-pick-col">
                  <input
                    type="checkbox"
                    aria-label="全选当前筛选结果"
                    title={allVisiblePicked ? "取消全选当前筛选结果" : "全选当前筛选结果"}
                    checked={allVisiblePicked}
                    onChange={toggleAllVisible}
                  />
                </th>
                <th>ID</th><th>类型</th><th>标的</th><th>周期</th><th>策略</th><th>状态</th><th>进度</th><th>头部指标</th><th>提交时间</th><th>耗时</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((run) => (
                <RunRow
                  key={run.id}
                  run={run}
                  picked={picked.includes(run.id)}
                  selected={run.id === selectedId}
                  busy={busy}
                  onTogglePick={() => togglePick(run.id)}
                  onSelect={() => setSelectedId(run.id === selectedId ? null : run.id)}
                  onCancel={() => void act(() => cancelRun(run.id))}
                  onRetry={() => void act(() => retryRun(run.id))}
                  onDelete={() => void removeRun(run)}
                />
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <p className="history-empty-inline">当前筛选条件下没有任务。</p>}
        </div>}

    {compareOpen && canCompare && <RunComparison runs={pickedRuns} onClose={() => setCompareOpen(false)} />}


    {selectedId !== null && (
      <section className="run-detail" aria-live="polite">
        {detailError && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{detailError}</p>}
        {detail && detail.id === selectedId
          ? <RunDetailPanel
            run={detail}
            validateBusy={validateBusy}
            analysisNotes={analysisNotes}
            onValidate={() => void validate(detail.id)}
          />
          : !detailError
            ? <div className="monitoring-loading">正在读取任务 #{selectedId} 详情…</div>
            : null}
      </section>
    )}

    {/* 重活提交：参数网格在训练段搜索、在滚动窗口中验证，交给后台执行器。 */}
    <details className="run-factor-library run-heavy-study" onToggle={(event) => setStudyOpen(event.currentTarget.open)}>
      <summary>
        <span className="run-section-heading"><h3>后台运行：参数搜索与滚动验证</h3><span>提交后由执行器排队，可离开本页</span></span>
      </summary>
      {studyOpen && <ValidationSubmitForm onSubmitted={() => void load(true)} />}
    </details>

    {/* 记录清理：只查看计划，确认后才真的删除；刷新列表由回调完成。 */}
    <RunMaintenancePanel onPruned={() => void load(true)} />
  </section>;
}

/* ------------------------------------------------------------------- table */

function RunRow({ run, picked, selected, busy, onTogglePick, onSelect, onCancel, onRetry, onDelete }: {
  run: RunSummary;
  picked: boolean;
  selected: boolean;
  busy: boolean;
  onTogglePick: () => void;
  onSelect: () => void;
  onCancel: () => void;
  onRetry: () => void;
  onDelete: () => void;
}) {
  const active = isActiveStatus(run.status);
  const canRetry = run.status === "failed" || run.status === "cancelled";
  const headline = run.headline ?? null;
  const symbols = run.symbols ?? [];
  const symbol = run.displaySymbol ?? run.symbol ?? (symbols.length ? symbols.join(" · ") : "—");
  const progress = progressOf(run.progress);
  return <tr
    className={selected ? "run-row run-row-selected" : "run-row"}
    tabIndex={0}
    title="点击查看任务详情"
    onClick={onSelect}
    onKeyDown={(event) => {
      // The row answers Enter and Space; a checkbox inside it answers its own.
      if (event.target !== event.currentTarget) return;
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(); }
    }}
  >
    {/* The checkbox must never open the detail: its own click stops here. */}
    <td className="runs-pick-col" onClick={(event) => event.stopPropagation()}>
      <input
        type="checkbox"
        checked={picked}
        aria-label={`选择任务 #${run.id}`}
        title={picked ? `取消选择 #${run.id}` : `选择 #${run.id} 用于对比或导出`}
        onChange={onTogglePick}
      />
    </td>
    <td><strong>#{run.id}</strong><span className="run-label" title={run.label}>{run.label || "—"}</span></td>
    <td>{runKindLabel(run.kind)}</td>
    <td><strong>{symbol}</strong>{symbols.length > 1 && <span className="run-label">{symbols.join(" · ")}</span>}</td>
    <td>{run.interval || "—"}</td>
    <td>
      {run.strategyId
        ? <>
            <span title={`${run.strategyId} v${run.strategyVersion}`}>{run.strategyId}</span>
            <small>v{run.strategyVersion || "—"}</small>
            {/* The list only has the summary, so this marks *which* runs carry a CPA
                phase block; the phases themselves are in the run's detail. */}
            {run.strategyId === "cpa_cycle" && (
              <span className="cpa-chip" title="该运行附带 CPA 阶段序列（在详情里展开）">CPA 阶段</span>
            )}
          </>
        : <span title="因子计算没有策略">—</span>}
    </td>
    <td>
      <em className={`history-status status-${run.status}`}>{runStatusLabel(run.status)}</em>
      {run.attempts > 1 && <small>第 {run.attempts} 次</small>}
    </td>
    <td>
      <div className="run-progress">
        <span>{progress.toFixed(0)}%</span>
        <i><b style={{ width: `${progress}%` }} /></i>
        <small title={run.stage}>{run.progressLabel || run.stage || "—"}</small>
      </div>
    </td>
    <td>
      {/* A factor run has no P&L: its coverage is what the headline means. */}
      {isFactorRun(run)
        ? <div className="run-headline run-headline-factor">
          <span>因子 {countText(headline?.factors)} 个</span>
          <span>有值 {countText(headline?.coveredFactors)}</span>
          <span>中位覆盖 {countText(headline?.medianCoverage)} 根</span>
          <span>窗口 {countText(headline?.bars)} 根</span>
        </div>
        : <div className="run-headline">
          <span className={toneOf(headline?.netReturnPct)}>净 {percentText(headline?.netReturnPct, true)}</span>
          <span className={toneOf(headline?.maxDrawdownPct) === "negative" ? "negative" : undefined}>回撤 {drawdownText(headline?.maxDrawdownPct)}</span>
          <span>交易 {countText(headline?.trades)}</span>
        </div>}
    </td>
    <td>{formatRunTime(run.queuedTs)}</td>
    <td>{formatDuration(run.durationMs)}</td>
    <td>
      <div className="task-actions" onClick={(event) => event.stopPropagation()}>
        {active && <button type="button" title="取消" disabled={busy} onClick={onCancel}><IconX size={13} /></button>}
        {canRetry && <button type="button" title="重试" disabled={busy} onClick={onRetry}><IconRestore size={13} /></button>}
        {!active && <button type="button" title="删除" disabled={busy} onClick={onDelete}><IconTrash size={13} /></button>}
      </div>
    </td>
  </tr>;
}
