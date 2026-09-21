/**
 * End-to-end smoke test against the running dev server.
 *
 * Verifies that the page actually mounts, that the fixed universe comes from
 * the engine, and that every instrument in the pool resolves to live Bybit
 * data — not to the labelled demo fallback.
 *
 * Usage: node scripts/smoke.mjs [url]
 */
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";

const URL_BASE = process.argv[2] ?? "http://localhost:4173/";
const CACHE = path.join(process.env.HOME, "Library/Caches/ms-playwright");

function findChromium() {
  const candidates = [];
  for (const entry of fs.readdirSync(CACHE)) {
    if (entry.startsWith("chromium_headless_shell-")) {
      candidates.push(path.join(CACHE, entry, "chrome-headless-shell-mac-arm64", "chrome-headless-shell"));
      candidates.push(path.join(CACHE, entry, "chrome-mac", "headless_shell"));
    }
    if (entry.startsWith("chromium-")) {
      candidates.push(path.join(CACHE, entry, "chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"));
      candidates.push(path.join(CACHE, entry, "chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"));
    }
  }
  const found = candidates.find((candidate) => fs.existsSync(candidate));
  if (!found) throw new Error(`no cached chromium found under ${CACHE}`);
  return found;
}

const executablePath = findChromium();

const browser = await chromium.launch({ executablePath });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const consoleErrors = [];
const failedRequests = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("requestfailed", (request) => failedRequests.push(`${request.url()} ${request.failure()?.errorText ?? ""}`));

// The research pass is the only expensive call; keep its body so the report can
// quote the engine's own verdict rather than inferring it from the DOM.
let researchResponse = null;
page.on("response", async (response) => {
  if (response.request().method() === "POST" && response.url().includes("/api/research")) {
    try {
      researchResponse = await response.json();
    } catch {
      researchResponse = null;
    }
  }
});

await page.goto(URL_BASE, { waitUntil: "domcontentloaded" });
await page.waitForSelector(".universe-heading", { timeout: 15_000 });

// Wait until the universe rail is populated from /api/instruments.
await page.waitForFunction(() => document.querySelectorAll(".instrument").length >= 17, { timeout: 20_000 });

const railCounts = await page.$$eval(".instrument-group", (groups) =>
  groups.map((group) => ({
    label: group.querySelector("h3")?.textContent?.trim() ?? "?",
    count: group.querySelectorAll(".instrument").length,
  })),
);
const productTags = await page.$$eval(".instrument em", (tags) => tags.map((tag) => tag.textContent?.trim()));

// Wait for the resonance panel to leave its loading state.
await page.waitForFunction(() => document.querySelector(".source-chip")?.textContent !== "加载中", { timeout: 90_000 });

const report = {
  railCounts,
  tagTally: productTags.reduce((tally, tag) => ({ ...tally, [tag]: (tally[tag] ?? 0) + 1 }), {}),
  status: await page.textContent(".system-state"),
  radarSource: await page.textContent(".source-chip"),
  radarRows: await page.$$eval(".resonance-bars div", (rows) =>
    rows.map((row) => ({
      interval: row.querySelector("span")?.textContent,
      verdict: row.querySelector("em")?.textContent,
    })),
  ),
  score: await page.textContent(".signal-score").catch(() => null),
  label: await page.textContent(".signal-label").catch(() => null),
  metrics: await page.$$eval(".metric", (metrics) =>
    metrics.map((metric) => ({
      label: metric.querySelector("span")?.textContent,
      value: metric.querySelector("strong")?.textContent,
      note: metric.querySelector("small")?.textContent,
    })),
  ),
  alert: await page.textContent(".data-alert").catch(() => null),
};

