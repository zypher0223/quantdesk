import { useMemo, useState } from "react";
import { IconActivity, IconAdjustments, IconBrain, IconChartCandle, IconDatabase, IconFileAnalytics, IconFlask, IconHeartbeat, IconHistory, IconPhoto, IconRefresh, IconReportAnalytics, IconAlertTriangle } from "@tabler/icons-react";
import { BackgroundBeams } from "./components/ui/background-beams";
import { FileUpload } from "./components/ui/file-upload";
import { FloatingDock } from "./components/ui/floating-dock";
import { MarketChart } from "./components/market-chart";
import { BacktestWorkspace } from "./components/backtest-workspace";
import { ChartAnalysisWorkspace } from "./components/chart-analysis";
import { SettingsWorkspace } from "./components/settings-workspace";
import { PaperWorkspace } from "./components/paper-workspace";
import { ResearchWorkspace } from "./components/research-workspace";
import { MonitoringWorkspace } from "./components/monitoring-workspace";
import { HistoryDataWorkspace } from "./components/history-data-workspace";
import { ResultCentreWorkspace } from "./components/result-centre-workspace";
import { AgentCampaignWorkspace } from "./components/agent-campaign-workspace";
import type { TradingPlan } from "./services/research";
import { RISK_COPY, TIMEFRAMES, instrumentsByPool, type Instrument, type InstrumentIndex, type Timeframe } from "./data/market";
import { useMarket } from "./hooks/use-market";
import { compactUsdt, formatCandleRange, formatClock, formatPercent, formatPrice, formatRate, relativeAge } from "./lib/format";
import { parseCandleFile } from "./lib/parse-candles";

type Workspace = "market" | "upload" | "analysis" | "research" | "backtest" | "results" | "campaigns" | "journal" | "history-data" | "monitoring" | "settings";

const POOL_ORDER: Array<"stock" | "crypto"> = ["stock", "crypto"];

