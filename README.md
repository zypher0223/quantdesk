# QuantDesk

QuantDesk is a self-hosted research and paper-trading workstation for the fixed Bybit contract universe in this repository. The production image contains the web app, QuantDesk engine, and the real upstream `TradingAgentsGraph` runtime. It does not depend on OpenClaw, a Mac path, or a pre-existing Python environment.

## Deploy on another machine

Requirements: Docker Engine with Docker Compose v2, at least 4 GB RAM, and outbound HTTPS access. TradingAgents is pinned to upstream commit `be952b8eccb49720509af544c6675233bc1f10d0` during the image build.

```bash
cp .env.example .env
# Edit .env locally on the server. Replace QUANTDESK_PASSWORD and fill DEEPSEEK_API_KEY.
docker compose up -d --build
```

Open `http://SERVER_IP:8080` and sign in with `QUANTDESK_USER` / `QUANTDESK_PASSWORD`. Change `QUANTDESK_PORT` in `.env` if that port is occupied. Nginx protects the entire web and API origin with HTTP Basic authentication. For an Internet-facing server, terminate HTTPS in a reverse proxy or access it through a private VPN so the password and research data are encrypted in transit. The API key is injected into the engine container and never bundled into the browser image.

Persistent settings, SQLite records, TradingAgents checkpoints, cache, and reports live in the named Docker volume `quantdesk_data`. They survive image upgrades. Back it up with your normal Docker volume backup process.

Useful checks:

```bash
docker compose ps
docker compose logs -f engine
curl http://127.0.0.1:8080/health
```

The default DeepSeek profile and models remain editable from QuantDesk's settings page. BTC/ETH multi-agent research uses the read-only Hyperliquid daily-market bridge. Every tokenized stock and ETF uses its exact Bybit USDT perpetual for price, volume, and technical analysis; only news and financial tools map to the public reference ticker. SPCX and SKHY participate in the full fundamental analyst flow through their public `SPCX` and `SKHY` identities.

No live order endpoint is included in this deployment.

## External plugins

QuantDesk has a manifest-driven plugin boundary for external repositories. Plugins run as JSON-RPC subprocesses instead of being imported into the engine, are disabled after installation, and receive only the environment variables explicitly listed in their manifest. The settings page can install, update, disable, uninstall, inspect dependencies, and run health checks. Python dependencies live in per-plugin virtual environments and require fixed versions, SHA-256 hashes, and binary wheels. The production image fails closed unless Bubblewrap can isolate filesystem access, writes, and network. See [PLUGIN_API.md](PLUGIN_API.md) and the runnable [plugin template](examples/quantdesk-plugin-template).

The business protocol now covers normalized candles, strategy discovery and bulk signal generation, cited research evidence, factor research, backtest validation, and notifications. Enabled strategy plugins appear in the same backtest selector as the three built-in strategies. `plugins/vibe-factors` is a shipped v3 reference adapter: 28 whitelisted time-series factors plus a statistical validator (moving-block bootstrap, signal randomization, Deflated Sharpe, CSCV/PBO). Install and enable it with `quantdesk plugins install ./plugins/vibe-factors && quantdesk plugins enable vibe-factors`; the result centre then offers 统计验证 on any finished run, and the factor panel computes the catalogue over stored history.

## Background services

The engine process starts three persistent services without requiring an open browser:

- The market collector rotates through the fixed 17-contract universe and stores closed `15m`, `1h`, `4h`, and `1d` candles plus funding and open interest in SQLite.
- The TradingAgents queue persists long-running jobs across browser disconnects and requeues an interrupted running job after a service restart. It uses one worker to avoid overlapping paid multi-agent runs.
- The backtest run queue executes formal studies in their own process, so a parameter search cannot stall the API. Submitting a run writes a row and returns; progress, the result, its artifacts and its validation verdicts are stored, and a run interrupted by a restart is requeued from its stored request. A study too large for an interactive request is refused by the synchronous endpoint and pointed at the queue.
- The historical-data queue walks trade candles, mark candles, funding, open interest, and risk tiers in the background. Its pause, resume, retry, cancellation, page frontier, and failure state survive browser and service restarts. Retryable failures (network, timeout, rate limit, upstream 5xx) wait 5s, then 20s, and consume one of three attempts; a series that exhausts them stops as `failed` with its classified reason, ready to be retried from the panel. The history page reports, per contract and data family, the window a formal study may actually use, whether that window is gap-free, and whether the walk has reached the oldest data the venue publishes.

Every backtest result carries its execution model: the bar a signal was read from, the latency to the fill, whether an order larger than the participation cap was trimmed or filled anyway, and the simplifications that remain (exits fill in one piece; queue position and book depth are not modelled). A study may set `latencyBars` and `partialFill`, and the defaults reproduce the historical behaviour exactly.

The settings page controls collection interval, cache depth, and the optional daily TradingAgents schedule. Daily paid model runs are disabled by default. Completion and failure events are delivered to enabled notifier plugins.

## Keep QuantDesk running on macOS

For this Mac, QuantDesk can run as one login service on port `4173`. FastAPI serves the production web build and all API routes from the same process, so the browser may be closed while market collection, paper risk checks, TradingAgents jobs, and alert evaluation continue. The service starts at login and `launchd` restarts it after an unexpected exit.

```bash
chmod +x scripts/macos-service.sh
./scripts/macos-service.sh install
./scripts/macos-service.sh status
```

