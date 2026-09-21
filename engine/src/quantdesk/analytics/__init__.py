"""External portfolio analytics: QuantDesk inputs in, risk numbers out.

The split of responsibility is deliberate and worth stating once:

* QuantDesk owns positions, margin, mark prices and returns. They come from the
  paper engine and from Bybit history, and they are hashed and versioned here.
* Fincept (through its official API) owns the *calculations*: volatility, VaR,
  CVaR, correlation, risk contributions, optimization and stress scenarios.

Two consequences follow, and both are enforced rather than hoped for:

1. A stored result is tied to the market-data version it was computed from. If
   prices or positions move, the cached answer no longer describes reality and is
   not reused.
2. Risk output is advice. Nothing in this module can place an order, change a
   position or relax a local limit; it produces numbers and warnings that the
   paper engine and the UI display.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config.symbol_map import MAPPINGS, mapping_for, resolve_shocks, scenario_for
from ..research.external import STATUS_ERROR, STATUS_OK

ANALYTICS_VERSION = "analytics/1"

# One hour of history is the shortest window a risk number can be estimated from
# without pretending hourly noise is a distribution.
MIN_RETURN_POINTS = 30
DEFAULT_RETURN_POINTS = 200


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def input_hash(*, kind: str, positions: list[dict], parameters: dict, market_version: str) -> str:
    """The identity of one analytics question.

    Positions, parameters and the market-data version all go in: two calls that
    differ in any of them are different questions, and only an identical question
    may be answered from cache - a paid computation must not be repeated by
    accident, nor reused for a different portfolio.
    """
    material = _canonical(
        {
            "version": ANALYTICS_VERSION,
            "kind": kind,
            "positions": positions,
            "parameters": parameters,
            "marketVersion": market_version,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def positions_hash(positions: list[dict]) -> str:
    material = _canonical(
        [
            {
                "symbol": item.get("symbol"),
                "side": item.get("side"),
                "quantity": item.get("quantity"),
                "markPrice": item.get("markPrice"),
            }
            for item in positions
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


@dataclass
class PortfolioSnapshot:
    """QuantDesk's view of the book, in the shape the analytics call takes."""

    as_of: str
    base_currency: str
    positions: list[dict]
    returns: dict[str, list[dict]]
    market_version: str
    equity: float
    total_notional: float
    gross_exposure: float
    net_exposure: float
    margin_used: float
    warnings: list[str] = field(default_factory=list)

    def request_positions(self) -> list[dict]:
        return list(self.positions)

    def as_dict(self) -> dict:
        return {
            "asOf": self.as_of,
            "baseCurrency": self.base_currency,
            "positions": self.positions,
            "marketVersion": self.market_version,
            "equity": self.equity,
            "totalNotional": self.total_notional,
            "grossExposure": self.gross_exposure,
            "netExposure": self.net_exposure,
            "marginUsed": self.margin_used,
            "warnings": self.warnings,
        }


def build_snapshot(
    *,
    positions: list[dict],
    returns: dict[str, list[dict]],
    market_version: str,
    equity: float,
    margin_by_symbol: dict[str, float] | None = None,
    base_currency: str = "USDT",
    as_of: str | None = None,
) -> PortfolioSnapshot:
    """Assemble the request body from positions QuantDesk already holds.

    A position outside the fixed pool is refused rather than passed through: the
    external calculator must receive contract codes the engine can map back.
    """
    warnings: list[str] = []
    cleaned: list[dict] = []
    gross = 0.0
    net = 0.0
    margin_used = 0.0
    for item in positions:
        symbol = str(item.get("symbol") or "")
        try:
            mapping = mapping_for(symbol)
        except KeyError:
            warnings.append(f"{symbol or '未知合约'} 不在固定合约池内，已排除在组合风险之外")
            continue
        quantity = float(item.get("quantity") or 0.0)
        mark = float(item.get("markPrice") or item.get("mark_price") or 0.0)
        notional = float(item.get("notional") or abs(quantity * mark))
        if notional <= 0:
            continue
        side = str(item.get("side") or "long").lower()
        signed = notional if side == "long" else -notional
        margin = float(
            (margin_by_symbol or {}).get(symbol)
            or item.get("margin")
            or 0.0
        )
        cleaned.append(
            {
                "symbol": symbol,
                "group": mapping.group,
                "side": "long" if side == "long" else "short",
                "quantity": quantity,
                "entryPrice": float(item.get("entryPrice") or item.get("entry_price") or mark),
                "markPrice": mark,
                "notional": round(notional, 8),
                "margin": round(margin, 8),
            }
        )
        gross += notional
        net += signed
        margin_used += margin
        if symbol not in returns or len(returns.get(symbol) or []) < MIN_RETURN_POINTS:
            warnings.append(f"{symbol} 的收益率样本不足 {MIN_RETURN_POINTS} 点，风险估计可能不可靠")
    return PortfolioSnapshot(
        as_of=as_of or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        base_currency=base_currency,
        positions=cleaned,
        returns={key: value for key, value in returns.items() if key in {item["symbol"] for item in cleaned}},
        market_version=market_version,
        equity=round(equity, 8),
        total_notional=round(gross, 8),
        gross_exposure=round(gross, 8),
        net_exposure=round(net, 8),
        margin_used=round(margin_used, 8),
        warnings=warnings,
    )


