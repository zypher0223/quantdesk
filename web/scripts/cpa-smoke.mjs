/**
 * Browser check for the CPA front-end.
 *
 * Three properties a unit test cannot reach are verified here, against a live engine:
 *
 * 1. with the CPA switch off, the chart is exactly what it was - no overlay element
 *    and *no* phase request;
 * 2. the existing chart controls (the Fibonacci overlay) still work with CPA present;
 * 3. with the switch on, the overlay draws phases, every marker that is drawn sits
 *    inside the plot, and clicking one opens the evidence card.
 *
 * Usage: node scripts/cpa-smoke.mjs [url]
 */
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";

const URL_BASE = process.argv[2] ?? "http://127.0.0.1:4173/";
const CACHE = path.join(process.env.HOME, "Library/Caches/ms-playwright");

function findChromium() {
  const candidates = [];
  if (!fs.existsSync(CACHE)) throw new Error(`no playwright cache at ${CACHE}`);
  for (const entry of fs.readdirSync(CACHE)) {
    if (entry.startsWith("chromium_headless_shell-")) {
      candidates.push(path.join(CACHE, entry, "chrome-headless-shell-mac-arm64", "chrome-headless-shell"));
    }
    if (entry.startsWith("chromium-")) {
      candidates.push(
        path.join(CACHE, entry, "chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"),
      );
    }
  }
  const found = candidates.find((candidate) => fs.existsSync(candidate));
  if (!found) throw new Error(`no cached chromium under ${CACHE}`);
  return found;
}

