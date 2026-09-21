#!/usr/bin/env python3
"""Fincept portfolio-analytics adapter: official REST API only.

Boundaries this file exists to keep:

* It talks to the Fincept **API** and nothing else. No Terminal repository code is
  cloned, vendored, imported or linked, and none of its UI, agent, backtest, paper
  trading or broker code is reproduced here.
* It has no order path. The only methods it implements are risk, optimization and
  stress computation; there is nothing in this process that could submit an order
  or change a QuantDesk position.
* Every result is computed on QuantDesk's contract codes and QuantDesk's own
  returns. Bybit remains the only source of tradable prices; this adapter never
  supplies a price of its own.
* A missing key or an unreachable endpoint is reported as unavailable. No metric is
  ever invented, defaulted to zero, or filled from a bundled sample.

Endpoint paths and field spellings differ between Fincept deployments, so both are
declared as data below. An operator points them at their subscription; a wrong
guess surfaces as a recorded error rather than as a plausible-looking number.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2.0"
DEFAULT_BASE_URL = "https://api.fincept.in"
DEFAULT_TIMEOUT = 60.0

# method -> path. Verify against the deployed API before trusting a result.
ENDPOINTS = {
    "portfolio": "/v1/quant/portfolio/risk",
    "optimize": "/v1/quant/portfolio/optimize",
    "scenario": "/v1/quant/portfolio/stress",
}

# Field spellings seen across deployments, most specific first. Accepting several
# keeps the adapter usable without pretending to know one exact schema.
METRIC_KEYS = {
    "volatility": ("volatility", "portfolioVolatility", "annualizedVolatility", "sigma"),
    "var": ("var", "VaR", "valueAtRisk", "var95"),
    "cvar": ("cvar", "CVaR", "expectedShortfall", "conditionalValueAtRisk"),
    "maxDrawdown": ("maxDrawdown", "maximumDrawdown", "mdd"),
    "sharpe": ("sharpe", "sharpeRatio"),
    "beta": ("beta", "portfolioBeta"),
}
CONTRIBUTION_KEYS = ("riskContributions", "contributions", "componentVaR", "risk_contributions")
CORRELATION_KEYS = ("correlation", "correlationMatrix", "correlation_matrix")
WEIGHT_KEYS = ("weights", "optimalWeights", "allocation")


class AnalyticsUnavailable(RuntimeError):
    """No usable Fincept credential or endpoint for this call."""


def _first(source: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


class FinceptClient:
    """Minimal REST client: POST JSON, read JSON, report failure plainly."""

    def __init__(self, base_url: str, api_key: str, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def post(self, path: str, body: dict) -> tuple[dict, str]:
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                request_id = response.headers.get("X-Request-Id", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"Fincept API {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Fincept API 无法连接：{exc.reason}") from exc
        try:
            return json.loads(raw), request_id
        except ValueError as exc:
            raise RuntimeError("Fincept API 返回的不是 JSON") from exc


def build_client(env: dict | None = None) -> FinceptClient:
    source = env if env is not None else os.environ
    base_url = str(source.get("FINCEPT_API_URL") or DEFAULT_BASE_URL).strip()
    api_key = str(source.get("FINCEPT_API_KEY") or "").strip()
    if not api_key:
        raise AnalyticsUnavailable(
            "未配置 FINCEPT_API_KEY：请在设置页保存 Fincept API Key 后再运行组合风险计算"
        )
    return FinceptClient(base_url, api_key)


# -- request shaping ----------------------------------------------------

def portfolio_body(params: dict) -> dict:
    """The API body for a portfolio-risk call, built from QuantDesk's own inputs."""
    return {
        "asOf": params.get("asOf"),
        "baseCurrency": params.get("baseCurrency") or "USDT",
        "confidence": params.get("confidence", 0.95),
        "riskFreeRate": params.get("riskFreeRate", 0.0),
        "positions": [
            {
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "quantity": item.get("quantity"),
                "entryPrice": item.get("entryPrice"),
                "markPrice": item.get("markPrice"),
                "notional": item.get("notional"),
                "margin": item.get("margin"),
                "group": item.get("group") or "",
            }
            for item in params.get("positions") or []
        ],
        # Returns are QuantDesk's, keyed by contract code: this adapter never
        # invents or fetches a price series.
        "returns": params.get("returns") or {},
    }


