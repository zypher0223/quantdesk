import { useEffect, useMemo, useState } from "react";
import { IconAlertTriangle, IconBell, IconClockPlay, IconInfoCircle, IconPlayerPlay } from "@tabler/icons-react";
import type { Candle, Instrument, Source, Timeframe } from "../data/market";
import { backtestRequestBody, DEFAULT_BACKTEST_CONFIG, EXIT_REASON_LABEL, fetchStrategies, runBacktest, type BacktestConfig, type BacktestResult, type StrategyInfo, type StrategyParams, type StudyEnvelope } from "../services/backtest";
import { queuedNotice } from "../services/study-queue";
import { CpaParameterPanel } from "./cpa-parameter-panel";
import { submitRun } from "../services/runs";
import { createStrategyAlert } from "../services/alerts";

interface Props {
  candles: Candle[];
  instrument: Instrument;
  timeframe: Timeframe;
  marketSource: Source;
}

const SOURCE_LABEL: Record<Source, string> = {
  live: "Bybit 行情",
  upload: "上传文件",
  demo: "演示数据",
  none: "无数据",
};

export function BacktestWorkspace({ candles, instrument, timeframe, marketSource }: Props) {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [strategyId, setStrategyId] = useState("ma_cross");
  const [strategyParams, setStrategyParams] = useState<StrategyParams>({});
  const [strategyError, setStrategyError] = useState("");
  const [fastPeriod, setFastPeriod] = useState("9");
  const [slowPeriod, setSlowPeriod] = useState("21");
  const [direction, setDirection] = useState<BacktestConfig["direction"]>("both");
  const [capital, setCapital] = useState("10000");
  const [allocation, setAllocation] = useState("50");
  const [feeBps, setFeeBps] = useState("");
  const [slippageBps, setSlippageBps] = useState("");
  const [leverage, setLeverage] = useState("1");
  const [maintenanceMarginRate, setMaintenanceMarginRate] = useState("0.5");
  const [includeFunding, setIncludeFunding] = useState(true);
  const [includeLiquidation, setIncludeLiquidation] = useState(true);
  const [fillOnThin, setFillOnThin] = useState<"skip" | "allow">("skip");
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [queueBusy, setQueueBusy] = useState(false);
  const [queueNote, setQueueNote] = useState("");
  const [queueError, setQueueError] = useState("");
  const [alertState, setAlertState] = useState<"" | "saving" | "saved">("");
  const [alertError, setAlertError] = useState("");
  const [lastRunAlertConfig, setLastRunAlertConfig] = useState<{
    strategyId: string;
    strategyName: string;
    parameters: StrategyParams;
  } | null>(null);

  useEffect(() => {
    let active = true;
    void fetchStrategies()
      .then((catalog) => {
        if (!active) return;
        setStrategies(catalog.strategies);
        setStrategyError(catalog.pluginErrors.map((item) => `${item.pluginId}: ${item.error}`).join("；"));
      })
      .catch((reason) => { if (active) setStrategyError(reason instanceof Error ? reason.message : "读取策略目录失败"); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    setResult(null);
    setError("");
    setAlertState("");
    setAlertError("");
    setLastRunAlertConfig(null);
    setQueueNote("");
    setQueueError("");
  }, [candles, instrument.venueSymbol, timeframe]);

  const selectedStrategy = useMemo(
    () => strategies.find((strategy) => strategy.id === strategyId),
    [strategies, strategyId],
  );
  const requiredLookback = strategyId === "ma_cross"
    ? Number(slowPeriod)
    : strategyId === "cpa_cycle"
      // The engine reports "insufficient sample" rather than trading on too few bars;
      // the form refuses the run at the same threshold so the two never disagree.
      ? Number(strategyParams.minBars ?? 60)
      : Number(strategyParams.lookback ?? strategyParams.period ?? 21);
  const canRun = candles.length >= requiredLookback + 3;

  // One builder for both submit paths, so a background task reproduces the
  // foreground run instead of drifting from it.
  const buildStudy = (): { config: BacktestConfig; parameters: StrategyParams } => {
    const parameters = strategyId === "ma_cross"
      ? { fastPeriod: Number(fastPeriod), slowPeriod: Number(slowPeriod) }
      : { ...strategyParams };
    const config: BacktestConfig = {
      ...DEFAULT_BACKTEST_CONFIG,
      strategyId,
      strategyParams: parameters,
      fastPeriod: Number(fastPeriod),
      slowPeriod: Number(slowPeriod),
      direction,
      initialCapital: Number(capital),
      allocationPct: Number(allocation),
      feeBps: feeBps.trim() === "" ? null : Number(feeBps),
      slippageBps: slippageBps.trim() === "" ? null : Number(slippageBps),
      leverage: Number(leverage),
      maintenanceMarginRate: Number(maintenanceMarginRate) / 100,
      includeFunding,
      includeLiquidation,
      fillOnThin,
    };
    return { config, parameters };
  };
  // Only an upload is worth sending: live candles are re-read by the engine.
  const uploadedCandles = marketSource === "upload" ? candles : undefined;

  const run = async () => {
    setBusy(true);
    setError("");
    setAlertState("");
    setAlertError("");
    setQueueNote("");
    setLastRunAlertConfig(null);
    try {
      const { config, parameters } = buildStudy();
      const outcome = await runBacktest(instrument, timeframe, config, uploadedCandles);
      // A study too large for one request was moved to the queue: it has not
      // produced numbers, so nothing here may be rendered as a finished result.
      if (outcome.queued) {
        setResult(null);
        setLastRunAlertConfig(null);
        setQueueNote(`${queuedNotice(outcome)}${outcome.reason ? ` · ${outcome.reason}` : ""}`);
        return;
      }
      setResult(outcome.study);
      setLastRunAlertConfig({
        strategyId,
        strategyName: selectedStrategy?.name ?? strategyId,
        parameters,
      });
    } catch (reason) {
      setResult(null);
      setLastRunAlertConfig(null);
      setError(reason instanceof Error ? reason.message : "回测失败");
    } finally {
      setBusy(false);
    }
  };

  const runAblation = async () => {
    setQueueBusy(true);
    setQueueNote("");
    setQueueError("");
    try {
      const group = instrument.productType === "crypto"
        ? "crypto"
        : ["SOXL", "SOXS"].includes(instrument.displaySymbol.toUpperCase())
          ? "leveraged_etf"
          : "stock";
      const outcome = await submitRun(
        "cpa_ablation",
        { group, interval: timeframe, bars: Math.max(60, Math.min(1200, candles.length)), allowDegraded: false },
        "CPA 消融 · " + group + " · " + timeframe,
      );
      setQueueNote("CPA 消融已提交到结果中心（#" + outcome.run.id + "），会按策略变体而不是按标的计算 DSR/PBO。");
    } catch (reason) {
      setQueueError(reason instanceof Error ? reason.message : "提交 CPA 消融失败");
    } finally {
      setQueueBusy(false);
    }
  };

  const runInBackground = async () => {
    setQueueBusy(true);
    setQueueNote("");
    setQueueError("");
    try {
      const { config } = buildStudy();
      const label = `${instrument.displaySymbol} · ${timeframe} · ${selectedStrategy?.name ?? strategyId}`;
      const outcome = await submitRun("backtest", backtestRequestBody(instrument, timeframe, config, uploadedCandles), label);
      setQueueNote(`已提交到结果中心（#${outcome.run.id}）${outcome.deduplicated ? " · 与进行中的相同请求合并" : "，可离开本页，进度在结果中心查看"}`);
    } catch (reason) {
      setQueueError(reason instanceof Error ? reason.message : "提交后台任务失败");
    } finally {
      setQueueBusy(false);
    }
  };

  const createSignalAlert = async () => {
    if (!lastRunAlertConfig) return;
    setAlertState("saving");
    setAlertError("");
    try {
      await createStrategyAlert({
        venueSymbol: instrument.venueSymbol,
        timeframe,
        strategyId: lastRunAlertConfig.strategyId,
        strategyParameters: lastRunAlertConfig.parameters,
        signalDirection: "any",
        name: `${instrument.displaySymbol} · ${lastRunAlertConfig.strategyName} 新信号`,
      });
      setAlertState("saved");
    } catch (reason) {
      setAlertState("");
      setAlertError(reason instanceof Error ? reason.message : "创建策略告警失败");
    }
  };

  const periodLabel = useMemo(
    () => (candles.length > 1 ? `${formatDate(candles[0].time)} — ${formatDate(candles.at(-1)!.time)}` : "暂无区间"),
    [candles],
  );

  return (
    <section className="backtest-workspace">
      <div className="backtest-heading">
        <div>
          <h2>规则策略回测</h2>
          <p>{instrument.displaySymbol} · {timeframe} · {SOURCE_LABEL[marketSource]} · {candles.length} 根K线 · 引擎计算</p>
        </div>
        <span className="backtest-period">{periodLabel}</span>
      </div>
      <div className="backtest-layout">
        <form className="backtest-controls" onSubmit={(event) => { event.preventDefault(); void run(); }}>
          <h3>{selectedStrategy?.name ?? "策略"}</h3>
          <p>{selectedStrategy?.description ?? "收盘确认信号，下一根K线开盘成交。"}</p>
          <label>策略
            <select value={strategyId} onChange={(event) => {
              const nextId = event.target.value;
              const next = strategies.find((item) => item.id === nextId);
              // CPA's defaults depend on the contract class and the interval, which the
              // engine publishes per combination; the panel takes the form from empty
              // and fills it from that table.
              const defaults = nextId === "cpa_cycle"
                ? {}
                : Object.fromEntries((next?.parameters ?? []).map((item) => [item.key, item.default]));
              setStrategyId(nextId);
              setStrategyParams(defaults);
              if (nextId === "ma_cross") {
                setFastPeriod(String(defaults.fastPeriod ?? 9));
                setSlowPeriod(String(defaults.slowPeriod ?? 21));
              }
              setResult(null);
              setLastRunAlertConfig(null);
            }}>
              {(strategies.length ? strategies : [{ id: "ma_cross", name: "双均线交叉", source: "builtin" } as StrategyInfo]).map((strategy) => (
                <option key={strategy.id} value={strategy.id}>{strategy.name}{strategy.source === "plugin" ? ` · 插件 ${strategy.plugin_id}` : ""}</option>
              ))}
            </select>
          </label>
          {strategyId === "ma_cross" ? (
            <div className="backtest-field-row">
              <label>快线周期<input type="number" min="2" max="200" value={fastPeriod} onChange={(event) => setFastPeriod(event.target.value)} /></label>
              <label>慢线周期<input type="number" min="3" max="500" value={slowPeriod} onChange={(event) => setSlowPeriod(event.target.value)} /></label>
            </div>
          ) : strategyId === "cpa_cycle" ? (
            <CpaParameterPanel
              productType={instrument.productType}
              timeframe={timeframe}
              availableBars={candles.length}
              value={strategyParams}
              onChange={(next) => { setStrategyParams(next); setResult(null); }}
            />
          ) : (
            <div className="backtest-field-row">
              {(selectedStrategy?.parameters ?? []).map((parameter) => {
                const value = strategyParams[parameter.key] ?? parameter.default;
                if (parameter.type === "boolean") {
                  return (
                    <label className="backtest-plugin-toggle" key={parameter.key}>
                      <input
                        type="checkbox"
                        checked={Boolean(value)}
                        onChange={(event) => setStrategyParams((current) => ({ ...current, [parameter.key]: event.target.checked }))}
                      />
                      {parameter.label}
                    </label>
                  );
                }
                if (parameter.type === "select") {
                  return (
                    <label key={parameter.key}>{parameter.label}
                      <select
                        value={String(value)}
                        onChange={(event) => setStrategyParams((current) => ({ ...current, [parameter.key]: event.target.value }))}
                      >
                        {(parameter.options ?? []).map((option) => <option key={option} value={option}>{option}</option>)}
                      </select>
                    </label>
                  );
                }
                return (
                  <label key={parameter.key}>{parameter.label}
                    <input
                      type="number"
                      min={parameter.minimum ?? undefined}
                      max={parameter.maximum ?? undefined}
                      step={parameter.type === "integer" ? 1 : "any"}
                      value={String(value)}
                      onChange={(event) => setStrategyParams((current) => ({ ...current, [parameter.key]: Number(event.target.value) }))}
                    />
                  </label>
                );
              })}
            </div>
          )}
          <label>交易方向<select value={direction} onChange={(event) => setDirection(event.target.value as BacktestConfig["direction"])}><option value="both">双向（做多 / 做空）</option><option value="long">仅做多</option></select></label>
          <div className="backtest-field-row">
            <label>初始资金（USDT）<input type="number" min="100" step="100" value={capital} onChange={(event) => setCapital(event.target.value)} /></label>
            <label>每次仓位（净值%）<input type="number" min="1" max="100" step="1" value={allocation} onChange={(event) => setAllocation(event.target.value)} /></label>
          </div>
          <div className="backtest-field-row">
            <label>手续费（bps，留空用配置默认）<input type="number" min="0" step="1" value={feeBps} placeholder="默认" onChange={(event) => setFeeBps(event.target.value)} /></label>
            <label>滑点（bps，留空用配置默认）<input type="number" min="0" step="1" value={slippageBps} placeholder="默认" onChange={(event) => setSlippageBps(event.target.value)} /></label>
          </div>
          <div className="backtest-field-row">
            <label>杠杆（x）<input type="number" min="1" max="100" step="1" value={leverage} onChange={(event) => setLeverage(event.target.value)} /></label>
            <label>维持保证金率（%）<input type="number" min="0.1" max="20" step="0.1" value={maintenanceMarginRate} onChange={(event) => setMaintenanceMarginRate(event.target.value)} /></label>
          </div>
          <label>休市空 bar
            <select value={fillOnThin} onChange={(event) => setFillOnThin(event.target.value as "skip" | "allow")}>
              <option value="skip">不建仓（默认，避免无深度成交）</option>
              <option value="allow">按价格成交（会标注警告）</option>
            </select>
          </label>
          <div className="backtest-toggles">
            <label><input type="checkbox" checked={includeFunding} onChange={(event) => setIncludeFunding(event.target.checked)} />计入历史资金费率</label>
            <label><input type="checkbox" checked={includeLiquidation} onChange={(event) => setIncludeLiquidation(event.target.checked)} />启用杠杆强平</label>
          </div>
          <div className="backtest-submit-row">
            <button className="backtest-run" type="submit" disabled={!canRun || busy}>
              <IconPlayerPlay size={16} />{busy ? "引擎计算中…" : "运行回测"}
            </button>
            <button
              className="backtest-queue"
              type="button"
              title="提交到结果中心后台执行，提交后可离开本页"
              disabled={!canRun || busy || queueBusy}
              onClick={() => void runInBackground()}
            >
              <IconClockPlay size={16} />{queueBusy ? "提交中…" : "后台运行"}
            </button>
            {strategyId === "cpa_cycle" && (
              <button
                className="backtest-queue"
                type="button"
                title="在同一资产组上比较 CPA 变体，并在结果中心查看 DSR/PBO"
                disabled={!canRun || busy || queueBusy}
                onClick={() => void runAblation()}
              >
                <IconClockPlay size={16} />运行 CPA 消融
              </button>
            )}
          </div>
          <button className="backtest-alert" type="button" disabled={!result || !lastRunAlertConfig || busy || alertState !== ""} onClick={() => void createSignalAlert()}>
            <IconBell size={15} />{alertState === "saving" ? "正在创建…" : alertState === "saved" ? "已加入后台监控" : "监控该策略新信号"}
          </button>
          {alertError && <p className="backtest-error" role="alert">{alertError}</p>}
          {!canRun && <p className="backtest-hint">当前K线不足。请上传更多历史K线，或切换到可用数据。</p>}
          {queueNote && <p className="backtest-hint" role="status">{queueNote}</p>}
          {queueError && <p className="backtest-error" role="alert">{queueError}</p>}
          {error && <p className="backtest-error" role="alert">{error}</p>}
          {strategyError && <p className="backtest-error" role="status">插件策略目录：{strategyError}</p>}
          <div className="backtest-caveat"><IconInfoCircle size={16} /><span>回测在本地引擎运行，包含历史资金费率、逐仓强平、交易所步长取整与最小下单额。仓位按净值百分比复利计算。</span></div>
        </form>
        <div className="backtest-results">
          {!result ? <div className="backtest-empty"><strong>配置参数后运行</strong><span>回测将使用当前标的和周期的K线，不会提交真实订单。</span></div> : <BacktestReport result={result} />}
        </div>
      </div>
    </section>
  );
}

function BacktestReport({ result }: { result: BacktestResult }) {
  const points = result.equity_curve.filter((_, index, all) => index === 0 || index === all.length - 1 || index % Math.max(1, Math.floor(all.length / 90)) === 0);
  const values = points.map((point) => point.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(max - min, 1);
  const line = points.map((point, index) => `${index === 0 ? "M" : "L"} ${(index / Math.max(1, points.length - 1)) * 1000} ${250 - ((point.equity - min) / range) * 220}`).join(" ");
  const liquidations = result.trades.filter((trade) => trade.liquidated).length;

  return (
    <>
      <div className="backtest-stats">
        <BacktestStat label="净收益" value={`${result.net_return_pct >= 0 ? "+" : ""}${result.net_return_pct.toFixed(2)}%`} tone={result.net_return_pct >= 0 ? "positive" : "negative"} />
        <BacktestStat label="最大回撤" value={`-${result.max_drawdown_pct.toFixed(2)}%`} tone="negative" />
        <BacktestStat label="胜率" value={`${result.win_rate_pct.toFixed(1)}%`} />
        <BacktestStat label="盈亏比" value={result.profit_factor === null ? "∞" : result.profit_factor.toFixed(2)} />
        <BacktestStat label="交易次数" value={String(result.trades.length)} />
        <BacktestStat label="强平" value={liquidations ? `${liquidations} 笔` : "无"} tone={liquidations ? "negative" : undefined} />
        <BacktestStat label="手续费" value={`${formatMoney(result.total_fees)} USDT`} />
        <BacktestStat label="资金费" value={`${result.total_funding >= 0 ? "+" : ""}${formatMoney(result.total_funding)} USDT`} tone={result.total_funding > 0 ? "negative" : undefined} />
        <BacktestStat label="期末权益" value={`${formatMoney(result.final_equity)}`} />
      </div>

      {result.warnings.length > 0 && (
        <ul className="backtest-warnings">
          {result.warnings.map((warning, index) => <li key={index}><IconAlertTriangle size={13} />{warning}</li>)}
        </ul>
      )}

      <section className="backtest-chart-panel">
        <div><h3>净值曲线</h3><span>期末 {formatMoney(result.final_equity)} USDT</span></div>
        <svg viewBox="0 0 1000 270" preserveAspectRatio="none" role="img" aria-label="回测账户净值曲线"><line x1="0" x2="1000" y1="250" y2="250" /><path d={line} /></svg>
      </section>

      <section className="backtest-trades">
        <div className="backtest-trades-heading"><h3>成交记录</h3><span>最近 {Math.min(10, result.trades.length)} / {result.trades.length}</span></div>
        {result.trades.length === 0 ? <p className="backtest-hint">该区间没有满足条件的交叉信号。</p> : (
          <div className="backtest-table-wrap">
            <table>
              <thead><tr><th>方向</th><th>入场</th><th>出场</th><th>入场价</th><th>出场价</th><th>数量</th><th>资金费</th><th>手续费</th><th>净盈亏</th><th>退出</th></tr></thead>
              <tbody>
                {result.trades.slice(-10).reverse().map((trade, index) => (
                  <tr key={`${trade.entry_time}-${index}`} className={trade.liquidated ? "row-liquidated" : undefined}>
                    <td className={trade.direction === "多" ? "positive" : "negative"}>{trade.direction}</td>
                    <td>{formatDate(trade.entry_time)}</td>
                    <td>{formatDate(trade.exit_time)}</td>
                    <td>{formatPrice(trade.entry_price)}</td>
                    <td>{formatPrice(trade.exit_price)}</td>
                    <td>{trade.quantity.toLocaleString("en-US", { maximumFractionDigits: 4 })}</td>
                    <td>{trade.funding_paid >= 0 ? "+" : ""}{formatMoney(trade.funding_paid)}</td>
                    <td>{formatMoney(trade.fees)}</td>
                    <td className={trade.net_pnl >= 0 ? "positive" : "negative"}>{trade.net_pnl >= 0 ? "+" : ""}{formatMoney(trade.net_pnl)}</td>
                    <td>{EXIT_REASON_LABEL[trade.exit_reason] ?? trade.exit_reason}{trade.thin_entry ? " · 休市成交" : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <details className="backtest-assumptions">
        <summary>引擎声明的口径与数据质量</summary>
        <ul>{result.assumptions.map((line, index) => <li key={index}>{line}</li>)}</ul>
        <dl className="backtest-quality">
          <div><dt>数据来源</dt><dd>{String(result.data_quality.source ?? "—")}</dd></div>
          <div><dt>K线根数</dt><dd>{String(result.data_quality.bars ?? "—")}</dd></div>
          <div><dt>休市空 bar</dt><dd>{String(result.data_quality.thinBars ?? "—")}</dd></div>
          <div><dt>资金费数据点</dt><dd>{String(result.data_quality.fundingPoints ?? "—")}</dd></div>
          <div><dt>tickSize</dt><dd>{String((result.instrument as { tickSize?: number }).tickSize ?? "—")}</dd></div>
          <div><dt>qtyStep</dt><dd>{String((result.instrument as { qtyStep?: number }).qtyStep ?? "—")}</dd></div>
        </dl>
      </details>
    </>
  );
}

function BacktestStat({ label, value, tone }: { label: string; value: string; tone?: "positive" | "negative" }) {
  return <div className="backtest-stat"><span>{label}</span><strong className={tone}>{value}</strong></div>;
}

function formatMoney(value: number) { return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function formatPrice(value: number) { return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 }); }
function formatDate(time: number) { return new Date(time).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" }); }
