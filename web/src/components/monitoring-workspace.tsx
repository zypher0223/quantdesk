import { useCallback, useEffect, useState } from "react";
import { IconAlertTriangle, IconCircleCheck, IconRefresh, IconShieldX } from "@tabler/icons-react";
import { fetchMonitoring, type FrameQuality, type MarketDataComponent, type MonitoringOverview, type QualityState } from "../services/monitoring";
import { humanAge } from "../lib/snapshot";
import { fetchFeedStatus } from "../services/market";
import type { FeedStatus } from "../data/market";

const STATE_LABEL: Record<QualityState, string> = {
  healthy: "正常",
  degraded: "需关注",
  critical: "已阻断",
};

export function MonitoringWorkspace() {
  const [value, setValue] = useState<MonitoringOverview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [feed, setFeed] = useState<FeedStatus | null>(null);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setBusy(true);
    try {
      setValue(await fetchMonitoring());
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取运行状态失败");
    } finally {
      if (!quiet) setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  // The link state is polled faster than the full audit because that is the
  // number an operator watches while a proxy node is flapping.
  useEffect(() => {
    let active = true;
    const probe = () =>
      fetchFeedStatus()
        .then((status) => {
          if (active) setFeed(status);
        })
        .catch(() => {
          if (active) setFeed(null);
        });
    void probe();
    const timer = window.setInterval(probe, 5_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <section className="monitoring-workspace">
      <header className="monitoring-heading">
        <div>
          <h2>运行监控</h2>
          <p>17 个合约 · 4 个周期 · 数据质量门禁与后台组件状态</p>
        </div>
        <button type="button" onClick={() => void load()} disabled={busy}><IconRefresh className={busy ? "spin" : ""} size={15} />重新审计</button>
      </header>
      {error && <p className="monitoring-error" role="alert"><IconAlertTriangle size={15} />{error}</p>}
      <LiveFeedPanel feed={feed} fallback={value?.components.marketData as MarketDataComponent | undefined} />
      {!value ? <div className="monitoring-loading">正在检查 SQLite 行情和后台任务…</div> : <MonitoringReport value={value} />}
    </section>
  );
}

function LiveFeedPanel({ feed, fallback }: { feed: FeedStatus | null; fallback?: MarketDataComponent }) {
  const connection = feed ?? fallback?.connection ?? null;
  const state = connection?.state ?? "unknown";
  const label: Record<string, string> = {
    connected: "实时连接正常",
    starting: "正在建立连接",
    degraded: "连接中断，显示最后真实数据",
    stopped: "行情服务未运行",
    unknown: "状态未知",
  };
  const tone = state === "connected" ? "component-ok" : state === "starting" ? "component-warn" : "component-bad";
  const delay = connection?.lastMessageAgeMs ?? null;
  const streams = connection?.streams ?? [];
  return (
    <section className="monitoring-components">
      <div className="monitoring-section-title">
        <h3>实时行情链路</h3>
        <span>
          {delay === null ? "尚无交易所消息" : `最近消息延迟 ${delay} ms`}
          {connection?.lastSuccessAgeMs != null ? ` · 最近成功 ${humanAge(connection.lastSuccessAgeMs)}前` : ""}
        </span>
      </div>
      <div>
        <article className={tone}>
          {state === "connected" ? <IconCircleCheck size={17} /> : <IconShieldX size={17} />}
          <div>
            <strong>Bybit WebSocket</strong>
            <span>{label[state] ?? state}{connection?.lastError ? ` · ${connection.lastError}` : ""}</span>
          </div>
        </article>
        <article className={connection?.reconnects ? "component-warn" : "component-ok"}>
          {connection?.reconnects ? <IconAlertTriangle size={17} /> : <IconCircleCheck size={17} />}
          <div><strong>重连次数</strong><span>{connection?.reconnects ?? 0} 次 · {streams.length} 条连接</span></div>
        </article>
        <article className={connection?.proxyConfigured ? "component-ok" : "component-warn"}>
          {connection?.proxyConfigured ? <IconCircleCheck size={17} /> : <IconAlertTriangle size={17} />}
          <div><strong>代理</strong><span>{connection?.proxyConfigured ? "已按 configured_proxy() 配置（地址不显示）" : "未配置代理"}</span></div>
        </article>
        <article className={fallback?.backfillErrors?.length ? "component-warn" : "component-ok"}>
          {fallback?.backfillErrors?.length ? <IconAlertTriangle size={17} /> : <IconCircleCheck size={17} />}
          <div>
            <strong>REST 补洞</strong>
            <span>
              {fallback?.backfills ?? 0} 轮 · 最近 {fallback?.lastBackfillAt ? new Date(fallback.lastBackfillAt).toLocaleTimeString("zh-CN", { hour12: false }) : "—"}
              {fallback?.backfillErrors?.length ? ` · ${fallback.backfillErrors[0]}` : ""}
            </span>
          </div>
        </article>
        <article className={fallback?.duplicateClosed ? "component-warn" : "component-ok"}>
          {fallback?.duplicateClosed ? <IconAlertTriangle size={17} /> : <IconCircleCheck size={17} />}
          <div><strong>已收盘K线</strong><span>入库 {fallback?.closedEvents ?? 0} 根 · 去重丢弃 {fallback?.duplicateClosed ?? 0} 根</span></div>
        </article>
      </div>
    </section>
  );
}

function MonitoringReport({ value }: { value: MonitoringOverview }) {
  const scheduler = value.components.marketScheduler ?? {};
  const queue = value.components.tradingAgents ?? {};
  const alerts = value.components.alertEngine ?? {};
  const paper = value.components.paperMonitor ?? {};
  const collectionEnabled = Boolean((scheduler.config as Record<string, unknown> | undefined)?.marketCollectionEnabled);
  const derivativesAt = Number(scheduler.lastDerivativesAt ?? 0);
  const rotationDetail = scheduler.lastError
    ? String(scheduler.lastError)
    : collectionEnabled
      ? "逐合约采集正常"
      : `K线由 WebSocket 实时写入，轮转只刷新资金费率与持仓量${
          derivativesAt ? ` · 最近 ${new Date(derivativesAt).toLocaleTimeString("zh-CN", { hour12: false })}` : " · 尚未开始"
        }`;
  const external = (value.components.external ?? {}) as Record<string, any>;
  const openbb = (external.openbb ?? {}) as Record<string, any>;
  const fincept = (external.fincept ?? {}) as Record<string, any>;
  const componentRows: Array<{ name: string; status: string; detail: string }> = [
    { name: "行情轮转", status: String(scheduler.status ?? "unknown"), detail: rotationDetail },
    { name: "告警引擎", status: String(alerts.status ?? "unknown"), detail: `${alerts.enabledRules ?? 0} 条启用` },
    { name: "TradingAgents", status: queue.workerRunning ? "running" : "stopped", detail: queue.active ? "1 个正在执行" : "队列空闲" },
    { name: "模拟盘监控", status: paper.error ? "error" : "running", detail: String(paper.error ?? "风险轮询正常") },
    {
      name: "OpenBB 研究",
      status: openbb.configured ? (openbb.healthy ? "running" : "error") : "off",
      detail: openbb.configured
        ? `缓存 ${openbb.cachedRows ?? 0} 条 · 不可用 ${openbb.unavailableRows ?? 0} · 拒绝 ${openbb.rejectedRows ?? 0}`
        : "未启用（不影响行情与回测）",
    },
    {
      name: "Fincept 分析",
      status: fincept.configured ? (fincept.healthy ? "running" : "error") : "off",
      detail: fincept.configured
        ? `调用 ${fincept.calls ?? 0} · 成功率 ${fincept.successRate != null ? `${(Number(fincept.successRate) * 100).toFixed(0)}%` : "—"} · 429 ${fincept.rateLimited ?? 0} · 平均 ${fincept.averageMs ?? "—"}ms`
        : "未启用（组合风险页会提示）",
    },
  ];
  return (
    <>
      <div className="monitoring-summary">
        <Summary label="信号可用" value={`${value.summary.signalEligible}/${value.summary.total}`} tone="healthy" />
        <Summary label="正常" value={String(value.summary.healthy)} tone="healthy" />
        <Summary label="需关注" value={String(value.summary.degraded)} tone="degraded" />
        <Summary label="已阻断" value={String(value.summary.critical)} tone="critical" />
        <Summary label="24H 请求" value={String(value.provider.total)} />
        <Summary label="429 / 重试" value={`${value.provider.rateLimited} / ${value.provider.retried}`} tone={value.provider.rateLimited ? "degraded" : undefined} />
      </div>

      <section className="monitoring-components">
        <div className="monitoring-section-title"><h3>后台组件</h3><span>平均行情响应 {value.provider.averageLatencyMs}ms</span></div>
        <div>{componentRows.map((row) => {
          const healthy = row.status === "running" || row.status === "idle";
          return <article key={row.name} className={healthy ? "component-ok" : "component-bad"}>
            {healthy ? <IconCircleCheck size={17} /> : <IconShieldX size={17} />}
            <div><strong>{row.name}</strong><span>{row.detail || row.status}</span></div>
          </article>;
        })}</div>
      </section>

      {(openbb.configured || fincept.configured) && (
        <section className="quality-matrix">
          <div className="monitoring-section-title">
            <h3>外部服务</h3>
            <span>
              最近成功：
              {openbb.lastSuccess || fincept.lastSuccess
                ? new Date(Math.max(Number(openbb.lastSuccess ?? 0), Number(fincept.lastSuccess ?? 0))).toLocaleString("zh-CN", { hour12: false })
                : "尚未调用"}
            </span>
          </div>
          <div className="quality-table-wrap"><table>
            <thead><tr><th>服务</th><th>状态</th><th>凭据</th><th>最近错误</th></tr></thead>
            <tbody>
              <tr>
                <td>OpenBB</td>
                <td>{openbb.configured ? (openbb.healthy ? "健康" : "不可用") : "未启用"}</td>
                <td>{openbb.runtime?.runtimeReady ? openbb.runtime?.transport ?? "就绪" : openbb.runtime?.note ?? "—"}</td>
                <td>{openbb.lastError?.error ?? "无"}</td>
              </tr>
              <tr>
                <td>Fincept</td>
                <td>{fincept.configured ? (fincept.healthy ? "健康" : "不可用") : "未启用"}</td>
                <td>{fincept.runtime?.credentialPresent ? "已配置" : "未配置"}</td>
                <td>{fincept.lastError?.error ?? "无"}</td>
              </tr>
            </tbody>
          </table></div>
          <p className="monitoring-note">
            {Object.values((external.licences ?? {}) as Record<string, { name: string; licence: string }>)
              .map((item) => `${item.name}：${item.licence}`)
              .join(" · ")}
          </p>
        </section>
      )}

      <section className="quality-matrix">
        <div className="monitoring-section-title"><h3>数据质量矩阵</h3><span>{new Date(value.checkedAt).toLocaleString("zh-CN", { hour12: false })}</span></div>
        <div className="quality-table-wrap"><table>
          <thead><tr><th>合约</th>{["15m", "1h", "4h", "1d"].map((frame) => <th key={frame}>{frame}</th>)}<th>信号门禁</th></tr></thead>
          <tbody>{value.instruments.map((item) => <tr key={item.venueSymbol}>
            <th><strong>{item.displaySymbol}</strong><span>{item.name}</span></th>
            {item.frames.map((frame) => <td key={frame.timeframe}><FrameCell value={frame} /></td>)}
            <td><span className={`quality-gate ${item.signalEligible ? "open" : "closed"}`}>{item.signalEligible ? "可用" : "已阻断"}</span>{!item.signalEligible && <small title={item.blockingReasons.join("；")}>{item.blockingReasons[0]}</small>}</td>
          </tr>)}</tbody>
        </table></div>
      </section>

      <section className="provider-events">
        <div className="monitoring-section-title"><h3>最近行情请求</h3><span>失败 {value.provider.failed} · 重试 {value.provider.retried}</span></div>
        {value.provider.recent.length === 0 ? <p>还没有请求遥测记录</p> : value.provider.recent.slice(0, 12).map((row, index) => <article key={`${row.created_ts}-${index}`}>
          <time>{new Date(row.created_ts).toLocaleTimeString("zh-CN", { hour12: false })}</time>
          <strong>{row.symbol || "BYBIT"}</strong>
          <span>{row.operation.replace("/v5/market/", "")}</span>
          <em className={row.status === "succeeded" ? "request-ok" : "request-bad"}>{row.http_status || "ERR"} · {row.duration_ms}ms{row.attempt > 1 ? ` · 第 ${row.attempt} 次` : ""}</em>
        </article>)}
      </section>
    </>
  );
}

function Summary({ label, value, tone }: { label: string; value: string; tone?: QualityState }) {
  return <div className={tone ? `summary-${tone}` : undefined}><span>{label}</span><strong>{value}</strong></div>;
}

function FrameCell({ value }: { value: FrameQuality }) {
  return <div className={`quality-cell quality-${value.signalEligible ? value.status : "stale"}`} title={`缺口 ${value.gaps} · 异常OHLC ${value.invalidBars} · 价格异常 ${value.priceOutliers} · 量能异常 ${value.volumeOutliers}`}>
    <span>{value.statusLabel}</span>
    <small>{value.bars} bars{value.gaps ? ` · ${value.gaps} 缺口` : ""}</small>
  </div>;
}
