import { useCallback, useEffect, useState } from "react";
import { IconAlertTriangle, IconBell, IconBrandGithub, IconCheck, IconDownload, IconPackage, IconPlayerPlay, IconPlus, IconPlugConnected, IconRefresh, IconRobot, IconShieldCheck, IconTrash, IconWorld, IconX } from "@tabler/icons-react";
import {
  fetchLlmSettings,
  fetchLlmStatus,
  listLlmModels,
  saveModelConfig,
  saveLlmKeys,
  saveLlmRoles,
  testLlmProfile,
  type LlmProfile,
  type LlmSettings,
  type LlmStatus,
  type LlmTestResult,
} from "../services/llm";
import {
  fetchExternalStatus,
  saveExternalSettings,
  testExternalProvider,
  type ExternalStatus,
} from "../services/external";
import {
  checkPlugin,
  fetchPlugins,
  installPluginDependencies,
  installPlugin,
  setPluginEnabled,
  uninstallPlugin,
  updatePlugin,
  type PluginCatalog,
  type PluginHealth,
  type PluginInfo,
} from "../services/plugins";
import { fetchSchedulerStatus, runMarketCollection, saveSchedulerConfig, type SchedulerConfig, type SchedulerStatus } from "../services/scheduler";
import { createAlertRule, deleteAlertRule, evaluateAlerts, fetchAlerts, updateAlertRule, type AlertCatalog, type AlertRule, type AlertRuleCondition, type AlertRuleInput } from "../services/alerts";

const CAPABILITY_LABELS: Record<string, string> = {
  data_provider: "数据源",
  strategy: "策略",
  research_tool: "研判工具",
  analytics: "组合分析",
  factor_provider: "因子研究",
  backtest_validator: "回测验证",
  strategy_agent: "策略代理",
  notifier: "通知",
};

interface AlertConditionDraft {
  conditionType: string;
  timeframe: string;
  threshold: string;
}

