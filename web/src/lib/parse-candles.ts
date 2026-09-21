import type { Candle } from "../data/market";

const REQUIRED = ["open", "high", "low", "close"] as const;

/**
 * Parse an uploaded CSV/JSON candle file in the browser.
 * Accepted time fields: time, timestamp, date, ts, open_time, openTime.
 * Accepted volume fields: volume, vol, qty.
 */
export function parseCandleFile(text: string, fileName: string): Candle[] {
  const lower = fileName.toLowerCase();
  const rows: unknown[] = lower.endsWith(".json") ? parseJson(text) : parseCsv(text);
  if (!rows.length) throw new Error("文件中没有可解析的记录。");

  const candles = rows
    .map((row) => normalize(row))
    .filter((row): row is Candle => row !== null)
    .sort((a, b) => a.time - b.time);

  const deduped = dedupe(candles);
  if (deduped.length < 10) throw new Error(`仅解析出 ${deduped.length} 条有效K线，至少需要 10 条。请检查字段名称（time/open/high/low/close/volume）。`);
  return deduped;
}

function parseJson(text: string): unknown[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("JSON 解析失败，请检查文件内容。");
  }
  if (Array.isArray(parsed)) return parsed;
  // Common shapes: { data: [...] } or { result: { list: [...] } }
  const record = parsed as Record<string, unknown>;
  for (const key of ["data", "candles", "list", "klines", "rows"]) {
    const value = record?.[key];
    if (Array.isArray(value)) return value;
  }
  const nested = record?.result as Record<string, unknown> | undefined;
  if (Array.isArray(nested?.list)) return nested.list as unknown[];
  throw new Error("JSON 中未找到K线数组，支持顶层数组或 data/candles/list 字段。");
}

function parseCsv(text: string): unknown[] {
  const lines = text.trim().split(/\r?\n/);
  if (lines.length < 2) return [];
  // Bybit exports sometimes carry a symbol/category preamble line before the header.
  const headerIndex = lines.findIndex((line) => /open/i.test(line) && /close/i.test(line));
  if (headerIndex < 0) throw new Error("CSV 缺少表头，需要包含 open 与 close 列。");
  const headers = lines[headerIndex].split(",").map((value) => value.trim().replace(/^"|"$/g, "").toLowerCase());
  return lines
    .slice(headerIndex + 1)
    .filter((line) => line.trim().length > 0)
    .map((line) => {
      const cells = line.split(",").map((value) => value.trim().replace(/^"|"$/g, ""));
      return Object.fromEntries(headers.map((header, index) => [header, cells[index]]));
    });
}

function pick(record: Record<string, unknown>, names: readonly string[]): unknown {
  for (const name of names) {
    const value = record[name];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return undefined;
}

function toTime(value: unknown): number {
  if (typeof value === "number") return value < 10_000_000_000 ? value * 1000 : value;
  const text = String(value).trim();
  if (/^\d+$/.test(text)) {
    const numeric = Number(text);
    return numeric < 10_000_000_000 ? numeric * 1000 : numeric;
  }
  return Date.parse(text.replace(" ", "T"));
}

function normalize(row: unknown): Candle | null {
  if (Array.isArray(row)) {
    // Array form: [time, open, high, low, close, volume, turnover?]
    if (row.length < 5) return null;
    const time = toTime(row[0]);
    const numbers = row.slice(1, 6).map(Number);
    if (!Number.isFinite(time) || numbers.some((value) => !Number.isFinite(value))) return null;
    return { time, open: numbers[0], high: numbers[1], low: numbers[2], close: numbers[3], volume: numbers[4] ?? 0 };
  }
  if (!row || typeof row !== "object") return null;

  const record = row as Record<string, unknown>;
  const lowered: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record)) lowered[key.trim().toLowerCase()] = value;

  const rawTime = pick(lowered, ["time", "timestamp", "date", "ts", "open_time", "opentime", "datetime"]);
  if (rawTime === undefined) return null;
  const time = toTime(rawTime);
  if (!Number.isFinite(time)) return null;

  const values: Record<string, number> = {};
  for (const field of REQUIRED) {
    const value = Number(pick(lowered, [field]));
    if (!Number.isFinite(value)) return null;
    values[field] = value;
  }
  const volume = Number(pick(lowered, ["volume", "vol", "qty", "basevolume"]) ?? 0);
  if (values.high < values.low) return null;
  return { time, open: values.open, high: values.high, low: values.low, close: values.close, volume: Number.isFinite(volume) ? volume : 0 };
}

function dedupe(candles: Candle[]): Candle[] {
  const byTime = new Map<number, Candle>();
  for (const candle of candles) byTime.set(candle.time, candle);
  return [...byTime.values()].sort((a, b) => a.time - b.time);
}
