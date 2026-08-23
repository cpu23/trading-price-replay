# Price Replay Lab

A local-first, candle-close market replay workstation. It imports canonical one-minute data, replays a selected range without future leakage, derives causal higher timeframes, and simulates independent long and short trades.

## Quick start

```bash
cd backend
uv sync --python 3.12 --locked --dev
uv run uvicorn app.main:app --reload
```

In another terminal:

```bash
cd frontend
npm ci
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
- Independent and opposing long/short trades, editable stops and targets, decimal-safe partial exits, and close-all.
- Configurable spread, slippage, per-side commission, and account balance.
- Cost-aware fill ledger, realized and unrealized P&L, equity, drawdown, R multiples, and statistically honest nullable averages/ratios.
- Bounded recent state with paginated complete trade/fill history, bounded historical chart focus, visible truncation/errors, and resumable review notes/tags.
- Versioned, transactional SQLite schema migrations, WAL snapshots, indexed session history, and immutable market-data versions.

## Execution model

`spread` is the full bid/ask width around the replayed midpoint. Each buy fills at
midpoint plus half-spread and adverse slippage; each sell fills at midpoint minus
half-spread and adverse slippage. Commission is charged per quantity unit on each
side. Equity includes the estimated cost to liquidate open positions.

Stops and targets are evaluated sequentially for every revealed one-minute candle.
An opening gap through a level fills at the candle open. If both levels are touched
later in the same candle and their ordering is unknowable, the stop wins
conservatively.

All close entry points use one quantity policy. Decimal user-visible remainders such as `0.3 - 0.1 = 0.2` close without binary-float dust; only non-material representational residue is canonicalized. Legitimate tiny quantities stay open, materially oversized closes remain errors, and the final fill books the actual stored remainder.

Statistics distinguish zero activity from unavailable evidence. Counts and totals are zero when empty; ratios and averages render as unavailable when their denominator has no observations. `profit_factor` is unavailable without gross losing P&L, and R averages exclude trades without defined finite initial risk.

The API documentation is available at `http://localhost:8000/docs`.

## Database safety

Schema migrations run automatically when the backend starts. A database created
by a newer application version is rejected rather than modified.

Create a validated online backup while the backend is running:

```bash
cd backend
uv run python -m app.maintenance backup ../price-replay-backup.sqlite3
```

Stop the backend before restoring. Restore validates and upgrades the backup,
creates a safety backup of the current database, and then replaces it:

```bash
uv run python -m app.maintenance restore ../price-replay-backup.sqlite3
```

These commands protect the SQLite session database. A complete workstation
backup must also preserve `data/raw/` and `data/ohlcv/`, because replay sessions
pin immutable market-data versions stored there.

## Dukascopy downloader

The standalone, multi-instrument downloader is documented in
[`tools/dukascopy_downloader/README.md`](tools/dukascopy_downloader/README.md).
It exports raw ticks or audited `M1`, `H1`, and `D1` candle CSVs. Prefer `M1`
exports for Price Replay imports; the application derives higher timeframes.

## Verification

Install the locked environments and browser once:

```bash
cd backend && uv sync --python 3.12 --locked --dev && cd ..
cd frontend && npm ci && npx playwright install chromium && cd ..
cd tools/dukascopy_downloader && uv sync --python 3.12 --locked --dev && cd ../..
```

From the repository root, run the complete local gate:

```bash
make verify
```

`make verify` runs restrained Ruff correctness linting, both Python suites, frontend Vitest and production TypeScript/Vite builds, OpenAPI/frontend-type regeneration with a drift check, the high-severity npm audit, and isolated Playwright E2E tests. `make test` omits lint, generated-contract drift, dependency audit, and browser tests.

The E2E runner creates an ephemeral backend data root and deterministic fixtures; it never uses the normal session database. CI retains browser traces, failure screenshots, and a JSON report as the `playwright-diagnostics` artifact when a browser test fails.

Design guarantees and measurement decisions are documented in [`docs/architecture.md`](docs/architecture.md), [`docs/performance.md`](docs/performance.md), and [`docs/research/manual-replay-market-review.md`](docs/research/manual-replay-market-review.md).
