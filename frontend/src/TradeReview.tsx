import { useEffect, useState } from "react";
import { api } from "./api";
import { formatAdaptiveNumber, formatDuration, formatExecutionTime, formatNumber, formatPrice, parseTags, utcDateTime } from "./helpers";
import type { ReplayState, Trade } from "./types";

type TradeReviewProps = {
  trade: Trade;
  replay: ReplayState;
  precision: number;
  busy: boolean;
  action: (call: () => Promise<ReplayState>) => Promise<void>;
  onFocus: (trade: Trade) => void;
};

export function TradeReview({ trade, replay, precision, busy, action, onFocus }: TradeReviewProps) {
  const [note, setNote] = useState(trade.review_note);
  const [tagInput, setTagInput] = useState("");
  const [saved, setSaved] = useState(false);

  // Re-sync the draft when the server-side review changes (save, reload,
  // or another tab writing through the same session).
  useEffect(() => {
    setNote(trade.review_note);
    setSaved(false);
  }, [trade.id, trade.review_note]);

  const currency = replay.account_currency;
  const realizedR = trade.initial_risk ? trade.realized_pnl / trade.initial_risk : null;
  const durationSeconds = trade.exit_time
    ? (new Date(trade.exit_time).getTime() - new Date(trade.entry_time).getTime()) / 1000
    : null;
  const totalCosts = trade.total_commission + trade.total_spread_cost + trade.total_slippage_cost;
  const grossPnl = trade.realized_pnl + totalCosts;
  const tags = trade.review_tags ?? [];
  const dirty = note !== trade.review_note || tagInput.trim() !== "";
  const exitLabel = formatExecutionTime({
    timestamp: trade.exit_time ?? trade.entry_time,
    time_precision: trade.exit_time_precision,
    execution_window_start: trade.exit_window_start,
    execution_window_end: trade.exit_window_end,
  });

  function markEditing() {
    if (saved) setSaved(false);
  }

  async function save() {
    setSaved(false);
    await action(() => api.patch<ReplayState>(`/api/trades/${trade.id}/review`, {
      session_id: replay.id,
      review_note: note,
      review_tags: parseTags(`${tags.join(", ")} ${tagInput}`),
    }));
    setTagInput("");
    setSaved(true);
  }

  return (
    <article className="trade-review" aria-labelledby={`review-${trade.id}`}>
      <div className="trade-review-summary">
        <div>
          <span className={`direction-badge direction-${trade.direction}`}>{trade.direction}</span>
          <h3 id={`review-${trade.id}`}>{formatAdaptiveNumber(trade.initial_quantity)} closed</h3>
        </div>
        <dl className="review-metrics">
          <div>
            <dt>Entry</dt>
            <dd>
              {formatPrice(trade.entry_market_price, precision)}
              <span className="review-sub"> {utcDateTime(trade.entry_time)} UTC</span>
            </dd>
          </div>
          <div>
            <dt>Exit</dt>
            <dd>
              {formatPrice(trade.exit_market_price, precision)}
              <span className="review-sub"> {exitLabel}</span>
            </dd>
          </div>
          <div>
            <dt>Duration</dt>
            <dd>{durationSeconds === null ? "—" : formatDuration(durationSeconds)}</dd>
          </div>
          <div>
            <dt>Reason</dt>
            <dd><span className="reason-chip">{(trade.final_exit_reason ?? "unknown").replaceAll("_", " ")}</span></dd>
          </div>
          <div>
            <dt>Net P&L</dt>
            <dd className={trade.realized_pnl >= 0 ? "positive" : "negative"}>
              {formatNumber(trade.realized_pnl)} {currency}
            </dd>
          </div>
          <div>
            <dt>Realized R</dt>
            <dd className={realizedR !== null && realizedR < 0 ? "negative" : ""}>
              {realizedR === null ? "—" : `${formatNumber(realizedR)} R`}
            </dd>
          </div>
          <div>
            <dt>MFE (close)</dt>
            <dd className={(trade.mfe_gross_pnl ?? 0) >= 0 ? "positive" : "negative"}>
              {trade.mfe_gross_pnl === null ? "—" : `${formatNumber(trade.mfe_gross_pnl)} ${currency}`}
            </dd>
          </div>
          <div>
            <dt>MAE (close)</dt>
            <dd className={(trade.mae_gross_pnl ?? 0) <= 0 ? "negative" : ""}>
              {trade.mae_gross_pnl === null ? "—" : `${formatNumber(trade.mae_gross_pnl)} ${currency}`}
            </dd>
          </div>
          <div>
            <dt>Costs</dt>
            <dd>{formatNumber(totalCosts)} {currency}</dd>
          </div>
        </dl>
      </div>

      <details className="trade-review-details">
        <summary>Full review</summary>
        <dl className="review-grid">
          <div>
            <dt>Entry market / fill</dt>
            <dd>{formatPrice(trade.entry_market_price, precision)} / {formatPrice(trade.entry_price, precision)}</dd>
          </div>
          <div>
            <dt>Exit market / fill</dt>
            <dd>{formatPrice(trade.exit_market_price, precision)} / {formatPrice(trade.exit_price, precision)}</dd>
          </div>
          <div>
            <dt>Stop / target</dt>
            <dd>{formatPrice(trade.stop_price, precision)} / {formatPrice(trade.target_price, precision)}</dd>
          </div>
          <div>
            <dt>Initial risk</dt>
            <dd>{trade.initial_risk === null ? "No stop risk" : `${formatNumber(trade.initial_risk)} ${currency}`}</dd>
          </div>
          <div>
            <dt>Gross P&L</dt>
            <dd>{formatNumber(grossPnl)} {currency}</dd>
          </div>
          <div>
            <dt>Commission / spread / slippage</dt>
            <dd>{formatNumber(trade.total_commission)} / {formatNumber(trade.total_spread_cost)} / {formatNumber(trade.total_slippage_cost)}</dd>
          </div>
          <div>
            <dt>Exit timing precision</dt>
            <dd>
              {trade.exit_time_precision === "bar_interval"
                ? `Known only within ${exitLabel} (M1 candle)`
                : trade.exit_time_precision === "exact"
                  ? "Exact execution time"
                  : "Recorded timestamp; precise execution timing unavailable"}
            </dd>
          </div>
        </dl>
      </details>

      <div className="trade-review-edit">
        <div className="field-group">
          <label htmlFor={`note-${trade.id}`}>Review note</label>
          <textarea
            id={`note-${trade.id}`}
            rows={2}
            maxLength={5000}
            placeholder="What went right or wrong?"
            value={note}
            disabled={busy}
            onChange={(event) => { setNote(event.target.value); markEditing(); }}
          />
        </div>
        <div className="field-group">
          <label htmlFor={`tags-${trade.id}`}>Tags</label>
          <div className="review-tags">
            {tags.map((tag) => <span key={tag} className="tag-chip">{tag}</span>)}
            <input
              id={`tags-${trade.id}`}
              value={tagInput}
              placeholder="add tags, comma separated"
              disabled={busy}
              onChange={(event) => { setTagInput(event.target.value); markEditing(); }}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  void save();
                }
              }}
            />
          </div>
        </div>
        <div className="trade-review-actions">
          <button type="button" onClick={() => void save()} disabled={busy || !dirty}>Save review</button>
          <button type="button" onClick={() => onFocus(trade)} disabled={busy}>Focus on chart</button>
          {saved && <span className="review-saved" role="status">Saved</span>}
        </div>
      </div>
    </article>
  );
}
