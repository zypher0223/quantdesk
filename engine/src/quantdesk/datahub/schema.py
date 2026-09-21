"""The database schema and its ordered migrations.

Kept apart from the store's behaviour: `db.py` is about reading and writing
candles, runs and reports, while this module is about the shape of the file and
how an older one is brought forward.

Two layers of change live here:

* `SCHEMA` - the current shape, applied with `CREATE TABLE IF NOT EXISTS` on every
  open, which is what makes a fresh install and an upgraded one converge;
* `MIGRATIONS` - ordered steps keyed by `PRAGMA user_version`. A step that has run
  on a database is remembered there, so a future change can be added as a new step
  instead of another conditional ALTER scattered through `Database.__init__`.

The migrations are idempotent on purpose: a database written before this framework
existed has `user_version = 0` and no record of which ad-hoc ALTERs already ran,
so the first step must be safe to apply to a database that is already current.
"""

from __future__ import annotations

import json
import sqlite3
import zlib

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    venue       TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    open_ts     INTEGER NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL DEFAULT 0,
    trades      INTEGER,
    -- Provenance: which path wrote the bar, when the venue stamped it, when this
    -- process received it, and which collector version wrote it. A bar that a
    -- backtest cannot attribute is a bar it cannot defend.
    source      TEXT,
    -- How the bar arrived, kept apart from where it came from: `source` answers
    -- "which venue produced this price", `ingestion_mode` answers "was it a live
    -- frame, a history walk, a repair, or an operator import". Conflating the two
    -- is how a backfill ends up looking like an unknown source.
    ingestion_mode TEXT,
    exchange_ts INTEGER,
    received_ts INTEGER,
    collector   TEXT,
    PRIMARY KEY (venue, symbol, interval, open_ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS funding (
    venue   TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    ts      INTEGER NOT NULL,
    rate    REAL NOT NULL,
    PRIMARY KEY (venue, symbol, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS open_interest (
    venue  TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL DEFAULT '1h',
    ts     INTEGER NOT NULL,
    oi     REAL NOT NULL,
    PRIMARY KEY (venue, symbol, interval, ts)
) WITHOUT ROWID;

-- Latest derivatives snapshot per contract. One row per symbol so the page can
-- render the last real quote immediately after a restart, without waiting for a
-- venue round trip, and so a stale snapshot is detectable by age rather than
-- being silently replaced by generated data.
CREATE TABLE IF NOT EXISTS market_snapshots (
    venue                 TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    last_price            REAL,
    mark_price            REAL,
    index_price           REAL,
    funding_rate          REAL,
    funding_interval_hour REAL,
    next_funding_time     INTEGER,
    open_interest         REAL,
    open_interest_value   REAL,
    turnover_24h          REAL,
    volume_24h            REAL,
    price_24h_pct         REAL,
    high_24h              REAL,
    low_24h               REAL,
    exchange_ts           INTEGER,
    received_ts           INTEGER NOT NULL,
    source                TEXT NOT NULL,          -- websocket / rest_backfill
    PRIMARY KEY (venue, symbol)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS risk_tiers (
    venue                   TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    tier_id                 INTEGER NOT NULL,
    risk_limit_value        REAL NOT NULL,        -- notional ceiling of this rung
    maintenance_margin_rate REAL NOT NULL,
    mm_deduction            REAL NOT NULL DEFAULT 0,
    max_leverage            REAL NOT NULL,
    initial_margin_rate     REAL,
    lowest_risk             INTEGER NOT NULL DEFAULT 0,
    source                  TEXT NOT NULL DEFAULT 'bybit:risk-limit',
    synced_at               INTEGER NOT NULL,
    PRIMARY KEY (venue, symbol, tier_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS mark_candles (
    venue      TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    interval   TEXT NOT NULL,
    open_ts    INTEGER NOT NULL,                  -- venue mark-price bar open time
    open       REAL NOT NULL,
    high       REAL NOT NULL,
    low        REAL NOT NULL,
    close      REAL NOT NULL,
    received_ts INTEGER NOT NULL,
    source     TEXT NOT NULL,                     -- rest_mark / ws_mark
    PRIMARY KEY (venue, symbol, interval, open_ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    strategy   TEXT NOT NULL,
    venue      TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    direction  TEXT NOT NULL,            -- long / short / flat
    strength   REAL,
    reason     TEXT,
    sl_price   REAL,
    tp_price   REAL,
    payload    TEXT                      -- full feature snapshot JSON
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts  INTEGER NOT NULL,
    venue       TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,            -- buy / sell
    qty         REAL NOT NULL,
    order_type  TEXT NOT NULL DEFAULT 'market',
    status      TEXT NOT NULL,            -- pending / filled / canceled / rejected
    signal_id   INTEGER,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    price      REAL NOT NULL,
    qty        REAL NOT NULL,
    fee        REAL NOT NULL DEFAULT 0,
    slippage   REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    venue      TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,             -- long / short
    qty        REAL NOT NULL,
    avg_price  REAL NOT NULL,
    leverage   REAL NOT NULL DEFAULT 1,
    liq_price  REAL,
    entry_fee  REAL NOT NULL DEFAULT 0,
    funding_paid REAL NOT NULL DEFAULT 0,
    updated_ts INTEGER NOT NULL,
    closed_ts  INTEGER
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id   INTEGER NOT NULL,
    order_type    TEXT NOT NULL,          -- stop_loss / take_profit_1 / take_profit_2
    trigger_price REAL NOT NULL,
    close_fraction REAL NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'open', -- open / filled / canceled
    created_ts    INTEGER NOT NULL,
    triggered_ts  INTEGER,
    FOREIGN KEY (position_id) REFERENCES positions(id)
);
CREATE INDEX IF NOT EXISTS paper_orders_position_idx ON paper_orders (position_id, status);

CREATE TABLE IF NOT EXISTS paper_funding_settlements (
    position_id INTEGER NOT NULL,
    funding_ts  INTEGER NOT NULL,
    rate        REAL NOT NULL,
    mark_price  REAL NOT NULL,
    cost        REAL NOT NULL,
    PRIMARY KEY (position_id, funding_ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS equity_points (
    ts     INTEGER NOT NULL,
    equity REAL NOT NULL,
    cash   REAL NOT NULL,
    PRIMARY KEY (ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ta_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    asset_type  TEXT NOT NULL,
    rating      TEXT,
    ok          INTEGER NOT NULL,
    duration_s  REAL,
    reports     TEXT,                     -- JSON from wrapper --full-state
    meta        TEXT,                     -- JSON: provider/models/duration
    created_ts  INTEGER NOT NULL,
    UNIQUE (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS tradingagents_costs (
    run_id            TEXT PRIMARY KEY,
    venue_symbol      TEXT NOT NULL,
    trade_date        TEXT NOT NULL,
    profile           TEXT NOT NULL,
    provider          TEXT NOT NULL,
    deep_model        TEXT,
    quick_model       TEXT,
    -- Everything that decides whether two runs are the same run.
    config_fingerprint TEXT NOT NULL,
    prompt_version    TEXT,
    data_version      TEXT,
    data_as_of        TEXT,
    analysts          TEXT,                       -- JSON list
    missing_analysts  TEXT,                       -- JSON list: requested but absent
    reuse_key         TEXT NOT NULL,
    reused_from       TEXT,                       -- run id this was served from
    ok                INTEGER NOT NULL,
    rating            TEXT,
    staleness         TEXT,                       -- JSON: stale flag and reason
    usage             TEXT,                       -- JSON: tokens per model
    -- 1 when a model was actually called, so a run that failed before spending
    -- anything cannot be mistaken for an unpriced run.
    usage_known       INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL,
    cost_detail       TEXT,                       -- JSON: per-model rates and cost
    duration_s        REAL,
    retries           INTEGER NOT NULL DEFAULT 0,
    -- passes repeated because a requested analyst returned no report
    analyst_retries   INTEGER NOT NULL DEFAULT 0,
    failure           TEXT,                       -- JSON: reason and type when failed
    created_ts        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tradingagents_costs_created_idx ON tradingagents_costs (created_ts DESC);
CREATE INDEX IF NOT EXISTS tradingagents_costs_reuse_idx ON tradingagents_costs (reuse_key, created_ts DESC);

CREATE TABLE IF NOT EXISTS tradingagents_runs (
    id              TEXT PRIMARY KEY,
    venue_symbol    TEXT NOT NULL,
    analysis_symbol TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    asset_type      TEXT NOT NULL,
    profile         TEXT NOT NULL,
    rating          TEXT,
    reports         TEXT,
    debates         TEXT,
    meta            TEXT,
    error           TEXT,
    created_ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tradingagents_runs_created_idx ON tradingagents_runs (created_ts DESC);

CREATE TABLE IF NOT EXISTS tradingagents_jobs (
    id              TEXT PRIMARY KEY,
    venue_symbol    TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    analysts        TEXT,
    profile         TEXT,
    status          TEXT NOT NULL,
    progress        TEXT,
    result          TEXT,
    error           TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_ts      INTEGER NOT NULL,
    started_ts      INTEGER,
    finished_ts     INTEGER
);
CREATE INDEX IF NOT EXISTS tradingagents_jobs_status_idx ON tradingagents_jobs (status, created_ts);

CREATE TABLE IF NOT EXISTS scheduler_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,
    subject     TEXT,
    status      TEXT NOT NULL,
    summary     TEXT,
    error       TEXT,
    started_ts  INTEGER NOT NULL,
    finished_ts INTEGER
);
CREATE INDEX IF NOT EXISTS scheduler_runs_started_idx ON scheduler_runs (started_ts DESC);

CREATE TABLE IF NOT EXISTS market_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    operation   TEXT NOT NULL,
    symbol      TEXT,
    status      TEXT NOT NULL,
    http_status INTEGER,
    attempt     INTEGER NOT NULL DEFAULT 1,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_ts  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS market_requests_created_idx
ON market_requests (created_ts DESC);

CREATE TABLE IF NOT EXISTS alert_rules (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    venue_symbol        TEXT NOT NULL,
    condition_type      TEXT NOT NULL,
    timeframe           TEXT,
    threshold           REAL NOT NULL,
    cooldown_seconds    INTEGER NOT NULL DEFAULT 3600,
    enabled             INTEGER NOT NULL DEFAULT 1,
    last_condition      INTEGER NOT NULL DEFAULT 0,
    last_metric         REAL,
    last_observed_ts    INTEGER,
    last_evaluated_ts   INTEGER,
    last_triggered_ts   INTEGER,
    severity            TEXT NOT NULL DEFAULT 'warning',
    quiet_start         TEXT,
    quiet_end           TEXT,
    timezone            TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    daily_limit         INTEGER NOT NULL DEFAULT 10,
    confirmation_count  INTEGER NOT NULL DEFAULT 1,
    consecutive_count   INTEGER NOT NULL DEFAULT 0,
    hysteresis          REAL NOT NULL DEFAULT 0,
    armed               INTEGER NOT NULL DEFAULT 1,
    last_observation_key TEXT,
    created_ts          INTEGER NOT NULL,
    updated_ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_rules_symbol_idx
ON alert_rules (venue_symbol, enabled);

CREATE TABLE IF NOT EXISTS alert_rule_conditions (
    id                  TEXT PRIMARY KEY,
    rule_id             TEXT NOT NULL,
    position            INTEGER NOT NULL,
    condition_type      TEXT NOT NULL,
    timeframe           TEXT,
    threshold           REAL,
    strategy_id         TEXT,
    strategy_parameters TEXT,
    signal_direction    TEXT,
    last_metric         REAL,
    last_observed_ts    INTEGER,
    last_met            INTEGER NOT NULL DEFAULT 0,
    UNIQUE (rule_id, position)
);
CREATE INDEX IF NOT EXISTS alert_rule_conditions_rule_idx
ON alert_rule_conditions (rule_id, position);

CREATE TABLE IF NOT EXISTS alert_events (
    id                   TEXT PRIMARY KEY,
    rule_id              TEXT NOT NULL,
    venue_symbol         TEXT NOT NULL,
    condition_type       TEXT NOT NULL,
    timeframe            TEXT,
    metric               REAL NOT NULL,
    threshold            REAL NOT NULL,
    observed_ts          INTEGER NOT NULL,
    triggered_ts         INTEGER NOT NULL,
    title                TEXT NOT NULL,
    message              TEXT NOT NULL,
    notification_results TEXT
);
CREATE INDEX IF NOT EXISTS alert_events_triggered_idx
ON alert_events (triggered_ts DESC);

CREATE TABLE IF NOT EXISTS chart_analyses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts INTEGER NOT NULL,
    file_name  TEXT,
    image_path TEXT,
    model      TEXT,
    profile    TEXT,
    result_md  TEXT,
    payload    TEXT
);

-- Trade rationale lives here rather than on the journal row: notes stay
-- editable while the resulting journal entry does not.
CREATE TABLE IF NOT EXISTS position_notes (
    position_id INTEGER PRIMARY KEY,
    notes       TEXT NOT NULL DEFAULT '',
    updated_ts  INTEGER NOT NULL
);

-- Append-only audit trail. Triggers reject edits and deletions even from code
-- that forgets the convention, so "immutable" is enforced, not just documented.
CREATE TABLE IF NOT EXISTS journal (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id  INTEGER,
    opened_ts    INTEGER NOT NULL,
    closed_ts    INTEGER NOT NULL,
    venue        TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,               -- long / short
    qty          REAL NOT NULL,
    entry_price  REAL NOT NULL,
    exit_price   REAL NOT NULL,
    leverage     REAL NOT NULL DEFAULT 1,
    liq_price    REAL,
    gross_pnl    REAL NOT NULL,
    funding_paid REAL NOT NULL DEFAULT 0,
    fees         REAL NOT NULL DEFAULT 0,
    net_pnl      REAL NOT NULL,
    exit_reason  TEXT NOT NULL DEFAULT 'manual',
    rationale    TEXT,
    tags         TEXT,
    signal_id    INTEGER,
    report_id    INTEGER,
    source       TEXT NOT NULL DEFAULT 'paper',
    entry_hash   TEXT
);
CREATE INDEX IF NOT EXISTS journal_closed_idx ON journal (closed_ts DESC);
CREATE INDEX IF NOT EXISTS journal_symbol_idx ON journal (symbol, closed_ts DESC);

CREATE TRIGGER IF NOT EXISTS journal_no_update
BEFORE UPDATE ON journal
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only: entries cannot be modified');
END;

CREATE TRIGGER IF NOT EXISTS journal_no_delete
BEFORE DELETE ON journal
BEGIN
    SELECT RAISE(ABORT, 'journal is append-only: entries cannot be deleted');
END;

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;

-- AI paper trading owns its configuration and decision audit trail in the main
-- database.  Actual positions and its immutable trade journal live in a
-- separate ai-paper-<profile>.db file, which prevents an autonomous simulation
-- from ever sharing cash or positions with the operator's manual paper account.
CREATE TABLE IF NOT EXISTS ai_paper_profiles (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '主模拟',
    enabled         INTEGER NOT NULL DEFAULT 0,
    initial_cash    REAL NOT NULL DEFAULT 100000,
    max_leverage    REAL NOT NULL DEFAULT 3,
    horizon         TEXT NOT NULL DEFAULT 'short',
    style           TEXT NOT NULL DEFAULT 'conservative',
    fib_only        INTEGER NOT NULL DEFAULT 0,
    symbols_json    TEXT NOT NULL DEFAULT '[]',
    model_role      TEXT NOT NULL DEFAULT 'ai_paper_trader',
    -- Monotonic config revision. Every decision and position-condition snapshot cites
    -- the revision it ran under, so "which rules produced this call?" is answerable
    -- after the fact.
    config_revision INTEGER NOT NULL DEFAULT 1,
    last_cycle_ts   INTEGER,
    last_bar_ts     INTEGER,
    last_error      TEXT,
    created_ts      INTEGER NOT NULL,
    updated_ts      INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS ai_paper_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      TEXT NOT NULL,
    cycle_ts        INTEGER NOT NULL,
    model_profile   TEXT,
    model           TEXT,
    action          TEXT NOT NULL,
    symbol          TEXT,
    side            TEXT,
    leverage        REAL,
    notional        REAL,
    stop_loss       REAL,
    take_profit_1   REAL,
    take_profit_2   REAL,
    confidence      REAL,
    reason          TEXT NOT NULL DEFAULT '',
    lesson_applied  TEXT NOT NULL DEFAULT '',
    evidence_json   TEXT NOT NULL DEFAULT '{}',
    raw_response    TEXT,
    status          TEXT NOT NULL,
    error           TEXT,
    position_id     INTEGER,
    created_ts      INTEGER NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES ai_paper_profiles(id)
);
CREATE INDEX IF NOT EXISTS ai_paper_decisions_profile_idx
    ON ai_paper_decisions (profile_id, cycle_ts DESC, id DESC);

-- One model evaluation per profile at a time, across threads and processes.
-- A lease has an expiry so a killed worker cannot permanently block the account.
CREATE TABLE IF NOT EXISTS ai_paper_leases (
    profile_id      TEXT PRIMARY KEY,
    owner           TEXT NOT NULL,
    acquired_ts     INTEGER NOT NULL,
    expires_ts      INTEGER NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES ai_paper_profiles(id)
) WITHOUT ROWID;

-- Where a history backfill got to. Written after every page so an interrupted
-- walk resumes from its own frontier instead of starting over, and so a failure
-- leaves a classified reason behind rather than only a missing range.
CREATE TABLE IF NOT EXISTS backfill_state (
    venue          TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    interval       TEXT NOT NULL DEFAULT '',      -- '' for kinds without a timeframe
    data_kind      TEXT NOT NULL DEFAULT 'trade_candle',
    oldest_ts      INTEGER,                       -- earliest row reached so far
    newest_ts      INTEGER,                       -- newest row reached so far
    complete       INTEGER NOT NULL DEFAULT 0,    -- walked back to the listing
    pages          INTEGER NOT NULL DEFAULT 0,
    rows_available INTEGER NOT NULL DEFAULT 0,    -- distinct stored rows
    attempts       INTEGER NOT NULL DEFAULT 0,
    -- ok | unsupported. `unsupported` means the venue does not publish this data
    -- for this contract; it is never represented by zero values.
    status         TEXT NOT NULL DEFAULT 'ok',
    reason         TEXT,
    last_error     TEXT,
    last_error_kind TEXT,                         -- rate_limited / timeout / no_data / …
    last_run_ts    INTEGER,
    updated_ts     INTEGER NOT NULL,
    PRIMARY KEY (venue, symbol, interval, data_kind)
) WITHOUT ROWID;

-- One queued unit of backfill work: a single series of a single contract. The
-- matrix is 17 contracts x 4 timeframes of candles plus the symbol-level series,
-- and every unit is independently retryable, pausable and reportable.
CREATE TABLE IF NOT EXISTS backfill_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    venue           TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    interval        TEXT NOT NULL DEFAULT '',
    data_kind       TEXT NOT NULL,
    -- pending | running | paused | done | failed | unsupported | cancelled
    status          TEXT NOT NULL DEFAULT 'pending',
    pages           INTEGER NOT NULL DEFAULT 0,
    pages_estimate  INTEGER NOT NULL DEFAULT 0,
    rows_available  INTEGER NOT NULL DEFAULT 0,
    -- Whether the walk reached the end of the series it was filling. Progress is
    -- computed from this, never from a countdown that can run out early.
    complete        INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    failure_attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    last_error      TEXT,
    last_error_kind TEXT,
    reason          TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_ts      INTEGER NOT NULL,
    started_ts      INTEGER,
    finished_ts     INTEGER,
    updated_ts      INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS backfill_tasks_unit_idx
    ON backfill_tasks (venue, symbol, interval, data_kind);
CREATE INDEX IF NOT EXISTS backfill_tasks_status_idx
    ON backfill_tasks (status, id);

-- What the venue says a contract is: its listing date, its tick and step, and
-- whether the data families below exist for it at all.
CREATE TABLE IF NOT EXISTS instrument_meta (
    venue          TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    display_symbol TEXT,
    contract_type  TEXT,
    status         TEXT,
    launch_ts      INTEGER,                       -- when trading started
    tick_size      REAL,
    qty_step       REAL,
    min_notional   REAL,
    funding_interval_hours REAL,                  -- NULL when the venue has none
    raw_json       TEXT,
    collected_ts   INTEGER NOT NULL,
    PRIMARY KEY (venue, symbol)
) WITHOUT ROWID;

-- A reproducible data snapshot: the version hash and coverage of the history a
-- backtest may pin itself to.
CREATE TABLE IF NOT EXISTS history_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    venue              TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    interval           TEXT NOT NULL DEFAULT '',
    data_kind          TEXT NOT NULL DEFAULT 'trade_candle',
    version            TEXT NOT NULL,
    from_ts            INTEGER NOT NULL,
    to_ts              INTEGER NOT NULL,
    bars               INTEGER NOT NULL,
    bars_available     INTEGER NOT NULL DEFAULT 0,
    complete           INTEGER NOT NULL DEFAULT 0,
    missing_in_session INTEGER NOT NULL DEFAULT 0,
    sources            TEXT,
    created_ts         INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS history_snapshots_version_idx
    ON history_snapshots (venue, symbol, interval, data_kind, version);
CREATE INDEX IF NOT EXISTS history_snapshots_symbol_idx
    ON history_snapshots (symbol, data_kind, interval, created_ts DESC);

-- External research readings (OpenBB and anything else that answers
-- research.collect). One row per reading, keyed by the cache key below, with the
-- published/observed times a point-in-time check needs and a content hash so an
-- unchanged document is reused rather than refetched.
CREATE TABLE IF NOT EXISTS external_evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key       TEXT NOT NULL UNIQUE,   -- provider+endpoint+symbol+as_of+params
    request_key     TEXT NOT NULL,          -- the same, without the per-reading params
    provider        TEXT NOT NULL,          -- the real provider, never just "openbb"
    endpoint        TEXT NOT NULL,
    symbol          TEXT NOT NULL,          -- QuantDesk contract code
    topic           TEXT NOT NULL,
    as_of           TEXT,                   -- the date the reading describes
    published_at    TEXT,                   -- when the provider published it
    observed_at     TEXT NOT NULL,          -- when QuantDesk fetched it
    expires_at      TEXT,
    source_url      TEXT,
    payload_json    TEXT,
    content_hash    TEXT,
    point_in_time   INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,          -- ok | unavailable | rejected | error
    warning         TEXT,
    created_ts      INTEGER NOT NULL,
    updated_ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS external_evidence_lookup_idx
    ON external_evidence (symbol, topic, updated_ts DESC);
-- A request produces one row per reading, so the cache lookup goes through the
-- request key rather than the per-reading unique key.
CREATE INDEX IF NOT EXISTS external_evidence_request_idx
    ON external_evidence (request_key, updated_ts DESC);
CREATE INDEX IF NOT EXISTS external_evidence_provider_idx
    ON external_evidence (provider, status, updated_ts DESC);

-- One row per external portfolio-analytics computation. The input hash lets an
-- identical request be answered from cache instead of paid for twice, and the
-- market snapshot version ties every result back to the prices it was computed
-- from. Failures are stored as their own rows so they never overwrite the last
-- successful result.
CREATE TABLE IF NOT EXISTS external_analytics_runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    provider                TEXT NOT NULL,
    kind                    TEXT NOT NULL,      -- portfolio | scenario
    as_of                   TEXT,
    input_hash              TEXT NOT NULL,
    market_snapshot_version TEXT,
    positions_hash          TEXT,
    parameters_json         TEXT,
    result_json             TEXT,
    request_id              TEXT,
    status                  TEXT NOT NULL,      -- ok | error | timeout | refused
    error                   TEXT,
    duration_ms             INTEGER,
    created_ts              INTEGER NOT NULL,
    updated_ts              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS external_analytics_lookup_idx
    ON external_analytics_runs (input_hash, status, updated_ts DESC);
CREATE INDEX IF NOT EXISTS external_analytics_kind_idx
    ON external_analytics_runs (kind, provider, updated_ts DESC);

-- A strategy as it existed when a run was made: the parameters, the engine
-- version and a hash of the implementation. A result that cannot name the code
-- that produced it is not reproducible, only remembered.
CREATE TABLE IF NOT EXISTS strategy_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id     TEXT NOT NULL,
    version         TEXT NOT NULL,          -- hash over id + params + engine + code
    parameters_json TEXT NOT NULL DEFAULT '{}',
    engine          TEXT,
    engine_version  TEXT,
    code_hash       TEXT,                   -- hash of the strategy implementation
    source          TEXT,                   -- builtin | plugin:<id>
    created_ts      INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS strategy_versions_key_idx
    ON strategy_versions (strategy_id, version);

-- One queued study. A backtest, a parameter search and a portfolio are all long
-- enough that running them inside an HTTP request means a browser timeout and no
-- record of what was computed. The run row is the record: the exact request, the
-- progress, the result, and the reason it failed.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,          -- backtest | validate | portfolio
    status           TEXT NOT NULL,          -- queued | running | done | failed | cancelled
    label            TEXT NOT NULL DEFAULT '',
    symbol           TEXT NOT NULL DEFAULT '',   -- comma-joined for a portfolio
    display_symbol   TEXT,
    interval         TEXT NOT NULL DEFAULT '',
    strategy_id      TEXT NOT NULL DEFAULT '',
    strategy_version TEXT NOT NULL DEFAULT '',
    request_hash     TEXT NOT NULL DEFAULT '',   -- identity of the request body
    request_json     TEXT NOT NULL,
    result_json      TEXT,
    summary_json     TEXT,                   -- headline metrics, for the list view
    progress         REAL NOT NULL DEFAULT 0,
    progress_label   TEXT NOT NULL DEFAULT '',
    stage            TEXT NOT NULL DEFAULT '',
    attempts         INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    -- Which process owns the current attempt. A superseded attempt's write is
    -- discarded instead of overwriting the recomputed result.
    lease            TEXT,
    error            TEXT,
    error_kind       TEXT,                   -- not_ready | invalid | internal | cancelled
    duration_ms      INTEGER,
    queued_ts        INTEGER NOT NULL,
    started_ts       INTEGER,
    finished_ts      INTEGER,
    updated_ts       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS backtest_runs_status_idx ON backtest_runs (status, id);
CREATE INDEX IF NOT EXISTS backtest_runs_recent_idx ON backtest_runs (queued_ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS backtest_runs_request_idx ON backtest_runs (request_hash, status);

-- The parts of a result a caller wants without re-reading the whole payload:
-- the equity curve, the trade list, the walk-forward windows. Payloads are
-- stored inline with their hash so a download can be checked.
CREATE TABLE IF NOT EXISTS backtest_artifacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    name         TEXT NOT NULL,
    media_type   TEXT NOT NULL DEFAULT 'application/json',
    bytes        INTEGER NOT NULL DEFAULT 0,
    sha256       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_ts   INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS backtest_artifacts_name_idx
    ON backtest_artifacts (run_id, name);

-- One statistical reading about a run: the walk-forward verdicts a validation
-- run produces, and the bootstrap / randomization / multiple-testing readings a
-- validator plugin adds. Kept as rows so "this result passed X" is a fact with a
-- number and a threshold behind it, not a sentence in a log.
CREATE TABLE IF NOT EXISTS backtest_validation_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    kind         TEXT NOT NULL,              -- walk_forward | leakage | bootstrap | randomization | deflated_sharpe | pbo
    verdict      TEXT NOT NULL DEFAULT '',   -- pass | warn | fail | info
    statistic    REAL,
    p_value      REAL,
    threshold    REAL,
    provider     TEXT,                       -- engine | plugin:<id>
    detail       TEXT NOT NULL DEFAULT '',
    detail_json  TEXT,
    -- required:sandbox-exec:enforced 等：产出这条结论时插件处于什么隔离级别。
    sandbox      TEXT NOT NULL DEFAULT '',
    created_ts   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS backtest_validation_run_idx
    ON backtest_validation_results (run_id, id);

-- The factor library a provider offers, as it was when the engine last asked.
-- Stored so the catalogue is readable (and a factor run is attributable) even
-- when the plugin is disabled or its process cannot start.
CREATE TABLE IF NOT EXISTS factor_definitions (
    provider         TEXT NOT NULL,
    factor_id        TEXT NOT NULL,
    name             TEXT NOT NULL DEFAULT '',
    family           TEXT NOT NULL DEFAULT '',
    mode             TEXT NOT NULL DEFAULT 'time_series',
    sources_json     TEXT NOT NULL DEFAULT '[]',
    required_json    TEXT NOT NULL DEFAULT '[]',
    warmup_bars      INTEGER NOT NULL DEFAULT 0,
    timeframes_json  TEXT NOT NULL DEFAULT '[]',
    implementation   TEXT NOT NULL DEFAULT '',
    formula_hash     TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL DEFAULT '',
    provider_version TEXT NOT NULL DEFAULT '',
    created_ts       INTEGER NOT NULL,
    updated_ts       INTEGER NOT NULL,
    PRIMARY KEY (provider, factor_id)
) WITHOUT ROWID;

-- One factor computation over one contract's stored history. The snapshot hash is
-- the data version the values belong to, so a factor series can be cited the same
-- way a backtest pins its bars.
CREATE TABLE IF NOT EXISTS factor_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    interval      TEXT NOT NULL DEFAULT '',
    snapshot_hash TEXT NOT NULL DEFAULT '',
    factor_ids    TEXT NOT NULL DEFAULT '[]',
    parameters_json TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'ok',   -- ok | error | unavailable
    bars          INTEGER NOT NULL DEFAULT 0,
    series_count  INTEGER NOT NULL DEFAULT 0,
    coverage_json TEXT,                          -- per factor: how many bars carry a value
    -- The series themselves. New rows store them zlib-compressed in `values_blob`
    -- (a 28-factor run over 20k bars is ~25 MB of JSON and ~2 MB compressed);
    -- `values_json` is kept so a row written before compression still reads back.
    values_json   TEXT,
    values_blob   BLOB,
    warnings_json TEXT,
    error         TEXT,
    -- Whether the provider ran under an enforced sandbox when it produced this:
    -- a result nobody can attribute to an isolation level is a result of unknown
    -- provenance.
    sandbox       TEXT NOT NULL DEFAULT '',
    duration_ms   INTEGER,
    created_ts    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS factor_runs_symbol_idx
    ON factor_runs (symbol, interval, created_ts DESC);

-- A campaign is a *pre-registered* search: the hypothesis, the universe, the factor
-- space, the three date windows and the budget are all written down before the first
-- proposal exists. Everything about this table exists to make that claim checkable -
-- the factor space is a frozen snapshot of the library rather than a live query, and
-- the windows are stored so nobody can move the goalposts afterwards.
--
-- The test window is written here at registration and is *never* served to the agent;
-- `test_unsealed_ts` is the single-use key that opens it (stage 6).
CREATE TABLE IF NOT EXISTS agent_campaigns (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_uid      TEXT NOT NULL,
    provider          TEXT NOT NULL,
    agent_version     TEXT NOT NULL DEFAULT '',
    mode              TEXT NOT NULL DEFAULT 'deterministic_search',
    group_name        TEXT NOT NULL,                 -- stock | leveraged_etf | crypto
    interval          TEXT NOT NULL DEFAULT '1h',
    horizon_bars      INTEGER NOT NULL DEFAULT 24,
    universe_json     TEXT NOT NULL DEFAULT '[]',    -- frozen symbol list
    factor_space_json TEXT NOT NULL DEFAULT '[]',    -- frozen {factorId, tier} snapshot
    library_ts        TEXT NOT NULL DEFAULT '',      -- which scan the space came from
    hypothesis        TEXT NOT NULL,
    success_criteria  TEXT NOT NULL,
    train_start_ts    INTEGER, train_end_ts      INTEGER,
    validation_start_ts INTEGER, validation_end_ts INTEGER,
    test_start_ts     INTEGER, test_end_ts       INTEGER,
    test_unsealed_ts  INTEGER,                       -- set once, never twice
    budget_json       TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'preregistered',
    stop_reason       TEXT NOT NULL DEFAULT '',
    rounds_used       INTEGER NOT NULL DEFAULT 0,
    trials_used       INTEGER NOT NULL DEFAULT 0,
    proposals_used    INTEGER NOT NULL DEFAULT 0,
    seed              INTEGER NOT NULL DEFAULT 42,
    snapshot_hash     TEXT NOT NULL DEFAULT '',
    created_ts        INTEGER NOT NULL,
    started_ts        INTEGER,
    finished_ts       INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_campaigns_uid_idx
    ON agent_campaigns (campaign_uid);
CREATE INDEX IF NOT EXISTS agent_campaigns_status_idx
    ON agent_campaigns (status, created_ts DESC);

-- One candidate, as data: factor ids from the frozen space, parameters inside the
-- provider's declared ranges, a rule template by name. Never code, never a path.
CREATE TABLE IF NOT EXISTS agent_proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   INTEGER NOT NULL,
    round         INTEGER NOT NULL DEFAULT 1,
    proposal_uid  TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'parameter_set',
    factor_ids    TEXT NOT NULL DEFAULT '[]',
    parameters    TEXT NOT NULL DEFAULT '{}',
    rule_json     TEXT,
    hypothesis    TEXT NOT NULL,
    expected_failure_mode TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'proposed',  -- proposed|evaluated|rejected|failed
    reject_reason TEXT NOT NULL DEFAULT '',
    created_ts    INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_proposals_key_idx
    ON agent_proposals (campaign_id, proposal_uid);
CREATE INDEX IF NOT EXISTS agent_proposals_round_idx
    ON agent_proposals (campaign_id, round);

-- What a proposal scored, per segment. `segment` is constrained by the service to
-- train/validation: the test segment has no writer until it is unsealed, so there is
-- no row to leak.
CREATE TABLE IF NOT EXISTS agent_trials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  INTEGER NOT NULL,
    proposal_id  INTEGER NOT NULL,
    segment      TEXT NOT NULL DEFAULT 'validation',
    run_id       TEXT,
    sharpe       REAL,
    return_pct   REAL,
    max_drawdown_pct REAL,
    trades       INTEGER,
    verdict      TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    created_ts   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_trials_campaign_idx
    ON agent_trials (campaign_id, segment, id);

-- Every budget decision, including the ones that did not breach. A campaign that
-- stopped because it ran out of budget must be able to show the arithmetic.
CREATE TABLE IF NOT EXISTS agent_budget_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  INTEGER NOT NULL,
    round        INTEGER NOT NULL DEFAULT 0,
    entry        TEXT NOT NULL,                 -- proposals|rounds|trials|wallclock_ms
    amount       REAL NOT NULL DEFAULT 0,
    limit_value  REAL NOT NULL DEFAULT 0,
    breached     INTEGER NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT '',
    created_ts   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_budget_campaign_idx
    ON agent_budget_ledger (campaign_id, id);

-- Gate-C: what the unsealed test segment said, once, with the pre-registered
-- criteria quoted verbatim next to the numbers that were compared to them.
-- One row per (campaign, proposal, segment): the unique index *is* the idempotency
-- of the one-shot unsealing, so a second adjudication cannot rewrite a verdict.
-- `verdict` is pass|fail|inconclusive: a campaign that fails is a result, not an
-- error, and "cannot compute" is never recorded as a pass.
CREATE TABLE IF NOT EXISTS agent_verdicts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id      INTEGER NOT NULL,
    campaign_uid     TEXT NOT NULL,
    proposal_uid     TEXT NOT NULL DEFAULT '',
    segment          TEXT NOT NULL DEFAULT 'test',
    group_name       TEXT NOT NULL DEFAULT '',
    interval         TEXT NOT NULL DEFAULT '',
    horizon_bars     INTEGER NOT NULL DEFAULT 0,
    window_start_ts  INTEGER,
    window_end_ts    INTEGER,
    run_id           TEXT,
    bars             INTEGER,
    sharpe           REAL,
    return_pct       REAL,
    max_drawdown_pct REAL,
    trades           INTEGER,
    deflated_sharpe  REAL,
    expected_max_sharpe REAL,
    pbo              REAL,
    trials           INTEGER,
    verdict          TEXT NOT NULL,
    reason           TEXT NOT NULL DEFAULT '',
    criteria         TEXT NOT NULL DEFAULT '',
    evidence_json    TEXT NOT NULL DEFAULT '{}',
    approved_by      TEXT NOT NULL DEFAULT '',
    created_ts       INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_verdicts_key_idx
    ON agent_verdicts (campaign_id, proposal_uid, segment);
CREATE INDEX IF NOT EXISTS agent_verdicts_campaign_idx
    ON agent_verdicts (campaign_id, created_ts DESC);
"""

# Bumped when a step is added to MIGRATIONS.
SCHEMA_VERSION = 1


def compress_legacy_factor_values(conn: sqlite3.Connection, *, limit: int = 200) -> int:
    """Compress factor series written before the column existed.

    Bounded on purpose: a startup must not turn into a multi-minute rewrite of
    the whole table, so it converts a batch per open and the rest follow on the
    next ones. Rows it has not reached still read correctly from `values_json`.
    """
    rows = conn.execute(
        "SELECT id, values_json FROM factor_runs "
        "WHERE values_json IS NOT NULL AND values_blob IS NULL LIMIT ?",
        (int(limit),),
    ).fetchall()
    converted = 0
    for row in rows:
        try:
            payload = zlib.compress(str(row["values_json"]).encode("utf-8"), 6)
        except (zlib.error, UnicodeError):
            continue
        conn.execute(
            "UPDATE factor_runs SET values_blob=?, values_json=NULL WHERE id=?",
            (payload, int(row["id"])),
        )
        converted += 1
    if converted:
        conn.commit()
    return converted


def repair_and_migrate_legacy(conn: sqlite3.Connection) -> None:
    """Columns, table rebuilds and data repairs an older file needs.

    This runs on **every** open, not once per database, and that is deliberate:
    the statements in it are idempotent, and two of them repair *rows* rather than
    shape - a snapshot recorded with an empty interval, a candle row still using
    the retired `venue_rest_backfill` source. A database restored from a backup, or
    written by an older binary after it was last opened, must be repaired too, and
    a `user_version` guard would skip exactly those cases.

    Structural changes that are *not* safe to repeat live in `MIGRATIONS` below,
    keyed by `user_version`.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
    if "entry_fee" not in columns:
        conn.execute("ALTER TABLE positions ADD COLUMN entry_fee REAL NOT NULL DEFAULT 0")
    if "funding_paid" not in columns:
        conn.execute("ALTER TABLE positions ADD COLUMN funding_paid REAL NOT NULL DEFAULT 0")
    # backfill_state gained a data_kind column in its primary key. An older
    # table only ever held trade-candle walks, so it is rebuilt rather than
    # guessed at; the state is operational, not history.
    task_columns = {row[1] for row in conn.execute("PRAGMA table_info(backfill_tasks)").fetchall()}
    if task_columns and "complete" not in task_columns:
        conn.execute("ALTER TABLE backfill_tasks ADD COLUMN complete INTEGER NOT NULL DEFAULT 0")
    if task_columns and "failure_attempts" not in task_columns:
        conn.execute("ALTER TABLE backfill_tasks ADD COLUMN failure_attempts INTEGER NOT NULL DEFAULT 0")
    state_columns = {row[1] for row in conn.execute("PRAGMA table_info(backfill_state)").fetchall()}
    if state_columns and "data_kind" not in state_columns:
        conn.execute("ALTER TABLE backfill_state RENAME TO backfill_state_old")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO backfill_state "
            "(venue, symbol, interval, data_kind, oldest_ts, newest_ts, complete, pages, "
            " rows_available, attempts, status, last_error, last_error_kind, last_run_ts, updated_ts) "
            "SELECT venue, symbol, interval, 'trade_candle', oldest_ts, newest_ts, complete, pages, "
            " bars, attempts, 'ok', last_error, last_error_kind, last_run_ts, updated_ts "
            "FROM backfill_state_old"
        )
        conn.execute("DROP TABLE backfill_state_old")
    # Open-interest points from different venue intervals must not replace
    # one another. Older databases only contained the default 1h series.
    oi_columns = {row[1] for row in conn.execute("PRAGMA table_info(open_interest)").fetchall()}
    if oi_columns and "interval" not in oi_columns:
        conn.execute("ALTER TABLE open_interest RENAME TO open_interest_old")
        conn.execute(
            "CREATE TABLE open_interest ("
            "venue TEXT NOT NULL, symbol TEXT NOT NULL, interval TEXT NOT NULL DEFAULT '1h', "
            "ts INTEGER NOT NULL, oi REAL NOT NULL, "
            "PRIMARY KEY (venue, symbol, interval, ts)) WITHOUT ROWID"
        )
        conn.execute(
            "INSERT OR REPLACE INTO open_interest (venue,symbol,interval,ts,oi) "
            "SELECT venue,symbol,'1h',ts,oi FROM open_interest_old"
        )
        conn.execute("DROP TABLE open_interest_old")
    # A claimed run carries the identity of the process that owns it, so an
    # attempt that has been superseded (its process was killed and the run
    # recomputed elsewhere) cannot publish its result over the newer one.
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(backtest_runs)").fetchall()}
    if run_columns and "lease" not in run_columns:
        conn.execute("ALTER TABLE backtest_runs ADD COLUMN lease TEXT")
    factor_columns = {row[1] for row in conn.execute("PRAGMA table_info(factor_runs)").fetchall()}
    if factor_columns and "values_blob" not in factor_columns:
        conn.execute("ALTER TABLE factor_runs ADD COLUMN values_blob BLOB")
        self._compress_stored_factor_values()
    if factor_columns and "sandbox" not in factor_columns:
        conn.execute("ALTER TABLE factor_runs ADD COLUMN sandbox TEXT NOT NULL DEFAULT ''")
    verdict_columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(backtest_validation_results)").fetchall()}
    if verdict_columns and "sandbox" not in verdict_columns:
        conn.execute(
            "ALTER TABLE backtest_validation_results ADD COLUMN sandbox TEXT NOT NULL DEFAULT ''")
    snapshot_columns = {row[1] for row in conn.execute("PRAGMA table_info(history_snapshots)").fetchall()}
    for column, ddl in (
        ("data_kind", "TEXT NOT NULL DEFAULT 'trade_candle'"),
        ("bars_available", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if snapshot_columns and column not in snapshot_columns:
            conn.execute(f"ALTER TABLE history_snapshots ADD COLUMN {column} {ddl}")
    candle_columns = {row[1] for row in conn.execute("PRAGMA table_info(candles)").fetchall()}
    for column, ddl in (
        ("source", "TEXT"),
        ("ingestion_mode", "TEXT"),
        ("exchange_ts", "INTEGER"),
        ("received_ts", "INTEGER"),
        ("collector", "TEXT"),
    ):
        if column not in candle_columns:
            conn.execute(f"ALTER TABLE candles ADD COLUMN {column} {ddl}")
    # `venue_rest_backfill` was a source string meaning "a venue REST read
    # taken during a history walk". It is normalised away: the provenance is
    # venue_rest, and the walk is recorded in ingestion_mode.
    conn.execute(
        "UPDATE candles SET ingestion_mode='backfill', source='venue_rest' "
        "WHERE source='venue_rest_backfill'"
    )
    conn.execute(
        "UPDATE candles SET ingestion_mode='live' WHERE ingestion_mode IS NULL"
    )
    # Early builds stored Bybit's fundingInterval minutes under an
    # hours-labelled column. Values above one day are unambiguously the
    # minute representation (for example 480 -> 8 hours).
    conn.execute(
        "UPDATE instrument_meta SET funding_interval_hours=funding_interval_hours/60.0 "
        "WHERE funding_interval_hours>24"
    )
    # Open-interest has an explicit venue interval. The first release used
    # an empty task interval even though it always fetched 1h points.
    conn.execute(
        "DELETE FROM backfill_tasks WHERE data_kind='open_interest' AND interval='' "
        "AND EXISTS (SELECT 1 FROM backfill_tasks newer WHERE newer.venue=backfill_tasks.venue "
        "AND newer.symbol=backfill_tasks.symbol AND newer.data_kind='open_interest' AND newer.interval='1h')"
    )
    conn.execute(
        "UPDATE backfill_tasks SET interval='1h' WHERE data_kind='open_interest' AND interval=''"
    )
    conn.execute(
        "UPDATE history_snapshots SET interval='1h' WHERE data_kind='open_interest' AND interval=''"
    )
    # Operational state added after the first backfill release is rebuilt
    # from authoritative tables whenever an older database is opened.
    conn.execute(
        "UPDATE backfill_tasks SET complete=1 WHERE status IN ('done','unsupported')"
    )
    conn.execute(
        "UPDATE backfill_tasks SET pages_estimate=0 WHERE status IN ('done','unsupported')"
    )
    conn.execute(
        "UPDATE backfill_tasks SET pages=COALESCE((SELECT s.pages FROM backfill_state s "
        "WHERE s.venue=backfill_tasks.venue AND s.symbol=backfill_tasks.symbol "
        "AND s.interval=backfill_tasks.interval AND s.data_kind=backfill_tasks.data_kind),pages), "
        "rows_available=COALESCE((SELECT s.rows_available FROM backfill_state s "
        "WHERE s.venue=backfill_tasks.venue AND s.symbol=backfill_tasks.symbol "
        "AND s.interval=backfill_tasks.interval AND s.data_kind=backfill_tasks.data_kind),rows_available)"
    )
    for snapshot in conn.execute(
        "SELECT id,venue,symbol,interval,data_kind,sources FROM history_snapshots"
    ).fetchall():
        try:
            sources = json.loads(snapshot["sources"] or "{}")
        except (TypeError, json.JSONDecodeError):
            sources = {}
        legacy = int(sources.pop("venue_rest_backfill", 0) or 0)
        if legacy:
            sources["venue_rest"] = int(sources.get("venue_rest", 0) or 0) + legacy
        kind = snapshot["data_kind"] or "trade_candle"
        if kind == "trade_candle":
            count_sql = "SELECT COUNT(*) FROM candles WHERE venue=? AND symbol=? AND interval=?"
            count_args = (snapshot["venue"], snapshot["symbol"], snapshot["interval"])
        elif kind == "mark_candle":
            count_sql = "SELECT COUNT(*) FROM mark_candles WHERE venue=? AND symbol=? AND interval=?"
            count_args = (snapshot["venue"], snapshot["symbol"], snapshot["interval"])
        elif kind == "funding":
            count_sql = "SELECT COUNT(*) FROM funding WHERE venue=? AND symbol=?"
            count_args = (snapshot["venue"], snapshot["symbol"])
        elif kind == "open_interest":
            count_sql = "SELECT COUNT(*) FROM open_interest WHERE venue=? AND symbol=? AND interval=?"
            count_args = (snapshot["venue"], snapshot["symbol"], snapshot["interval"] or "1h")
        else:
            count_sql = "SELECT COUNT(*) FROM risk_tiers WHERE venue=? AND symbol=?"
            count_args = (snapshot["venue"], snapshot["symbol"])
        available = int(conn.execute(count_sql, count_args).fetchone()[0])
        conn.execute(
            "UPDATE history_snapshots SET sources=?,bars_available=? WHERE id=?",
            (json.dumps(sources, ensure_ascii=False), available, snapshot["id"]),
        )
    # A table that existed before a column was added keeps working: every
    # new column is applied in place rather than assumed present.
    cost_columns = {row[1] for row in conn.execute("PRAGMA table_info(tradingagents_costs)").fetchall()}
    for column, ddl in (
        ("usage_known", "INTEGER NOT NULL DEFAULT 0"),
        ("reused_from", "TEXT"),
        ("prompt_version", "TEXT"),
        ("data_version", "TEXT"),
        ("data_as_of", "TEXT"),
        ("missing_analysts", "TEXT"),
        ("staleness", "TEXT"),
        ("cost_detail", "TEXT"),
        ("retries", "INTEGER NOT NULL DEFAULT 0"),
        ("analyst_retries", "INTEGER NOT NULL DEFAULT 0"),
        ("failure", "TEXT"),
    ):
        if cost_columns and column not in cost_columns:
            conn.execute(f"ALTER TABLE tradingagents_costs ADD COLUMN {column} {ddl}")
    alert_columns = {row[1] for row in conn.execute("PRAGMA table_info(alert_rules)").fetchall()}
    alert_migrations = {
        "severity": "TEXT NOT NULL DEFAULT 'warning'",
        "quiet_start": "TEXT",
        "quiet_end": "TEXT",
        "timezone": "TEXT NOT NULL DEFAULT 'Asia/Shanghai'",
        "daily_limit": "INTEGER NOT NULL DEFAULT 10",
        "confirmation_count": "INTEGER NOT NULL DEFAULT 1",
        "consecutive_count": "INTEGER NOT NULL DEFAULT 0",
        "hysteresis": "REAL NOT NULL DEFAULT 0",
        "armed": "INTEGER NOT NULL DEFAULT 1",
        "last_observation_key": "TEXT",
    }
    for column, definition in alert_migrations.items():
        if column not in alert_columns:
            conn.execute(f"ALTER TABLE alert_rules ADD COLUMN {column} {definition}")
    ai_profile_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(ai_paper_profiles)").fetchall()
    }
    if ai_profile_columns and "fib_only" not in ai_profile_columns:
        conn.execute(
            "ALTER TABLE ai_paper_profiles ADD COLUMN fib_only INTEGER NOT NULL DEFAULT 0"
        )
    if ai_profile_columns and "name" not in ai_profile_columns:
        conn.execute(
            "ALTER TABLE ai_paper_profiles ADD COLUMN name TEXT NOT NULL DEFAULT '主模拟'"
        )
    if ai_profile_columns and "config_revision" not in ai_profile_columns:
        # An existing profile keeps the rules it is already running as revision 1; the
        # next real change moves it to 2. Nothing is rewritten in place.
        conn.execute(
            "ALTER TABLE ai_paper_profiles ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 1"
        )
    # Upgrade pre-combination rules without changing their identity or
    # trigger history. New rules always write their condition rows in
    # the same transaction as the parent.
    conn.execute(
        "INSERT OR IGNORE INTO alert_rule_conditions "
        "(id,rule_id,position,condition_type,timeframe,threshold) "
        "SELECT lower(hex(randomblob(16))),id,0,condition_type,timeframe,threshold "
        "FROM alert_rules"
    )
    conn.commit()

    conn.commit()


# Structural steps that must run exactly once per database, in order. Add a new
# tuple when a change cannot be expressed as an idempotent repair above.
MIGRATIONS: list[tuple[int, str, object]] = []


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Repair, then bring the file to `SCHEMA_VERSION`, and say what it is now.

    A failure is raised rather than swallowed: half a schema is worse than a
    refused start, and the message names the step that broke.
    """
    repair_and_migrate_legacy(conn)
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    applied = current
    for version, description, step in MIGRATIONS:
        if version <= current:
            continue
        try:
            step(conn)  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 - reported with the step name
            raise RuntimeError(f"数据库迁移步骤 {version}（{description}）失败：{exc}") from exc
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()
        applied = version
    if applied < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        applied = SCHEMA_VERSION
    return applied
