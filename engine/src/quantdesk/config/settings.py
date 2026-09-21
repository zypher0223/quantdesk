"""Application paths and default config seeding.

Layout under QUANTDESK_HOME (env override; default ~/.quantdesk):
  config.toml   universe / intervals / paper defaults
  llm.toml      LLM provider profiles + role mapping
  keys.env      API keys loaded into environment (chmod 600, git-ignored)
  quantdesk.db  SQLite (candles cache, journal, reports, ...)
  uploads/      uploaded chart images
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def toml_dumps(raw: dict) -> str:
    """Serialise a config document, loading the writer only when one is written.

    The engine sources are imported by the TradingAgents runtime, which installs
    the graph and its own dependencies but none of the engine's. A writer import
    at module scope would make reading a config fail in that environment - the
    tokenised-stock bridge imports this module just to read the proxy setting -
    so the dependency is paid for only on the rare path that actually saves.
    """
    try:
        import tomli_w
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError("写入配置需要 tomli-w，请安装 engine 依赖后再保存") from exc

    return tomli_w.dumps(raw)
import tomllib

from dotenv import load_dotenv

# `1w` is fetchable and backtestable, but it is not part of the default matrix:
# weekly bars move once a week, so the scheduled refresh would spend a task per
# contract per run to confirm nothing changed. Fetch it explicitly when wanted.
DEFAULT_INTERVALS = ["15m", "1h", "4h", "1d"]

# Symbols added after the first release. An existing config.toml that predates
# them would otherwise keep a stale universe and never see the new contracts.
UNIVERSE_MIGRATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("SPCXUSDT", "SKHYUSDT"), "2026-09 新增 SpaceX 与 SK海力士 ADR 合约"),
)

DEFAULT_CONFIG_TOML = """\
# QuantDesk 主配置
[app]
intervals = ["15m", "1h", "4h", "1d"]
default_cash_usd = 100000

# 固定合约池：15 个 Bybit TradFi 股票永续 + BTC/ETH USDT 永续。
# 这里只保存 venue 原生代码；展示代码与风险分类由内置 registry 管理。
[universe]
symbols = ["AAPLUSDT", "MSFTUSDT", "GOOGLUSDT", "AMZNUSDT", "NVDAUSDT", "METAUSDT", "TSLAUSDT", "SNDKUSDT", "MUUSDT", "AMDSTOCKUSDT", "NBISUSDT", "SPCXUSDT", "SKHYUSDT", "SOXLUSDT", "SOXSUSDT", "BTCUSDT", "ETHUSDT"]

# Bybit API 网络受限地区需走代理（本机实测 127.0.0.1:12003 可用）
[proxy]
# url = "http://127.0.0.1:12003"   # 留空或注释 = 直连；也可用环境变量 QUANTDESK_PROXY

[paper]
taker_fee_bps = 10          # 手工模拟/回测默认吃单手续费（万分比）
ai_paper_taker_fee_bps = 5  # AI 自动模拟：Gate VIP 0 USDT 永续 Taker（0.0500%/每次成交）
slippage_bps = 5            # 默认滑点（万分位）；可在 symbols 中按合约覆盖
stock_perp_slippage_bps = 15

[paper.symbols]             # 按标的覆盖滑点/手续费示例
# "AAPLUSDT" = { slippage_bps = 20 }

[scheduler]
bar_close_offset_sec = 5    # bar 收盘后延迟拉取，等交易所落定
daily_ta_time = "16:05"     # 日线收盘后的 TradingAgents 定时研判（UTC 时间）
paper_monitor_interval_sec = 15 # 模拟盘资金费、强平与保护条件轮询
# 实时行情由进程内 MarketDataService（WebSocket）负责。
# 轮转采集器保留为对账/兜底：默认关闭，避免与实时流重复取数。
market_collection_enabled = false
market_symbol_interval_sec = 20 # 启用时：每次采集一个标的，轮转固定合约池
market_reconcile_interval_sec = 300 # REST 补洞间隔（断线重连后会立即补一次）
market_backfill_bars = 400
daily_ta_enabled = false     # 付费模型任务默认不自动开启
daily_ta_symbols = ["BTCUSDT", "ETHUSDT"]

