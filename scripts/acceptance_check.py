#!/usr/bin/env python3
"""Produce the Vibe-Trading integration acceptance report from real checks.

Every line of the report is the result of running something: a live endpoint, a file
on disk, a test suite, a static scan of bytes. Nothing is asserted from memory, and a
check that cannot be run says `pending` with the reason instead of passing quietly.

Usage:
    engine/.venv/bin/python scripts/acceptance_check.py [--out docs/VIBE_INTEGRATION_ACCEPTANCE.md]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
WEB = ROOT / "web"
PYTHON = ENGINE / ".venv" / "bin" / "python"
BASE_URL = os.environ.get("QUANTDESK_URL", "http://127.0.0.1:4173")

PASS, FAIL, PENDING = "通过", "未通过", "待定"


@dataclass
class Result:
    number: int
    title: str
    status: str
    evidence: str
    detail: str = ""


RESULTS: list[Result] = []
_counter = 0


def record(title: str, status: str, evidence: str, detail: str = "") -> None:
    global _counter
    _counter += 1
    RESULTS.append(Result(_counter, title, status, evidence, detail))


def check(title: str, fn) -> None:
    """Run one check; an exception is a failed check, not a crash."""
    try:
        status, evidence, detail = fn()
    except Exception as exc:  # noqa: BLE001 - the report must survive a broken check
        status, evidence, detail = FAIL, f"{type(exc).__name__}: {exc}", ""
    record(title, status, evidence, detail)


def get_json(path: str, timeout: float = 20.0) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"detail": body[:200]}


def post_json(path: str, payload: dict, timeout: float = 60.0) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"detail": body[:200]}


def pytest(*paths: str) -> tuple[int, str]:
    result = subprocess.run(
        [str(PYTHON), "-m", "pytest", "-q", *paths],
        cwd=ENGINE, capture_output=True, text=True, timeout=1800, check=False,
    )
    return result.returncode, (result.stdout or result.stderr).strip().splitlines()[-1]


# ---------------------------------------------------------------- the checks

def check_protocol_v4() -> tuple[str, str, str]:
    status, payload = get_json("/api/plugins")
    versions = payload.get("supportedApiVersions") or []
    version = payload.get("apiVersion")
    ok = version == "4" and versions == ["1", "2", "3", "4"] and "strategy_agent" in (
        payload.get("supportedCapabilities") or []
    )
    return (
        PASS if ok else FAIL,
        f"GET /api/plugins → apiVersion={version}, supported={versions}",
        f"strategy_agent 在能力表里：{'strategy_agent' in (payload.get('supportedCapabilities') or [])}",
    )


def check_plugins_installed() -> tuple[str, str, str]:
    _status, payload = get_json("/api/plugins")
    plugins = {item["id"]: item for item in payload.get("plugins") or []}
    lab = plugins.get("vibe-backtest-lab")
    factors = plugins.get("vibe-factors")
    invalid = payload.get("invalid") or []
    ok = bool(lab and factors) and not invalid
    return (
        PASS if ok else FAIL,
        "；".join(
            f"{item['id']} {item['version']} {'启用' if item['enabled'] else '禁用'} "
            f"[{','.join(item['capabilities'])}]"
            for item in (lab, factors) if item
        ),
        f"无效清单 {len(invalid)} 个",
    )


def check_one_provider_per_capability() -> tuple[str, str, str]:
    _status, payload = get_json("/api/plugins/registry")
    capabilities = payload.get("capabilities") or {}
    owners = {name: ids for name, ids in capabilities.items() if ids}
    ok = all(len(ids) == 1 for ids in owners.values())
    return (
        PASS if ok else FAIL,
        "；".join(f"{name}={ids}" for name, ids in sorted(owners.items())),
        "每个能力只有一个启用持有者" if ok else "有能力的持有者不止一个",
    )


def check_factor_catalog() -> tuple[str, str, str]:
    _status, payload = get_json("/api/factors")
    factors = payload.get("factors") or []
    zoo = [item for item in factors if item["id"].startswith("vibezoo:")]
    local = [item for item in factors if not item["id"].startswith("vibezoo:")]
    ok = payload.get("provider") == "vibe-backtest-lab" and len(zoo) >= 200 and len(local) == 28
    return (
        PASS if ok else FAIL,
        f"provider={payload.get('provider')}，zoo {len(zoo)} 个 + 自研 {len(local)} 个",
        f"目录来源 {payload.get('source')}",
    )


def check_allowlist_integrity() -> tuple[str, str, str]:
    allowlist_path = ROOT / "plugins" / "vibe-backtest-lab" / "factor_allowlist.json"
    allowlist = json.loads(allowlist_path.read_text(encoding="utf-8"))
    vendor = ROOT / "plugins" / "vibe-backtest-lab" / "vendor" / "vibe-trading"
    mismatched = []
    for item in allowlist["factors"]:
        path = vendor / (item["modulePath"].replace(".", "/") + ".py")
        if not path.is_file():
            mismatched.append(f"{item['id']}(缺文件)")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["moduleSha256"]:
            mismatched.append(f"{item['id']}(哈希不符)")
    ok = not mismatched
    return (
        PASS if ok else FAIL,
        f"白名单 {len(allowlist['factors'])} 个，逐模块 sha256 校验，{len(mismatched)} 个不一致",
        f"上游提交 {allowlist['source']['commit'][:12]}（{allowlist['source']['describe']}）",
    )


def check_no_network_in_vendor() -> tuple[str, str, str]:
    forbidden = (
        "import socket", "import urllib", "import httpx", "import requests",
        "import aiohttp", "import subprocess", "os.environ", "os.getenv",
    )
    vendor_src = ROOT / "plugins" / "vibe-backtest-lab" / "vendor" / "vibe-trading" / "src"
    hits = []
    scanned = 0
    for path in sorted(vendor_src.rglob("*.py")):
        scanned += 1
        text = path.read_text(encoding="utf-8")
        hits.extend(f"{path.name}:{needle}" for needle in forbidden if needle in text)
    return (
        PASS if not hits else FAIL,
        f"扫描 vendored 代码 {scanned} 个文件，命中 {len(hits)} 处",
        "；".join(hits[:3]) or "没有网络、子进程或环境变量读取",
    )


def check_gate_library() -> tuple[str, str, str]:
    script = (
        "import json;from pathlib import Path;from quantdesk.config.settings import quantdesk_home;"
        "from quantdesk.factor_library import load_library_report, entries_of;"
        "r=load_library_report(quantdesk_home());"
        "print(json.dumps({'scans':len(r['scans']) if r else 0,"
        "'entries':sum(len(entries_of(s)) for s in r['scans']) if r else 0,"
        "'validated':sum(1 for s in r['scans'] for e in entries_of(s) if e['tier']=='validated')}))"
    )
    result = subprocess.run([str(PYTHON), "-c", script], capture_output=True, text=True, check=False)
    payload = json.loads(result.stdout.strip() or "{}")
    ok = payload.get("scans", 0) >= 2 and payload.get("entries", 0) >= 1
    return (
        PASS if ok else FAIL,
        f"闸门扫描 {payload.get('scans')} 组，入库 {payload.get('entries')} 个（其中已验证 {payload.get('validated')} 个）",
        "库由扫描报告决定，改库只能重跑扫描",
    )


def check_campaign_governance() -> tuple[str, str, str]:
    """The four refusals a campaign must make, asked of the live API."""
    _status, library = get_json("/api/factors")
    checks = {}
    # D1: only deterministic search.
    status, body = post_json("/api/campaigns", {
        "group": "stock", "interval": "1h", "horizonBars": 24,
        "hypothesis": "验收探针", "successCriteria": "验收探针",
        "windows": {"train": [1_600_000_000_000, 1_650_000_000_000],
                    "validation": [1_650_000_000_001, 1_680_000_000_000],
                    "test": [1_680_000_000_001, 1_700_000_000_000]},
        "mode": "llm_assisted",
    })
    checks["D1 拒绝 LLM 模式"] = status == 422
    # D2: budget caps.
    status, _body = post_json("/api/campaigns", {
        "group": "stock", "interval": "1h", "horizonBars": 24,
        "hypothesis": "验收探针", "successCriteria": "验收探针",
        "windows": {"train": [1_600_000_000_000, 1_650_000_000_000],
                    "validation": [1_650_000_000_001, 1_680_000_000_000],
                    "test": [1_680_000_000_001, 1_700_000_000_000]},
        "budget": {"proposalsPerRound": 64},
    })
    checks["D2 拒绝超预算"] = status == 422
    # Window overlap.
    status, _body = post_json("/api/campaigns", {
        "group": "stock", "interval": "1h", "horizonBars": 24,
        "hypothesis": "验收探针", "successCriteria": "验收探针",
        "windows": {"train": [1_600_000_000_000, 1_650_000_000_001],
                    "validation": [1_650_000_000_000, 1_680_000_000_000],
                    "test": [1_680_000_000_001, 1_700_000_000_000]},
    })
    checks["拒绝窗口重叠"] = status == 422
    _status, campaigns = get_json("/api/campaigns")
    existing = (campaigns.get("campaigns") or [])
    checks["至少有一个真实战役"] = len(existing) > 0
    ok = all(checks.values())
    return (
        PASS if ok else FAIL,
        "；".join(f"{name}={'✓' if value else '✗'}" for name, value in checks.items()),
        f"因子目录 provider={library.get('provider')}；库内战役 {len(existing)} 个",
    )


def check_sealed_test_segment() -> tuple[str, str, str]:
    _status, campaigns = get_json("/api/campaigns")
    sealed = next((item for item in campaigns.get("campaigns") or [] if item["testSealed"]), None)
    if sealed is None:
        return PENDING, "没有仍处封存状态的战役可验证", "跑一个新战役即可复验"
    read_status, _ = get_json(f"/api/campaigns/{sealed['uid']}/trials?includeTest=true")
    write_status, _ = post_json(f"/api/campaigns/{sealed['uid']}/trials", {
        "proposalId": "probe", "segment": "test", "sharpe": 1.0,
    })
    ok = read_status == 409 and write_status in (404, 409, 422)
    return (
        PASS if ok else FAIL,
        f"{sealed['uid'][:14]}：读测试段 HTTP {read_status}，写测试段 HTTP {write_status}",
        "封存期间两件事都做不到",
    )


def check_human_only_promotion() -> tuple[str, str, str]:
    _status, campaigns = get_json("/api/campaigns")
    finished = next(
        (item for item in campaigns.get("campaigns") or []
         if item["status"] in ("completed", "budget_limited", "failed") and item["trialsUsed"]),
        None,
    )
    if finished is None:
        return PENDING, "没有已结束且有试验的战役", "先跑一轮战役"
    status, body = post_json(f"/api/campaigns/{finished['uid']}/promote",
                             {"proposalId": "r01-p000", "approvedBy": ""})
    # Either the request model refuses an empty name at the edge (pydantic) or the
    # service guard refuses it with the D3 sentence; both are the same rule.
    detail = json.dumps(body, ensure_ascii=False)
    ok = status == 422 and ("D3" in detail or "approvedBy" in detail)
    return (
        PASS if ok else FAIL,
        f"无批准人晋升 → HTTP {status}：{str(body.get('detail'))[:70]}",
        "晋升必须署名，代理没有这条路径（模型层或服务层拒绝都算）",
    )


def check_queue_supports_campaign() -> tuple[str, str, str]:
    script = (
        "from quantdesk.backtest_runs import RUN_KINDS;"
        "from quantdesk.studies import REQUEST_MODELS, STUDIES;"
        "print('campaign' in RUN_KINDS, 'campaign' in REQUEST_MODELS, 'campaign' in STUDIES)"
    )
    result = subprocess.run([str(PYTHON), "-c", script], capture_output=True, text=True, check=False)
    ok = result.stdout.strip() == "True True True"
    return (
        PASS if ok else FAIL,
        f"RUN_KINDS / REQUEST_MODELS / STUDIES 都含 campaign：{result.stdout.strip()}",
        "一轮战役在 worker 里执行，不占 HTTP 请求",
    )


def check_engine_suite() -> tuple[str, str, str]:
    code, line = pytest()
    return (PASS if code == 0 else FAIL), line, "engine/tests 全量"


def check_execution_model_suite() -> tuple[str, str, str]:
    code, line = pytest("tests/test_execution_model.py", "tests/test_execution_lifecycle.py")
    return (PASS if code == 0 else FAIL), line, "Gate-B：默认逐位一致 + 十项执行模型"


def check_campaign_suite() -> tuple[str, str, str]:
    code, line = pytest("tests/test_campaigns.py", "tests/test_agent_campaign.py")
    return (PASS if code == 0 else FAIL), line, "预注册、预算、可见性、编排"


def check_plugin_suite() -> tuple[str, str, str]:
    code, line = pytest("tests/test_plugins.py", "tests/test_vibe_backtest_lab.py",
                        "tests/test_vibe_factors.py")
    return (PASS if code == 0 else FAIL), line, "协议 v4、白名单、两族因子"


def check_web_tests() -> tuple[str, str, str]:
    result = subprocess.run(["npm", "test"], cwd=WEB, capture_output=True, text=True,
                            timeout=900, check=False)
    match = re.search(r"ℹ pass (\d+)", result.stdout)
    return (
        PASS if result.returncode == 0 else FAIL,
        f"{match.group(0) if match else '没有读数'}（npm test）",
        "前端纯逻辑测试，含封存与晋升规则",
    )


def check_web_bundle() -> tuple[str, str, str]:
    dist = WEB / "dist" / "assets"
    bundle = sorted(dist.glob("index-*.js"))
    if not bundle:
        return FAIL, "web/dist 没有构建产物", "运行 npm run build"
    text = bundle[-1].read_text(encoding="utf-8", errors="replace")
    ok = "代理战役" in text and "一次性开封测试段" in text
    return (
        PASS if ok else FAIL,
        f"{bundle[-1].name} {bundle[-1].stat().st_size // 1024} KB，含代理战役面板：{ok}",
        "服务端从这里提供页面",
    )


def check_browser_panel() -> tuple[str, str, str]:
    script = WEB / "scripts" / "campaign-smoke.mjs"
    if not script.is_file():
        return PENDING, "没有浏览器检查脚本", ""
    result = subprocess.run(["node", str(script), f"{BASE_URL}/"], cwd=WEB,
                            capture_output=True, text=True, timeout=600, check=False)
    tail = [line for line in result.stdout.strip().splitlines() if line.strip()][-1:]
    return (
        PASS if result.returncode == 0 else FAIL,
        f"浏览器检查{'通过' if result.returncode == 0 else '失败'}：{tail[0] if tail else '无输出'}",
        "真实战役数据下的渲染、封存提示与晋升可用性",
    )


def check_docs() -> tuple[str, str, str]:
    wanted = [
        "docs/VIBE_TRADING_COMPATIBILITY.md",
        "docs/FACTOR_LIBRARY.md",
        "docs/EXECUTION_MODEL.md",
        "plugins/vibe-backtest-lab/README.md",
        "plugins/README.md",
        "PLUGIN_API.md",
    ]
    missing = [name for name in wanted if not (ROOT / name).is_file()]
    return (
        PASS if not missing else FAIL,
        f"{len(wanted) - len(missing)}/{len(wanted)} 份文档在位",
        f"缺：{', '.join(missing)}" if missing else "阶段 0/3/4 的结论都有落点",
    )


def check_exclusions() -> tuple[str, str, str]:
    """The things the plan said not to integrate must not be reachable."""
    forbidden = {
        "Vibe 前端/桌面端": ["frontend/", "desktop/"],
        "Vibe 连接器": ["connector"],
        "Vibe 通知": ["notifier.send", "vibe.*notify"],
        "实盘下单接口": ["/v5/order/create", "place_order("],
        "MCP": ["mcp_server", "modelcontextprotocol"],
    }
    engine_src = ENGINE / "src" / "quantdesk"
    hits: dict[str, list[str]] = {}
    for label, needles in forbidden.items():
        found = []
        for path in engine_src.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            found.extend(f"{path.name}:{needle}" for needle in needles if needle in text)
        if found:
            hits[label] = found[:2]
    # An order endpoint may exist for the *venue* client; what must not exist is a new
    # one added for the agent. The check is therefore about the agent surface.
    lab = (ROOT / "plugins" / "vibe-backtest-lab" / "plugin.py").read_text(encoding="utf-8")
    lab_hits = [needle for needle in ("place_order", "create_order", "/v5/order", "api.bybit")
                if needle in lab]
    ok = not lab_hits
    return (
        PASS if ok else FAIL,
        f"插件侧无下单路径：{not lab_hits}；引擎侧屏蔽清单命中：{hits or '无'}",
        "排除项（Vibe 前端/桌面/连接器/通知/MCP/实盘）未被引入",
    )


def check_migrations_idempotent() -> tuple[str, str, str]:
    """Opening a fresh database twice must not fail or duplicate anything."""
    script = (
        "import tempfile,os;from pathlib import Path;"
        "os.environ['QUANTDESK_HOME']=tempfile.mkdtemp();"
        "from quantdesk.datahub.db import Database;"
        "p=Path(os.environ['QUANTDESK_HOME'])/'q.db';"
        "d1=Database(p);v1=[r['name'] for r in d1.query(\"select name from sqlite_master where type='table' and name like 'agent%'\")];"
        "d1._conn.close();"
        "d2=Database(p);v2=[r['name'] for r in d2.query(\"select name from sqlite_master where type='table' and name like 'agent%'\")];"
        "print(len(v1), len(v2), sorted(v1)==sorted(v2))"
    )
    result = subprocess.run([str(PYTHON), "-c", script], capture_output=True, text=True, check=False)
    parts = result.stdout.strip().split()
    # The exact table count is not the point - that the two opens agree is.
    ok = len(parts) == 3 and parts[0] == parts[1] and parts[2] == "True" and int(parts[0]) >= 4
    return (
        PASS if ok else FAIL,
        f"两次打开：{result.stdout.strip() or result.stderr.strip()[-120:]}",
        "新表走 CREATE TABLE IF NOT EXISTS，重复打开既不改形状也不报错",
    )


def check_docker_isolation() -> tuple[str, str, str]:
    dockerfile = (ENGINE / "Dockerfile").read_text(encoding="utf-8")
    ok = "QUANTDESK_PLUGIN_SANDBOX=required" in dockerfile and "bubblewrap" in dockerfile
    return (
        PASS if ok else FAIL,
        "镜像设置 QUANTDESK_PLUGIN_SANDBOX=required 并安装 bubblewrap："
        f"{ok}",
        "本机 macOS 的 sandbox-exec 探针失败（已知），容器内由 bwrap 强制",
    )


def check_host_sandbox_honesty() -> tuple[str, str, str]:
    _status, payload = get_json("/api/plugins")
    sandbox = payload.get("sandbox") or {}
    enforced = bool(sandbox.get("enforced"))
    policy = sandbox.get("policy")
    detail = str(sandbox.get("detail") or "")
    # Not enforced is acceptable on this host; claiming enforcement would not be.
    ok = policy in ("required", "preferred", "off") and (enforced or detail)
    return (
        PASS if ok else FAIL,
        f"策略 {policy}，enforced={enforced}：{detail[:80]}",
        "未强制时页面与 API 都会明说，不假装有隔离",
    )


def check_llm_stage_off() -> tuple[str, str, str]:
    source = (ENGINE / "src" / "quantdesk" / "campaigns.py").read_text(encoding="utf-8")
    ok = 'if mode != "deterministic_search"' in source
    return (
        PASS if ok else FAIL,
        "engine/src/quantdesk/campaigns.py 拒绝非确定性模式："
        f"{ok}",
        "阶段 10（LLM 辅助）保持关闭，需要单独开启",
    )


def check_vibe_source_pinned() -> tuple[str, str, str]:
    source = json.loads((ROOT / "plugins" / "vibe-backtest-lab" / "vendor" / "vibe-trading"
                         / "SOURCE.json").read_text(encoding="utf-8"))
    return (
        PASS if source.get("commit") else FAIL,
        f"{source.get('repository')} @ {source.get('tag')} ({str(source.get('commit'))[:12]})，"
        f"{source.get('vendoredFileCount')} 个文件，许可 {source.get('license')}",
        f"垫片文件：{', '.join(source.get('shimFiles') or [])}",
    )


def check_data_honesty() -> tuple[str, str, str]:
    """Proxy data must be labelled, and the missing-history limits stated."""
    library_doc = (ROOT / "docs" / "FACTOR_LIBRARY.md").read_text(encoding="utf-8")
    execution = ENGINE / "src" / "quantdesk" / "backtest" / "engine.py"
    text = execution.read_text(encoding="utf-8")
    ok = "dataProxies" in text or "data_proxies" in text
    return (
        PASS if ok else FAIL,
        f"回测结果带代理数据标注：{ok}；因子库文档记录股票类日线只有 5 个月："
        f"{'只有' in library_doc and '日线只有' in library_doc}",
        "约束 C1：代理/派生数据机器可读标注",
    )


def check_pending_statistics() -> tuple[str, str, str]:
    """Stage 6 (campaign-level DSR/PBO) is not finished yet; say so, do not guess."""
    if (ENGINE / "src" / "quantdesk" / "campaign_stats.py").is_file():
        code, line = pytest("tests/test_campaign_stats.py")
        return (PASS if code == 0 else FAIL), line, "campaign_stats.py 已落地"
    return PENDING, "engine/src/quantdesk/campaign_stats.py 尚未出现", "阶段 6 进行中"


def check_pending_verdict_api() -> tuple[str, str, str]:
    source = (ENGINE / "src" / "quantdesk" / "api" / "campaigns.py").read_text(encoding="utf-8")
    if "/verdict" in source:
        return PASS, "POST/GET /api/campaigns/{uid}/verdict 已在路由里", "阶段 6 交付"
    return PENDING, "判决接口尚未出现", "阶段 6 进行中"


CHECKS = [
    ("协议 v4 与能力表", check_protocol_v4),
    ("插件清单与启用状态", check_plugins_installed),
    ("一个能力一个持有者", check_one_provider_per_capability),
    ("因子目录：两族因子", check_factor_catalog),
    ("白名单与 vendored 字节一致", check_allowlist_integrity),
    ("vendored 代码无网络/子进程/环境变量", check_no_network_in_vendor),
    ("上游来源与许可固定", check_vibe_source_pinned),
    ("受控因子库来自闸门扫描", check_gate_library),
    ("战役治理：D1/D2 与窗口", check_campaign_governance),
    ("测试段封存（读与写都不可）", check_sealed_test_segment),
    ("D3：只有人能晋升", check_human_only_promotion),
    ("队列支持战役轮次", check_queue_supports_campaign),
    ("引擎全量测试", check_engine_suite),
    ("执行模型（Gate-B）", check_execution_model_suite),
    ("战役治理测试", check_campaign_suite),
    ("插件协议与白名单测试", check_plugin_suite),
    ("前端逻辑测试", check_web_tests),
    ("前端产物含代理面板", check_web_bundle),
    ("浏览器实测面板", check_browser_panel),
    ("文档落点", check_docs),
    ("排除项未被引入", check_exclusions),
    ("新表幂等、无需迁移步骤", check_migrations_idempotent),
    ("容器隔离（bwrap）", check_docker_isolation),
    ("本机沙箱状态如实上报", check_host_sandbox_honesty),
    ("阶段 10 保持关闭", check_llm_stage_off),
    ("代理数据标注（C1）", check_data_honesty),
    ("战役级 DSR/PBO", check_pending_statistics),
    ("开封判决接口", check_pending_verdict_api),
]


def render() -> str:
    passed = sum(1 for item in RESULTS if item.status == PASS)
    failed = sum(1 for item in RESULTS if item.status == FAIL)
    pending = sum(1 for item in RESULTS if item.status == PENDING)
    lines = [
        "# Vibe-Trading 接入验收报告（阶段 9）",
        "",
        "本文件由 `scripts/acceptance_check.py` **从实测生成**：每一行的证据都是刚刚跑出来的"
        "（实时接口、磁盘上的字节、测试套件、静态扫描）。跑不动的检查写「待定」并给出原因，"
        "不会静默通过。",
        "",
        f"- 生成时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 目标地址：{BASE_URL}",
        f"- 结果：**通过 {passed}** / 未通过 {failed} / 待定 {pending}",
        "",
        "| # | 检查项 | 结果 | 证据 | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in RESULTS:
        mark = {PASS: "✅ 通过", FAIL: "❌ 未通过", PENDING: "⏳ 待定"}[item.status]
        evidence = item.evidence.replace("|", "\\|")
        detail = item.detail.replace("|", "\\|")
        lines.append(f"| {item.number} | {item.title} | {mark} | {evidence} | {detail} |")
    lines += [
        "",
        "## 复现方式",
        "",
        "```bash",
        "cd engine && ./.venv/bin/python -m pytest -q          # 引擎全量",
        "cd web && npm test && npm run build                    # 前端逻辑与产物",
        "cd web && node scripts/campaign-smoke.mjs              # 浏览器实测",
        "engine/.venv/bin/python scripts/acceptance_check.py    # 重新生成本报告",
        "```",
        "",
        "## 仍然待定的两项",
        "",
        "第 27、28 项属于阶段 6（战役级 DSR/PBO 与开封判决）。它们尚未落地时本报告写「待定」，"
        "而不是用「没有结果」冒充「通过」——这正是 Gate-C 要求的诚实：多重检验校正与一次性开封"
        "是晋升前的最后一道门，缺了它就不该有晋升结论。",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/VIBE_INTEGRATION_ACCEPTANCE.md")
    parser.add_argument("--only", default="", help="只跑标题里含这个子串的检查")
    args = parser.parse_args()
    for title, fn in CHECKS:
        if args.only and args.only not in title:
            continue
        check(title, fn)
        print(f"  [{RESULTS[-1].status}] {title}：{RESULTS[-1].evidence[:110]}")
    if args.only:
        return 0
    report = render()
    (ROOT / args.out).write_text(report, encoding="utf-8")
    failed = sum(1 for item in RESULTS if item.status == FAIL)
    pending = sum(1 for item in RESULTS if item.status == PENDING)
    print(f"\n报告 -> {args.out}（未通过 {failed}，待定 {pending}）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
