/** Verify that Lightweight Charts preserves every source candle and that the
 * crosshair describes the original bar under the pointer.
 * Usage: node scripts/probe-chart.mjs [url] [timeframe]
 */
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";

const URL_BASE = process.argv[2] ?? "http://localhost:4173/";
const TIMEFRAME = process.argv[3] ?? null;
const CACHE = path.join(process.env.HOME, "Library/Caches/ms-playwright");

function findChromium() {
  const candidates = [];
  for (const entry of fs.readdirSync(CACHE)) {
    if (entry.startsWith("chromium_headless_shell-")) candidates.push(path.join(CACHE, entry, "chrome-headless-shell-mac-arm64", "chrome-headless-shell"));
    if (entry.startsWith("chromium-")) candidates.push(path.join(CACHE, entry, "chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"));
  }
  return candidates.find((candidate) => fs.existsSync(candidate));
}

const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
await page.goto(URL_BASE, { waitUntil: "domcontentloaded" });
await page.waitForFunction(() => Boolean(window.__qdChart?.coordinateForIndex), { timeout: 60_000 });
if (TIMEFRAME) {
  await page.click(`.timeframes button:text-is("${TIMEFRAME}")`);
  await page.waitForTimeout(1500);
  await page.waitForFunction((expected) => window.__qdChart?.timeframe === expected, TIMEFRAME, { timeout: 30_000 });
}

const sourceBars = Number((await page.textContent(".chart-meta span:last-child")).match(/(\d+)\s+BARS/)?.[1] ?? 0);
const drawn = await page.evaluate(() => window.__qdChart.originalBarCount);
const box = await page.locator(".tradingview-chart").boundingBox();
const firstVisible = Math.max(0, drawn - 140);
const span = drawn - 1 - firstVisible;
const targets = [...new Set([firstVisible, firstVisible + 1, firstVisible + Math.floor(span * .25), firstVisible + Math.floor(span * .5), firstVisible + Math.floor(span * .75), drawn - 2, drawn - 1])];
const rows = [];
let mismatches = drawn === sourceBars ? 0 : 1;

for (const index of targets) {
  const cursorX = await page.evaluate((target) => window.__qdChart.coordinateForIndex(target), index);
  if (cursorX == null) {
    rows.push({ index, match: "MISMATCH outside visible range" });
    mismatches += 1;
    continue;
  }
  await page.mouse.move(box.x + cursorX, box.y + box.height / 2);
  await page.waitForTimeout(130);
  const snapshot = await page.evaluate((target) => {
    const state = window.__qdChart;
    const bar = state.bars[target];
    const cells = [...document.querySelectorAll(".chart-readout span")].map((node) => node.textContent?.trim() ?? "");
    return { bar, cells };
  }, index);
  const tooltipHigh = Number((snapshot.cells.find((cell) => cell.startsWith("最高")) ?? "").replace(/[^\d.-]/g, ""));
  const tooltipLow = Number((snapshot.cells.find((cell) => cell.startsWith("最低")) ?? "").replace(/[^\d.-]/g, ""));
  const highOk = Math.abs(tooltipHigh - snapshot.bar.high) < 0.02;
  const lowOk = Math.abs(tooltipLow - snapshot.bar.low) < 0.02;
  const rangeOk = (snapshot.cells[0] ?? "").includes("→");
  const ok = highOk && lowOk && rangeOk;
  if (!ok) mismatches += 1;
  rows.push({ index, cursorX: Number(cursorX.toFixed(1)), high: `${tooltipHigh} vs ${snapshot.bar.high.toFixed(2)}`, low: `${tooltipLow} vs ${snapshot.bar.low.toFixed(2)}`, hasRange: rangeOk, match: ok ? "OK" : "MISMATCH" });
}

console.log(JSON.stringify({ timeframe: TIMEFRAME ?? "default", sourceBars, renderedOriginalBars: drawn, rows, mismatches }, null, 2));
await browser.close();
if (mismatches) {
  console.error(`\nFAILED: ${mismatches} chart fidelity checks failed.`);
  process.exit(1);
}
console.log("\nChart fidelity OK: every source candle is preserved and the crosshair reads the original bar.");
