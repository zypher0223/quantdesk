import { IconShieldCheck, IconShieldOff } from "@tabler/icons-react";
import type { PluginSandboxStatus } from "../services/plugins";
import { sandboxLabel, sandboxState } from "../services/runs";

/**
 * Which isolation was in force, as one chip.
 *
 * The engine records the sandbox per result and per verdict row; the page prints
 * its answer rather than the operator's assumption. A run whose row says nothing
 * reads 未知 — an unrecorded isolation is not a proven one.
 */
export function SandboxChip({ value, detail }: { value: string | null | undefined; detail?: string }) {
  const state = sandboxState(value);
  const title = detail ?? (value ? `沙箱记录：${value}` : "该记录没有沙箱信息");
  return <span className={`sandbox-chip sandbox-${state}`} title={title}>
    {state === "enforced" ? <IconShieldCheck size={12} /> : state === "unenforced" ? <IconShieldOff size={12} /> : null}
    {sandboxLabel(value)}
  </span>;
}

/**
 * The plugin host's own sandbox state, for the factor panel header.
 *
 * `enforced` is the engine's verdict, and `detail` is its sentence about why —
 * shown as the tooltip so an unenforced sandbox is explained where it is
 * reported instead of leaving the operator to guess.
 */
export function PluginSandboxChip({ status }: { status: PluginSandboxStatus | null | undefined }) {
  if (!status) return null;
  const enforced = status.enforced === true;
  const label = enforced ? sandboxLabel("enforced") : sandboxLabel("unenforced");
  const backend = status.backend ? `后端 ${status.backend}` : status.available ? "后端未报告" : "没有可用的沙箱后端";
  return <span
    className={`sandbox-chip sandbox-${enforced ? "enforced" : "unenforced"}`}
    title={`${status.detail || "沙箱状态未说明"} · 策略 ${status.policy || "—"} · ${backend}`}
  >
    {enforced ? <IconShieldCheck size={12} /> : <IconShieldOff size={12} />}
    {label}
  </span>;
}
