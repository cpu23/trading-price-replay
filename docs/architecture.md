# Architecture

Normalized UTC one-minute Parquet is the canonical market source. Each successful import publishes an immutable version. SQLite stores the active version pointer, symbol metadata, import records, replay snapshots, orders, fills, and events.

The backend owns replay time, bounded market-data reads, resampling, fills, indicators, and statistics. The React client requests authoritative state and projects it into Lightweight Charts.

## Causality

At session creation, only candles before the selected start are available as context. A manual or client-paced step reveals the next N one-minute candles. Each minute is processed sequentially before state is committed, so a stop hit during a multi-minute step fills at the correct minute.

A one-minute bar's `timestamp` is its **opening** time; the bar represents `[timestamp, timestamp + 1m)` and its close is not causally available until the interval ends. The domain exposes this through a single `bar_reveal_time(bar)` helper (`revealed_at = timestamp + 1m`) rather than scattered minute arithmetic. The state API keeps both concepts distinct:

- `current_market_time` — the revealed causal market time (the latest revealed candle's close time). This is the market clock the user sees and the timestamp of every market-price execution.
- `current_candle_time` — the opening time of the underlying M1 candle that produced it.

Market entries, manual partial/full exits, close-all, and the final session liquidation all execute at the revealed close and carry that exact reveal timestamp.

Higher-timeframe candles are always derived from context plus revealed one-minute candles. The active bucket is partial; earlier buckets are finalized. SMA 35 uses only these causal displayed candles.

## Data publication and session durability

An import validates the complete CSV before publishing Parquet under a batch-specific version directory. The symbol's current version and import record change in one SQLite transaction. Existing versions are retained because replay sessions pin both the data version and contract multiplier they started with; replacing a symbol therefore cannot change their candle sequence or P&L scale.

Legacy sessions and datasets without a version pointer continue to read the retained `data/ohlcv/<symbol>/1m` layout. New sessions always pin the current immutable version.

Session snapshots, indicators, trades, fills, order audit rows, and replay events commit in one database transaction. A failed mutation cannot leave an order record without the corresponding authoritative state.

Database schema changes are ordered and versioned. Each migration and its
version marker commit in one explicit SQLite transaction; an interrupted
migration rolls back and resumes on the next startup. Databases from a newer
application version are rejected without modification. Session-owned child
tables are indexed by session ID for bounded resume and deletion work.

The maintenance command uses SQLite's online backup API, so committed WAL
frames are included. Backups and restore candidates must pass `PRAGMA
quick_check`; restore upgrades older supported schemas, creates a validated
safety backup, checkpoints the stopped live database, and only then installs
the candidate. SQLite backup does not include the immutable Parquet/raw stores,
which must be retained separately for a complete workstation recovery.

## Execution and accounting

Market candles represent midpoint prices. Buys execute above midpoint and sells below midpoint by half the configured full spread plus adverse slippage. Commission is charged per quantity unit on every entry or exit side. Fills retain both reference and execution prices, gross P&L, each cost component, and net P&L.

Opening gaps through a stop or target execute at the candle open. When an open lies between both levels and the candle later touches both, the engine chooses the stop conservatively. Multi-minute steps still process candles individually.

One-minute OHLC cannot identify the exact intrabar moment of a stop or target touch, so fills state their time precision explicitly instead of inventing exact timestamps:

- `exact` — the execution time is known: market-price executions at a revealed close, and gap executions at the candle open.
- `bar_interval` — the fill is known only to have occurred inside one M1 candle; the fill stores `execution_window_start` (candle open) and `execution_window_end` (candle close) alongside an ordering timestamp, and the UI renders the interval rather than a fake instant.
- `legacy` — fills persisted before these fields exist. They load safely and display their recorded timestamp without claiming precision.

Realized balance is the sum of committed fill P&L. Unrealized P&L deducts the projected exit-side costs for each open remainder, making reported equity a liquidation value.

Close quantities remain binary floats on the wire and in persisted legacy rows, but every backend close path delegates to one policy. It subtracts `Decimal(str(value))` representations to remove ordinary base-10 display drift, tolerates only a 256-ULP residual that is smaller than both positive operands, and uses exact comparison when that window would be material (including subnormal quantities). A tolerated final close fills the actual stored remainder and writes canonical `0.0`; genuine oversize is rejected. There is no inferred symbol lot step, and price or P&L fields are not converted to Decimal.

## Statistics and trade review

Session statistics come from a durable incremental accumulator persisted with the replay snapshot. Every authoritative mutation (entry fill, exit fill, final close) updates the relevant aggregates in the same transaction: costs, balance, direction P&L, and — only when a trade finally reaches canonical zero quantity — completed/win/loss and finite realized-R aggregates. Partial exits never move final-trade statistics. Current unrealized P&L is recomputed from open trades, and maximum drawdown is the worse of historical realized drawdown and current equity below the historical peak.

Counts and totals are numeric zero with no observations. Ratios and averages whose denominator is empty are null: `win_rate`, `profit_factor` without a gross loss, R averages without qualifying risk observations, winning/losing averages without that outcome, and average holding time without a completed trade. Non-finite derived R values are excluded rather than emitted; the wire contract permits finite numbers or null only.

Because of the accumulator, routine replay operations never scan history: resume and step load scalar session state, every open trade, at most 200 recent closed trades, and at most 1,000 fills. Session statistics are O(1) reads. Legacy sessions without an accumulator get it backfilled once from normalized fills and trades inside the migration/commit transaction.

Closed trades carry review metadata: final exit reason, holding duration, net realized P&L, realized R (null when the trade had no initial risk), total costs, and close-based excursion. Excursions are **close-based by construction**: they update only from causally revealed candle closes while the trade is open, never from a close after an intrabar stop or target already closed the trade. The UI labels them `MFE (close)` / `MAE (close)` and never presents them as exact intrabar values.

Each closed trade also stores a user `review_note` and `review_tags`, mutated through `PATCH /api/trades/{trade_id}/review` with bounded, trimmed, de-duplicated tags.

Complete closed-trade and fill history stays authoritative in normalized tables and is fetched through cursor pagination. Client arrays mirror the server bounds during ordinary stepping; explicitly loaded older pages are de-duplicated without entering routine mutation payloads.

Historical chart focus is a read-only bounded view, not replay mutation. The server returns at most 2,000 M1 source bars around a trade, clamps an active session to its latest revealed candle opening time, reports truncation, and anchors fills to source candles. The client exposes loading, error/retry, and truncation states. Selection generations prevent trade A from installing over trade B; a timeframe/indicator signature also prevents an old settings response from installing. Clearing focus restores the overlapping part of the pre-focus live time range when possible, otherwise it follows the current live edge.

## API contract and frontend types

The FastAPI OpenAPI document is the single source of truth for wire types. `backend/openapi.json` is committed and regenerated from the running app by `scripts/export_openapi.py`; CI fails if it drifts from the code. The frontend derives `frontend/src/api-types.ts` from that schema with `openapi-typescript`, and `frontend/src/types.ts` is a thin layer of semantic aliases over the generated types — the client no longer hand-maintains API models. CI regenerates and diffs both artifacts.

Playwright e2e tests (`frontend/e2e/`) exercise causal reveal timing, market-order and tiny-quantity close validation, review persistence, bounded historical focus, and history beyond both routine caps against deterministic fixtures through the real backend and frontend. A test-only CLI builds the large normalized ledger in one transaction; it is not a production endpoint. Each run uses an ephemeral data root, and CI uploads retained traces, failure screenshots, and a JSON report when browser tests fail.

## Alignment profiles

- `utc_aligned`: calendar boundaries in UTC.
- `new_york_close`: DST-aware `America/New_York` sessions anchored at 17:00.
- `custom_session_anchor`: reserved but rejected in v1.

The session snapshots the selected profile, immutable data version, contract multiplier, account conversion, and execution costs, preventing those inputs from changing during a replay.

## Extension points

Pure modules in `backend/app/domain.py`, `backend/app/timeframes.py`, `execution.py`, `indicators.py`, and `stats.py` are deliberately independent of FastAPI. Reveal-time semantics, execution precision, the statistics accumulator, and trade review are all defined there. Future backtests and traffic-light rules should call those functions rather than duplicating replay behavior.