const results = [];
function check(label, ok, detail = "") {
  results.push({ label, ok });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? `：${detail}` : ""}`);
}

const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const phaseRequests = [];
const consoleErrors = [];
page.on("request", (request) => {
  if (request.url().includes("/api/cpa/phases")) phaseRequests.push(request.url());
});
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text().slice(0, 160));
});

await page.goto(URL_BASE, { waitUntil: "domcontentloaded" });
await page.waitForSelector(".universe-heading", { timeout: 25_000 });
await page.waitForSelector(".tradingview-chart canvas", { timeout: 25_000 });
// Candles stream in, and the chart locks its visible range on the first batch it sees.
// A phase overlay can only draw markers for bars that are both held and on screen, so
// wait for a full series and then widen the window.
await page.waitForFunction(() => (window.__qdChart?.bars?.length ?? 0) >= 100, null, { timeout: 30_000 });
for (let index = 0; index < 3; index += 1) {
  await page.locator(".chart-shell").getByRole("button", { name: "缩小K线图" }).first().click();
  await page.waitForTimeout(250);
}
await page.waitForTimeout(1500);
const held = await page.evaluate(() => window.__qdChart?.bars?.length ?? 0);
console.log(`  （图表持有 ${held} 根K线，视窗已放宽）`);

const cpaToggle = page.getByRole("button", { name: /CPA 周期/ });
check("工具条有 CPA 开关且默认关闭", (await cpaToggle.count()) === 1 && (await cpaToggle.first().getAttribute("aria-pressed")) === "false");
check("关闭状态下没有请求阶段接口", phaseRequests.length === 0, `requests=${phaseRequests.length}`);
check("关闭状态下没有 CPA 叠层", (await page.locator(".cpa-overlay").count()) === 0);

const fibToggle = page.getByRole("button", { name: /斐波那契/ });
check("斐波那契开关仍在", (await fibToggle.count()) === 1);
await fibToggle.first().click();
await page.waitForTimeout(400);
check("斐波那契可正常打开", (await fibToggle.first().getAttribute("aria-pressed")) === "true");
check("打开斐波那契后出现选点提示", (await page.locator(".fib-instruction").count()) === 1);
const fibPlot = await page.locator(".tradingview-chart").boundingBox();
if (fibPlot) {
  await page.mouse.click(fibPlot.x + fibPlot.width * 0.45, fibPlot.y + fibPlot.height * 0.72);
  await page.mouse.click(fibPlot.x + fibPlot.width * 0.62, fibPlot.y + fibPlot.height * 0.28);
  await page.waitForTimeout(400);
}
const fibLabels = page.locator(".fib-level-label");
check("选定两点后画出七档价位", (await fibLabels.count()) === 7);
const fibOverlayBox = await page.locator(".fib-overlay").boundingBox();
const fibLabelBoxes = await fibLabels.evaluateAll((labels) => labels.map((label) => {
  const box = label.getBBox();
  return { left: box.x, right: box.x + box.width };
}));
const fibLabelsInside = fibOverlayBox && fibLabelBoxes.every(
  (box) => box.left >= 0 && box.right <= fibOverlayBox.width - 64,
);
check(
  "斐波那契价位完整留在价格轴左侧",
  Boolean(fibLabelsInside),
  `labels=${fibLabelBoxes.length} plot=${Math.round(fibOverlayBox?.width ?? 0)}px`,
);
const fibAnchorBoxes = await page.locator(".fib-anchor-label").evaluateAll((labels) => labels.map((label) => {
  const box = label.getBBox();
  return { left: box.x, right: box.x + box.width, top: box.y };
}));
check(
  "锚点标签不会越过图表边缘",
  Boolean(fibOverlayBox) && fibAnchorBoxes.length === 2 && fibAnchorBoxes.every(
    (box) => box.left >= 0 && box.right <= fibOverlayBox.width && box.top >= 0,
  ),
);
await fibToggle.first().click();
await page.waitForTimeout(300);
check("斐波那契可正常关闭", (await fibToggle.first().getAttribute("aria-pressed")) === "false");

await cpaToggle.first().click();
await page.waitForTimeout(6000);
check("打开后请求了阶段接口", phaseRequests.length > 0, `requests=${phaseRequests.length}`);
const status = await page.locator(".cpa-status").first().innerText().catch(() => "");
check("状态行有阶段摘要或样本不足说明", /阶段|样本不足/.test(status), status.slice(0, 64));
check("画出了阶段叠层", (await page.locator(".cpa-overlay").count()) === 1);

const bands = await page.locator(".cpa-band").count();
check("有阶段背景带", bands > 0, `bands=${bands}`);
const anchorCount = await page.locator(".cpa-anchor").count();
check("有可点击的阶段标记", anchorCount > 0, `anchors=${anchorCount}`);

const confirmed = await page.locator(".cpa-anchor-confirmed").count();
const candidate = await page.locator(".cpa-anchor-candidate").count();
const observation = await page.locator(".cpa-anchor-observation").count();
check(
  "每个标记都有明确的分类（已确认/候选/观察）",
  confirmed + candidate + observation === anchorCount,
  `confirmed=${confirmed} candidate=${candidate} observation=${observation}`,
);

const dotGeometry = await page.locator(".cpa-anchor-dot").evaluateAll((els) =>
  els.map((el) => ({ cx: Number(el.getAttribute("cx")), cy: Number(el.getAttribute("cy")) })));
const overlayBox = await page.locator(".cpa-overlay").boundingBox();
const chartWidth = overlayBox?.width ?? 0;
const chartHeight = overlayBox?.height ?? 0;
const inside = dotGeometry.filter(
  (dot) => Number.isFinite(dot.cx) && Number.isFinite(dot.cy)
    && dot.cx >= 0 && dot.cx <= chartWidth && dot.cy >= 0 && dot.cy <= chartHeight,
);
check("所有标记都在图内（视图外或价格尺度外的已被丢弃）", inside.length === dotGeometry.length,
  `dots=${dotGeometry.length} inside=${inside.length}`);

if (inside.length > 0) {
  // Click the dot, not the group: the group's box also covers its text label, and its
  // centre sits ~50px away from the marker a reader would aim at.
  const dot = page.locator(".cpa-anchor-dot").last();
  const box = await dot.boundingBox();
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await page.waitForTimeout(700);
  const cards = await page.locator(".cpa-card").count();
  check("点击标记弹出证据卡片", cards === 1);
  if (cards === 1) {
    const text = await page.locator(".cpa-card").innerText();
    check("卡片含规则版本与数据版本", /规则版本/.test(text) && /数据版本/.test(text));
    check("卡片含识别理由", /识别理由/.test(text));
    check("卡片写明观察/候选不开仓", !/候选|观察/.test(text) || /不会在它上面开仓|只作风险提示/.test(text));
  }
}

await cpaToggle.first().click();
await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(500);
await fibToggle.first().click();
const mobileFibPlot = await page.locator(".tradingview-chart").boundingBox();
if (mobileFibPlot) {
  await page.mouse.click(mobileFibPlot.x + mobileFibPlot.width * 0.38, mobileFibPlot.y + mobileFibPlot.height * 0.7);
  await page.mouse.click(mobileFibPlot.x + mobileFibPlot.width * 0.66, mobileFibPlot.y + mobileFibPlot.height * 0.3);
  await page.waitForTimeout(400);
}
const mobileFibOverlay = await page.locator(".fib-overlay").boundingBox();
const mobileFibLabels = await page.locator(".fib-level-label").evaluateAll((labels) => labels.map((label) => {
  const box = label.getBBox();
  return { left: box.x, right: box.x + box.width };
}));
check(
  "390px 窄屏价位也完整留在价格轴左侧",
  Boolean(mobileFibOverlay) && mobileFibLabels.length === 7 && mobileFibLabels.every(
    (box) => box.left >= 0 && box.right <= mobileFibOverlay.width - 64,
  ),
  `labels=${mobileFibLabels.length} plot=${Math.round(mobileFibOverlay?.width ?? 0)}px`,
);
await fibToggle.first().click();

check("无控制台错误", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));

await browser.close();
const failed = results.filter((item) => !item.ok);
if (failed.length) {
  console.error(`\n失败 ${failed.length} 项：${failed.map((item) => item.label).join("；")}`);
  process.exit(1);
}
console.log("\n浏览器检查通过。");
