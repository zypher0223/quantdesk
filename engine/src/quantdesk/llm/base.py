"""Provider drivers and the failure vocabulary they share."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LLMErrorKind(str, Enum):
    """Why a call failed, in terms a user can act on."""

    NO_KEY = "no_key"
    INVALID_KEY = "invalid_key"
    MODEL_NOT_FOUND = "model_not_found"
    RATE_LIMITED = "rate_limited"
    QUOTA = "quota"
    NETWORK = "network"
    TIMEOUT = "timeout"
    BAD_REQUEST = "bad_request"
    SERVER = "server"
    UNKNOWN = "unknown"


# Chinese copy per failure kind, plus what the user should do about it.
ERROR_COPY: dict[LLMErrorKind, tuple[str, str]] = {
    LLMErrorKind.NO_KEY: ("未配置 API Key", "在设置页填入该 profile 对应的 Key，或写入 ~/.quantdesk/keys.env"),
    LLMErrorKind.INVALID_KEY: ("API Key 无效", "Key 被拒绝，请核对是否过期或填错了供应商"),
    LLMErrorKind.MODEL_NOT_FOUND: ("模型名不存在", "点「拉取模型列表」从该账号实际可用的模型中选择"),
    LLMErrorKind.RATE_LIMITED: ("触发限流", "请求过于频繁，稍后重试或降低并发"),
    LLMErrorKind.QUOTA: ("余额或配额不足", "请到供应商控制台充值或调整额度"),
    LLMErrorKind.NETWORK: ("网络不可达", "本机直连该供应商被拒，需要在 profile 里配置代理"),
    LLMErrorKind.TIMEOUT: ("请求超时", "模型响应过慢，可换更快的模型或提高超时时间"),
    LLMErrorKind.BAD_REQUEST: ("请求被拒绝", "参数或消息格式不被该模型接受"),
    LLMErrorKind.SERVER: ("供应商服务异常", "对方返回 5xx，稍后重试"),
    LLMErrorKind.UNKNOWN: ("未知错误", "请查看详细信息"),
}


class LLMError(RuntimeError):
    def __init__(self, kind: LLMErrorKind, detail: str, *, status: int | None = None):
        self.kind = kind
        self.detail = detail
        self.status = status
        super().__init__(f"{ERROR_COPY[kind][0]}: {detail}")

    def as_dict(self) -> dict:
        title, action = ERROR_COPY[self.kind]
        return {"kind": self.kind.value, "title": title, "detail": self.detail, "action": action, "status": self.status}


# Upstream status codes, then message fragments for gateways that do not use them.
_STATUS_KINDS: dict[int, LLMErrorKind] = {
    400: LLMErrorKind.BAD_REQUEST,
    401: LLMErrorKind.INVALID_KEY,
    402: LLMErrorKind.QUOTA,
    403: LLMErrorKind.INVALID_KEY,
    404: LLMErrorKind.MODEL_NOT_FOUND,
    408: LLMErrorKind.TIMEOUT,
    429: LLMErrorKind.RATE_LIMITED,
}
_MESSAGE_KINDS: tuple[tuple[tuple[str, ...], LLMErrorKind], ...] = (
    (("insufficient balance", "insufficient_quota", "exceeded your current quota", "余额不足", "额度"), LLMErrorKind.QUOTA),
    (("invalid api key", "incorrect api key", "unauthorized", "authentication"), LLMErrorKind.INVALID_KEY),
    (("model not found", "does not exist", "unknown model", "no such model"), LLMErrorKind.MODEL_NOT_FOUND),
    (("rate limit", "too many requests", "tpm", "rpm"), LLMErrorKind.RATE_LIMITED),
    (("connection error", "connect", "proxy", "name or service not known", "nodename"), LLMErrorKind.NETWORK),
    (("timed out", "timeout"), LLMErrorKind.TIMEOUT),
)


def classify_failure(status: int | None, message: str) -> LLMErrorKind:
    text = (message or "").lower()
    for fragments, kind in _MESSAGE_KINDS:
        if any(fragment in text for fragment in fragments):
            return kind
    if status in _STATUS_KINDS:
        return _STATUS_KINDS[status]
    if status is not None and status >= 500:
        return LLMErrorKind.SERVER
    return LLMErrorKind.UNKNOWN


KEY_ENV_CANDIDATES: dict[str, tuple[str, ...]] = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openai_compatible": ("GLM_API_KEY", "ZHIPU_API_KEY", "OPENAI_API_KEY"),
    "glm": ("GLM_API_KEY", "ZHIPU_API_KEY"),
}

# Providers that are not reachable without the local proxy on this network.
PROXY_REQUIRED_HINT = {"openai": True}


def key_candidates(provider: str, declared: str = "") -> tuple[str, ...]:
    names: list[str] = []
    if declared:
        names.append(declared)
    names.extend(KEY_ENV_CANDIDATES.get(provider, ()))
    names.append(f"{provider.upper()}_API_KEY")
    seen: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


# Credentials that only look like credentials. A documentation placeholder pasted
# verbatim produces an upstream 401 that reads like a wrong key, which sends people
# looking in the wrong place.
PLACEHOLDER_VALUES = {
    "sk-", "sk-...", "sk-xxx", "sk-xxxx", "sk-your-key", "sk-your-api-key", "your-api-key",
    "xxx", "xxxx", "todo", "changeme", "placeholder", "<your-key>",
}


def credential_issue(value: str | None) -> str | None:
    """Return a human-readable problem with a credential value, or None if it looks usable.

    Never returns the value itself — only what is wrong with its shape.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return "值为空"
    lowered = text.lower()
    if lowered in PLACEHOLDER_VALUES or lowered.rstrip(".") in {item.rstrip(".") for item in PLACEHOLDER_VALUES}:
        return f"值是占位符 {text!r}，不是真实密钥"
    if "..." in text or "…" in text:
        return "值里含省略号，像是从文档里复制了占位符"
    if text != value:
        return "值首尾有空白字符"
    # Length is deliberately not checked: provider key formats differ widely, and
    # a short-but-real key would be worse to reject here than to let the provider
    # answer 401 about.
    if any(ord(char) > 127 for char in text):
        return "值含非 ASCII 字符"
    if " " in text:
        return "值含空格"
    return None