def scenario_body(params: dict) -> dict:
    return {
        "asOf": params.get("asOf"),
        "baseCurrency": params.get("baseCurrency") or "USDT",
        "confidence": params.get("confidence", 0.95),
        "scenario": params.get("scenario"),
        "shocks": params.get("shocks") or {},
        "positions": portfolio_body(params)["positions"],
    }


# -- response normalisation ---------------------------------------------

def normalise_metrics(payload: dict) -> tuple[dict, dict]:
    """Named metrics plus anything else the provider sent, kept as extras."""
    named: dict = {}
    used: set[str] = set()
    for name, keys in METRIC_KEYS.items():
        value = _number(_first(payload, keys))
        if value is not None:
            named[name] = value
            for key in keys:
                used.add(key)
    extra = {
        key: value
        for key, value in payload.items()
        if key not in used and isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    return named, extra


def normalise_contributions(payload: dict) -> list[dict]:
    raw = _first(payload, CONTRIBUTION_KEYS)
    items: list[dict] = []
    if isinstance(raw, dict):
        for symbol, value in raw.items():
            number = _number(value)
            if number is not None:
                items.append({"symbol": str(symbol), "value": number, "percentage": 0.0})
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            symbol = _first(item, ("symbol", "asset", "ticker"))
            value = _number(_first(item, ("value", "contribution", "componentVar", "componentVaR")))
            percentage = _number(_first(item, ("percentage", "percent", "pct", "share")))
            if symbol is None or value is None:
                continue
            items.append(
                {
                    "symbol": str(symbol),
                    "value": value,
                    "percentage": percentage if percentage is not None else 0.0,
                }
            )
    # Percentages are derived only when the provider sent values without them.
    if items and all(item["percentage"] == 0.0 for item in items):
        total = sum(abs(item["value"]) for item in items) or 1.0
        for item in items:
            item["percentage"] = round(abs(item["value"]) / total * 100, 6)
    return items


def normalise_correlation(payload: dict) -> dict | None:
    raw = _first(payload, CORRELATION_KEYS)
    if raw is None and "matrix" in payload:
        # Some deployments return the correlation object at the top level.
        raw = payload
    if isinstance(raw, dict) and "matrix" in raw:
        symbols = [str(item) for item in raw.get("symbols") or []]
        matrix = raw.get("matrix") or []
    elif isinstance(raw, list):
        symbols = [str(item) for item in payload.get("symbols") or []]
        matrix = raw
    else:
        return None
    rows: list[list[float]] = []
    for row in matrix:
        if isinstance(row, dict):
            rows.append([_number(value) or 0.0 for _, value in sorted(row.items())])
        elif isinstance(row, list):
            rows.append([_number(value) or 0.0 for value in row])
    if not rows:
        return None
    if not symbols:
        symbols = [f"asset{i}" for i in range(len(rows))]
    return {"symbols": symbols, "matrix": rows}


def normalise_optimization(payload: dict) -> dict | None:
    raw = _first(payload, WEIGHT_KEYS)
    if not isinstance(raw, dict) or not raw:
        return None
    weights = {str(key): _number(value) or 0.0 for key, value in raw.items()}
    return {
        "method": str(_first(payload, ("method", "optimizer", "strategy")) or "fincept"),
        "weights": weights,
        "expectedReturn": _number(_first(payload, ("expectedReturn", "expected_return"))),
        "expectedVolatility": _number(_first(payload, ("expectedVolatility", "expected_volatility"))),
        "notes": [],
    }


def portfolio_result(params: dict, payload: dict, request_id: str) -> dict:
    metrics, extra = normalise_metrics(payload)
    return {
        "provider": "fincept-api",
        "asOf": str(params.get("asOf") or ""),
        "metrics": {**metrics, "extra": extra},
        "riskContributions": normalise_contributions(payload),
        "correlation": normalise_correlation(payload),
        "optimization": normalise_optimization(payload),
        "source": str(payload.get("source") or "Fincept QuantLib API"),
        "requestId": str(payload.get("requestId") or request_id or ""),
        "warnings": [str(item) for item in payload.get("warnings") or []],
    }


def scenario_result(params: dict, payload: dict, request_id: str) -> dict:
    positions = []
    for item in payload.get("positions") or []:
        if not isinstance(item, dict):
            continue
        symbol = _first(item, ("symbol", "asset"))
        pnl = _number(_first(item, ("pnl", "profitLoss", "loss", "change")))
        if symbol is None or pnl is None:
            continue
        positions.append(
            {
                "symbol": str(symbol),
                "pnl": pnl,
                "pnlPct": _number(_first(item, ("pnlPct", "pnl_pct", "percentage", "pct"))),
                "shockedPrice": _number(_first(item, ("shockedPrice", "shocked_price", "price"))),
            }
        )
    equity_before = _number(_first(payload, ("equityBefore", "equity_before", "initialEquity")))
    equity_after = _number(_first(payload, ("equityAfter", "equity_after", "finalEquity")))
    change = _number(_first(payload, ("equityChange", "equity_change", "pnl")))
    if change is None and equity_before is not None and equity_after is not None:
        change = equity_after - equity_before
    change_pct = _number(_first(payload, ("equityChangePct", "equity_change_pct", "pnlPct")))
    if change_pct is None and change is not None and equity_before:
        change_pct = round(change / equity_before * 100, 6)
    return {
        "provider": "fincept-api",
        "asOf": str(params.get("asOf") or ""),
        "scenario": str(params.get("scenario") or payload.get("scenario") or ""),
        "equityBefore": equity_before,
        "equityAfter": equity_after,
        "equityChange": change,
        "equityChangePct": change_pct,
        "positions": positions,
        "marginUsageBefore": _number(_first(payload, ("marginUsageBefore", "margin_usage_before"))),
        "marginUsageAfter": _number(_first(payload, ("marginUsageAfter", "margin_usage_after"))),
        "breachesAccountRisk": bool(
            _first(payload, ("breachesAccountRisk", "breaches_account_risk", "marginCall")) or False
        ),
        "breachedLimits": [str(item) for item in payload.get("breachedLimits") or []],
        "source": str(payload.get("source") or "Fincept QuantLib API"),
        "requestId": str(payload.get("requestId") or request_id or ""),
        "warnings": [str(item) for item in payload.get("warnings") or []],
    }


# -- methods ------------------------------------------------------------

def _client(factory):
    try:
        return factory(), ""
    except AnalyticsUnavailable as exc:
        return None, str(exc)


def _unavailable(params: dict, problem: str, **extra) -> dict:
    """A refusal that still satisfies the protocol: who answered, as of when, why not."""
    return {
        "provider": "fincept-api",
        "asOf": str(params.get("asOf") or ""),
        "unavailable": problem,
        "metrics": {},
        "riskContributions": [],
        "correlation": None,
        "warnings": [problem],
        **extra,
    }


def portfolio(request: dict, client_factory=build_client) -> dict:
    params = request.get("params") or {}
    client, problem = _client(client_factory)
    if client is None:
        return _unavailable(params, problem)
    payload, request_id = client.post(ENDPOINTS["portfolio"], portfolio_body(params))
    if params.get("optimize"):
        # Optimization is a separate, separately billed call, so it is only made
        # when the caller asked for it.
        try:
            weights, weights_request_id = client.post(ENDPOINTS["optimize"], portfolio_body(params))
            payload.setdefault("weights", weights.get("weights") or weights.get("optimalWeights") or {})
            request_id = request_id or weights_request_id
        except Exception as exc:  # noqa: BLE001 - risk numbers still stand without weights
            payload.setdefault("warnings", []).append(f"组合优化失败：{type(exc).__name__}: {exc}")
    return portfolio_result(params, payload, request_id)


def scenario(request: dict, client_factory=build_client) -> dict:
    params = request.get("params") or {}
    client, problem = _client(client_factory)
    if client is None:
        return _unavailable(params, problem, scenario=str(params.get("scenario") or ""), positions=[])
    payload, request_id = client.post(ENDPOINTS["scenario"], scenario_body(params))
    return scenario_result(params, payload, request_id)


def health(_: dict) -> dict:
    """Report credential presence and shape. Never calls a billed endpoint."""
    base_url = str(os.environ.get("FINCEPT_API_URL") or DEFAULT_BASE_URL).strip()
    key = str(os.environ.get("FINCEPT_API_KEY") or "").strip()
    return {
        "ok": True,
        "protocol": PROTOCOL_VERSION,
        "baseUrl": base_url,
        # Presence only. Not even a suffix: a key must not appear in a response,
        # a log line or a database row.
        "credentialPresent": bool(key),
        "endpoints": ENDPOINTS,
        "runtimeReady": bool(key),
        "note": "只使用官方 REST API；本适配器不包含任何下单能力" if key else "未配置 FINCEPT_API_KEY",
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


METHODS = {"health": health, "analytics.portfolio": portfolio, "analytics.scenario": scenario}


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
