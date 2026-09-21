import { useEffect, useMemo, useRef, useState } from "react";
import { IconAlertTriangle, IconInfoCircle } from "@tabler/icons-react";

import type { StrategyParams } from "../services/backtest";
import {
  attributionText,
  fetchCpaCatalog,
  parameterControls,
  parameterDefaults,
  phaseLabel,
  positionNotice,
  warmupState,
  type CpaCatalog,
  type CpaParameterSpec,
} from "../services/cpa";

/**
 * The CPA parameter panel.
 *
 * Rendered from `/api/cpa/catalog` rather than from a hard-coded form, so the labels,
 * units, ranges and help text are the engine's own. The defaults are re-taken whenever
 * the contract class or the interval changes, because the engine publishes a different
 * set for a leveraged ETF than for a crypto perp and a different set again for an
 * intraday interval.
 *
 * Two things are always visible here and are not optional: the rule version that will
 * stamp the result, and the simplified-position notice.
 */

function NumberField({
  spec,
  value,
  onChange,
}: {
  spec: CpaParameterSpec;
  value: unknown;
  onChange: (value: number) => void;
}) {
  return (
    <label>
      {spec.label}
      {spec.unit ? <span className="cpa-unit">（{spec.unit}）</span> : null}
      <input
        type="number"
        min={spec.minimum ?? undefined}
        max={spec.maximum ?? undefined}
        step={spec.type === "integer" ? 1 : "any"}
        value={typeof value === "number" ? value : ""}
        onChange={(event) => onChange(Number(event.target.value))}
        title={spec.help}
      />
    </label>
  );
}

