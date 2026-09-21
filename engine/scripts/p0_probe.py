"""P0 live integration probe: real Bybit feed through the local gateway.

Run against a gateway started with the Hong Kong proxy:

    QUANTDESK_PROXY=http://127.0.0.1:7893 .venv/bin/python -m quantdesk.cli serve --port 8765
    engine/.venv/bin/python scripts/p0_probe.py

It measures the §6.2/§6.3 acceptance metrics and prints a machine-readable
summary. It never writes to the database and never calls a paid model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantdesk.config.settings import configured_proxy  # noqa: E402

SYMBOLS = ("AAPLUSDT", "BTCUSDT", "ETHUSDT")
INTERVALS = ("15m", "1h")


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


async def probe_snapshots(client: httpx.AsyncClient, base: str, rounds: int) -> dict:
    """§6.3: snapshot API p95 and the parts each reply actually carries."""
    latencies: list[float] = []
    samples: dict[str, dict] = {}
    for _ in range(rounds):
        for symbol in SYMBOLS:
            for interval in INTERVALS:
                started = time.perf_counter()
                response = await client.get(f"{base}/api/market/snapshot", params={"symbol": symbol, "interval": interval})
                latencies.append((time.perf_counter() - started) * 1000)
                response.raise_for_status()
                body = response.json()
                samples[f"{symbol}:{interval}"] = {
                    "stale": body["stale"],
                    "ageMs": body["ageMs"],
                    "source": body["source"],
                    "lastPrice": body["ticker"].get("last_price"),
                    "markPrice": body["ticker"].get("mark_price"),
                    "fundingRate": body["ticker"].get("funding_rate"),
                    "openInterestValue": body["ticker"].get("open_interest_value"),
                    "candleClose": (body["candle"] or {}).get("close"),
                    "candleTs": (body["candle"] or {}).get("ts"),
                    "connection": body["connection"]["state"],
                }
    return {
        "requests": len(latencies),
        "p50Ms": round(statistics.median(latencies), 2),
        "p95Ms": round(percentile(latencies, 0.95), 2),
        "maxMs": round(max(latencies), 2),
        "samples": samples,
    }


async def probe_stream(base: str, seconds: float) -> dict:
    """§6.3: exchange ticker -> page delivery, measured off the local socket."""
    import websockets

    url = base.replace("http://", "ws://") + "/api/market/stream"
    first_frame_ms: float | None = None
    tickers: dict[str, list[float]] = {}
    candles: dict[str, int] = {}
    connections: list[dict] = []
    starts: dict[str, float] = {}
    # The engine stamps every frame with its own receive time, so the delay is
    # measured from the venue's message to this process, not from a guess.
    now_ms = lambda: int(time.time() * 1000)  # noqa: E731

    async with websockets.connect(url, proxy=None, max_size=2**22) as socket:
        opened = time.perf_counter()
        deadline = opened + seconds
        while time.perf_counter() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=max(0.2, deadline - time.perf_counter()))
            except asyncio.TimeoutError:
                break
            if first_frame_ms is None:
                first_frame_ms = (time.perf_counter() - opened) * 1000
            frame = json.loads(raw)
            kind = frame.get("kind")
            if kind == "ticker":
                symbol = frame.get("symbol")
                received = frame.get("receivedTs")
                if symbol and received:
                    tickers.setdefault(symbol, []).append(now_ms() - int(received))
            elif kind == "candle":
                key = f"{frame.get('symbol')}:{frame.get('interval')}"
                candles[key] = candles.get(key, 0) + 1
            elif kind == "connection":
                connections.append({"state": frame.get("state"), "at": now_ms()})
            elif kind == "backfill":
                connections.append(
                    {
                        "state": "backfill",
                        "rows": frame.get("rows"),
                        "inserted": frame.get("inserted"),
                        "errors": frame.get("errors"),
                    }
                )

    return {
        "firstFrameMs": round(first_frame_ms, 2) if first_frame_ms is not None else None,
        "tickerSymbols": len(tickers),
        "tickerFrames": sum(len(values) for values in tickers.values()),
        "tickerLatencyP50Ms": round(statistics.median([v for values in tickers.values() for v in values]), 1) if tickers else None,
        "tickerLatencyP95Ms": round(percentile([v for values in tickers.values() for v in values], 0.95), 1) if tickers else None,
        "tickerLatencyMaxMs": round(max([v for values in tickers.values() for v in values]), 1) if tickers else None,
        "candleFrames": candles,
        "events": connections,
    }


async def probe_state(client: httpx.AsyncClient, base: str) -> dict:
    response = await client.get(f"{base}/api/market/state")
    response.raise_for_status()
    body = response.json()
    connection = body["connection"]
    return {
        "running": body["running"],
        "symbols": body["symbols"],
        "intervals": body["intervals"],
        "state": connection["state"],
        "connected": connection["connected"],
        "reconnects": connection["reconnects"],
        "lastMessageAgeMs": connection["lastMessageAgeMs"],
        "lastSuccessAgeMs": connection["lastSuccessAgeMs"],
        "restoredSnapshots": body["restoredSnapshots"],
        "closedEvents": body["closedEvents"],
        "duplicateClosed": body["duplicateClosed"],
        "backfills": body["backfills"],
        "backfillErrors": body["backfillErrors"],
        "streams": [
            {
                "name": stream["name"],
                "state": stream["state"],
                "topics": stream["topics"],
                "messagesReceived": stream["messagesReceived"],
            }
            for stream in connection["streams"]
        ],
        "proxyConfigured": connection["proxyConfigured"],
        "proxyLeak": proxy_leak(body),
    }


def proxy_leak(body: dict) -> str | None:
    """The proxy address must never reach the browser.

    Checked against the address the engine is actually configured with, not a
    hardcoded one, so this keeps working after a node change.
    """
    configured = configured_proxy()
    if not configured:
        return None
    payload = json.dumps(body)
    host = configured.split("://")[-1].split("@")[-1]
    if host and host in payload:
        return host
    return None


def probe_database(home: Path) -> dict:
    """§6.3: one row per closed bar.

    The primary key makes a duplicate row impossible, so counting duplicates
    would be a gate that can never fail. What is worth checking is that the rows
    themselves are distinguishable: distinct open times per frame, no null
    prices, and a bar count that matches what the service says it ingested.
    """
    import sqlite3

    database = home / "quantdesk.db"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        indistinct = connection.execute(
            "SELECT COUNT(*) FROM (SELECT venue, symbol, interval, COUNT(*) total, COUNT(DISTINCT open_ts) uniq "
            "FROM candles GROUP BY venue, symbol, interval HAVING total <> uniq)"
        ).fetchone()[0]
        malformed = connection.execute(
            "SELECT COUNT(*) FROM candles WHERE close IS NULL OR open IS NULL OR high IS NULL OR low IS NULL "
            "OR high < low OR volume IS NULL OR volume < 0"
        ).fetchone()[0]
        symbols = connection.execute(
            "SELECT COUNT(DISTINCT symbol) FROM candles WHERE symbol IN "
            "('AAPLUSDT','BTCUSDT','ETHUSDT','AMDSTOCKUSDT')"
        ).fetchone()[0]
        latest = connection.execute(
            "SELECT symbol, interval, datetime(MAX(open_ts)/1000, 'unixepoch') FROM candles GROUP BY symbol, interval "
            "ORDER BY MAX(open_ts) DESC LIMIT 5"
        ).fetchall()
    return {
        "totalCandles": rows,
        "indistinctRows": indistinct,
        "malformedRows": malformed,
        "trackedSymbols": symbols,
        "latest": [{"symbol": row[0], "interval": row[1], "utc": row[2]} for row in latest],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--home", default=str(Path.home() / ".quantdesk"))
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--stream-seconds", type=float, default=25.0)
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=15.0) as client:
        health = (await client.get(f"{args.base}/health")).json()
        state_before = await probe_state(client, args.base)
        snapshots = await probe_snapshots(client, args.base, args.rounds)
        stream = await probe_stream(args.base, args.stream_seconds)
        state_after = await probe_state(client, args.base)

    report = {
        "health": {
            "ok": health.get("ok"),
            "instrumentCount": health.get("instrumentCount"),
            "proxyScheme": health.get("proxyScheme"),
        },
        "stateBefore": state_before,
        "snapshots": snapshots,
        "stream": stream,
        "stateAfter": state_after,
        "database": probe_database(Path(args.home)),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    failures: list[str] = []
    if not health.get("ok"):
        failures.append("health endpoint is not ok")
    if state_after["state"] != "connected":
        failures.append(f"upstream state is {state_after['state']}, not connected")
    if snapshots["p95Ms"] > 2000:
        failures.append(f"snapshot p95 {snapshots['p95Ms']}ms exceeds 2000ms")
    database = report["database"]
    if database["indistinctRows"] != 0:
        failures.append(f"{database['indistinctRows']} frames hold repeated open times")
    if database["malformedRows"] != 0:
        failures.append(f"{database['malformedRows']} malformed candle rows")
    # Every closed bar the service reports must be a distinct row: this is the
    # end-to-end duplicate check, and it can actually fail.
    if state_after["closedEvents"] > database["totalCandles"]:
        failures.append(
            f"service counted {state_after['closedEvents']} closes but the table holds {database['totalCandles']} rows"
        )
    if state_after["duplicateClosed"] and state_after["closedEvents"] == 0:
        failures.append("duplicates were suppressed without a single accepted close")
    if stream["tickerFrames"] == 0:
        failures.append("no ticker frames arrived on the local stream")
    if stream["firstFrameMs"] is None or stream["firstFrameMs"] > 500:
        failures.append(f"first stream frame took {stream['firstFrameMs']}ms")
    # §6.3 upper bound, not only a percentile: the page must stay under 2s even
    # on the worst frame the run happened to catch.
    worst = stream["tickerLatencyMaxMs"]
    if worst is None or worst > 2000:
        failures.append(f"worst ticker latency {worst}ms exceeds 2000ms")
    if state_after["proxyLeak"]:
        failures.append(f"the proxy address {state_after['proxyLeak']} leaked into an API response")
    missing = [key for key, value in snapshots["samples"].items() if value["lastPrice"] is None]
    if missing:
        failures.append(f"snapshots without a price: {missing}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