@dataclass
class AnalyticsOutcome:
    """What the engine learned, whether or not the provider answered."""

    ok: bool
    kind: str
    provider: str = ""
    result: dict | None = None
    cached: bool = False
    unavailable: str = ""
    error: str = ""
    request_id: str = ""
    duration_ms: int = 0
    market_version: str = ""
    input_hash: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "provider": self.provider,
            "result": self.result,
            "cached": self.cached,
            "unavailable": self.unavailable,
            "error": self.error,
            "requestId": self.request_id,
            "durationMs": self.duration_ms,
            "marketVersion": self.market_version,
            "inputHash": self.input_hash,
            "warnings": self.warnings,
        }


class PortfolioAnalyticsService:
    """Runs one analytics question: cache, call, store, and report honestly."""

    def __init__(self, db, provider: Callable[[str, dict], dict], *, config: dict | None = None, now=None):
        self.db = db
        # `provider(kind, params)` performs the actual external call. Injected so
        # this layer can be tested without a network or a paid subscription.
        self.provider = provider
        self.config = config or {}
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))

    def _retention_days(self) -> int:
        return int((self.config.get("retention_days") or {}).get("analytics") or 400)

    def _cache_ttl_seconds(self, kind: str) -> int:
        minutes = (self.config.get("cache_ttl_minutes") or {}).get(kind)
        if isinstance(minutes, (int, float)) and minutes >= 0:
            return int(minutes) * 60
        return 15 * 60

    def _cached(self, key: str, kind: str) -> dict | None:
        row = self.db.find_external_analytics(key, kind=kind)
        if row is None:
            return None
        ttl = self._cache_ttl_seconds(kind)
        if ttl <= 0:
            return None
        age = self._now().timestamp() * 1000 - int(row.get("updated_ts") or 0)
        if age > ttl * 1000:
            return None
        try:
            return json.loads(row.get("result_json") or "null")
        except (TypeError, ValueError):
            return None

    def _remember(self, *, kind: str, key: str, snapshot: PortfolioSnapshot, outcome: dict, status: str,
                  result: dict | None, error: str | None, request_id: str, duration_ms: int) -> None:
        self.db.record_external_analytics(
            {
                "provider": outcome.get("provider") or "fincept-api",
                "kind": kind,
                "as_of": snapshot.as_of,
                "input_hash": key,
                # Every row names the market-data version it was computed from.
                "market_snapshot_version": snapshot.market_version,
                "positions_hash": positions_hash(snapshot.positions),
                "parameters_json": _canonical(
                    {"baseCurrency": snapshot.base_currency, "positions": len(snapshot.positions)}
                ),
                "result_json": _canonical(result) if result is not None else None,
                "request_id": request_id or None,
                "status": status,
                "error": error,
                "duration_ms": duration_ms,
                "updated_ts": int(self._now().timestamp() * 1000),
            }
        )

    def portfolio(
        self, snapshot: PortfolioSnapshot, *, confidence: float = 0.95, optimize: bool = False, force: bool = False
    ) -> AnalyticsOutcome:
        parameters = {"confidence": confidence, "optimize": bool(optimize)}
        key = input_hash(
            kind="portfolio", positions=snapshot.positions, parameters=parameters,
            market_version=snapshot.market_version,
        )
        if not force:
            cached = self._cached(key, "portfolio")
            if cached is not None:
                return AnalyticsOutcome(
                    ok=True, kind="portfolio", provider=cached.get("provider", "fincept-api"),
                    result=cached, cached=True, market_version=snapshot.market_version,
                    input_hash=key, warnings=list(snapshot.warnings),
                )
        params = {
            "asOf": snapshot.as_of,
            "baseCurrency": snapshot.base_currency,
            "confidence": confidence,
            "positions": snapshot.positions,
            "returns": snapshot.returns,
            "marketSnapshotVersion": snapshot.market_version,
            "optimize": bool(optimize),
        }
        started = time.monotonic()
        try:
            outcome = self.provider("portfolio", params)
        except Exception as exc:  # noqa: BLE001 - the run continues without analytics
            error = f"{type(exc).__name__}: {exc}"
            duration = int((time.monotonic() - started) * 1000)
            self._remember(kind="portfolio", key=key, snapshot=snapshot, outcome={}, status=STATUS_ERROR,
                           result=None, error=error, request_id="", duration_ms=duration)
            return AnalyticsOutcome(
                ok=False, kind="portfolio", error=error, market_version=snapshot.market_version,
                input_hash=key, duration_ms=duration, warnings=list(snapshot.warnings),
            )
        duration = int((time.monotonic() - started) * 1000)
        unavailable = str(outcome.get("unavailable") or "")
        if unavailable:
            self._remember(kind="portfolio", key=key, snapshot=snapshot, outcome=outcome,
                           status=STATUS_ERROR, result=None, error=unavailable, request_id="",
                           duration_ms=duration)
            return AnalyticsOutcome(
                ok=False, kind="portfolio", unavailable=unavailable, duration_ms=duration,
                market_version=snapshot.market_version, input_hash=key, warnings=list(snapshot.warnings),
            )
        self._remember(kind="portfolio", key=key, snapshot=snapshot, outcome=outcome, status=STATUS_OK,
                       result=outcome, error=None, request_id=str(outcome.get("requestId") or ""),
                       duration_ms=duration)
        return AnalyticsOutcome(
            ok=True, kind="portfolio", provider=str(outcome.get("provider") or "fincept-api"),
            result=outcome, request_id=str(outcome.get("requestId") or ""), duration_ms=duration,
            market_version=snapshot.market_version, input_hash=key,
            warnings=list(snapshot.warnings) + list(outcome.get("warnings") or []),
        )

    def scenario(
        self, snapshot: PortfolioSnapshot, *, scenario_id: str, shocks: dict[str, float] | None = None,
        confidence: float = 0.95, force: bool = False,
    ) -> AnalyticsOutcome:
        scenario = scenario_for(scenario_id)
        # The rules are resolved to one shock per contract here, so the request the
        # provider receives is explicit and the result is reproduciable.
        resolved = resolve_shocks(shocks if shocks is not None else scenario["shocks"])
        parameters = {"scenario": scenario_id, "shocks": resolved, "confidence": confidence}
        key = input_hash(
            kind="scenario", positions=snapshot.positions, parameters=parameters,
            market_version=snapshot.market_version,
        )
        if not force:
            cached = self._cached(key, "scenario")
            if cached is not None:
                return AnalyticsOutcome(
                    ok=True, kind="scenario", provider=cached.get("provider", "fincept-api"),
                    result=cached, cached=True, market_version=snapshot.market_version,
                    input_hash=key, warnings=list(snapshot.warnings),
                )
        params = {
            "asOf": snapshot.as_of,
            "baseCurrency": snapshot.base_currency,
            "confidence": confidence,
            "scenario": scenario_id,
            "shocks": resolved,
            "positions": snapshot.positions,
            "marketSnapshotVersion": snapshot.market_version,
        }
        started = time.monotonic()
        try:
            outcome = self.provider("scenario", params)
        except Exception as exc:  # noqa: BLE001 - contained like any provider failure
            error = f"{type(exc).__name__}: {exc}"
            duration = int((time.monotonic() - started) * 1000)
            self._remember(kind="scenario", key=key, snapshot=snapshot, outcome={}, status=STATUS_ERROR,
                           result=None, error=error, request_id="", duration_ms=duration)
            return AnalyticsOutcome(
                ok=False, kind="scenario", error=error, market_version=snapshot.market_version,
                input_hash=key, duration_ms=duration, warnings=list(snapshot.warnings),
            )
        duration = int((time.monotonic() - started) * 1000)
        unavailable = str(outcome.get("unavailable") or "")
        if unavailable:
            self._remember(kind="scenario", key=key, snapshot=snapshot, outcome=outcome, status=STATUS_ERROR,
                           result=None, error=unavailable, request_id="", duration_ms=duration)
            return AnalyticsOutcome(
                ok=False, kind="scenario", unavailable=unavailable, duration_ms=duration,
                market_version=snapshot.market_version, input_hash=key, warnings=list(snapshot.warnings),
            )
        self._remember(kind="scenario", key=key, snapshot=snapshot, outcome=outcome, status=STATUS_OK,
                       result=outcome, error=None, request_id=str(outcome.get("requestId") or ""),
                       duration_ms=duration)
        return AnalyticsOutcome(
            ok=True, kind="scenario", provider=str(outcome.get("provider") or "fincept-api"),
            result=outcome, request_id=str(outcome.get("requestId") or ""), duration_ms=duration,
            market_version=snapshot.market_version, input_hash=key,
            warnings=list(snapshot.warnings) + list(outcome.get("warnings") or []),
        )

    def prune(self) -> dict:
        now_ms = int(self._now().timestamp() * 1000)
        days = self._retention_days()
        return {"analytics": self.db.prune_external_analytics(max_age_days=days, now_ms=now_ms)}

    def current_version(self) -> str:
        """The market-data version the last successful run was computed from."""
        rows = self.db.list_external_analytics(limit=1)
        return str(rows[0].get("market_snapshot_version") or "") if rows else ""


