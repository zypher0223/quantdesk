import { IconInfoCircle, IconX } from "@tabler/icons-react";

import {
  cpaEvidenceCard,
  type CpaCatalog,
  type CpaGeometry,
  type CpaMarker,
  type CpaOverlayPlan,
  type CpaPhaseRecord,
} from "../services/cpa";

/**
 * The CPA drawing layer, as plain SVG over the chart.
 *
 * Everything it draws comes from `CpaOverlayPlan`; when the plan says `draw: false`
 * the component returns `null`, which is what makes "switch off" and "not enough
 * data" both mean *nothing was added to the chart* rather than "an empty overlay was
 * added". It renders no chart series of its own, so the candles, the volume and the
 * Fibonacci overlay are untouched by its presence.
 */

export function CpaOverlay({
  plan,
  geometry,
  height,
  width,
  onSelect,
}: {
  plan: CpaOverlayPlan;
  geometry: CpaGeometry;
  height: number;
  width: number;
  onSelect: (marker: CpaMarker) => void;
}) {
  if (!plan.draw) return null;
  const hasMarks =
    geometry.bands.length + geometry.levels.length + geometry.markers.length + geometry.lines.length > 0;
  if (!hasMarks) return null;

  return (
    <svg
      className="cpa-overlay"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`CPA 阶段叠加：${geometry.bands.length} 段阶段背景，${geometry.markers.length} 个标记`}
    >
      {geometry.bands.map((band, index) => (
        <rect
          key={`band-${index}-${band.label}`}
          className={`cpa-band cpa-band-${band.tone}${band.status === "candidate" ? " cpa-band-candidate" : ""}`}
          x={band.x1}
          y={0}
          width={Math.max(2, band.x2 - band.x1)}
          height={height}
        />
      ))}
      {geometry.lines.map((line) => (
        <path
          key={line.key}
          className={`cpa-line cpa-line-${line.key}`}
          d={line.path}
          fill="none"
          strokeWidth={1}
        />
      ))}
      {geometry.levels.map((level, index) => (
        <g key={`level-${level.kind}-${index}`}>
          <line
            className={level.kind === "invalidation" ? "cpa-invalidation" : "cpa-pivot"}
            x1={level.x1}
            x2={level.x2}
            y1={level.y}
            y2={level.y}
          />
          <text x={Math.max(4, level.x1 + 4)} y={level.y - 3}>{level.label}</text>
        </g>
      ))}
      {geometry.markers.map((mark, index) => (
        <g
          key={`marker-${mark.marker.time}-${index}`}
          className={`cpa-marker cpa-marker-${mark.marker.kind}${mark.marker.side === "short" ? " cpa-marker-short" : ""}`}
        >
          <circle cx={mark.x} cy={mark.y} r={mark.marker.kind === "confirmed" ? 4 : 3} />
        </g>
      ))}
      {geometry.anchors.map((anchor) => (
        <g
          key={`anchor-${anchor.marker.time}-${anchor.marker.kind}`}
          className={`cpa-anchor cpa-anchor-${anchor.marker.kind}`}
          role="button"
          tabIndex={0}
          aria-label={`查看阶段证据：${anchor.marker.label}`}
          onClick={() => onSelect(anchor.marker)}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") onSelect(anchor.marker);
          }}
        >
          {/* A generous invisible hit area: the visible dot is 6px across. */}
          <circle className="cpa-anchor-hit" cx={anchor.x} cy={anchor.y} r={11} />
          <circle className="cpa-anchor-dot" cx={anchor.x} cy={anchor.y} r={5} />
          <text x={anchor.x + 8} y={anchor.y - 8}>{anchor.marker.label}</text>
        </g>
      ))}
    </svg>
  );
}

/**
 * Why the overlay is not drawn, when it is not.
 *
 * A silent absence would read as "no phases here", which is a different statement
 * from "the sample is too short" - so the reason is always rendered.
 */
