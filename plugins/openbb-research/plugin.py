#!/usr/bin/env python3
"""OpenBB research adapter: one JSON-RPC request in, standardised evidence out.

This process runs in its own environment. It never imports the QuantDesk engine,
and the engine never imports OpenBB: the only thing that crosses the boundary is
a JSON-RPC message.

Two transports are supported, chosen by environment:

* ``OPENBB_API_URL`` set -> the OpenBB Platform REST API is called over HTTP.
* otherwise -> ``from openbb import obb`` is used in this process's environment.

If neither is available the plugin reports the runtime as unavailable. It does not
fall back to bundled sample data: a research report must never be built on demo
numbers dressed up as market data.

Evidence rules enforced here, because the engine's point-in-time gate can only
judge what it is told:

* every reading carries the real provider that produced it;
* ``publishedAt`` and ``asOf`` are kept apart - a fiscal period end is not a
  publication date, and a reading without a publication time says so in its
  warnings instead of implying one;
* a provider with no data yields ``unavailable`` with a reason, never a zero, an
  empty object or a substituted ticker.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2.0"
# Bounded so one verbose provider response cannot blow the engine's 1 MB stdout
# limit and take the whole run down with it.
MAX_ROWS_PER_READING = 40
MAX_STRING = 600
MAX_EVIDENCE_ITEMS = 24

# topic -> OpenBB entry points, in the `<router>.<function>` form OpenBB uses.
# Kept as data so an operator can correct a name for their installed version
# without touching code; an unknown name surfaces as a recorded provider failure
# rather than as an empty reading.
TOPIC_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "company_profile": ("equity.profile",),
    "fundamentals": (
        "equity.fundamental.income",
        "equity.fundamental.balance",
        "equity.fundamental.cash",
    ),
    "financial_growth": ("equity.fundamental.metrics",),
    "earnings": ("equity.calendar.earnings",),
    "filings": ("equity.fundamental.filings",),
    "company_events": ("equity.calendar.dividend", "equity.calendar.splits"),
    "news": ("news.company",),
    "short_interest": ("equity.shorts.short_interest",),
    "institutional_ownership": ("equity.ownership.institutional",),
    "macro": ("economy.fred_series",),
    "macro_calendar": ("economy.calendar",),
}

# Topics whose readings are published documents: the provider must date them, and
# the engine will refuse an undated one for a historical judgement.
DOCUMENT_TOPICS = frozenset(
    {"fundamentals", "financial_growth", "earnings", "filings", "company_events", "short_interest"}
)

# Where a publication timestamp tends to live, across providers.
PUBLISHED_KEYS = ("published_at", "publishedAt", "filing_date", "filingDate", "acceptance_datetime",
                  "acceptanceDateTime", "publication_date", "publicationDate", "release_date",
                  "date", "updated_at", "updatedAt")
PERIOD_KEYS = ("period_ending", "periodEnding", "period_end", "fiscal_period_end", "as_of", "asOf",
               "report_date", "reportDate")


class RuntimeUnavailable(RuntimeError):
    """The OpenBB runtime is not installed or not configured."""


# -- transport ----------------------------------------------------------

class RestTransport:
    """Calls the OpenBB Platform REST API."""

    def __init__(self, base_url: str, timeout: float = 25.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, endpoint: str, provider: str, params: dict) -> object:
        # `equity.fundamental.income` is router `equity.fundamental` + function
        # `income`, so the split is on the last dot: splitting on the first one
        # produces a URL the platform does not serve.
        router, _, function = endpoint.rpartition(".")
        url = f"{self.base_url}/api/v1/{router.replace('.', '/')}/{function}"
        query = urllib.parse.urlencode({**{k: v for k, v in params.items() if v not in (None, "")},
                                        "provider": provider})
        request = urllib.request.Request(f"{url}?{query}", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"OpenBB REST {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenBB REST 无法连接：{exc.reason}") from exc


class SdkTransport:
    """Calls the OpenBB Python SDK inside this process's own environment."""

    def __init__(self, timeout: float = 25.0):
        self.timeout = timeout
        try:
            from openbb import obb  # noqa: F401 - imported for availability only
        except Exception as exc:  # noqa: BLE001 - any import failure means unusable
            raise RuntimeUnavailable(
                "OpenBB 运行环境不可用：请在本插件独立 venv 中安装 openbb，"
                "或设置 OPENBB_API_URL 指向 OpenBB Platform REST 服务"
            ) from exc
        self.obb = obb

    def call(self, endpoint: str, provider: str, params: dict) -> object:
        node = self.obb
        for part in endpoint.split("."):
            node = getattr(node, part, None)
            if node is None:
                raise RuntimeError(f"OpenBB 没有该接口：{endpoint}")
        try:
            result = node(provider=provider, **params)
        except TypeError:
            result = node(**params)
        return _to_plain(result)


