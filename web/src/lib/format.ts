import { INTERVAL_MS, type Timeframe } from "../data/market";

export function formatPrice(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (Math.abs(value) >= 1000) return value.toLocaleString("en-US", { maximumFractionDigits: digits });
  return value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: Math.max(digits, 4) });
}

/** Compact USDT amount for desk readouts. */
export function compactUsdt(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const units: Array<[number, string]> = [
    [1e12, "万亿"],
    [1e8, "亿"],
    [1e4, "万"],
  ];
  for (const [size, suffix] of units) {
    if (Math.abs(value) >= size) return `${(value / size).toFixed(2)}${suffix}`;
  }
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

export function formatPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`;
}

/** Funding is quoted as a rate; 4 decimals matches the venue's own precision. */
export function formatRate(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(4)}%`;
}

export function formatCandleTime(time: number, timeframe: string): string {
  const date = new Date(time);
  const zone = "zh-CN";
  if (timeframe === "1d") return date.toLocaleDateString(zone, { year: "numeric", month: "2-digit", day: "2-digit" });
  return date.toLocaleString(zone, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

const pad = (value: number) => String(value).padStart(2, "0");

/**
 * The span a single bar covers: open time to the next bar's open time.
 *
 * A bar stamped 14:00 on a 15m timeframe is the 14:00–14:15 bar, so the end is
 * derived from the following bar when the caller has it. `nextOpen` is omitted
 * for the newest bar, whose end comes from the timeframe length instead.
 */
export function formatCandleRange(time: number, timeframe: string, nextOpen?: number | null): string {
  const start = new Date(time);
  const day = `${pad(start.getMonth() + 1)}/${pad(start.getDate())}`;
  const startText = `${day} ${pad(start.getHours())}:${pad(start.getMinutes())}`;

  if (timeframe === "1d") {
    const end = new Date(time + 86_400_000);
    return `${start.getFullYear()}/${day} → ${pad(end.getMonth() + 1)}/${pad(end.getDate())}`;
  }

  const step = INTERVAL_MS[timeframe as Timeframe] ?? 3_600_000;
  const end = new Date(nextOpen ?? time + step);
  const endDay = `${pad(end.getMonth() + 1)}/${pad(end.getDate())}`;
  const endText = `${pad(end.getHours())}:${pad(end.getMinutes())}`;
  // Same calendar day: show the date once, and only when there is room.
  if (endDay === day) return `${startText} → ${endText}`;
  return `${startText} → ${endDay} ${endText}`;
}

export function formatClock(time: number | null | undefined): string {
  if (!time) return "—";
  return new Date(time).toLocaleTimeString("zh-CN", { hour12: false });
}

export function relativeAge(timestamp: number | null | undefined, now = Date.now()): string {
  if (!timestamp) return "尚未获取";
  const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
  if (seconds < 5) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}
