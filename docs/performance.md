# Replay performance and bounded-work evidence

Measured 2026-08-23 on the workstation described by the orchestration environment. Wall-clock values are diagnostic, not CI thresholds; stable page, row, statement, payload, and request bounds are the acceptance criteria.

## Method

### Payload and persisted-state harness

A temporary, uncommitted Python harness generated 2,600 deterministic M1 bars in an isolated data root, imported them through the real FastAPI application, created 500-bar and 2,000-bar sessions, stepped them through the real service/repository path, and measured:

- raw `TestClient` response-body bytes;
- `displayed_bars` length; and
- UTF-8 bytes in SQLite `replay_sessions.state_json`.

The same `/tmp/tpr_baseline_measure.py` harness ran before implementation and again from a clean final worktree with Python 3.12.13. It does not reuse a developer database.

### Stable work counters

Temporary instrumentation wrapped existing boundaries rather than replacing production behavior:

- `RangeBars` page loads and Parquet partition scans;
- SQLite trace callbacks for statements and hydrated rows;
- routine snapshot, save, pagination, and historical-focus response sizes;
- full-series, marker, style, indicator, and price-line work in the existing chart effect; and
- large-history fixture records and browser requests.

The permanent regression gates are the counter-based tests in `backend/tests/test_history_bound.py`, `test_history_pages.py`, `test_long_session_scale.py`, and `frontend/tests/store.test.ts`, plus the real-browser scenarios in `frontend/e2e/replay-workflow.spec.ts`.

### Timing samples

Backend samples used a deterministic 260,000-bar dataset spanning two yearly Parquet partitions. Frontend samples used real application CSS and Lightweight Charts in headless Chromium. Chart timings are 20-run medians with p95 recorded. These values identify disproportionate work; they are not portable latency guarantees.

## Before and after payloads

| Measurement | Baseline | Final | Result |
| --- | ---: | ---: | --- |
| 500-bar create response | 1,299 B | 1,307 B | +8 B |
| 500-bar update | 124,706 B | 124,714 B | +8 B; still exactly 500 bars |
| 500-bar snapshot | 125,118 B | 125,126 B | +8 B |
| 2,000-bar update | 496,031 B | 496,039 B | +8 B; still exactly 2,000 bars |
| 2,000-bar snapshot | 496,444 B | 496,452 B | +8 B |
| 500-bar `state_json` | 1,187 B | 1,187 B | unchanged |
| 2,000-bar `state_json` | 1,190 B | 1,190 B | unchanged |

The eight-byte wire increase is the explicit nullable-statistics contract. It does not expand chart or history bounds, and persisted scalar state is unchanged. The final 2,000-bar create response was 1,309 B; no baseline value was recorded for that case, so no comparison is claimed.

## Final bounded-work results

| Path | Dataset | Measured work | Timing evidence |
| --- | --- | --- | ---: |
| Routine M1 step | 260,000 M1 bars, 2,000-bar context | First step loaded one 1,024-bar page; ten following steps loaded zero new pages and scanned zero partitions from the session cache. | 33.7 ms median, 35.6 ms p95 |
| Maximum working-set step | 205 trades, 1,000 fills, SMA enabled, 2,000 chart bars | Working history stayed at 200 recent closed trades, every open trade, and 1,000 fills. | 50.8 ms |
| 1d displayed timeframe | 53,840 M1 source bars for context and warmup | One bounded partition-tail read; no complete-session history scan. | 45.9 ms |
| Cold reconciliation snapshot | 5 open + 200 recent closed trades, 1,000 fills | Four SELECTs: scalar snapshot, all open trades, closed `LIMIT 200`, fills `LIMIT 1000`; 1,205 history rows hydrated. | 766,163 B response, 32 ms |
| First capped save | 205 trades and 1,005 pre-prune fills | 1,215 statements, including one transaction and 1,212 inserts for normalized rows, event, and indicator work; the in-memory snapshot is then pruned to 1,000 fills. | 15.6 ms |
| Unchanged save | Pruned 205-trade / 1,000-fill working set | Four statements; zero trade or fill rewrites because fingerprints matched. | 13.9 ms |
| Historical chart focus | Older closed trade | One page read of at most 1,024 bars, two indexed fills, 2,356 B response; server maximum remains 2,000 M1 bars. | 6.7 ms |
| History pagination | 1,005 fills / 200+ trades | Fill page at most 500 rows; closed-trade page at most 200 rows; count/cursor checks remain bounded. | Not thresholded |
| SQLite initialization | Fresh database through ordered migrations | 72 statements across 11 explicit transactions. | 1.5 ms |
| Established WAL request | Existing SQLite connection | `PRAGMA journal_mode=WAL` no-op measured at 0.7 microseconds; retained because no measurable hot-path value supports a change. | 0.7 µs |

## Frontend update decision

The baseline chart effect replaces candle and SMA series, rebuilds markers, reads style tokens, and recreates open-trade price lines together. At 2,000 bars and 1,005 markers it measured 3.9 ms median and 4.9 ms p95:

- marker model construction: 2.7 ms;
- candle `setData`: 0.4 ms;
- marker installation: 0.2 ms;
- SMA `setData`: 0.5 ms; and
- price lines: below 0.1 ms.

A proposed incremental refactor was reverted. The final implementation intentionally retains the measured baseline update path: roughly 4 ms of chart work is small beside the 34–51 ms authoritative backend step, remains far below the existing 250 ms fastest playback interval, and changing transport/update semantics would add equivalence risk without a demonstrated user bottleneck. No chart-speed improvement is claimed.

## Large-history E2E setup

The final test-only CLI constructs 501 closed trades and 1,002 fills—over both routine caps—in one repository transaction. It uses the real domain models, accumulator reconstruction, normalized trade/fill tables, snapshot pruning, and optimistic revision update. It is never mounted as a production endpoint. The browser then uses the real API and frontend to verify:

- state hydration of 200 closed trades and 1,000 fills;
- two bounded 200-trade pages and one bounded fill page;
- de-duplicated 501-card historical review;
- continued live stepping and market entry after older history is loaded; and
- isolated temporary data roots between tests.

The clean final Playwright suite ran six tests with one worker in 21.0 seconds; the five-test baseline ran in 28.0 seconds. The suites differ and wall-clock variance is material, so this is diagnostic only—not a speedup claim. Stable evidence is the fixture record counts, page sizes, and absence of hundreds of HTTP setup mutations.

## Reproduction gates

From a locked Python 3.12 and Node 22 environment:

```bash
cd backend
uv sync --python 3.12 --locked --dev
uv run pytest tests/test_history_bound.py tests/test_history_pages.py tests/test_long_session_scale.py

cd ../frontend
npm ci
npm run test
npx playwright install chromium
npm run test:e2e
```

The one-off timing instrumentation is intentionally not a permanent benchmark suite: its wall-clock values would be hardware-sensitive and unsuitable as CI gates. Repeat optimization work only after a user-visible regression or the permanent counters show more pages, rows, payload, or client history than the documented bounds.

## Deferred optimizations

- Incremental chart-series and marker transport: deferred after the 3.9 ms measurement and reverted equivalence-risking patch.
- Additional chart protocol: deferred; current bounded payloads are adequate.
- Parquet read-path changes: deferred; routine steps page once then hit cache, and bounded history focus reads one page.
- SQLite connection/WAL restructuring: deferred; the WAL no-op is not the measured connection cost.
- Response-limit increases: rejected; they would weaken routine-state bounds rather than solve a measured bottleneck.