def _to_plain(value: object) -> object:
    """Turn an OpenBB result object into JSON-able data without inventing any."""
    for attribute in ("to_dict", "model_dump", "dict"):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                produced = method()
            except Exception:  # noqa: BLE001 - fall through to the next shape
                continue
            if isinstance(produced, dict):
                return produced.get("results", produced)
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return value
    return str(value)


def build_transport(env: dict | None = None) -> object:
    source = env if env is not None else os.environ
    base_url = str(source.get("OPENBB_API_URL") or "").strip()
    if base_url:
        return RestTransport(base_url)
    return SdkTransport()


# -- evidence shaping ---------------------------------------------------

def _trim(value: object, depth: int = 0) -> object:
    """Bound a value so one chatty provider cannot exhaust the message budget."""
    if depth > 4:
        return str(value)[:MAX_STRING]
    if isinstance(value, str):
        return value[:MAX_STRING]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _trim(item, depth + 1) for key, item in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_trim(item, depth + 1) for item in list(value)[:MAX_ROWS_PER_READING]]
    return str(value)[:MAX_STRING]


def _rows(payload: object) -> list[dict]:
    """The list of records inside whatever shape the provider returned."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "records", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return [value]
        return [payload]
    return []


def _first(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            # An epoch timestamp is a date too, and providers do send them.
            if value > 10_000_000:
                seconds = value / 1000 if value > 10_000_000_000 else value
                return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
    return ""


def publication_time(row: dict) -> str:
    return _first(row, PUBLISHED_KEYS)


def period_end(row: dict) -> str:
    return _first(row, PERIOD_KEYS)


def shape_evidence(
    *,
    topic: str,
    endpoint: str,
    provider: str,
    symbol: str,
    reference: str,
    payload: object,
    observed_at: str,
) -> list[dict]:
    """One standardised reading per returned row, with its dates kept apart."""
    rows = _rows(payload)
    if not rows:
        return []
    items: list[dict] = []
    for index, row in enumerate(rows[:MAX_ROWS_PER_READING]):
        published = publication_time(row)
        as_of = period_end(row)
        warnings: list[str] = []
        if topic in DOCUMENT_TOPICS and not published:
            warnings.append("Provider 未提供发布时间，历史研判将无法通过时点校验")
        if published and as_of and published[:10] == as_of[:10]:
            warnings.append("发布时间与期末日期相同，需人工确认是否为真实公布日")
        value = _trim(row)
        # The endpoint function is part of the key: one topic is answered by
        # several endpoints (income, balance, cash), and an index alone would make
        # three different readings share one key.
        function = endpoint.split(".")[-1]
        items.append(
            {
                "key": f"{topic}.{provider}.{reference.lower()}.{function}.{index}",
                "label": f"{reference} {topic}（{endpoint}）",
                "value": value,
                "source": str(
                    row.get("url") or row.get("source") or row.get("link") or f"{provider}:{endpoint}"
                ),
                "provider": provider,
                "endpoint": endpoint,
                "asOf": as_of,
                "publishedAt": published,
                "observedAt": observed_at,
                "pointInTime": topic in DOCUMENT_TOPICS,
                "warnings": warnings,
            }
        )
    return items[:MAX_EVIDENCE_ITEMS]


# -- method handling ----------------------------------------------------

def collect(request: dict, transport_factory=build_transport) -> dict:
    """Answer one research.collect call."""
    params = request.get("params") or {}
    symbol = str(params.get("symbol") or "")
    reference = str((params.get("mapping") or {}).get("openbbSymbol") or "")
    topics = [str(item) for item in (params.get("topics") or [])]
    providers = [str(item) for item in (params.get("providers") or [])]
    trade_date = str(params.get("tradeDate") or "")
    observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if not reference:
        return _unavailable(
            observed_at,
            f"{symbol} 没有可用的公开参考代码（映射表未提供），不猜测替代代码",
        )
    if not providers:
        return _unavailable(observed_at, "没有为该接口配置 Provider，未发起调用")

    try:
        transport = transport_factory()
    except RuntimeUnavailable as exc:
        return _unavailable(observed_at, str(exc))

    attempts: list[dict] = []
    warnings: list[str] = []
    for provider in providers:
        provider_items: list[dict] = []
        provider_errors: list[str] = []
        for topic in topics:
            endpoints = TOPIC_ENDPOINTS.get(topic)
            if not endpoints:
                provider_errors.append(f"未定义 topic：{topic}")
                continue
            for endpoint in endpoints:
                try:
                    payload = transport.call(endpoint, provider, {"symbol": reference})
                except Exception as exc:  # noqa: BLE001 - recorded, then next provider
                    provider_errors.append(f"{endpoint}：{type(exc).__name__}: {exc}")
                    continue
                provider_items.extend(
                    shape_evidence(
                        topic=topic, endpoint=endpoint, provider=provider, symbol=symbol,
                        reference=reference, payload=payload, observed_at=observed_at,
                    )
                )
        if provider_items:
            for item in provider_items:
                item["tradeDate"] = trade_date
            return {
                "provider": provider,
                "attempts": attempts,
                "source": provider_items[0].get("source") or "",
                "observedAt": observed_at,
                "evidence": provider_items,
                "warnings": warnings,
            }
        attempts.append(
            {"provider": provider, "reason": "; ".join(provider_errors) or "该 Provider 没有返回数据"}
        )

    return {
        "provider": "",
        "attempts": attempts,
        "observedAt": observed_at,
        "evidence": [],
        # Structured, one entry per attempted provider, so the engine can name the
        # reason per source instead of parsing a sentence.
        "unavailable": [{"provider": item["provider"], "reason": item["reason"]} for item in attempts],
        "unavailableReason": "全部 Provider 均无数据或不可用：" + "; ".join(
            f"{item['provider']}（{item['reason']}）" for item in attempts
        ),
        "warnings": warnings,
    }


def _unavailable(observed_at: str, reason: str) -> dict:
    """The shape of a refusal: no evidence, and a reason that names itself."""
    return {
        "provider": "",
        "attempts": [],
        "observedAt": observed_at,
        "evidence": [],
        "unavailable": [{"provider": "", "reason": reason}],
        "unavailableReason": reason,
    }


def health(_: dict) -> dict:
    """Report the runtime's state without calling a paid endpoint."""
    state: dict = {"ok": True, "protocol": PROTOCOL_VERSION, "topics": sorted(TOPIC_ENDPOINTS)}
    base_url = str(os.environ.get("OPENBB_API_URL") or "").strip()
    state["transport"] = "rest" if base_url else "sdk"
    state["baseUrl"] = base_url
    if base_url:
        state["runtimeReady"] = True
        state["note"] = "使用 OpenBB Platform REST，不在本进程导入 OpenBB"
        return state
    try:
        from openbb import obb  # noqa: F401

        state["runtimeReady"] = True
        state["note"] = "OpenBB SDK 可用"
    except Exception as exc:  # noqa: BLE001 - absence is a state, not a crash
        state["runtimeReady"] = False
        state["note"] = f"OpenBB SDK 不可用：{type(exc).__name__}: {exc}"
    return state


METHODS = {"health": health, "research.collect": collect}


def main() -> int:
    line = sys.stdin.readline()
    if not line.strip():
        return 0
    try:
        request = json.loads(line)
    except ValueError:
        print(json.dumps({"jsonrpc": PROTOCOL_VERSION, "id": None,
                          "error": {"code": -32700, "message": "请求不是有效 JSON"}}))
        return 0
    request_id = request.get("id")
    handler = METHODS.get(str(request.get("method") or ""))
    if handler is None:
        print(json.dumps({"jsonrpc": PROTOCOL_VERSION, "id": request_id,
                          "error": {"code": -32601, "message": f"不支持的方法：{request.get('method')}"}}))
        return 0
    try:
        result = handler(request)
    except Exception as exc:  # noqa: BLE001 - the boundary reports, never crashes silently
        print(json.dumps({"jsonrpc": PROTOCOL_VERSION, "id": request_id,
                          "error": {"code": -32000, "message": f"{type(exc).__name__}: {exc}"}}))
        return 0
    print(json.dumps({"jsonrpc": PROTOCOL_VERSION, "id": request_id, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
