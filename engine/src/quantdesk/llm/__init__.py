"""LLM provider adapter.

One narrow interface, several providers, and failure modes that name themselves.
The browser never sees an API key: it asks the local gateway to run a task, and
the gateway reads credentials from ``keys.env`` / the environment.

DeepSeek and OpenAI both speak ``/v1/chat/completions``, so a single
OpenAI-compatible driver covers them; what differs is the base URL and — in this
environment — whether the request needs the local proxy. ``api.openai.com`` is
not reachable directly from this network, so a missing proxy must surface as a
network error rather than as an authentication failure.
"""

from .base import (
    ChatMessage,
    Completion,
    DriverConfig,
    LLMError,
    LLMErrorKind,
    OpenAICompatibleDriver,
    build_driver,
    classify_failure,
    credential_issue,
    resolve_api_key,
)

__all__ = [
    "ChatMessage",
    "Completion",
    "DriverConfig",
    "LLMError",
    "LLMErrorKind",
    "OpenAICompatibleDriver",
    "build_driver",
    "classify_failure",
    "credential_issue",
    "resolve_api_key",
]