export function CpaParameterPanel({
  productType,
  timeframe,
  availableBars,
  value,
  onChange,
}: {
  productType: string;
  timeframe: string;
  availableBars: number;
  value: StrategyParams;
  onChange: (next: StrategyParams) => void;
}) {
  const [catalog, setCatalog] = useState<CpaCatalog | null>(null);
  const [error, setError] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);

  useEffect(() => {
    let active = true;
    void fetchCpaCatalog()
      .then((payload) => { if (active) { setCatalog(payload); setError(""); } })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : "读取 CPA 参数表失败");
      });
    return () => { active = false; };
  }, []);

  const defaults = useMemo(
    () => parameterDefaults(catalog, productType, timeframe),
    [catalog, productType, timeframe],
  );
  const controls = useMemo(() => parameterControls(catalog), [catalog]);
  const warmup = useMemo(
    () => warmupState(catalog, timeframe, availableBars, productType),
    [catalog, timeframe, availableBars, productType],
  );

  /**
   * Fill in the preset for this contract class and interval.
   *
   * An empty form (the state right after switching to `cpa_cycle`) is filled straight
   * away. If the class or the interval changes while the form already holds values,
   * the new preset is offered as a button instead of being applied: an edit is never
   * discarded silently.
   */
  const signature = useMemo(() => {
    const keys = Object.keys(defaults).sort();
    return `${productType}|${timeframe}|${keys.join(",")}`;
  }, [defaults, productType, timeframe]);
  const appliedRef = useRef<string>("");
  useEffect(() => {
    if (!catalog || !Object.keys(defaults).length) return;
    if (appliedRef.current === signature) return;
    appliedRef.current = signature;
    if (Object.keys(value ?? {}).length === 0) {
      onChange({ ...defaults });
      setConfirmReset(false);
      return;
    }
    setConfirmReset(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, catalog]);

  const spec = (key: string) => catalog?.parameters?.find((item) => item.key === key);
  const positionModel = String(value.positionModel ?? "single");
  const intentOnlyNumbers = new Set([
    "initialExposurePct", "addExposurePct", "reduceExposurePct",
    "maxPortfolioRiskPct", "maxPerTradeRiskPct",
  ]);
  const dedicatedBooleans = new Set(["exitOnExhaustion", "requireHigherTimeframe"]);
  const numberKeys = (catalog?.parameters ?? [])
    .filter((item) => (item.type === "number" || item.type === "integer")
      && (positionModel === "intent" || !intentOnlyNumbers.has(item.key)))
    .map((item) => item.key);
  const boolKeys = (catalog?.parameters ?? [])
    .filter((item) => item.type === "boolean" && !dedicatedBooleans.has(item.key))
    .map((item) => item.key);

  const entryStages = Array.isArray(value.entryStages) ? (value.entryStages as string[]) : [];
  const entryOptions = controls.entryStages?.options ?? ["wedge_pop", "ema_crossback", "base_n_break"];

  return (
    <section className="cpa-parameters" aria-label="CPA 参数">
      <header>
        <h4>CPA 周期参数</h4>
        <span className="cpa-version">规则版本 {catalog?.parameterVersion ?? "—"}</span>
      </header>

      {error && (
        <p className="cpa-warn" role="status">
          <IconAlertTriangle size={13} /> 参数表读取失败，正在使用引擎目录里的默认值：{error}
        </p>
      )}

      <p className={warmup.ok ? "cpa-hint" : "cpa-warn"} role="status">
        <IconInfoCircle size={13} />
        {warmup.ok
          ? `当前 ${warmup.available} 根K线满足 CPA 预热（至少 ${warmup.required} 根）`
          : warmup.reason}
      </p>

      {confirmReset && (
        <div className="cpa-reset">
          <span>资产类别或周期已变化，引擎为它准备了另一套默认值。</span>
          <button type="button" onClick={() => { onChange({ ...defaults }); setConfirmReset(false); }}>
            恢复该组合的默认值
          </button>
        </div>
      )}

      <div className="cpa-switches">
        {controls.positionModel && (
          <label>
            {controls.positionModel.label}
            <select
              value={positionModel}
              onChange={(event) => onChange({ ...value, positionModel: event.target.value })}
              title={controls.positionModel.help}
            >
              {(catalog?.positionModels ?? controls.positionModel.options ?? ["single", "intent"]).map((model) => (
                <option key={model} value={model}>
                  {model === "intent" ? "intent（分批仓位与结构止损）" : "single（单仓位简化）"}
                </option>
              ))}
            </select>
          </label>
        )}
        {controls.sideMode && (
          <label>
            {controls.sideMode.label}
            <select
              value={String(value.sideMode ?? "long_only")}
              onChange={(event) => onChange({ ...value, sideMode: event.target.value })}
              title={controls.sideMode.help}
            >
              <option value="long_only">long_only（只做多）</option>
              <option value="symmetric">symmetric（启用下行做空）</option>
            </select>
          </label>
        )}
        {controls.exitOnExhaustion && (
          <label className="cpa-toggle">
            <input
              type="checkbox"
              checked={Boolean(value.exitOnExhaustion)}
              onChange={(event) => onChange({ ...value, exitOnExhaustion: event.target.checked })}
            />
            {controls.exitOnExhaustion.label}
          </label>
        )}
        {controls.requireHigherTimeframe && (
          <label className="cpa-toggle">
            <input
              type="checkbox"
              checked={Boolean(value.requireHigherTimeframe)}
              onChange={(event) => onChange({ ...value, requireHigherTimeframe: event.target.checked })}
            />
            {controls.requireHigherTimeframe.label}
          </label>
        )}
      </div>

      {controls.entryStages && (
        <fieldset className="cpa-stages">
          <legend>{controls.entryStages.label}</legend>
          {entryOptions.map((option) => (
            <label key={option} className="cpa-toggle">
              <input
                type="checkbox"
                checked={entryStages.includes(option)}
                onChange={(event) => {
                  const next = event.target.checked
                    ? [...entryStages, option]
                    : entryStages.filter((item) => item !== option);
                  onChange({ ...value, entryStages: next });
                }}
              />
              {phaseLabel(option, catalog, true)}
            </label>
          ))}
          {entryStages.length === 0 && (
            <p className="cpa-warn" role="status">
              <IconAlertTriangle size={13} /> 至少选择一个入场阶段，否则策略永远不会开仓。
            </p>
          )}
        </fieldset>
      )}

      <details className="cpa-thresholds" open>
        <summary>阈值（来自引擎参数表）</summary>
        <div className="cpa-field-grid">
          {numberKeys.map((key) => {
            const item = spec(key);
            if (!item) return null;
            return (
              <NumberField
                key={key}
                spec={item}
                value={value[key]}
                onChange={(next) => onChange({ ...value, [key]: next })}
              />
            );
          })}
        </div>
        <div className="cpa-switches">
          {boolKeys.map((key) => {
            const item = spec(key);
            if (!item) return null;
            return (
              <label key={key} className="cpa-toggle">
                <input
                  type="checkbox"
                  checked={Boolean(value[key])}
                  onChange={(event) => onChange({ ...value, [key]: event.target.checked })}
                />
                {item.label}
              </label>
            );
          })}
        </div>
      </details>

      <p className="cpa-notice" role="note">
        <IconInfoCircle size={13} /> {positionNotice(catalog, positionModel)}
      </p>
      <p className="cpa-attribution">{attributionText(catalog)}</p>
    </section>
  );
}
