"""Local, read-only market gateway. No exchange credentials or order routes."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config.instruments import (
    BY_VENUE_SYMBOL,
    CORE_TIMEFRAMES,
    RESONANCE_MIN_BARS,
    TIMEFRAMES,
    UnknownInstrumentError,
    instrument_payload,
    require_instrument,
)
from ..config.settings import configured_proxy, load_app_config, quantdesk_home
from ..datahub.bybit import BybitClient
from ..datahub.db import Database
from ..plugins import PLUGIN_API_VERSION, PLUGIN_API_VERSIONS, PluginError, PluginManager
from ..scheduler import get_scheduler
from ..tradingagents_queue import get_tradingagents_queue
from .llm import router as llm_router
from .alerts import router as alerts_router
from .data import get_backfill_worker, router as data_router
from .factors import router as factors_router
from .runs import get_run_worker, router as runs_router
from ..retention import run_startup_prune
from .monitoring import router as monitoring_router
from .external import router as external_router
from ..alerts import AlertEngine
from ..ai_paper import run_ai_paper_cycle
from ..datahub.market_service import backfill_completed_event, get_market_service
from .panels import (
    CACHE_TTL_SECONDS,
    KLINE_BARS,
    clean,
    compute_resonance,
    get_cache,
    init_cache,
)
from .campaigns import router as campaigns_router
from .cpa import router as cpa_router
from .plugins import router as plugins_router
from .research import router as research_router
from .scheduler import router as scheduler_router
from .trading import router as trading_router, run_paper_monitor_cycle
from .tradingagents import router as tradingagents_router
from .ai_paper import router as ai_paper_router

# The venue interval codes this gateway will forward. `W` is here because the
# engine now stores and analyses a weekly series; the allowlist stays explicit
# so an arbitrary string can never reach the upstream query.
IntervalCode = Literal["15", "60", "240", "D", "W"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(proxy=configured_proxy(), timeout=12.0) as client:
        app.state.client = client
        app.state.cache = init_cache()
        app.state.paper_monitor = {"lastRunAt": None, "lastEvents": [], "error": None}
        app.state.ai_paper_monitor = {"lastRunAt": None, "lastResult": None, "error": None}
        home = quantdesk_home()
        tradingagents_queue = get_tradingagents_queue(home)
        await tradingagents_queue.start()
        backfill_worker = get_backfill_worker()
        await backfill_worker.start()
        # Studies run here, not inside the request that asked for them.
        run_worker = get_run_worker()
        await run_worker.start()
        # A run record is a working record: keep the newest ones and let the rest
        # go, so the database does not grow without bound. The policy lives in
        # config.toml and the outcome is logged rather than assumed.
        _warn_if_plugins_run_unisolated()
        try:
            pruned = await asyncio.to_thread(run_startup_prune, run_worker.queue.db, home)
            if pruned.get("deletedRuns") or pruned.get("deletedFactorRuns"):
                print(
                    f"[retention] 清理 {pruned['deletedRuns']} 条研究记录、"
                    f"{pruned['deletedFactorRuns']} 条因子记录，释放约 {pruned['bytes'] / 1e6:.1f} MB",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 - retention must never stop the engine
            print(f"[retention] 清理失败：{type(exc).__name__}: {exc}", flush=True)

        async def enqueue_daily(symbol: str, trade_date: str) -> str:
            return await tradingagents_queue.enqueue(symbol, trade_date, deduplicate=True)

        # One market state for the whole process: the browser, resonance, alerts,
        # paper trading and research all read from here instead of opening their
        # own venue connections.
        market = get_market_service(home)
        market.on_closed_candle(_on_closed_candle)
        await market.start()

        scheduler = get_scheduler(home, enqueue_daily)
        await scheduler.start()
        tasks = [
            asyncio.create_task(_paper_monitor_loop(app), name="quantdesk-paper-monitor"),
            asyncio.create_task(_ai_paper_monitor_loop(app), name="quantdesk-ai-paper-monitor"),
            asyncio.create_task(_market_reconcile_loop(app, market), name="quantdesk-market-reconcile"),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await scheduler.stop()
            await market.stop()
            await tradingagents_queue.stop()
            await backfill_worker.stop()
            await run_worker.stop()


def _warn_if_plugins_run_unisolated() -> None:
    """Say it once at startup when an enabled plugin has no OS-level isolation.

    The state is visible on the plugins page and recorded with every artifact a
    plugin produces; a line in the service log makes it impossible to miss.
    """
    try:
        from ..plugins import PluginManager
        from ..plugins.sandbox import status as sandbox_state

        state = sandbox_state()
        if state.enforced:
            return
        enabled = [
            record.manifest.id for record in PluginManager(quantdesk_home()).discover()[0]
            if record.enabled
        ]
        if enabled:
            print(
                f"[plugins] 沙箱策略 {state.policy}，但操作系统级隔离未生效"
                f"（{state.backend or '无后端'}）：{', '.join(sorted(enabled))} 将在无沙箱下运行。"
                "生产环境请设 QUANTDESK_PLUGIN_SANDBOX=required（容器内用 bwrap）。",
                flush=True,
            )
    except Exception as exc:  # noqa: BLE001 - a warning must never stop startup
        print(f"[plugins] 沙箱状态检查失败：{type(exc).__name__}: {exc}", flush=True)


async def _on_closed_candle(event: dict) -> None:
    """Evaluate only the rules that depend on the bar that just closed.

    Waking every rule on every bar would scan the whole book four times a minute
    per symbol for no benefit.
    """
    symbol = event.get("symbol")
    interval = event.get("interval")
    if not symbol or not interval:
        return
    try:
        await run_in_threadpool(_evaluate_alerts_for_closed_candle, symbol, interval)
    except Exception as exc:  # noqa: BLE001 - alerting must not kill the feed
        logger.warning("alert evaluation after %s %s failed: %s", symbol, interval, exc)


def _evaluate_alerts_for_closed_candle(symbol: str, interval: str) -> None:
    home = quantdesk_home()
    engine = AlertEngine(home)
    rules = [rule for rule in engine.list_rules() if rule["enabled"] and rule["venueSymbol"] == symbol]
    if not rules:
        return
    # Skip the evaluation entirely when no enabled rule depends on the bar that
    # closed; otherwise every symbol pays for four timeframes per bar.
    if not any(interval in _rule_intervals(rule) for rule in rules):
        return
    engine.evaluate_symbol(symbol)


def _rule_intervals(rule: dict) -> set[str]:
    """Timeframes that should wake this rule.

    Conditions such as funding, open interest and resonance carry no timeframe of
    their own, so a rule built only from those would otherwise never be woken by
    a closed bar. They are read from the database and depend on nothing that
    closes, so one anchor frame is enough to keep them evaluated.
    """
    frames = set()
    for condition in rule.get("conditions") or []:
        frame = condition.get("timeframe")
        if frame:
            frames.add(frame)
    return frames or {TIMEFRAMES[0]}


async def _market_reconcile_loop(app: FastAPI, market) -> None:
    """Backfill gaps over REST on connect/reconnect, then keep watching.

    The WebSocket is the live path; this loop only reconciles what a dropped
    connection may have missed, so it runs far less often than the old polling
    collector and never competes with the stream for the same data.
    """
    interval = max(15.0, float(load_app_config().scheduler.get("market_reconcile_interval_sec", 300)))
    # Wait on the service's reconnect signal instead of sleeping blindly, so a
    # dropped socket is reconciled within seconds and a healthy feed is not
    # swept at all.
    while True:
        try:
            await asyncio.wait_for(market.wait_for_gap(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        try:
            # A proxy node switch changes the address the streams hold, and the
            # settings are re-read here so the change is adopted without a restart.
            configured = configured_proxy()
            if configured != market.proxy:
                logger.info("proxy configuration changed; rebuilding the market streams")
                await market.set_proxy(configured)
            # Runs once at startup and after every (re)connect: the stream is the
            # live path, REST only closes the window a dropped socket left behind.
            result = await market.backfill()
            if not result.get("skipped"):
                market.publish(backfill_completed_event(result))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - transient venue errors are retried
            logger.warning("market reconcile failed: %s", exc)


async def _paper_monitor_loop(app: FastAPI) -> None:
    """Keep paper risk controls alive even when no browser is connected."""
    interval = max(5.0, float(load_app_config().scheduler.get("paper_monitor_interval_sec", 15)))
    while True:
        try:
            events = await run_in_threadpool(run_paper_monitor_cycle)
            app.state.paper_monitor = {
                "lastRunAt": int(time.time() * 1000),
                "lastEvents": events[-20:],
                "error": None,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - transient venue errors are retried
            app.state.paper_monitor = {
                "lastRunAt": int(time.time() * 1000),
                "lastEvents": [],
                "error": str(exc),
            }
        await asyncio.sleep(interval)


async def _ai_paper_monitor_loop(app: FastAPI) -> None:
    """Value AI positions continuously and evaluate once per newly closed bar."""
    interval = max(15.0, float(load_app_config().scheduler.get("ai_paper_interval_sec", 30)))
    while True:
        try:
            result = await run_in_threadpool(run_ai_paper_cycle, quantdesk_home())
            app.state.ai_paper_monitor = {
                "lastRunAt": int(time.time() * 1000),
                "lastResult": result,
                "error": None,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - failures are visible and retried next cycle/bar
            app.state.ai_paper_monitor = {
                "lastRunAt": int(time.time() * 1000),
                "lastResult": None,
                "error": str(exc),
            }
        await asyncio.sleep(interval)


logger = logging.getLogger(__name__)

app = FastAPI(title="QuantDesk local market gateway", lifespan=lifespan)


@app.exception_handler(UnknownInstrumentError)
async def unknown_instrument(_request: Request, exc: UnknownInstrumentError) -> JSONResponse:
    """Turn an invalid user symbol into a validation response, not a server error."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})