export default function App() {
  const market = useMarket();
  const [workspace, setWorkspace] = useState<Workspace>("market");
  const [uploadError, setUploadError] = useState<string | null>(null);
  // A plan handed from research to the paper-trading form.
  const [planPrefill, setPlanPrefill] = useState<TradingPlan | null>(null);
  const { state, index } = market;

  // The newest bar that has actually closed. Naming it - and naming the bar that is
  // still forming - is what stops a reader from comparing the last candle against the
  // wall clock and concluding the timestamps are wrong: the chart is one closed bar
  // behind the live market by design, and now it says so.
  const lastClosed = state.candles.at(-1) ?? null;
  const instrument = state.instrument;
  const ticker = state.ticker;
  const change = ticker?.price24hPcnt ?? null;
  const pools = useMemo(() => instrumentsByPool(index), [index]);

  const dockItems = [
    { title: "市场", icon: <IconChartCandle />, active: workspace === "market", onSelect: () => setWorkspace("market") },
    { title: "K线上传", icon: <IconFileAnalytics />, active: workspace === "upload", onSelect: () => setWorkspace("upload") },
    { title: "截图识别", icon: <IconPhoto />, active: workspace === "analysis", onSelect: () => setWorkspace("analysis") },
    { title: "深度研判", icon: <IconBrain />, active: workspace === "research", onSelect: () => setWorkspace("research") },
    { title: "策略回测", icon: <IconFlask />, active: workspace === "backtest", onSelect: () => setWorkspace("backtest") },
    { title: "结果中心", icon: <IconReportAnalytics />, active: workspace === "results", onSelect: () => setWorkspace("results") },
    { title: "代理战役", icon: <IconBrain />, active: workspace === "campaigns", onSelect: () => setWorkspace("campaigns") },
    { title: "交易日志", icon: <IconHistory />, active: workspace === "journal", onSelect: () => setWorkspace("journal") },
    { title: "历史数据", icon: <IconDatabase />, active: workspace === "history-data", onSelect: () => setWorkspace("history-data") },
    { title: "运行监控", icon: <IconHeartbeat />, active: workspace === "monitoring", onSelect: () => setWorkspace("monitoring") },
    { title: "设置", icon: <IconAdjustments />, active: workspace === "settings", onSelect: () => setWorkspace("settings") },
  ];

  const handleUpload = async (file: File) => {
    try {
      const parsed = parseCandleFile(await file.text(), file.name);
      market.applyUpload(parsed, `${file.name} · ${parsed.length} 根`);
      setUploadError(null);
    } catch (reason) {
      setUploadError(reason instanceof Error ? reason.message : "文件解析失败");
    }
  };

  return (
    <main className="app-shell">
      <BackgroundBeams className="beams" />
      <header className="topbar">
        <button type="button" className="brand" onClick={() => setWorkspace("market")}><span className="brand-mark">QD</span><span>QUANTDESK</span></button>
        <StatusReadout state={state} />
        <button type="button" className="refresh-button" onClick={() => market.refresh()} disabled={state.loading}>
          <IconRefresh size={16} className={state.loading ? "spin" : ""} />刷新
        </button>
      </header>

      <aside className="universe-panel">
        <div className="universe-heading">
          <span>合约池</span>
          <span className="pool-totals">
            {POOL_ORDER.map((key) => {
              const pool = index?.pools.find((item) => item.key === key);
              return pool ? <strong key={key}>{pool.label} {pool.count}</strong> : null;
            })}
          </span>
        </div>
        <nav className="instrument-list" aria-label="固定交易标的">
          {market.indexError && <p className="universe-error">{market.indexError}</p>}
          {pools.map(({ pool, items }) => (
            <section key={pool.key} className="instrument-group">
              <h3>{pool.label}<em>{pool.count}</em></h3>
              {items.map((item) => (
                <InstrumentButton
                  key={item.venueSymbol}
                  item={item}
                  selected={item.venueSymbol === instrument?.venueSymbol}
                  onSelect={() => { market.select(item); setWorkspace("market"); }}
                />
              ))}
            </section>
          ))}
          {!index && !market.indexError && <p className="universe-loading">正在读取固定合约池…</p>}
        </nav>
      </aside>

      <section className="workspace">
        <div className="market-heading">
          <div>
            <h1>{instrument?.displaySymbol ?? "—"}<span>/ USDT PERP</span></h1>
            <p>
              {instrument ? `${instrument.name} · ${instrument.group} · Bybit Linear ${instrument.productLabel}` : "等待合约池"}
              {instrument?.symbolMapped && <em className="mapping-note">交易所代码 {instrument.venueSymbol}</em>}
            </p>
          </div>
          <div className="price-block">
            <strong>{formatPrice(ticker?.lastPrice)}</strong>
            <span className={change === null ? "" : change >= 0 ? "positive" : "negative"}>24H {formatPercent(change)}</span>
          </div>
        </div>

        <div className="timeframe-strip">
          <div className="timeframes" role="group" aria-label="K线周期">
            {TIMEFRAMES.map((frame) => (
              <button key={frame} type="button" className={frame === state.timeframe ? "active" : ""} aria-pressed={frame === state.timeframe} onClick={() => market.setTimeframe(frame)}>{frame}</button>
            ))}
          </div>
          <span>
            仅使用已收盘K线
            {lastClosed && ` · 最新已收盘 ${formatCandleRange(lastClosed.time, state.timeframe)}`}
            {state.formingCandle &&
              ` · 形成中 ${formatCandleRange(state.formingCandle.time, state.timeframe)}（未参与分析）`}
            {market.barClosesAt && ` · 本根收于 ${formatClock(market.barClosesAt)} CST`}
          </span>
        </div>

        <StatusAlert state={state} />

        {workspace === "market" && (
          <div className="market-grid">
            <section className="chart-panel">
              <div className="chart-meta">
                <span>价格结构 · {state.timeframe}</span>
                <span>VOL · {state.candles.length} BARS · {state.sources.candles === "live" ? "BYBIT" : state.sources.candles === "upload" ? "上传" : "演示"}</span>
              </div>
              <MarketChart
                candles={state.candles}
                timeframe={state.timeframe}
                symbol={instrument?.venueSymbol}
                formingCandle={state.formingCandle}
                // The CPA overlay is opt-in per chart: the switch appears, the
                // overlay itself stays off until a reader turns it on.
                cpaEnabled
              />
            </section>
            <SignalPanel market={market} index={index} />
            <DerivativesStrip market={market} />
          </div>
        )}

        {workspace === "upload" && (
          <section className="feature-workspace">
            <div className="feature-copy">
              <h2>载入你自己的K线</h2>
              <p>文件只在当前浏览器会话中解析。载入后可立即切换周期、查看结构，并作为后续回测与模型分析的数据输入。</p>
              <ul><li>CSV 或 JSON</li><li>标准 OHLCV 字段</li><li>按时间升序或降序均可</li></ul>
              {market.uploadLabel && (
                <p className="upload-active">当前图表使用：{market.uploadLabel}
                  <button type="button" onClick={market.clearUpload}>恢复实时行情</button>
                </p>
              )}
              {uploadError && <p className="upload-error-inline" role="alert">{uploadError}</p>}
            </div>
            <FileUpload onChange={handleUpload} />
          </section>
        )}

        {workspace === "backtest" && instrument && (
          <BacktestWorkspace candles={state.candles} instrument={instrument} timeframe={state.timeframe} marketSource={state.sources.candles} />
        )}
        {workspace === "analysis" && <ChartAnalysisWorkspace instrument={instrument} timeframe={state.timeframe} />}
        {workspace === "research" && (
          <ResearchWorkspace
            instrument={instrument}
            timeframe={state.timeframe}
            candles={state.candles}
            source={state.sources.candles}
            onSendToPaper={(plan) => {
              setPlanPrefill(plan);
              setWorkspace("journal");
            }}
          />
        )}
        {workspace === "journal" && (
          <PaperWorkspace instrument={instrument} prefill={planPrefill} onPrefillConsumed={() => setPlanPrefill(null)} />
        )}
        {workspace === "monitoring" && <MonitoringWorkspace />}
        {workspace === "results" && <ResultCentreWorkspace />}
        {workspace === "campaigns" && <AgentCampaignWorkspace />}
        {workspace === "history-data" && <HistoryDataWorkspace />}
        {workspace === "settings" && <SettingsWorkspace />}
      </section>
      <FloatingDock items={dockItems} />
    </main>
  );
}

