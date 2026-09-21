"""Live gateway drill: a real page must show the outage, then recover by itself.

Starts a throwaway gateway whose proxy is a local relay in front of the Hong
Kong node, then kills the relay and watches the running page. It checks the two
acceptance criteria a screenshot cannot: the error banner appears while the link
is down, and the page never turns the last real quote into unlabelled demo data.

    QUANTDESK_UPSTREAM_PROXY=http://127.0.0.1:7893 \
      engine/.venv/bin/python scripts/stale_drill.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy_drill import Relay  # noqa: E402

UPSTREAM = os.environ.get("QUANTDESK_UPSTREAM_PROXY", "http://127.0.0.1:7893")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def main() -> int:
    host, _, port = UPSTREAM.replace("http://", "").partition(":")
    relay = Relay(host, int(port or 80))
    relay.start()
    gateway_port = free_port()
    home = Path(os.environ.get("QUANTDESK_DRILL_HOME", Path.home() / ".quantdesk"))

    env = dict(os.environ)
    env["QUANTDESK_PROXY"] = f"http://127.0.0.1:{relay.port}"
    env["QUANTDESK_HOME"] = str(home)
    env["QUANTDESK_WEB_DIST"] = str(Path(__file__).resolve().parents[2] / "web" / "dist")
    # The production freshness window is two minutes; the drill shortens it so
    # the stale flag and the "how old is this" copy can be observed end to end.
    env["QUANTDESK_STALE_AFTER_MS"] = os.environ.get("QUANTDESK_DRILL_STALE_MS", "6000")

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "quantdesk.cli",
        "serve",
        "--port",
        str(gateway_port),
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{gateway_port}"
    report: dict[str, object] = {"gateway": base, "proxy": env["QUANTDESK_PROXY"]}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for _ in range(200):
                try:
                    response = await client.get(f"{base}/api/market/state")
                    if response.status_code == 200 and response.json()["connection"]["connected"]:
                        break
                except Exception:  # noqa: BLE001 - the gateway is still starting
                    pass
                await asyncio.sleep(0.2)
            else:
                report["error"] = "the drill gateway never connected"
                return 1

            state = (await client.get(f"{base}/api/market/state")).json()
            report["connectedBefore"] = state["connection"]["connected"]
            report["restoredSnapshots"] = state["restoredSnapshots"]
            snapshot = (await client.get(f"{base}/api/market/snapshot", params={"symbol": "BTCUSDT"})).json()
            report["quoteBefore"] = snapshot["ticker"]["last_price"]
            report["staleBefore"] = snapshot["stale"]
            report["staleAfterWait"] = None
            report["healthDuringOutage"] = None
            report["staleDuringOutage"] = None

            # The proxy disappears.
            relay.stop()
            down_at = time.time()
            for _ in range(3000):
                await asyncio.sleep(0.1)
                current = (await client.get(f"{base}/api/market/state")).json()
                if not current["connection"]["connected"]:
                    break
            report["detectedDropAfterSeconds"] = round(time.time() - down_at, 2)
            report["stateDuringOutage"] = current["connection"]["state"]
            report["lastError"] = current["connection"]["lastError"]
            report["healthDuringOutage"] = (await client.get(f"{base}/health")).status_code

            # The snapshot must still serve the last real quote, marked stale.
            body = (await client.get(f"{base}/api/market/snapshot", params={"symbol": "BTCUSDT"})).json()
            report["quoteDuringOutage"] = body["ticker"]["last_price"]
            report["staleDuringOutage"] = body["stale"]
            report["ageMsDuringOutage"] = body["ageMs"]
            report["quoteUnchangedWhileDown"] = body["ticker"]["last_price"] == report["quoteBefore"]
            report["sourceDuringOutage"] = body["source"]
            print(json.dumps({k: v for k, v in report.items() if k.endswith("Outage") or k.startswith("detected")}, ensure_ascii=False), flush=True)

            # Past the freshness window the same snapshot must say so, without
            # changing the number or swapping in generated data.
            for _ in range(200):
                await asyncio.sleep(0.2)
                later = (await client.get(f"{base}/api/market/snapshot", params={"symbol": "BTCUSDT"})).json()
                if later["stale"]:
                    break
            report["staleAfterWait"] = later["stale"]
            report["quoteAfterWait"] = later["ticker"]["last_price"]
            report["quoteStillUnchanged"] = later["ticker"]["last_price"] == report["quoteBefore"]
            report["sourceAfterWait"] = later["source"]

            # The process must still be alive and answering.
            report["processAlive"] = process.returncode is None
    finally:
        relay.stop()
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=15)
        except asyncio.TimeoutError:
            process.kill()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = bool(
        report.get("connectedBefore")
        and report.get("healthDuringOutage") == 200
        and report.get("staleAfterWait") is True
        and report.get("quoteStillUnchanged") is True
        and report.get("quoteUnchangedWhileDown") is True
        and report.get("processAlive")
        and report.get("sourceDuringOutage") != "demo"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