# LLM configuration and screenshot analysis. The browser can read whether a key
# is configured and can replace one, but never receives the value.
app.include_router(llm_router)
# Backtest, paper trading, and the append-only journal.
app.include_router(trading_router)
app.include_router(ai_paper_router)
# TradingAgents research: evidence bundle in, audited observation out.
app.include_router(research_router)
# Real upstream TradingAgentsGraph, isolated in a child process.
app.include_router(tradingagents_router)
# External repository adapters: discovery, install, enablement, and health.
app.include_router(plugins_router)
app.include_router(campaigns_router)
app.include_router(cpa_router)
# Background market collection and timed task status.
app.include_router(scheduler_router)
# Persistent alert rules and trigger journal.
app.include_router(alerts_router)
app.include_router(data_router)
app.include_router(runs_router)
app.include_router(factors_router)
# Data quality, provider telemetry, and background component health.
app.include_router(monitoring_router)
app.include_router(external_router)


def _client() -> BybitClient:
    """Synchronous read-only Bybit client for panel endpoints."""
    return BybitClient(proxy=configured_proxy(), timeout=12.0)


def _db() -> Database:
    return Database(quantdesk_home() / "quantdesk.db")


def _cache():
    """The shared cache, initialised by the lifespan hook."""
    return get_cache()


