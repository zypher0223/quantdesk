import { useCallback, useEffect, useRef, useState } from "react";
import {
  IconAlertTriangle, IconBrain, IconCheck, IconDownload, IconPlayerPlay,
  IconPlayerStop, IconPlus, IconRefresh, IconTrash, IconX,
} from "@tabler/icons-react";
import { compactUsdt, formatPrice, relativeAge } from "../lib/format";
import { fetchInstrumentIndex } from "../services/api";
import {
  aiPaperExportUrl, closeAiPaperPosition, createAiPaper, deleteAiPaper, fetchAiPaper,
  fetchAiPaperProfiles, resetAiPaper, runAiPaperNow, saveAiPaperConfig, startAiPaper,
  stopAiPaper, type AiPaperHorizon, type AiPaperProfile, type AiPaperProfileSummary,
  type AiPaperSnapshot, type AiPaperStyle, type AiSimulationCondition,
} from "../services/ai-paper";
import {
  HORIZON_LABEL, STYLE_LABEL, configPayload, draftFromProfile, draftReady,
  isDraftDirty, pendingSummary, receiptMismatch, reconcileDraft, responseIsCurrent,
  revisionLabel, runningSummary, type AiPaperDraft,
} from "../services/ai-paper-draft";
import type { PositionView } from "../services/paper";
import type { Instrument } from "../data/market";

const STYLE_COPY: Record<AiPaperStyle, { name: string; summary: string }> = {
  conservative: { name: STYLE_LABEL.conservative, summary: "单笔风险 0.5% · 最高 3x · 置信度 ≥72%" },
  aggressive: { name: STYLE_LABEL.aggressive, summary: "单笔风险 1.5% · 最高 10x · 置信度 ≥58%" },
  gambler: { name: STYLE_LABEL.gambler, summary: "单笔风险 4% · 使用用户杠杆上限 · 置信度 ≥45%" },
};

const STATUS_LABEL: Record<string, string> = {
  executed: "已执行", held: "观望", rejected: "风控拒绝", failed: "调用失败",
};

const messageOf = (reason: unknown, fallback: string) =>
  reason instanceof Error ? reason.message : fallback;

/**
 * Which rule revision a position was opened under, and how many contracts it covered.
 *
 * Read only from the position's own condition block. A position logged before the
 * engine recorded revisions keeps an unknown one ("修订 —") rather than borrowing the
 * profile's *current* revision, which would state as fact something the row does not
 * say: the profile may have been re-configured since that position was opened.
 */
function positionCondition(simulation: AiSimulationCondition | undefined): string {
  const count = simulation?.symbols?.length ?? 0;
  const revision = revisionLabel(simulation?.configRevision);
  return count > 0 ? `规则 ${revision} · ${count} 个合约` : `规则 ${revision}`;
}

/** The rules a logged decision was taken under, when the engine recorded them. */
function decisionCondition(simulation: AiSimulationCondition): string {
  const count = simulation.symbols?.length ?? 0;
  return [
    simulation.styleLabel, simulation.horizonLabel,
    count > 0 ? `${count} 个合约` : null, revisionLabel(simulation.configRevision),
  ].filter(Boolean).join(" · ");
}

