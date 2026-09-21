import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { IconAlertTriangle, IconCalculator, IconRefresh } from "@tabler/icons-react";
import { useInstrumentSymbols } from "../hooks/use-instrument-symbols";
import { fetchPlugins, type PluginSandboxStatus } from "../services/plugins";
import { queuedNotice } from "../services/study-queue";
import { PluginSandboxChip } from "./sandbox-chip";
import {
  computeFactors,
  factorCoveragePct,
  factorFamilyLabel,
  fetchFactorCatalog,
  fetchFactorRun,
  fetchFactorRuns,
  summariseCoverage,
  type FactorCatalog,
  type FactorComputeResult,
  type FactorCoverageRow,
  type FactorRunDetail,
  type FactorRunSummary,
  providerLabel,
} from "../services/factors";
import { formatDuration, formatRunTime, runStatusLabel, sandboxLabel } from "../services/runs";

/** The intervals the engine reads; anything else is refused by /api/factors. */
const INTERVALS = ["15m", "1h", "4h", "1d"];
const BARS_MIN = 30;
const BARS_MAX = 20_000;
const DEFAULT_BARS = "2000";
const DEFAULT_SYMBOL = "BTCUSDT";
const RUN_LIMIT = 20;

/**
 * The factor library, embedded in the result centre.
 *
 * On demand only: the catalogue and the recent runs are read once when the
 * section is opened, and a computation is read back when the operator asks for
 * one. Nothing here polls, and nothing here invents a number — a coverage the
 * run did not report prints as "—" and leaves its bar empty.
 */