@app.get("/health")
def health():
    proxy = configured_proxy()
    plugin_manager = PluginManager(quantdesk_home())
    try:
        discovered_plugins, invalid_plugins = plugin_manager.discover()
        plugin_status = {
            "apiVersion": PLUGIN_API_VERSION,
            "supportedApiVersions": list(PLUGIN_API_VERSIONS),
            "installed": len(discovered_plugins),
            "enabled": sum(1 for plugin in discovered_plugins if plugin.enabled),
            "invalid": len(invalid_plugins),
        }
    except PluginError as exc:
        plugin_status = {"apiVersion": PLUGIN_API_VERSION, "error": str(exc)}
    return {
        "ok": True,
        "service": "quantdesk-market",
        "proxyConfigured": bool(proxy),
        # Never echo credentials embedded in a proxy URL back to the browser.
        "proxyScheme": proxy.split(":", 1)[0] if proxy else None,
        "timeframes": list(TIMEFRAMES),
        "instrumentCount": len(BY_VENUE_SYMBOL),
        "paperMonitor": getattr(app.state, "paper_monitor", None),
        "aiPaperMonitor": getattr(app.state, "ai_paper_monitor", None),
        "scheduler": get_scheduler(quantdesk_home()).status(),
        "tradingAgentsQueue": get_tradingagents_queue(quantdesk_home()).status(),
        "backfillQueue": get_backfill_worker().status(),
        "backtestRuns": get_run_worker().status(),
        "plugins": plugin_status,
    }