export function SettingsWorkspace() {
  const [settings, setSettings] = useState<LlmSettings | null>(null);
  const [status, setStatus] = useState<LlmStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, LlmTestResult | { error: string }>>({});
  const [modelLists, setModelLists] = useState<Record<string, string[]>>({});
  const [modelDrafts, setModelDrafts] = useState<Record<string, { deep: string; quick: string; vision: string; maxTokens: number }>>({});
  const [roleDraft, setRoleDraft] = useState<Record<string, string>>({});
  const [plugins, setPlugins] = useState<PluginCatalog | null>(null);
  const [external, setExternal] = useState<ExternalStatus | null>(null);
  const [externalError, setExternalError] = useState<string | null>(null);
  const [pluginLoadError, setPluginLoadError] = useState<string | null>(null);
  const [pluginSource, setPluginSource] = useState("");
  const [pluginRef, setPluginRef] = useState("");
  const [pluginHealth, setPluginHealth] = useState<Record<string, PluginHealth | { error: string }>>({});
  const [scheduler, setScheduler] = useState<SchedulerStatus | null>(null);
  const [schedulerError, setSchedulerError] = useState<string | null>(null);
  const [alerts, setAlerts] = useState<AlertCatalog | null>(null);
  const [alertError, setAlertError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const pluginRequest = fetchPlugins()
        .then((value) => ({ value, error: null as string | null }))
        .catch((reason) => ({
          value: null,
          error: reason instanceof Error ? reason.message : "读取插件目录失败",
        }));
      const schedulerRequest = fetchSchedulerStatus()
        .then((value) => ({ value, error: null as string | null }))
        .catch((reason) => ({
          value: null,
          error: reason instanceof Error ? reason.message : "读取后台任务失败",
        }));
      const alertRequest = fetchAlerts()
        .then((value) => ({ value, error: null as string | null }))
        .catch((reason) => ({
          value: null,
          error: reason instanceof Error ? reason.message : "读取告警规则失败",
        }));
      const [loadedSettings, loadedStatus, pluginResult, schedulerResult, alertResult] = await Promise.all([
        fetchLlmSettings(),
        fetchLlmStatus(),
        pluginRequest,
        schedulerRequest,
        alertRequest,
      ]);
      setSettings(loadedSettings);
      setStatus(loadedStatus);
      setPlugins(pluginResult.value);
      setPluginLoadError(pluginResult.error);
      setScheduler(schedulerResult.value);
      setSchedulerError(schedulerResult.error);
      setAlerts(alertResult.value);
      setAlertError(alertResult.error);
      setRoleDraft(loadedSettings.roles);
      setLoadError(null);
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason.message : "读取设置失败");
    }
  }, []);

  const loadExternal = useCallback(async () => {
    try {
      setExternal(await fetchExternalStatus());
      setExternalError(null);
    } catch (reason) {
      setExternalError(reason instanceof Error ? reason.message : "读取外部服务状态失败");
    }
  }, []);

  useEffect(() => {
    void load();
    void loadExternal();
  }, [load, loadExternal]);

  const runAction = async (key: string, action: () => Promise<string | null>) => {
    setBusy(key);
    setNotice(null);
    try {
      setNotice(await action());
      await load();
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      setBusy(null);
    }
  };

  if (loadError) {
    return (
      <section className="coming-workspace">
        <IconAlertTriangle size={28} />
        <h2>设置不可用</h2>
        <p>{loadError}</p>
        <div className="coming-context">
          <span>需要</span>
          <strong>本地网关</strong>
          <span>请先运行 python -m quantdesk.cli serve</span>
        </div>
      </section>
    );
  }

  if (!settings) return <section className="coming-workspace"><p>正在读取模型设置…</p></section>;

  return (
    <div className="settings-workspace">
      <header className="settings-heading">
        <div>
          <h2>模型、数据与插件设置</h2>
          <p>
            密钥只写入 <code>{settings.keysFile}</code>（权限 600），浏览器只能读到「是否已配置」，读不到值。
          </p>
        </div>
        <div className={`settings-readiness ${status?.canAnalyzeCharts ? "ready" : "not-ready"}`}>
          {status?.canAnalyzeCharts ? <IconCheck size={15} /> : <IconAlertTriangle size={15} />}
          <span>
            图表识别：
            {status?.canAnalyzeCharts
              ? `已就绪（${status.visionProfile}）`
              : status?.visionProfile
                ? `未就绪（${status.visionProfile}）`
                : "无可用视觉模型"}
          </span>
        </div>
      </header>

      {!settings.homeWritable && (
        <p className="settings-notice settings-notice-warn" role="status">
          配置目录 <code>{settings.keysFile.replace(/keys\.env$/, "")}</code> 当前不可写，保存 Key 与切换角色会失败。
          请让运行网关的账号对该目录有写权限，或用 <code>QUANTDESK_HOME</code> 指向可写目录后重启网关。
        </p>
      )}
      {notice && <p className="settings-notice" role="status">{notice}</p>}
      {pluginLoadError && (
        <p className="settings-notice settings-notice-warn" role="status">
          插件管理暂不可用：{pluginLoadError}。模型与数据设置仍可正常使用。
        </p>
      )}
      {schedulerError && (
        <p className="settings-notice settings-notice-warn" role="status">后台任务状态不可用：{schedulerError}</p>
      )}
      {alertError && (
        <p className="settings-notice settings-notice-warn" role="status">告警规则暂不可用：{alertError}</p>
      )}

      <section className="settings-roles">
        <h3>能力映射</h3>
        <p>每个能力指向一个 profile，换供应商只需改这里，不用改配置文件。</p>
        <div className="settings-role-grid">
          {settings.knownRoles.map((role) => {
            const row = status?.roles.find((item) => item.role === role);
            const current = roleDraft[role] ?? "";
            return (
              <label key={role} className="settings-role">
                <span>
                  {settings.roleLabels[role] ?? role}
                  {row?.supportsVision && <em className="tag-vision">可读图</em>}
                  {row?.ready ? (
                    <em className="tag-ready">已配置</em>
                  ) : row?.credentialIssue ? (
                    <em className="tag-missing" title={row.credentialIssue}>Key 无效</em>
                  ) : (
                    <em className="tag-missing">缺 Key</em>
                  )}
                </span>
                <select
                  value={current}
                  onChange={(event) => {
                    const next = { ...roleDraft, [role]: event.target.value };
                    setRoleDraft(next);
                    void runAction(`role:${role}`, async () => {
                      await saveLlmRoles({ [role]: event.target.value });
                      return `已把「${settings.roleLabels[role] ?? role}」切到 ${event.target.value}`;
                    });
                  }}
                  disabled={busy !== null}
                >
                  <option value="">（未设置）</option>
                  {settings.profiles.map((profile) => (
                    <option key={profile.name} value={profile.name}>
                      {profile.name}{profile.supportsVision ? " · 可读图" : ""}
                    </option>
                  ))}
                </select>
              </label>
            );
          })}
        </div>
      </section>

      <section className="settings-profiles">
        <h3>供应商 Profile</h3>
        <div className="settings-profile-grid">
          {settings.profiles.map((profile) => (
            <ProfileCard
              key={profile.name}
              profile={profile}
              busy={busy}
              result={results[profile.name]}
              models={modelLists[profile.name]}
              draft={keyDrafts[profile.name] ?? ""}
              onDraft={(value) => setKeyDrafts((current) => ({ ...current, [profile.name]: value }))}
              modelDraft={modelDrafts[profile.name] ?? { ...profile.models, maxTokens: profile.maxTokens }}
              onModelDraft={(next) => setModelDrafts((current) => ({ ...current, [profile.name]: next }))}
              onSaveModels={(next) =>
                runAction(`models-cfg:${profile.name}`, async () => {
                  const result = await saveModelConfig(profile.name, {
                    deepModel: next.deep,
                    quickModel: next.quick,
                    visionModel: next.vision,
                    maxTokens: next.maxTokens,
                  });
                  setModelDrafts((current) => {
                    const copy = { ...current };
                    delete copy[profile.name];
                    return copy;
                  });
                  return `${profile.name} 模型名已保存：深度 ${result.models.deep || "—"}｜快速 ${result.models.quick || "—"}`;
                })
              }
              onSaveKey={() =>
                runAction(`key:${profile.name}`, async () => {
                  const value = (keyDrafts[profile.name] ?? "").trim();
                  if (!value) return "请先填入 Key";
                  await saveLlmKeys({ [profile.apiKeyEnv || profile.apiKeyCandidates[0]]: value });
                  setKeyDrafts((current) => ({ ...current, [profile.name]: "" }));
                  return `${profile.name} 的 Key 已写入 ${settings.keysFile}`;
                })
              }
              onClearKey={() =>
                runAction(`key:${profile.name}`, async () => {
                  await saveLlmKeys({ [profile.apiKeyEnv || profile.apiKeyCandidates[0]]: "" });
                  return `${profile.name} 的 Key 已清除`;
                })
              }
              onTest={() =>
                runAction(`test:${profile.name}`, async () => {
                  const result = await testLlmProfile(profile.name);
                  setResults((current) => ({ ...current, [profile.name]: result }));
                  if (result.ok) return `${profile.name} 连通正常（${result.model} · ${result.latencySeconds}s）`;
                  return `${profile.name}：${result.error?.title ?? "测试失败"}`;
                })
              }
              onModels={() =>
                runAction(`models:${profile.name}`, async () => {
                  const result = await listLlmModels(profile.name);
                  if (!result.ok) {
                    setResults((current) => ({ ...current, [profile.name]: { error: result.error?.detail ?? "拉取失败" } }));
                    return `${profile.name}：${result.error?.title ?? "拉取模型列表失败"}`;
                  }
                  setModelLists((current) => ({ ...current, [profile.name]: result.models }));
                  return `${profile.name} 返回 ${result.models.length} 个可用模型`;
                })
              }
            />
          ))}
        </div>
      </section>

      {scheduler && (
        <SchedulerSection
          value={scheduler}
          busy={busy}
          onRun={() => runAction("scheduler:market", async () => {
            const result = await runMarketCollection();
            return result ? `${result.symbol} 行情采集完成` : "行情采集完成";
          })}
          onSave={(config) => runAction("scheduler:settings", async () => {
            await saveSchedulerConfig(config);
            return "后台调度配置已保存，下一轮立即使用新设置";
          })}
        />
      )}

      {alerts && (
        <AlertsSection
          value={alerts}
          busy={busy}
          onCreate={(input) => runAction("alert:create", async () => {
            const rule = await createAlertRule(input);
            return `已创建告警「${rule.name}」`;
          })}
          onToggle={(rule) => runAction(`alert:toggle:${rule.id}`, async () => {
            const updated = await updateAlertRule(rule, { enabled: !rule.enabled });
            return `告警「${updated.name}」已${updated.enabled ? "启用" : "停用"}`;
          })}
          onDelete={(rule) => runAction(`alert:delete:${rule.id}`, async () => {
            await deleteAlertRule(rule.id);
            return `告警「${rule.name}」已删除`;
          })}
          onEvaluate={(symbol) => runAction(`alert:evaluate:${symbol}`, async () => {
            const result = await evaluateAlerts(symbol);
            return `检查 ${result.evaluated} 条规则，触发 ${result.triggered.length} 条`;
          })}
        />
      )}

      {externalError && (
        <p className="settings-notice settings-notice-warn" role="status">外部服务状态不可用：{externalError}</p>
      )}
      {external && (
        <ExternalSection
          status={external}
          busy={busy}
          onSave={(payload) =>
            runAction("external:save", async () => {
              await saveExternalSettings(payload);
              await loadExternal();
              return "外部研究与分析设置已保存";
            })
          }
          onTest={(capability, allowPaid) =>
            runAction("external:test", async () => {
              const result = await testExternalProvider({
                capability,
                allowPaid,
                symbol: "NVDAUSDT",
                topic: capability === "research_tool" ? "company_profile" : undefined,
              });
              return `${result.outcome}${result.note ? `（${result.note}）` : ""}`;
            })
          }
          onSaveKey={(name, value) =>
            runAction(`external:key:${name}`, async () => {
              const saved = await saveLlmKeys({ [name]: value });
              await loadExternal();
              return `已更新 ${saved.saved.join("、")}（值不回显）`;
            })
          }
        />
      )}

      {plugins && (
        <PluginsSection
          catalog={plugins}
          source={pluginSource}
          gitRef={pluginRef}
          busy={busy}
          health={pluginHealth}
          onSource={setPluginSource}
          onRef={setPluginRef}
          onInstall={() =>
            runAction("plugin:install", async () => {
              const source = pluginSource.trim();
              if (!source) return "请先填写 GitHub 仓库地址";
              const installed = await installPlugin(source, pluginRef);
              setPluginSource("");
              setPluginRef("");
              return `已安装 ${installed.name} ${installed.version}；默认未启用`;
            })
          }
          onToggle={(plugin) =>
            runAction(`plugin:toggle:${plugin.id}`, async () => {
              const updated = await setPluginEnabled(plugin.id, !plugin.enabled);
              return `${updated.name} 已${updated.enabled ? "启用" : "停用"}`;
            })
          }
          onHealth={(plugin) =>
            runAction(`plugin:health:${plugin.id}`, async () => {
              try {
                const result = await checkPlugin(plugin.id);
                setPluginHealth((current) => ({ ...current, [plugin.id]: result }));
                return `${plugin.name} 健康检查通过（${result.latencyMs}ms）`;
              } catch (reason) {
                const error = reason instanceof Error ? reason.message : "健康检查失败";
                setPluginHealth((current) => ({ ...current, [plugin.id]: { error } }));
                throw reason;
              }
            })
          }
          onUpdate={(plugin) =>
            runAction(`plugin:update:${plugin.id}`, async () => {
              const result = await updatePlugin(plugin.id, plugin.origin.ref);
              setPluginHealth((current) => { const next = { ...current }; delete next[plugin.id]; return next; });
              return `${plugin.name} 已更新到 ${result.plugin.version}，为便于复核已保持停用`;
            })
          }
          onDependencies={(plugin) =>
            runAction(`plugin:deps:${plugin.id}`, async () => {
              const result = await installPluginDependencies(plugin.id);
              return result.ready ? `${plugin.name} 的独立依赖环境已就绪` : result.problems.join("；");
            })
          }
          onUninstall={(plugin) =>
            runAction(`plugin:remove:${plugin.id}`, async () => {
              await uninstallPlugin(plugin.id, false);
              setPluginHealth((current) => { const next = { ...current }; delete next[plugin.id]; return next; });
              return `${plugin.name} 已卸载；插件私有数据已保留`;
            })
          }
        />
      )}
    </div>
  );
}

