"""Model research endpoint: evidence in, auditable observation out.

The configured role is named ``tradingagents`` for compatibility, but this
endpoint is a single model pass rather than a TradingAgentsGraph execution.
"""

from __future__ import annotations

import json
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ..backtest import BacktestConfig, run_backtest
from ..config.instruments import CORE_TIMEFRAMES, TIMEFRAMES, require_instrument
from ..config.settings import configured_proxy, load_app_config, load_llm_settings, quantdesk_home
from ..datahub.bybit import BybitClient
from ..datahub.db import Database
from ..llm import ChatMessage, LLMError, credential_issue, resolve_api_key
from ..research.external_bridge import (
    appendix,
    collect_best_effort,
    prompt_block,
    summary_meta,
)
from ..research import (
    RESEARCH_SYSTEM_PROMPT,
    build_bundle,
    compute_price_structure,
    parse_report,
    report_markdown,
    validate_plan,
    validate_report,
)
from .llm import _profile_driver, _profile_payload
from .panels import compute_resonance, get_cache, resonance_frames

router = APIRouter(prefix="/api/research", tags=["research"])

BACKTEST_BARS = 400
MAX_CANDLES = 1_000


class CandleInput(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def _evidence_date(request: "ResearchRequest") -> str:
    """The date external evidence must not postdate.

    For an uploaded history that is the last uploaded bar, not today: asking a
    provider for today's fundamentals while judging last month's chart would hand
    the model information the moment did not have.
    """
    candles = request.candles or []
    if request.source == "upload" and candles:
        newest = max(int(item.time) for item in candles)
        return time.strftime("%Y-%m-%d", time.gmtime(newest / 1000))
    return time.strftime("%Y-%m-%d")


class ResearchRequest(BaseModel):
    symbol: str
    timeframe: str = Field("1h", description="图表周期，同时决定价格结构的取样")
    bars: int = Field(400, ge=220, le=1000)
    includeBacktest: bool = True
    # Reasoning models burn most of the budget before answering; 3000 was not
    # enough for deepseek-v4-pro, which needs ~4700 reasoning + ~900 output.
    maxTokens: int | None = Field(None, ge=512, le=200_000)
    focus: str | None = Field(None, max_length=300, description="用户额外关注点")
    news: str = Field("", max_length=4000, description="用户粘贴的新闻/公告，作为外部输入标注")
    source: Literal["live", "upload", "demo"] = "live"
    # The chart the user is actually looking at, so the analysis reasons over
    # the same candles on screen instead of a separate fetch.
    candles: list[CandleInput] | None = None


def _timeframe_candles(spec, interval: str, bars: int) -> list[dict]:
    client = BybitClient(proxy=configured_proxy(), timeout=20.0)
    try:
        return client.kline_snapshot(spec.venue_symbol, interval, limit=bars, completed_only=True)
    finally:
        client.close()


def _ticker(spec) -> dict:
    client = BybitClient(proxy=configured_proxy(), timeout=15.0)
    try:
        return client.ticker("linear", spec.venue_symbol)
    finally:
        client.close()


def _funding_stats(symbol: str) -> tuple[int, bool]:
    """Local funding history: how many points, and whether they are all zero."""
    try:
        db = Database(quantdesk_home() / "quantdesk.db")
    except Exception:  # noqa: BLE001 - a missing database is not fatal for research
        return 0, False
    rows = db.load_funding("bybit", symbol, limit=500)
    return len(rows), bool(rows) and all(float(row["rate"]) == 0 for row in rows)


def _rule_signals(resonance: dict, ticker: dict) -> dict:
    """Deterministic signals, kept explicitly separate from the model's reading."""
    frames = {frame["interval"]: frame for frame in resonance.get("timeframes", [])}
    stances = {interval: frame.get("stance") for interval, frame in frames.items()}
    score = resonance.get("score_100")
    agree = len({value for value in stances.values() if value}) == 1
    return {
        "resonance_score_100": score,
        "resonance_label": resonance.get("label"),
        "stance_15m": stances.get("15m"),
        "stance_1h": stances.get("1h"),
        "stance_4h": stances.get("4h"),
        "stance_1d": stances.get("1d"),
        "timeframes_aligned": agree,
        "price_24h_pct": ticker.get("price24hPcnt"),
    }


def _backtest_summary(spec, interval: str, bars: int) -> dict | None:
    client = BybitClient(proxy=configured_proxy(), timeout=20.0)
    try:
        rows = client.kline_snapshot(spec.venue_symbol, interval, limit=bars, completed_only=True)
        live = client.configured_instruments().get(spec.venue_symbol) or {}
    finally:
        client.close()
    return _backtest_summary_from_rows(spec, interval, rows, live)


def _backtest_summary_from_rows(spec, interval: str, rows: list[dict], live: dict | None = None) -> dict | None:
    """Run the research backtest over the exact evidence window supplied."""
    if len(rows) < 60:
        return None
    live = live or {}
    paper = (load_app_config().paper or {})
    last_close = rows[-1]["close"]
    config = BacktestConfig(
        fast_period=9,
        slow_period=21,
        fee_bps=float(paper.get("taker_fee_bps", 10)),
        slippage_bps=float(
            paper.get("slippage_bps", 5) if spec.is_crypto else paper.get("stock_perp_slippage_bps", paper.get("slippage_bps", 5))
        ),
        tick_size=float(live.get("priceFilter", {}).get("tickSize") or 0) or None,
        qty_step=float(live.get("lotSizeFilter", {}).get("qtyStep") or 0) or None,
        min_order_notional=max(float(live.get("lotSizeFilter", {}).get("minOrderQty") or 0) * last_close, 1.0),
    )
    try:
        db = Database(quantdesk_home() / "quantdesk.db")
        funding = db.load_funding("bybit", spec.venue_symbol, start_ts=rows[0]["ts"], end_ts=rows[-1]["ts"])
    except Exception:  # noqa: BLE001
        funding = []
    result = run_backtest(rows, config, funding=funding, instrument={"productType": spec.product_type}, interval=interval)
    return {
        "timeframe": interval,
        "bars": len(rows),
        "net_return_pct": result.net_return_pct,
        "max_drawdown_pct": result.max_drawdown_pct,
        "win_rate_pct": result.win_rate_pct,
        "profit_factor": result.profit_factor,
        "trades": len(result.trades),
        "liquidations": sum(1 for trade in result.trades if trade.liquidated),
        "total_fees": result.total_fees,
        "total_funding": result.total_funding,
        "warnings": result.warnings,
    }


@router.get("/readiness")
def readiness():
    """Whether a research run can start, and if not, exactly what is missing."""
    home = quantdesk_home()
    settings = load_llm_settings(home)
    role = "tradingagents"
    profile = settings.profiles.get(settings.roles.get(role, ""))
    if profile is None:
        return {"ready": False, "role": role, "reason": f"角色 {role} 未指向任何 profile，请在设置页配置"}
    key, candidates = resolve_api_key(profile.provider, profile.api_key_env)
    if not key or credential_issue(key):
        issue = credential_issue(key)
        return {
            "ready": False,
            "role": role,
            "profile": profile.name,
            "reason": (
                f"profile「{profile.name}」的 {candidates[0]} {issue}"
                if issue
                else f"profile「{profile.name}」缺少 API Key（{candidates[0]}）"
            ),
            "action": "在设置页写入真实密钥，或写入 ~/.quantdesk/keys.env",
        }
    return {"ready": True, "role": role, "profile": _profile_payload(profile, home)}


@router.post("")
async def run_research(request: ResearchRequest):
    """Assemble the evidence bundle, ask the configured model, audit the answer."""
    try:
        spec = require_instrument(request.symbol)
    except ValueError as exc:
        raise HTTPException(422, "该合约不在固定合约池内") from exc
    if request.timeframe not in TIMEFRAMES:
        raise HTTPException(422, f"不支持的周期：{request.timeframe}")
    if request.source == "demo":
        raise HTTPException(422, "演示数据不能用于深度研判，请连接 Bybit 或上传真实历史K线")
    if request.source == "upload" and not request.candles:
        raise HTTPException(422, "历史研判必须随请求提供上传的K线")

    home = quantdesk_home()
    settings = load_llm_settings(home)
    role = "tradingagents"
    profile = settings.profiles.get(settings.roles.get(role, "")) or (
        next(iter(settings.profiles.values())) if settings.profiles else None
    )
    if profile is None:
        raise HTTPException(409, "llm.toml 中没有任何 profile")

    # Price structure comes from the candles on screen when the browser sent
    # them, otherwise from a fresh fetch of the requested timeframe.
    structure_source = "engine_fetch"
    if request.candles:
        candle_rows = [
            {"ts": row.time, "open": row.open, "high": row.high, "low": row.low, "close": row.close, "volume": row.volume}
            for row in request.candles[:MAX_CANDLES]
        ]
        structure_source = request.source
    else:
        try:
            candle_rows = await run_in_threadpool(_timeframe_candles, spec, request.timeframe, request.bars)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"读取价格结构失败：{exc}") from exc
    price_structure = await run_in_threadpool(compute_price_structure, candle_rows)
    price_structure["interval"] = request.timeframe
    price_structure["source"] = structure_source

    # Uploaded history is an isolated replay. Current ticker and multi-timeframe
    # readings would belong to another date, so they must not enter its bundle.
    if request.source == "upload":
        resonance = {
            "score": None, "score_100": None, "label": "历史单周期",
            "timeframes": [],
            "unavailable": [
                {"interval": interval, "bars": 0, "required": request.bars}
                for interval in TIMEFRAMES if interval != request.timeframe
            ],
        }
        ticker = {}
        funding_points, funding_all_zero = 0, False
        backtest = None
        if request.includeBacktest:
            backtest = await run_in_threadpool(
                _backtest_summary_from_rows, spec, request.timeframe, candle_rows
            )
    else:
        try:
            # Core timeframes: the research verdict reads the same multi-timeframe resonance the
            # panel does, so an added analysis timeframe cannot change its meaning.
            resonance = await compute_resonance(get_cache(), spec.venue_symbol,
                                                list(CORE_TIMEFRAMES), request.bars)
            ticker = await run_in_threadpool(_ticker, spec)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - quote a venue failure plainly
            raise HTTPException(502, f"采集证据包失败：{exc}") from exc
        funding_points, funding_all_zero = await run_in_threadpool(_funding_stats, spec.venue_symbol)
        backtest = None
        if request.includeBacktest:
            try:
                backtest = await run_in_threadpool(_backtest_summary, spec, request.timeframe, BACKTEST_BARS)
            except Exception as exc:  # noqa: BLE001 - research still works without it
                backtest = {"unavailable": True, "error": str(exc)}

    missing: list[str] = []
    for entry in resonance.get("unavailable", []):
        missing.append(f"{entry['interval']} 仅有 {entry['bars']}/{entry['required']} 根已收盘K线，该周期立场未参与评分")
    if funding_points == 0:
        missing.append("本地没有资金费历史，无法判断资金费方向与成本")
    if request.source == "upload":
        missing.append("历史上传模式未混入当前 ticker 与其他周期行情")
    if not backtest:
        missing.append("规则回测未能生成摘要")
    if not price_structure.get("atr"):
        missing.append("价格结构样本不足，无法计算 ATR，止损距离无从判断")

    bundle = build_bundle(
        spec=spec,
        timeframe=request.timeframe,
        frames=resonance_frames(resonance),
        resonance=resonance,
        ticker=ticker,
        funding_points=funding_points,
        funding_all_zero=funding_all_zero,
        signals=_rule_signals(resonance, ticker),
        backtest=backtest,
        price_structure=price_structure,
        news=request.news,
        missing=missing,
    )

    # External research is optional enrichment: collected best-effort, and the run
    # proceeds with the engine's own evidence when it is missing or rejected.
    external_bundle = await run_in_threadpool(
        collect_best_effort,
        symbol=spec.venue_symbol,
        trade_date=_evidence_date(request),
        home=home,
        # A demo chart is not market data, and the report forbids using demo data
        # to drive an external provider call.
        skip_reason="演示数据不调用外部研究" if request.source == "demo" else "",
    )
    external_prompt = prompt_block(external_bundle)

    focus = f"\n用户额外关注：{request.focus.strip()}" if request.focus and request.focus.strip() else ""
    user_text = (
        "以下是系统采集的证据包，请按系统要求输出 JSON。\n\n"
        + json.dumps(bundle.prompt_payload(), ensure_ascii=False, indent=1, default=str)
        + (f"\n\n{external_prompt}" if external_prompt else "")
        + focus
    )

    budget = request.maxTokens or profile.max_tokens or 12000

    def call_model() -> dict:
        driver = _profile_driver(profile)
        return driver.complete(
            [
                ChatMessage(role="system", text=RESEARCH_SYSTEM_PROMPT),
                ChatMessage(role="user", text=user_text),
            ],
            model=profile.model_for(role),
            temperature=0.2,
            max_tokens=budget,
        ).as_dict()

    try:
        completion = await run_in_threadpool(call_model)
    except HTTPException:
        raise
    except LLMError as exc:
        raise HTTPException(502, json.dumps(exc.as_dict(), ensure_ascii=False)) from exc

    usage = completion.get("usage") or {}
    truncated = usage.get("finish_reason") == "length"
    if not completion["text"].strip() and truncated:
        raise HTTPException(
            502,
            json.dumps(
                {
                    "kind": "truncated",
                    "title": "模型没有产出正文",
                    "detail": (
                        f"{completion['model']} 把 {budget} token 全部用于推理"
                        f"（reasoning_tokens={usage.get('reasoning_tokens')}），未留下任何回答。"
                    ),
                    "action": "在设置页把该 profile 的「最大 token」调大（推理模型建议 12000 起），或换用非推理模型。",
                },
                ensure_ascii=False,
            ),
        ) from None

    report = parse_report(completion["text"])
    if report is None and truncated:
        raise HTTPException(
            502,
            json.dumps(
                {
                    "kind": "truncated",
                    "title": "模型回答被截断",
                    "detail": (
                        f"推理用了 {usage.get('reasoning_tokens')} token，正文写到一半就到达 {budget} 上限，无法解析为 JSON。"
                    ),
                    "action": "在设置页提高该 profile 的「最大 token」后重试。",
                },
                ensure_ascii=False,
            ),
        ) from None
    if report:
        # Plan levels are the model's own choice, so they are judged for
        # coherence and risk rather than looked up in the bundle.
        plan_check = validate_plan(report.get("trading_plan"), bundle)
        validation = validate_report(report, bundle, plan_check)
    else:
        plan_check = {"present": False, "ok": False, "problems": ["模型未返回可用 JSON"], "notes": []}
        validation = {
            "unsupportedNumbers": [],
            "nonEvidenceCitations": [],
            "signFlippedNumbers": [],
            "derivedNumbers": [],
            "directives": [],
            "citedKeys": [],
            "unknownCitedKeys": [],
            "plan": plan_check,
            "verified": False,
        }
    markdown = report_markdown(report or {"headline": "模型未返回可用 JSON"}, bundle, validation)
    if external_bundle.external_enabled:
        markdown = f"{markdown}\n\n{appendix(external_bundle)}"

    # ---- archive it, with the provenance needed to audit it later
    report_id = None
    try:
        db = Database(home / "quantdesk.db")
        db.execute(
            "INSERT OR REPLACE INTO ta_reports (symbol, trade_date, asset_type, rating, ok, duration_s, reports, meta, created_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                spec.venue_symbol,
                time.strftime("%Y-%m-%d"),
                spec.product_type,
                (report or {}).get("headline"),
                1 if report else 0,
                completion["latency_s"],
                markdown,
                json.dumps(
                    {
                        "profile": profile.name,
                        "model": completion["model"],
                        "usage": completion["usage"],
                        "validation": validation,
                        "evidenceKeys": [item.key for item in bundle.items],
                        "missing": missing,
                        "externalEvidence": summary_meta(external_bundle),
                    },
                    ensure_ascii=False,
                ),
                int(time.time() * 1000),
            ),
        )
        rows = db.query("SELECT id FROM ta_reports ORDER BY id DESC LIMIT 1")
        report_id = int(rows[0]["id"]) if rows else None
    except Exception as exc:  # noqa: BLE001 - never lose the answer over an archive failure
        validation.setdefault("archiveError", str(exc))

    return {
        "ok": report is not None,
        "reportId": report_id,
        "profile": profile.name,
        "model": completion["model"],
        "latencySeconds": completion["latency_s"],
        "usage": completion["usage"],
        "maxTokens": budget,
        "notes": completion.get("notes") or [],
        "report": report,
        "markdown": markdown,
        "validation": validation,
        "evidence": [item.__dict__ for item in bundle.items],
        "externalEvidence": {
            **summary_meta(external_bundle),
            "appendix": appendix(external_bundle),
            "records": [record.as_dict() for record in external_bundle.usable],
        },
        "priceStructure": price_structure,
        "source": request.source,
        "missing": missing,
        "builtAt": bundle.built_at,
        "disclaimer": (
            "本报告由模型基于上传的历史证据生成，是历史研究观察而非当前行情或交易指令。"
            if request.source == "upload"
            else "本报告由模型基于上述证据生成，是研究观察而非交易指令；价格与指标以交易所实时数据为准。"
        ),
    }