export function CpaStatusLine({ plan, error }: { plan: CpaOverlayPlan; error?: string }) {
  if (error) {
    return (
      <p className="cpa-status cpa-status-error" role="status">
        <IconInfoCircle size={13} /> CPA 阶段读取失败：{error}
      </p>
    );
  }
  if (!plan.enabled) return null;
  if (plan.draw) {
    return (
      <p className="cpa-status" role="status">
        阶段 {plan.bands.length} 段 · 标记 {plan.markers.length} 个 · 规则版本 {plan.parameterVersion || "—"}
        {plan.dataVersion ? ` · 数据版本 ${plan.dataVersion.slice(0, 8)}` : ""}
      </p>
    );
  }
  return (
    <p className={plan.insufficient ? "cpa-status cpa-status-warn" : "cpa-status"} role="status">
      <IconInfoCircle size={13} /> {plan.reason}
    </p>
  );
}

/** One field of the evidence card, rendered only when it carries a value. */
function Field({ label, value }: { label: string; value: string | null }) {
  if (value === null || value === "" || value === "—") return null;
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function price(value: number | null): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function ratio(value: number | null): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return value.toFixed(2);
}

function stamp(time: number): string {
  return new Date(time).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/**
 * The card a phase marker opens: the evidence behind one phase reading.
 *
 * It shows the version that produced the reading and the reasons the detector gave,
 * because a phase without its parameters and its reasons is an opinion.
 */
export function CpaPhaseCard({
  record,
  catalog,
  dataVersion,
  higherIntervals,
  onClose,
}: {
  record: CpaPhaseRecord;
  catalog: CpaCatalog | null;
  dataVersion: string;
  higherIntervals: { management: string; background: string };
  onClose: () => void;
}) {
  const card = cpaEvidenceCard(record, { catalog, dataVersion, higherIntervals });
  const higher = card.higherTimeframe;
  const background = card.backgroundTimeframe;
  const higherText = higher?.interval
    ? `${higher.interval} · ${higher.phase === "none" ? "无阶段" : higher.phase} · ${higher.trend}${higher.available ? "" : "（数据不足）"}`
    : "未配置";
  const backgroundText = background?.interval
    ? `${background.interval} · ${background.phase === "none" ? "无阶段" : background.phase} · ${background.trend}${background.available ? "" : "（数据不足）"}`
    : "未配置";

  return (
    <aside className="cpa-card" role="dialog" aria-label="CPA 阶段证据">
      <header>
        <div>
          <strong>{card.title}</strong>
          <span className="cpa-card-time">{stamp(card.time)}</span>
        </div>
        <button type="button" onClick={onClose} aria-label="关闭阶段证据">
          <IconX size={14} />
        </button>
      </header>
      {!card.signal.actionable && (
        <p className="cpa-card-flag">
          {card.signal.kind === "candidate"
            ? "这是候选阶段，引擎未确认，回测不会在它上面开仓。"
            : "这是观察阶段，只作风险提示，回测不会在它上面开仓。"}
        </p>
      )}
      <dl>
        <Field label="阶段" value={card.title} />
        <Field label="方向" value={card.direction === "bearish" ? "偏空" : card.direction === "bullish" ? "偏多" : "中性"} />
        <Field label="置信度" value={ratio(card.confidence)} />
        <Field label="管理周期（高周期）" value={higherText} />
        <Field label="背景周期" value={backgroundText} />
        <Field label="成交量比" value={ratio(card.volumeRatio)} />
        <Field label="波动收缩" value={ratio(card.contractionScore)} />
        <Field label="距均线（ATR）" value={ratio(card.distanceAtr)} />
        <Field label="枢轴价" value={price(card.pivotPrice)} />
        <Field label="结构失效价" value={price(card.invalidationPrice)} />
        <Field label="规则版本" value={card.parameterVersion} />
        <Field label="数据版本" value={dataVersion ? dataVersion.slice(0, 16) : ""} />
      </dl>
      {card.reasons.length > 0 && (
        <div className="cpa-card-reasons">
          <h4>识别理由</h4>
          <ul>{card.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>
        </div>
      )}
      {card.warnings.length > 0 && (
        <div className="cpa-card-warnings">
          <h4>注意</h4>
          <ul>{card.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
        </div>
      )}
      <footer>
        概念来源：Oliver Kell 公开描述的 Cycle of Price Action；阈值为 QuantDesk 研究参数。
      </footer>
    </aside>
  );
}