[research]
timeout_seconds = 1800
# 付费研判的成本闸门。留空表示不限额；一旦设置，费用不可知的模型会被直接拒绝，
# 因为无法执行的限额看起来像保护，实际不是。
agent_run_budget_usd = 1.5      # 单次多智能体研判上限（美元）
agent_daily_budget_usd = 8.0    # 当日全部研判合计上限（美元）
reuse_window_hours = 24         # 同标的、同日、同配置、同数据版本在此窗口内复用结果
max_retries = 1                 # 可重试失败（限流/超时）的重试次数
allow_unpriced_agents = false   # 单价未知的模型是否允许运行
max_analyst_retries = 1         # 某分析师未产出报告时，允许整轮重跑的次数
max_missing_analysts = 1        # 缺失多少名分析师后，本次结论不再作为评级发布

# ── 外部研究（OpenBB）与分析（Fincept）───────────────────────────────
# 两者都是可选增强：关掉或不可用时，行情、回测、告警、模拟盘与研究照常运行。
[external]
openbb_enabled = false          # OpenBB 研究插件是否启用
fincept_enabled = false         # Fincept 组合分析插件是否启用
default_timeout_seconds = 30    # 单次外部调用的默认超时
max_retries = 2                 # 429/5xx 的可重试次数上限
backoff_base_seconds = 1.0      # 指数退避基数；第 n 次重试等待 base * 2^(n-1)
max_backoff_seconds = 30.0      # 单次退避上限
requests_per_minute = 60        # 每个 Provider 的限速（独立计数）

# 缓存时间按数据类型配置，不写死在代码里（分钟）。
[external.cache_ttl_minutes]
company_profile = 10080         # 公司资料：长缓存（7 天）
fundamentals = 1440             # 财务报表：1 天，下一次财报事件前复用
financial_growth = 1440
earnings = 720
filings = 4320                  # SEC 文件：内容哈希不变时复用
company_events = 720
news = 30                       # 新闻：短缓存
short_interest = 1440           # 做空数据：按发布周期
institutional_ownership = 1440
macro_calendar = 180            # 经济日历：定时刷新
macro = 720
portfolio_risk = 15             # Fincept：持仓或价格快照变化后失效
scenario = 15

[retention]
runs = 500                       # 每类任务保留最近多少条已完成记录
factorRuns = 100                 # 因子任务更占空间（一次就是一张矩阵），单独设上限
days = 30                        # 超过多少天的已完成记录会被清理

[backtest]
# 单次回测/验证允许请求的K线上限。超过就拒绝并说明原因，绝不静默截断样本；
# 需要更长样本时把它调大，或走后台队列。硬顶见 studies.ABSOLUTE_MAX_CANDLES。
max_bars = 20000

[plugins]
sandbox = "preferred"            # required = 沙箱不可用时拒绝运行插件；off = 关闭

[external.retention_days]
evidence = 400                  # 外部证据保留天数
analytics = 400                 # 分析结果保留天数

# OpenBB Provider 必须显式指定。不支持的接口会尝试备用 Provider 并记录失败原因，
# 绝不静默换用未知 Provider，也不用空值填充。
[openbb.providers]
profile = "yfinance"
fundamentals = "sec"
news = "yfinance"
macro = "fred"
short_interest = "finra"
filings = "sec"
earnings = "yfinance"
institutional_ownership = "yfinance"
company_events = "yfinance"
macro_calendar = "fred"
financial_growth = "sec"

# 每个接口可选的备用 Provider，按顺序尝试。
[openbb.fallbacks]
fundamentals = ["yfinance"]
news = ["benzinga", "polygon"]
macro = ["yfinance"]

