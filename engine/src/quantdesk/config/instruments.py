"""Canonical QuantDesk instrument registry.

The UI universe is deliberately fixed. Exchange metadata is still refreshed at
runtime so a listed symbol can be disabled when Bybit changes its status or
contract specification.
"""

from __future__ import annotations

from dataclasses import dataclass

# Canonical analysis timeframes. Order is the display order and the
# resonance weight order.
TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h", "1d", "1w")

# What a *signal* needs. Weekly bars are analysis enrichment - a factor study or a
# gate scan may use them - but a live stance must not become ineligible because a
# weekly bar is missing, and resonance must not silently change shape when a new
# analysis timeframe is added. Anything that gates trading reads this, not TIMEFRAMES.
CORE_TIMEFRAMES: tuple[str, ...] = ("15m", "1h", "4h", "1d")

# Bybit kline interval codes for TIMEFRAMES.
BYBIT_INTERVALS = {"15m": "15", "1h": "60", "4h": "240", "1d": "D", "1w": "W"}

# Candle length in ms, used to decide whether a bar has closed yet.
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
                "1w": 604_800_000}

# EMA200 + ADX(14) need this much history before a stance means anything.
# Fewer completed bars than this and the resonance panel must say so.
RESONANCE_MIN_BARS = 220

# `product_type` is about what the contract *is* on the venue:
#   stock - single-name equity perpetual (Bybit symbolType=stock)
#   etf   - ETF perpetual (Bybit symbolType=ETF); SOXL/SOXS are 3x leveraged
#   crypto- crypto perpetual, no symbolType on the venue side
# The UI must not label a leveraged ETF as a stock.
CRYPTO_VENUE_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")


@dataclass(frozen=True)
class InstrumentSpec:
    display_symbol: str
    venue_symbol: str
    name: str
    group: str
    product_type: str
    risk_class: str = "standard"
    underlying_symbol: str | None = None
    chart_interval: str = "1h"

    @property
    def is_crypto(self) -> bool:
        return self.product_type == "crypto"


INSTRUMENTS: tuple[InstrumentSpec, ...] = (
    InstrumentSpec("AAPL", "AAPLUSDT", "苹果", "科技龙头", "stock", underlying_symbol="AAPL"),
    InstrumentSpec("MSFT", "MSFTUSDT", "微软", "科技龙头", "stock", underlying_symbol="MSFT"),
    InstrumentSpec("GOOGL", "GOOGLUSDT", "Alphabet", "科技龙头", "stock", underlying_symbol="GOOGL"),
    InstrumentSpec("AMZN", "AMZNUSDT", "亚马逊", "科技龙头", "stock", underlying_symbol="AMZN"),
    InstrumentSpec("NVDA", "NVDAUSDT", "英伟达", "科技龙头", "stock", "sector", "NVDA"),
    InstrumentSpec("META", "METAUSDT", "Meta", "科技龙头", "stock", underlying_symbol="META"),
    InstrumentSpec("TSLA", "TSLAUSDT", "特斯拉", "科技龙头", "stock", underlying_symbol="TSLA"),
    InstrumentSpec("SNDK", "SNDKUSDT", "闪迪", "半导体", "stock", "sector", "SNDK"),
    InstrumentSpec("MU", "MUUSDT", "美光", "半导体", "stock", "sector", "MU"),
    InstrumentSpec("AMD", "AMDSTOCKUSDT", "AMD", "半导体", "stock", "sector", "AMD"),
    InstrumentSpec("NBIS", "NBISUSDT", "Nebius", "AI成长", "stock", "special", "NBIS"),
    InstrumentSpec("SPCX", "SPCXUSDT", "SpaceX", "特殊标的", "stock", "special", "SPCX"),
    InstrumentSpec("SKHY", "SKHYUSDT", "SK海力士 ADR", "半导体", "stock", "adr", "SKHY"),
    # The venue reports SOXL/SOXS with symbolType=ETF — keep that visible.
    InstrumentSpec("SOXL", "SOXLUSDT", "半导体三倍做多", "杠杆ETF", "etf", "leveraged_etf", "SOXL"),
    InstrumentSpec("SOXS", "SOXSUSDT", "半导体三倍做空", "杠杆ETF", "etf", "leveraged_etf", "SOXS"),
    InstrumentSpec("BTC", "BTCUSDT", "比特币", "加密资产", "crypto"),
    InstrumentSpec("ETH", "ETHUSDT", "以太坊", "加密资产", "crypto"),
)

BY_VENUE_SYMBOL = {item.venue_symbol: item for item in INSTRUMENTS}
BY_DISPLAY_SYMBOL = {item.display_symbol: item for item in INSTRUMENTS}
VENUE_SYMBOLS = tuple(BY_VENUE_SYMBOL)

# The confirmed UI split: a 15-name stock-class pool plus a 2-name crypto pool.
STOCK_CLASS_SYMBOLS: tuple[str, ...] = tuple(
    item.venue_symbol for item in INSTRUMENTS if not item.is_crypto
)
CRYPTO_SYMBOLS: tuple[str, ...] = tuple(
    item.venue_symbol for item in INSTRUMENTS if item.is_crypto
)

# Venue symbolType values that make up the stock-class pool. The catalog
# reports stocks as `stock` and the leveraged ETFs as `ETF`; querying only
# `stock` silently drops SOXL/SOXS.
STOCK_CLASS_VENUE_SYMBOL_TYPES: tuple[str, ...] = ("stock", "ETF")

PRODUCT_LABELS = {"stock": "STOCK", "etf": "ETF", "crypto": "CRYPTO"}


class UnknownInstrumentError(ValueError):
    """A caller supplied a symbol outside QuantDesk's fixed universe."""


def require_instrument(symbol: str) -> InstrumentSpec:
    normalized = symbol.upper()
    item = BY_VENUE_SYMBOL.get(normalized) or BY_DISPLAY_SYMBOL.get(normalized)
    if item is None:
        raise UnknownInstrumentError(f"{symbol!r} 不在 QuantDesk 固定合约池中")
    return item


def instrument_payload() -> dict:
    """Everything the UI needs to render the fixed universe and its pools."""
    pools = (
        {"key": "stock", "label": "股票池", "count": len(STOCK_CLASS_SYMBOLS), "symbols": list(STOCK_CLASS_SYMBOLS)},
        {"key": "crypto", "label": "加密池", "count": len(CRYPTO_SYMBOLS), "symbols": list(CRYPTO_SYMBOLS)},
    )
    return {
        "timeframes": list(TIMEFRAMES),
        "resonanceMinBars": RESONANCE_MIN_BARS,
        "pools": pools,
        "instruments": [
            {
                "displaySymbol": item.display_symbol,
                "venueSymbol": item.venue_symbol,
                "name": item.name,
                "group": item.group,
                "productType": item.product_type,
                "productLabel": PRODUCT_LABELS.get(item.product_type, item.product_type.upper()),
                "riskClass": item.risk_class,
                "chartInterval": item.chart_interval,
                "underlyingSymbol": item.underlying_symbol,
                "pool": "crypto" if item.is_crypto else "stock",
                # AMD is the only display symbol that differs from its venue code today;
                # keep the flag so the UI can explain the mapping without hardcoding it.
                "symbolMapped": item.display_symbol != item.venue_symbol.removesuffix("USDT"),
            }
            for item in INSTRUMENTS
        ],
    }