def stale_versions(db, *, limit: int = 50) -> list[dict]:
    """Stored results whose market-data version is no longer the current one.

    The UI shows these as "based on an older snapshot" instead of presenting a
    stale risk figure as if it described the book in front of the user.
    """
    return [
        {
            "inputHash": row.get("input_hash"),
            "kind": row.get("kind"),
            "marketVersion": row.get("market_snapshot_version"),
            "asOf": row.get("as_of"),
            "updatedTs": row.get("updated_ts"),
        }
        for row in db.list_external_analytics(limit=limit)
    ]


def pool_symbols() -> list[str]:
    return list(MAPPINGS)


def paper_config(home):
    """The paper engine's config object, built the way the trading API builds it."""
    from ..backtest import DEFAULT_SLIPPAGE_BPS, DEFAULT_TAKER_FEE_BPS
    from ..config.settings import load_app_config
    from ..paper.engine import DEFAULT_MAINTENANCE_MARGIN_RATE, PaperConfig

    config = load_app_config(home)
    paper = config.paper or {}
    return PaperConfig(
        initial_cash=float(config.default_cash_usd),
        taker_fee_bps=float(paper.get("taker_fee_bps", DEFAULT_TAKER_FEE_BPS)),
        slippage_bps=float(paper.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)),
        maintenance_margin_rate=float(paper.get("maintenance_margin_rate", DEFAULT_MAINTENANCE_MARGIN_RATE)),
    )