export function FactorPanel() {
  const [catalog, setCatalog] = useState<FactorCatalog | null>(null);
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  // The host's isolation state. It is context for reading a run, not a
  // dependency of the panel, so a failed read leaves it unknown and silent.
  const [sandbox, setSandbox] = useState<PluginSandboxStatus | null>(null);
  const [runs, setRuns] = useState<FactorRunSummary[] | null>(null);
  const [runError, setRunError] = useState("");
  const [result, setResult] = useState<FactorComputeResult | null>(null);
  const [computeBusy, setComputeBusy] = useState(false);
  const [computeError, setComputeError] = useState("");
  // A computation the engine moved to the queue: a transfer, not a result.
  const [queueNote, setQueueNote] = useState("");
  const symbols = useInstrumentSymbols();
  const [symbol, setSymbol] = useState(DEFAULT_SYMBOL);
  const [interval, setIntervalValue] = useState("1h");
  const [bars, setBars] = useState(DEFAULT_BARS);
  const [picked, setPicked] = useState<string[] | null>(null);
  const [openRunId, setOpenRunId] = useState<number | null>(null);
  const [openRun, setOpenRun] = useState<FactorRunDetail | null>(null);
  const [openError, setOpenError] = useState("");

  const loadCatalog = useCallback(async () => {
    setCatalogBusy(true);
    try {
      setCatalog(await fetchFactorCatalog());
      setCatalogError("");
    } catch (reason) {
      setCatalogError(reason instanceof Error ? reason.message : "读取因子目录失败");
    } finally {
      setCatalogBusy(false);
    }
  }, []);

  const loadRuns = useCallback(async () => {
    try {
      setRuns(await fetchFactorRuns({ limit: RUN_LIMIT }));
      setRunError("");
    } catch (reason) {
      setRunError(reason instanceof Error ? reason.message : "读取因子任务失败");
    }
  }, []);

  useEffect(() => { void loadCatalog(); void loadRuns(); }, [loadCatalog, loadRuns]);

  // Asked once when the panel mounts. The engine's `sandbox` block says which
  // isolation the factor plugin actually ran under; a panel that cannot read it
  // simply shows no chip rather than blocking the catalogue.
  useEffect(() => {
    let live = true;
    fetchPlugins()
      .then((plugins) => { if (live) setSandbox(plugins.sandbox ?? null); })
      .catch(() => { if (live) setSandbox(null); });
    return () => { live = false; };
  }, []);

  // The fixed universe when the local gateway answers, a free-text code otherwise.
  useEffect(() => {
    if (symbols.length === 0) return;
    setSymbol((current) => (symbols.includes(current) ? current : symbols[0]));
  }, [symbols]);

  const factors = catalog?.factors ?? [];
  const factorIds = useMemo(() => factors.map((factor) => factor.id), [factors]);
  const names = useMemo(() => new Map(factors.map((factor) => [factor.id, factor.name])), [factors]);
  // Null means "everything the catalogue lists"; an explicit empty selection is
  // treated the same way, which the label says out loud.
  const selectedIds = useMemo(() => {
    if (picked === null) return factorIds;
    const known = new Set(factorIds);
    return picked.filter((id) => known.has(id));
  }, [picked, factorIds]);

  const compute = async () => {
    const count = Number(bars);
    // A fresh attempt replaces the previous answer, queued or not.
    setQueueNote("");
    if (!symbol.trim()) {
      setComputeError("请先选择或填写合约代码。");
      return;
    }
    if (!Number.isFinite(count) || count < BARS_MIN || count > BARS_MAX) {
      setComputeError(`K 线根数需在 ${BARS_MIN} 到 ${BARS_MAX} 之间。`);
      return;
    }
    setComputeBusy(true);
    try {
      const outcome = await computeFactors({
        symbol: symbol.trim(),
        interval,
        bars: Math.floor(count),
        ...(selectedIds.length ? { factorIds: selectedIds } : {}),
      });
      setOpenRunId(null);
      setOpenRun(null);
      setOpenError("");
      if (outcome.queued) {
        // The window was too wide to answer inline: there is no coverage yet, and
        // the notice names the run to watch instead.
        setResult(null);
        setComputeError("");
        setQueueNote(`${queuedNotice(outcome)}${outcome.reason ? ` · ${outcome.reason}` : ""}`);
        await loadRuns();
        return;
      }
      setResult(outcome.study);
      setComputeError("");
      setQueueNote("");
      await loadRuns();
    } catch (reason) {
      setComputeError(reason instanceof Error ? reason.message : "因子计算失败");
    } finally {
      setComputeBusy(false);
    }
  };

  const toggleRun = async (id: number) => {
    if (openRunId === id) {
      setOpenRunId(null);
      setOpenRun(null);
      setOpenError("");
      return;
    }
    setOpenRunId(id);
    setOpenRun(null);
    setOpenError("");
    try {
      setOpenRun(await fetchFactorRun(id));
    } catch (reason) {
      setOpenError(reason instanceof Error ? reason.message : `读取因子任务 #${id} 失败`);
    }
  };

  const overall = result ? factorCoveragePct(result) : null;

  return <section className="factor-panel">
    <header className="history-heading">
      <div>
        <h3>因子库</h3>
        <p>插件提供的因子目录、按合约的覆盖率诊断与最近的计算记录</p>
      </div>
      <div className="history-actions">
        <PluginSandboxChip status={sandbox} />
        <span className={catalog?.available ? "factor-provider factor-on" : "factor-provider"} title="因子提供者与可用状态">
          {catalog ? providerLabel(catalog) : "提供者未知"}
        </span>
        <button type="button" onClick={() => void loadCatalog()} disabled={catalogBusy}>
          <IconRefresh className={catalogBusy ? "spin" : ""} size={14} />{catalogBusy ? "读取中…" : "刷新目录"}
        </button>
      </div>
    </header>

    {catalogError && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{catalogError}</p>}

    {catalog && !catalog.available && <>
      {(catalog.warnings.length ? catalog.warnings : ["因子插件未启用，目录不可用。"]).map((warning) => (
        <p className="history-error" role="status" key={warning}><IconAlertTriangle size={15} />{warning}</p>
      ))}
      {factors.length > 0 && <p className="backtest-hint">
        来自已存目录（插件未启用）：以下是上一次插件可用时记录的因子定义，可以查阅；重新计算需要启用插件。
      </p>}
    </>}
    {catalog?.available && catalog.warnings.length > 0 && <ul className="factor-notes">
      {catalog.warnings.map((warning) => <li key={warning}>{warning}</li>)}
    </ul>}

    <div className="run-toolbar">
      <label>合约
        {symbols.length > 0
          ? <select value={symbol} onChange={(event) => setSymbol(event.target.value)}>
            {symbols.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
          : <input value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder={DEFAULT_SYMBOL} />}
      </label>
      <label>周期
        <select value={interval} onChange={(event) => setIntervalValue(event.target.value)}>
          {INTERVALS.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <label>K 线根数
        <input type="number" min={BARS_MIN} max={BARS_MAX} step={10} value={bars} onChange={(event) => setBars(event.target.value)} />
      </label>
      <label className="run-picker">因子（未选择时按目录全部计算）
        <select
          multiple
          size={5}
          value={selectedIds}
          onChange={(event) => setPicked([...event.target.selectedOptions].map((option) => option.value))}
        >
          {factors.map((factor) => <option key={factor.id} value={factor.id}>{factor.id} · {factor.name || "—"}</option>)}
        </select>
      </label>
      <button type="button" onClick={() => void compute()} disabled={computeBusy || !symbol.trim()}>
        <IconCalculator size={14} />{computeBusy ? "计算中…" : "计算"}
      </button>
    </div>

    {computeError && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{computeError}</p>}
    {queueNote && <p className="study-submit-note" role="status">{queueNote}</p>}

    {result && <section className="factor-result">
      <div className="run-section-heading">
        <h3>本次计算覆盖</h3>
        <span>
          #{result.runId} · {result.symbol || "—"} {result.interval || "—"} · {formatCount(result.bars)} 根
          {" · "}整体覆盖 {percentText(overall)}
        </span>
      </div>
      <CoverageTable rows={summariseCoverage(result.coverage, result.bars)} names={names} />
      <p className="backtest-hint">
        数据版本 {result.snapshotHash || "—"} · {result.batches > 0 ? `分 ${result.batches} 批计算` : "批次数未报告"}
        {" · "}提供者 {result.provider ?? "—"}
      </p>
      {result.warnings.length > 0 && <ul className="factor-notes">{result.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
    </section>}

    <section className="factor-catalog">
      <div className="run-section-heading"><h3>因子目录</h3><span>{factors.length} 个定义</span></div>
      {factors.length === 0
        ? <p className="backtest-hint">{catalog ? "目录中没有因子定义。" : "正在读取因子目录…"}</p>
        : <div className="history-table-wrap">
          <table className="history-table factor-catalog-table">
            <thead>
              <tr><th>因子</th><th>名称</th><th>族</th><th>预热</th><th>所需字段</th><th>公式指纹</th></tr>
            </thead>
            <tbody>
              {factors.map((factor) => (
                <tr key={factor.id}>
                  <td>
                    <strong title={factor.description || factor.id}>{factor.id}</strong>
                    <span>{factor.mode || "—"}{factor.supportedTimeframes.length > 0 ? ` · ${factor.supportedTimeframes.join("/")}` : ""}</span>
                  </td>
                  <td>{factor.name || "—"}</td>
                  <td>{factorFamilyLabel(factor.family)}</td>
                  <td>{formatCount(factor.warmupBars)} 根</td>
                  <td title={factor.requiredFields.join("、")}>{factor.requiredFields.length > 0 ? factor.requiredFields.join("、") : "—"}</td>
                  <td title={factor.formulaHash}>{factor.formulaHash ? `${factor.formulaHash.slice(0, 12)}…` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>}
    </section>

    <section className="factor-runs">
      <div className="run-section-heading"><h3>最近计算</h3><span>{runs ? `${runs.length} 条记录` : "读取中"}</span></div>
      {runError && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{runError}</p>}
      {!runs
        ? !runError && <div className="monitoring-loading">正在读取因子任务…</div>
        : runs.length === 0
          ? <p className="backtest-hint">还没有因子计算记录。</p>
          : <div className="history-table-wrap">
            <table className="history-table factor-runs-table">
              <thead>
                <tr><th>任务</th><th>合约</th><th>周期</th><th>K 线</th><th>序列/请求</th><th>提交时间</th><th>耗时</th><th /></tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                const open = run.id === openRunId;
                return <Fragment key={run.id}>
                  <tr
                    className={open ? "factor-run-row factor-run-open" : "factor-run-row"}
                    tabIndex={0}
                    title="展开查看该次计算的覆盖率"
                    onClick={() => void toggleRun(run.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); void toggleRun(run.id); }
                    }}
                  >
                    <td><strong>#{run.id}</strong><span>{runStatusLabel(run.status)}</span></td>
                    <td>{run.symbol || "—"}</td>
                    <td>{run.interval || "—"}</td>
                    <td>{formatCount(run.bars)}</td>
                    <td>{formatCount(run.seriesCount)} / {formatCount(run.factorIds.length)}</td>
                    <td>{formatRunTime(run.createdTs)}</td>
                    <td>{formatDuration(run.durationMs)}</td>
                    <td>{open ? "▾" : "▸"}</td>
                  </tr>
                  {open && <tr className="factor-run-detail">
                    <td colSpan={8}>
                      {openError && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{openError}</p>}
                      {!openRun && !openError && <div className="monitoring-loading">正在读取任务 #{run.id} 的覆盖率…</div>}
                      {openRun && openRun.id === run.id && <>
                        <p className="backtest-hint">
                          整体覆盖 {percentText(factorCoveragePct(openRun))} · 数据版本 {openRun.snapshotHash || "—"}
                          {" · "}沙箱 {sandboxLabel(openRun.sandbox)}
                          {" · "}请求 {openRun.factorIds.length} 个因子，返回 {openRun.seriesCount} 条序列
                          {openRun.valuesIncluded ? "" : "（覆盖来自计数，序列值按需读取）"}
                        </p>
                        <CoverageTable rows={summariseCoverage(openRun.coverage, openRun.bars)} names={names} />
                        {openRun.error && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{openRun.error}</p>}
                        {openRun.warnings.length > 0 && <ul className="factor-notes">{openRun.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
                      </>}
                    </td>
                  </tr>}
                </Fragment>;
              })}
            </tbody>
          </table>
        </div>}
    </section>
  </section>;
}

/** One run's coverage: the bar and the percent always agree, and never exceed 100. */
function CoverageTable({ rows, names }: { rows: FactorCoverageRow[]; names: Map<string, string> }) {
  if (rows.length === 0) return <p className="backtest-hint">该次计算没有回报覆盖率。</p>;
  return <div className="history-table-wrap">
    <table className="history-table factor-coverage-table">
      <thead><tr><th>因子</th><th>名称</th><th>覆盖</th><th>覆盖度</th></tr></thead>
      <tbody>
        {rows.map((row) => <tr key={row.factorId}>
          <td><strong>{row.factorId}</strong></td>
          <td>{names.get(row.factorId) ?? "—"}</td>
          <td>
            <i className="factor-bar" title={row.pct === null ? "覆盖率未报告" : percentText(row.pct)}>
              <b style={{ width: `${row.pct ?? 0}%` }} />
            </i>
          </td>
          <td>
            {percentText(row.pct)}
            <small>{row.covered === null ? "有效值未报告" : `${formatCount(row.covered)} / ${row.bars > 0 ? formatCount(row.bars) : "—"} 根`}</small>
          </td>
        </tr>)}
      </tbody>
    </table>
  </div>;
}

/* ------------------------------------------------------------------ format */

function formatCount(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";
}

function percentText(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)}%` : "—";
}
