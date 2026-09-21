/**
 * Browser check for the AI paper "the page showed one configuration and the account ran
 * another" fix.
 *
 * What only a browser can verify, and how it is verified *without* spending model quota:
 *
 * 1. the operator's unsaved edits survive the 15-second auto-refresh. The engine only
 *    polls while some instance is running, so the profiles *index* is fulfilled with a
 *    stub that marks the temporary instance as running - the 15s timer then really
 *    fires, and every snapshot it fetches is a real response from the real server
 *    holding the OLD configuration. That is precisely the reported bug's shape.
 * 2. pressing save-and-start sends the page's rules, not the stored ones: the request
 *    body is captured and asserted. The response is stubbed, because letting a real
 *    profile start would hand it to the engine's 30-second monitor loop, which calls the
 *    configured model - the acceptance brief forbids a real model call.
 * 3. the page then displays the running rules from that response.
 * 4. no console errors.
 *
 * The real save-then-enable transaction, the database contents, the decision evidence and
 * the revision are covered against the real API by scripts/ai_paper_acceptance.py, which
 * runs on an isolated home with an injected decision stub.
 *
 * The temporary instance stays stopped in the database the whole time, and is deleted at
 * the end. Nothing else is touched.
 *
 * Usage: node scripts/ai-paper-smoke.mjs [url]
 */
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";

const URL_BASE = process.argv[2] ?? "http://127.0.0.1:4173/";
const API = new URL("/api/ai-paper", URL_BASE).toString();
const CACHE = path.join(process.env.HOME, "Library/Caches/ms-playwright");
const TEMP_NAME = `验收临时模拟${Date.now().toString().slice(-6)}`;

function findChromium() {
  const candidates = [];
  if (!fs.existsSync(CACHE)) throw new Error(`no playwright cache at ${CACHE}`);
  for (const entry of fs.readdirSync(CACHE)) {
    if (entry.startsWith("chromium_headless_shell-")) {
      candidates.push(path.join(CACHE, entry, "chrome-headless-shell-mac-arm64", "chrome-headless-shell"));
    }
    if (entry.startsWith("chromium-")) {
      candidates.push(path.join(CACHE, entry, "chromium-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"));
      candidates.push(path.join(CACHE, entry, "chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"));
    }
  }
  const found = candidates.find((candidate) => fs.existsSync(candidate));
  if (!found) throw new Error(`no cached chromium under ${CACHE}`);
  return found;
}

const results = [];
function check(label, ok, detail = "") {
  results.push({ label, ok, detail });
  console.log(`  ${ok ? "✓" : "✗"} ${label}${detail ? ` —— ${detail}` : ""}`);
}

