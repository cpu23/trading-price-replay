import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import { ReplayChart } from "./Chart";
import { formatAdaptiveNumber, formatExecutionTime, formatMetricLabel, formatNumber, formatPrice, formatStatistic, historyCountLabel, replayProgress, utcClock, utcDateTime, validateOrderTicket } from "./helpers";
import { TradeRow } from "./TradeRow";
import { TradeReview } from "./TradeReview";
import { useReplayStore } from "./store";
import type { ReplayState, ReplayStats, Timeframe, Trade, TradeDirection } from "./types";

const TIMEFRAMES: Timeframe[] = ["1m", "5m", "15m", "1h", "4h", "1d"];
const STEP_SIZES = [1, 2, 3, 5, 10, 15];
const PLAYBACK_SPEEDS = [
  { label: "0.5×", delay: 2000 },
  { label: "1×", delay: 1000 },
  { label: "2×", delay: 500 },
  { label: "4×", delay: 250 },
];
const PRIMARY_STATS: (keyof ReplayStats)[] = [
  "balance",
  "equity",
  "net_pnl",
  "unrealized_pnl",
  "gross_pnl",
  "trading_costs",
  "commission_paid",
  "spread_cost",
  "slippage_cost",
];

export function ReplayWorkspace() {
  const replay = useReplayStore((state) => state.replay);
  return replay ? <ReplayWorkspaceContent replay={replay} /> : null;
}