Open `http://127.0.0.1:4173/`. Operational commands:

```bash
./scripts/macos-service.sh restart
./scripts/macos-service.sh logs
./scripts/macos-service.sh uninstall
```

The launch configuration contains paths only. LLM keys remain in `~/.quantdesk/llm.keys`; application data and logs remain under `~/.quantdesk`. Installing a new frontend version requires running `install` again so the production bundle is rebuilt.

## Risk model and venue limits

Paper trading and backtests margin a position the way the venue does, from the contract's own risk ladder rather than one shared constant:

```bash
# Ladder and mark-price series, cached in SQLite (17 contracts, 30-35 rungs each)
engine/.venv/bin/python -m quantdesk.cli fetch risk --marks 1h,4h --mark-bars 600
```

- Maintenance margin is `notional x rate - deduction` of the rung the position sits in, so a size that grows into a stricter rung is margined there.
- Liquidation solves for the price where remaining margin equals that requirement. A 1x long cannot be liquidated by price; a 1x short can. The old leverage-only approximation claimed otherwise for both.
- Liquidation, valuation and funding use the venue's own mark-price bars (`/v5/market/mark-price-kline`) when they are stored, and the backtest says so when it had to fall back to the bar close.
- Funding settles at the venue's own settlement timestamps from `/v5/market/funding/history`, priced off the mark at that moment, instead of every 8 hours from the first bar.
- Leverage is capped per contract and per rung (BTCUSDT allows 150x at its smallest notional, SOXLUSDT 100x, AMDSTOCKUSDT 50x). A request above the cap is refused with the rung that refused it.
- `slippageModel: "participation"` adds an impact term proportional to the share of the bar's traded notional the order takes, on top of the configured spread.
- Without a cached ladder the run still works, but every result carries a warning naming the constant rate it fell back to.

The five-contract comparison the acceptance asks for runs offline against these ladders in `engine/tests/test_risk.py::FiveContractComparisonTests`, and against live venue data through `POST /api/backtest` per symbol.

## Strategy validation

A backtest says what a rule would have returned; it does not say whether the rule was fitted to the sample. `validate` adds the second half:

```bash
# Split by time, search the grid on train, decide on validation, score test once
engine/.venv/bin/python -m quantdesk.cli validate AAPL --interval 1h --bars 1500 \
  --fast-grid 5,9,20 --slow-grid 21,50,100 --windows 4
```

- **Segments**: consecutive train / validation / test cuts, never shuffled. Later segments carry warmup bars in front so indicators are warm at their start, and those bars are read but never scored.
- **Walk-forward**: anchored windows that each train on more history than the last and select parameters on the training part only. The report names how many distinct parameter sets were chosen and how many windows lost money.
- **Parameter search**: the grid is a cross product, so pairs the strategy rejects are recorded as skipped instead of aborting the search. The winner is chosen on the validation segment and the test segment is scored once, afterwards.
- **Overfitting warnings**: in-sample versus out-of-sample degradation, a winner that is positive only in sample, a result that is far above the grid median, and any segment with too few trades to mean anything. Comparisons use interval return when a segment is shorter than 29 days, because annualising a two-month window multiplies it by six; the annualised figure is still shown, next to a warning that it is an extrapolation.
- **Leakage checks**: signals are regenerated on truncated history and compared bar by bar, so a rule that reads the future is caught; a sample whose last bar had not closed is reported too.
- **Portfolio**: `POST /api/portfolio` runs one strategy across several contracts, scales each leg onto its capital weight, and reports the combined book plus a same-weights buy-and-hold benchmark.
- **Provenance**: every result carries a data fingerprint (sha256 over the bars), strategy id, parameters, cost model, risk source, engine version and run time. `resolution_check` re-verifies a stored result against the data on hand.
- **Metrics**: total and annualised return, benchmark and excess return, max drawdown and its duration, volatility, Sharpe, Sortino, Calmar, win rate, profit factor, payoff ratio, expectancy, exposure, longest losing streak and liquidation count. Each figure states the window it was computed over.

`POST /api/validate` exposes the same report to the browser.

## Alert rules

The settings page can persist up to five AND conditions for closing price, true price crossing, funding rate, 24-hour open-interest change, volume ratio, and multi-timeframe resonance. Policies include consecutive confirmation, hysteresis rearming, severity, quiet hours, cooldown, and a local-time daily limit. Rules are evaluated automatically after each successful background collection and can also be checked manually. A completed backtest can register its exact strategy and parameters as a new-signal alert.

Only closed candles and stored derivative observations are used. The data-quality gate blocks automated evaluation when a required timeframe is stale, malformed, or has a recent gap. A rule cannot trigger twice on the same observation. Every trigger is written to SQLite before enabled notifier plugins are called, so the audit history remains available even when no notifier is installed or a delivery fails.

## Operations and data quality

The **运行监控** workspace audits all 17 contracts across `15m`, `1h`, `4h`, and `1d`. It reports freshness, missing bars, schema-invalid OHLCV rows, extreme price/volume observations, and whether each symbol is eligible to produce automated signals. Weekend slots are excluded for stock-class contracts. SQLite primary keys prevent duplicate candles and the dashboard states the resulting duplicate count.

The background Bybit collector records each request attempt, status code, latency, retry number, and rate-limit result. HTTP 429, 5xx, timeout, and network failures receive bounded retries. The same page shows market scheduler, alert engine, TradingAgents worker, and paper-risk monitor status and refreshes while it is open.