def stored_marks(db, symbols, *, venue: str = "bybit", interval: str = "1h") -> dict[str, float]:
    """The newest stored mark per symbol: the price the local data supports.

    Used for research inputs, which must be reproducible. A mark fetched live
    would make the same book produce different numbers seconds apart, and a
    "reproducible" study that silently reads a moving price is not reproducible.
    """
    prices: dict[str, float] = {}
    for symbol in symbols:
        rows = db.load_mark_candles(venue, symbol, interval, limit=1)
        if rows:
            prices[symbol] = float(rows[-1]["close"])
    return prices


def paper_snapshot(home, *, points: int = 200, db=None, live_marks: bool = True) -> PortfolioSnapshot:
    """The analytics input built from the paper book and QuantDesk's own history.

    Lives here rather than in the API layer because both the risk page and the
    research prompt need the same book, priced the same way: two builders would
    eventually disagree, and the disagreement would be about money.

    `live_marks=False` prices the book from stored marks only. The risk page wants
    the current price; a research input wants the reproducible one.
    """
    from ..datahub.db import Database
    from ..datahub.view import read_history
    from ..paper.engine import PaperEngine

    db = db or Database(home / "quantdesk.db")
    engine = PaperEngine(db, paper_config(home))
    if live_marks:
        account = engine.account()
    else:
        held = [row["symbol"] for row in engine.open_positions()]
        account = engine.account(mark_prices=stored_marks(db, held))
    positions = [
        {
            "symbol": view.symbol,
            "side": view.side,
            "quantity": view.qty,
            "entryPrice": view.entry_price,
            "markPrice": view.mark_price or view.entry_price,
            "notional": view.notional,
            "margin": view.margin,
        }
        for view in account.positions
    ]
    returns: dict[str, list[dict]] = {}
    versions: list[str] = []
    for item in positions:
        symbol = item["symbol"]
        try:
            history = read_history(db, symbol=symbol, interval="1h", bars=max(points, 30), with_funding=False)
        except Exception:  # noqa: BLE001 - a symbol without history simply has no returns
            continue
        closes = [(int(row["ts"]), float(row["close"])) for row in history.bars]
        series: list[dict] = []
        for index in range(1, len(closes)):
            previous = closes[index - 1][1]
            if previous <= 0:
                continue
            series.append({"time": closes[index][0], "return": round(closes[index][1] / previous - 1, 10)})
        returns[symbol] = series[-points:]
        if history.version:
            versions.append(str(history.version))
    snapshot = build_snapshot(
        positions=positions,
        returns=returns,
        market_version="|".join(sorted(set(versions))) or "no-history",
        equity=account.equity,
    )
    snapshot.warnings.extend(account.warnings)
    return snapshot