export function AiPaperWorkspace() {
  // Two sources of truth, on purpose: `snapshot` is what the engine reported, `draft`
  // is what the operator is editing. Nothing but an explicit save writes the draft to
  // the engine, and nothing but an explicit adopt writes the server into the draft.
  const [snapshot, setSnapshot] = useState<AiPaperSnapshot | null>(null);
  const [draft, setDraft] = useState<AiPaperDraft | null>(null);
  const [profiles, setProfiles] = useState<AiPaperProfileSummary[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState("default");
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [memoryOpen, setMemoryOpen] = useState(false);

  // Staleness guards. A response is applied only if no newer request has started and
  // the instance it belongs to is still the selected one; the id lives in a ref so the
  // check is made against the value at *response* time, not at render time.
  const snapshotSeq = useRef(0);
  const indexSeq = useRef(0);
  const selectedRef = useRef(selectedProfileId);
  /** The server profile the draft was last reconciled against, for the dirty test. */
  const adoptedRef = useRef<AiPaperProfile | null>(null);
  const selectProfile = useCallback((id: string) => {
    selectedRef.current = id;
    setSelectedProfileId(id);
  }, []);

  /** A write is about to happen: any read already in flight is now out of date. */
  const invalidatePending = useCallback(() => { snapshotSeq.current += 1; }, []);

  const adoptSnapshot = useCallback((next: AiPaperSnapshot, options?: { keepDraft?: boolean }) => {
    const baseline = adoptedRef.current;
    adoptedRef.current = next.profile;
    setSnapshot(next);
    setDraft((current) => (
      options?.keepDraft && current && baseline
        ? reconcileDraft(current, next.profile, baseline)
        : draftFromProfile(next.profile)
    ));
  }, []);

  /** Replace only the profile inside the snapshot, for calls that answer with it. */
  const applyProfile = useCallback((profile: AiPaperProfile) => {
    // The baseline moves with it: the draft is about to be rebuilt from this profile,
    // and judging that draft against the previous one would invent unsaved edits.
    adoptedRef.current = profile;
    setSnapshot((previous) => (previous ? { ...previous, profile } : previous));
  }, []);

  const loadProfiles = useCallback(async () => {
    const sequence = ++indexSeq.current;
    try {
      const index = await fetchAiPaperProfiles();
      if (sequence !== indexSeq.current) return; // a newer index read supersedes this one
      setProfiles(index.profiles);
    } catch {
      /* Keep the tabs from the last good read; the snapshot error is the one shown. */
    }
  }, []);

  const load = useCallback(async (
    quiet = false,
    profileId = selectedRef.current,
    options?: { resetDraft?: boolean },
  ) => {
    const sequence = ++snapshotSeq.current;
    if (!quiet) setBusy("refresh");
    try {
      const [next] = await Promise.all([fetchAiPaper(profileId), loadProfiles()]);
      if (!responseIsCurrent(sequence, snapshotSeq.current, profileId, selectedRef.current)) return;
      // `keepDraft` is what makes a background refresh safe: reconcile keeps unsaved
      // edits and only re-syncs a draft that has nothing unsaved in it.
      adoptSnapshot(next, { keepDraft: !options?.resetDraft });
      setError(null);
    } catch (reason) {
      if (sequence !== snapshotSeq.current) return;
      setError(messageOf(reason, "读取 AI 模拟账户失败"));
    } finally {
      if (!quiet && sequence === snapshotSeq.current) setBusy(null);
    }
  }, [adoptSnapshot, loadProfiles]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    let active = true;
    void fetchInstrumentIndex().then((index) => {
      if (active) setInstruments(index.instruments);
    }).catch(() => {
      /* The saved profile still renders as a usable fallback. */
    });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!profiles.some((item) => item.profile.enabled)) return;
    const timer = window.setInterval(() => void load(true), 15_000);
    return () => window.clearInterval(timer);
  }, [load, profiles]);

  function update<K extends keyof AiPaperDraft>(key: K, value: AiPaperDraft[K]) {
    setDraft((current) => (current ? { ...current, [key]: value } : current));
  }

  async function act(label: string, action: () => Promise<unknown>, success: string) {
    invalidatePending();
    setBusy(label); setError(null); setNotice(null);
    try {
      await action();
      setNotice(success);
      await load(true);
    } catch (reason) {
      setError(messageOf(reason, "操作失败"));
    } finally { setBusy(null); }
  }

  /** Standalone save. The engine's receipt is checked here too: reporting "saved" for
   *  rules the engine did not store is the same lie in a smaller place. */
  async function saveDraft() {
    if (!snapshot || !draft) return;
    const payload = configPayload(draft);
    invalidatePending();
    setBusy("save"); setError(null); setNotice(null);
    try {
      const saved = await saveAiPaperConfig(snapshot.profile.id, payload);
      const mismatches = receiptMismatch(payload, saved);
      applyProfile(saved);
      if (mismatches.length > 0) {
        // The stored profile is the truth; the draft stays as typed so the difference
        // is visible and fixable rather than silently adopted.
        setError(`规则未按页面保存：${mismatches.join("；")}。草稿已保留，请修正后重试。`);
        return;
      }
      setDraft(draftFromProfile(saved));
      setNotice("AI 模拟规则已保存");
    } catch (reason) {
      setError(messageOf(reason, "保存规则失败"));
    } finally { setBusy(null); }
  }

  /**
   * Stop a start that cannot be confirmed, and say why.
   *
   * The engine ran - or may have run - something the page cannot vouch for, so the only
   * safe end state is stopped. The draft is kept so the operator still has their rules.
   */
  async function abortStart(profileId: string, reasons: string[]) {
    let stopped = true;
    try { await stopAiPaper(profileId); } catch { stopped = false; }
    setError(
      `启动已中止：${reasons.join("；")}。`
      + (stopped
        ? "自动模拟已停止，请修正后重试。"
        : "但停止调用失败，请立即手动停止该模拟。"),
    );
    await load(true, profileId);
  }

  /**
   * Save the draft and start in one call, then verify the receipt.
   *
   * A start that cannot be confirmed is aborted rather than reported: the engine would
   * otherwise run rules the operator never saw.
   */
  async function saveAndStart() {
    if (!snapshot || !draft) return;
    const payload = configPayload(draft);
    const profileId = snapshot.profile.id;
    invalidatePending();
    setBusy("start"); setError(null); setNotice(null);
    try {
      const result = await startAiPaper(profileId, payload);
      // The type says a payload call answers with a snapshot; checked anyway, because a
      // front-end built ahead of the engine would get the legacy profile-only shape and
      // a save-then-start whose result nobody verified is exactly the bug being fixed.
      const confirmed = (result as { profile?: AiPaperProfile } | null)?.profile;
      if (!confirmed) {
        await abortStart(profileId, ["引擎未返回启动后的完整配置（引擎可能尚未更新）"]);
        return;
      }
      const mismatches = receiptMismatch(payload, confirmed);
      if (mismatches.length > 0) {
        adoptSnapshot(result, { keepDraft: true });
        await abortStart(confirmed.id ?? profileId, mismatches);
        return;
      }
      adoptSnapshot(result);
      setNotice("规则已保存并启动；将在新收盘 K 线后评估");
      await load(true, profileId);
    } catch (reason) {
      setError(messageOf(reason, "启动自动模拟失败"));
    } finally { setBusy(null); }
  }

  /** Stopped + unsaved edits: save first, and only call the model if the save landed. */
  async function saveAndEvaluate() {
    if (!snapshot || !draft) return;
    const payload = configPayload(draft);
    invalidatePending();
    setBusy("run"); setError(null); setNotice(null);
    try {
      const saved = await saveAiPaperConfig(snapshot.profile.id, payload);
      const mismatches = receiptMismatch(payload, saved);
      applyProfile(saved);
      if (mismatches.length > 0) {
        setError(`未调用模型：规则未按页面保存 —— ${mismatches.join("；")}。`);
        return;
      }
      setDraft(draftFromProfile(saved));
      await runAiPaperNow(saved.id);
      setNotice("规则已保存，模型已完成一轮评估");
      await load(true, saved.id);
    } catch (reason) {
      setError(messageOf(reason, "保存并评估失败"));
    } finally { setBusy(null); }
  }

  async function createSimulation() {
    if (!draft) return;
    const cleanName = newName.trim();
    if (!cleanName) { setError("请填写模拟名称"); return; }
    invalidatePending();
    setBusy("create"); setError(null); setNotice(null);
    try {
      const created = await createAiPaper({ ...configPayload(draft), name: cleanName });
      selectProfile(created.profile.id);
      adoptSnapshot(created);
      await loadProfiles();
      setCreateOpen(false); setNewName("");
      setNotice(`已创建独立模拟「${created.profile.name}」；原有模拟继续运行`);
    } catch (reason) {
      setError(messageOf(reason, "创建模拟失败"));
    } finally { setBusy(null); }
  }

  async function selectSimulation(profileId: string) {
    if (profileId === selectedRef.current) return;
    // Point the ref at the new instance *before* awaiting: any read still in flight
    // belongs to the previous one and must not be applied.
    selectProfile(profileId);
    setError(null); setNotice(null);
    await load(false, profileId, { resetDraft: true });
  }

  async function removeSimulation() {
    const removing = snapshot?.profile;
    if (!removing) return;
    invalidatePending();
    setBusy("delete"); setError(null); setNotice(null);
    try {
      await deleteAiPaper(removing.id);
      selectProfile("default");
      await load(false, "default", { resetDraft: true });
      setNotice(`独立模拟「${removing.name}」已删除`);
    } catch (reason) {
      setError(messageOf(reason, "删除模拟失败"));
    } finally { setBusy(null); }
  }

  if (!snapshot && error) {
    return <section className="coming-workspace"><IconAlertTriangle size={28} /><h2>AI 模拟交易不可用</h2><p>{error}</p></section>;
  }
  if (!snapshot || !draft) return <div className="ai-paper-loading">正在读取独立模拟账户…</div>;

  const { account, metrics, decisions, journal, policy, feePolicy } = snapshot;
  const profile = snapshot.profile;
  // Comparison, not a third copy of the state: the badge and the save button cannot
  // drift out of step with the values they describe.
  const draftDirty = isDraftDirty(draft, profile);
  const ready = draftReady(draft);
  const effectiveLeverage = Math.min(profile.max_leverage, policy.max_leverage);
  const availableInstruments = instruments.length > 0 ? instruments : profile.symbols.map((symbol) => ({
    displaySymbol: symbol.replace(/STOCKUSDT$|USDT$/u, ""), venueSymbol: symbol,
    name: symbol, group: "", productType: (["BTCUSDT", "ETHUSDT"].includes(symbol) ? "crypto" : "stock") as Instrument["productType"],
    productLabel: "", riskClass: "standard" as const, chartInterval: "", underlyingSymbol: null,
    pool: (["BTCUSDT", "ETHUSDT"].includes(symbol) ? "crypto" : "stock") as Instrument["pool"], symbolMapped: false,
  }));
  const stockPool = availableInstruments.filter((item) => item.pool === "stock");
  const cryptoPool = availableInstruments.filter((item) => item.pool === "crypto");
  const selectedSet = new Set(draft.symbols);
  const selectedStockCount = stockPool.filter((item) => selectedSet.has(item.venueSymbol)).length;
  const selectedCryptoCount = cryptoPool.filter((item) => selectedSet.has(item.venueSymbol)).length;

  function setPool(pool: Instrument[], selected: boolean) {
    const poolSymbols = new Set(pool.map((item) => item.venueSymbol));
    setDraft((current) => (current ? {
      ...current,
      symbols: selected
        ? [...new Set([...current.symbols, ...poolSymbols])]
        : current.symbols.filter((symbol) => !poolSymbols.has(symbol)),
    } : current));
  }

  function toggleSymbol(symbol: string) {
    setDraft((current) => (current ? {
      ...current,
      symbols: current.symbols.includes(symbol)
        ? current.symbols.filter((item) => item !== symbol)
        : [...current.symbols, symbol],
    } : current));
  }

  return (
    <div className="ai-paper">
      <section className="ai-simulation-switcher" aria-label="AI 模拟实例">
        <header><div><span>独立模拟实例</span><strong>{profiles.length}</strong></div><button type="button" onClick={() => { setNewName(`模拟 ${profiles.length + 1}`); setCreateOpen(true); }}><IconPlus size={13} />新增模拟</button></header>
        <div className="ai-simulation-tabs">{profiles.map((item) => (
          <button type="button" key={item.profile.id} className={item.profile.id === profile.id ? "active" : ""} onClick={() => void selectSimulation(item.profile.id)}>
            <span><i className={item.profile.enabled ? "running" : ""} />{item.profile.name}</span>
            <strong>{item.metrics.returnPct >= 0 ? "+" : ""}{item.metrics.returnPct.toFixed(2)}%</strong>
            <small>{STYLE_COPY[item.profile.style].name} · {HORIZON_LABEL[item.profile.horizon]} · {item.profile.fib_only ? "Fib" : "多 Agent"} · {item.openPositions} 仓 · {revisionLabel(item.profile.config_revision)}</small>
          </button>
        ))}</div>
        {createOpen && <div className="ai-simulation-create">
          <div><strong>新增独立模拟</strong><span>复制当前页面中的条件；本金、持仓、统计、日志和记忆完全独立。</span></div>
          <label>模拟名称<input autoFocus maxLength={40} value={newName} onChange={(event) => setNewName(event.target.value)} /></label>
          <button type="button" onClick={() => void createSimulation()} disabled={Boolean(busy) || !newName.trim() || !ready}>创建</button>
          <button type="button" className="secondary" onClick={() => setCreateOpen(false)} disabled={Boolean(busy)}>取消</button>
        </div>}
      </section>
      <section className="ai-paper-hero">
        <div>
          <span className="ai-paper-eyebrow"><IconBrain size={14} /> AI AUTO PAPER</span>
          <h3>{profile.name}</h3>
          <p>从股票池和加密池选择一个或多个合约。决策模型会通过受控 Agent Council 协作读取技术、衍生品、CPA、TradingAgents、回测与因子证据，再经过本地风控进入模拟撮合。</p>
        </div>
        <div className={`ai-paper-state ${profile.enabled ? "running" : "stopped"}`}>
          <strong>{profile.enabled ? "自动运行中" : "已停止"}</strong>
          <span>仅模拟 · 无实盘接口</span>
          <small>{profile.last_cycle_ts ? `上次评估 ${relativeAge(profile.last_cycle_ts)}` : "尚未评估"}</small>
        </div>
      </section>

      <div className="ai-paper-grid">
        <aside className="ai-paper-config">
          <header>
            <h4>运行规则</h4>
            <span>{draft.symbols.length} 个已选合约 · {revisionLabel(profile.config_revision)}</span>
          </header>
          {draftDirty && (
            <p className="ai-paper-dirty-badge" role="status">
              <IconAlertTriangle size={12} />有未保存的规则修改
            </p>
          )}
          <label>模拟名称<input maxLength={40} value={draft.name} disabled={profile.enabled} onChange={(e) => update("name", e.target.value)} /></label>
          <label>初始本金（USDT）<input type="number" min="100" value={draft.initialCash} disabled={profile.enabled} onChange={(e) => update("initialCash", e.target.value)} /></label>
          <label>用户最大杠杆<input type="number" min="1" max="200" value={draft.maxLeverage} disabled={profile.enabled} onChange={(e) => update("maxLeverage", e.target.value)} /></label>
          <fieldset className="ai-paper-horizon" disabled={profile.enabled}>
            <legend>交易周期</legend>
            <button type="button" className={draft.horizon === "short" ? "selected" : ""} onClick={() => update("horizon", "short")}><strong>短线</strong><span>15m / 1h / 4h</span></button>
            <button type="button" className={draft.horizon === "swing" ? "selected" : ""} onClick={() => update("horizon", "swing")}><strong>中长线</strong><span>4h / 日线</span></button>
          </fieldset>
          <fieldset className="ai-paper-styles" disabled={profile.enabled}>
            <legend>交易风格</legend>
            {(Object.keys(STYLE_COPY) as AiPaperStyle[]).map((key) => (
              <button type="button" key={key} className={`${draft.style === key ? "selected" : ""} ${key === "gambler" ? "danger" : ""}`} onClick={() => update("style", key)}>
                <strong>{STYLE_COPY[key].name}</strong><span>{STYLE_COPY[key].summary}</span>
              </button>
            ))}
          </fieldset>
          <fieldset className="ai-paper-symbols" disabled={profile.enabled || instruments.length === 0}>
            <legend>交易合约 <span>可单选或多选</span></legend>
            <details open>
              <summary><strong>股票池</strong><span>{selectedStockCount}/{stockPool.length}</span></summary>
              <div className="ai-paper-pool-actions"><button type="button" onClick={() => setPool(stockPool, true)}>全选</button><button type="button" onClick={() => setPool(stockPool, false)}>清空</button></div>
              <div className="ai-paper-symbol-grid">{stockPool.map((item) => (
                <label key={item.venueSymbol} className={selectedSet.has(item.venueSymbol) ? "selected" : ""}>
                  <input type="checkbox" checked={selectedSet.has(item.venueSymbol)} onChange={() => toggleSymbol(item.venueSymbol)} />
                  <span><strong>{item.displaySymbol}</strong><small>{item.name}</small></span>
                </label>
              ))}</div>
            </details>
            <details open>
              <summary><strong>加密池</strong><span>{selectedCryptoCount}/{cryptoPool.length}</span></summary>
              <div className="ai-paper-pool-actions"><button type="button" onClick={() => setPool(cryptoPool, true)}>全选</button><button type="button" onClick={() => setPool(cryptoPool, false)}>清空</button></div>
              <div className="ai-paper-symbol-grid crypto">{cryptoPool.map((item) => (
                <label key={item.venueSymbol} className={selectedSet.has(item.venueSymbol) ? "selected" : ""}>
                  <input type="checkbox" checked={selectedSet.has(item.venueSymbol)} onChange={() => toggleSymbol(item.venueSymbol)} />
                  <span><strong>{item.displaySymbol}</strong><small>{item.name}</small></span>
                </label>
              ))}</div>
            </details>
          </fieldset>
          <label className={`ai-fib-mode ${draft.fibOnly ? "selected" : ""}`}>
            <input type="checkbox" checked={draft.fibOnly} disabled={profile.enabled} onChange={(event) => update("fibOnly", event.target.checked)} />
            <span><strong>仅斐波那契回调交易</strong><small>只在确认摆动的 0.618–0.786 区间入场；其他 Agent 仅计算止盈止损</small></span>
          </label>
          {draft.style === "gambler" && <p className="ai-paper-warning"><IconAlertTriangle size={14} />高风险模拟模式会扩大回撤与爆仓概率，但仍只操作模拟账户。</p>}
          {draft.symbols.length === 0 && <p className="ai-paper-warning"><IconAlertTriangle size={14} />至少选择一个股票或加密合约。</p>}
          <button
            className={`ai-paper-save ${draftDirty ? "dirty" : ""}`}
            type="button"
            disabled={Boolean(busy) || profile.enabled || !draftDirty || !ready}
            onClick={() => void saveDraft()}
          >保存规则</button>
          <p className={`ai-paper-summary ${profile.enabled ? "running" : "pending"}`}>
            <strong>{profile.enabled ? "当前运行" : "即将启动"}</strong>
            <span>{profile.enabled ? runningSummary(profile, instruments) : pendingSummary(draft, instruments)}</span>
          </p>
          <div className="ai-paper-controls">
            {profile.enabled ? (
              <button type="button" className="stop" disabled={Boolean(busy)} onClick={() => void act("stop", () => stopAiPaper(profile.id), "自动模拟已停止，持仓仍由保护条件监控")}><IconPlayerStop size={14} />停止</button>
            ) : (
              <button type="button" className="start" disabled={Boolean(busy) || !ready} onClick={() => void saveAndStart()}><IconPlayerPlay size={14} />保存规则并启动</button>
            )}
            <button
              type="button"
              // While it runs the form is locked and the draft is not what gets sent, so
              // this only needs a valid draft when a save is actually going to happen.
              disabled={Boolean(busy) || (!profile.enabled && !ready)}
              title={profile.enabled
                ? "立即调用当前绑定的大模型，会消耗 API 额度"
                : "保存未保存的规则，再调用当前绑定的大模型，会消耗 API 额度"}
              onClick={() => void (profile.enabled || !draftDirty
                ? act("run", () => runAiPaperNow(profile.id), "模型已完成一轮评估")
                : saveAndEvaluate())}
            ><IconBrain size={14} />{!profile.enabled && draftDirty ? "保存并立即评估" : "立即评估"}</button>
          </div>
          <button className="ai-paper-reset" type="button" disabled={Boolean(busy) || profile.enabled || account.positions.length > 0} onClick={() => {
            if (window.confirm(`清空「${profile.name}」的账户、交易日志和决策历史？此操作不可撤销。`)) void act("reset", () => resetAiPaper(profile.id), "AI 模拟账户已重置");
          }}><IconTrash size={13} />重置独立账户</button>
          {profile.id !== "default" && <button className="ai-paper-delete" type="button" disabled={Boolean(busy) || profile.enabled || account.positions.length > 0} onClick={() => {
            if (!window.confirm(`永久删除独立模拟「${profile.name}」及其日志和记忆？`)) return;
            void removeSimulation();
          }}><IconTrash size={13} />删除此模拟</button>}
          {profile.last_error && <p className="ai-paper-error"><IconX size={13} />{profile.last_error}</p>}
          {error && <p className="ai-paper-error" role="alert"><IconX size={13} />{error}</p>}
          {notice && <p className="ai-paper-notice"><IconCheck size={13} />{notice}</p>}
        </aside>

        <main className="ai-paper-main">
          <section className="ai-paper-protocol" aria-label="AI 模拟交易执行协议">
            <div><span>监控范围</span><strong>{profile.symbols.length} 个合约</strong><small>股票 {profile.symbols.filter((symbol) => !["BTCUSDT", "ETHUSDT"].includes(symbol)).length} · 加密 {profile.symbols.filter((symbol) => ["BTCUSDT", "ETHUSDT"].includes(symbol)).length}</small></div>
            <div><span>协作协议</span><strong>Agent Council v1</strong><small>只读分析 · 本地风控执行</small></div>
            <div><span>入场权限</span><strong>{profile.fib_only ? "Fib 0.618–0.786" : "多 Agent 综合"}</strong><small>{profile.fib_only ? "Agent 仅制定退出计划" : "证据冲突必须记录"}</small></div>
            <div><span>费用基准</span><strong>{feePolicy.benchmark} {feePolicy.tier} · {feePolicy.fillType.toUpperCase()}</strong><small>单边 {feePolicy.feeRatePct.toFixed(4)}% · 开仓/平仓均扣</small></div>
          </section>
          <div className="ai-paper-kpis">
            <Kpi label="当前权益" value={`${compactUsdt(account.equity)} USDT`} sub={`本金 ${compactUsdt(account.initial_cash)}`} />
            <Kpi label="收益率" value={`${metrics.returnPct >= 0 ? "+" : ""}${metrics.returnPct.toFixed(2)}%`} tone={metrics.returnPct >= 0 ? "positive" : "negative"} sub={`总盈亏 ${metrics.netPnl >= 0 ? "+" : ""}${compactUsdt(metrics.netPnl)} · 已实现 ${metrics.realizedNetPnl >= 0 ? "+" : ""}${compactUsdt(metrics.realizedNetPnl)}`} />
            <Kpi label="模拟正确率" value={`${metrics.winRate.toFixed(1)}%`} sub={`${metrics.wins} 盈 / ${metrics.losses} 亏 / ${metrics.closedTrades} 单`} />
            <Kpi label="最大已实现回撤" value={`${metrics.maxDrawdownPct.toFixed(2)}%`} tone={metrics.maxDrawdownPct > 10 ? "negative" : undefined} sub={`有效杠杆上限 ${effectiveLeverage}x`} />
            <Kpi label="累计手续费" value={`${compactUsdt(account.fees_paid)} USDT`} sub={`${feePolicy.formula} · 已计入净收益`} />
          </div>

          <section className="ai-paper-section">
            <SectionTitle title={`当前持仓 · ${account.positions.length}`} action={<button type="button" onClick={() => void load()} disabled={Boolean(busy)}><IconRefresh size={13} />刷新</button>} />
            {account.positions.length === 0 ? <Empty text="暂无 AI 模拟持仓。模型可以选择观望，这也会被写入决策日志。" /> : (
              <div className="ai-position-grid">{account.positions.map((position: PositionView & { simulation?: AiSimulationCondition }) => (
                <article key={position.id}>
                  <header><div><strong>{position.display_symbol}</strong><span className={position.side}>{position.side === "long" ? "多" : "空"} · {position.leverage}x</span></div><b className={(position.unrealized_pnl ?? 0) >= 0 ? "positive" : "negative"}>{(position.unrealized_pnl ?? 0) >= 0 ? "+" : ""}{compactUsdt(position.unrealized_pnl)} USDT</b></header>
                  <div className="ai-position-condition"><strong>{position.simulation?.name ?? profile.name}</strong><span>{position.simulation?.styleLabel ?? STYLE_COPY[profile.style].name}</span><span>{position.simulation?.horizonLabel ?? HORIZON_LABEL[profile.horizon]}</span><span>{position.simulation?.entryLabel ?? (profile.fib_only ? "Fib 0.618–0.786" : "多 Agent 综合")}</span><span>上限 {position.simulation?.maxLeverage ?? profile.max_leverage}x</span><span>{positionCondition(position.simulation)}</span></div>
                  <dl><div><dt>入场</dt><dd>{formatPrice(position.entry_price)}</dd></div><div><dt>标记价</dt><dd>{formatPrice(position.mark_price)}</dd></div><div><dt>名义额</dt><dd>{compactUsdt(position.notional)}</dd></div><div><dt>入场手续费</dt><dd>{compactUsdt(position.fees_paid)}</dd></div><div><dt>强平价</dt><dd>{formatPrice(position.liq_price)}</dd></div></dl>
                  <p>{position.notes || "AI 未记录理由"}</p>
                  <button type="button" disabled={Boolean(busy)} onClick={() => void act(`close-${position.id}`, () => closeAiPaperPosition(profile.id, position.id), `${position.display_symbol} 已手动退出 AI 模拟账户`)}>人工紧急平仓</button>
                </article>
              ))}</div>
            )}
          </section>

          <section className="ai-paper-section">
            <SectionTitle title={`AI 模拟交易日志 · ${profile.name}`} action={<div className="ai-paper-export"><a href={aiPaperExportUrl(profile.id, "csv")} download><IconDownload size={12} />决策 CSV</a><a href={aiPaperExportUrl(profile.id, "json")} download>完整 JSON</a></div>} />
            {decisions.length === 0 ? <Empty text="尚无模型决策。启动后会在对应周期的新 K 线收盘时评估。" /> : (
              <div className="ai-decision-list">{decisions.map((item) => (
                <article key={item.id} className={item.status}>
                  <time>{new Date(item.cycle_ts).toLocaleString("zh-CN", { hour12: false })}</time>
                  <span className="ai-decision-action">{item.action === "open" ? "开仓" : item.action === "close" ? "平仓" : "观望"}</span>
                  <div><strong>{item.symbol ?? "全市场"}{item.side ? ` · ${item.side === "long" ? "多" : "空"}` : ""}</strong><p>{item.error || item.reason || "未提供原因"}</p>{item.lesson_applied && <small>采用经验：{item.lesson_applied}</small>}{Boolean(item.evidence?.agentsConsulted?.length) && <small>协作 Agent：{item.evidence?.agentsConsulted?.join(" · ")}</small>}{item.evidence?.simulation && <small>决策时规则：{decisionCondition(item.evidence.simulation)}</small>}</div>
                  <span>{STATUS_LABEL[item.status] ?? item.status}<small>{item.confidence === null ? "" : ` ${(item.confidence * 100).toFixed(0)}%`}</small></span>
                  <em>{item.model ?? "—"}</em>
                </article>
              ))}</div>
            )}
          </section>

          <section className="ai-paper-section">
            <SectionTitle title={`已完成交易 · ${journal.length}`} />
            {journal.length === 0 ? <Empty text="尚无已平仓交易；每次盈利和亏损都会单独列在这里，并写入持久记忆。" /> : (
              <div className="ai-trade-list">{journal.map((trade) => (
                <article key={trade.id}>
                  <time>{new Date(trade.closed_ts).toLocaleString("zh-CN", { hour12: false })}</time><strong>{trade.symbol}</strong><span>{trade.side === "long" ? "多" : "空"} · {trade.leverage}x</span><span>{formatPrice(trade.entry_price)} → {formatPrice(trade.exit_price)}</span><b className={trade.net_pnl >= 0 ? "positive" : "negative"}>{trade.net_pnl >= 0 ? "+" : ""}{compactUsdt(trade.net_pnl)} USDT<small>手续费 {compactUsdt(trade.fees)}</small></b><p>{trade.rationale || "未记录原因"}</p>
                </article>
              ))}</div>
            )}
          </section>

          <section className="ai-paper-section ai-memory">
            <SectionTitle title={`独立持久记忆 · ${profile.name}`} action={<div className="ai-paper-export"><a href={aiPaperExportUrl(profile.id, "md")} download><IconDownload size={12} />导出 Markdown</a><button type="button" onClick={() => setMemoryOpen((value) => !value)}>{memoryOpen ? "收起" : "查看"}</button></div>} />
            <p>记忆保存在本机独立文档中，不绑定 DeepSeek、OpenAI 或其他模型。更换模型后，新模型仍会收到历史盈亏和已采用经验。</p>
            <code>{snapshot.memory.path}</code>
            {memoryOpen && <pre>{snapshot.memory.content}</pre>}
          </section>
        </main>
      </div>
    </div>
  );
}

function Kpi({ label, value, sub, tone }: { label: string; value: string; sub: string; tone?: string }) {
  return <div><span>{label}</span><strong className={tone}>{value}</strong><small>{sub}</small></div>;
}
function SectionTitle({ title, action }: { title: string; action?: React.ReactNode }) {
  return <header className="ai-paper-section-title"><h4>{title}</h4>{action}</header>;
}
function Empty({ text }: { text: string }) { return <p className="ai-paper-empty">{text}</p>; }
