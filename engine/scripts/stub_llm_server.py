#!/usr/bin/env python3
"""A stub OpenAI-compatible endpoint for exercising the vision path offline.

The screenshot flow needs a provider that accepts an image and returns JSON.
This server answers both, so the whole chain — gateway route, vision routing
guard, prompt assembly, persistence — can be verified without a real API key or
network access.

Usage:
    python scripts/stub_llm_server.py --port 8791
    # then point a profile at it:
    #   [profiles.stubvision]
    #   provider = "openai_compatible"
    #   base_url = "http://127.0.0.1:8791/v1"
    #   api_key_env = "STUB_KEY"
    #   vision_model = "stub-vision-1"
    #   supports_vision = true
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

# Chart-vision answer.
STRUCTURED = {
    "instrument": {"visible": True, "value": "AMDUSDT"},
    "timeframe": {"visible": True, "value": "1h"},
    "trend": "震荡偏多",
    "structure": ["价格在 500 上方做箱体", "最近三根收在上沿附近"],
    "levels": [
        {"price": "501.87", "kind": "支撑", "note": "前低"},
        {"price": "521.61", "kind": "阻力", "note": "24h 高点"},
    ],
    "patterns": ["箱体"],
    "indicators_visible": ["EMA", "成交量"],
    "uncertain": ["无法确认精确成交量数值"],
    "confidence": "medium",
}

# Research answer. Levels are expressed as fractions of the live mark price so
# the stub stays coherent against whatever the engine actually fetched.
def research_answer(payload: dict) -> dict:
    structure = payload.get("price_structure") or {}
    entry = float(structure.get("last_close") or payload.get("mark_price") or 100.0)
    atr = float(structure.get("atr") or entry * 0.01)
    stop = round(entry - 2 * atr, 4)
    tp1 = round(entry + 2 * atr, 4)
    tp2 = round(entry + 4 * atr, 4)
    return {
        "headline": "4h 偏多、1h 转弱，等回踩确认",
        "confidence": "medium",
        "facts": [
            {"statement": "4h 立场为 bull，1h 立场为 bear", "evidence": ["4h.stance", "1h.stance"]},
            {"statement": f"ATR 为 {round(atr, 4)}，区间上沿为 {structure.get('recent_high')}", "evidence": ["price_structure.atr", "price_structure.recent_high"]},
        ],
        "inferences": [
            {"statement": "高周期偏多但低周期回落，属于回踩而非破位", "basis": ["4h.stance", "1h.stance"], "confidence": "medium"}
        ],
        "trading_plan": {
            "direction": "long",
            "entry": round(entry, 4),
            "entry_zone": [round(entry - 0.5 * atr, 4), round(entry + 0.5 * atr, 4)],
            "stop_loss": stop,
            "take_profit_1": tp1,
            "take_profit_2": tp2,
            "risk_reward": 1.0,
            "position_size_pct": 10,
            "timeframe": "1h",
            "rationale": "止损置于两倍 ATR 之外，依据 price_structure.atr",
            "trigger": None,
            "valid_until": "本根K线收盘前",
        },
        "scenarios": [
            {"name": "回踩守住", "condition": f"收盘不低于 {round(entry - atr, 4)}", "implication": "结构维持，向区间上沿运行"}
        ],
        "invalidation": ["1d 立场转为 bear", "收盘落于计划失效价位之下"],
        # Derived from the bundle so the answer stays true for 7x24 crypto too,
        # where the daily timeframe does have enough bars.
        "missing_evidence": list(payload.get("missing") or []),
        "data_caveats": [
            f"{entry['interval']} 仅有 {entry['bars']} 根已收盘K线，不足 {entry['required']} 根，未参与评分"
            for entry in (payload.get("resonance") or {}).get("unavailable") or []
        ],
    }


def _extract_bundle(text: str) -> dict:
    """Pull the evidence bundle out of the user turn.

    The bundle is pretty-printed JSON followed by an optional
    "用户额外关注：..." line. It must be located by its own anchor: the system
    prompt also contains a JSON template, and its first brace would otherwise be
    mistaken for the payload.
    """
    anchor = text.find("证据包，请按系统要求输出 JSON")
    start = text.find("{", anchor if anchor >= 0 else 0)
    if start < 0:
        return {}
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


class Handler(BaseHTTPRequestHandler):
    verbose = False

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            body = {}
        images = sum(
            1
            for message in body.get("messages", [])
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image_url"
        )
        # The messages carry the whole evidence bundle; keep the raw newlines so
        # the JSON stays parseable.
        prompt = "\n".join(
            str(message.get("content", "")) for message in body.get("messages", []) if isinstance(message.get("content"), str)
        )
        if Handler.verbose:
            print(f"[stub] prompt chars={len(prompt)} braces={prompt.count('{')} has_plan={'trading_plan' in prompt}", flush=True)
        if "trading_plan" in prompt:
            # Research request: answer with a plan derived from the evidence.
            bundle = _extract_bundle(prompt)
            if Handler.verbose:
                structure = bundle.get("price_structure") or {}
                first = prompt.find("{")
                print(f"[stub] bundle_keys={len(bundle)} structure_atr={structure.get('atr')} first_brace_ctx={prompt[max(0,first-30):first+20]!r}", flush=True)
            answer = research_answer(bundle)
        else:
            answer = STRUCTURED
        payload = {
            "id": "stub-completion",
            "object": "chat.completion",
            "model": body.get("model", "stub"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        if self.verbose:
            print(f"[stub] model={body.get('model')} images={images}", flush=True)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            raw = json.dumps({"object": "list", "data": [{"id": "stub-vision-1", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args) -> None:  # keep the console usable
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    Handler.verbose = args.verbose
    print(f"stub LLM endpoint on http://127.0.0.1:{args.port}/v1 (POST /v1/chat/completions)", flush=True)
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