function AlertsSection({
  value,
  busy,
  onCreate,
  onToggle,
  onDelete,
  onEvaluate,
}: {
  value: AlertCatalog;
  busy: string | null;
  onCreate: (input: AlertRuleInput) => void;
  onToggle: (rule: AlertRule) => void;
  onDelete: (rule: AlertRule) => void;
  onEvaluate: (symbol: string) => void;
}) {
  const [name, setName] = useState("");
  const [symbol, setSymbol] = useState(value.instruments[0]?.venueSymbol ?? "BTCUSDT");
  const [conditions, setConditions] = useState<AlertConditionDraft[]>([
    { conditionType: "price_above", timeframe: "1h", threshold: "" },
  ]);
  const [cooldownMinutes, setCooldownMinutes] = useState(60);
  const [severity, setSeverity] = useState<AlertRuleInput["severity"]>("warning");
  const [confirmationCount, setConfirmationCount] = useState(1);
  const [hysteresis, setHysteresis] = useState(0);
  const [dailyLimit, setDailyLimit] = useState(10);
  const [quietEnabled, setQuietEnabled] = useState(false);
  const [quietStart, setQuietStart] = useState("23:00");
  const [quietEnd, setQuietEnd] = useState("07:00");
  const selectableConditions = value.conditions.filter((item) => !item.strategy);
  const conditionsValid = conditions.every((item) => item.threshold.trim() !== "" && Number.isFinite(Number(item.threshold)));
  const requiresSingleConfirmation = conditions.some((item) => ["price_cross_above", "price_cross_below"].includes(item.conditionType));
  const conditionLabel = (id: string) => value.conditions.find((item) => item.id === id)?.label ?? id;
  const displaySymbol = (venueSymbol: string) => value.instruments.find((item) => item.venueSymbol === venueSymbol)?.displaySymbol ?? venueSymbol;
  const describeCondition = (item: AlertRuleCondition) => item.conditionType === "strategy_signal"
    ? `${item.strategyId ?? "策略"} · ${item.timeframe} · ${item.signalDirection === "long" ? "做多" : item.signalDirection === "short" ? "做空" : "任意方向"}`
    : `${conditionLabel(item.conditionType)}${item.timeframe ? ` · ${item.timeframe}` : ""} · ${item.threshold}`;
  const updateCondition = (index: number, changes: Partial<AlertConditionDraft>) => setConditions((current) => current.map((item, position) => position === index ? { ...item, ...changes } : item));

  return (
    <section className="settings-alerts">
      <div className="settings-section-heading">
        <div>
          <h3>告警规则引擎</h3>
          <p>后台只检查已收盘数据；相同观测不会重复触发，冷却结束后等待下一条新数据。</p>
        </div>
        <span className={value.rules.some((item) => item.enabled) ? "tag-ready" : "tag-missing"}>
          {value.rules.filter((item) => item.enabled).length} 条启用
        </span>
      </div>

      <div className="alert-composer">
        <div className="alert-basics">
          <label>规则名称<input value={name} maxLength={80} placeholder="如 BTC 共振放量" onChange={(event) => setName(event.target.value)} /></label>
          <label>合约<select value={symbol} onChange={(event) => setSymbol(event.target.value)}>
            {value.instruments.map((item) => <option key={item.venueSymbol} value={item.venueSymbol}>{item.displaySymbol} · {item.name}</option>)}
          </select></label>
          <label>等级<select value={severity} onChange={(event) => setSeverity(event.target.value as AlertRuleInput["severity"])}>
            <option value="info">提醒</option><option value="warning">警告</option><option value="critical">严重</option>
          </select></label>
        </div>

        <div className="alert-condition-list">
          <div><strong>同时满足以下条件</strong><span>AND · 最多 5 条</span></div>
          {conditions.map((item, index) => {
            const catalog = value.conditions.find((entry) => entry.id === item.conditionType);
            return <div className="alert-condition-row" key={index}>
              <em>{index === 0 ? "IF" : "AND"}</em>
              <select value={item.conditionType} onChange={(event) => {
                updateCondition(index, { conditionType: event.target.value });
                if (["price_cross_above", "price_cross_below"].includes(event.target.value)) setConfirmationCount(1);
              }}>
                {selectableConditions.map((entry) => <option key={entry.id} value={entry.id}>{entry.label}</option>)}
              </select>
              <select value={item.timeframe} disabled={!catalog?.timeframe} onChange={(event) => updateCondition(index, { timeframe: event.target.value })}>
                {(["15m", "1h", "4h", "1d"] as const).map((frame) => <option key={frame}>{frame}</option>)}
              </select>
              <label><span>{catalog?.unit ?? "阈值"}</span><input type="number" step="any" value={item.threshold} placeholder="阈值" onChange={(event) => updateCondition(index, { threshold: event.target.value })} /></label>
              <button type="button" aria-label="移除条件" disabled={conditions.length === 1} onClick={() => setConditions((current) => current.filter((_, position) => position !== index))}><IconX size={14} /></button>
            </div>;
          })}
          <button type="button" className="alert-add-condition" disabled={conditions.length >= 5} onClick={() => setConditions((current) => [...current, { conditionType: "volume_ratio_above", timeframe: "1h", threshold: "" }])}><IconPlus size={13} />添加 AND 条件</button>
        </div>

        <div className="alert-policy-grid">
          <label title={requiresSingleConfirmation ? "上穿和下穿本身只发生在一个新观测点" : undefined}>连续确认（次）<input type="number" min={1} max={requiresSingleConfirmation ? 1 : 10} disabled={requiresSingleConfirmation} value={confirmationCount} onChange={(event) => setConfirmationCount(Number(event.target.value))} /></label>
          <label>迟滞回归值<input type="number" min={0} step="any" value={hysteresis} onChange={(event) => setHysteresis(Number(event.target.value))} /></label>
          <label>冷却（分钟）<input type="number" min={1} max={10080} value={cooldownMinutes} onChange={(event) => setCooldownMinutes(Number(event.target.value))} /></label>
          <label>每日上限<input type="number" min={1} max={1000} value={dailyLimit} onChange={(event) => setDailyLimit(Number(event.target.value))} /></label>
          <label className="alert-quiet-check"><input type="checkbox" checked={quietEnabled} onChange={(event) => setQuietEnabled(event.target.checked)} />静默时段</label>
          <label className={!quietEnabled ? "alert-field-muted" : ""}>开始<input type="time" disabled={!quietEnabled} value={quietStart} onChange={(event) => setQuietStart(event.target.value)} /></label>
          <label className={!quietEnabled ? "alert-field-muted" : ""}>结束<input type="time" disabled={!quietEnabled} value={quietEnd} onChange={(event) => setQuietEnd(event.target.value)} /></label>
        </div>

        <div className="alert-composer-actions">
          <button type="button" className="alert-create" disabled={busy !== null || !name.trim() || !conditionsValid || cooldownMinutes < 1} onClick={() => onCreate({
            name: name.trim(), venueSymbol: symbol,
            conditions: conditions.map((item) => ({
              conditionType: item.conditionType,
              timeframe: value.conditions.find((entry) => entry.id === item.conditionType)?.timeframe ? item.timeframe : null,
              threshold: Number(item.threshold),
            })),
            cooldownSeconds: Math.round(cooldownMinutes * 60), enabled: true, severity,
            quietStart: quietEnabled ? quietStart : null, quietEnd: quietEnabled ? quietEnd : null,
            timezone: "Asia/Shanghai", dailyLimit, confirmationCount, hysteresis,
          })}><IconBell size={14} />创建组合告警</button>
          <button type="button" className="alert-evaluate" disabled={busy !== null} onClick={() => onEvaluate(symbol)}><IconPlayerPlay size={14} />立即检查该合约</button>
        </div>
      </div>

      {value.rules.length === 0 ? (
        <div className="alert-empty"><IconBell size={22} /><span>尚无告警规则。创建后由后台行情轮转自动检查。</span></div>
      ) : (
        <div className="alert-rule-list">
          {value.rules.map((rule) => (
            <article key={rule.id} className={rule.enabled ? "alert-rule active" : "alert-rule"}>
              <div className="alert-rule-main">
                <span className="alert-rule-symbol">{displaySymbol(rule.venueSymbol)}</span>
                <div>
                  <strong>{rule.name}</strong>
                  <p>{rule.conditions.map(describeCondition).join(" AND ")}</p>
                  <small>{rule.severity === "critical" ? "严重" : rule.severity === "warning" ? "警告" : "提示"} · 每日最多 {rule.dailyLimit} 次{rule.quietStart ? ` · 静默 ${rule.quietStart}–${rule.quietEnd}` : ""}</small>
                </div>
              </div>
              <dl>
                <div><dt>状态</dt><dd>{rule.armed ? `${rule.consecutiveCount}/${rule.confirmationCount} 确认` : "等待迟滞回归"}</dd></div>
                <div><dt>最近触发</dt><dd>{rule.lastTriggeredAt ? new Date(rule.lastTriggeredAt).toLocaleString("zh-CN", { hour12: false }) : "尚未触发"}</dd></div>
              </dl>
              <div className="alert-rule-actions">
                <button type="button" onClick={() => onToggle(rule)} disabled={busy !== null}>{rule.enabled ? "停用" : "启用"}</button>
                <button type="button" className="plugin-remove" disabled={busy !== null} onClick={() => { if (window.confirm(`删除告警「${rule.name}」？历史触发记录会保留。`)) onDelete(rule); }}><IconTrash size={13} />删除</button>
              </div>
            </article>
          ))}
        </div>
      )}

      <div className="alert-history">
        <div><h4>最近触发</h4><span>{value.events.length} 条记录</span></div>
        {value.events.length === 0 ? <p>还没有触发记录</p> : value.events.slice(0, 20).map((event) => (
          <article key={event.id}>
            <time>{new Date(event.triggeredAt).toLocaleString("zh-CN", { hour12: false })}</time>
            <strong>{event.title}</strong>
            <span>{event.message}</span>
            <em>{event.notificationResults.length ? `${event.notificationResults.length} 个通知插件` : "仅已记录"}</em>
          </article>
        ))}
      </div>
    </section>
  );
}

