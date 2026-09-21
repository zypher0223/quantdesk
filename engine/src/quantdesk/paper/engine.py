"""Paper trading: mark-price valuation, margin, funding, and liquidation preview.

Nothing here places an order. Positions are recorded against the live mark price
so risk is visible before any execution layer exists, and every realised result
lands in the append-only journal with a content hash.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from math import floor

from ..backtest import MAX_LEVERAGE
from ..config.instruments import require_instrument
from ..config.settings import configured_proxy
from ..datahub.bybit import BybitClient
from ..datahub.db import Database
from ..datahub.market_service import get_market_service
from ..risk import RiskProfile, leverage_violation, position_risk

DEFAULT_MAINTENANCE_MARGIN_RATE = 0.005
DEFAULT_TAKER_FEE_BPS = 10.0


class PaperError(RuntimeError):
    """A rejected paper action, with the reason stated plainly."""


@dataclass
class PaperConfig:
    initial_cash: float = 100_000.0
    taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS
    slippage_bps: float = 5.0
    maintenance_margin_rate: float = DEFAULT_MAINTENANCE_MARGIN_RATE
    max_leverage: float = MAX_LEVERAGE


@dataclass
class PositionView:
    id: int
    venue: str
    symbol: str
    display_symbol: str
    side: str
    qty: float
    entry_price: float
    leverage: float
    liq_price: float | None
    mark_price: float | None
    notional: float
    margin: float
    unrealized_pnl: float | None
    unrealized_pct: float | None
    margin_ratio: float | None
    distance_to_liq_pct: float | None
    opened_ts: int
    notes: str
    fees_paid: float = 0.0
    protective_orders: list[dict] = field(default_factory=list)
    # Which venue risk rung this position is margined in, when a ladder is known.
    risk_tier_id: int | None = None
    maintenance_margin_rate: float | None = None
    max_leverage: float | None = None
    risk_warnings: list[str] = field(default_factory=list)


@dataclass
class PaperAccount:
    cash: float
    initial_cash: float
    realized_pnl: float
    unrealized_pnl: float
    fees_paid: float
    funding_paid: float
    funding_received: float
    funding_net: float
    equity: float
    margin_used: float
    free_margin: float
    positions: list[PositionView] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mark_prices_at: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _round_to(value: float, step: float | None) -> float:
    if not step or step <= 0:
        return value
    return round(value / step) * step


def _floor_to(value: float, step: float | None) -> float:
    if not step or step <= 0:
        return value
    return floor((value + step * 1e-12) / step) * step


class PaperEngine:
    """Position bookkeeping over the live mark price."""

    def __init__(self, db: Database, config: PaperConfig | None = None, risk_book=None):
        self.db = db
        self.config = config or PaperConfig()
        # The venue ladder, when one is available locally. Without it every
        # position falls back to the configured constant rate, which is what the
        # first prototype used for every contract.
        if risk_book is None:
            from ..risk import RiskBook

            risk_book = RiskBook(db)
        self.risk_book = risk_book

    def risk_profile(self, venue_symbol: str) -> RiskProfile:
        return self.risk_book.cached(venue_symbol)

    # -- mark price ------------------------------------------------------
    def mark_prices(self, symbols: list[str], client: BybitClient | None = None) -> dict[str, float]:
        """Live mark price per venue symbol; missing entries are left out.

        The in-process market service is the single source of market state, so it
        answers first. Only symbols it does not currently hold fall through to a
        direct REST read, which keeps paper risk correct even while the stream is
        reconnecting -- and never invents a price for a symbol with no mark.
        """
        wanted = [symbol for symbol in dict.fromkeys(symbols) if symbol]
        prices: dict[str, float] = {}
        for symbol in wanted:
            value = self._service_mark(symbol)
            if value:
                prices[symbol] = value
        missing = [symbol for symbol in wanted if symbol not in prices]
        if not missing:
            return prices
        owned = client is None
        client = client or BybitClient(proxy=configured_proxy(), timeout=12.0)
        try:
            for symbol in missing:
                try:
                    row = client.ticker("linear", symbol)
                except Exception:  # noqa: BLE001 - one bad ticker must not hide the book
                    continue
                value = row.get("markPrice") or row.get("lastPrice")
                if value:
                    prices[symbol] = float(value)
        finally:
            if owned:
                client.close()
        return prices

    def _service_mark(self, symbol: str) -> float | None:
        """Mark price from the shared market service, or None when it has none.

        A quote the service itself calls stale is treated as absent on purpose:
        sizing a position, pricing an exit or settling funding from a frozen quote
        would write that stale price into the append-only journal. Stale means the
        caller falls through to the direct REST read.
        """
        try:
            service = get_market_service(self.db.path.parent)
        except Exception:  # noqa: BLE001 - a missing service must not break paper risk
            return None
        try:
            if service.stale(symbol):
                return None
            return service.mark_price(symbol)
        except Exception:  # noqa: BLE001 - same: fall back to REST instead of failing
            return None

    # -- state -----------------------------------------------------------
    def open_positions(self) -> list[dict]:
        return self.db.query(
            "SELECT * FROM positions WHERE closed_ts IS NULL ORDER BY id"
        )

    def account(self, mark_prices: dict[str, float] | None = None) -> PaperAccount:
        rows = self.open_positions()
        cash = float(self.db.kv_get("paper_cash") or self.config.initial_cash)
        realized = float(self.db.kv_get("paper_realized") or 0.0)
        fees = float(self.db.kv_get("paper_fees") or 0.0)
        funding_net = float(self.db.kv_get("paper_funding") or 0.0)
        funding_paid = float(self.db.kv_get("paper_funding_paid") or 0.0)
        funding_received = float(self.db.kv_get("paper_funding_received") or 0.0)
        if mark_prices is None:
            mark_prices = self.mark_prices([row["symbol"] for row in rows]) if rows else {}

        views: list[PositionView] = []
        unrealized = 0.0
        margin_used = 0.0
        warnings: list[str] = []
        for row in rows:
            spec = None
            try:
                spec = require_instrument(row["symbol"])
            except ValueError:
                warnings.append(f"{row['symbol']} 不在固定合约池中，仍按已记录持仓估值")
            view = self._view(row, mark_prices.get(row["symbol"]), spec)
            if view.mark_price is None:
                warnings.append(f"{row['symbol']} 暂无标记价，未实现盈亏与本仓保证金率不可用")
            unrealized += view.unrealized_pnl or 0.0
            margin_used += view.margin
            views.append(view)

        equity = cash + unrealized
        return PaperAccount(
            cash=round(cash, 6),
            initial_cash=self.config.initial_cash,
            realized_pnl=round(realized, 6),
            unrealized_pnl=round(unrealized, 6),
            fees_paid=round(fees, 6),
            funding_paid=round(funding_paid, 6),
            funding_received=round(funding_received, 6),
            funding_net=round(funding_net, 6),
            equity=round(equity, 6),
            margin_used=round(margin_used, 6),
            free_margin=round(equity - margin_used, 6),
            positions=views,
            warnings=warnings,
            mark_prices_at=int(time.time() * 1000) if mark_prices else None,
        )

    def _view(self, row: dict, mark: float | None, spec) -> PositionView:
        direction = 1 if row["side"] == "long" else -1
        qty = float(row["qty"])
        entry = float(row["avg_price"])
        leverage = float(row["leverage"] or 1)
        notional = qty * (mark if mark is not None else entry)
        margin = qty * entry / leverage
        unrealized = direction * qty * (mark - entry) if mark is not None else None
        unrealized_pct = (unrealized / margin * 100) if (unrealized is not None and margin) else None
        # The rung is chosen from what the position is worth now, so one that has
        # grown into a stricter tier reports the stricter maintenance margin.
        risk = position_risk(
            direction=direction,
            entry_price=entry,
            quantity=qty,
            leverage=leverage,
            profile=self.risk_profile(row["symbol"]),
            mark_price=mark,
            reference_notional=notional,
            fallback_maintenance_rate=self.config.maintenance_margin_rate,
        )
        liq = row["liq_price"] or risk.liq_price
        ratio = risk.margin_ratio
        distance = risk.liquidation_distance_pct
        return PositionView(
            id=int(row["id"]),
            venue=row["venue"],
            symbol=row["symbol"],
            display_symbol=getattr(spec, "display_symbol", row["symbol"]),
            side=row["side"],
            qty=qty,
            entry_price=entry,
            leverage=leverage,
            liq_price=liq,
            mark_price=mark,
            notional=round(notional, 6),
            margin=round(margin, 6),
            unrealized_pnl=None if unrealized is None else round(unrealized, 6),
            unrealized_pct=None if unrealized_pct is None else round(unrealized_pct, 4),
            margin_ratio=None if ratio is None else round(ratio, 6),
            distance_to_liq_pct=None if distance is None else round(distance, 4),
            risk_tier_id=risk.tier.tier_id if risk.tier else None,
            maintenance_margin_rate=risk.maintenance_margin_rate,
            max_leverage=risk.max_leverage,
            risk_warnings=list(risk.warnings),
            opened_ts=int(row["updated_ts"]),
            notes=self.db.position_notes(int(row["id"])),
            fees_paid=round(float(row.get("entry_fee") or qty * entry * self.config.taker_fee_bps / 10_000), 6),
            protective_orders=[
                {
                    "id": int(order["id"]),
                    "type": order["order_type"],
                    "trigger_price": float(order["trigger_price"]),
                    "close_fraction": float(order["close_fraction"]),
                    "status": order["status"],
                }
                for order in self.db.paper_orders(int(row["id"]), status="open")
            ],
        )

    # -- actions ---------------------------------------------------------
    def open_position(
        self,
        symbol: str,
        side: str,
        *,
        notional: float | None = None,
        qty: float | None = None,
        leverage: float = 1.0,
        rationale: str = "",
        mark_price: float | None = None,
        mark_prices: dict[str, float] | None = None,
        tick_size: float | None = None,
        qty_step: float | None = None,
        min_order_notional: float = 5.0,
        max_leverage: float | None = None,
        stop_loss: float | None = None,
        take_profit_1: float | None = None,
        take_profit_2: float | None = None,
    ) -> PositionView:
        if side not in {"long", "short"}:
            raise PaperError("方向只能是 long 或 short")
        if not 1.0 <= leverage <= self.config.max_leverage:
            raise PaperError(f"杠杆必须在 1 到 {self.config.max_leverage:g} 之间")
        if max_leverage is not None and leverage > max_leverage:
            raise PaperError(f"{symbol.upper()} 当前交易所最大杠杆为 {max_leverage:g}x")
        if (notional is None) == (qty is None):
            raise PaperError("名义额与数量必须二选一")

        spec = require_instrument(symbol)
        open_rows = self.open_positions()
        symbols = sorted({row["symbol"] for row in open_rows} | {spec.venue_symbol})
        prices = dict(mark_prices or {})
        if mark_prices is None:
            needs_remote = [name for name in symbols if name != spec.venue_symbol or mark_price is None]
            if needs_remote:
                prices.update(self.mark_prices(needs_remote))
        if mark_price is not None:
            prices[spec.venue_symbol] = mark_price
        mark = prices.get(spec.venue_symbol)
        if not mark:
            raise PaperError(f"{spec.venue_symbol} 暂无标记价，无法开仓")
        missing = [row["symbol"] for row in open_rows if row["symbol"] not in prices]
        if missing:
            raise PaperError(f"已有持仓 {', '.join(sorted(set(missing)))} 暂无标记价，无法可靠核算保证金")

        if qty is None:
            qty = float(notional) / mark
        qty = _floor_to(qty, qty_step)
        if qty <= 0:
            raise PaperError("按交易所步长取整后数量为 0，请提高名义额")

        fill = _round_to(mark * (1 + self.config.slippage_bps / 10_000), tick_size) if side == "long" else _round_to(mark * (1 - self.config.slippage_bps / 10_000), tick_size)
        stop_loss = _round_to(float(stop_loss), tick_size) if stop_loss is not None else None
        take_profit_1 = _round_to(float(take_profit_1), tick_size) if take_profit_1 is not None else None
        take_profit_2 = _round_to(float(take_profit_2), tick_size) if take_profit_2 is not None else None
        self._validate_protective_levels(side, fill, stop_loss, take_profit_1, take_profit_2)
        value = qty * fill
        if value < min_order_notional:
            raise PaperError(f"名义额 {value:.2f} 低于交易所最小下单额 {min_order_notional:g}")

        profile = self.risk_profile(spec.venue_symbol)
        violation = leverage_violation(profile, value, leverage, fallback_max_leverage=MAX_LEVERAGE)
        if violation:
            raise PaperError(f"{violation}（名义额 {value:,.2f}）")

        margin = value / leverage
        account = self.account(prices)
        fee = value * self.config.taker_fee_bps / 10_000
        if margin + fee > account.free_margin:
            raise PaperError(
                f"保证金不足：本仓需要保证金及手续费 {margin + fee:,.2f}，可用 {account.free_margin:,.2f}"
                f"（权益 {account.equity:,.2f}，已占用 {account.margin_used:,.2f}）"
            )

        opened_risk = position_risk(
            direction=1 if side == "long" else -1,
            entry_price=fill,
            quantity=qty,
            leverage=leverage,
            profile=profile,
            fallback_maintenance_rate=self.config.maintenance_margin_rate,
        )
        liq = opened_risk.liq_price
        now = int(time.time() * 1000)
        position_id = self.db.open_paper_position(
            venue="bybit", symbol=spec.venue_symbol, side=side, qty=qty,
            avg_price=fill, leverage=leverage, liq_price=liq, entry_fee=fee,
            rationale=rationale, updated_ts=now, expected_cash=account.cash,
            expected_open_ids=[int(row["id"]) for row in open_rows],
            stop_loss=stop_loss,
            take_profit_1=take_profit_1,
            take_profit_2=take_profit_2,
        )
        if position_id is None:
            raise PaperError("账户在开仓校验期间发生变化，请刷新后重试")

        view = self._view(
            {"id": position_id, "venue": "bybit", "symbol": spec.venue_symbol, "side": side, "qty": qty,
             "avg_price": fill, "leverage": leverage, "liq_price": liq,
             "entry_fee": fee, "funding_paid": 0.0, "updated_ts": now},
            mark,
            spec,
        )
        return view

    @staticmethod
    def _validate_protective_levels(
        side: str,
        entry: float,
        stop_loss: float | None,
        take_profit_1: float | None,
        take_profit_2: float | None,
    ) -> None:
        levels = (stop_loss, take_profit_1, take_profit_2)
        if any(level is not None and level <= 0 for level in levels):
            raise PaperError("止损和止盈价格必须大于 0")
        if side == "long":
            if stop_loss is not None and stop_loss >= entry:
                raise PaperError("做多止损必须低于模拟成交价")
            if take_profit_1 is not None and take_profit_1 <= entry:
                raise PaperError("做多止盈必须高于模拟成交价")
            if take_profit_2 is not None and take_profit_2 <= entry:
                raise PaperError("做多止盈必须高于模拟成交价")
            if take_profit_1 is not None and take_profit_2 is not None and take_profit_2 <= take_profit_1:
                raise PaperError("做多止盈2必须高于止盈1")
        else:
            if stop_loss is not None and stop_loss <= entry:
                raise PaperError("做空止损必须高于模拟成交价")
            if take_profit_1 is not None and take_profit_1 >= entry:
                raise PaperError("做空止盈必须低于模拟成交价")
            if take_profit_2 is not None and take_profit_2 >= entry:
                raise PaperError("做空止盈必须低于模拟成交价")
            if take_profit_1 is not None and take_profit_2 is not None and take_profit_2 >= take_profit_1:
                raise PaperError("做空止盈2必须低于止盈1")

    def close_position(
        self,
        position_id: int,
        *,
        mark_price: float | None = None,
        funding_paid: float = 0.0,
        tick_size: float | None = None,
        exit_reason: str = "manual",
    ) -> dict:
        rows = self.db.query("SELECT * FROM positions WHERE id = ? AND closed_ts IS NULL", (position_id,))
        if not rows:
            raise PaperError(f"没有找到未平仓的持仓 #{position_id}")
        row = rows[0]
        spec = require_instrument(row["symbol"])
        mark = mark_price if mark_price is not None else self.mark_prices([spec.venue_symbol]).get(spec.venue_symbol)
        if not mark:
            raise PaperError(f"{spec.venue_symbol} 暂无标记价，无法平仓")
        direction = 1 if row["side"] == "long" else -1
        exit_price = _round_to(mark * (1 - self.config.slippage_bps / 10_000) if direction == 1 else mark * (1 + self.config.slippage_bps / 10_000), tick_size)
        qty = float(row["qty"])
        fee = qty * exit_price * self.config.taker_fee_bps / 10_000

        result = self.db.close_position(
            position_id,
            exit_price,
            funding_paid=funding_paid,
            fees=fee,
            exit_reason=exit_reason,
        )
        if result is None:
            raise PaperError(f"持仓 #{position_id} 已经平仓")
        return {
            **result,
            "exit_price": exit_price,
            "symbol": spec.venue_symbol,
            "display_symbol": spec.display_symbol,
            "exit_reason": exit_reason,
        }

    def settle_funding(self, position_id: int, rate: float, mark_price: float | None = None) -> dict:
        """Apply one funding settlement to an open position."""
        rows = self.db.query("SELECT * FROM positions WHERE id = ? AND closed_ts IS NULL", (position_id,))
        if not rows:
            raise PaperError(f"没有找到未平仓的持仓 #{position_id}")
        row = rows[0]
        spec = require_instrument(row["symbol"])
        mark = mark_price if mark_price is not None else self.mark_prices([spec.venue_symbol]).get(spec.venue_symbol)
        if not mark:
            raise PaperError(f"{spec.venue_symbol} 暂无标记价，无法结算资金费")
        direction = 1 if row["side"] == "long" else -1
        cost = direction * float(row["qty"]) * mark * rate
        if not self.db.settle_paper_funding(position_id, cost):
            raise PaperError(f"持仓 #{position_id} 已经平仓")
        return {"position_id": position_id, "rate": rate, "mark_price": mark, "cost": round(cost, 6)}

    def settle_funding_once(
        self,
        position_id: int,
        *,
        funding_ts: int,
        rate: float,
        mark_price: float,
    ) -> dict | None:
        """Apply one exchange settlement idempotently; duplicate polls do nothing."""
        rows = self.db.query("SELECT * FROM positions WHERE id = ? AND closed_ts IS NULL", (position_id,))
        if not rows:
            return None
        row = rows[0]
        direction = 1 if row["side"] == "long" else -1
        cost = direction * float(row["qty"]) * float(mark_price) * float(rate)
        booked = self.db.settle_paper_funding_once(
            position_id,
            funding_ts=funding_ts,
            rate=rate,
            mark_price=mark_price,
            cost=cost,
        )
        if not booked:
            return None
        return {
            "type": "funding",
            "position_id": position_id,
            "funding_ts": int(funding_ts),
            "rate": float(rate),
            "mark_price": float(mark_price),
            "cost": round(cost, 6),
        }

    def reconcile(
        self,
        mark_prices: dict[str, float],
        *,
        funding_by_symbol: dict[str, list[dict]] | None = None,
        tick_sizes: dict[str, float | None] | None = None,
        now_ms: int | None = None,
    ) -> list[dict]:
        """Settle new funding rows and execute simulated liquidation/SL/TP orders."""
        events: list[dict] = []
        funding_by_symbol = funding_by_symbol or {}
        tick_sizes = tick_sizes or {}
        now = int(now_ms or time.time() * 1000)

        # Funding is charged before the current-price trigger check. A unique
        # (position, venue timestamp) key makes a 15-second monitor safe to retry.
        for row in self.open_positions():
            mark = mark_prices.get(row["symbol"])
            if mark is None:
                continue
            opened_ts = int(row["updated_ts"])
            for funding in funding_by_symbol.get(row["symbol"], []):
                funding_ts = int(funding["ts"])
                if funding_ts < opened_ts or funding_ts > now:
                    continue
                event = self.settle_funding_once(
                    int(row["id"]),
                    funding_ts=funding_ts,
                    rate=float(funding["rate"]),
                    mark_price=float(mark),
                )
                if event:
                    events.append(event)

        # Fetch fresh rows because funding booking or a concurrent manual close
        # may have changed the position before condition evaluation.
        for row in self.open_positions():
            symbol = row["symbol"]
            mark = mark_prices.get(symbol)
            if mark is None:
                continue
            position_id = int(row["id"])
            side = row["side"]
            liq = float(row["liq_price"]) if row.get("liq_price") is not None else None
            if liq is not None and ((side == "long" and mark <= liq) or (side == "short" and mark >= liq)):
                event = self._execute_trigger(
                    row, float(mark), tick_sizes.get(symbol), "liquidation", None, 1.0, now
                )
                if event:
                    events.append(event)
                continue

            orders = self.db.paper_orders(position_id, status="open")
            stop = next((item for item in orders if item["order_type"] == "stop_loss"), None)
            tp1 = next((item for item in orders if item["order_type"] == "take_profit_1"), None)
            tp2 = next((item for item in orders if item["order_type"] == "take_profit_2"), None)
            chosen = None
            if stop and ((side == "long" and mark <= stop["trigger_price"]) or (side == "short" and mark >= stop["trigger_price"])):
                chosen = stop
            elif tp2 and ((side == "long" and mark >= tp2["trigger_price"]) or (side == "short" and mark <= tp2["trigger_price"])):
                chosen = tp2
            elif tp1 and ((side == "long" and mark >= tp1["trigger_price"]) or (side == "short" and mark <= tp1["trigger_price"])):
                chosen = tp1
            if chosen:
                event = self._execute_trigger(
                    row,
                    float(mark),
                    tick_sizes.get(symbol),
                    chosen["order_type"],
                    int(chosen["id"]),
                    float(chosen["close_fraction"]),
                    now,
                )
                if event:
                    events.append(event)
        return events

    def _execute_trigger(
        self,
        row: dict,
        mark: float,
        tick_size: float | None,
        reason: str,
        order_id: int | None,
        close_fraction: float,
        now_ms: int,
    ) -> dict | None:
        direction = 1 if row["side"] == "long" else -1
        raw_exit = mark * (1 - self.config.slippage_bps / 10_000) if direction == 1 else mark * (1 + self.config.slippage_bps / 10_000)
        exit_price = _round_to(raw_exit, tick_size)
        qty = float(row["qty"]) * min(max(close_fraction, 0.0), 1.0)
        fee = qty * exit_price * self.config.taker_fee_bps / 10_000
        result = self.db.reduce_paper_position(
            int(row["id"]),
            qty=qty,
            exit_price=exit_price,
            exit_fee=fee,
            exit_reason=reason,
            order_id=order_id,
            closed_ts=now_ms,
        )
        if result is None:
            return None
        return {
            "type": reason,
            "position_id": int(row["id"]),
            "symbol": row["symbol"],
            "mark_price": mark,
            "exit_price": exit_price,
            **result,
        }

    def set_note(self, position_id: int, notes: str) -> None:
        self.db.set_position_note(position_id, notes)

    def _set_cash(
        self,
        cash: float,
        *,
        fee_delta: float = 0.0,
        realized_delta: float = 0.0,
        funding_delta: float = 0.0,
    ) -> None:
        self.db.kv_set("paper_cash", repr(round(cash, 6)))
        if fee_delta:
            current = float(self.db.kv_get("paper_fees") or 0.0)
            self.db.kv_set("paper_fees", repr(round(current + fee_delta, 6)))
        if realized_delta:
            current = float(self.db.kv_get("paper_realized") or 0.0)
            self.db.kv_set("paper_realized", repr(round(current + realized_delta, 6)))
        if funding_delta:
            # Tracked as three distinct numbers so "paid" never silently means
            # "net": a long paying 1 and a short receiving 1 is not zero cost.
            current = float(self.db.kv_get("paper_funding") or 0.0)
            self.db.kv_set("paper_funding", repr(round(current + funding_delta, 6)))
            key = "paper_funding_paid" if funding_delta > 0 else "paper_funding_received"
            total = float(self.db.kv_get(key) or 0.0)
            self.db.kv_set(key, repr(round(total + abs(funding_delta), 6)))

    def reset(self) -> None:
        for key in (
            "paper_realized",
            "paper_fees",
            "paper_funding",
            "paper_funding_paid",
            "paper_funding_received",
        ):
            self.db.kv_set(key, repr(0.0))
        self.db.kv_set("paper_cash", repr(self.config.initial_cash))
