# Price Replay Lab

A local-first, candle-close market replay workstation. It imports canonical one-minute data, replays a selected range without future leakage, derives causal higher timeframes, and simulates independent long and short trades.

## Quick start

```bash
cd backend
uv sync --dev
uv run uvicorn app.main:app --reload
```

In another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`.

## Supported import schema

V1 accepts one downloader-style UTC CSV schema:

```text
Date,Time,Open,High,Low,Close,TickVolume,Volume,Spread
2026.01.02,17:00:00,1.1010,1.1015,1.1007,1.1013,15,0,0
```

- `Date` and `Time` are the one-minute candle's exact UTC opening timestamp; seconds must be `00`.
- Rows must be unique by minute and contain finite, valid OHLC and non-negative volume values; source ordering is audited and normalized.
- `Volume` is used when positive; otherwise `TickVolume` is used.
- Gaps are reported but accepted.
- The importer keeps the source under `data/raw/` and publishes normalized yearly Parquet as an immutable data version. New sessions pin that version, so re-importing a symbol cannot change an existing replay.

## Current behavior

- UTC-aligned and DST-aware New York-close-aligned higher timeframes.
- Configurable, bounded 500-2,000 bar chart context with causal SMA 35.
- Server-authoritative replay with arbitrary positive step sizes and resumable session history.
- Independent and opposing long/short trades, editable stops and targets, partial exits, and close-all.
- Configurable spread, slippage, per-side commission, and account balance.
- Cost-aware fill ledger, realized and unrealized P&L, equity, drawdown, R multiples, and trade statistics.
- SQLite WAL snapshots, transactional order/event audit records, and immutable market-data versions.

## Execution model

`spread` is the full bid/ask width around the replayed midpoint. Each buy fills at
midpoint plus half-spread and adverse slippage; each sell fills at midpoint minus
half-spread and adverse slippage. Commission is charged per quantity unit on each
side. Equity includes the estimated cost to liquidate open positions.

Stops and targets are evaluated sequentially for every revealed one-minute candle.
An opening gap through a level fills at the candle open. If both levels are touched
later in the same candle and their ordering is unknowable, the stop wins
conservatively.

The API documentation is available at `http://localhost:8000/docs`.

## Dukascopy downloader

The standalone, multi-instrument downloader is documented in
[`tools/dukascopy_downloader/README.md`](tools/dukascopy_downloader/README.md).
It exports raw ticks or audited `M1`, `H1`, and `D1` candle CSVs. Prefer `M1`
exports for Price Replay imports; the application derives higher timeframes.

## Tests

From the repository root:

```bash
make test
```

This runs the backend and downloader Pytest suites, the frontend Vitest suite, and
the production TypeScript/Vite build.