function SchedulerSection({ value, busy, onRun, onSave }: { value: SchedulerStatus; busy: string | null; onRun: () => void; onSave: (config: SchedulerConfig) => void }) {
  const last = value.market.lastResult;
  const [draft, setDraft] = useState<SchedulerConfig>(value.config);
  useEffect(() => setDraft(value.config), [value.config]);
  return (
    <section className="settings-scheduler">
      <div className="settings-section-heading">
        <div>
          <h3>后台行情与任务调度</h3>
          <p>服务端逐个轮转固定合约池，无需保持浏览器开启。</p>
        </div>
        <span className={value.running ? "tag-ready" : "tag-missing"}>{value.running ? "运行中" : "未启动"}</span>
      </div>
      <div className="scheduler-grid">
        <div><span>行情采集</span><strong>{value.config.marketCollectionEnabled ? `每 ${value.config.marketSymbolIntervalSec}s 一个标的` : "已关闭"}</strong></div>
        <div><span>下一标的</span><strong>{value.nextMarketSymbol}</strong></div>
        <div><span>最近成功</span><strong>{value.market.lastSuccessAt ? new Date(value.market.lastSuccessAt).toLocaleString("zh-CN", { hour12: false }) : "尚无"}</strong></div>
        <div><span>日线多智能体</span><strong>{value.config.dailyTradingAgentsEnabled ? `${value.config.dailyTradingAgentsTime} UTC` : "默认关闭"}</strong></div>
      </div>
      {last && <p className="scheduler-last">{last.symbol} · K线 {Object.values(last.candles).reduce((sum, count) => sum + count, 0)} · 资金费 {last.funding} · OI {last.openInterest}{last.alerts ? ` · 告警 ${last.alerts.evaluated} 条 / 触发 ${last.alerts.triggered} 条 / 质量阻断 ${last.alerts.blocked} 条` : ""}</p>}
      {value.market.lastError && <p className="settings-error">{value.market.lastError}</p>}
      <div className="scheduler-controls">
        <label className="scheduler-check"><input type="checkbox" checked={draft.marketCollectionEnabled} onChange={(event) => setDraft((current) => ({ ...current, marketCollectionEnabled: event.target.checked }))} />启用后台行情轮转</label>
        <label>单标的间隔（秒）<input type="number" min="5" max="3600" value={draft.marketSymbolIntervalSec} onChange={(event) => setDraft((current) => ({ ...current, marketSymbolIntervalSec: Number(event.target.value) }))} /></label>
        <label>每周期缓存根数<input type="number" min="30" max="1000" value={draft.marketBackfillBars} onChange={(event) => setDraft((current) => ({ ...current, marketBackfillBars: Number(event.target.value) }))} /></label>
        <label className="scheduler-check"><input type="checkbox" checked={draft.dailyTradingAgentsEnabled} onChange={(event) => setDraft((current) => ({ ...current, dailyTradingAgentsEnabled: event.target.checked }))} />启用每日 TradingAgents（会调用付费模型）</label>
        <label>每日执行时间（UTC）<input type="time" value={draft.dailyTradingAgentsTime} onChange={(event) => setDraft((current) => ({ ...current, dailyTradingAgentsTime: event.target.value }))} /></label>
        <label>每日标的（逗号分隔）<input value={draft.dailyTradingAgentsSymbols.join(", ")} onChange={(event) => setDraft((current) => ({ ...current, dailyTradingAgentsSymbols: event.target.value.split(",").map((item) => item.trim().toUpperCase()).filter(Boolean) }))} /></label>
      </div>
      <div className="scheduler-actions">
        <button type="button" className="scheduler-run" onClick={() => onSave(draft)} disabled={busy !== null}>保存调度设置</button>
        <button type="button" className="scheduler-run" onClick={onRun} disabled={busy !== null || value.market.status === "running"}>
          <IconRefresh size={14} />立即采集 {value.nextMarketSymbol}
        </button>
      </div>
    </section>
  );
}