# 需要时点模式（point-in-time）的数据类型；未列出的类型一律要求可判定的发布时间。
[openbb.point_in_time]
default = true
exempt = ["macro_calendar"]     # 未来日历事件本来就没有发布时间
"""

DEFAULT_LLM_TOML = """\
# LLM 配置：profiles 定义端点与模型，roles 把功能映射到 profile。
# 密钥只从环境变量（或 keys.env）读取，永不写入本文件之外的库表。
#
# supports_vision = 可以接收图片（K线截图识别只会路由到这类 profile）
# proxy            = 该供应商是否需要走本地代理；留空表示直连。
#                    本机实测 api.openai.com 直连不通，必须配代理。

[profiles.deepseek]
provider = "deepseek"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
# 这两个名字来自 GET /v1/models 的实际返回。DeepSeek 对不存在的模型名不会报错，
# 而是静默改用默认模型，所以写错名字不会被发现——请用设置页的「拉取模型」核对。
deep_model = "deepseek-v4-pro"
quick_model = "deepseek-flash"
supports_vision = false
supports_json_mode = true
# 推理模型会先把预算花在思维链上：实测 deepseek-v4-pro 完成一次研判需要
# 约 4700 token 推理 + 900 token 正文，所以上限要留足。
max_tokens = 12000

[profiles.openai]
provider = "openai"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
deep_model = "gpt-4o"
quick_model = "gpt-4o-mini"
# proxy = "http://127.0.0.1:12003"
supports_vision = true
supports_json_mode = true

[profiles.glm-vision]
provider = "openai_compatible"
base_url = "https://open.bigmodel.cn/api/paas/v4"
api_key_env = "GLM_API_KEY"
vision_model = "glm-4v-plus"
supports_vision = true

