import { useState } from "react";
import { IconAlertTriangle, IconCheck, IconRefresh, IconTrash } from "@tabler/icons-react";
import { megabytesText } from "../services/run-figures";
import { pruneRuns, type PruneReport } from "../services/runs";

/**
 * Record cleanup, behind two deliberate clicks.
 *
 * The engine's retention policy is applied only when it is asked for: opening
 * this section runs a dry run and shows the policy, the records that would go and
 * the room they would free. The 确认清理 button appears only after that plan is on
 * screen, and it is the only thing here that deletes anything. Nothing prunes on
 * a timer, on a mount, or as a side effect of reading.
 */
export function RunMaintenancePanel({ onPruned }: { onPruned?: () => void }) {
  const [plan, setPlan] = useState<PruneReport | null>(null);
  const [applied, setApplied] = useState<PruneReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const inspect = async () => {
    setBusy(true);
    try {
      setPlan(await pruneRuns(true));
      setApplied(null);
      setError("");
    } catch (reason) {
      setPlan(null);
      setError(reason instanceof Error ? reason.message : "读取清理计划失败");
    } finally {
      setBusy(false);
    }
  };

  const apply = async () => {
    setBusy(true);
    try {
      const report = await pruneRuns(false);
      setApplied(report);
      setPlan(null);
      setError("");
      onPruned?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "清理失败");
    } finally {
      setBusy(false);
    }
  };

  const doomedRuns = plan?.runs.length ?? 0;
  const doomedFactors = plan?.factorRuns.length ?? 0;
  const nothingToDo = plan !== null && doomedRuns + doomedFactors === 0;

  return <details className="run-maintenance" onToggle={(event) => {
    // The dry run happens when the section is opened, never on mount.
    if (event.currentTarget.open && plan === null && applied === null && !busy) void inspect();
  }}>
    <summary>
      <span className="run-section-heading"><h3>记录清理</h3><span>先查看计划，再手动确认</span></span>
    </summary>

    {error && <p className="history-error" role="alert"><IconAlertTriangle size={15} />{error}</p>}

    {!plan && !applied && !error && <p className="backtest-hint">{busy ? "正在读取清理计划…" : "正在准备清理计划…"}</p>}

    {plan && <>
      <dl className="run-maintenance-policy">
        <div><dt>保留条数</dt><dd>{plan.policy.keepRuns > 0 ? `最近 ${plan.policy.keepRuns} 条 / 类` : "不限条数"}</dd></div>
        <div><dt>保留天数</dt><dd>{plan.policy.keepDays > 0 ? `${plan.policy.keepDays} 天` : "不限天数"}</dd></div>
        <div><dt>将删除</dt><dd>{doomedRuns} 条研究记录 · {doomedFactors} 条因子记录</dd></div>
        <div><dt>释放空间</dt><dd>{megabytesText(plan.bytes)}</dd></div>
      </dl>
      <p className="backtest-hint">
        排队中与运行中的任务不会被清理；结论、结果文件会随所属任务一并删除。当前只是计划，尚未删除任何记录。
      </p>
      {doomedRuns + doomedFactors > 0 && <p className="run-maintenance-ids">
        研究 #{plan.runs.slice(0, 24).join("、#") || "—"}{plan.runs.length > 24 ? ` 等 ${plan.runs.length} 条` : ""}
        {doomedFactors > 0 ? ` · 因子 #${plan.factorRuns.slice(0, 12).join("、#")}${plan.factorRuns.length > 12 ? ` 等 ${plan.factorRuns.length} 条` : ""}` : ""}
      </p>}
      <div className="run-maintenance-actions">
        <button type="button" onClick={() => void inspect()} disabled={busy}>
          <IconRefresh className={busy ? "spin" : ""} size={14} />重新检查
        </button>
        <button type="button" className="run-maintenance-confirm" onClick={() => void apply()} disabled={busy || nothingToDo}>
          <IconTrash size={14} />{nothingToDo ? "没有需要清理的记录" : `确认清理（${doomedRuns + doomedFactors} 条）`}
        </button>
      </div>
    </>}

    {applied && <div className="run-maintenance-done" role="status">
      <p>
        <IconCheck size={14} />
        <span>
          已删除 {applied.deletedRuns} 条研究记录、{applied.deletedFactorRuns} 条因子记录，释放约 {megabytesText(applied.bytes)}。
          {applied.remaining ? `剩余 ${applied.remaining.total} 条（排队 ${applied.remaining.queued} · 运行中 ${applied.remaining.running}）。` : ""}
        </span>
      </p>
      <button type="button" onClick={() => void inspect()} disabled={busy}>
        <IconRefresh className={busy ? "spin" : ""} size={14} />再检查一次
      </button>
    </div>}
  </details>;
}
