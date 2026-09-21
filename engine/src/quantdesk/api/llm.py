"""LLM configuration endpoints and the screenshot-analysis task.

The browser never receives an API key: it can ask whether one is configured, and
it can replace one, but reads always come back as a boolean plus the variable
name that holds it.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ..config.instruments import require_instrument
from ..config.settings import (
    LLMProfile,
    ensure_keys_env,
    home_writable,
    load_llm_settings,
    quantdesk_home,
    read_key_env,
    set_key_env,
    write_llm_settings,
)
from ..datahub.db import Database
from ..llm import ChatMessage, DriverConfig, LLMError, build_driver, credential_issue, resolve_api_key

router = APIRouter(prefix="/api/llm", tags=["llm"])

ROLES = ("tradingagents", "chart_analysis", "journal_summary", "signal_explain", "ai_paper_trader")
ROLE_LABELS = {
    "tradingagents": "深度研判",
    "chart_analysis": "图表识别",
    "journal_summary": "日志总结",
    "signal_explain": "信号解释",
    "ai_paper_trader": "AI 模拟交易",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
KEY_ENV_CANDIDATES_ALL = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY")
IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"RIFF": "image/webp",
}

VISION_SYSTEM_PROMPT = """你是 QuantDesk 的图表结构识别器。

任务：只描述这张K线截图里**确实可见**的内容，供后续人工复核。

硬性要求：
1. 不给出任何买卖建议、目标价、仓位或止损建议。
2. 区分「图中可见」与「无法确认」。看不清的价格、指标数值、周期一律写进 uncertain，
   不要猜测具体数字。
3. 必须声明你无法从截图中确认的事项：精确OHLC、成交量、资金费率、持仓量、合约代码。
4. 只输出 JSON，不要输出 Markdown 代码块或额外说明。

