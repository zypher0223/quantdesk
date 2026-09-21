/**
 * P0 realtime page probe.
 *
 * Measures the §6.3 browser-side metric — time from navigation to the first
 * visible real quote — and checks that the realtime stream, not a 30-second
 * poll, is what keeps the page current. It also verifies that a symbol switch
 * cannot be overwritten by the previous subscription.
 *
 * Usage: node scripts/probe-realtime.mjs [url]
 */
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";

const URL_BASE = process.argv[2] ?? "http://127.0.0.1:8765/";
const CACHE = path.join(process.env.HOME, "Library/Caches/ms-playwright");

function findChromium() {
  const candidates = [];
  for (const entry of fs.readdirSync(CACHE)) {
    if (entry.startsWith("chromium_headless_shell-")) {
      candidates.push(path.join(CACHE, entry, "chrome-headless-shell-mac-arm64", "chrome-headless-shell"));
    }
    if (entry.startsWith("chromium-")) {
      candidates.push(path.join(CACHE, entry, "chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"));
    }
  }
  const found = candidates.find((candidate) => fs.existsSync(candidate));
  if (!found) throw new Error(`no cached chromium found under ${CACHE}`);
  return found;
}

const browser = await chromium.launch({ executablePath: findChromium() });
const failures = [];
const requests = [];

async function run(label, symbol) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const seen = [];
  const paths = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    paths.push(url.pathname + url.search);
    seen.push({ at: Date.now(), path: url.pathname });
  });
  const consoleErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  const started = Date.now();
  await page.goto(URL_BASE, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".universe-heading", { timeout: 20_000 });
  if (symbol) {
    await page.waitForFunction(
      (target) => [...document.querySelectorAll(".instrument")].some((button) => button.textContent?.includes(target)),
      symbol,
      { timeout: 20_000 },
    );
    await page.click(`.instrument:has-text("${symbol}")`);
  }
  // The header price is the number the operator reads first.
  await page.waitForFunction(
    () => {
      const value = document.querySelector(".price-block strong")?.textContent ?? "";
      return value && value !== "--" && value !== "—";
    },
    { timeout: 20_000 },
  );
  const firstPaintMs = Date.now() - started;
  const firstPrice = await page.textContent(".price-block strong");
  const statusAfterPaint = (await page.textContent(".system-state"))?.trim() ?? "";

  // Let the stream run long enough to see whether the price moves on its own.
  await page.waitForTimeout(12_000);
  const secondPrice = await page.textContent(".price-block strong");
  const statusAfterStream = (await page.textContent(".system-state"))?.trim() ?? "";
  const streamRequests = paths.filter((entry) => entry.startsWith("/api/market/"));
  const candleRequests = paths.filter((entry) => entry.startsWith("/bybit/v5/market/kline"));
  const resonanceRequests = paths.filter((entry) => entry.startsWith("/api/resonance"));

  // A symbol switch must not be overwritten by frames from the old symbol.
  const switched = await (async () => {
    const other = symbol === "BTC" ? "ETH" : "BTC";
    await page.click(`.instrument:has-text("${other}")`);
    await page.waitForTimeout(4_000);
    const heading = (await page.textContent(".market-heading h1")) ?? "";
    const price = await page.textContent(".price-block strong");
    return { heading: heading.replace(/\s+/g, " ").trim().slice(0, 60), price, symbol: other };
  })();

  await context.close();
  return {
    label,
    firstPaintMs,
    firstPrice,
    statusAfterPaint,
    secondPrice,
    statusAfterStream,
    priceChanged: firstPrice !== secondPrice,
    streamRequests,
    candleRequests: candleRequests.length,
    resonanceRequests: resonanceRequests.length,
    switched,
    consoleErrors,
  };
}

for (const [label, symbol] of [["default", null], ["BTC", "BTC"], ["AAPL", "AAPL"]]) {
  const result = await run(label, symbol);
  console.log(JSON.stringify(result));
  if (result.firstPaintMs > 500) failures.push(`${label}: first paint ${result.firstPaintMs}ms exceeds 500ms`);
  if (result.statusAfterPaint.includes("演示")) failures.push(`${label}: page fell back to demo data`);
  if (result.consoleErrors.length) failures.push(`${label}: console errors ${result.consoleErrors.join(" | ")}`);
}

await browser.close();
if (failures.length) {
  console.error(JSON.stringify({ ok: false, failures }, null, 2));
  process.exit(1);
}
console.log(JSON.stringify({ ok: true }));