@app.get("/api/instruments")
def instruments_index():
    """Fixed universe, pool split, timeframes and resonance requirements."""
    payload = instrument_payload()
    payload["cacheTtlSeconds"] = CACHE_TTL_SECONDS
    payload["klineBars"] = KLINE_BARS
    return payload


async def market_request(endpoint: str, symbol: str, params: dict):
    try:
        instrument = require_instrument(symbol)
    except ValueError as exc:
        raise HTTPException(422, "该合约不在固定合约池内") from exc
    params.update(category="linear", symbol=instrument.venue_symbol)
    try:
        response = await app.state.client.get("https://api.bybit.com/v5/market/" + endpoint, params=params)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            504,
            "Bybit 请求超时：网络或代理不可达。请检查 QUANTDESK_PROXY 或 config.toml 的代理设置，"
            "以及代理软件本身是否在运行。",
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Bybit 连接失败：{exc}。请检查网络及代理设置。") from exc

    # A regional block and a broken proxy need different fixes, so they must not
    # share one message. CloudFront answers a blocked region with 403; a dead
    # proxy never gets this far.
    if response.status_code == 403:
        raise HTTPException(403, _geo_block_message(response))
    try:
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Bybit 返回异常（HTTP {response.status_code}）：{exc}") from exc
    if not isinstance(body, dict) or body.get("retCode") != 0:
        raise HTTPException(502, f"Bybit 返回行情错误：{str(body)[:200]}")
    return body


def _geo_block_message(response: httpx.Response) -> str:
    """Explain a CloudFront regional block, naming the edge node we were routed to."""
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("message") or "")
    except ValueError:
        detail = (response.text or "")[:160]
    pop = response.headers.get("x-amz-cf-pop", "未知节点")
    return (
        f"Bybit 按地区拒绝访问（CloudFront 403，边缘节点 {pop}）：{detail or '未提供原因'}。"
        "这不是代理没配好，而是当前代理的出口地区被 Bybit 屏蔽。"
        "请在代理软件里切换到新加坡 / 日本 / 香港 / 台湾等节点后重试——美国节点会被拒。"
        "切换节点后不需要重启网关，下一次请求即生效。"
    )


@app.get("/bybit/v5/market/kline")
async def kline(symbol: str, category: Literal["linear"] = "linear", interval: IntervalCode = "60", limit: int = Query(220, ge=1, le=1000)):
    return await market_request("kline", symbol, {"interval": interval, "limit": limit})


@app.get("/bybit/v5/market/tickers")
async def tickers(symbol: str, category: Literal["linear"] = "linear"):
    return await market_request("tickers", symbol, {})


@app.get("/api/instruments-info")
async def instruments_info(symbol: str | None = None):
    """Live venue metadata for the fixed pool only.

    Restricted to the fixed universe on purpose: this drives contract status,
    tick size and lot size checks, and must not become a general catalog proxy.
    """
    if symbol:
        try:
            targets = [require_instrument(symbol)]
        except ValueError as exc:
            raise HTTPException(422, "该合约不在固定合约池内") from exc
    else:
        targets = [BY_VENUE_SYMBOL[venue] for venue in BY_VENUE_SYMBOL]

    def load() -> list[dict]:
        client = _client()
        try:
            found = client.configured_instruments()
        finally:
            client.close()
        rows = []
        for item in targets:
            live = found.get(item.venue_symbol)
            lot = (live or {}).get("lotSizeFilter", {})
            price = (live or {}).get("priceFilter", {})
            rows.append(
                {
                    "displaySymbol": item.display_symbol,
                    "venueSymbol": item.venue_symbol,
                    "productType": item.product_type,
                    "riskClass": item.risk_class,
                    "trading": live is not None,
                    "status": (live or {}).get("status") or "Unavailable",
                    "symbolType": (live or {}).get("symbolType") or ("Crypto" if item.is_crypto else None),
                    "tickSize": price.get("tickSize"),
                    "minOrderQty": lot.get("minOrderQty"),
                    "qtyStep": lot.get("qtyStep"),
                    "maxLeverage": (live or {}).get("leverageFilter", {}).get("maxLeverage"),
                }
            )
        return rows

    rows = await run_in_threadpool(load)
    inactive = [row["venueSymbol"] for row in rows if not row["trading"]]
    return {"instruments": rows, "checkedAt": int(time.time() * 1000), "inactive": inactive}