JSON 结构：
{
  "instrument": {"visible": true/false, "value": "图中标注的标的，没有则空字符串"},
  "timeframe": {"visible": true/false, "value": "图中标注的周期"},
  "trend": "上升/下降/震荡/无法判断",
  "structure": ["可见的结构要点，每条一句话"],
  "levels": [{"price": "数值或区间", "kind": "支撑/阻力/缺口/前高前低", "note": "依据"}],
  "patterns": ["可见的形态，如双顶、旗形、头肩顶；没有则空数组"],
  "indicators_visible": ["图中出现的指标名称"],
  "uncertain": ["无法从截图确认的事项"],
  "confidence": "high/medium/low"
}"""


class ChartAnalysisRequest(BaseModel):
    """A chart screenshot plus the little context the engine already knows."""

    imageBase64: str = Field(..., description="PNG/JPEG/WebP，base64 或 data URL")
    mimeType: str | None = None
    fileName: str | None = None
    symbol: str | None = Field(None, description="固定合约池内的标的，用于给模型提供上下文")
    timeframe: str | None = None
    note: str | None = None


def _profile_driver(profile: LLMProfile):
    """Build a driver for a profile, or explain precisely why it cannot be built."""
    key, candidates = resolve_api_key(profile.provider, profile.api_key_env)
    if not key:
        raise HTTPException(
            409,
            f"profile「{profile.name}」未配置 API Key：请写入 {candidates[0]}（设置页或 ~/.quantdesk/keys.env）",
        )
    config = DriverConfig(
        profile=profile.name,
        provider=profile.provider,
        base_url=profile.base_url,
        api_key=key,
        proxy=profile.proxy or None,
        supports_vision=profile.supports_vision,
        supports_json_mode=profile.supports_json_mode,
    )
    return build_driver(config)


def _database() -> Database:
    """Open the local database, or explain why it cannot be opened."""
    home = quantdesk_home()
    path = home / "quantdesk.db"
    try:
        return Database(path)
    except Exception as exc:  # noqa: BLE001 - sqlite raises OperationalError
        raise HTTPException(
            503,
            f"无法打开本地数据库 {path}：{exc}。该目录对运行网关的账号不可写；"
            "把 QUANTDESK_HOME 指向可写目录后重启网关。",
        ) from exc


def _masked_key_status(home: Path) -> dict[str, bool]:
    file_keys = read_key_env(home)
    status: dict[str, bool] = {}
    for name, value in file_keys.items():
        status[name] = bool(value)
    return status


def _profile_payload(profile: LLMProfile, home: Path) -> dict:
    key, candidates = resolve_api_key(profile.provider, profile.api_key_env)
    issue = credential_issue(key)
    return {
        "name": profile.name,
        "provider": profile.provider,
        "baseUrl": profile.base_url,
        "apiKeyEnv": profile.api_key_env,
        "apiKeyCandidates": list(candidates),
        # A credential that is present but obviously malformed is not "configured"
        # in any useful sense; say so instead of letting a 401 explain it later.
        "hasKey": key is not None and issue is None,
        "credentialIssue": issue,
        "keyPresent": key is not None,
        "proxy": profile.proxy,
        "supportsVision": profile.supports_vision,
        "supportsJsonMode": profile.supports_json_mode,
        "models": {
            "deep": profile.deep_model,
            "quick": profile.quick_model,
            "vision": profile.vision_model,
        },
        "maxTokens": profile.max_tokens,
    }


@router.get("/settings")
def llm_settings():
    home = quantdesk_home()
    ensure_keys_env(home)
    settings = load_llm_settings(home)
    env_keys = {name: bool(os.environ.get(name)) for name in sorted({*_masked_key_status(home), *KEY_ENV_CANDIDATES_ALL})}
    return {
        "profiles": [_profile_payload(profile, home) for profile in settings.profiles.values()],
        "roles": settings.roles,
        "roleLabels": ROLE_LABELS,
        "knownRoles": list(ROLES),
        # Only presence is ever reported; the value stays on the server.
        "configuredKeys": env_keys,
        "keysFile": str(home / "keys.env"),
        # The UI must be able to say up front whether saving can work at all.
        "homeWritable": home_writable(home),
    }


@router.post("/roles")
def save_roles(roles: dict[str, str]):
    """Point each capability at a profile — the 'switch provider whenever' action."""
    home = quantdesk_home()
    settings = load_llm_settings(home)
    unknown = [name for name in roles if name not in ROLES]
    if unknown:
        raise HTTPException(422, f"未知角色：{', '.join(unknown)}")
    invalid = [name for name, target in roles.items() if target and target not in settings.profiles]
    if invalid:
        raise HTTPException(422, f"角色指向了不存在的 profile：{', '.join(invalid)}")
    for name, target in roles.items():
        if target:
            settings.roles[name] = target
        else:
            settings.roles.pop(name, None)
    try:
        write_llm_settings(settings, home)
    except OSError as exc:
        raise HTTPException(
            409,
            f"无法写入 {home / 'llm.toml'}：{exc.strerror or exc}。请让运行网关的账号可写该目录。",
        ) from exc
    return {"roles": settings.roles}


@router.post("/keys")
def save_keys(updates: dict[str, str]):
    """Write credentials to keys.env (chmod 600). Values are never echoed back."""
    if not updates:
        raise HTTPException(422, "没有需要写入的 Key")
    for name in updates:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise HTTPException(422, f"环境变量名不合法：{name}")
    home = quantdesk_home()
    try:
        path = set_key_env(updates, home)
    except OSError as exc:
        raise HTTPException(
            409,
            f"无法写入 {home / 'keys.env'}：{exc.strerror or exc}。"
            "该目录不在本进程的可写范围内时，请改为写入进程环境变量，或用 QUANTDESK_HOME 指向可写目录后重启网关。",
        ) from exc
    stored = _masked_key_status(home)
    return {"saved": sorted(updates), "keysFile": str(path), "configuredKeys": stored}


@router.post("/profiles/{name}/models")
def list_models(name: str):
    """Ask the provider what this account can actually call."""
    home = quantdesk_home()
    settings = load_llm_settings(home)
    profile = settings.profiles.get(name)
    if profile is None:
        raise HTTPException(404, f"没有名为 {name} 的 profile")
    try:
        driver = _profile_driver(profile)
        models = driver.list_models()
    except LLMError as exc:
        return {"ok": False, "profile": name, "error": exc.as_dict(), "models": []}
    return {"ok": True, "profile": name, "models": models}


class ModelTestRequest(BaseModel):
    model: str | None = None


@router.post("/profiles/{name}/test")
def test_profile(name: str, request: ModelTestRequest | None = None, model: str | None = None):
    """Round-trip a tiny request so the failure mode is named, not guessed.

    The model may be given as a query parameter or in the body. Some providers
    answer a request for an unknown model by silently serving a default one, so
    the response carries both the requested and the actual model — a caller must
    never be told "ok" while a different model answered.
    """
    home = quantdesk_home()
    settings = load_llm_settings(home)
    profile = settings.profiles.get(name)
    if profile is None:
        raise HTTPException(404, f"没有名为 {name} 的 profile")
    requested = (request.model if request and request.model else model) or profile.model_for("signal_explain")
    try:
        driver = _profile_driver(profile)
        completion = driver.complete(
            [ChatMessage(role="user", text="回复两个字：可用")],
            model=requested,
            temperature=0,
            # Reasoning models spend most of a small budget on the chain of
            # thought and can return an empty message; leave room for both.
            max_tokens=max(profile.max_tokens, 2048),
        )
    except LLMError as exc:
        return {"ok": False, "profile": name, "requestedModel": requested, "model": requested, "error": exc.as_dict()}

    # The provider echoing a different model means the configured name is not the
    # one that ran; surface that instead of reporting a clean success.
    substituted = bool(completion.model) and completion.model != requested
    return {
        "ok": True,
        "profile": name,
        "requestedModel": requested,
        "model": completion.model,
        "notes": completion.notes,
        "substituted": substituted,
        "warning": (
            f"请求的模型 {requested} 未生效，供应商实际使用 {completion.model}。"
            "该模型名在你的账号下可能不存在，请从「拉取模型」的列表中选择。"
            if substituted
            else None
        ),
        "latencySeconds": completion.latency_s,
        "usage": completion.usage,
        "reply": completion.text[:80],
    }


class ModelConfigRequest(BaseModel):
    """Update the model names a profile uses. Omitted fields are left alone."""

    deepModel: str | None = None
    quickModel: str | None = None
    visionModel: str | None = None
    maxTokens: int | None = Field(None, ge=0, le=200_000)


@router.post("/profiles/{name}/models-config")
def save_model_config(name: str, request: ModelConfigRequest):
    """Persist model names so the settings page is not read-only for them."""
    home = quantdesk_home()
    settings = load_llm_settings(home)
    profile = settings.profiles.get(name)
    if profile is None:
        raise HTTPException(404, f"没有名为 {name} 的 profile")
    if request.deepModel is not None:
        profile.deep_model = request.deepModel.strip()
    if request.quickModel is not None:
        profile.quick_model = request.quickModel.strip()
    if request.visionModel is not None:
        profile.vision_model = request.visionModel.strip()
    if request.maxTokens is not None:
        profile.max_tokens = request.maxTokens
    if not (profile.deep_model or profile.quick_model or profile.vision_model):
        raise HTTPException(422, "至少要保留一个模型名")
    try:
        write_llm_settings(settings, home)
    except OSError as exc:
        raise HTTPException(409, f"无法写入 {home / 'llm.toml'}：{exc.strerror or exc}") from exc
    return {
        "profile": name,
        "models": {"deep": profile.deep_model, "quick": profile.quick_model, "vision": profile.vision_model},
        "maxTokens": profile.max_tokens,
        "rolesUsingIt": [role for role, target in settings.roles.items() if target == name],
    }


def _detect_image_type(payload: bytes, declared: str | None) -> str | None:
    for magic, mime in IMAGE_MAGIC.items():
        if payload.startswith(magic):
            return mime
    if declared in ALLOWED_IMAGE_TYPES:
        return declared
    return None


@router.post("/analyze-chart")
async def analyze_chart(request: ChartAnalysisRequest):
    """Read a chart screenshot with a vision-capable profile.

    The image arrives base64-encoded in JSON rather than as multipart form data,
    which keeps the gateway free of an extra parser dependency for a single
    upload path. The result is stored as an observation with its source, never
    as a trading instruction: the gateway still fetches its own market data and
    never trusts the model as a price feed.
    """
    payload, mime = _decode_image(request.imageBase64, request.mimeType)
    if len(payload) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"图片超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB 限制")

    instrument = None
    if request.symbol:
        try:
            instrument = require_instrument(request.symbol)
        except ValueError as exc:
            raise HTTPException(422, "该合约不在固定合约池内") from exc

    home = quantdesk_home()
    settings = load_llm_settings(home)
    try:
        profile = settings.vision_profile("chart_analysis")
    except KeyError as exc:
        raise HTTPException(409, exc.args[0] if exc.args else "无法选择图表识别 profile") from exc

    context = []
    if instrument:
        context.append(f"系统已知：标的 {instrument.display_symbol}（{instrument.name}），交易所代码 {instrument.venue_symbol}。")
    if request.timeframe:
        context.append(f"系统已知：周期 {request.timeframe}。")
    if request.note:
        context.append(f"用户补充：{request.note.strip()[:500]}")

    message = ChatMessage(
        role="user",
        text="\n".join(context + ["请按系统要求识别这张K线截图，只输出 JSON。"]),
        images=[payload],
    )

    def run() -> dict:
        driver = _profile_driver(profile)
        return driver.complete(
            [ChatMessage(role="system", text=VISION_SYSTEM_PROMPT), message],
            model=profile.model_for("chart_analysis"),
            temperature=0,
            max_tokens=1600,
        ).as_dict()

    try:
        completion = await run_in_threadpool(run)
    except HTTPException:
        raise
    except LLMError as exc:
        raise HTTPException(502, json.dumps(exc.as_dict(), ensure_ascii=False)) from exc

    raw = completion["text"]
    structured = _parse_json_block(raw)

    uploads = home / "uploads"
    uploads.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = Path(request.fileName or "chart").stem or "chart"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:60]
    suffix = ALLOWED_IMAGE_TYPES.get(mime, ".png")
    image_path = uploads / f"{stamp}-{safe_name}{suffix}"
    image_path.write_bytes(payload)

    db = _database()
    db.execute(
        "INSERT INTO chart_analyses (created_ts, file_name, image_path, model, profile, result_md, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            int(time.time() * 1000),
            request.fileName,
            str(image_path),
            completion["model"],
            profile.name,
            raw,
            json.dumps(
                {
                    "symbol": instrument.venue_symbol if instrument else None,
                    "timeframe": request.timeframe,
                    "note": request.note,
                    "usage": completion["usage"],
                    "latencySeconds": completion["latency_s"],
                    "structured": structured,
                },
                ensure_ascii=False,
            ),
        ),
    )

    return {
        "ok": True,
        "profile": profile.name,
        "model": completion["model"],
        "latencySeconds": completion["latency_s"],
        "usage": completion["usage"],
        "structured": structured,
        "raw": raw,
        "imagePath": str(image_path),
        "symbol": instrument.venue_symbol if instrument else None,
        "timeframe": request.timeframe,
        "disclaimer": "此为截图内容的结构化识别结果，不是交易指令；价格与指标请以交易所实时数据为准。",
    }


def _decode_image(data: str, declared: str | None) -> tuple[bytes, str | None]:
    """Accept a raw base64 payload or a data: URL."""
    text = (data or "").strip()
    inline_mime = None
    if text.startswith("data:"):
        header, _, text = text.partition(",")
        inline_mime = header[5:].split(";")[0] or None
    if not text:
        raise HTTPException(422, "缺少图片数据")
    import base64
    import binascii

    try:
        payload = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(422, "图片 base64 解码失败") from exc
    if not payload:
        raise HTTPException(422, "上传的图片为空")
    mime = _detect_image_type(payload, inline_mime or declared)
    if mime is None:
        raise HTTPException(415, "只支持 PNG / JPEG / WebP 截图")
    return payload, mime


def _parse_json_block(text: str):
    """Tolerate a fenced or chatty response; return None when it is not JSON."""
    if not text:
        return None
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.S)
    if fence:
        candidate = fence.group(1).strip()
    if not candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        candidate = candidate[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


@router.get("/status")
def llm_status():
    """Compact readiness summary for the settings page header."""
    home = quantdesk_home()
    settings = load_llm_settings(home)
    rows = []
    for role in ROLES:
        profile = settings.profiles.get(settings.roles.get(role, ""))
        key, _ = resolve_api_key(profile.provider, profile.api_key_env) if profile else (None, ())
        issue = credential_issue(key)
        rows.append(
            {
                "role": role,
                "label": ROLE_LABELS[role],
                "profile": profile.name if profile else None,
                "ready": bool(profile and key and not issue),
                "credentialIssue": issue,
                "supportsVision": bool(profile and profile.supports_vision),
            }
        )
    try:
        vision = settings.vision_profile("chart_analysis")
        vision_name = vision.name
    except KeyError:
        vision_name = None
    return {
        "roles": rows,
        "visionProfile": vision_name,
        "canAnalyzeCharts": vision_name is not None and any(row["ready"] and row["supportsVision"] for row in rows),
    }