function StatusReadout({ state }: { state: ReturnType<typeof useMarket>["state"] }) {
  const level = state.status.level;
  const tone = level === "live" ? "status-live" : level === "down" ? "status-down" : "status-demo";
  // The ticker is the part that arrives continuously, so it is what "how fresh
  // is this page" means; the candle timestamp only moves when a bar closes.
  const stamp = state.tickerFetchedAt ?? state.candlesFetchedAt;
  const delay = state.feed?.lastMessageAgeMs;
  return (
    <div className="system-state">
      <span className={tone} />
      <span>{state.status.message}</span>
      <span
        className="timestamp"
        title={
          delay === null || delay === undefined
            ? "最近一次成功获取数据的时间"
            : `最近一次成功获取数据的时间；交易所消息延迟 ${delay} ms`
        }
      >
        {relativeAge(stamp)}
      </span>
    </div>
  );
}

function StatusAlert({ state }: { state: ReturnType<typeof useMarket>["state"] }) {
  if (state.status.level === "live") return null;
  const tone = state.status.level === "down" ? "data-alert data-alert-down" : state.status.level === "degraded" ? "data-alert data-alert-warn" : "data-alert";
  return (
    <div className={tone} role="status">
      <IconAlertTriangle size={15} />
      <div>
        <strong>{state.status.message}</strong>
        {state.status.detail && <span>{state.status.detail}</span>}
      </div>
    </div>
  );
}

function InstrumentButton({ item, selected, onSelect }: { item: Instrument; selected: boolean; onSelect: () => void }) {
  return (
    <button type="button" className={selected ? "instrument active" : "instrument"} onClick={onSelect} aria-pressed={selected}>
      <span><strong>{item.displaySymbol}</strong><small>{item.name}</small></span>
      <em className={item.riskClass === "leveraged_etf" ? "tag-leveraged" : undefined}>{item.productLabel}</em>
    </button>
  );
}

