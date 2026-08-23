# Manual replay market review

Access date: 2026-08-23

## Purpose and scope

Price Replay Lab is a local-first workstation for deliberate practice on historical one-minute prices. Its essential loop is to configure a session, reveal prices causally, make and manage discretionary trades, review outcomes, and resume authoritative state later. This review compares replay mechanisms and communication patterns, not vendor feature counts.

Competitor research does not override this project's no-new-features constraint. The findings below are filtered to polish of existing controls, execution, review, persistence, diagnostics, and performance. Rewind, pending orders, tick replay, multi-symbol or multi-chart replay, drawing, broker connectivity, live trading, automated strategies, new analytics categories, and other excluded product areas remain deferred even where competitors provide them.

Official vendor documentation and help centers were preferred. Undated pages are identified as such. Marketing claims about data breadth, accuracy, speed, or outcomes are not treated as independently verified facts.

## Comparison dimensions

| Dimension | Vendor evidence | Applicable lesson for Price Replay Lab |
| --- | --- | --- |
| Replay clock and controls | TradingView, FX Replay, TradingSim, NinjaTrader, and Forex Tester expose play/pause, forward stepping, and speed semantics. Quantower exposes start/current/end progress in its backtest tooling. | Keep the backend clock authoritative. Make existing play, pause, step, busy, and completed states consistent. Explain the existing step and playback controls; expose their keyboard shortcuts accessibly. |
| Causality and fidelity | NinjaTrader distinguishes exact event replay from estimated historical fills. Quantower documents accuracy/performance tradeoffs between tick, OHLC, open, and close models. | Continue to identify M1 timestamps as candle opens. Label revealed-close executions as exact and ordinary intrabar touches as interval precision. Never imply that M1 OHLC identifies an exact intrabar sequence. |
| Ambiguous fills | NinjaTrader and Quantower disclose historical fill assumptions and modeling limits. | Preserve deterministic conservative same-bar stop/target behavior and document it near the execution model. Do not add invented intrabar paths. |
| Session continuation | TradingView offers an explicit continue-versus-new workflow and documents what is restored. FX Replay treats sessions as durable review objects. | Preserve complete authoritative backend state and make resume/reconciliation failures actionable. Do not rely on client-only replay state. |
| Keyboard interaction | TradingView, FX Replay, TradingSim, NinjaTrader, and Forex Tester document replay shortcuts or place them in tooltips/help surfaces. | Keep shortcuts disabled while typing. Add `aria-keyshortcuts` and shortcut text to the controls that already implement Space, ArrowRight, B, and S. |
| Historical review | FX Replay provides paginated trade review and notes. TradingView organizes session results and trade lists. | Keep normalized history authoritative and routine state bounded. Make older-history loading, bounded chart focus, truncation, loading, and errors explicit. Preserve source-candle marker anchoring. |
| Statistics | Competitors provide session summaries, trade lists, and empty-state guidance, but their zero/undefined policies are often incomplete or product-specific. | Define every existing metric. Use zero for counts/totals and null for ratios or averages without observations. Render unavailable values without inventing evidence. |
| Data availability | TradingView reports unavailable replay depth. NinjaTrader exposes available replay data. Forex Tester visualizes missing data quality. | Validate selected ranges and explain bounded or truncated historical views. Preserve accepted market-data gaps; do not silently imply complete coverage. |
| Accuracy/performance disclosure | Quantower explicitly trades finer input for slower processing. NinjaTrader distinguishes fast historical estimates from exact replay data. | Measure work, not only time. Preserve bounded chart payloads, bounded history hydration, bounded caches, and sequential per-bar execution. Adopt optimizations only when equivalence is demonstrable. |
| Failure communication | FX Replay documents end-of-range, missing-data, frozen playback, and order-mode failures. TradingView documents unsupported replay contexts. | Surface focus, reconciliation, conflict, validation, and completion errors in the existing workspace instead of silently clearing state or continuing playback. |

## Vendor-neutral design principles