@app.get("/bybit/v5/market/funding/history")
async def funding_history(symbol: str, limit: int = Query(60, ge=1, le=200)):
    """Funding rate history with the venue's current per-symbol cadence."""
    instrument = require_instrument(symbol)

    def load() -> tuple[list[dict], int | None]:
        client = _client()
        try:
            rows = client.funding_history(instrument.venue_symbol, limit=limit)
            metadata = client.instruments("linear", symbol=instrument.venue_symbol)
            interval = int(metadata[0].get("fundingInterval", 0) or 0) if metadata else None
            return rows, interval or None
        finally:
            client.close()

    rows, interval_minutes = await run_in_threadpool(load)
    return {
        "source": "bybit",
        "venueSymbol": instrument.venue_symbol,
        "cadenceMinutes": interval_minutes,
        "cadenceHours": interval_minutes / 60 if interval_minutes else None,
        "list": rows,
        # A TradFi stock perp can legitimately quote exactly 0 at every
        # settlement; the UI must not render that as a missing value.
        "allZero": bool(rows) and all(row["rate"] == 0 for row in rows),
    }


@app.get("/bybit/v5/market/open-interest")
async def open_interest(symbol: str, intervalTime: str = "1h", limit: int = Query(60, ge=1, le=200)):
    instrument = require_instrument(symbol)

    def load() -> list[dict]:
        client = _client()
        try:
            return client.open_interest(instrument.venue_symbol, interval_time=intervalTime, limit=limit)
        finally:
            client.close()

    try:
        rows = await run_in_threadpool(load)
    except ValueError as exc:
        raise HTTPException(422, "不支持的持仓量周期") from exc
    return {
        "source": "bybit",
        "venueSymbol": instrument.venue_symbol,
        "intervalTime": intervalTime,
        "unit": "base_coin",
        "list": rows,
    }


@app.get("/api/resonance")
async def resonance_panel(
    symbol: str,
    intervals: str = Query(",".join(CORE_TIMEFRAMES),
                         description="逗号分隔，缺省为核心周期 15m,1h,4h,1d；周线可显式索取"),
    bars: int = Query(KLINE_BARS, ge=RESONANCE_MIN_BARS, le=1000),
):
    """Multi-timeframe resonance computed by the engine, not by the browser.

    Each requested timeframe is analysed independently and then combined with
    the documented weights (1d .40 / 4h .30 / 1h .20 / 15m .10). A timeframe
    without enough closed bars is reported as unavailable instead of being
    silently scored as neutral.
    """
    try:
        instrument = require_instrument(symbol)
    except ValueError as exc:
        raise HTTPException(422, "该合约不在固定合约池内") from exc
    wanted = [item.strip() for item in intervals.split(",") if item.strip()]
    unknown = [item for item in wanted if item not in TIMEFRAMES]
    if unknown:
        raise HTTPException(422, f"不支持的周期：{', '.join(unknown)}")

    result = await compute_resonance(_cache(), instrument.venue_symbol, wanted, bars)
    return clean(
        {
            "venueSymbol": instrument.venue_symbol,
            "displaySymbol": instrument.display_symbol,
            "weights": result.get("weights"),
            "barsRequested": bars,
            "minBars": RESONANCE_MIN_BARS,
            "computedAt": int(time.time() * 1000),
            **result,
        }
    )


