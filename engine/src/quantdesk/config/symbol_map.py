"""The single symbol map shared by every external provider.

OpenBB speaks in public securities (`AAPL`, `NVDA`, `BTC`), Fincept takes risk in
QuantDesk contract codes (`AAPLUSDT`, `AMDSTOCKUSDT`), and Bybit remains the only
source of tradable prices. Those three views of the same instrument live here and
nowhere else: a plugin that hardcodes its own translation table is a plugin that
will disagree with the engine the first time the pool changes.

Nothing in this module fetches anything. It maps, and it refuses to guess.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .instruments import BY_VENUE_SYMBOL, INSTRUMENTS, InstrumentSpec

# Reference codes that may legitimately resolve to nothing at the provider. A
# failed lookup for these is reported as unavailable - never retried against a
# guessed substitute, and never filled with a lookalike ticker.
REFERENCE_MAY_BE_UNAVAILABLE = frozenset({"SPCX", "SKHY"})

# Asset classes as the external providers see them, derived from what the
# contract is rather than from its name.
ASSET_CLASS_BY_RISK_CLASS = {
    "leveraged_etf": "leveraged_etf",
    "adr": "adr",
    "special": "equity",
    "sector": "equity",
    "standard": "equity",
}


@dataclass(frozen=True)
class SymbolMapping:
    """One instrument's three names, plus what a provider must know about it."""

    venue_symbol: str
    display_symbol: str
    openbb_symbol: str
    fincept_symbol: str
    group: str
    asset_class: str
    product_type: str
    leveraged: bool
    reference_may_be_unavailable: bool
    risk_class: str

    def as_dict(self) -> dict:
        """The wire shape: camelCase, like every other plugin message."""
        return {
            "venueSymbol": self.venue_symbol,
            "displaySymbol": self.display_symbol,
            "openbbSymbol": self.openbb_symbol,
            "finceptSymbol": self.fincept_symbol,
            "group": self.group,
            "assetClass": self.asset_class,
            "productType": self.product_type,
            "leveraged": self.leveraged,
            "referenceMayBeUnavailable": self.reference_may_be_unavailable,
            "riskClass": self.risk_class,
        }


def _openbb_symbol(spec: InstrumentSpec) -> str:
    """The public reference code for the underlying, or "" when there is none.

    Tokenised-stock contracts carry their underlying; crypto perps reference the
    coin itself. A contract without either has no reference asset, and an empty
    value here is the honest answer - the caller reports it as unavailable rather
    than inventing a ticker.
    """
    if spec.product_type == "crypto":
        return spec.display_symbol
    return spec.underlying_symbol or ""


def _mapping(spec: InstrumentSpec) -> SymbolMapping:
    reference = _openbb_symbol(spec)
    return SymbolMapping(
        venue_symbol=spec.venue_symbol,
        display_symbol=spec.display_symbol,
        openbb_symbol=reference,
        # Fincept risk is always computed on QuantDesk's own contract code and
        # QuantDesk's own returns, so this column never changes.
        fincept_symbol=spec.venue_symbol,
        group=spec.group,
        asset_class=(
            "crypto"
            if spec.product_type == "crypto"
            else ASSET_CLASS_BY_RISK_CLASS.get(spec.risk_class, "equity")
        ),
        product_type=spec.product_type,
        leveraged=spec.risk_class == "leveraged_etf",
        reference_may_be_unavailable=reference in REFERENCE_MAY_BE_UNAVAILABLE,
        risk_class=spec.risk_class,
    )


MAPPINGS: dict[str, SymbolMapping] = {item.venue_symbol: _mapping(item) for item in INSTRUMENTS}
BY_REFERENCE: dict[str, SymbolMapping] = {
    item.openbb_symbol: item for item in MAPPINGS.values() if item.openbb_symbol
}

# A reference code is only a reference if it means exactly one contract.
_DUPLICATE_REFERENCES = [
    code for code in BY_REFERENCE if sum(1 for m in MAPPINGS.values() if m.openbb_symbol == code) > 1
]
if _DUPLICATE_REFERENCES:  # pragma: no cover - guards a future pool edit
    raise RuntimeError(f"参考资产代码重复，映射无法唯一：{sorted(set(_DUPLICATE_REFERENCES))}")


def mapping_for(venue_symbol: str) -> SymbolMapping:
    """The mapping for one contract, or a clear refusal naming the pool."""
    try:
        return MAPPINGS[venue_symbol]
    except KeyError as exc:
        raise KeyError(f"标的 {venue_symbol!r} 不在固定合约池内，无法映射到外部数据源") from exc


def mapping_for_reference(reference: str) -> SymbolMapping | None:
    """Reverse lookup by public reference code, if this pool contains it."""
    return BY_REFERENCE.get(reference)


