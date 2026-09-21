"""One real paid research run through the governed endpoint, with its receipt.

Usage: paid_run.py <SYMBOL> [YYYY-MM-DD] [--force]

Every field printed here is part of the acceptance record: what the run cost,
which data version it read, which analysts reported, and whether the answer was
published as a rating or archived as a degraded run.
"""
import json, sys, time, urllib.error, urllib.request

args = [a for a in sys.argv[1:] if not a.startswith("--")]
force = "--force" in sys.argv
symbol = args[0]
trade_date = args[1] if len(args) > 1 else "2026-09-15"
payload = {"symbol": symbol, "tradeDate": trade_date, "force": force}
request = urllib.request.Request(
    "http://127.0.0.1:4173/api/tradingagents/run",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
)
started = time.time()
try:
    with urllib.request.urlopen(request, timeout=1700) as response:
        body = json.load(response)
except urllib.error.HTTPError as exc:
    body = {"httpError": exc.code, "detail": exc.read().decode()[:2000]}
elapsed = time.time() - started

cost = body.get("cost") or {}
detail = cost.get("detail") or {}
print(f"== {symbol} {trade_date} ==")
print("wall clock:", round(elapsed, 1), "s")
if "httpError" in body:
    print("HTTP", body["httpError"], body["detail"][:800])
    raise SystemExit(1)
print("runId:", body.get("runId"))
print("rating:", body.get("rating"), "| model rating:", body.get("modelRating"))
print("ok:", body.get("ok"), "| degraded:", body.get("degraded"), "| reused:", body.get("reused"))
print("cost $:", cost.get("usd"))
print("usage:", json.dumps(cost.get("usage"), ensure_ascii=False))
print("unpriced models:", json.dumps(detail.get("unpriced"), ensure_ascii=False))
print("analyst coverage:", json.dumps(body.get("analystCoverage"), ensure_ascii=False))
print("data:", json.dumps(body.get("data"), ensure_ascii=False))
print("staleness:", json.dumps(body.get("staleness"), ensure_ascii=False))
print("durationS:", body.get("durationS"), "| retries:", body.get("retries"),
      "| analyst retries:", body.get("analystRetries"))
print("budget:", json.dumps(cost.get("budget"), ensure_ascii=False))
if body.get("failure"):
    print("failure:", json.dumps(body["failure"], ensure_ascii=False))
for warning in body.get("warnings") or []:
    print("  !", warning)
reports = body.get("reports") or {}
print("reports:", {k: len(str(v)) for k, v in reports.items()})
decision = reports.get("final_trade_decision") or ""
if decision:
    print("--- final decision (first 700 chars) ---")
    print(str(decision)[:700])