// Walk every instrument in the pool and confirm each renders live candles.
const symbols = await page.$$eval(".instrument strong", (nodes) => nodes.map((node) => node.textContent));
const perSymbol = [];
for (const symbol of symbols) {
  await page.click(`.instrument:has(strong:text-is("${symbol}"))`);
  await page.waitForFunction(
    (target) => document.querySelector(".market-heading h1")?.textContent?.startsWith(target),
    symbol,
    { timeout: 15_000 },
  );
  await page.waitForFunction(
    () => {
      const meta = document.querySelector(".chart-meta span:last-child")?.textContent ?? "";
      const price = document.querySelector(".price-block strong")?.textContent ?? "";
      // Both slices must have landed: candles fill the chart, the ticker fills
      // the derivatives strip and the price block.
      return /BYBIT|上传|演示/.test(meta) && !/0 BARS/.test(meta) && price !== "—";
    },
    { timeout: 40_000 },
  );
  const row = await page.evaluate(() => ({
    symbol: document.querySelector(".market-heading h1")?.textContent?.replace("/ USDT PERP", "").trim(),
    meta: document.querySelector(".chart-meta span:last-child")?.textContent?.replace("VOL · ", "").trim(),
    product: document.querySelector(".market-heading p")?.textContent?.trim(),
    price: document.querySelector(".price-block strong")?.textContent,
    change: document.querySelector(".price-block span")?.textContent,
    oi: [...document.querySelectorAll(".metric")].find((m) => m.querySelector("span")?.textContent === "持仓量")?.querySelector("strong")?.textContent,
    funding: [...document.querySelectorAll(".metric")].find((m) => m.querySelector("span")?.textContent === "资金费率")?.querySelector("strong")?.textContent,
  }));
  perSymbol.push(row);
}

// Let every in-flight slice settle, then assert the panel reaches a live state.
await page.waitForTimeout(2500);
const settled = await page.evaluate(() => ({
  status: document.querySelector(".system-state")?.textContent?.trim().replace(/\s+/g, " ") ?? "",
  alert: document.querySelector(".data-alert")?.textContent?.trim().replace(/\s+/g, " ") ?? null,
  sources: [...document.querySelectorAll(".risk-readout div")]
    .map((row) => `${row.querySelector("dt")?.textContent}=${row.querySelector("dd")?.textContent}`)
    .join(" | "),
  volumeNote: [...document.querySelectorAll(".risk-readout div")]
    .find((row) => row.querySelector("dt")?.textContent === "量能状态")?.querySelector("dd")?.textContent ?? "",
  radarNote: document.querySelector(".radar-note")?.textContent?.trim() ?? "",
}));
report.settled = settled;

// Every timeframe must render every source candle without aggregation, and the
// crosshair readout must describe the original candle under the pointer.
const hoverChecks = [];
for (const frame of ["15m", "1h", "4h", "1d"]) {
  await page.click(`.timeframes button:text-is("${frame}")`);
  await page.waitForTimeout(1200);
  await page.waitForFunction(() => Boolean(window.__qdChart?.coordinateForIndex), { timeout: 20_000 });
  const sourceBars = Number((await page.textContent(".chart-meta span:last-child")).match(/(\d+)\s+BARS/)?.[1] ?? 0);
  const drawn = await page.evaluate(() => window.__qdChart.originalBarCount);
  if (drawn !== sourceBars) throw new Error(`${frame}: chart has ${drawn} bars but source reports ${sourceBars}`);
  const chartBox = await page.locator(".tradingview-chart").boundingBox();
  const firstVisible = Math.max(0, drawn - 140);
  for (const index of [firstVisible, Math.floor((firstVisible + drawn - 1) / 2), drawn - 1]) {
    const cursorX = await page.evaluate((target) => window.__qdChart.coordinateForIndex(target), index);
    if (cursorX == null) throw new Error(`${frame}: bar ${index} is outside the visible range`);
    await page.mouse.move(chartBox.x + cursorX, chartBox.y + chartBox.height / 2);
    await page.waitForTimeout(110);
    const probe = await page.evaluate((target) => {
      const state = window.__qdChart;
      const bar = state.bars[target];
      const cells = [...document.querySelectorAll(".chart-readout span")].map((node) => node.textContent?.trim() ?? "");
      const high = Number((cells.find((cell) => cell.startsWith("最高")) ?? "").replace(/[^\d.-]/g, ""));
      const low = Number((cells.find((cell) => cell.startsWith("最低")) ?? "").replace(/[^\d.-]/g, ""));
      return {
        matchesHigh: Math.abs(high - bar.high) < 0.02,
        matchesLow: Math.abs(low - bar.low) < 0.02,
        hasRange: (cells[0] ?? "").includes("→"),
      };
    }, index);
    hoverChecks.push({ frame, index, ...probe });
  }
}
report.hoverChecks = hoverChecks;