def reference_symbols() -> list[str]:
    return [item.openbb_symbol for item in MAPPINGS.values() if item.openbb_symbol]


def fincept_symbols() -> list[str]:
    return [item.fincept_symbol for item in MAPPINGS.values()]


def symbol_map_payload() -> list[dict]:
    """The whole map as plain data, for handing to an out-of-process adapter."""
    return [item.as_dict() for item in MAPPINGS.values()]


def mapping_payload(venue_symbol: str) -> dict:
    """The one mapping a research request needs, without shipping the pool.

    A named contract resolves to a named reference asset, and the plugin is told
    exactly which one: the translation is decided here, not re-derived there.
    """
    item = mapping_for(venue_symbol)
    return {
        "mapping": item.as_dict(),
        # Which providers may legitimately have nothing for this instrument.
        "referenceOptional": item.reference_may_be_unavailable,
    }


# -- stress scenarios ----------------------------------------------------
#
# A scenario is a set of percentage shocks by symbol, group or asset class.
# Defining them here keeps the membership in one place: the UI, the adapter and
# the tests all describe a "semiconductor shock" the same way.

# The semiconductor chain, as a stress scenario means it. Separate from the
# display group on purpose - see the scenario below.
SEMICONDUCTOR_SYMBOLS: tuple[str, ...] = (
    "NVDAUSDT", "SNDKUSDT", "MUUSDT", "AMDSTOCKUSDT", "SKHYUSDT", "SOXLUSDT", "SOXSUSDT",
)
LEVERAGED_SYMBOLS: tuple[str, ...] = tuple(
    item.venue_symbol for item in MAPPINGS.values() if item.leveraged
)

SCENARIOS: tuple[dict, ...] = (
    {
        "id": "all_equities_down",
        "label": "全部股票合约下跌",
        "description": "股票类合约统一下跌 10%，加密货币不变",
        "shocks": {"assetClass:equity": -10.0, "assetClass:leveraged_etf": -10.0, "assetClass:adr": -10.0},
    },
    {
        "id": "semiconductor_shock",
        "label": "半导体板块冲击",
        "description": "半导体产业链下跌 15%，其余股票下跌 3%",
        # Named explicitly rather than taken from the display group: the pool files
        # NVDA under 科技龙头, and a semiconductor shock that skips NVDA would not
        # be a semiconductor shock.
        "shocks": {
            **{f"symbol:{symbol}": -15.0 for symbol in SEMICONDUCTOR_SYMBOLS},
            "assetClass:equity": -3.0,
        },
    },
    {
        "id": "crypto_selloff",
        "label": "BTC/ETH 同步下跌",
        "description": "加密资产下跌 20%，股票不变",
        "shocks": {"assetClass:crypto": -20.0},
    },
    {
        "id": "volatility_spike",
        "label": "波动率上升",
        "description": "全市场波动率放大 2 倍，价格按 1 倍标准差不利方向移动",
        "shocks": {"volatilityMultiplier": 2.0},
    },
    {
        "id": "funding_anomaly",
        "label": "资金费率异常",
        "description": "资金费率按 3 倍结算，衡量持仓成本冲击",
        "shocks": {"fundingMultiplier": 3.0},
    },
    {
        "id": "leveraged_etf_halved",
        "label": "杠杆 ETF 腰斩",
        "description": "三倍杠杆 ETF 下跌 33%，其余股票不变",
        "shocks": {f"symbol:{symbol}": -33.0 for symbol in LEVERAGED_SYMBOLS},
    },
)

SCENARIO_IDS = tuple(item["id"] for item in SCENARIOS)


def scenario_for(scenario_id: str) -> dict:
    for item in SCENARIOS:
        if item["id"] == scenario_id:
            return item
    raise KeyError(f"未知情景 {scenario_id!r}；可用：{', '.join(SCENARIO_IDS)}")


def shock_for(mapping: SymbolMapping, shocks: dict[str, float]) -> float | None:
    """The percentage shock a scenario applies to one contract, if any.

    Explicit symbol shocks win over group shocks, which win over asset-class
    shocks - so a custom "NVDA -5%" is not overwritten by the group rule.
    """
    for key in (f"symbol:{mapping.venue_symbol}", f"group:{mapping.group}", f"assetClass:{mapping.asset_class}"):
        if key in shocks:
            return float(shocks[key])
    return None


def resolve_shocks(shocks: dict[str, float]) -> dict[str, float]:
    """Turn a scenario's rules into one shock per contract in the pool."""
    resolved: dict[str, float] = {}
    for venue_symbol, mapping in MAPPINGS.items():
        value = shock_for(mapping, shocks)
        if value is not None:
            resolved[venue_symbol] = value
    return resolved