@router.get("/reports")
def reports(limit: int = 50):
    """Archived reports, newest first."""
    try:
        db = Database(quantdesk_home() / "quantdesk.db")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"无法打开本地数据库：{exc}") from exc
    rows = db.query(
        "SELECT id, symbol, trade_date, rating, ok, duration_s, meta, created_ts FROM ta_reports ORDER BY id DESC LIMIT ?",
        (int(limit),),
    )
    out = []
    for row in rows:
        meta = {}
        try:
            meta = json.loads(row["meta"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        out.append(
            {
                "id": row["id"],
                "symbol": row["symbol"],
                "tradeDate": row["trade_date"],
                "headline": row["rating"],
                "ok": bool(row["ok"]),
                "durationSeconds": row["duration_s"],
                "createdAt": row["created_ts"],
                "model": meta.get("model"),
                "profile": meta.get("profile"),
                "verified": bool((meta.get("validation") or {}).get("verified")),
            }
        )
    return {"reports": out}


@router.get("/reports/{report_id}")
def report_detail(report_id: int):
    try:
        db = Database(quantdesk_home() / "quantdesk.db")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"无法打开本地数据库：{exc}") from exc
    rows = db.query("SELECT * FROM ta_reports WHERE id = ?", (report_id,))
    if not rows:
        raise HTTPException(404, f"没有找到报告 #{report_id}")
    row = rows[0]
    try:
        meta = json.loads(row["meta"] or "{}")
    except json.JSONDecodeError:
        meta = {}
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "tradeDate": row["trade_date"],
        "headline": row["rating"],
        "markdown": row["reports"],
        "meta": meta,
        "createdAt": row["created_ts"],
    }
