# QuantDesk Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Delegated from the confirmed brief: React, TypeScript, Vite, Tailwind CSS v4, and a shadcn-compatible `components/ui` structure. The Python engine remains the analytics backend.

## Users

The primary user is an active short-term trader reviewing a small, fixed universe of Bybit USDT perpetual contracts. They need one local workspace to scan several timeframes, inspect derivatives data, test rules, request an AI research pass, and record simulated trades.

## Product Purpose

QuantDesk combines contract market data, deterministic strategy signals, multi-timeframe confirmation, research, backtesting, and paper trading. Success means the user can move from market scan to an auditable decision without switching tools or confusing model commentary with executable trading rules.

## Positioning

The product joins deterministic contract analytics with an optional TradingAgents research layer. Every conclusion retains its source, timeframe, data freshness, and invalidation condition.

## Operating Context

The product starts as a local web application and may later be packaged as a desktop app. Bybit is the primary venue. OpenClaw and WeChat notifications are later integrations. Live execution is deferred until the data, strategy, risk, and paper-trading layers are stable.

## Capabilities and Constraints

- Fixed universe: AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA, SNDK, MU, AMD, NBIS, SPCX, SKHY, SOXL, SOXS, BTC, and ETH.
- Stock instruments are Bybit TradFi linear USDT perpetual contracts, not xStocks spot tokens.
- Required timeframes: 15m, 1h, 4h, and 1d.
- Required inputs include candles, volume, funding, open interest, mark price, and index price.
- The application supports rule backtests, TradingAgents research, paper trading, and an immutable trading journal.
- LLM providers and API credentials are configured independently from market-data providers.
- Signals use closed candles. Instrument status and exchange specifications are checked dynamically.
- AMD maps to `AMDSTOCKUSDT`; all display symbols are mapped explicitly to venue symbols.

## Evidence on Hand

- A partial Python engine exists under `engine/src/quantdesk`.
- User-provided React references define a floating dock, a K-line file uploader, and animated background beams.
- No production performance claims, testimonials, or branded image assets exist and none should be invented.

## Product Principles

1. Show data freshness and provenance beside every conclusion.
2. Keep deterministic signals separate from AI interpretation.
3. Make risk visible before action, especially for leveraged ETF contracts.
4. Preserve a fixed, intentional instrument universe while validating venue availability dynamically.
5. Default to simulation and auditability before execution.