async function api(method, suffix, body) {
  const response = await fetch(`${API}${suffix}`, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  let parsed = null;
  try { parsed = text ? JSON.parse(text) : null; } catch { /* keep raw */ }
  return { status: response.status, body: parsed, text };
}

const ALL = ["AAPLUSDT", "MSFTUSDT", "GOOGLUSDT", "AMZNUSDT", "NVDAUSDT", "METAUSDT",
             "TSLAUSDT", "SNDKUSDT", "MUUSDT", "AMDSTOCKUSDT", "NBISUSDT", "SPCXUSDT",
             "SKHYUSDT", "SOXLUSDT", "SOXSUSDT", "BTCUSDT", "ETHUSDT"];

let profileId = null;
let browser = null;
let baseSnapshot = null;
let capturedStartBody = null;
const consoleErrors = [];

try {
  console.log(`目标页面：${URL_BASE}`);
  console.log("\n[准备] 建立一个「稳妥 + 全部 17 个合约」的临时实例（全程保持停止）");
  const created = await api("POST", "/profiles", {
    name: TEMP_NAME, initialCash: 100_000, maxLeverage: 3,
    horizon: "short", style: "conservative", fibOnly: false, symbols: ALL,
  });
  if (created.status !== 201) throw new Error(`创建临时实例失败：${created.status} ${created.text}`);
  profileId = created.body.profile.id;
  baseSnapshot = created.body;
  check("临时实例已建立", created.status === 201, profileId);

  browser = await chromium.launch({ executablePath: findChromium(), headless: true });
  const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("pageerror", (error) => consoleErrors.push(String(error)));

  // The engine polls only while something runs. Mark *only the temporary instance* as
  // running in the index so the real 15-second timer fires; every snapshot the page then
  // reads is a real response carrying the old stored configuration.
  await page.route("**/api/ai-paper/profiles", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    for (const row of payload.profiles ?? []) {
      if (row.profile?.id === profileId) row.profile.enabled = true;
    }
    await route.fulfill({ response, json: payload });
  });
  await page.route(`**/api/ai-paper/profiles/${profileId}/start`, async (route) => {
    capturedStartBody = JSON.parse(route.request().postData() ?? "null");
    // Answer with what a correct engine would have confirmed, without starting anything.
    const profile = { ...baseSnapshot.profile, ...capturedStartBody, enabled: true, config_revision: 2 };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...baseSnapshot, profile }),
    });
  });

  await page.goto(URL_BASE, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "交易日志" }).first().click();
  await page.getByRole("tab", { name: "AI 自动模拟" }).click();
  await page.getByRole("button", { name: new RegExp(TEMP_NAME) }).click();
  // Wait for the switch to land before touching the form: selecting an instance rebuilds
  // the draft from that instance's profile, so editing too early would be discarded - by
  // design, and it would look like the bug under test.
  await page.waitForFunction(
    (name) => document.querySelector(".ai-paper-hero h3")?.textContent === name,
    TEMP_NAME,
    { timeout: 20_000 },
  );
  check("已切到临时实例", (await page.locator(".ai-paper-hero h3").innerText()) === TEMP_NAME);

  console.log("\n[1] 在页面上改成 短线 + 激进，取消 SOXL、SOXS（不点保存）");
  await page.locator(".ai-paper-horizon button", { hasText: "短线" }).click();
  await page.locator(".ai-paper-styles button", { hasText: "激进" }).click();
  for (const symbol of ["SOXL", "SOXS"]) {
    await page.locator(".ai-paper-symbol-grid label", { hasText: new RegExp(`^${symbol}`) })
      .locator("input[type=checkbox]").uncheck();
  }
  await page.waitForTimeout(300);
  check("出现「有未保存的规则修改」提示",
    await page.getByText("有未保存的规则修改").isVisible().catch(() => false));
  const pendingText = await page.locator(".ai-paper-summary.pending span").innerText();
  check("「即将启动」摘要来自草稿，且点名排除了 SOXL、SOXS",
    pendingText.includes("短线") && pendingText.includes("激进")
    && pendingText.includes("15 个合约") && pendingText.includes("SOXL") && pendingText.includes("SOXS"),
    pendingText);

  console.log("\n[2] 等待超过两个自动刷新周期（15 秒/次）：草稿不得被服务器旧配置覆盖");
  const storedBefore = (await api("GET", `/profiles/${profileId}`)).body.profile;
  check("服务器此刻确实是旧配置（稳妥 + 17 个合约）",
    storedBefore.style === "conservative" && storedBefore.symbols.length === 17,
    `${storedBefore.style} / ${storedBefore.symbols.length}`);
  await page.waitForTimeout(34_000);
  const styleSelected = (await page.locator(".ai-paper-styles button.selected").innerText()).split("\n")[0];
  const horizonSelected = (await page.locator(".ai-paper-horizon button.selected").innerText()).split("\n")[0];
  const soxlChecked = await page.locator(".ai-paper-symbol-grid label", { hasText: /^SOXL/ })
    .locator("input[type=checkbox]").isChecked();
  const soxsChecked = await page.locator(".ai-paper-symbol-grid label", { hasText: /^SOXS/ })
    .locator("input[type=checkbox]").isChecked();
  const afterPollText = await page.locator(".ai-paper-summary.pending span").innerText();
  check("刷新后风格仍是「激进」", styleSelected.includes("激进"), styleSelected);
  check("刷新后周期仍是「短线」", horizonSelected.includes("短线"), horizonSelected);
  check("刷新后 SOXL 仍是未选中", soxlChecked === false);
  check("刷新后 SOXS 仍是未选中", soxsChecked === false);
  check("刷新后摘要没有被改回稳妥/全选",
    afterPollText.includes("激进") && afterPollText.includes("15 个合约"), afterPollText);
  check("未保存提示仍在",
    await page.getByText("有未保存的规则修改").isVisible().catch(() => false));

  console.log("\n[3] 直接点「保存规则并启动」：请求体必须是页面上的规则");
  await page.getByRole("button", { name: "保存规则并启动" }).click();
  await page.waitForTimeout(1500);
  check("捕获到启动请求", capturedStartBody !== null);
  check("请求体风格是 aggressive", capturedStartBody?.style === "aggressive", String(capturedStartBody?.style));
  check("请求体周期是 short", capturedStartBody?.horizon === "short", String(capturedStartBody?.horizon));
  check("请求体杠杆是 3（页面显示值）", Number(capturedStartBody?.maxLeverage) === 3, String(capturedStartBody?.maxLeverage));
  check("请求体不含 SOXLUSDT", !(capturedStartBody?.symbols ?? []).includes("SOXLUSDT"));
  check("请求体不含 SOXSUSDT", !(capturedStartBody?.symbols ?? []).includes("SOXSUSDT"));
  check("请求体合约数为 15", (capturedStartBody?.symbols ?? []).length === 15,
    String((capturedStartBody?.symbols ?? []).length));

  console.log("\n[4] 页面随后显示服务器确认的「当前运行」配置");
  const runningText = await page.locator(".ai-paper-summary.running span").innerText();
  check("「当前运行」摘要来自服务器回执",
    runningText.includes("短线") && runningText.includes("激进") && runningText.includes("15 个合约"),
    runningText);
  check("未保存提示已消失", (await page.getByText("有未保存的规则修改").count()) === 0);

  console.log("\n[5] 运行中表单锁定");
  check("运行中风格按钮被锁定", await page.locator(".ai-paper-styles button").first().isDisabled());
  check("运行中合约复选框被锁定",
    await page.locator(".ai-paper-symbol-grid input[type=checkbox]").first().isDisabled());

  console.log("\n[6] 无控制台错误");
  check("浏览器控制台无错误", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));

  const untouched = (await api("GET", `/profiles/${profileId}`)).body.profile;
  check("数据库里的临时实例始终没有被真的启动", untouched.enabled === false,
    `enabled=${untouched.enabled}`);
} catch (reason) {
  check("脚本执行完成", false, reason instanceof Error ? reason.message : String(reason));
} finally {
  if (profileId) {
    console.log("\n[清理] 删除临时实例（保留其它实例与历史）");
    await api("POST", `/profiles/${profileId}/stop`);
    const removed = await api("DELETE", `/profiles/${profileId}?confirm=true`);
    check("临时实例已删除", removed.status === 200, String(removed.status));
    check("删除后查询返回 404", (await api("GET", `/profiles/${profileId}`)).status === 404);
  }
  if (browser) await browser.close();
}

const failed = results.filter((item) => !item.ok);
console.log(`\n${"=".repeat(60)}`);
if (failed.length > 0) {
  console.log(`浏览器验收失败 ${failed.length}/${results.length} 项：`);
  for (const item of failed) console.log(`  - ${item.label}${item.detail ? ` (${item.detail})` : ""}`);
  process.exit(1);
}
console.log(`浏览器验收通过：${results.length}/${results.length} 项`);
