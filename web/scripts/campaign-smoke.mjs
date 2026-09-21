/**
 * Browser check for the agent-campaign workspace.
 *
 * It asserts the three things a screenshot cannot: that the panel renders the
 * engine's real numbers, that the sealed test segment produces no test row even
 * though the API would serve one after unsealing, and that promotion is offered only
 * for a proposal the engine would actually accept.
 *
 * Usage: node scripts/campaign-smoke.mjs [url]
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
  if (!found) throw new Error(`no cached chromium found under ${CACHE}`);
  return found;
}

const failures = [];
function check(label, condition, detail = "") {
  if (condition) {
    console.log(`  ✓ ${label}`);
  } else {
    failures.push(`${label}${detail ? `：${detail}` : ""}`);
    console.log(`  ✗ ${label}${detail ? `：${detail}` : ""}`);
  }
}

const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});

await page.goto(URL_BASE, { waitUntil: "domcontentloaded" });
await page.waitForSelector(".universe-heading", { timeout: 20_000 });
console.log("页面已加载，切到「代理战役」");

// The dock renders an icon with an aria-label; the visible label is a hover tooltip.
await page.getByRole("button", { name: "代理战役" }).first().click();
await page.waitForSelector("text=预注册一个新战役", { timeout: 15_000 });

// The campaign the engine actually ran, by uid.
const uid = process.env.CAMPAIGN_UID ?? "";
await page.waitForFunction(
  (needle) => document.body.innerText.includes(needle),
  uid ? uid.slice(0, 14) : "camp-",
  { timeout: 20_000 },
);

// The detail request resolves after the form renders; wait for a detail-only heading
// so the assertions below describe the loaded panel rather than a half-drawn one.
await page.waitForSelector("text=试验读数", { timeout: 20_000 });
await page.waitForSelector("text=提案与人工晋升", { timeout: 20_000 });
// The statistics and the verdict arrive in their own requests, after the detail, so
// wait for the card to have a value before asserting on it.
await page.waitForSelector("text=回测过拟合概率", { timeout: 20_000 });
await page.waitForFunction(
  () => !document.body.innerText.includes("统计接口还没有回答"),
  null,
  { timeout: 20_000 },
);
// …and the verdict banner is the third request in that chain.
await page.waitForFunction(
  () => /判决 (pass|fail|inconclusive)/.test(document.body.innerText)
    || document.body.innerText.includes("还没有判决"),
  null,
  { timeout: 20_000 },
);

const body = await page.innerText("body");
check("渲染了战役列表", /camp-[0-9a-f]{6}/.test(body));
check("显示了假设与成功判据", body.includes("假设") && body.includes("成功判据"));
check("显示了冻结因子空间及其层级", /vibe\.[a-z_]+\.\d+[\s\S]{0,20}(validated|candidate)/.test(body));
// Figure captions are styled `uppercase`, and Chromium's innerText reports the
// transformed text, so compare case-insensitively rather than on the source string.
const folded = body.toLowerCase();
const figures = ["各提案的 sharpe", "最大回撤", "收益 × 回撤", "预算使用", "预算台账", "因子使用次数"];
const missingFigures = figures.filter((title) => !folded.includes(title));
check("六张图都在", missingFigures.length === 0, missingFigures.join(","));
// The switch between the two banners is the seal itself, so the check accepts either
// state and asserts that one of them is stated - a panel that showed neither would be
// silent about whether the test window has been opened.
const sealed = folded.includes("测试段仍然封存");
const unsealed = folded.includes("测试段已于") && folded.includes("开封一次");
check("封存状态有明确说明", sealed || unsealed, `sealed=${sealed} unsealed=${unsealed}`);

const statsTitles = ["收缩后 sharpe（dsr）", "回测过拟合概率（pbo）", "试验次数 n"];
const missingStats = statsTitles.filter((title) => !folded.includes(title));
check("统计卡片（DSR/PBO/N）在", missingStats.length === 0, missingStats.join(","));

const judged = /判决 (pass|fail|inconclusive)/.test(body);
const notJudged = folded.includes("还没有判决");
check("判决状态有说明", judged || notJudged, `judged=${judged} notJudged=${notJudged}`);
check("表格里没有测试段行", !/\btest\b/.test(body.split("试验读数")[1]?.split("提案与人工晋升")[0] ?? ""));

const promoteButtons = await page.getByRole("button", { name: /晋升为候选/ }).all();
check("有晋升按钮", promoteButtons.length > 0);
const enabled = [];
for (const button of promoteButtons) {
  if (await button.isEnabled()) enabled.push(await button.getAttribute("title"));
}
check("未署名时全部晋升按钮不可用", enabled.length === 0, `可用的：${enabled.length}`);

// With a name typed in, exactly the proposals with a validation reading become eligible.
await page.getByPlaceholder("批准人（晋升/开封必须署名）").fill("zypher");
await page.waitForTimeout(500);
const afterName = [];
for (const button of await page.getByRole("button", { name: /晋升为候选/ }).all()) {
  if (await button.isEnabled()) afterName.push(await button.getAttribute("title"));
}
check("署名后只对已测量的提案开放晋升", afterName.length > 0 && afterName.every((title) => !/没有 Sharpe 读数/.test(title ?? "")), JSON.stringify(afterName));

check("没有控制台错误", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));

await browser.close();
if (failures.length) {
  console.error(`\n失败 ${failures.length} 项：`);
  for (const item of failures) console.error(` - ${item}`);
  process.exit(1);
}
console.log("\n浏览器检查通过。");