function SignalPanel({ market, index }: { market: ReturnType<typeof useMarket>; index: InstrumentIndex | null }) {
  const { state } = market;
  const instrument = state.instrument;
  const resonance = state.resonance;
  const frames = resonance?.timeframes ?? [];
  const unavailable = new Map((resonance?.unavailable ?? []).map((entry) => [entry.interval, entry]));
  const score = resonance?.score_100 ?? null;
  const staleFrames = (resonance?.unavailable ?? []).length > 0;

  return (
    <aside className="signal-panel">
      <div className="radar-title">
        <IconActivity size={18} />
        <span>多周期雷达</span>
        <em className="source-chip">{state.sources.resonance === "live" ? "引擎计算" : state.sources.resonance === "demo" ? "演示数据" : "加载中"}</em>
      </div>

      {score === null ? (
        <p className="radar-empty">四周期数据尚未就绪。</p>
      ) : (
        <>
          <strong className={`signal-score ${(resonance?.score ?? 0) >= 0 ? "positive" : "negative"}`}>
            {Math.round(score)}<small>/100</small>
          </strong>
          <p className="signal-label">{resonance?.label}</p>
        </>
      )}

      <div className="resonance-bars">
        {TIMEFRAMES.map((frame) => {
          const entry = frames.find((item) => item.interval === frame);
          const missing = unavailable.get(frame);
          const stance = entry?.stance ?? null;
          return (
            <div key={frame} className={missing ? "bar-unavailable" : undefined}>
              <span>{frame}</span>
              <i>
                <b
                  className={stance === "bear" ? "bar-bear" : stance === "bull" ? "bar-bull" : "bar-neutral"}
                  style={{ width: entry?.score == null ? "0%" : `${Math.max(4, Math.abs(entry.score) * 100)}%` }}
                />
              </i>
              <em title={missing ? `仅 ${missing.bars}/${missing.required} 根已收盘K线` : undefined}>
                {missing ? "不足" : stance === "bull" ? "多" : stance === "bear" ? "空" : "中"}
              </em>
            </div>
          );
        })}
      </div>
      <p className="radar-note">
        权重 {resonance ? Object.entries(resonance.weights).map(([key, value]) => `${key} ${value}`).join(" / ") : "1d 0.4 / 4h 0.3 / 1h 0.2 / 15m 0.1"}
        {staleFrames && ` · 标记「不足」的周期未达 ${index?.resonanceMinBars ?? 220} 根已收盘K线，不参与评分`}
      </p>

      <dl className="risk-readout">
        <div><dt>风险类别</dt><dd>{instrument ? RISK_COPY[instrument.riskClass] : "—"}</dd></div>
        <div><dt>量能状态</dt><dd>{describeVolume(frames)}</dd></div>
        <div><dt>数据来源</dt><dd>{describeSources(state)}</dd></div>
      </dl>
    </aside>
  );
}

function describeVolume(frames: ReturnType<typeof useMarket>["state"]["resonance"] extends null ? never : NonNullable<ReturnType<typeof useMarket>["state"]["resonance"]>["timeframes"]): string {
  if (!frames.length) return "—";
  const thin = frames.filter((frame) => frame.notes?.session_thin).map((frame) => frame.interval);
  const expanding = frames.filter((frame) => frame.volume !== 0).map((frame) => frame.interval);
  if (thin.length === frames.length) return "标的市场休市，量能票已全部停用";
  const parts: string[] = [];
  if (expanding.length) parts.push(`${expanding.join("/")} 放量`);
  if (thin.length) parts.push(`${thin.join("/")} 休市空 bar，量能票停用`);
  return parts.join("；") || "无放量周期";
}

function describeSources(state: ReturnType<typeof useMarket>["state"]): string {
  const label = (source: string) => (source === "live" ? "Bybit" : source === "upload" ? "上传" : source === "demo" ? "演示" : "无");
  return `K线 ${label(state.sources.candles)} · 衍生品 ${label(state.sources.ticker)} · 共振 ${label(state.sources.resonance)}`;
}

function DerivativesStrip({ market }: { market: ReturnType<typeof useMarket> }) {
  const { state } = market;
  const ticker = state.ticker;
  const instrument = state.instrument;
  const funding = ticker?.fundingRate ?? null;
  const fundingWarn = funding !== null && Math.abs(funding) > 0.0005;
  const cryptoZero = ticker !== null && funding === 0 && instrument?.productType !== "crypto";

  return (
    <section className="derivatives-strip">
      <Metric label="标记价格" value={formatPrice(ticker?.markPrice)} hint="交易所强平与未实现盈亏的计价基准" />
      <Metric label="指数价格" value={formatPrice(ticker?.indexPrice)} hint="现货指数，用于判断基差" />
      <Metric
        label="资金费率"
        value={formatRate(funding)}
        tone={fundingWarn ? "warn" : undefined}
        hint={
          cryptoZero
            ? "TradFi 股票永续当前费率为交易所实际报价 0，并非数据缺失"
            : ticker?.nextFundingTime
              ? `每 ${ticker.fundingIntervalHour ?? 8} 小时结算 · 下次 ${formatClock(ticker.nextFundingTime)}`
              : "每 8 小时结算"
        }
      />
      <Metric
        label="持仓量"
        value={compactUsdt(ticker?.openInterestValue)}
        hint={ticker?.openInterest != null ? `${compactUsdt(ticker.openInterest)} 张 × 标记价折算为 USDT 名义额` : "USDT 名义额"}
      />
      <Metric label="24H成交额" value={compactUsdt(ticker?.turnover24h)} hint={ticker?.volume24h != null ? `${compactUsdt(ticker.volume24h)} 张成交` : "过去 24 小时名义成交额"} />
    </section>
  );
}

function Metric({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "warn" }) {
  return (
    <div className={tone === "warn" ? "metric metric-warn" : "metric"} title={hint}>
      <span>{label}</span>
      <strong>{value}</strong>
      {hint && <small>{hint}</small>}
    </div>
  );
}