1. **One authoritative replay clock.** Presentation timers request work; they do not own market time or trading state.
2. **Separate simulation mutation from chart navigation.** Historical focus may inspect already-authorized data but must not reveal future bars or mutate the session.
3. **State precision honestly.** Exact timestamps require exact evidence. Interval-only executions retain interval precision, and legacy rows do not gain fabricated precision.
4. **Publish assumptions with results.** Spread, slippage, commission, OHLC limitations, same-bar precedence, and close-based excursions must remain visible and reproducible.
5. **Resume from authoritative storage.** A page reload or client reconciliation installs a server snapshot with an optimistic revision; stale clients must not win conflicts.
6. **Bound routine work.** Stepping and routine state load the chart window, all open trades, and capped recent history—not complete replay or ledger history.
7. **Identify bounded views.** A historical chart window or recent-history list must say when it is truncated and must derive its viewport from returned data.
8. **Treat unavailable statistics as unavailable.** Absence of observations is not a zero ratio or zero average. Wire values must remain finite JSON.
9. **Make controls self-describing.** Busy, paused, playing, and completed states must agree across buttons and keyboard handling. Existing shortcuts belong in labels, tooltips, and accessibility metadata.
10. **Prefer deterministic recovery over retries.** Stop playback on conflict or reconciliation failure, show the error, and let the next authoritative snapshot restore a known state.
11. **Measure equivalent work.** Pages, partitions, rows, series replacements, marker rebuilds, and fixture mutations are more stable evidence than wall-clock thresholds alone.

## Existing strengths

- The backend owns replay time, trading mutations, resampling, fills, and statistics; the React client projects authoritative state.
- `bar_reveal_time` separates each M1 candle's opening timestamp from the close reveal time used by market executions.
- Exact, bar-interval, and legacy execution precision are separate wire semantics. Chart markers use source candle opening times rather than execution timestamps.
- Same-bar stop/target behavior is deterministic and conservative without inventing an intrabar path.
- Imports publish immutable data versions. Sessions pin data version, contract multiplier, display precision, currency, profile, costs, and account inputs.
- SQLite commits session snapshots, trades, fills, events, indicators, and order audit rows transactionally. Optimistic revisions detect concurrent writers.
- Routine state keeps every open trade while bounding recent closed trades at 200 and fills at 1,000. Complete history remains in normalized tables and is paginated.
- The statistics accumulator and bounded state snapshot prevent routine step and state paths from scanning complete history.
- Market-data reads are paged and cached with explicit bounds; chart payloads are capped at 500–2,000 M1 bars.
- OpenAPI and frontend types are generated and checked for drift. Migrations are ordered, transactional, forward-only, and covered by backup/restore behavior.

These strengths provide stronger causality, persistence, and precision guarantees than the mechanisms described in several vendor help pages. The quality pass should protect them rather than imitate broader competitor feature sets.

## Baseline weaknesses and pass decisions

These repository-observed targets drove the quality pass; they are not a competitor backlog:

| Baseline observation | Decision and final disposition |
| --- | --- |
| Binary-float subtraction could leave quantity dust, reject a displayed remainder, or fail exact-zero finalization. | **Implemented.** One backend policy now subtracts decimal renderings, canonicalizes only non-material ULP-scale residue, falls back to exact comparison when that window would be material, books the actual remainder on final close, and rejects genuine oversize. Frontend drafts preserve decimal and exponent input without becoming authoritative. |
| Ratios and averages with no observations were serialized as zero. | **Implemented.** Counts and totals remain zero; undefined ratios and averages are nullable, non-finite derived R values are excluded, and every API statistic is finite or null. |
| Historical focus could hide failures, mishandle truncated spans, race newer focus or settings, and lose the live viewport. | **Implemented.** Focus has visible loading, error, retry, and truncation states; requests are guarded by selection generation and chart-setting signature; returned windows clamp the viewport; returning restores an overlapping pre-focus live range or the current live edge. |
| Existing Space, ArrowRight, B, and S shortcuts lacked control-level accessibility metadata. | **Implemented.** Existing behavior is unchanged, but controls expose `aria-keyshortcuts`, labels/tooltips identify the keys, and editable fields continue to suppress shortcuts. |
| The chart effect rebuilt full series data, markers, style lookups, and price lines together. | **Measured and deferred.** The final full replacement path measured 3.9 ms median at 2,000 bars and 1,005 markers; backend stepping remained the larger cost. An incremental refactor was reverted because the measured benefit did not justify its equivalence risk. |
| Large-history browser setup and CI failures lacked deterministic work-count evidence and retained diagnostics. | **Implemented.** A test-only CLI seeds 501 closed trades and 1,002 fills through the real repository/domain serialization in one transaction; the browser still exercises the real API and frontend. CI retains Playwright traces and screenshots on failure. |
| Backend dependency and lint configuration needed an import audit and restrained gate. | **Implemented.** Unused SQLAlchemy, Greenlet, and multipart installation were removed; Ruff checks only the selected correctness rules and runs in CI and `make verify`. |