// Settings page: profiles must come from the gateway and no key value may appear.
await page.click('.dock-icon[aria-label="设置"]');
await page.waitForSelector(".settings-profiles", { timeout: 20_000 });
const settingsReport = await page.evaluate(() => ({
  profiles: [...document.querySelectorAll(".settings-profile h4")].map((node) => node.textContent?.trim()),
  roles: [...document.querySelectorAll(".settings-role")].length,
  readiness: document.querySelector(".settings-readiness")?.textContent?.trim() ?? "",
  bodyHasSecret: /sk-[A-Za-z0-9]{8,}/.test(document.body.innerText),
}));
report.settings = settingsReport;

// Screenshot workspace must reach the gateway and report a named failure
// (no vision key is configured in this environment).
await page.click('.dock-icon[aria-label="截图识别"]');
await page.waitForSelector(".shot-zone", { timeout: 20_000 });
const shotReport = await page.evaluate(() => ({
  heading: document.querySelector(".analysis-heading h2")?.textContent?.trim() ?? "",
  hasPrompt: Boolean(document.querySelector(".shot-prompt")),
  emptyState: document.querySelector(".analysis-output .backtest-empty strong")?.textContent?.trim() ?? "",
}));
report.chartAnalysis = shotReport;

// The proxy must carry /api/llm/* to the gateway, not to Vite itself.
const llmProbe = await page.evaluate(async () => {
  const response = await fetch("/api/llm/settings");
  if (!response.ok) return { status: response.status, profiles: [] };
  const body = await response.json();
  return {
    status: response.status,
    profileCount: body.profiles.length,
    profiles: body.profiles.map((profile) => `${profile.name}:${profile.hasKey ? "key" : "nokey"}`),
  };
});
report.llmProxy = llmProbe;

// Backtest must run on the engine, not in the browser.
await page.click('.dock-icon[aria-label="策略回测"]');
await page.waitForSelector(".backtest-controls", { timeout: 20_000 });
await page.click(".backtest-controls button[type=submit]");
await page.waitForSelector(".backtest-stats", { timeout: 120_000 });
const backtestReport = await page.evaluate(() => ({
  stats: [...document.querySelectorAll(".backtest-stat")].map((node) => ({
    label: node.querySelector("span")?.textContent,
    value: node.querySelector("strong")?.textContent,
  })),
  warnings: [...document.querySelectorAll(".backtest-warnings li")].map((node) => node.textContent?.trim()),
  assumptionsShown: Boolean(document.querySelector(".backtest-assumptions")),
  rows: document.querySelectorAll(".backtest-trades tbody tr").length,
  error: document.querySelector(".backtest-error")?.textContent ?? null,
}));
report.backtest = backtestReport;

// Paper trading + journal workspace must render from the gateway.
await page.click('.dock-icon[aria-label="交易日志"]');
await page.waitForSelector(".paper-tabs", { timeout: 20_000 });
const paperReport = await page.evaluate(() => ({
  stats: [...document.querySelectorAll(".paper-stats .backtest-stat")].map((node) => node.querySelector("span")?.textContent),
  hasForm: Boolean(document.querySelector(".paper-form")),
  integrity: document.querySelector(".journal-integrity")?.textContent?.trim() ?? null,
  errors: document.querySelector(".coming-workspace h2")?.textContent ?? null,
}));
await page.click('.paper-tabs button:nth-child(2)');
await page.waitForSelector(".journal-toolbar", { timeout: 20_000 });
// Integrity is verified on the engine; wait for the result rather than reading
// the placeholder.
await page.waitForFunction(
  () => !(document.querySelector(".journal-integrity")?.textContent ?? "").includes("正在校验"),
  { timeout: 60_000 },
);
paperReport.journalIntegrity = await page.textContent(".journal-integrity");
paperReport.exportLinks = await page.$$eval(".journal-actions a", (nodes) => nodes.map((node) => node.getAttribute("href")));
report.paper = paperReport;

