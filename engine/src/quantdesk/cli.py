"""Typer CLI — engine usable headless, independent of the web UI."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any

from pathlib import Path

import typer

from .config.settings import ensure_keys_env, load_app_config, quantdesk_home
from .datahub.bybit import BybitClient
from .datahub.cache import CandleCache
from .datahub.db import Database
from .datahub.hyperliquid import candles as hl_candles
from .datahub.hyperliquid import funding_history as hl_funding_history
from .datahub.hyperliquid import perp_markets as hl_perp_markets
from .datahub.hyperliquid import predicted_fundings as hl_predicted_fundings

app = typer.Typer(help="QuantDesk 引擎 CLI", no_args_is_help=True, add_completion=False)
fetch_app = typer.Typer(help="拉取行情落库", no_args_is_help=True)
plugins_app = typer.Typer(help="管理外部仓库插件", no_args_is_help=True)
factors_app = typer.Typer(help="因子目录与闸门", no_args_is_help=True)
app.add_typer(fetch_app, name="fetch")
app.add_typer(plugins_app, name="plugins")
app.add_typer(factors_app, name="factors")


def _db() -> Database:
    return Database(quantdesk_home() / "quantdesk.db")


def _plugin_manager():
    from .plugins import PluginManager

    return PluginManager(quantdesk_home())


def _plugin_action(action: Callable[[], Any]) -> Any:
    """Render expected plugin failures as concise CLI errors."""
    from .plugins import PluginError

    try:
        return action()
    except PluginError as exc:
        typer.echo(f"plugin error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@plugins_app.command("list")
def plugins_list() -> None:
    """List valid and invalid plugin manifests without executing them."""
    manager = _plugin_manager()
    plugins, invalid = manager.discover()
    for plugin in plugins:
        state = "enabled" if plugin.enabled else "disabled"
        typer.echo(
            f"{plugin.manifest.id:24s} {plugin.manifest.version:12s} {state:8s} "
            f"{','.join(plugin.manifest.capabilities)}"
        )
    for item in invalid:
        typer.echo(f"INVALID {item['path']}: {item['error']}", err=True)


@plugins_app.command("install")
def plugins_install(
    source: str = typer.Argument(..., help="本地目录或 https://github.com/OWNER/REPO"),
    ref: str | None = typer.Option(None, "--ref", help="Git 分支或标签；生产环境建议固定标签"),
) -> None:
    """Copy/clone and validate a plugin repository; never executes setup scripts."""
    plugin = _plugin_action(lambda: _plugin_manager().install(source, ref))
    typer.echo(f"installed {plugin.manifest.id} {plugin.manifest.version} -> {plugin.path}")
    typer.echo("disabled by default; run `quantdesk plugins enable ID` after reviewing the repository")


@plugins_app.command("enable")
def plugins_enable(plugin_id: str) -> None:
    plugin = _plugin_action(lambda: _plugin_manager().set_enabled(plugin_id, True))
    typer.echo(f"enabled {plugin.manifest.id}")


@plugins_app.command("disable")
def plugins_disable(plugin_id: str) -> None:
    plugin = _plugin_action(lambda: _plugin_manager().set_enabled(plugin_id, False))
    typer.echo(f"disabled {plugin.manifest.id}")


@plugins_app.command("check")
def plugins_check(plugin_id: str) -> None:
    result = _plugin_action(lambda: _plugin_manager().health(plugin_id))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=1))


@plugins_app.command("sandbox")
def plugins_sandbox() -> None:
    """报告插件操作系统沙箱是否真的生效，失败时给出可复现命令。"""
    from .plugins import sandbox

    report = sandbox.diagnose()
    typer.echo(f"策略:   {report.policy}")
    typer.echo(f"后端:   {report.backend or '未检测到'}")
    typer.echo(f"可用:   {report.available}")
    typer.echo(f"已强制: {report.enforced}")
    if report.signal:
        typer.echo(f"信号:   {report.signal}")
    typer.echo(f"说明:   {report.detail}")
    if report.diagnostic:
        typer.echo(f"诊断:   {report.diagnostic}")
    if report.probe_command and not report.available:
        typer.echo("复现命令:")
        typer.echo("  " + " ".join(report.probe_command))
    if not report.enforced and report.policy == "preferred":
        typer.echo()
        typer.echo("注意：当前插件没有操作系统级隔离，仅受环境变量白名单、超时和输出大小限制。")
        typer.echo("      要求强制隔离请设置 QUANTDESK_PLUGIN_SANDBOX=required（沙箱不可用时会拒绝运行插件）。")


@plugins_app.command("update")
def plugins_update(
    plugin_id: str,
    ref: str | None = typer.Option(None, "--ref", help="新的 Git 分支或标签；留空沿用安装来源"),
) -> None:
    result = _plugin_action(lambda: _plugin_manager().update(plugin_id, ref))
    plugin = result["plugin"]
    typer.echo(f"updated {plugin_id}: {result['previousVersion']} -> {plugin['version']}")
    typer.echo("disabled after update; review dependencies and run check before enabling")


@plugins_app.command("dependencies")
def plugins_dependencies(
    plugin_id: str,
    install: bool = typer.Option(False, "--install", help="从带哈希的锁文件建立独立虚拟环境"),
) -> None:
    manager = _plugin_manager()
    result = _plugin_action(
        lambda: manager.install_dependencies(plugin_id) if install else manager.dependency_status(plugin_id)
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=1))


@plugins_app.command("uninstall")
def plugins_uninstall(
    plugin_id: str,
    purge_data: bool = typer.Option(False, "--purge-data", help="同时删除该插件的私有数据"),
) -> None:
    result = _plugin_action(lambda: _plugin_manager().uninstall(plugin_id, purge_data=purge_data))
    typer.echo(json.dumps(result, ensure_ascii=False))


@factors_app.command("gate-scan")
def factors_gate_scan(
    group: str = typer.Option(..., "--group", help="stock / leveraged_etf / crypto"),
    interval: str = typer.Option("1h", "--interval", help="15m / 1h / 4h / 1d"),
    horizon: int = typer.Option(0, "--horizon", help="前向收益的 bar 数；0 表示 24（日内）/1（日线）"),
    bars: int = typer.Option(2_000, "--bars", help="每个标的读取的K线根数"),
    factors: str = typer.Option("all", "--factors", help="all 或逗号分隔的因子 ID"),
    out: str = typer.Option("", "--out", help="把完整报告写到这个 JSON 文件"),
) -> None:
    """对一组因子跑七道闸门，产出受控因子库。

    闸门只读 QuantDesk 自己的K线，严格 as-of，不做任何联网或下单动作。
    """
    from . import factor_gates

    code = factor_gates.main([
        "--group", group, "--interval", interval, "--horizon", str(horizon),
        "--bars", str(bars), "--factors", factors, *(["--out", out] if out else []),
    ])
    if code:
        raise typer.Exit(code=code)


@factors_app.command("library")
def factors_library(out: str = typer.Option("", "--out", help="把受控因子库写到这个 JSON 文件")) -> None:
    """显示当前受控因子库（由最近一次闸门扫描决定）。"""
    from .factor_library import horizon_sensitivity, load_library_report, summarise

    home = quantdesk_home()
    report = load_library_report(home)
    if report is None:
        typer.echo("还没有闸门扫描报告；先运行 `quantdesk factors gate-scan`")
        raise typer.Exit(code=1)
    typer.echo(summarise(report))
    sensitive = horizon_sensitivity(home)
    if sensitive:
        typer.echo("\n换视野后结论变化的因子：")
        for entry in sorted(sensitive.values(), key=lambda item: item["factorId"]):
            horizons = "；".join(
                f"h{key} {item['tier'] or '不入库'} IC "
                f"{'—' if item['ic'] is None else format(item['ic'], '+.4f')}"
                for key, item in sorted(entry["horizons"].items(), key=lambda pair: int(pair[0]))
            )
            typer.echo(f"  {entry['factorId']}: {horizons}")
    if out:
        Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        typer.echo(f"报告 -> {out}")


def _proxy() -> str | None:
    return os.environ.get("QUANTDESK_PROXY") or None


def _hl_fetch(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    return hl_candles(symbol, interval, start_ms, end_ms, proxy=_proxy())


def _bybit_category(symbol: str) -> str:
    # QuantDesk 固定合约池全部为 Bybit linear USDT 永续。
    from .config.instruments import VENUE_SYMBOLS, require_instrument

    require_instrument(symbol)
    return "linear"


@fetch_app.command("history")
def fetch_history(
    symbol: str = typer.Argument(..., help="如 NVDAUSDT"),
    intervals: str = typer.Option("1h", "--intervals", help="逗号分隔；15m 全history 很慢"),
    max_pages: int = typer.Option(40, "--max-pages", help="本次最多回溯多少页（每页 1000 根）"),
    restart: bool = False,
    snapshot: bool = True,
) -> None:
    """Walk a contract's history back to its listing, resumably.

    Progress is saved after every page, so an interrupted run continues where it
    stopped. Failures are classified rather than only reported.
    """
    from .datahub.backfill import HistoryBackfill, bybit_fetch

    db = _db()
    client = BybitClient(proxy=_proxy())
    category = _bybit_category(symbol)
    backfill = HistoryBackfill(db, bybit_fetch(client, category))
    for itv in [item.strip() for item in intervals.split(",") if item.strip()]:
        result = backfill.run(symbol, itv, max_pages=max_pages, restart=restart)
        payload = result.as_dict()
        span = ""
        if payload["oldestTs"] and payload["newestTs"]:
            span = (
                f" {time.strftime('%Y-%m-%d', time.gmtime(payload['oldestTs'] / 1000))}"
                f"→{time.strftime('%Y-%m-%d', time.gmtime(payload['newestTs'] / 1000))}"
            )
        typer.echo(
            f"{symbol:10s} {itv:4s} 本次页数={payload['pages']:3d} 累计={payload['pagesTotal']:3d} "
            f"累计根数={payload['barsStored']:6d} 已回溯到上线={str(payload['complete']):5s} "
            f"停止原因={payload['stoppedBecause'] or '-':16s}{span}"
        )
        if payload["failure"]:
            typer.echo(f"    ! {payload['failureKind']}：{payload['failureLabel']}（{payload['failure']}）")
        if snapshot:
            record = backfill.snapshot(symbol, itv)
            if record["available"]:
                # "complete" on a snapshot means the stored range has no gaps;
                # whether the walk reached the listing is the run's own field.
                gap_label = "无缺口" if record["complete"] else f"缺 {record['missing_in_session']} 根"
                typer.echo(
                    f"    快照 {record['version']}：{record['bars']} 根，"
                    f"区间内{gap_label}"
                )
            else:
                typer.echo(f"    快照不可用：{record['reason']}")
    typer.echo(f"db: {db.path}")


@fetch_app.command("matrix")
def fetch_matrix(
    symbols: str = typer.Option("", "--symbols", help="逗号分隔；留空为全部固定合约"),
    intervals: str = typer.Option("15m,1h,4h,1d", "--intervals"),
    kinds: str = typer.Option(
        "trade_candle,mark_candle,funding,open_interest,risk_limit", "--kinds",
        help="要回填的数据族",
    ),
    concurrency: int = typer.Option(2, "--concurrency", help="同时进行的任务数"),
    pages_per_minute: int = typer.Option(120, "--pages-per-minute", help="全局限速"),
    pages_per_run: int = typer.Option(20, "--pages-per-run", help="单任务每次运行最多取多少页"),
    reset: bool = False,
    dry_run: bool = False,
) -> None:
    """Build the whole backfill matrix and drain it, with a status board.

    The matrix is 17 contracts x 4 timeframes of candles plus the symbol-level
    series; each unit is a task you can pause, retry or cancel from the API.
    """
    import asyncio

    from .datahub.backfill import bybit_fetch
    from .datahub.history import HistoryCollector, KIND_LABELS
    from .datahub.tasks import BackfillQueue

    db = _db()
    client = BybitClient(proxy=_proxy())

    def factory() -> HistoryCollector:
        return HistoryCollector(db, BybitClient(proxy=_proxy()))

    selected = [item.strip() for item in symbols.split(",") if item.strip()] or None
    timeframes = [item.strip() for item in intervals.split(",") if item.strip()]
    data_kinds = [item.strip() for item in kinds.split(",") if item.strip()]

    # Metadata first: the launch time is what makes a page estimate meaningful,
    # and it is how a contract with no funding history is recognised.
    collector = factory()
    for symbol in selected or list(VENUE_SYMBOLS):
        try:
            collector.collect_instrument_meta(symbol)
        except Exception as exc:  # noqa: BLE001 - one contract must not stop the sweep
            typer.echo(f"  元数据失败 {symbol}: {type(exc).__name__}: {exc}", err=True)

    queue = BackfillQueue(
        db, factory, concurrency=concurrency, pages_per_minute=pages_per_minute,
        pages_per_run=pages_per_run,
    )
    built = queue.build_matrix(symbols=selected, timeframes=timeframes, data_kinds=data_kinds, reset=reset)
    typer.echo(f"任务矩阵：{built['summary']['total']} 个任务（本次新建/重置 {built['tasks']}）")
    if dry_run:
        for task in queue.status()["tasks"]:
            typer.echo(
                f"  {task['symbol']:10s} {task['kindLabel']:10s} {task['interval'] or '-':4s} "
                f"{task['status']:9s} 预计剩余 {task['pagesRemaining']} 页"
            )
        return

    asyncio.run(queue.run_pending())
    board = queue.status()
    typer.echo(f"完成情况：{board['summary']['byStatus']}")
    for kind, counts in sorted(board["byKind"].items()):
        typer.echo(f"  {KIND_LABELS.get(kind, kind):10s} {counts}")
    stalled = [t for t in board["tasks"] if t["status"] in ("failed", "unsupported")]
    for task in stalled[:10]:
        detail = task["failureLabel"] or task["reason"] or ""
        typer.echo(f"  ! {task['symbol']} {task['kindLabel']} {task['interval']}: {task['status']} {detail}", err=True)


@fetch_app.command("candles")
def fetch_candles(
    symbol: str = typer.Argument(..., help="如 BTCUSDT（bybit）或 BTC（hyperliquid）"),
    venue: str = typer.Option(..., "--venue", help="bybit | hyperliquid"),
    intervals: str = typer.Option("15m,1h,4h,1d", help="逗号分隔"),
    backfill: int | None = typer.Option(None, "--bars", help="首次回填根数（默认按周期）"),
) -> None:
    cfg = load_app_config()
    db = _db()
    for itv in [i.strip() for i in intervals.split(",") if i.strip()]:
        if venue == "bybit":
            client = BybitClient(proxy=_proxy())
            category = _bybit_category(symbol)
            cache = CandleCache(db, lambda s, i, a, b, c=client, cat=category: c.kline(cat, s, i, a, b))
        elif venue == "hyperliquid":
            cache = CandleCache(db, _hl_fetch)
        else:
            raise typer.BadParameter(f"unknown venue {venue!r}")
        n = cache.ensure(venue, symbol, itv, backfill_bars=backfill)
        total = db.count_candles(venue, symbol, itv)
        last = db.last_open_ts(venue, symbol, itv)
        last_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(last / 1000)) if last else "-"
        typer.echo(f"{venue:11s} {symbol:10s} {itv:4s} fetched={n:5d} total={total:5d} last_open={last_str} UTC")
    typer.echo(f"db: {db.path}")


@fetch_app.command("derivatives")
def fetch_derivatives(
    symbol: str = typer.Argument("BTCUSDT"),
    venue: str = typer.Option("bybit", "--venue"),
    oi_interval: str = typer.Option("1h", help="5min/15min/30min/1h/4h/1d"),
) -> None:
    """拉取资金费率与持仓量历史（仅永续）。"""
    db = _db()
    if venue == "bybit":
        client = BybitClient(proxy=_proxy())
        fr = client.funding_history(symbol, limit=500)
        oi = client.open_interest(symbol, interval_time=oi_interval, limit=500)
    elif venue == "hyperliquid":
        coin = symbol.removesuffix("-USD").removesuffix("USDT")
        fr = hl_funding_history(coin, proxy=_proxy())
        # HL 的 OI 从 perp_markets 取当前快照
        markets = {m["coin"]: m for m in hl_perp_markets(proxy=_proxy())}
        cur = markets.get(coin, {})
        oi = [{"ts": int(time.time() * 1000), "oi": cur.get("openInterest") or 0.0}]
    else:
        raise typer.BadParameter(f"unknown venue {venue!r}")
    typer.echo(f"funding rows={db.upsert_funding(venue, symbol, fr)} oi rows={db.upsert_oi(venue, symbol, oi)}")


@fetch_app.command("risk")
def fetch_risk(
    symbols: str = typer.Option("", "--symbols", help="逗号分隔；缺省为固定合约池全部"),
    force: bool = typer.Option(False, "--force", help="忽略本地新鲜度，强制刷新"),
    marks: str = typer.Option("", "--marks", help="同时拉取标记价的周期，如 1h,4h；缺省不拉"),
    mark_bars: int = typer.Option(600, "--mark-bars", help="每个周期拉取的标记价根数"),
) -> None:
    """落库交易所风险档位（维持保证金率/最高杠杆）与标记价序列。

    回测与模拟盘优先读取这里的数据；没有本地档位时按配置的固定维持保证金率估算，
    并在结果里明确标注。
    """
    from .config.instruments import VENUE_SYMBOLS
    from .risk import RiskBook

    targets = [item.strip() for item in symbols.split(",") if item.strip()] or list(VENUE_SYMBOLS)
    unknown = [item for item in targets if item not in VENUE_SYMBOLS]
    if unknown:
        raise typer.BadParameter(f"不在固定合约池内：{', '.join(unknown)}")

    db = _db()
    book = RiskBook(db)
    client = BybitClient(proxy=_proxy())
    try:
        report = book.sync(client, targets, force=force)
    finally:
        client.close()
    typer.echo(
        f"risk tiers: refreshed={report['refreshed']}/{report['requested']} "
        f"symbols={report['status']['symbols']} tiers={report['status']['tiers']}"
    )
    for failure in report["failed"]:
        typer.echo(f"  FAILED {failure}", err=True)

    intervals = [item.strip() for item in marks.split(",") if item.strip()]
    if intervals:
        client = BybitClient(proxy=_proxy(), timeout=25.0)
        try:
            for symbol in targets:
                for interval in intervals:
                    written = book.sync_marks(client, symbol, interval, limit=mark_bars)
                    typer.echo(f"marks {symbol:16s} {interval:4s} rows={written}")
        finally:
            client.close()
    typer.echo(json.dumps(db.risk_tier_status("bybit"), ensure_ascii=False))


@app.command("universe")
def list_universe() -> None:
    """校验固定合约池在 Bybit 的实时交易状态。"""
    from .config.instruments import INSTRUMENTS

    client = BybitClient(proxy=_proxy())
    found = client.configured_instruments()
    for item in INSTRUMENTS:
        inst = found.get(item.venue_symbol)
        if not inst:
            typer.echo(f"{item.display_symbol:8s} {item.venue_symbol:16s} INACTIVE")
            continue
        lot = inst.get("lotSizeFilter", {})
        typer.echo(f"{item.display_symbol:8s} {item.venue_symbol:16s} Trading minQty={lot.get('minOrderQty')} tick={inst.get('priceFilter', {}).get('tickSize')}")
    typer.echo(f"active: {len(found)}/{len(INSTRUMENTS)}")


@app.command("funding-screen")
def funding_screen(coin: str = typer.Option(None, "--coin", help="如 BTC；缺省全表前 20 行")) -> None:
    """跨所预测资金费率对比（HL vs Binance vs Bybit，APR%）。"""
    rows = hl_predicted_fundings(coin, proxy=_proxy())
    rows = [r for r in rows if r["hlAprPct"] is not None]
    rows.sort(key=lambda r: r["hlAprPct"], reverse=True)
    shown = rows if coin else rows[:20]
    typer.echo(f"{'coin':10s} {'hlApr%':>9s} {'binApr%':>9s} {'bybApr%':>9s} {'hl-bin':>8s}")
    for r in shown:
        typer.echo(
            f"{r['coin']:10s} {r['hlAprPct']:9.2f} "
            f"{(r['binanceAprPct'] if r['binanceAprPct'] is not None else float('nan')):9.2f} "
            f"{(r['bybitAprPct'] if r['bybitAprPct'] is not None else float('nan')):9.2f} "
            f"{(r['hlVsBinancePct'] if r['hlVsBinancePct'] is not None else float('nan')):8.2f}"
        )


@app.command("candles")
def show_candles(
    symbol: str = typer.Argument(...),
    venue: str = typer.Option(..., "--venue"),
    interval: str = typer.Option("1h", "--interval"),
    limit: int = typer.Option(5, "--limit"),
) -> None:
    db = _db()
    rows = db.load_candles(venue, symbol, interval, limit=limit)
    typer.echo(json.dumps(rows, ensure_ascii=False, indent=1))


def _augmented(cache: CandleCache, venue: str, symbol: str, interval: str, limit: int = 600):
    """Ensure fresh data, return indicator-augmented frame (completed bars)."""
    cache.ensure(venue, symbol, interval)
    df = cache.load_df(venue, symbol, interval, limit=limit)
    from .features.indicators import add_indicators

    df.attrs["interval"] = interval
    return add_indicators(df)


@app.command("resonance")
def resonance_cmd(
    symbol: str = typer.Argument(...),
    venue: str = typer.Option(..., "--venue"),
    intervals: str = typer.Option("15m,1h,4h,1d"),
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="先增量拉取"),
) -> None:
    """多周期共振面板（15m/1h/4h/1d 加权）。"""
    import json as _json

    from .features.resonance import DEFAULT_WEIGHTS, resonance, stance_for_interval

    db = _db()
    cache = CandleCache(db, _hl_fetch if venue == "hyperliquid" else _bybit_fetch)
    stances = []
    for itv in [i.strip() for i in intervals.split(",") if i.strip()]:
        if refresh:
            cache.ensure(venue, symbol, itv)
        df = cache.load_df(venue, symbol, itv, limit=600)
        if df.empty:
            typer.echo(f"{itv}: 无数据，跳过", err=True)
            continue
        from .features.indicators import add_indicators

        df.attrs["interval"] = itv
        stances.append(stance_for_interval(add_indicators(df)))
    if not stances:
        raise typer.Exit(1)
    result = resonance(stances, DEFAULT_WEIGHTS)
    typer.echo(_json.dumps(result, ensure_ascii=False, indent=1))


def _bybit_fetch(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    client = BybitClient(proxy=_proxy())
    return client.kline(_bybit_category(symbol), symbol, interval, start_ms, end_ms)


@app.command("sepa")
def sepa_cmd(
    symbol: str = typer.Argument(...),
    venue: str = typer.Option(..., "--venue"),
    benchmark: str = typer.Option("BTC", "--bench", help="RS 对照基准（hyperliquid 日线）"),
) -> None:
    """SEPA 趋势模板 8 条件 + VCP（日线）。"""
    db = _db()
    cache = CandleCache(db, _hl_fetch if venue == "hyperliquid" else _bybit_fetch)
    daily = _augmented(cache, venue, symbol, "1d", limit=800)
    bench = None
    try:
        bench_df = CandleCache(db, _hl_fetch).load_df("hyperliquid", benchmark, "1d", limit=800)
        if not bench_df.empty:
            bench = bench_df["close"]
    except Exception:
        pass
    from .features.sepa import trend_template, vcp

    result = trend_template(daily, bench)
    typer.echo(f"SEPA 趋势模板 {symbol}（日线）: {'✅ 全部通过' if result.qualified else '❌ 未全部通过'} "
               f"({result.passed_count}/{len(result.conditions)})")
    for c in result.conditions:
        mark = {True: "✅", False: "❌", None: "❓"}[c.passed]
        typer.echo(f"  {mark} #{c.index} {c.name} — {c.detail}")
    v = vcp(daily)
    typer.echo(f"VCP: {'✅ 检测到' if v['is_vcp'] else '未检测到'} {v}")


@app.command("derivs")
def derivs_cmd(
    symbol: str = typer.Argument("BTCUSDT"),
    venue: str = typer.Option("bybit", "--venue"),
) -> None:
    """衍生品面板：资金费率 / 持仓量 / 量能。"""
    import json as _json

    from .features.derivatives import derivatives_panel, funding_stats, oi_stats, volume_stats

    db = _db()
    if venue == "bybit":
        client = BybitClient(proxy=_proxy())
        fr = client.funding_history(symbol, limit=500)
        oi = client.open_interest(symbol, interval_time="1h", limit=500)
        hourly_cat = "linear"
    else:
        coin = symbol.removesuffix("-USD").removesuffix("USDT")
        fr = hl_funding_history(coin, proxy=_proxy())
        oi = []
        hourly_cat = None
    fstats = funding_stats(fr, interval_hours=8.0 if venue == "bybit" else 1.0)
    ostats = oi_stats(oi) if oi else {"available": False}
    cache = CandleCache(db, _hl_fetch if venue == "hyperliquid" else _bybit_fetch)
    daily = cache.load_df(venue, symbol, "1d", limit=260)
    panel = derivatives_panel(fstats, ostats, volume_stats(daily))
    typer.echo(_json.dumps(panel, ensure_ascii=False, indent=1))


@app.command("backtest")
def backtest_cmd(
    symbol: str = typer.Argument(..., help="如 AAPLUSDT 或 AAPL"),
    interval: str = typer.Option("1h", "--interval", help="15m/1h/4h/1d"),
    fast: int = typer.Option(9, "--fast"),
    slow: int = typer.Option(21, "--slow"),
    direction: str = typer.Option("both", "--direction", help="both | long"),
    capital: float = typer.Option(10_000.0, "--capital"),
    allocation: float = typer.Option(50.0, "--allocation", help="每次仓位占净值百分比"),
    fee_bps: float | None = typer.Option(None, "--fee-bps", help="缺省用配置默认"),
    slippage_bps: float | None = typer.Option(None, "--slippage-bps", help="缺省用配置默认（股票类更宽）"),
    leverage: float = typer.Option(1.0, "--leverage"),
    bars: int = typer.Option(600, "--bars", help="取多少根已收盘K线"),
    no_funding: bool = typer.Option(False, "--no-funding", help="不计历史资金费"),
    no_liquidation: bool = typer.Option(False, "--no-liquidation", help="不启用强平"),
    fill_on_thin: str = typer.Option("skip", "--fill-on-thin", help="skip | allow，休市空 bar 是否建仓"),
) -> None:
    """用引擎回测双均线规则（含资金费、杠杆、逐仓强平、交易所步长）。"""
    from .backtest import BacktestConfig, run_backtest
    from .config.instruments import require_instrument

    spec = require_instrument(symbol)
    cfg = load_app_config()
    paper = cfg.paper or {}
    default_slip = float(
        paper.get("slippage_bps", 5) if spec.is_crypto else paper.get("stock_perp_slippage_bps", paper.get("slippage_bps", 5))
    )
    client = BybitClient(proxy=_proxy())
    rows = client.kline_snapshot(spec.venue_symbol, interval, limit=bars, completed_only=True)
    live = client.configured_instruments().get(spec.venue_symbol) or {}
    client.close()
    if len(rows) < slow + 3:
        typer.echo(f"已收盘K线只有 {len(rows)} 根，至少需要 {slow + 3} 根", err=True)
        raise typer.Exit(1)

    last_close = rows[-1]["close"]
    config = BacktestConfig(
        fast_period=fast,
        slow_period=slow,
        direction=direction,
        initial_capital=capital,
        allocation_pct=allocation,
        fee_bps=fee_bps if fee_bps is not None else float(paper.get("taker_fee_bps", 10)),
        slippage_bps=slippage_bps if slippage_bps is not None else default_slip,
        leverage=leverage,
        include_funding=not no_funding,
        include_liquidation=not no_liquidation,
        fill_on_thin=fill_on_thin,
        tick_size=float(live.get("priceFilter", {}).get("tickSize") or 0) or None,
        qty_step=float(live.get("lotSizeFilter", {}).get("qtyStep") or 0) or None,
        min_order_notional=max(float(live.get("lotSizeFilter", {}).get("minOrderQty") or 0) * last_close, 1.0),
    )
    funding = _db().load_funding("bybit", spec.venue_symbol, start_ts=rows[0]["ts"], end_ts=rows[-1]["ts"])
    result = run_backtest(rows, config, funding=funding, instrument={"productType": spec.product_type}, interval=interval)

    typer.echo(f"{spec.display_symbol} {interval} · {len(rows)} 根 · 复利仓位 {allocation:g}% · 杠杆 {leverage:g}x")
    typer.echo(
        f"净收益 {result.net_return_pct:+.2f}%  最大回撤 {result.max_drawdown_pct:.2f}%  "
        f"胜率 {result.win_rate_pct:.1f}%  交易 {len(result.trades)} 笔  "
        f"手续费 {result.total_fees:.2f}  资金费 {result.total_funding:+.2f}"
    )
    for reason in ("liquidation",):
        count = sum(1 for trade in result.trades if trade.exit_reason == reason)
        if count:
            typer.echo(f"{'强平' if reason == 'liquidation' else reason}: {count} 笔")
    for warning in result.warnings:
        typer.echo(f"! {warning}")
    typer.echo("口径：" + "；".join(result.assumptions))


@app.command("data")
def data_cmd(
    symbol: str = typer.Option("", "--symbol", help="如 BTCUSDT 或 BTC；缺省为固定合约池全部"),
    interval: str = typer.Option("1h", "--interval"),
    bars: int = typer.Option(500, "--bars"),
    replay: bool = typer.Option(False, "--replay", help="逐条回放存储的历史（含空洞）"),
    repair: bool = typer.Option(False, "--repair", help="从交易所只补交易时段内的缺口"),
    limit: int = typer.Option(40, "--limit", help="回放显示条数"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """历史行情数据层：覆盖率、来源留档、可补的缺口，以及一次回放。"""
    from .config.instruments import VENUE_SYMBOLS, require_instrument
    from .datahub.snapshot import load_snapshot, snapshot_summary, snapshot_version
    from .datahub.venue import INTERVAL_MS, last_closed_open_ts

    db = _db()
    step = INTERVAL_MS.get(interval)
    if step is None:
        raise typer.BadParameter(f"不支持的周期 {interval!r}")
    if symbol:
        spec = require_instrument(symbol)
        targets, types = [spec.venue_symbol], {spec.venue_symbol: spec.product_type}
        displays = {spec.venue_symbol: spec.display_symbol}
    else:
        specs = [require_instrument(item) for item in VENUE_SYMBOLS]
        targets = [item.venue_symbol for item in specs]
        types = {item.venue_symbol: item.product_type for item in specs}
        displays = {item.venue_symbol: item.display_symbol for item in specs}

    stored = max((db.last_open_ts("bybit", item, interval) or 0) for item in targets)
    if stored <= 0:
        typer.echo("本地没有该周期的K线，先运行 fetch candles", err=True)
        raise typer.Exit(1)
    # The window ends at the last closed bar: a forming bar would be counted as
    # missing coverage, which is the opposite of what it is.
    newest = last_closed_open_ts(stored, step)
    from_ts = newest - (bars - 1) * step
    snapshot = load_snapshot(db, symbols=targets, interval=interval, from_ts=from_ts, to_ts=newest, product_types=types)

    if json_out:
        payload = snapshot_summary(snapshot)
        payload["version"] = snapshot_version(snapshot)
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=1))
        return

    provenance = snapshot.provenance
    typer.echo(f"版本 {snapshot_version(snapshot)} · {len(targets)} 个合约 · {provenance.bars} 根 {interval} K线")
    typer.echo(f"来源 {provenance.sources} · 采集器 {provenance.collectors}")
    typer.echo(f"{'合约':10s} {'应有':>6s} {'实有':>6s} {'时段内缺':>8s} {'非交易':>7s} {'服务中断':>8s} {'完整':>5s}")
    for item in targets:
        coverage = snapshot.coverage[item]
        typer.echo(
            f"{displays.get(item, item):10s} {coverage.expected:6d} {coverage.present:6d} "
            f"{coverage.missing_in_session:8d} {coverage.off_hours + coverage.weekend + coverage.holiday:7d} "
            f"{coverage.service_down:8d} {'是' if coverage.complete else '否':>5s}"
        )
    repairable = sum(snapshot.repair_list(item)["bars"] for item in targets)
    typer.echo(f"可补缺口合计 {repairable} 根（非交易时段与休市的缺口不计入）")
    if repair and repairable:
        from .datahub.bybit import BybitClient
        from .datahub.snapshot import repair_gaps

        client = BybitClient(proxy=_proxy(), timeout=25.0)
        try:
            outcome = repair_gaps(db, snapshot, client)
        finally:
            client.close()
        typer.echo(
            f"补洞：{outcome['ranges']} 段 / {outcome['bars']} 根，写入 {outcome['written']} 行"
        )
        for item in outcome["detail"]:
            typer.echo(f"  {item}")
        for failure in outcome["errors"]:
            typer.echo(f"! {failure}", err=True)
        snapshot = load_snapshot(
            db, symbols=targets, interval=interval, from_ts=from_ts, to_ts=newest, product_types=types
        )
        remaining = sum(snapshot.repair_list(item)["bars"] for item in targets)
        typer.echo(f"补洞后剩余可补缺口 {remaining} 根 · 新版本 {snapshot_version(snapshot)}")
    for note in snapshot.notes:
        typer.echo(f"! {note}")

    if replay:
        events = list(snapshot.replay())
        typer.echo(f"回放 {len(events)} 条事件，前 {min(limit, len(events))} 条：")
        for event in events[:limit]:
            if event["kind"] == "candle":
                row = event["payload"]
                typer.echo(f"  {event['iso'][:19]} {event['symbol']:10s} candle close={row['close']} src={row.get('source')}")
            elif event["kind"] == "gap":
                typer.echo(f"  {event['iso'][:19]} {event['symbol']:10s} gap    {event['payload']['reason']} bars={event['payload']['bars']}")
            else:
                typer.echo(f"  {event['iso'][:19]} {event['symbol']:10s} {event['kind']}")


@app.command("validate")
def validate_cmd(
    symbol: str = typer.Argument(..., help="如 AAPLUSDT 或 AAPL"),
    interval: str = typer.Option("1h", "--interval"),
    bars: int = typer.Option(1500, "--bars", help="取多少根已收盘K线（验证需要更长样本）"),
    strategy: str = typer.Option("ma_cross", "--strategy"),
    train: float = typer.Option(0.6, "--train", help="训练段比例"),
    validation: float = typer.Option(0.2, "--validation", help="验证段比例，其余为测试段"),
    fast_grid: str = typer.Option("5,9,20", "--fast-grid", help="快线候选，逗号分隔"),
    slow_grid: str = typer.Option("21,50,100", "--slow-grid"),
    windows: int = typer.Option(4, "--windows", help="walk-forward 窗口数"),
    capital: float = typer.Option(10_000.0, "--capital"),
    allocation: float = typer.Option(50.0, "--allocation"),
    leverage: float = typer.Option(1.0, "--leverage"),
    json_out: bool = typer.Option(False, "--json", help="输出完整 JSON 报告"),
) -> None:
    """严格策略验证：三段分离、walk-forward、参数搜索、过拟合与泄漏检查。

    报告里的每一个收益数字都标注它来自哪一段；测试段只在参数选定后评估一次。
    """
    from .backtest import BacktestConfig
    from .config.instruments import require_instrument
    from .risk import RiskBook
    from .strategy.registry import generate_builtin_events, generate_events
    from .strategy.validation import (
        MIN_TRADES_FOR_CONFIDENCE,
        build_provenance,
        leakage_report,
        ranking_return,
        search_parameters,
        run_walk_forward,
        split_segments,
    )
    from .datahub.venue import INTERVAL_MS

    spec = require_instrument(symbol)
    db = _db()
    client = BybitClient(proxy=_proxy())
    try:
        rows = client.kline_snapshot(spec.venue_symbol, interval, limit=bars, completed_only=True)
        live = client.configured_instruments().get(spec.venue_symbol) or {}
    finally:
        client.close()
    if len(rows) < 200:
        typer.echo(f"已收盘K线只有 {len(rows)} 根，验证至少需要 200 根", err=True)
        raise typer.Exit(1)

    grid = {
        "fastPeriod": [int(value) for value in fast_grid.split(",") if value.strip()],
        "slowPeriod": [int(value) for value in slow_grid.split(",") if value.strip()],
    }
    warmup = max(grid["slowPeriod"]) + 2
    segments = split_segments(rows, train=train, validation=validation, warmup=warmup)
    book = RiskBook(db)
    profile = book.cached(spec.venue_symbol)
    funding = db.load_funding("bybit", spec.venue_symbol, start_ts=rows[0]["ts"], end_ts=rows[-1]["ts"])
    marks = book.marks(spec.venue_symbol, interval, start_ts=rows[0]["ts"], end_ts=rows[-1]["ts"])
    last_close = rows[-1]["close"]
    lot = live.get("lotSizeFilter", {})
    config = BacktestConfig(
        strategy_id=strategy,
        fast_period=grid["fastPeriod"][0],
        slow_period=grid["slowPeriod"][0],
        direction="both",
        initial_capital=capital,
        allocation_pct=allocation,
        leverage=leverage,
        include_funding=True,
        include_liquidation=True,
        tick_size=float(live.get("priceFilter", {}).get("tickSize") or 0) or None,
        qty_step=float(lot.get("qtyStep") or 0) or None,
        min_order_notional=max(float(lot.get("minOrderQty") or 0) * last_close, 1.0),
    )
    interval_ms = INTERVAL_MS.get(interval)

    def source(series, parameters):
        # Through the same entry as every other study: a CLI walk-forward that built
        # its own events could disagree with the API about what a strategy does.
        return generate_events(series, strategy, parameters)

    search = search_parameters(
        rows, config, grid, signal_source=source,
        train=segments[0], validation=segments[1], test=segments[2],
        funding=funding, marks=marks, risk_profile=profile, interval=interval,
        interval_ms=interval_ms, product_type=spec.product_type,
    )
    walk = run_walk_forward(
        rows, config, signal_source=source, grid=grid, windows=windows, train_fraction=0.5,
        warmup=warmup, funding=funding, marks=marks, risk_profile=profile, interval=interval,
        interval_ms=interval_ms, product_type=spec.product_type,
    )
    best_parameters = (search["best"] or {}).get("parameters") or {}
    signals = source(rows, best_parameters) if best_parameters else None
    leakage = leakage_report(rows, signal_source=source, parameters=best_parameters or {}, interval_ms=interval_ms or 0, signals=signals)
    provenance = build_provenance(
        strategy_id=strategy, parameters=best_parameters, candles=rows, config=config,
        symbol=spec.venue_symbol, interval=interval, risk_profile=profile, data_source="bybit",
    )

    report = {
        "symbol": spec.venue_symbol,
        "interval": interval,
        "bars": len(rows),
        "segments": [segment.as_dict() for segment in segments],
        "parameterSearch": search,
        "walkForward": walk,
        "leakage": leakage,
        "provenance": provenance.as_dict(),
    }
    if json_out:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=1))
        return

    typer.echo(f"{spec.display_symbol} {interval} · {len(rows)} 根 · 参数网格 {len(grid['fastPeriod']) * len(grid['slowPeriod'])} 组")
    for segment in segments:
        typer.echo(f"  {segment.name:11s} {segment.bars:5d} 根  {segment.from_ts} -> {segment.to_ts}（预热 {segment.warmup}）")
    best = search["best"] or {}
    inside = best.get("inSample") or {}
    outside = best.get("outOfSample") or {}
    test = search.get("test") or {}
    typer.echo(f"选定参数 {best.get('parameters')}")
    typer.echo(
        f"  训练段 收益 {ranking_return_echo(inside):+.2f}%  回撤 {inside.get('max_drawdown_pct', 0):.2f}%  "
        f"交易 {inside.get('trades', 0)}  Sharpe {fmt(inside.get('sharpe'))}"
    )
    typer.echo(
        f"  验证段 收益 {ranking_return_echo(outside):+.2f}%  回撤 {outside.get('max_drawdown_pct', 0):.2f}%  "
        f"交易 {outside.get('trades', 0)}  Sharpe {fmt(outside.get('sharpe'))}"
    )
    typer.echo(
        f"  测试段 收益 {ranking_return_echo(test):+.2f}%  回撤 {test.get('max_drawdown_pct', 0):.2f}%  "
        f"交易 {test.get('trades', 0)}  Sharpe {fmt(test.get('sharpe'))}  基准 {fmt(test.get('benchmark_return_pct'))}%"
    )
    typer.echo(
        f"walk-forward {len(walk['windows'])} 窗口 · 参数稳定 {walk['stableParameters']} · "
        f"验证段为正 {walk['positiveWindows']}/{len(walk['windows'])}"
    )
    typer.echo(f"泄漏检查 通过={leakage['clean']} 采样={leakage['signals']['checked']} 未收盘={len(leakage['bars']['unclosed'])}")
    for warning in [*search["warnings"], *walk["warnings"], *leakage["warnings"]]:
        typer.echo(f"! {warning}")
    if (inside.get("trades") or 0) < MIN_TRADES_FOR_CONFIDENCE:
        typer.echo(f"! 训练段成交少于 {MIN_TRADES_FOR_CONFIDENCE} 笔，结论不具统计意义")
    typer.echo(f"数据指纹 {provenance.data_hash} · 运行时间 {provenance.run_at}")


def ranking_return_echo(metrics: dict) -> float:
    """The return figure the report should quote for one segment."""
    if not metrics:
        return 0.0
    years = metrics.get("years") or 0.0
    if years >= 0.08 and metrics.get("annualised_return_pct") is not None:
        return float(metrics["annualised_return_pct"])
    return float(metrics.get("total_return_pct") or 0.0)


def fmt(value) -> str:
    return "—" if value is None else f"{float(value):.2f}"


@app.command("info")
def info() -> None:
    home = quantdesk_home()
    ensure_keys_env(home)
    cfg = load_app_config()
    typer.echo(f"home: {home}")
    typer.echo(f"db:   {home / 'quantdesk.db'}")
    typer.echo(f"intervals: {cfg.intervals}")
    typer.echo(f"universe: {cfg.universe.symbols}")
    if cfg.universe.migration_notes:
        for note in cfg.universe.migration_notes:
            typer.echo(f"note: {note}")


@app.command("serve")
def serve(port: int = typer.Option(8765, min=1024, max=65535)) -> None:
    """启动仅绑定本机的只读行情服务。"""
    import uvicorn

    uvicorn.run("quantdesk.api.server:app", host="127.0.0.1", port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