## Explicitly deferred features

The following competitor capabilities are evidence only and are not implementation targets for this pass:

- multi-symbol replay, synchronized multi-chart layouts, scanners, DOM, market depth, and time-and-sales;
- random or blind replay starts, timeline scrubbing, backward stepping, rewind, and trade restoration after rewind;
- drawing tools, bookmarks as a new review system, screenshots, rich-text journals, and strategy checklists;
- pending limit, stop-entry, stop-limit, bracket, OCO, or OTO orders;
- tick replay, generated ticks, bid/ask event streams, Level II, order flow, or alternative fill-resolution modes;
- new indicators, automated strategies, backtest optimization, Monte Carlo, prop-firm modes, AI analysis, or mentoring;
- broker connections, live trading, public accounts, cloud synchronization, collaboration, or leaderboards;
- new analytics categories, strategy dashboards, session-hours analytics, or new export formats;
- multi-asset breadth, vendor data subscriptions, plan tiers, billing, and marketing claims.

## Sources

All sources accessed 2026-08-23.

### FX Replay

- FX Replay, “How to Use Bar Replay / Right-Click Replay,” updated 2025-08-21: <https://support.fxreplay.com/articles/how-to-use-bar-replay---right-click-replay>
- FX Replay, “How to Use Keyboard Shortcuts & Tooltips,” updated 2025-08-19: <https://support.fxreplay.com/articles/how-to-use-keyboard-shortcuts-tooltips>
- FX Replay, “Available hotkeys for FX Replay,” updated 2025-02-06: <https://support.fxreplay.com/articles/available-hotkeys-for-fx-replay>
- FX Replay, “Go To Date Feature,” updated 2026-04-15: <https://support.fxreplay.com/articles/go-to-date-feature>
- FX Replay, “General Session Basics,” updated 2025-05-01: <https://support.fxreplay.com/articles/general-session-basics>
- FX Replay, “Trades and Logging,” updated 2025-05-06: <https://support.fxreplay.com/articles/trades-and-logging>
- FX Replay, “Session Stats Overview,” updated 2025-05-01: <https://support.fxreplay.com/articles/session-stats-overview>
- FX Replay, “Session Playback and Skip Button Not Working — Troubleshooting,” updated 2026-06-11: <https://support.fxreplay.com/articles/session-playback-and-skip-button-not-working-troubleshooting>
- FX Replay, “What broker data sources does FX Replay use for its charts?”, updated 2025-02-06: <https://support.fxreplay.com/articles/what-broker-data-sources-does-fx-replay-use-for-its-charts>

### TradingView

TradingView help pages below show no publication or update date.

