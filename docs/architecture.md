# Architecture

Normalized UTC one-minute Parquet is the canonical market source. Each successful import publishes an immutable version. SQLite stores the active version pointer, symbol metadata, import records, replay snapshots, orders, fills, and events.

The backend owns replay time, bounded market-data reads, resampling, fills, indicators, and statistics. The React client requests authoritative state and projects it into Lightweight Charts.

## Causality

At session creation, only candles before the selected start are available as context. A manual or client-paced step reveals the next N one-minute candles. Each minute is processed sequentially before state is committed, so a stop hit during a multi-minute step fills at the correct minute.

Higher-timeframe candles are always derived from context plus revealed one-minute candles. The active bucket is partial; earlier buckets are finalized. SMA 35 uses only these causal displayed candles.

Chart payloads contain only the configured trailing context window. Indicator warmup is loaded separately, so a long replay does not make every step response grow while SMA values remain causal.

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

Realized balance is the sum of committed fill P&L. Unrealized P&L deducts the projected exit-side costs for each open remainder, making reported equity a liquidation value.

## Alignment profiles

- `utc_aligned`: calendar boundaries in UTC.
- `new_york_close`: DST-aware `America/New_York` sessions anchored at 17:00.
- `custom_session_anchor`: reserved but rejected in v1.

The session snapshots the selected profile, immutable data version, contract multiplier, account conversion, and execution costs, preventing those inputs from changing during a replay.

## Extension points

Pure modules in `backend/app/timeframes.py`, `execution.py`, `indicators.py`, and `stats.py` are deliberately independent of FastAPI. Future backtests and traffic-light rules should call those functions rather than duplicating replay behavior.