def resolve_api_key(provider: str, declared: str = "") -> tuple[str | None, tuple[str, ...]]:
    """First non-empty credential from the declared name, then provider defaults."""
    names = key_candidates(provider, declared)
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip(), names
    return None, names


@dataclass
class DriverConfig:
    profile: str
    provider: str
    base_url: str
    api_key: str
    proxy: str | None = None
    timeout: float = 120.0
    supports_vision: bool = False
    supports_json_mode: bool = False
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ChatMessage:
    role: str  # system / user / assistant
    text: str = ""
    images: list[bytes] = field(default_factory=list)  # PNG/JPEG payloads, vision only

    def content_parts(self, allow_images: bool) -> Any:
        if not self.images or not allow_images:
            return self.text
        parts: list[dict] = [{"type": "text", "text": self.text}]
        for image in self.images:
            encoded = base64.b64encode(image).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})
        return parts


@dataclass
class Completion:
    text: str
    model: str
    usage: dict
    latency_s: float
    kind: str = "chat"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "model": self.model,
            "usage": self.usage,
            "latency_s": self.latency_s,
            "kind": self.kind,
            "notes": self.notes,
        }


class OpenAICompatibleDriver:
    """Chat completions over the OpenAI wire format (DeepSeek, OpenAI, GLM)."""

    def __init__(self, config: DriverConfig):
        self.config = config

    def _client(self):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise LLMError(LLMErrorKind.UNKNOWN, f"openai 库未安装：{exc}") from exc
        kwargs: dict[str, Any] = {
            "api_key": self.config.api_key,
            "timeout": self.config.timeout,
            "max_retries": 0,  # retries hide the failure kind from the operator
        }
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        if self.config.proxy:
            kwargs["http_client"] = _proxied_http_client(self.config.proxy, self.config.timeout)
        if self.config.extra_headers:
            kwargs["default_headers"] = self.config.extra_headers
        return OpenAI(**kwargs)

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_object: bool = False,
    ) -> Completion:
        import time

        if not model:
            raise LLMError(LLMErrorKind.MODEL_NOT_FOUND, "该 profile 未配置模型名")
        images = any(message.images for message in messages)
        if images and not self.config.supports_vision:
            raise LLMError(
                LLMErrorKind.BAD_REQUEST,
                f"profile {self.config.profile!r} 未声明 supports_vision，拒绝发送图片",
            )

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": message.role, "content": message.content_parts(self.config.supports_vision)}
                for message in messages
            ],
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_object and self.config.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        try:
            response = self._client().chat.completions.create(**payload)
        except Exception as exc:  # noqa: BLE001 - every provider error funnels here
            raise _as_llm_error(exc, self.config) from exc

        choice = response.choices[0] if getattr(response, "choices", None) else None
        text = (getattr(choice.message, "content", "") or "") if choice else ""
        # Reasoning models (deepseek-v4-pro/flash among them) put their chain of
        # thought in `reasoning_content` and only fill `content` afterwards. If the
        # token budget ran out mid-reasoning, `content` is empty while the call
        # still reports success — falling back keeps the answer instead of losing it.
        reasoning = (getattr(choice.message, "reasoning_content", None) or "") if choice else ""
        finish_reason = getattr(choice, "finish_reason", None) if choice else None
        used_reasoning = False
        if not text.strip() and reasoning.strip():
            text = reasoning
            used_reasoning = True

        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        out = Completion(
            text=text.strip(),
            model=getattr(response, "model", model),
            usage={
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "reasoning_tokens": getattr(details, "reasoning_tokens", None) if details else None,
                "finish_reason": finish_reason,
            },
            latency_s=round(time.monotonic() - started, 2),
            kind="vision" if images else "chat",
        )
        if used_reasoning:
            out.notes.append(
                "供应商未返回正文，仅返回了推理过程（通常是 token 预算被推理耗尽）。"
                "请提高 max_tokens 或换用非推理模型。"
            )
        if finish_reason == "length" and not text.strip():
            out.notes.append("回答因达到 token 上限而被截断，且没有留下正文。")
        return out

    def list_models(self) -> list[str]:
        try:
            page = self._client().models.list()
        except Exception as exc:  # noqa: BLE001
            raise _as_llm_error(exc, self.config) from exc
        rows = getattr(page, "data", None) or []
        return sorted({getattr(row, "id", "") for row in rows if getattr(row, "id", "")})


def _proxied_http_client(proxy: str, timeout: float):
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise LLMError(LLMErrorKind.UNKNOWN, f"httpx 未安装：{exc}") from exc
    # A socks:// proxy needs python-socks; surface that as a network problem
    # rather than letting it look like an authentication failure.
    return httpx.Client(proxy=proxy, timeout=timeout)


def _as_llm_error(exc: Exception, config: DriverConfig) -> LLMError:
    status = getattr(exc, "status_code", None)
    message = str(exc)
    kind = classify_failure(status, message)
    if kind is LLMErrorKind.NETWORK and PROXY_REQUIRED_HINT.get(config.provider) and not config.proxy:
        message = f"{message}（{config.provider} 在本机需要代理，请在 profile 里配置 proxy）"
    return LLMError(kind, message, status=status)


def build_driver(config: DriverConfig) -> OpenAICompatibleDriver:
    return OpenAICompatibleDriver(config)
