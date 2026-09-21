"""Bybit risk-limit tiers, maintenance margin and liquidation.

The first prototype approximated every contract with one fixed maintenance
margin rate (0.5%) and one global leverage cap. The venue actually publishes a
ladder per contract: the leverage a position may use and the maintenance margin
it must post both depend on the notional that position carries. A 300k BTC
position and a 5k AAPL position therefore do not share a margin rate.

This module holds that ladder and the arithmetic that follows from it, so the
paper book, the backtest and any risk readout cannot drift into three answers.
Every number here is either read from the venue or derived from venue numbers by
a stated formula; nothing is a remembered constant.

Model used (Bybit isolated-margin linear perpetuals):

    maintenance margin = notional x mmr - mmDeduction
    liquidation price  = entry -/+ (margin - maintenance margin) / qty
                         (minus for a long, plus for a short)

which reduces to the older `entry x (1 -/+ 1/L) / (1 -/+ mmr)` only when one
leverage applies to the whole position and mmDeduction is zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# A ladder nobody published is not a ladder: without venue data the caller is
# told so instead of being handed the old approximation as if it were current.
DEFAULT_SOURCE = "bybit:risk-limit"


@dataclass(frozen=True)
class RiskTier:
    """One rung: the notional ceiling it covers and what it costs to hold."""

    tier_id: int
    max_leverage: float
    maintenance_margin_rate: float
    risk_limit_value: float
    mm_deduction: float = 0.0
    initial_margin_rate: float | None = None
    lowest_risk: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "tierId": self.tier_id,
            "maxLeverage": self.max_leverage,
            "maintenanceMarginRate": self.maintenance_margin_rate,
            "riskLimitValue": self.risk_limit_value,
            "mmDeduction": self.mm_deduction,
            "initialMarginRate": self.initial_margin_rate,
            "lowestRisk": self.lowest_risk,
        }


@dataclass(frozen=True)
class RiskProfile:
    """The ladder for one instrument, newest first from the venue."""

    venue_symbol: str
    tiers: tuple[RiskTier, ...]
    source: str = DEFAULT_SOURCE
    synced_at: int | None = None
    notional_step: float | None = None
    tick_size: float | None = None
    qty_step: float | None = None
    min_order_notional: float | None = None
    leverage_step: float | None = None
    warnings: tuple[str, ...] = ()

    # -- ladder reads ----------------------------------------------------
    @property
    def max_leverage(self) -> float | None:
        """The loosest leverage the venue allows at the smallest notional."""
        if not self.tiers:
            return None
        return max(tier.max_leverage for tier in self.tiers)

    @property
    def min_maintenance_margin_rate(self) -> float | None:
        if not self.tiers:
            return None
        return min(tier.maintenance_margin_rate for tier in self.tiers)

    def tier_for(self, notional: float, *, leverage: float | None = None) -> RiskTier | None:
        """The rung a position of this size sits in.

        A tier is eligible when the notional fits under its limit and, when a
        leverage is given, when the venue would actually allow that leverage
        there. The tightest eligible rung is chosen, because a position that
        grows past its rung is re-margined at the stricter one.
        """
        if not self.tiers:
            return None
        size = abs(float(notional))
        eligible = [
            tier
            for tier in self.tiers
            if size <= tier.risk_limit_value and (leverage is None or leverage <= tier.max_leverage)
        ]
        if eligible:
            return min(eligible, key=lambda tier: tier.risk_limit_value)
        # Larger than every rung, or more leverage than any rung allows: the
        # strictest published rung is the honest answer, and the caller can see
        # `leverage_allowed` to know the request was out of range.
        return max(self.tiers, key=lambda tier: tier.risk_limit_value)

    def leverage_allowed(self, notional: float, leverage: float) -> bool:
        tier = self.tier_for(notional, leverage=leverage)
        return bool(tier and leverage <= tier.max_leverage)

    def max_leverage_for(self, notional: float) -> float | None:
        tier = self.tier_for(notional)
        return tier.max_leverage if tier else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "venueSymbol": self.venue_symbol,
            "source": self.source,
            "syncedAt": self.synced_at,
            "tiers": [tier.as_dict() for tier in self.tiers],
            "maxLeverage": self.max_leverage,
            "minMaintenanceMarginRate": self.min_maintenance_margin_rate,
            "notionalStep": self.notional_step,
            "tickSize": self.tick_size,
            "qtyStep": self.qty_step,
            "minOrderNotional": self.min_order_notional,
            "leverageStep": self.leverage_step,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_rows(cls, venue_symbol: str, rows: Iterable[dict], **meta: Any) -> "RiskProfile":
        """Build a profile from venue rows, dropping the ones that are unusable."""
        tiers: list[RiskTier] = []
        for row in rows:
            tier = _tier_from_row(row)
            if tier is not None:
                tiers.append(tier)
        tiers.sort(key=lambda tier: tier.risk_limit_value)
        return cls(venue_symbol=venue_symbol, tiers=tuple(tiers), **meta)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _tier_from_row(row: dict) -> RiskTier | None:
    """One venue row, or None when it cannot describe a rung."""
    limit = _number(row.get("riskLimitValue"))
    mmr = _number(row.get("maintenanceMargin"))
    leverage = _number(row.get("maxLeverage"))
    if limit is None or mmr is None or leverage is None or limit <= 0 or mmr <= 0 or leverage < 1:
        return None
    return RiskTier(
        tier_id=int(_number(row.get("id")) or len(str(limit))),
        max_leverage=leverage,
        maintenance_margin_rate=mmr,
        risk_limit_value=limit,
        # Missing or empty on most linear contracts; only used when published.
        mm_deduction=_number(row.get("mmDeduction")) or 0.0,
        initial_margin_rate=_number(row.get("initialMargin")),
        lowest_risk=bool(row.get("isLowestRisk")),
    )


@dataclass
class TradeRisk:
    """The risk numbers that belong to one open position."""

    tier: RiskTier | None
    maintenance_margin_rate: float
    maintenance_margin: float
    initial_margin: float
    leverage: float
    max_leverage: float | None
    liq_price: float | None
    margin_ratio: float | None
    liquidation_distance_pct: float | None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tierId": self.tier.tier_id if self.tier else None,
            "maxLeverage": self.max_leverage,
            "maintenanceMarginRate": self.maintenance_margin_rate,
            "maintenanceMargin": round(self.maintenance_margin, 6),
            "initialMargin": round(self.initial_margin, 6),
            "leverage": self.leverage,
            "liqPrice": None if self.liq_price is None else round(self.liq_price, 8),
            "marginRatio": None if self.margin_ratio is None else round(self.margin_ratio, 6),
            "liquidationDistancePct": (
                None if self.liquidation_distance_pct is None else round(self.liquidation_distance_pct, 4)
            ),
            "warnings": list(self.warnings),
        }


def maintenance_margin_for(tier: RiskTier | None, notional: float, fallback_rate: float) -> float:
    """Maintenance margin required for a position of this size.

    `notional x mmr - mmDeduction`, floored at zero. Without a tier the caller's
    fallback rate applies, which is what the older code used unconditionally.
    """
    size = abs(float(notional))
    if tier is None:
        return size * fallback_rate
    return max(0.0, size * tier.maintenance_margin_rate - tier.mm_deduction)


def liquidation_price(
    direction: int,
    entry_price: float,
    quantity: float,
    margin: float,
    mmr: float = 0.0,
    mm_deduction: float = 0.0,
    *,
    leverage: float | None = None,
) -> float | None:
    """Isolated-margin liquidation price.

    Solve `margin -/+ qty x (price - entry) = |qty x price| x mmr - mmDeduction`
    for `price`, with the sign chosen by direction. Returns None when no positive
    price liquidates the position, which is a real outcome rather than a missing
    value: a 1x long cannot be liquidated by price, and the old leverage-only
    approximation wrongly claimed it could.
    """
    qty = abs(float(quantity))
    if qty <= 0 or entry_price <= 0:
        return None
    if margin <= 0 and leverage is None:
        return None
    posted = float(margin)
    if posted <= 0 and leverage:
        posted = qty * entry_price / float(leverage)
    if posted <= 0:
        return None
    deduction = float(mm_deduction or 0.0)
    rate = float(mmr or 0.0)
    if direction == 1:
        denominator = qty * (1 - rate)
        if denominator <= 0:
            return None
        price = -(posted - deduction - qty * entry_price) / denominator
    else:
        denominator = qty * (1 + rate)
        if denominator <= 0:
            return None
        price = (posted + deduction + qty * entry_price) / denominator
    if price <= 0 or price != price:
        return None
    return price


def margin_ratio(notional: float, remaining_margin: float, mmr: float) -> float | None:
    """Maintenance margin as a fraction of the margin that is left."""
    if remaining_margin <= 0:
        return None
    return abs(float(notional)) * float(mmr) / remaining_margin


def position_risk(
    *,
    direction: int,
    entry_price: float,
    quantity: float,
    leverage: float,
    profile: RiskProfile | None,
    mark_price: float | None = None,
    reference_notional: float | None = None,
    fallback_maintenance_rate: float = 0.005,
    fallback_max_leverage: float = 100.0,
) -> TradeRisk:
    """Everything a risk readout needs for one position.

    `reference_notional` is the size the tier is chosen from. It defaults to the
    entry notional, which is what the venue uses when the position is opened; a
    caller that knows the current mark may pass the marked notional instead so a
    position that grew into a stricter rung is reported there.
    """
    qty = abs(float(quantity))
    if qty <= 0 or entry_price <= 0:
        raise ValueError("数量与入场价必须大于 0")
    notional = reference_notional if reference_notional is not None else qty * entry_price
    tier = profile.tier_for(notional, leverage=leverage) if profile else None
    mmr = tier.maintenance_margin_rate if tier else fallback_maintenance_rate
    max_leverage = tier.max_leverage if tier else fallback_max_leverage
    margin = qty * entry_price / leverage
    maintenance = maintenance_margin_for(tier, qty * entry_price, fallback_maintenance_rate)
    marked_notional = qty * mark_price if mark_price else qty * entry_price
    warnings: list[str] = []
    if profile is None or not profile.tiers:
        symbol = profile.venue_symbol if profile else "该合约"
        warnings.append(
            f"本地没有 {symbol} 的风险档位，按固定维持保证金率 {fallback_maintenance_rate * 100:g}% 估算"
        )
    if leverage > max_leverage:
        warnings.append(f"{leverage:g}x 超过该档位允许的 {max_leverage:g}x，交易所会拒绝该仓位")
    if tier and abs(float(notional)) > tier.risk_limit_value:
        warnings.append(f"名义额 {abs(float(notional)):,.0f} 超过该档位上限 {tier.risk_limit_value:,.0f}")

    liq = liquidation_price(
        direction,
        entry_price,
        qty,
        margin,
        mmr,
        tier.mm_deduction if tier else 0.0,
    )
    ratio = None
    distance = None
    if mark_price:
        pnl = direction * qty * (mark_price - entry_price)
        ratio = margin_ratio(marked_notional, margin + pnl, mmr)
        if liq:
            distance = abs(mark_price - liq) / mark_price * 100
    return TradeRisk(
        tier=tier,
        maintenance_margin_rate=mmr,
        maintenance_margin=maintenance,
        initial_margin=margin,
        leverage=leverage,
        max_leverage=max_leverage,
        liq_price=liq,
        margin_ratio=ratio,
        liquidation_distance_pct=distance,
        warnings=warnings,
    )


def leverage_violation(
    profile: RiskProfile | None,
    notional: float,
    leverage: float,
    *,
    fallback_max_leverage: float = 100.0,
) -> str | None:
    """Why this leverage is not allowed at this size, or None when it is."""
    cap = profile.max_leverage_for(notional) if profile else fallback_max_leverage
    if cap is None:
        return None
    if leverage > cap:
        return f"{leverage:g}x 超过该名义额档位允许的最高杠杆 {cap:g}x"
    return None

# -- local cache -------------------------------------------------------------
#
# The ladder changes rarely and the venue rate-limits, so it is cached in SQLite
# and only refreshed when it is older than this.
RISK_TIER_TTL_MS = 24 * 60 * 60 * 1000
MARK_MAX_BARS = 1_000


def tier_rows_for_db(profile: RiskProfile) -> list[dict]:
    """The ladder in the shape `Database.upsert_risk_tiers` expects."""
    return [
        {
            "tier_id": tier.tier_id,
            "risk_limit_value": tier.risk_limit_value,
            "maintenance_margin_rate": tier.maintenance_margin_rate,
            "mm_deduction": tier.mm_deduction,
            "max_leverage": tier.max_leverage,
            "initial_margin_rate": tier.initial_margin_rate,
            "lowest_risk": tier.lowest_risk,
        }
        for tier in profile.tiers
    ]


def profile_from_rows(venue_symbol: str, rows: list[dict], **meta: Any) -> RiskProfile:
    """Rebuild a profile from stored rows, keeping their provenance."""
    if not rows:
        return RiskProfile(venue_symbol=venue_symbol, tiers=())
    source = str(rows[0].get("source") or DEFAULT_SOURCE)
    synced_at = rows[0].get("synced_at")
    return RiskProfile.from_rows(
        venue_symbol,
        [
            {
                "id": row["tier_id"],
                "riskLimitValue": row["risk_limit_value"],
                "maintenanceMargin": row["maintenance_margin_rate"],
                "mmDeduction": row.get("mm_deduction"),
                "maxLeverage": row["max_leverage"],
                "initialMargin": row.get("initial_margin_rate"),
                "isLowestRisk": row.get("lowest_risk"),
            }
            for row in rows
        ],
        source=source,
        synced_at=int(synced_at) if synced_at else None,
        **meta,
    )


class RiskBook:
    """Local risk ladders, refreshed from the venue when they are stale."""

    def __init__(self, db, venue: str = "bybit", ttl_ms: int = RISK_TIER_TTL_MS):
        self.db = db
        self.venue = venue
        self.ttl_ms = ttl_ms
        self.synced: list[str] = []
        self.failed: list[str] = []

    def cached(self, venue_symbol: str, **meta: Any) -> RiskProfile:
        """The stored ladder for one contract, or an empty profile."""
        return profile_from_rows(venue_symbol, self.db.load_risk_tiers(self.venue, venue_symbol), **meta)

    def is_stale(self, venue_symbol: str, now: int | None = None) -> bool:
        rows = self.db.load_risk_tiers(self.venue, venue_symbol)
        if not rows:
            return True
        newest = max(int(row.get("synced_at") or 0) for row in rows)
        stamp = int(now if now is not None else _now_ms())
        return (stamp - newest) > self.ttl_ms

    def sync(self, client, symbols: Iterable[str], *, force: bool = False, now: int | None = None) -> dict[str, Any]:
        """Refresh stale ladders from the venue; keep the old one on failure."""
        stamp = int(now if now is not None else _now_ms())
        refreshed = 0
        for symbol in symbols:
            if not force and not self.is_stale(symbol, now=stamp):
                continue
            try:
                rows = client.risk_limit(symbol)
            except Exception as exc:  # noqa: BLE001 - one contract must not stop the sweep
                self.failed.append(f"{symbol}: {type(exc).__name__}: {exc}")
                continue
            profile = RiskProfile.from_rows(symbol, rows, synced_at=stamp)
            if not profile.tiers:
                # An empty answer must not wipe a ladder that is still usable.
                self.failed.append(f"{symbol}: 交易所返回空档位，保留本地缓存")
                continue
            self.db.upsert_risk_tiers(self.venue, symbol, tier_rows_for_db(profile), source=profile.source, synced_at=stamp)
            self.synced.append(symbol)
            refreshed += 1
        return {
            "requested": len(list(symbols)),
            "refreshed": refreshed,
            "synced": list(self.synced),
            "failed": list(self.failed),
            "status": self.db.risk_tier_status(self.venue),
        }

    def sync_marks(self, client, venue_symbol: str, interval: str, *, limit: int = 600) -> int:
        """Pull the mark-price series for one contract and timeframe."""
        rows = client.mark_price_kline(venue_symbol, interval, limit=min(limit, MARK_MAX_BARS))
        return self.db.upsert_mark_candles(self.venue, venue_symbol, interval, rows, source="rest_mark")

    def marks(self, venue_symbol: str, interval: str, *, start_ts: int | None = None, end_ts: int | None = None) -> list[dict]:
        return self.db.load_mark_candles(self.venue, venue_symbol, interval, start_ts=start_ts, end_ts=end_ts)


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)

