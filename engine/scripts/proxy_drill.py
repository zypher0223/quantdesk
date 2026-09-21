"""Proxy-loss drill: does the feed come back on its own?

Runs a real Bybit public stream through a local TCP relay in front of the Hong
Kong proxy, kills the relay, then brings it back and measures how long the
service takes to reconnect and resume real quotes. Nothing is faked except the
relay, and no price is invented while the link is down.

    QUANTDESK_UPSTREAM_PROXY=http://127.0.0.1:7893 \
      engine/.venv/bin/python scripts/proxy_drill.py
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantdesk.datahub.market_service import MarketDataService  # noqa: E402

UPSTREAM = os.environ.get("QUANTDESK_UPSTREAM_PROXY", "http://127.0.0.1:7893")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Relay:
    """Minimal TCP forwarder used as a proxy that can be switched off and on."""

    def __init__(self, target_host: str, target_port: int):
        self.target = (target_host, target_port)
        self.port = free_port()
        self.server: socket.socket | None = None
        self.thread: threading.Thread | None = None
        # Live sockets, so stopping the relay behaves like a proxy that dies
        # rather than one that merely stops accepting new connections.
        self.sockets: list[socket.socket] = []
        self.stopped = False

    def start(self) -> None:
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", self.port))
        server.listen(64)
        self.server = server
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _track(self, sock: socket.socket) -> socket.socket:
        self.sockets.append(sock)
        return sock

    def _serve(self) -> None:
        while self.server is not None:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._pipe_pair, args=(client,), daemon=True).start()

    def _pipe_pair(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self.target, timeout=10)
        except OSError:
            client.close()
            return
        if self.stopped:
            # The relay was switched off while this connection was being made.
            for sock in (client, upstream):
                sock.close()
            return
        self._track(client)
        self._track(upstream)
        for source, sink in ((client, upstream), (upstream, client)):
            threading.Thread(target=self._pipe, args=(source, sink), daemon=True).start()

    @staticmethod
    def _pipe(source: socket.socket, sink: socket.socket) -> None:
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def stop(self) -> None:
        self.stopped = True
        if self.server is not None:
            server, self.server = self.server, None
            try:
                server.close()
            except OSError:
                pass
        for sock in list(self.sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.sockets.clear()


async def main() -> int:
    host, _, port = UPSTREAM.replace("http://", "").partition(":")
    relay = Relay(host, int(port or 80))
    relay.start()
    proxy = f"http://127.0.0.1:{relay.port}"
    print(f"relay listening on {proxy} -> {UPSTREAM}", flush=True)

    home = Path(tempfile.mkdtemp(prefix="quantdesk-drill-"))
    service = MarketDataService(
        home,
        symbols=("BTCUSDT", "ETHUSDT"),
        intervals=("15m",),
        backfill=False,
        proxy=proxy,
    )
    report: dict[str, object] = {"proxy": proxy}
    try:
        await service.start()
        for _ in range(400):
            if service.connection_state()["connected"] and service.mark_price("BTCUSDT"):
                break
            await asyncio.sleep(0.05)
        first = service.mark_price("BTCUSDT")
        report["connectedBefore"] = service.connection_state()["connected"]
        report["priceBefore"] = first
        print(f"connected with a real quote: {first}", flush=True)

        # Pull the plug. The quote may still tick for a moment from frames that
        # were already in flight, so the frozen value is the last one observed
        # once the link is actually reported down.
        relay.stop()
        down_at = time.time()
        report["stateAfterKill"] = None
        # Detection is bounded by the silent-socket timeout inside the transport,
        # so this legitimately takes up to ~90s; the process must stay up for all
        # of it and the last real quote must not move.
        for _ in range(2600):
            await asyncio.sleep(0.05)
            if not service.connection_state()["connected"] and all(
                stream["state"] == "reconnecting" for stream in service.connection_state()["streams"]
            ):
                report["stateAfterKill"] = "degraded"
                break
        report["detectedDropAfterSeconds"] = round(time.time() - down_at, 2)
        print(f"drop detected after {report['detectedDropAfterSeconds']}s", flush=True)

        # While the proxy is gone the last real quote must be preserved, not
        # replaced or refreshed. Sample twice: it must not move again.
        frozen = service.mark_price("BTCUSDT")
        report["priceWhileDown"] = frozen
        await asyncio.sleep(5)
        report["priceAfterFiveSecondsDown"] = service.mark_price("BTCUSDT")
        report["preservedQuote"] = frozen is not None and frozen == report["priceAfterFiveSecondsDown"]
        report["frozenIsReal"] = frozen is not None
        print(f"quote frozen while down: {frozen} -> {report['priceAfterFiveSecondsDown']}", flush=True)

        # Bring it back and time the recovery. A new node means a new address, so
        # the service is told about it and rebuilds its streams on the new link.
        back_at = time.time()
        relay = Relay(host, int(port or 80))
        relay.stopped = False
        relay.start()
        await service.set_proxy(f"http://127.0.0.1:{relay.port}")
        recovered_after = None
        for _ in range(1200):
            await asyncio.sleep(0.05)
            state = service.connection_state()
            if state["connected"]:
                age = service.quote_age_ms("BTCUSDT")
                if age is not None and age < 5_000:
                    recovered_after = time.time() - back_at
                    break
        report["recoveredAfterSeconds"] = round(recovered_after, 2) if recovered_after else None
        report["reconnects"] = service.connection_state()["reconnects"]
        report["streamStatesAfterRecovery"] = [stream["state"] for stream in service.connection_state()["streams"]]
        report["priceAfterRecovery"] = service.mark_price("BTCUSDT")
        report["quoteIsRealAfterRecovery"] = service.quote_age_ms("BTCUSDT") is not None and service.quote_age_ms("BTCUSDT") < 5_000
        print(f"recovered after {report['recoveredAfterSeconds']}s, reconnects={report['reconnects']}", flush=True)

        report["ok"] = bool(
            report["connectedBefore"]
            and report["preservedQuote"]
            and report["frozenIsReal"]
            and report["stateAfterKill"] == "degraded"
            and recovered_after is not None
            and recovered_after <= 30
            and report["quoteIsRealAfterRecovery"]
        )
    finally:
        await service.stop()
        relay.stop()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