// Research workspace: the plan must render with engine-recomputed ratios.
await page.click('.dock-icon[aria-label="深度研判"]');
await page.waitForSelector(".research-layout", { timeout: 20_000 });
const researchReport = await page.evaluate(async () => {
  const readiness = await (await fetch("/api/research/readiness")).json();
  return { ready: readiness.ready, reason: readiness.reason ?? null };
});
researchReport.engineVerdict = null;
if (researchReport.ready) {
  await page.click(".research-form .analysis-run");
  // A reasoning model spends a minute or two thinking before the plan appears
  // (deepseek-v4-pro has been observed between 56s and 145s).
  await page.waitForSelector(".plan-card", { timeout: 400_000 });
  Object.assign(researchReport, await page.evaluate(() => {
    const levels = [...document.querySelectorAll(".plan-level")].map((node) => ({
      label: node.querySelector("span")?.textContent,
      value: node.querySelector("strong")?.textContent,
    }));
    const facts = [...document.querySelectorAll(".plan-facts div")].map((node) => ({
      label: node.querySelector("dt")?.textContent,
      value: node.querySelector("dd")?.textContent?.trim(),
    }));
    return {
      direction: document.querySelector(".plan-direction")?.textContent,
      levels,
      facts,
      hasProblems: Boolean(document.querySelector(".plan-problems")),
      verified: document.querySelector(".analysis-meta [data-verified]")?.getAttribute("data-verified") ?? null,
      verifiedLabel: document.querySelector(".analysis-meta [data-verified]")?.textContent ?? null,
      auditRows: document.querySelectorAll(".audit-evidence div").length,
      sendButton: Boolean(document.querySelector(".plan-send")),
    };
  }));

  // The plan must be handable to the paper-trading form.
  await page.click(".plan-send");
  await page.waitForSelector(".paper-form", { timeout: 20_000 });
  researchReport.handedToPaper = await page.evaluate(() => ({
    note: document.querySelector(".paper-from-plan")?.textContent?.trim() ?? null,
    rationale: document.querySelector(".paper-form textarea")?.value ?? "",
  }));
  researchReport.engineVerdict = researchResponse
    ? {
        verified: researchResponse.validation?.verified ?? null,
        planOk: researchResponse.validation?.plan?.ok ?? null,
        unsupported: researchResponse.validation?.unsupportedNumbers?.length ?? null,
        unsupportedSample: researchResponse.validation?.unsupportedNumbers?.slice(0, 3) ?? [],
        nonEvidence: researchResponse.validation?.nonEvidenceCitations ?? [],
        signFlipped: researchResponse.validation?.signFlippedNumbers?.length ?? null,
        derived: researchResponse.validation?.derivedNumbers?.length ?? null,
        directives: researchResponse.validation?.directives?.length ?? null,
        unknownKeys: researchResponse.validation?.unknownCitedKeys?.length ?? null,
      }
    : null;
} else {
  researchReport.blocked = await page.textContent(".research-blocked").catch(() => null);
}
report.research = researchReport;

report.perSymbol = perSymbol;
report.demoSymbols = perSymbol.filter((row) => row.meta?.includes("演示")).map((row) => row.symbol);
report.consoleErrors = consoleErrors;
report.failedRequests = failedRequests;

await browser.close();
console.log(JSON.stringify(report, null, 2));

