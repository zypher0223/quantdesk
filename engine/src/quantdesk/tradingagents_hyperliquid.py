"""Read-only Hyperliquid daily-candle adapter for TradingAgents crypto tools."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

API_URL = "https://api.hyperliquid.xyz/info"
MAX_DAILY_BARS = 500


class HyperliquidMarketDataError(RuntimeError):
    pass


class HyperliquidDailyBridge:
    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}

    @staticmethod
    def coin_for_symbol(symbol: str) -> str:
        canonical = symbol.strip().upper()
        for suffix in ("-USD", "-USDT", "-USDC"):
            if canonical.endswith(suffix) and canonical[: -len(suffix)]:
                return canonical[: -len(suffix)]
        raise HyperliquidMarketDataError(f"不支持的加密资产代码：{symbol}")

    @staticmethod
    def _end_of_day_ms(curr_date: str) -> int:
        requested = datetime.strptime(curr_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = requested + timedelta(days=1) - timedelta(milliseconds=1)
        return int(min(end, datetime.now(timezone.utc)).timestamp() * 1000)

    @staticmethod
    def _request(payload: dict[str, Any], attempts: int = 4) -> Any:
        encoded = json.dumps(payload).encode("utf-8")
        for attempt in range(attempts):
            request = urllib.request.Request(
                API_URL, data=encoded,
                headers={"Content-Type": "application/json", "User-Agent": "QuantDesk-TradingAgents/1.0"},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise HyperliquidMarketDataError(f"Hyperliquid HTTP {exc.code}") from exc
                if attempt == attempts - 1:
                    raise HyperliquidMarketDataError(f"Hyperliquid HTTP {exc.code}，重试仍失败") from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempt == attempts - 1:
                    raise HyperliquidMarketDataError("Hyperliquid 网络请求失败") from exc
            time.sleep(2**attempt)
        raise HyperliquidMarketDataError("Hyperliquid 请求失败")

    def load_ohlcv(self, symbol: str, curr_date: str) -> pd.DataFrame:
        coin = self.coin_for_symbol(symbol)
        key = (coin, curr_date)
        if key in self._cache:
            return self._cache[key].copy()
        end_ms = self._end_of_day_ms(curr_date)
        rows = self._request({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1d", "startTime": end_ms - MAX_DAILY_BARS * 86_400_000, "endTime": end_ms},
        })
        if not isinstance(rows, list) or not rows:
            raise HyperliquidMarketDataError(f"Hyperliquid 没有返回 {coin} 日线")
        records = []
        for row in rows:
            try:
                records.append({
                    "Date": pd.to_datetime(int(row["t"]), unit="ms", utc=True).tz_localize(None),
                    "Open": float(row["o"]), "High": float(row["h"]), "Low": float(row["l"]),
                    "Close": float(row["c"]), "Volume": float(row["v"]),
                })
            except (KeyError, TypeError, ValueError) as exc:
                raise HyperliquidMarketDataError(f"{coin} K线格式异常") from exc
        frame = pd.DataFrame.from_records(records).drop_duplicates(subset=["Date"]).sort_values("Date")
        cutoff = pd.Timestamp(curr_date)
        frame = frame[frame["Date"] <= cutoff].reset_index(drop=True)
        if frame.empty or (cutoff - frame["Date"].max().normalize()).days > 2:
            raise HyperliquidMarketDataError(f"{coin} 日线缺失或已过期")
        self._cache[key] = frame
        return frame.copy()

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        frame = self.load_ohlcv(symbol, end_date)
        selected = frame[(frame["Date"] >= pd.Timestamp(start_date)) & (frame["Date"] <= pd.Timestamp(end_date))].copy()
        if selected.empty:
            raise HyperliquidMarketDataError(f"{symbol} 在请求区间内没有日线")
        selected["Date"] = selected["Date"].dt.strftime("%Y-%m-%d")
        header = (
            f"# Hyperliquid perpetual daily OHLCV for {self.coin_for_symbol(symbol)} from {start_date} to {end_date}\n"
            f"# Total records: {len(selected)}\n# Source: Hyperliquid public candleSnapshot; UTC; read-only\n\n"
        )
        return header + selected.to_csv(index=False)


def install_hyperliquid_bridge(config: dict[str, Any]) -> HyperliquidDailyBridge:
    from tradingagents.dataflows import interface, market_data_validator, stockstats_utils, y_finance

    bridge = HyperliquidDailyBridge()
    stockstats_utils.load_ohlcv = bridge.load_ohlcv
    y_finance.load_ohlcv = bridge.load_ohlcv
    market_data_validator.load_ohlcv = bridge.load_ohlcv
    interface.VENDOR_METHODS["get_stock_data"]["hyperliquid"] = bridge.get_stock_data
    interface.VENDOR_METHODS["get_indicators"]["hyperliquid"] = y_finance.get_stock_stats_indicators_window
    vendors = dict(config.get("data_vendors", {}))
    vendors["core_stock_apis"] = "hyperliquid"
    vendors["technical_indicators"] = "hyperliquid"
    config["data_vendors"] = vendors
    return bridge
