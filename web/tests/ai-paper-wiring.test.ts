/**
 * Wiring guards for the AI paper page: the shape of the calls, not the values.
 *
 * The defect these pin down was a *wiring* one - the start button called the enable-only
 * endpoint, and "run now" called the model with whatever the database happened to hold.
 * A pure-function test cannot see that: the functions were all correct, and the page
 * simply did not call them. So these read the sources and assert the calls that must
 * exist, and the ones that must not.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";

// Read from the project root, not from `import.meta.url`: the runner bundles every test
// into a temporary directory, so a URL relative to this module resolves to nothing.
const ROOT = process.cwd();
const WORKSPACE = readFileSync(join(ROOT, "src/components/ai-paper-workspace.tsx"), "utf8");
const SERVICE = readFileSync(join(ROOT, "src/services/ai-paper.ts"), "utf8");

/** Every call to `name(` in the source, with its argument text. */
function calls(source: string, name: string): string[] {
  const found: string[] = [];
  const pattern = new RegExp(`\\b${name}\\(`, "g");
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source)) !== null) {
    // Walk to the matching close paren so nested calls (`f(g(x))`) stay in one slice.
    let depth = 1;
    let index = match.index + match[0].length;
    while (index < source.length && depth > 0) {
      const char = source[index];
      if (char === "(") depth += 1;
      else if (char === ")") depth -= 1;
      index += 1;
    }
    found.push(source.slice(match.index + match[0].length, index - 1));
  }
  return found;
}

/** Split an argument list on top-level commas. */
function argumentsOf(call: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const char of call) {
    if ("([{".includes(char)) depth += 1;
    if (")]}".includes(char)) depth -= 1;
    if (char === "," && depth === 0) { parts.push(current.trim()); current = ""; continue; }
    current += char;
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

describe("the start button sends the page's rules", () => {
  it("always calls startAiPaper with a payload", () => {
    const startCalls = calls(WORKSPACE, "startAiPaper");
    assert.ok(startCalls.length > 0, "页面必须调用 startAiPaper");
    for (const call of startCalls) {
      const args = argumentsOf(call);
      assert.equal(
        args.length, 2,
        `startAiPaper 必须带上完整配置（当前调用只有 ${args.length} 个参数：${call}）——`
        + "不带请求体的启动会执行数据库里的旧规则，这正是要修的问题",
      );
      assert.ok(
        args[1].includes("payload"),
        `startAiPaper 的第二个参数必须是配置负载，当前是 ${args[1]}`,
      );
    }
  });

  it("builds that payload from the draft", () => {
    assert.ok(
      WORKSPACE.includes("const payload = configPayload(draft)"),
      "启动负载必须来自页面草稿 configPayload(draft)",
    );
  });

  it("the service sends a JSON body when a payload is given", () => {
    const body = startAiPaperBody();
    assert.ok(body.includes("JSON.stringify(payload)"), "带负载时必须以 JSON 请求体发送");
    assert.ok(
      /if\s*\(!payload\)\s*return\s+request<AiPaperProfile>\([\s\S]*?method:\s*"POST"\s*\}\)/.test(body),
      "无负载时才回退到旧的无请求体调用（仅供旧客户端兼容）",
    );
  });

  it("the workspace never uses the bodyless start", () => {
    for (const call of calls(WORKSPACE, "startAiPaper")) {
      assert.ok(argumentsOf(call).length === 2, `不得出现无请求体的 startAiPaper 调用：${call}`);
    }
  });
});

/** The body of the implementation overload (the two declarations above it say nothing). */
function startAiPaperBody(): string {
  const marker = SERVICE.indexOf("export function startAiPaper(id: string, payload?:");
  assert.ok(marker >= 0, "服务里必须有 startAiPaper 的实现");
  const end = SERVICE.indexOf("\n}", marker);
  return SERVICE.slice(marker, end === -1 ? SERVICE.length : end + 2);
}

describe("run-now uses the rules it just saved", () => {
  it("saves before calling the model, and only when the save landed", () => {
    const runCalls = calls(WORKSPACE, "runAiPaperNow");
    assert.ok(runCalls.length >= 1, "页面必须能立即评估");
    assert.ok(
      runCalls.some((call) => argumentsOf(call).join(",") === "saved.id"),
      "有一条路径必须用刚保存成功的那份配置调用模型（saved.id）；"
      + "否则会拿数据库里的旧规则调用模型，这正是要修的问题",
    );
    // And the save has to come first: the call is inside the same function after it.
    const saveAndEvaluate = WORKSPACE.indexOf("async function saveAndEvaluate");
    const saveCall = WORKSPACE.indexOf("saveAiPaperConfig", saveAndEvaluate);
    const runCall = WORKSPACE.indexOf("runAiPaperNow", saveAndEvaluate);
    assert.ok(saveAndEvaluate >= 0 && saveCall > saveAndEvaluate, "saveAndEvaluate 必须先保存");
    assert.ok(runCall > saveCall, "必须先保存成功再调用模型");
  });

  it("only calls the model directly when nothing is unsaved", () => {
    const direct = WORKSPACE.indexOf("runAiPaperNow(profile.id)");
    assert.ok(direct >= 0, "运行中/无改动时应直接调用");
    const context = WORKSPACE.slice(Math.max(0, direct - 260), direct);
    assert.ok(
      context.includes("!draftDirty"),
      "直接调用只能出现在「运行中或没有未保存改动」的分支里；"
      + `有脏草稿时必须先保存（当前上下文：${context.slice(-120)}）`,
    );
  });

  it("does not call the model when the save reported a mismatch", () => {
    const body = WORKSPACE.slice(
      WORKSPACE.indexOf("async function saveAndEvaluate"),
      WORKSPACE.indexOf("async function createSimulation"),
    );
    const mismatchGuard = body.indexOf("mismatches.length > 0");
    const runCall = body.indexOf("runAiPaperNow");
    assert.ok(mismatchGuard >= 0 && mismatchGuard < runCall,
      "回执不一致时必须提前返回，不得继续调用模型");
    assert.ok(body.includes("未调用模型"), "回执不一致时要说清没有调用模型");
  });
});

describe("a background refresh cannot overwrite what is being edited", () => {
  it("the interval refresh reconciles instead of adopting the server draft", () => {
    assert.ok(
      WORKSPACE.includes("setInterval(() => void load(true), 15_000)"),
      "15 秒轮询必须仍然存在（本次修的是它不得覆盖草稿，而不是去掉它）",
    );
    assert.ok(
      WORKSPACE.includes("reconcileDraft(current, next.profile, baseline)"),
      "刷新必须用 reconcileDraft 与基线比较，脏草稿要保留",
    );
  });

  it("switching instance points the guard at the new id before awaiting", () => {
    const body = WORKSPACE.slice(
      WORKSPACE.indexOf("async function selectSimulation"),
      WORKSPACE.indexOf("async function removeSimulation"),
    );
    const select = body.indexOf("selectProfile(profileId)");
    const load = body.indexOf("await load(");
    assert.ok(select >= 0 && load > select,
      "切换实例必须先把 selectedRef 指向新实例，再发起读取，否则迟到的响应会串到新实例");
  });
});