def resonance_frames(result: dict) -> dict[str, dict]:
    """Flatten a resonance result into citable per-timeframe evidence rows."""
    frames: dict[str, dict] = {}
    for frame in result.get("timeframes", []):
        notes = frame.get("notes") or {}
        frames[frame["interval"]] = {
            "close": frame.get("close"),
            "stance": frame.get("stance"),
            "score": frame.get("score"),
            "trend": frame.get("trend"),
            "momentum": frame.get("momentum"),
            "volume": frame.get("volume"),
            "adx": notes.get("adx"),
            "rsi": notes.get("rsi"),
            "session_thin": notes.get("session_thin"),
            "activity_ratio": notes.get("activity_ratio"),
            "bars": (result.get("barsByInterval") or {}).get(frame["interval"]),
        }
    return frames


@app.get("/api/market/snapshot")
async def market_snapshot(symbol: str, interval: str | None = Query(None)):
    """Last real quote and closed candle, with provenance and freshness.

    Served from the in-process market state, so the page paints without waiting
    for a venue round trip. `stale` reports whether the feed is still live; the
    response never contains generated data.

    Declared async on purpose: reading the state also announces a stale/fresh
    transition to the browser queues, and those belong to the event loop. A sync
    route would run in a worker thread and drop that wakeup on the floor.
    """
    try:
        instrument = require_instrument(symbol)
    except ValueError as exc:
        raise HTTPException(422, "该合约不在固定合约池内") from exc
    if interval is not None and interval not in TIMEFRAMES:
        raise HTTPException(422, f"不支持的周期：{interval}；仅支持 {', '.join(TIMEFRAMES)}")
    market = get_market_service()
    try:
        return market.snapshot(instrument.venue_symbol, interval)
    except KeyError as exc:
        raise HTTPException(503, "行情服务尚未就绪，请稍后重试") from exc


@app.get("/api/market/state")
async def market_state():
    """Whole-feed status: connection, reconnects, last message age, backfills."""
    return get_market_service().health()


# Registered before the catch-all websocket below, or every stream request would
# be answered with a bare close.
@app.websocket("/api/market/stream")
async def market_stream(websocket: WebSocket) -> None:
    """Local push stream of normalized market events.

    Only fixed-universe symbols and the four canonical intervals are subscribed
    upstream, so nothing a browser sends can widen the venue subscription. The
    subscription is released when the socket closes.
    """
    await websocket.accept()
    market = get_market_service()
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
    unsubscribe = market.subscribe(queue)

    async def watch_for_close() -> None:
        """Resolve as soon as the browser goes away.

        Without an explicit read, a client that closes leaves this handler
        parked on the queue until the next heartbeat, and its subscription
        would outlive the tab.
        """
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
        except Exception:  # noqa: BLE001 - any receive failure means the peer is gone
            return

    closer = asyncio.create_task(watch_for_close(), name="quantdesk-market-stream-close")
    try:
        await websocket.send_text(
            json.dumps({"kind": "connection", "state": market.connection_state()["state"]}, ensure_ascii=False)
        )
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, closer}, timeout=20.0, return_when=asyncio.FIRST_COMPLETED)
            if closer in done:
                getter.cancel()
                with suppress(asyncio.CancelledError):
                    await getter
                break
            if getter not in done:
                getter.cancel()
                with suppress(asyncio.CancelledError):
                    await getter
                # Keepalive, so an idle tab is not mistaken for a dead peer.
                await websocket.send_text(json.dumps({"kind": "heartbeat", "ts": int(time.time() * 1000)}))
                continue
            await websocket.send_text(getter.result())
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - a broken pipe must not kill the app
        logger.debug("market stream closed: %s", exc)
    finally:
        closer.cancel()
        with suppress(asyncio.CancelledError):
            await closer
        unsubscribe()


# A local long-running installation serves the production frontend from the
# same process and origin. Docker keeps using its dedicated Nginx container.
@app.websocket("/{path:path}")
async def reject_unknown_websocket(websocket: WebSocket, path: str) -> None:
    """Close stale development/HMR sockets without routing them to StaticFiles."""
    del path
    await websocket.close(code=1000)


_web_dist_value = os.environ.get("QUANTDESK_WEB_DIST", "").strip()
if _web_dist_value:
    _web_dist = Path(_web_dist_value).expanduser().resolve()
    if (_web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(_web_dist), html=True), name="quantdesk-web")