function PluginsSection({
  catalog,
  source,
  gitRef,
  busy,
  health,
  onSource,
  onRef,
  onInstall,
  onToggle,
  onHealth,
  onUpdate,
  onDependencies,
  onUninstall,
}: {
  catalog: PluginCatalog;
  source: string;
  gitRef: string;
  busy: string | null;
  health: Record<string, PluginHealth | { error: string }>;
  onSource: (value: string) => void;
  onRef: (value: string) => void;
  onInstall: () => void;
  onToggle: (plugin: PluginInfo) => void;
  onHealth: (plugin: PluginInfo) => void;
  onUpdate: (plugin: PluginInfo) => void;
  onDependencies: (plugin: PluginInfo) => void;
  onUninstall: (plugin: PluginInfo) => void;
}) {
  return (
    <section className="settings-plugins">
      <div className="settings-section-heading">
        <div>
          <h3>外部插件</h3>
          <p>从 GitHub 添加适配仓库。安装只拉取和校验清单，新插件默认停用。</p>
        </div>
        <div className="plugin-heading-status">
          <code>API v{catalog.apiVersion}</code>
          <span className={`plugin-sandbox ${catalog.sandbox.enforced ? "secure" : "degraded"}`} title={catalog.sandbox.detail}>
            <IconShieldCheck size={13} />
            {catalog.sandbox.enforced ? `系统隔离 · ${catalog.sandbox.backend}` : "系统隔离降级"}
          </span>
        </div>
      </div>

      <div className="plugin-install">
        <label>
          GitHub 仓库
          <span><IconBrandGithub size={14} /><input value={source} placeholder="https://github.com/OWNER/REPO" onChange={(event) => onSource(event.target.value)} /></span>
        </label>
        <label>
          分支或标签（可选）
          <input value={gitRef} placeholder="如 v1.2.0" onChange={(event) => onRef(event.target.value)} />
        </label>
        <button type="button" onClick={onInstall} disabled={busy !== null || !source.trim() || !catalog.homeWritable}>
          <IconPackage size={14} />安装并校验
        </button>
      </div>
      <p className="plugin-security">
        插件使用独立进程和私有依赖环境；系统隔离会限制用户文件访问、写入范围和网络。黄色“降级”表示当前主机未能启动操作系统沙箱。更新后自动停用，重新启用前会执行隔离健康检查。
      </p>

      {catalog.invalid.length > 0 && (
        <div className="plugin-invalid" role="alert">
          {catalog.invalid.map((item) => <p key={item.path}><strong>{item.id}</strong><span>{item.error}</span><code>{item.path}</code></p>)}
        </div>
      )}

      {catalog.plugins.length === 0 ? (
        <div className="plugin-empty">
          <IconPackage size={24} />
          <p>还没有安装插件</p>
          <code>{catalog.pluginRoot}</code>
        </div>
      ) : (
        <div className="plugin-grid">
          {catalog.plugins.map((plugin) => {
            const result = health[plugin.id];
            const failed = result && "error" in result;
            return (
              <article className={`plugin-card ${plugin.enabled ? "enabled" : ""}`} key={plugin.id}>
                <header>
                  <div><h4>{plugin.name}</h4><code>{plugin.id} · {plugin.version}</code></div>
                  <span className={plugin.enabled ? "plugin-state-on" : "plugin-state-off"}>{plugin.enabled ? "已启用" : "已停用"}</span>
                </header>
                <p>{plugin.description || "没有说明"}</p>
                <div className="plugin-capabilities">
                  {plugin.capabilities.map((item) => <span key={item}>{CAPABILITY_LABELS[item] ?? item}</span>)}
                </div>
                <div className={`plugin-sandbox ${plugin.sandbox.enforced ? "secure" : "degraded"}`} title={plugin.sandbox.detail}>
                  <IconShieldCheck size={13} />
                  {plugin.sandbox.enforced ? `沙箱已启用 · ${plugin.sandbox.backend}` : "沙箱降级"}
                </div>
                <dl>
                  <div><dt>来源</dt><dd>{plugin.origin.kind === "github" ? plugin.origin.source : plugin.origin.kind === "local" ? "本地安装" : "外部路径"}</dd></div>
                  <div><dt>网络声明</dt><dd>{plugin.network ? "需要网络" : "不需要网络"}</dd></div>
                  <div><dt>环境变量</dt><dd>{plugin.required_env.join(", ") || "无"}</dd></div>
                  <div><dt>Python依赖</dt><dd>{plugin.dependencies.requirementsFile || "无"}</dd></div>
                  <div><dt>依赖状态</dt><dd>{plugin.dependencies.ready ? "已就绪" : plugin.dependencies.problems.join("；")}</dd></div>
                </dl>
                {result && (
                  <p className={failed ? "plugin-health-failed" : "plugin-health-ok"}>
                    {failed ? result.error : `${result.result.message || "运行正常"} · ${result.latencyMs}ms`}
                  </p>
                )}
                <footer>
                  <button type="button" onClick={() => onToggle(plugin)} disabled={busy !== null}>{plugin.enabled ? "停用" : "启用"}</button>
                  <button type="button" onClick={() => onHealth(plugin)} disabled={busy !== null}><IconPlugConnected size={13} />健康检查</button>
                  {plugin.origin.kind === "github" && <button type="button" onClick={() => onUpdate(plugin)} disabled={busy !== null}><IconRefresh size={13} />更新</button>}
                  {plugin.dependencies.needsInstall && <button type="button" onClick={() => onDependencies(plugin)} disabled={busy !== null || plugin.enabled}><IconDownload size={13} />安装依赖</button>}
                  <button
                    type="button"
                    className="plugin-remove"
                    onClick={() => { if (window.confirm(`卸载 ${plugin.name}？插件代码和独立依赖会删除，私有数据会保留。`)) onUninstall(plugin); }}
                    disabled={busy !== null || plugin.enabled}
                  ><IconTrash size={13} />卸载</button>
                </footer>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function ProfileCard({
  profile,
  busy,
  result,
  models,
  draft,
  onDraft,
  modelDraft,
  onModelDraft,
  onSaveModels,
  onSaveKey,
  onClearKey,
  onTest,
  onModels,
}: {
  profile: LlmProfile;
  busy: string | null;
  result?: LlmTestResult | { error: string };
  models?: string[];
  draft: string;
  onDraft: (value: string) => void;
  modelDraft: { deep: string; quick: string; vision: string; maxTokens: number };
  onModelDraft: (next: { deep: string; quick: string; vision: string; maxTokens: number }) => void;
  onSaveModels: (next: { deep: string; quick: string; vision: string; maxTokens: number }) => void;
  onSaveKey: () => void;
  onClearKey: () => void;
  onTest: () => void;
  onModels: () => void;
}) {
  const envName = profile.apiKeyEnv || profile.apiKeyCandidates[0] || "";
  const isFailure = result !== undefined && !("ok" in result);
  const providerFailure = result !== undefined && "ok" in result && !result.ok ? result.error : undefined;

  return (
    <article className="settings-profile">
      <header>
        <div>
          <h4>{profile.name}</h4>
          <span className="settings-provider">{profile.provider}</span>
        </div>
        <span className={profile.hasKey ? "key-state key-state-ok" : "key-state key-state-missing"}>
          {profile.hasKey ? "Key 已配置" : profile.credentialIssue ? "Key 无效" : "缺 Key"}
        </span>
      </header>

      <dl className="settings-facts">
        <div><dt>端点</dt><dd>{profile.baseUrl || "—"}</dd></div>
        <div><dt>深度模型</dt><dd className={modelDraft.deep !== profile.models.deep ? "model-dirty" : undefined}>{modelDraft.deep || "—"}</dd></div>
        <div><dt>快速模型</dt><dd className={modelDraft.quick !== profile.models.quick ? "model-dirty" : undefined}>{modelDraft.quick || "—"}</dd></div>
        <div><dt>视觉模型</dt><dd className={modelDraft.vision !== profile.models.vision ? "model-dirty" : undefined}>{modelDraft.vision || "未声明"}</dd></div>
        <div><dt>代理</dt><dd>{profile.proxy || "直连"}</dd></div>
        <div><dt>能力</dt><dd>{[profile.supportsVision ? "可读图" : null, profile.supportsJsonMode ? "JSON 模式" : null].filter(Boolean).join(" · ") || "纯文本"}</dd></div>
      </dl>

      <div className="settings-models-edit">
        <span className="settings-models-edit-title">模型名与推理预算（保存后写入 llm.toml）</span>
        <div className="settings-model-fields">
          <label>
            深度
            <input value={modelDraft.deep} placeholder="如 deepseek-v4-pro"
              onChange={(event) => onModelDraft({ ...modelDraft, deep: event.target.value })} />
          </label>
          <label>
            快速
            <input value={modelDraft.quick} placeholder="留空表示不用"
              onChange={(event) => onModelDraft({ ...modelDraft, quick: event.target.value })} />
          </label>
          <label>
            视觉
            <input value={modelDraft.vision} placeholder="留空表示不支持读图"
              onChange={(event) => onModelDraft({ ...modelDraft, vision: event.target.value })} />
          </label>
          <label>
            最大 token（含推理）
            <input type="number" min={0} step={1000} value={modelDraft.maxTokens || ""} placeholder="默认 12000"
              onChange={(event) => onModelDraft({ ...modelDraft, maxTokens: Number(event.target.value) || 0 })} />
          </label>
        </div>
        {(modelDraft.deep !== profile.models.deep ||
          modelDraft.quick !== profile.models.quick ||
          modelDraft.vision !== profile.models.vision ||
          modelDraft.maxTokens !== profile.maxTokens) && (
          <button type="button" className="settings-save-models" onClick={() => onSaveModels(modelDraft)} disabled={busy !== null}>
            保存模型名
          </button>
        )}
        {models && models.length > 0 && (
          <div className="settings-model-picks">
            <span>点击填入深度模型</span>
            <div>
              {models.slice(0, 30).map((model) => (
                <button key={model} type="button" onClick={() => onModelDraft({ ...modelDraft, deep: model })}>{model}</button>
              ))}
            </div>
          </div>
        )}
      </div>

      <label className="settings-key">
        <span>写入 {envName}</span>
        <input
          type="password"
          value={draft}
          placeholder={profile.hasKey ? "已配置；填入新值可覆盖" : "粘贴 API Key"}
          autoComplete="off"
          onChange={(event) => onDraft(event.target.value)}
        />
      </label>

      <div className="settings-actions">
        <button type="button" onClick={onSaveKey} disabled={busy !== null || !draft.trim()}>保存 Key</button>
        <button type="button" onClick={onTest} disabled={busy !== null || !profile.hasKey}>
          <IconPlugConnected size={14} />测试连通
        </button>
        <button type="button" onClick={onModels} disabled={busy !== null || !profile.hasKey}>
          <IconRobot size={14} />拉取模型
        </button>
        {profile.hasKey && <button type="button" className="danger" onClick={onClearKey} disabled={busy !== null}>清除</button>}
      </div>

      {profile.credentialIssue && (
        <p className="settings-error" role="alert">
          <strong>已保存的 {envName} 不可用</strong>
          <span>{profile.credentialIssue}</span>
          <em>请粘贴真实密钥后重新保存；占位符（如 sk-...）会被上游拒绝并报成鉴权失败。</em>
        </p>
      )}

      {profile.provider === "openai" && !profile.proxy && (
        <p className="settings-hint settings-hint-warn">
          <IconWorld size={13} />本机实测 api.openai.com 直连不通，需要在 <code>llm.toml</code> 为该 profile 配置 <code>proxy</code>。
        </p>
      )}

      {isFailure && <p className="settings-error" role="alert">{(result as { error: string }).error}</p>}
      {providerFailure && (
        <div className="settings-error" role="alert">
          <strong>{providerFailure.title}</strong>
          <span>{providerFailure.detail}</span>
          <em>{providerFailure.action}</em>
        </div>
      )}
      {result && "ok" in result && result.ok && !result.substituted && (
        <p className="settings-ok"><IconCheck size={13} />{result.model} · {result.latencySeconds}s · 回复「{result.reply}」</p>
      )}
      {result && "ok" in result && result.ok && result.substituted && (
        <div className="settings-error" role="alert">
          <strong>模型名未生效</strong>
          <span>{result.warning}</span>
          <em>把「深度模型」改成上面列表里的名称并保存，再重新测试。</em>
        </div>
      )}

      {models && models.length > 0 && (
        <div className="settings-models">
          <span>可用模型（点击复制到上方模型名需改 llm.toml）</span>
          <div>{models.slice(0, 40).map((model) => <code key={model}>{model}</code>)}</div>
        </div>
      )}
    </article>
  );
}

export function RefreshHint() {
  return <IconRefresh size={14} />;
}

/** OpenBB research and Fincept analytics: switches, providers, health, licences. */
function ExternalSection({
  status,
  busy,
  onSave,
  onTest,
  onSaveKey,
}: {
  status: ExternalStatus;
  busy: string | null;
  onSave: (payload: Parameters<typeof saveExternalSettings>[0]) => void;
  onTest: (capability: "research_tool" | "analytics", allowPaid: boolean) => void;
  onSaveKey: (name: string, value: string) => void;
}) {
  const [draft, setDraft] = useState(() => ({
    openbbEnabled: status.external.openbbEnabled,
    finceptEnabled: status.external.finceptEnabled,
    requestsPerMinute: status.external.requestsPerMinute ?? 60,
    timeoutSeconds: status.external.timeoutSeconds ?? 30,
    maxRetries: status.external.maxRetries ?? 2,
  }));
  const [providers, setProviders] = useState<Record<string, string>>(() => ({ ...status.providers.openbb }));
  const [ttl, setTtl] = useState<Record<string, number>>(() => ({ ...status.external.cacheTtlMinutes }));
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});

  useEffect(() => {
    setDraft({
      openbbEnabled: status.external.openbbEnabled,
      finceptEnabled: status.external.finceptEnabled,
      requestsPerMinute: status.external.requestsPerMinute ?? 60,
      timeoutSeconds: status.external.timeoutSeconds ?? 30,
      maxRetries: status.external.maxRetries ?? 2,
    });
    setProviders({ ...status.providers.openbb });
    setTtl({ ...status.external.cacheTtlMinutes });
  }, [status]);

  const research = status.plugins.research;
  const analytics = status.plugins.analytics;
  const keyNames = ["OPENBB_FRED_API_KEY", "OPENBB_FMP_API_KEY", "OPENBB_BENZINGA_API_KEY", "OPENBB_POLYGON_API_KEY", "OPENBB_API_URL", "FINCEPT_API_KEY", "FINCEPT_API_URL"];

  return (
    <section className="settings-external">
      <div className="settings-section-heading">
        <h3>外部研究与分析</h3>
        <span>OpenBB 提供基本面、新闻与宏观证据；Fincept 只做组合风险与压力测试。两者都关掉时 QuantDesk 功能完整。</span>
      </div>

      <div className="external-switches">
        <label className="external-switch">
          <input
            type="checkbox"
            checked={draft.openbbEnabled}
            onChange={(event) => setDraft({ ...draft, openbbEnabled: event.target.checked })}
          />
          <span>启用 OpenBB 研究</span>
          <em>{research.pluginId ? `插件 ${research.pluginId}` : "未安装研究插件"}</em>
        </label>
        <label className="external-switch">
          <input
            type="checkbox"
            checked={draft.finceptEnabled}
            onChange={(event) => setDraft({ ...draft, finceptEnabled: event.target.checked })}
          />
          <span>启用 Fincept 组合分析</span>
          <em>{analytics.pluginId ? `插件 ${analytics.pluginId}` : "未安装分析插件"}</em>
        </label>
      </div>

      <div className="external-limits">
        <label>
          限速（次/分钟）
          <input
            type="number"
            min={1}
            value={draft.requestsPerMinute}
            onChange={(event) => setDraft({ ...draft, requestsPerMinute: Number(event.target.value) })}
          />
        </label>
        <label>
          超时（秒）
          <input
            type="number"
            min={1}
            value={draft.timeoutSeconds}
            onChange={(event) => setDraft({ ...draft, timeoutSeconds: Number(event.target.value) })}
          />
        </label>
        <label>
          429 重试上限
          <input
            type="number"
            min={0}
            value={draft.maxRetries}
            onChange={(event) => setDraft({ ...draft, maxRetries: Number(event.target.value) })}
          />
        </label>
        <button
          type="button"
          disabled={busy === "external:save"}
          onClick={() =>
            onSave({
              ...draft,
              providers,
              cacheTtlMinutes: Object.fromEntries(Object.entries(ttl).map(([key, value]) => [key, Number(value)])),
            })
          }
        >
          保存外部设置
        </button>
      </div>

      <div className="external-grid">
        <div>
          <h4>Provider 覆盖矩阵</h4>
          <table className="external-table">
            <thead>
              <tr><th>接口</th><th>Provider</th><th>缓存（分钟）</th></tr>
            </thead>
            <tbody>
              {Object.entries(providers).map(([topic, provider]) => (
                <tr key={topic}>
                  <td>{topic}</td>
                  <td>
                    <select value={provider} onChange={(event) => setProviders({ ...providers, [topic]: event.target.value })}>
                      {["yfinance", "sec", "fred", "finra", "benzinga", "polygon", "fmp"].map((name) => (
                        <option key={name} value={name}>{name}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      type="number"
                      min={0}
                      value={ttl[topic] ?? 0}
                      onChange={(event) => setTtl({ ...ttl, [topic]: Number(event.target.value) })}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="external-note">
            时点校验：{status.providers.pointInTime.default === false ? "默认关闭" : "默认开启"}
            {status.providers.pointInTime.exempt?.length ? `（豁免：${status.providers.pointInTime.exempt.join("、")}）` : ""}
          </p>
        </div>

        <div>
          <h4>连通性与健康</h4>
          <ul className="external-health">
            <li>
              <span>OpenBB 插件</span>
              <strong className={research.healthy ? "confidence-high" : "confidence-low"}>
                {research.healthy == null ? "未启用" : research.healthy ? "健康" : "不可用"}
              </strong>
            </li>
            <li>
              <span>OpenBB 运行环境</span>
              <strong>{research.runtime?.runtimeReady ? research.runtime.transport ?? "就绪" : research.runtime?.note ?? "未就绪"}</strong>
            </li>
            <li>
              <span>Fincept 插件</span>
              <strong className={analytics.healthy ? "confidence-high" : "confidence-low"}>
                {analytics.healthy == null ? "未启用" : analytics.healthy ? "健康" : "不可用"}
              </strong>
            </li>
            <li>
              <span>Fincept 凭据</span>
              <strong>{analytics.runtime?.credentialPresent ? "已配置" : "未配置"}</strong>
            </li>
            <li>
              <span>外部证据缓存</span>
              <strong>{status.evidence.rows} 条{status.evidence.cacheHitRate != null ? ` · 命中率 ${(status.evidence.cacheHitRate * 100).toFixed(0)}%` : ""}</strong>
            </li>
            <li>
              <span>分析调用</span>
              <strong>
                {status.analytics.calls} 次
                {status.analytics.rate.averageMs != null ? ` · 平均 ${status.analytics.rate.averageMs}ms` : ""}
              </strong>
            </li>
            <li>
              <span>最近错误</span>
              <strong className={status.analytics.rate.lastError ? "confidence-low" : ""}>
                {status.analytics.rate.lastError?.error ?? "无"}
              </strong>
            </li>
          </ul>
          <div className="external-actions">
            <button type="button" disabled={busy === "external:test"} onClick={() => onTest("research_tool", false)}>
              测试 OpenBB（免费检查）
            </button>
            <button type="button" disabled={busy === "external:test"} onClick={() => onTest("analytics", false)}>
              测试 Fincept（免费检查）
            </button>
            <button
              type="button"
              className="external-paid"
              disabled={busy === "external:test"}
              onClick={() => onTest("research_tool", true)}
            >
              允许一次付费探测
            </button>
          </div>
        </div>
      </div>

      <details className="external-keys">
        <summary>API Key（只覆盖写入，不回显）</summary>
        <div className="external-key-grid">
          {keyNames.map((name) => (
            <label key={name}>
              <span>{name}</span>
              <input
                type="password"
                placeholder="留空表示不修改"
                value={keyDrafts[name] ?? ""}
                onChange={(event) => setKeyDrafts({ ...keyDrafts, [name]: event.target.value })}
              />
              <button
                type="button"
                disabled={busy === `external:key:${name}` || !(keyDrafts[name] ?? "").trim()}
                onClick={() => {
                  onSaveKey(name, (keyDrafts[name] ?? "").trim());
                  setKeyDrafts({ ...keyDrafts, [name]: "" });
                }}
              >
                保存
              </button>
            </label>
          ))}
        </div>
      </details>

      <div className="external-licences">
        {Object.values(status.licences).map((item) => (
          <p key={item.name}>
            <strong>{item.name}</strong>
            <span>{item.licence}</span>
            <em>{item.mode}；{item.note}</em>
          </p>
        ))}
      </div>
    </section>
  );
}