function ReplayWorkspaceContent({ replay }: { replay: ReplayState }) {
  const symbols = useReplayStore((state) => state.symbols);
  const action = useReplayStore((state) => state.action);
  const leave = useReplayStore((state) => state.leave);
  const playing = useReplayStore((state) => state.playing);
  const setPlaying = useReplayStore((state) => state.setPlaying);
  const busy = useReplayStore((state) => state.busy);
  const error = useReplayStore((state) => state.error);
  const clearError = useReplayStore((state) => state.clearError);

  const [quantity, setQuantity] = useState(1);
  const [stop, setStop] = useState("");
  const [target, setTarget] = useState("");
  const [ticketError, setTicketError] = useState("");
  const [playbackDelay, setPlaybackDelay] = useState(1000);
  const [confirmCloseAll, setConfirmCloseAll] = useState(false);
  const [chartFocus, setChartFocus] = useState<Trade | null>(null);

  const metadata = symbols.find((item) => item.symbol === replay.symbol);
  // Formatting follows the session's pinned snapshot; legacy sessions fall back
  // to the current symbol row.
  const precision = replay.price_precision ?? metadata?.price_precision ?? 2;
  const pnlCurrency = replay.pnl_currency ?? metadata?.pnl_currency ?? replay.account_currency;
  const contractMultiplier = replay.contract_multiplier ?? metadata?.contract_multiplier;
  const openTrades = replay.trades.filter((trade) => trade.status === "open");
  const closedTrades = replay.trades.filter((trade) => trade.status === "closed");
  const progress = replayProgress(replay.current_index, replay.remaining_bars);
  const canEnter = replay.status !== "completed" && replay.current_index >= 0 && replay.current_price !== null;

  const step = useCallback(async () => {
    await action(() => api.post<ReplayState>(`/api/replay/sessions/${replay.id}/step`));
  }, [action, replay.id]);

  const placeOrder = useCallback(async (direction: TradeDirection) => {
    if (replay.status === "completed") {
      setTicketError("Replay is complete; new entries are disabled.");
      return;
    }
    const validationError = validateOrderTicket({
      direction,
      quantity,
      stop,
      target,
      currentPrice: replay.current_price,
      canEnter,
    });
    if (validationError) {
      setTicketError(validationError);
      return;
    }
    setTicketError("");
    await action(() => api.post<ReplayState>(`/api/replay/sessions/${replay.id}/orders/market`, {
      direction,
      quantity,
      stop_price: stop.trim() ? Number(stop) : null,
      target_price: target.trim() ? Number(target) : null,
    }));
  }, [action, canEnter, quantity, replay.current_price, replay.id, replay.status, stop, target]);

  useEffect(() => {
    if (!playing || busy || replay.status === "completed") return;
    const timer = window.setTimeout(() => void step(), playbackDelay);
    return () => window.clearTimeout(timer);
  }, [busy, playbackDelay, playing, replay.status, step]);

  useEffect(() => {
    if (replay.status === "completed" && playing) setPlaying(false);
  }, [playing, replay.status, setPlaying]);

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if (event.repeat) return;
      const targetElement = event.target;
      if (targetElement instanceof HTMLElement && (
        targetElement.isContentEditable
        || ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(targetElement.tagName)
      )) return;

      if (event.code === "Space") {
        event.preventDefault();
        if (!busy && replay.status !== "completed") setPlaying(!playing);
        return;
      }
      if (busy) return;
      if (event.code === "ArrowRight" && replay.status !== "completed") {
        event.preventDefault();
        void step();
      } else if (event.key.toLowerCase() === "b") {
        void placeOrder("long");
      } else if (event.key.toLowerCase() === "s") {
        void placeOrder("short");
      }
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [busy, placeOrder, playing, replay.status, setPlaying, step]);

  const displayedStats = useMemo(() => {
    const primary = PRIMARY_STATS
      .filter((name) => typeof replay.stats[name] === "number")
      .map((name) => [name, replay.stats[name]] as const);
    const secondary = Object.entries(replay.stats)
      .filter(([name]) => !PRIMARY_STATS.includes(name as keyof ReplayStats))
      .sort(([left], [right]) => left.localeCompare(right));
    return { primary, secondary };
  }, [replay.stats]);

  async function closeAll() {
    setConfirmCloseAll(false);
    await action(() => api.post<ReplayState>(`/api/replay/sessions/${replay.id}/close-all`));
  }

  return (
    <main className="app workspace">
      <header className="workspace-header">
        <div className="instrument-title">
          <div>
            <span className={`status-dot status-${replay.status}`} aria-hidden="true" />
            <span className="eyebrow">{replay.status === "completed" ? "REPLAY COMPLETE" : playing ? "PLAYING" : "PAUSED"}</span>
          </div>
          <h1>{replay.symbol}</h1>
          <p>
            {metadata ? `${metadata.asset_class} · ${pnlCurrency} P&L · ×${contractMultiplier}` : replay.account_currency}
          </p>
        </div>
        <div className="market-clock">
          <span>Market time (UTC)</span>
          <strong>
            {replay.current_market_time
              ? `${utcDateTime(replay.current_market_time)} UTC`
              : "No candle revealed"}
          </strong>
          {replay.current_market_time && replay.current_candle_time && (
            <span className="market-clock-candle">M1 candle {utcClock(replay.current_candle_time)} UTC opened</span>
          )}
          <span>{replay.remaining_bars.toLocaleString()} bars remaining</span>
        </div>
        <button className="button-quiet leave-button" type="button" onClick={leave}>Leave replay</button>
      </header>

      <div className="replay-progress" aria-label={`${progress.toFixed(0)} percent of replay revealed`}>
        <progress max="100" value={progress}>{progress.toFixed(0)}%</progress>
      </div>

      <section className="control-bar" aria-label="Replay controls">
        <div className="segmented" role="group" aria-label="Chart timeframe">
          {TIMEFRAMES.map((timeframe) => (
            <button
              key={timeframe}
              type="button"
              className={timeframe === replay.visible_timeframe ? "active" : ""}
              aria-pressed={timeframe === replay.visible_timeframe}
              disabled={busy}
              onClick={() => void action(() => api.patch<ReplayState>(`/api/replay/sessions/${replay.id}/settings`, { visible_timeframe: timeframe }))}
            >
              {timeframe}
            </button>
          ))}
        </div>
        <div className="control-field">
          <label htmlFor="step-size">Step size</label>
          <select
            id="step-size"
            value={replay.advance_step_minutes}
            disabled={busy || replay.status === "completed"}
            onChange={(event) => void action(() => api.patch<ReplayState>(`/api/replay/sessions/${replay.id}/settings`, { advance_step_minutes: Number(event.target.value) }))}
          >
            {STEP_SIZES.map((minutes) => <option key={minutes} value={minutes}>{minutes} min</option>)}
          </select>
        </div>
        <div className="control-field">
          <label htmlFor="playback-speed">Playback</label>
          <select id="playback-speed" value={playbackDelay} onChange={(event) => setPlaybackDelay(Number(event.target.value))}>
            {PLAYBACK_SPEEDS.map((speed) => <option key={speed.delay} value={speed.delay}>{speed.label}</option>)}
          </select>
        </div>
        <div className="transport-controls">
          <button
            type="button"
            onClick={() => setPlaying(!playing)}
            disabled={replay.status === "completed" || (busy && !playing)}
            aria-label={playing ? "Pause replay" : "Play replay"}
          >
            {playing ? "Pause" : "Play"}
          </button>
          <button className="button-primary" type="button" onClick={() => void step()} disabled={busy || replay.status === "completed"}>
            {busy ? "Working…" : "Step"}
          </button>
        </div>
        <span className="shortcut-hint" aria-label="Keyboard shortcuts">Space play · → step · B buy · S sell</span>
      </section>

      {error && (
        <div className="alert alert-error alert-dismissible" role="alert">
          <span>{error}</span>
          <button type="button" onClick={clearError} aria-label="Dismiss error">Dismiss</button>
        </div>
      )}
      {replay.warnings?.map((warning) => <div className="alert alert-warning" role="status" key={warning}>{warning}</div>)}
      {replay.status === "completed" && (
        <div className="alert alert-info" role="status">
          Replay complete. New entries and stepping are disabled; open positions can still be closed at the final causal price.
        </div>
      )}

      <ReplayChart
        replay={replay}
        precision={precision}
        focus={chartFocus ? { from: chartFocus.entry_time, to: chartFocus.exit_time ?? chartFocus.entry_time } : null}
        onClearFocus={() => setChartFocus(null)}
      />

      <section className="cost-strip" aria-label="Execution configuration">
        <div><span>Initial balance</span><strong>{formatNumber(replay.initial_balance)} {replay.account_currency}</strong></div>
        <div><span>Full spread</span><strong>{formatPrice(replay.spread, precision)}</strong></div>
        <div><span>Slippage / execution</span><strong>{formatPrice(replay.slippage, precision)}</strong></div>
        <div><span>Commission / qty / side</span><strong>{formatAdaptiveNumber(replay.commission_per_quantity)} {replay.account_currency}</strong></div>
      </section>

      <div className="workspace-grid">
        <section className="panel order-ticket" aria-labelledby="ticket-heading">
          <div className="panel-heading">
            <div>
              <p className="section-kicker">EXECUTION</p>
              <h2 id="ticket-heading">Order ticket</h2>
            </div>
            <strong className="ticket-price">{formatPrice(replay.current_price, precision)}</strong>
          </div>
          {!canEnter && replay.status !== "completed" && (
            <p className="inline-message">Step once to reveal a causal entry price.</p>
          )}
          <div className="field-group">
            <label htmlFor="order-quantity">Quantity</label>
            <input id="order-quantity" type="number" min="0.00000001" step="any" value={quantity} onChange={(event) => setQuantity(Number(event.target.value))} />
          </div>
          <div className="protection-grid">
            <div className="field-group">
              <label htmlFor="order-stop">Stop price <span>(optional)</span></label>
              <input id="order-stop" inputMode="decimal" value={stop} onChange={(event) => setStop(event.target.value)} />
            </div>
            <div className="field-group">
              <label htmlFor="order-target">Target price <span>(optional)</span></label>
              <input id="order-target" inputMode="decimal" value={target} onChange={(event) => setTarget(event.target.value)} />
            </div>
          </div>
          {ticketError && <p className="inline-message message-error" role="alert">{ticketError}</p>}
          <div className="order-buttons">
            <button className="button-buy" type="button" onClick={() => void placeOrder("long")} disabled={busy || !canEnter}>Buy / Long</button>
            <button className="button-sell" type="button" onClick={() => void placeOrder("short")} disabled={busy || !canEnter}>Sell / Short</button>
          </div>
        </section>

        <section className="panel stats-panel" aria-labelledby="stats-heading">
          <div className="panel-heading">
            <div>
              <p className="section-kicker">PERFORMANCE</p>
              <h2 id="stats-heading">Account statistics</h2>
            </div>
            <span className="panel-note">Net of costs</span>
          </div>
          <dl className="metric-list metric-primary">
            {displayedStats.primary.map(([name, value]) => (
              <div key={name}>
                <dt>{formatMetricLabel(name)}</dt>
                <dd className={name.includes("pnl") && value !== 0 ? (value > 0 ? "positive" : "negative") : ""}>
                  {formatNumber(value)} {replay.account_currency}
                </dd>
              </div>
            ))}
          </dl>
          {displayedStats.secondary.length > 0 && (
            <details>
              <summary>Additional statistics</summary>
              <dl className="metric-list">
                {displayedStats.secondary.map(([name, value]) => (
                  <div key={name}><dt>{formatMetricLabel(name)}</dt><dd>{formatStatistic(name, value, replay.account_currency)}</dd></div>
                ))}
              </dl>
            </details>
          )}
        </section>

        <section className="panel tools-panel" aria-labelledby="tools-heading">
          <div className="panel-heading">
            <div>
              <p className="section-kicker">DISPLAY</p>
              <h2 id="tools-heading">Chart tools</h2>
            </div>
          </div>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={replay.enabled_indicators.includes("sma_close_35")}
              disabled={busy}
              onChange={() => void action(() => api.post<ReplayState>(`/api/replay/sessions/${replay.id}/indicators/sma_close_35/toggle`))}
            />
            <span><strong>SMA 35 close</strong><small>Causal moving average on the selected timeframe</small></span>
          </label>
          <div className="close-all-area">
            <span>{openTrades.length} open trade{openTrades.length === 1 ? "" : "s"}</span>
            {confirmCloseAll ? (
              <div className="delete-confirm">
                <span>Close every open trade at the current causal price?</span>
                <button className="button-danger button-small" type="button" onClick={() => void closeAll()} disabled={busy}>Confirm close all</button>
                <button className="button-small" type="button" onClick={() => setConfirmCloseAll(false)}>Cancel</button>
              </div>
            ) : (
              <button type="button" onClick={() => setConfirmCloseAll(true)} disabled={busy || openTrades.length === 0 || replay.current_index < 0}>
                Close all positions
              </button>
            )}
          </div>
        </section>
      </div>

      <section className="panel positions-panel" aria-labelledby="positions-heading">
        <div className="panel-heading">
          <div>
            <p className="section-kicker">POSITIONS</p>
            <h2 id="positions-heading">Open trades</h2>
          </div>
          <span className="panel-note">Stop-first on ambiguous candles</span>
        </div>
        {openTrades.length === 0 ? (
          <div className="empty-state compact"><strong>No open trades</strong><span>Use the order ticket or B / S shortcuts after revealing a price.</span></div>
        ) : openTrades.map((trade) => (
          <TradeRow key={trade.id} trade={trade} replay={replay} precision={precision} busy={busy} action={action} />
        ))}
      </section>

      <div className="ledger-grid">
        <section className="panel table-panel" aria-labelledby="fills-heading">
          <div className="panel-heading">
            <div>
              <p className="section-kicker">AUDIT</p>
              <h2 id="fills-heading">Fill ledger</h2>
            </div>
            <span className="panel-note">{historyCountLabel(replay.fills_total, replay.fills.length, "fills")}</span>
          </div>
          {replay.fills.length === 0 ? (
            <div className="empty-state compact"><strong>No fills yet</strong><span>Entries, partial closes, stops, and targets appear here.</span></div>
          ) : (
            <div className="table-scroll">
              <table>
                <thead><tr><th>Time (UTC)</th><th>Reason</th><th>Qty</th><th>Market</th><th>Fill</th><th>Gross</th><th>Commission</th><th>Spread</th><th>Slippage</th><th>Net</th></tr></thead>
                <tbody>
                  {replay.fills.slice().reverse().map((fill) => (
                    <tr key={fill.id}>
                      <td><time dateTime={fill.timestamp} title={fill.time_precision === null || fill.time_precision === "legacy" ? "Precise execution timing unavailable for this legacy fill" : undefined}>{formatExecutionTime(fill)}</time></td>
                      <td><span className="reason-chip">{fill.reason.replaceAll("_", " ")}</span></td>
                      <td>{formatAdaptiveNumber(fill.quantity)}</td>
                      <td>{formatPrice(fill.market_price, precision)}</td>
                      <td>{formatPrice(fill.price, precision)}</td>
                      <td>{formatNumber(fill.gross_pnl)}</td>
                      <td>{formatAdaptiveNumber(fill.commission)}</td>
                      <td>{formatNumber(fill.spread_cost)}</td>
                      <td>{formatNumber(fill.slippage_cost)}</td>
                      <td className={fill.pnl >= 0 ? "positive" : "negative"}>{formatNumber(fill.pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="panel table-panel" aria-labelledby="closed-heading">
          <div className="panel-heading">
            <div>
              <p className="section-kicker">HISTORY</p>
              <h2 id="closed-heading">Closed trades</h2>
            </div>
            <span className="panel-note">{historyCountLabel(replay.closed_trades_total, closedTrades.length, "closed")}</span>
          </div>
          {closedTrades.length === 0 ? (
            <div className="empty-state compact"><strong>No closed trades</strong><span>Completed positions will remain available for review.</span></div>
          ) : (
          <div className="table-scroll review-list">
            {closedTrades.slice().reverse().map((trade) => (
              <TradeReview
                key={trade.id}
                trade={trade}
                replay={replay}
                precision={precision}
                busy={busy}
                action={action}
                onFocus={(item) => setChartFocus(item)}
              />
            ))}
          </div>
          )}
        </section>
      </div>
    </main>
  );
}
