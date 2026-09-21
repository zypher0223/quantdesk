#!/usr/bin/env python3
"""QuantDesk v1 strategy plugin example using only the standard library."""

from __future__ import annotations

import json
import sys


def respond(request_id, *, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = {"code": -32000, "message": error}
    else:
        payload["result"] = result or {}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main():
    request = json.loads(sys.stdin.readline())
    method = request.get("method")
    params = request.get("params") or {}
    if method == "health":
        respond(request.get("id"), result={"ok": True, "message": "example strategy ready"})
        return
    if method == "strategy.describe":
        respond(request.get("id"), result={"strategies": [{
            "id": "momentum-cross",
            "name": "示例动量交叉",
            "description": "收盘价上穿或下穿简单均线时发出方向事件。",
            "parameters": [
                {"key": "period", "label": "均线周期", "type": "integer", "default": 20, "minimum": 3, "maximum": 200},
                {"key": "enabled", "label": "启用信号", "type": "boolean", "default": True},
                {"key": "mode", "label": "交易模式", "type": "select", "default": "both", "options": ["both", "long_only"]},
            ],
        }]})
        return
    if method == "strategy.generate":
        candles = params.get("candles") or []
        options = params.get("parameters") or {}
        period = max(3, min(int(options.get("period", 20)), 200))
        enabled = bool(options.get("enabled", True))
        mode = str(options.get("mode", "both"))
        signals = []
        closes = [float(item["close"]) for item in candles]
        if enabled:
            for index in range(period, len(candles)):
                previous_average = sum(closes[index - period:index]) / period
                current_average = sum(closes[index - period + 1:index + 1]) / period
                previous, current = closes[index - 1], closes[index]
                direction = None
                if previous <= previous_average and current > current_average:
                    direction = "long"
                elif mode == "both" and previous >= previous_average and current < current_average:
                    direction = "short"
                if direction:
                    signals.append({
                        "time": int(candles[index]["time"]),
                        "direction": direction,
                        "strength": 0.5,
                        "reason": f"close/MA{period} cross",
                    })
        respond(request.get("id"), result={"signals": signals, "warnings": []})
        return
    respond(request.get("id"), error=f"unsupported method: {method}")


if __name__ == "__main__":
    main()