[roles]
tradingagents = "deepseek"
chart_analysis = "glm-vision"
journal_summary = "deepseek"
signal_explain = "deepseek"
ai_paper_trader = "deepseek"
"""

# Upstream base URLs, used to fill in profiles written by older versions.
PROVIDER_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    "openai_compatible": "",
}
# Credentials are read from these names when a profile declares none.
PROVIDER_KEY_ENVS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai_compatible": "GLM_API_KEY",
}
PROVIDER_DEEP_MODELS = {
    "deepseek": "deepseek-v4-pro",
    "openai": "gpt-4o",
}


def quantdesk_home() -> Path:
    """Config/data directory. Falls back to the default path when it cannot be created.

    A read-only or sandboxed HOME must not take the whole app down: the gateway
    still has to answer with defaults so the UI can explain what is wrong.
    """
    home = Path(os.environ.get("QUANTDESK_HOME", Path.home() / ".quantdesk"))
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / "uploads").mkdir(exist_ok=True)
    except OSError:
        pass
    return home


def configured_proxy(home: Path | None = None) -> str | None:
    """Resolve the market-data proxy from env first, then config.toml."""
    if os.environ.get("QUANTDESK_PROXY"):
        return os.environ["QUANTDESK_PROXY"]
    config = (home or quantdesk_home()) / "config.toml"
    if config.exists():
        try:
            with config.open("rb") as handle:
                return tomllib.load(handle).get("proxy", {}).get("url") or None
        except (OSError, tomllib.TOMLDecodeError):
            return None
    return None


def home_writable(home: Path | None = None) -> bool:
    home = home or quantdesk_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _read_toml(path: Path, seed: str) -> dict:
    """Read a TOML file, seeding it from `seed` when absent.

    If the file is missing and cannot be written, the seed is parsed in memory
    instead of raising: the caller gets working defaults and the write path
    reports the permission problem where a user can act on it.
    """
    if not path.exists():
        try:
            path.write_text(seed, encoding="utf-8")
        except OSError:
            return tomllib.loads(seed)
    with path.open("rb") as f:
        return tomllib.load(f)


def load_runtime_secrets(home: Path | None = None) -> None:
    """Load saved credentials for headless jobs without overwriting process env."""
    home = home or quantdesk_home()
    keys = home / "keys.env"
    if keys.exists():
        load_dotenv(keys, override=False)


@dataclass
class UniverseConfig:
    symbols: list[str] = field(default_factory=list)
    migration_notes: list[str] = field(default_factory=list)


@dataclass
class AppConfig:
    intervals: list[str] = field(default_factory=lambda: list(DEFAULT_INTERVALS))
    default_cash_usd: float = 100000.0
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    paper: dict = field(default_factory=dict)
    scheduler: dict = field(default_factory=dict)
    research: dict = field(default_factory=dict)
    # External providers are optional research and analytics inputs; the engine
    # starts and trades without either of them, so these stay plain dicts.
    external: dict = field(default_factory=dict)
    openbb: dict = field(default_factory=dict)
    # How much run history to keep. A run record is a working record, not an
    # archive: `runs` newest per family and nothing older than `days`.
    retention: dict = field(default_factory=lambda: {"runs": 500, "factorRuns": 100, "days": 30})
    # Plugin sandbox policy: required | preferred | off. `required` refuses to run
    # a plugin when the host cannot isolate it, which is what a production install
    # should say; the environment variable still overrides this file.
    plugins: dict = field(default_factory=lambda: {"sandbox": "preferred"})
    # Backtest limits: how many bars one study may request, read at validation
    # time so editing config.toml takes effect on the next request.
    backtest: dict = field(default_factory=lambda: {"max_bars": 20000})


@lru_cache(maxsize=1)
def default_config() -> dict:
    """The shipped config, parsed once: the source of defaults for new sections."""
    import tomllib

    return tomllib.loads(DEFAULT_CONFIG_TOML)


def _fill_missing(current: dict | None, defaults: dict) -> dict:
    """Fill absent keys from the defaults, never overwriting what is there.

    An installation that upgrades has a config.toml written before a section
    existed. Reading it as "no limits configured" would silently disable every
    default - which is how a rate limit or a cache window quietly disappears.
    """
    merged = dict(current or {})
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _fill_missing(merged[key], value)
    return merged


def _is_known_symbol(symbol: str) -> bool:
    from .instruments import require_instrument

    try:
        require_instrument(symbol)
        return True
    except ValueError:
        return False


def _migrate_universe(raw: dict) -> tuple[list[str], list[str]]:
    """Return (symbols, migration_notes) for the fixed universe.

    Existing installations may still carry an older symbol list. Missing
    `symbols` migrates to the fixed built-in universe; a present-but-outdated
    list gets the newer contracts appended. Unrecognized entries are reported,
    never silently dropped.
    """
    from .instruments import VENUE_SYMBOLS

    notes: list[str] = []
    configured = raw.get("universe", {}).get("symbols")
    if not configured:
        return list(VENUE_SYMBOLS), ["缺少 universe.symbols，已迁移为 17 个固定合约"]

    symbols = [str(item).upper() for item in configured]
    for added, reason in UNIVERSE_MIGRATIONS:
        missing = [symbol for symbol in added if symbol not in symbols]
        if missing:
            symbols.extend(missing)
            notes.append(f"补充 {', '.join(missing)}：{reason}")

    unknown = [symbol for symbol in symbols if not _is_known_symbol(symbol)]
    if unknown:
        notes.append(f"配置中有 {len(unknown)} 个不在固定池内的代码：{', '.join(unknown)}")
    return symbols, notes


def load_app_config(home: Path | None = None) -> AppConfig:
    home = home or quantdesk_home()
    raw = _read_toml(home / "config.toml", DEFAULT_CONFIG_TOML)
    cfg = AppConfig(
        intervals=raw.get("app", {}).get("intervals", DEFAULT_INTERVALS),
        default_cash_usd=float(raw.get("app", {}).get("default_cash_usd", 100000)),
        paper=raw.get("paper", {}),
        scheduler=raw.get("scheduler", {}),
        research=raw.get("research", {}),
        external=_fill_missing(raw.get("external"), default_config().get("external", {})),
        openbb=_fill_missing(raw.get("openbb"), default_config().get("openbb", {})),
        retention={**{"runs": 500, "factorRuns": 100, "days": 30}, **(raw.get("retention") or {})},
        plugins={**{"sandbox": "preferred"}, **(raw.get("plugins") or {})},
        backtest={**{"max_bars": 20000}, **(raw.get("backtest") or {})},
    )
    symbols, notes = _migrate_universe(raw)
    cfg.universe = UniverseConfig(symbols=symbols, migration_notes=notes)
    return cfg


# Provider names an operator may select per interface. A typo must be a validation
# error, not a silent "provider not found" at run time.
KNOWN_OPENBB_PROVIDERS = (
    "yfinance", "sec", "fred", "finra", "benzinga", "polygon", "fmp", "intrinio", "tiingo", "nasdaq",
)


def write_external_settings(values: dict, home: Path | None = None) -> Path:
    """Merge the external-research settings into config.toml, preserving the rest.

    Only the keys a caller actually sends are touched, so the settings page can
    save one panel without rewriting the file.
    """
    home = home or quantdesk_home()
    path = home / "config.toml"
    raw = _read_toml(path, DEFAULT_CONFIG_TOML)

    external = values.get("external") or {}
    if external:
        raw["external"] = {**raw.get("external", {}), **external}
    for table in ("cache_ttl_minutes", "retention_days"):
        incoming = (values.get("cache") or {}).get(table) if table == "cache_ttl_minutes" else None
        incoming = values.get(table) if incoming is None else incoming
        if incoming:
            section = dict(raw.get("external", {}))
            section = {**section, table: {**(section.get(table) or {}), **incoming}}
            raw["external"] = section
    openbb = values.get("openbb") or {}
    if openbb:
        section = dict(raw.get("openbb", {}))
        for key in ("providers", "fallbacks", "point_in_time"):
            if openbb.get(key) is not None:
                section[key] = {**(section.get(key) or {}), **(openbb[key] or {})}
        raw["openbb"] = section
    temporary = path.with_suffix(".toml.tmp")
    temporary.write_text(toml_dumps(raw), encoding="utf-8")
    temporary.replace(path)
    return path


def write_scheduler_settings(values: dict, home: Path | None = None) -> Path:
    """Update only the scheduler table while preserving the rest of config.toml."""
    home = home or quantdesk_home()
    path = home / "config.toml"
    raw = _read_toml(path, DEFAULT_CONFIG_TOML)
    raw["scheduler"] = {**raw.get("scheduler", {}), **values}
    temporary = path.with_suffix(".toml.tmp")
    temporary.write_text(toml_dumps(raw), encoding="utf-8")
    temporary.replace(path)
    return path


@dataclass
class LLMProfile:
    name: str
    provider: str
    api_key_env: str = ""
    base_url: str = ""
    deep_model: str = ""
    quick_model: str = ""
    vision_model: str = ""
    proxy: str = ""
    supports_vision: bool = False
    supports_json_mode: bool = False
    # Reasoning models spend most of the budget thinking before they answer, so
    # the ceiling has to cover reasoning + output, not just the answer.
    max_tokens: int = 0

    def model_for(self, role: str) -> str:
        """Pick the model a role should use, falling back sensibly."""
        if role == "chart_analysis":
            return self.vision_model or self.deep_model or self.quick_model
        return self.deep_model or self.quick_model or self.vision_model


@dataclass
class LLMSettings:
    profiles: dict[str, LLMProfile] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)

    def profile_for(self, role: str) -> LLMProfile:
        name = self.roles.get(role)
        if name and name in self.profiles:
            return self.profiles[name]
        # 角色未配置时回退到第一个 profile，保证开箱即用
        if self.profiles:
            return next(iter(self.profiles.values()))
        raise KeyError("llm.toml 中没有任何 profile")

    def vision_profile(self, role: str = "chart_analysis") -> LLMProfile:
        """A profile that can actually accept an image.

        If the role points at a text-only profile this raises instead of quietly
        substituting another provider: a silently rerouted screenshot would put
        chart data in front of a model the operator did not choose.
        """
        preferred = self.roles.get(role)
        if preferred and preferred in self.profiles:
            profile = self.profiles[preferred]
            if profile.supports_vision:
                return profile
            raise KeyError(f"角色 {role} 指向的 profile「{preferred}」未声明 supports_vision，拒绝发送图片")
        for profile in self.profiles.values():
            if profile.supports_vision:
                return profile
        raise KeyError("没有任何 profile 声明 supports_vision，无法进行图表识别")


def _profile_from_raw(name: str, raw: dict) -> LLMProfile:
    provider = raw.get("provider", "openai_compatible")
    return LLMProfile(
        name=name,
        provider=provider,
        api_key_env=raw.get("api_key_env", "") or PROVIDER_KEY_ENVS.get(provider, ""),
        # Older configs omitted base_url; without it the OpenAI SDK would target
        # api.openai.com for a DeepSeek key.
        base_url=raw.get("base_url", "") or PROVIDER_BASE_URLS.get(provider, ""),
        deep_model=raw.get("deep_model", "") or raw.get("vision_model", "") or PROVIDER_DEEP_MODELS.get(provider, ""),
        quick_model=raw.get("quick_model", ""),
        vision_model=raw.get("vision_model", ""),
        proxy=raw.get("proxy", ""),
        supports_vision=bool(raw.get("supports_vision", False)),
        supports_json_mode=bool(raw.get("supports_json_mode", False)),
        max_tokens=int(raw.get("max_tokens", 0) or 0),
    )


def load_llm_settings(home: Path | None = None) -> LLMSettings:
    home = home or quantdesk_home()
    load_runtime_secrets(home)
    raw = _read_toml(home / "llm.toml", DEFAULT_LLM_TOML)
    profiles = {name: _profile_from_raw(name, body) for name, body in raw.get("profiles", {}).items()}
    return LLMSettings(profiles=profiles, roles=raw.get("roles", {}))


def write_llm_settings(settings: LLMSettings, home: Path | None = None) -> None:
    """Persist llm.toml (settings page save action)."""
    home = home or quantdesk_home()
    raw = {
        "profiles": {
            name: {k: v for k, v in {
                "provider": p.provider,
                "api_key_env": p.api_key_env,
                "base_url": p.base_url,
                "deep_model": p.deep_model,
                "quick_model": p.quick_model,
                "vision_model": p.vision_model,
                "proxy": p.proxy,
                "supports_vision": p.supports_vision,
                "supports_json_mode": p.supports_json_mode,
                "max_tokens": p.max_tokens or "",
            }.items() if v}
            for name, p in settings.profiles.items()
        },
        "roles": settings.roles,
    }
    path = home / "llm.toml"
    path.write_text(toml_dumps(raw), encoding="utf-8")


def read_key_env(home: Path | None = None) -> dict[str, str]:
    """Parse keys.env into a mapping (values are never returned to the browser)."""
    home = home or quantdesk_home()
    keys = home / "keys.env"
    out: dict[str, str] = {}
    if not keys.exists():
        return out
    for line in keys.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        out[name.strip()] = value.strip().strip('"').strip("'")
    return out


def set_key_env(updates: dict[str, str], home: Path | None = None) -> Path:
    """Merge KEY=value pairs into keys.env, keeping the file mode at 600.

    Raises OSError when the directory is not writable so the caller can report
    the real cause instead of a generic failure.
    """
    home = home or quantdesk_home()
    keys = ensure_keys_env(home)
    existing = read_key_env(home)
    for name, value in updates.items():
        name = name.strip()
        if not name:
            continue
        if value:
            existing[name] = value
        else:
            existing.pop(name, None)  # an empty value clears the key
    # Collapse case-insensitive duplicates so a re-save cannot leave two lines for
    # the same variable with different values.
    merged: dict[str, str] = {}
    for name, value in existing.items():
        merged[name.upper()] = value
    lines = ["# QuantDesk API keys (KEY=value per line)"]
    lines.extend(f"{name}={value}" for name, value in sorted(merged.items()))
    keys.write_text("\n".join(lines) + "\n", encoding="utf-8")
    keys.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return keys


def ensure_keys_env(home: Path | None = None) -> Path:
    """Create an empty keys.env with safe permissions if missing.

    A read-only directory is not fatal here: the file simply will not exist, and
    the credential lookup falls through to the process environment.
    """
    home = home or quantdesk_home()
    keys = home / "keys.env"
    if not keys.exists():
        try:
            keys.write_text("# QuantDesk API keys (KEY=value per line)\n", encoding="utf-8")
            keys.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return keys