- TradingView, “Bar Replay: how and why to test a strategy in the past”: <https://www.tradingview.com/support/solutions/43000712747-bar-replay-how-and-why-to-test-a-strategy-in-the-past/>
- TradingView, “How do I turn Bar Replay on?”: <https://www.tradingview.com/support/solutions/43000474024-how-do-i-turn-bar-replay-on/>
- TradingView, “Learn to trade on historical data”: <https://www.tradingview.com/support/solutions/43000691889-learn-to-trade-on-historical-data/>
- TradingView, “How much data is available for Bar Replay?”: <https://www.tradingview.com/support/solutions/43000692816-how-much-data-is-available-for-bar-replay/>
- TradingView, “How to select replay interval for the Bar Replay”: <https://www.tradingview.com/support/solutions/43000739158-how-to-select-replay-interval-for-the-bar-replay/>
- TradingView, “Bar Replay doesn't work, there is no Replay toolbar”: <https://www.tradingview.com/support/solutions/43000475470-bar-replay-doesn-t-work-there-is-no-replay-toolbar/>

### NinjaTrader

NinjaTrader 8 Help Guide pages below are continuously maintained but show no per-page date.

- NinjaTrader, “Playback”: <https://ninjatrader.com/support/helpGuides/nt8/playback.htm>
- NinjaTrader, “Set Up” for Playback Connection: <https://ninjatrader.com/support/helpGuides/nt8/set_up12.htm>
- NinjaTrader, “Understanding Historical Fill Processing”: <https://ninjatrader.com/support/helpGuides/nt8/understanding_historical_fill_.htm>
- NinjaTrader, “Backtest a Strategy”: <https://ninjatrader.com/support/helpGuides/nt8/backtest_a_strategy.htm>
- NinjaTrader, “Tick Replay”: <https://ninjatrader.com/support/helpGuides/nt8/tick_replay.htm>

### Forex Tester

The official desktop manual pages are undated, carry a 2006–2026 copyright, and warn that they describe the older desktop product rather than Forex Tester Online.

- Forex Tester, “Testing process”: <https://desktop.forextester.com/testing>
- Forex Tester, “Creating a new project”: <https://desktop.forextester.com/newproject>
- Forex Tester, “Data center”: <https://desktop.forextester.com/datacenter>
- Forex Tester, “Saving projects”: <https://desktop.forextester.com/projects>
- Forex Tester, “Placing Orders”: <https://desktop.forextester.com/placeorder>

### TradingSim

TradingSim provides product pages and vendor articles rather than a formal help center; product breadth and data claims remain marketing claims.

- TradingSim, product homepage, undated: <https://www.tradingsim.com/>
- TradingSim, “Features,” undated: <https://www.tradingsim.com/features>
- TradingSim, demo and in-product hotkeys, undated; JavaScript application not fully verifiable in reader mode: <https://app.tradingsim.com/demo/>
- TradingSim, Al Hill, revised by Kunal Vakil, “Day Trading Simulator: Practice Without Risking Real Money in 2026,” published 2026-07-19: <https://www.tradingsim.com/blog/day-trading-simulator>

### Quantower

Quantower help pages below show no per-page date. The older History Player article is retained only where current help corroborates the mechanism.

- Quantower, “Market Replay”: <https://help.quantower.com/quantower/trading-panels/market-replay.md>
- Quantower, “Trading simulator”: <https://help.quantower.com/quantower/trading-panels/trading-simulator.md>
- Quantower, “Backtest & Optimize”: <https://help.quantower.com/quantower/quantower-algo/backtest-and-optimize.md>
- Quantower, “Account performance”: <https://help.quantower.com/quantower/informational-panels/account-perfomance.md>
- Quantower, “Plugin for manual backtesting — a brief review of Market Replay,” published 2018-05-28: <https://www.quantower.com/blog/software-for-manual-backtesting-a-brief-review-of-history-player-plugin>

## Research limitations

- No paid competitor application was exercised. Mechanisms are based on vendor documentation, official product surfaces, and vendor articles.
- TradingView and Quantower help pages expose no reliable per-page dates.
- TradingSim claims about synchronization, data depth, and execution data were not independently verified.
- Forex Tester desktop documentation is official but explicitly identifies itself as the older product line.
- FX Replay shortcut documentation has changed over time; the durable lesson is discoverability and metadata, not copying a particular key binding.