const failures = [];
if (report.railCounts.length !== 2) failures.push(`expected 2 pools, saw ${report.railCounts.length}`);
if (report.demoSymbols.length) failures.push(`fell back to demo data: ${report.demoSymbols.join(", ")}`);
if (report.tagTally?.ETF !== 2) failures.push(`expected 2 ETF-tagged contracts, saw ${report.tagTally?.ETF ?? 0}`);
if (!report.settled?.status?.includes("BYBIT 实时")) failures.push(`settled status was not live: ${report.settled?.status}`);
if (report.settled?.alert) failures.push(`a warning alert stayed visible once settled: ${report.settled.alert}`);
const badHover = (report.hoverChecks ?? []).filter((check) => !check.matchesHigh || !check.matchesLow || !check.hasRange || check.resolvedIndex !== check.index);
if (badHover.length) failures.push(`chart hover mis-resolved at ${badHover.map((check) => `${check.frame}#${check.index}`).join(", ")}`);
if ((report.settings?.profiles ?? []).length !== (report.llmProxy?.profileCount ?? -1)) {
  failures.push(`settings listed ${report.settings?.profiles?.length} profiles but the gateway reports ${report.llmProxy?.profileCount}`);
}
if (!(report.settings?.profiles ?? []).length) failures.push("settings page listed no LLM profiles");
if (report.settings?.roles !== 4) failures.push(`expected 4 role mappings, saw ${report.settings?.roles}`);
if (report.settings?.bodyHasSecret) failures.push("a key-looking value was rendered into the page");
if (report.llmProxy?.status !== 200) failures.push(`/api/llm/settings did not proxy (status ${report.llmProxy?.status})`);
if (!report.chartAnalysis?.hasPrompt) failures.push("screenshot workspace did not render its upload prompt");
if (report.backtest?.error) failures.push(`engine backtest failed: ${report.backtest.error}`);
if (!report.backtest?.stats?.length) failures.push("engine backtest produced no summary stats");
if (!report.backtest?.assumptionsShown) failures.push("engine backtest did not surface its assumptions");
if (!report.paper?.hasForm) failures.push("paper trading form did not render");
if (report.paper?.errors) failures.push(`paper workspace reported: ${report.paper.errors}`);
if (!report.paper?.journalIntegrity?.includes("哈希校验通过")) failures.push(`journal integrity not reported: ${report.paper?.journalIntegrity}`);
if ((report.paper?.exportLinks ?? []).length !== 2) failures.push("journal export links missing");
if (!report.research) failures.push("research workspace did not render");
else if (report.research.ready) {
  if (!report.research.levels?.length) failures.push("research produced no plan levels");
  for (const level of report.research.levels ?? []) {
    if (!level.value || level.value === "—") failures.push(`plan level ${level.label} is empty`);
  }
  if (!report.research.facts?.some((fact) => fact.label?.includes("盈亏比"))) failures.push("plan did not expose the recomputed risk/reward");
  const verdict = report.research.engineVerdict;
  if (!verdict) failures.push("no engine verdict captured for the research pass");
  else if (!verdict.verified || !verdict.planOk) {
    failures.push(
      `engine rejected the plan: unsupported=${verdict.unsupported} directives=${verdict.directives} ` +
        `unknownKeys=${verdict.unknownKeys} nonEvidence=${JSON.stringify(verdict.nonEvidence)} signFlipped=${verdict.signFlipped}`,
    );
  } else if (report.research.verified !== "true") {
    failures.push(`page still shows "${report.research.verifiedLabel}" while the engine verified the plan`);
  }
  if (!report.research.sendButton) failures.push("plan cannot be handed to paper trading");
  if (!report.research.handedToPaper?.note) failures.push("plan was not prefilled into the paper order form");
} else if (!report.research.blocked) {
  failures.push("research was not ready and no reason was shown");
}
if (consoleErrors.length) failures.push(`console errors: ${consoleErrors.length}`);
if (failures.length) {
  console.error("\nFAILED:\n - " + failures.join("\n - "));
  process.exit(1);
}
console.log("\nAll smoke checks passed.");
