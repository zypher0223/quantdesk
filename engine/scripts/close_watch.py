"""Watch one real 15m bar close on the local stream and time the pipeline.

    engine/.venv/bin/python scripts/close_watch.py --wait-until 04:30

It reports, per symbol: the venue's own timestamp on the confirmed frame, the
moment this process received it, and the moment the browser-facing frame was
published, which together are the §6.3 "confirmed close to persisted and
evaluated" latency.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import time

import httpx

import websockets

BASE = "http://127.0.0.1:8765"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-until", default="", help="UTC HH:MM to start waiting for a close")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--interval", default="15m")
    args = parser.parse_args()

    if args.wait_until:
        hour, minute = (int(part) for part in args.wait_until.split(":"))
        now = datetime.datetime.now(datetime.UTC)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target < now:
            target += datetime.timedelta(days=1)
        delay = (target - now).total_seconds() - 5
        print(f"waiting {delay:.0f}s until {target:%H:%M} UTC", flush=True)
        await asyncio.sleep(max(0.0, delay))

    closes: list[dict] = []
    async with websockets.connect(BASE.replace("http", "ws") + "/api/market/stream", proxy=None, max_size=2**22) as socket:
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=max(0.2, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            frame = json.loads(raw)
            if frame.get("kind") != "candle" or not frame.get("closed"):
                continue
            if frame.get("interval") != args.interval:
                continue
            closes.append(
                {
                    "symbol": frame["symbol"],
                    "openTs": frame["openTs"],
                    "exchangeTs": frame["exchangeTs"],
                    "receivedTs": frame["receivedTs"],
                    "sentTs": frame["sentTs"],
                    "close": frame["close"],
                    "observedAt": int(time.time() * 1000),
                }
            )
            print("CandleClosed", frame["symbol"], datetime.datetime.fromtimestamp(frame["openTs"] / 1000, datetime.UTC).strftime("%H:%M"), flush=True)

    if not closes:
        print(json.dumps({"closes": 0, "note": "no confirmed close inside the window"}))
        return 0

    delays = [entry["sentTs"] - entry["exchangeTs"] for entry in closes]
    venue_to_local = [entry["receivedTs"] - entry["exchangeTs"] for entry in closes]
    print(
        json.dumps(
            {
                "closes": len(closes),
                "uniqueSymbols": len({entry["symbol"] for entry in closes}),
                "exchangeToPublishMs": {"min": min(delays), "max": max(delays), "avg": round(sum(delays) / len(delays), 1)},
                "exchangeToReceiveMs": {
                    "min": min(venue_to_local),
                    "max": max(venue_to_local),
                    "avg": round(sum(venue_to_local) / len(venue_to_local), 1),
                },
                "first": closes[0],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        state = (await client.get(f"{BASE}/api/market/state")).json()
        print(
            json.dumps(
                {
                    "closedEvents": state["closedEvents"],
                    "duplicateClosed": state["duplicateClosed"],
                    "backfills": state["backfills"],
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
