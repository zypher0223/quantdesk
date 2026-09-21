"""Bybit contract-market adapter for TradingAgents stock-class analysis.

The graph ticker remains the exact traded USDT perpetual. Price, volume and
technical indicators therefore come from Bybit. Company news and financial
tools map only their first ticker argument to the public underlying symbol.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable

import pandas as pd

from .config.settings import configured_proxy
from .datahub.bybit import BybitClient

MAX_DAILY_BARS = 500


class BybitContractMarketDataError(RuntimeError):
    pass


class BybitDailyBridge:
    def __init__(self, venue_symbol: str, fundamental_symbol: str | None, company_name: str | None) -> None:
        self.venue_symbol = venue_symbol.upper()
        self.fundamental_symbol = fundamental_symbol.upper() if fundamental_symbol else None
        self.company_name = company_name or self.venue_symbol
        self._cache: dict[str, pd.DataFrame] = {}

    @staticmethod
    def _end_of_day(curr_date: str) -> datetime:
        requested = datetime.strptime(curr_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return min(requested + timedelta(days=1), datetime.now(timezone.utc))

    def load_ohlcv(self, symbol: str, curr_date: str) -> pd.DataFrame:
        accepted = {self.venue_symbol, self.fundamental_symbol}
        if symbol.upper() not in accepted:
            raise BybitContractMarketDataError(
                f"拒绝把 {symbol} 的行情混入 {self.venue_symbol} 合约分析"
            )
        if curr_date in self._cache:
            return self._cache[curr_date].copy()

        end = self._end_of_day(curr_date)
        start = end - timedelta(days=MAX_DAILY_BARS + 15)
        client = BybitClient(proxy=configured_proxy(), timeout=30)
        try:
            rows = client.kline(
                "linear", self.venue_symbol, "1d",
                int(start.timestamp() * 1000), int(end.timestamp() * 1000),
                max_bars=MAX_DAILY_BARS,
            )
        except Exception as exc:
            raise BybitContractMarketDataError(
                f"Bybit 未能返回 {self.venue_symbol} 日线：{exc}"
            ) from exc
        finally:
            client.close()
        if not rows:
            raise BybitContractMarketDataError(f"Bybit 没有返回 {self.venue_symbol} 日线")

        records = [
            {
                "Date": pd.to_datetime(row["ts"], unit="ms", utc=True).tz_localize(None),
                "Open": row["open"], "High": row["high"], "Low": row["low"],
                "Close": row["close"], "Volume": row["volume"],
            }
            for row in rows
            if row["ts"] + 86_400_000 <= int(end.timestamp() * 1000)
        ]
        frame = pd.DataFrame.from_records(records)
        if frame.empty:
            raise BybitContractMarketDataError(f"{self.venue_symbol} 没有已收盘日线")
        frame = frame.drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)
        self._cache[curr_date] = frame
        return frame.copy()

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        frame = self.load_ohlcv(symbol, end_date)
        selected = frame[
            (frame["Date"] >= pd.Timestamp(start_date)) & (frame["Date"] <= pd.Timestamp(end_date))
        ].copy()
        if selected.empty:
            raise BybitContractMarketDataError(f"{self.venue_symbol} 在请求区间内没有日线")
        selected["Date"] = selected["Date"].dt.strftime("%Y-%m-%d")
        header = (
            f"# Bybit tokenized perpetual daily OHLCV for {self.venue_symbol}\n"
            f"# Company/reference asset: {self.company_name} ({self.fundamental_symbol or 'N/A'})\n"
            f"# Range: {start_date} to {end_date}; records: {len(selected)}\n"
            "# Source: Bybit V5 public linear kline; UTC; read-only; this is contract market data, not spot shares.\n\n"
        )
        return header + selected.to_csv(index=False)

    def resolve_identity(self, ticker: str) -> dict[str, str]:
        identity: dict[str, str] = {
            "company_name": self.company_name,
            "exchange": "Bybit tokenized USDT perpetual",
            "quote_type": "DERIVATIVE",
        }
        if self.fundamental_symbol:
            identity["reference_symbol"] = self.fundamental_symbol
        return identity


def _map_first_ticker(function: Callable, public_symbol: str) -> Callable:
    @wraps(function)
    def mapped(*args, **kwargs):
        if args:
            args = (public_symbol, *args[1:])
        elif "ticker" in kwargs:
            kwargs = {**kwargs, "ticker": public_symbol}
        elif "symbol" in kwargs:
            kwargs = {**kwargs, "symbol": public_symbol}
        return function(*args, **kwargs)
    return mapped


def _with_evidence(implementation, block: str):
    """Append the external evidence block to a vendor's returned text.

    The evidence reaches the analyst through the same call that already supplies
    fundamentals or news, so the model reads it as part of its brief rather than
    as an afterthought nobody consults.
    """

    def wrapped(*args, **kwargs):
        text = implementation(*args, **kwargs)
        if not isinstance(text, str) or not block.strip():
            return text
        return f"{text}\n\n{block}"

    return wrapped


def install_bybit_bridge(
    config: dict[str, Any], *, venue_symbol: str,
    fundamental_symbol: str | None, company_name: str | None,
    external_evidence: str = "",
) -> BybitDailyBridge:
    from tradingagents.dataflows import interface, market_data_validator, stockstats_utils, y_finance
    from tradingagents.graph import trading_graph

    bridge = BybitDailyBridge(venue_symbol, fundamental_symbol, company_name)
    stockstats_utils.load_ohlcv = bridge.load_ohlcv
    y_finance.load_ohlcv = bridge.load_ohlcv
    market_data_validator.load_ohlcv = bridge.load_ohlcv
    trading_graph.resolve_instrument_identity = bridge.resolve_identity

    interface.VENDOR_METHODS["get_stock_data"]["bybit"] = bridge.get_stock_data
    interface.VENDOR_METHODS["get_indicators"]["bybit"] = y_finance.get_stock_stats_indicators_window

    if fundamental_symbol:
        for method in (
            "get_fundamentals", "get_balance_sheet", "get_cashflow",
            "get_income_statement", "get_news", "get_insider_transactions",
        ):
            implementation = interface.VENDOR_METHODS.get(method, {}).get("yfinance")
            if implementation is not None:
                mapped = _map_first_ticker(implementation, fundamental_symbol)
                # Fundamentals and news are where external evidence belongs; the
                # price-bearing calls are left untouched, because a Bybit bar must
                # never be replaced by anything OpenBB returns.
                if external_evidence and method in ("get_fundamentals", "get_news"):
                    mapped = _with_evidence(mapped, external_evidence)
                interface.VENDOR_METHODS[method]["yfinance"] = mapped

    vendors = dict(config.get("data_vendors", {}))
    vendors["core_stock_apis"] = "bybit"
    vendors["technical_indicators"] = "bybit"
    config["data_vendors"] = vendors
    return bridge
