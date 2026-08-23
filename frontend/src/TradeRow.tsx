import { useEffect, useState } from "react";
import { api } from "./api";
import { closeQuantityExceedsRemainder, formatAdaptiveNumber, formatNumber, formatPrice, parsePositiveQuantity, quantityDraft, utcDateTime } from "./helpers";
import type { ReplaySnapshot, ReplayUpdate, Trade } from "./types";

type TradeRowProps = {
  trade: Trade;
  replay: ReplaySnapshot;
  precision: number;
  busy: boolean;
  action: (call: () => Promise<ReplayUpdate>) => Promise<void>;
};

export function TradeRow({ trade, replay, precision, busy, action }: TradeRowProps) {
  // The close quantity is string-backed so empty/incomplete decimal drafts
  // survive editing; it is parsed only when Close is pressed.
  const [closeQuantity, setCloseQuantity] = useState(quantityDraft(trade.remaining_quantity));
  const [stop, setStop] = useState(trade.stop_price?.toString() ?? "");
  const [target, setTarget] = useState(trade.target_price?.toString() ?? "");
  const [validationError, setValidationError] = useState("");

  useEffect(() => {
    setCloseQuantity((value) => {
      const parsed = parsePositiveQuantity(value);
      if (parsed === null) return value;
      return closeQuantityExceedsRemainder(parsed, trade.remaining_quantity)
        ? quantityDraft(trade.remaining_quantity)
        : value;
    });
  }, [trade.remaining_quantity]);

  useEffect(() => {
    setStop(trade.stop_price?.toString() ?? "");
    setTarget(trade.target_price?.toString() ?? "");
  }, [trade.stop_price, trade.target_price]);

  async function closeTrade(quantity: number | null) {
    if (quantity === null || !Number.isFinite(quantity) || quantity <= 0) {
      setValidationError("Enter a finite close quantity greater than zero.");
      return;
    }
    if (closeQuantityExceedsRemainder(quantity, trade.remaining_quantity)) {
      setValidationError(`Close quantity cannot exceed ${formatAdaptiveNumber(trade.remaining_quantity)}.`);
      return;
    }
    setValidationError("");
    await action(() => api.closeTrade(trade.id, {
      session_id: replay.id,
      quantity,
    }));
  }

  async function updateProtection(kind: "stop" | "target") {
    const rawValue = kind === "stop" ? stop : target;
    const price = rawValue.trim() === "" ? null : Number(rawValue);
    if (price !== null && (!Number.isFinite(price) || price <= 0)) {
      setValidationError(`${kind === "stop" ? "Stop" : "Target"} must be a positive price or blank to clear it.`);
      return;
    }
    if (price !== null && replay.current_price !== null) {
      const crossed = kind === "stop"
        ? (trade.direction === "long" ? price >= replay.current_price : price <= replay.current_price)
        : (trade.direction === "long" ? price <= replay.current_price : price >= replay.current_price);
      if (crossed) {
        setValidationError(`${kind === "stop" ? "Stop" : "Target"} is already crossed by the current market price.`);
        return;
      }
    }
    setValidationError("");
    await action(() => kind === "stop"
      ? api.updateTradeStop(trade.id, {
        session_id: replay.id,
        price,
      })
      : api.updateTradeTarget(trade.id, {
        session_id: replay.id,
        price,
      }));
  }

  return (
    <article className="trade-row" aria-labelledby={`trade-${trade.id}`}>
      <div className="trade-summary">
        <div>
          <span className={`direction-badge direction-${trade.direction}`}>{trade.direction}</span>
          <h3 id={`trade-${trade.id}`}>{formatAdaptiveNumber(trade.remaining_quantity)} open</h3>
        </div>
        <dl>
          <div>
            <dt>Market / fill</dt>
            <dd>{formatPrice(trade.entry_market_price, precision)} / {formatPrice(trade.entry_price, precision)}</dd>
          </div>
          <div>
            <dt>Opened</dt>
            <dd><time dateTime={trade.entry_time}>{utcDateTime(trade.entry_time)} UTC</time></dd>
          </div>
          <div>
            <dt>Realized net</dt>
            <dd className={trade.realized_pnl >= 0 ? "positive" : "negative"}>
              {formatNumber(trade.realized_pnl)} {replay.account_currency}
            </dd>
          </div>
        </dl>
      </div>

      <div className="trade-actions">
        <div className="trade-action-group">
          <label htmlFor={`close-${trade.id}`}>Close quantity</label>
          <div className="input-action multi-action">
            <input
              id={`close-${trade.id}`}
              type="text"
              inputMode="decimal"
              value={closeQuantity}
              onChange={(event) => setCloseQuantity(event.target.value)}
            />
            <button type="button" onClick={() => void closeTrade(parsePositiveQuantity(closeQuantity))} disabled={busy}>Close</button>
            <button type="button" onClick={() => void closeTrade(trade.remaining_quantity / 2)} disabled={busy}>50%</button>
            <button type="button" onClick={() => void closeTrade(trade.remaining_quantity)} disabled={busy}>All</button>
          </div>
        </div>
        <div className="protection-grid">
          <div className="field-group">
            <label htmlFor={`stop-${trade.id}`}>Stop price</label>
            <div className="input-action">
              <input id={`stop-${trade.id}`} inputMode="decimal" value={stop} onChange={(event) => setStop(event.target.value)} placeholder="No stop" />
              <button type="button" onClick={() => void updateProtection("stop")} disabled={busy}>Apply</button>
            </div>
          </div>
          <div className="field-group">
            <label htmlFor={`target-${trade.id}`}>Target price</label>
            <div className="input-action">
              <input id={`target-${trade.id}`} inputMode="decimal" value={target} onChange={(event) => setTarget(event.target.value)} placeholder="No target" />
              <button type="button" onClick={() => void updateProtection("target")} disabled={busy}>Apply</button>
            </div>
          </div>
        </div>
      </div>
      {validationError && <p className="inline-message message-error trade-error" role="alert">{validationError}</p>}
    </article>
  );
}
