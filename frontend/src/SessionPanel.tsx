import { FormEvent, useEffect, useMemo, useState } from "react";
import { api, errorMessage } from "./api";
import { fromDateTimeLocalValue, toDateTimeLocalValue, validateReplayRange } from "./helpers";
import { SESSION_STORAGE_KEY, useReplayStore } from "./store";
import type { TimeframeProfile } from "./types";

export function SessionPanel() {
  const symbols = useReplayStore((state) => state.symbols);
  const symbolsLoading = useReplayStore((state) => state.symbolsLoading);
  const sessions = useReplayStore((state) => state.sessions);
  const sessionsLoading = useReplayStore((state) => state.sessionsLoading);
  const busy = useReplayStore((state) => state.busy);
  const loadSessions = useReplayStore((state) => state.loadSessions);
  const action = useReplayStore((state) => state.action);

  const [symbol, setSymbol] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [profile, setProfile] = useState<TimeframeProfile>("utc_aligned");
  const [contextBars, setContextBars] = useState(1000);
  const [initialBalance, setInitialBalance] = useState(10000);
  const [spread, setSpread] = useState(0);
  const [slippage, setSlippage] = useState(0);
  const [commission, setCommission] = useState(0);
  const [formError, setFormError] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  const selected = useMemo(
    () => symbols.find((item) => item.symbol === symbol) ?? symbols[0],
    [symbol, symbols],
  );

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    if (!selected) return;
    setSymbol(selected.symbol);
    setStart(toDateTimeLocalValue(selected.first_timestamp));
    setEnd(toDateTimeLocalValue(selected.last_timestamp));
    setProfile(selected.default_profile === "new_york_close" ? "new_york_close" : "utc_aligned");
    setFormError("");
  }, [selected?.symbol, selected?.first_timestamp, selected?.last_timestamp, selected?.default_profile]);

  async function createSession(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const rangeError = validateReplayRange(start, end, selected.first_timestamp, selected.last_timestamp);
    if (rangeError) {
      setFormError(rangeError);
      return;
    }
    if (!Number.isFinite(initialBalance) || initialBalance <= 0) {
      setFormError("Initial balance must be greater than zero.");
      return;
    }
    if ([spread, slippage, commission].some((value) => !Number.isFinite(value) || value < 0)) {
      setFormError("Spread, slippage, and commission cannot be negative.");
      return;
    }
    if (!Number.isInteger(contextBars) || contextBars < 500 || contextBars > 2000) {
      setFormError("Chart context must be a whole number from 500 to 2,000 bars.");
      return;
    }

    const startUtc = fromDateTimeLocalValue(start);
    const endUtc = fromDateTimeLocalValue(end);
    if (!startUtc || !endUtc) return;
    setFormError("");
    await action(() => api.createSession({
      symbol: selected.symbol,
      start: startUtc,
      end: endUtc,
      profile,
      visible_timeframe: "1m",
      advance_step_minutes: 1,
      chart_context_1m_bars: contextBars,
      account_currency: selected.pnl_currency,
      conversion_rate: 1,
      initial_balance: initialBalance,
      spread,
      slippage,
      commission_per_quantity: commission,
    }));
  }

  async function deleteSession(sessionId: string) {
    setDeleting(sessionId);
    setDeleteError("");
    try {
      await api.deleteSession(sessionId);
      if (localStorage.getItem(SESSION_STORAGE_KEY) === sessionId) {
        localStorage.removeItem(SESSION_STORAGE_KEY);
      }
      setConfirmDelete(null);
      await loadSessions();
    } catch (caught) {
      setDeleteError(errorMessage(caught));
    } finally {
      setDeleting(null);
    }
  }

  return (
    <section className="session-stack" aria-label="Replay sessions">
      <section className="panel" aria-labelledby="new-session-heading">
        <div className="panel-heading">
          <div>
            <p className="section-kicker">REPLAY</p>
            <h2 id="new-session-heading">New session</h2>
          </div>
          <span className="panel-note">UTC causal</span>
        </div>

        {symbolsLoading && symbols.length === 0 ? (
          <p className="empty-state" role="status">Loading imported instruments…</p>
        ) : !selected ? (
          <div className="empty-state">
            <strong>No instruments yet</strong>
            <span>Import a supported one-minute CSV to configure a replay.</span>
          </div>
        ) : (
          <form onSubmit={createSession} noValidate>
            <div className="form-grid">
              <div className="field-group form-grid-wide">
                <label htmlFor="session-symbol">Instrument</label>
                <select id="session-symbol" value={selected.symbol} onChange={(event) => setSymbol(event.target.value)}>
                  {symbols.map((item) => (
                    <option key={item.symbol} value={item.symbol}>
                      {item.symbol} · {item.asset_class} · {item.pnl_currency} · ×{item.contract_multiplier}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field-group">
                <label htmlFor="session-start">Start (UTC)</label>
                <input
                  id="session-start"
                  type="datetime-local"
                  value={start}
                  min={toDateTimeLocalValue(selected.first_timestamp)}
                  max={toDateTimeLocalValue(selected.last_timestamp)}
                  onChange={(event) => setStart(event.target.value)}
                />
              </div>
              <div className="field-group">
                <label htmlFor="session-end">End (UTC)</label>
                <input
                  id="session-end"
                  type="datetime-local"
                  value={end}
                  min={toDateTimeLocalValue(selected.first_timestamp)}
                  max={toDateTimeLocalValue(selected.last_timestamp)}
                  onChange={(event) => setEnd(event.target.value)}
                />
              </div>
              <div className="field-group">
                <label htmlFor="session-profile">Candle alignment</label>
                <select id="session-profile" value={profile} onChange={(event) => setProfile(event.target.value === "new_york_close" ? "new_york_close" : "utc_aligned")}>
                  <option value="utc_aligned">UTC aligned</option>
                  <option value="new_york_close">New York close</option>
                </select>
              </div>
              <div className="field-group">
                <label htmlFor="context-bars">Chart context bars</label>
                <input id="context-bars" type="number" min="500" max="2000" step="100" value={contextBars} onChange={(event) => setContextBars(Number(event.target.value))} />
              </div>
            </div>

            <fieldset>
              <legend>Account and execution costs</legend>
              <p className="field-hint">Spread is the full bid/ask spread in price units. Commission is charged per quantity, per side.</p>
              <div className="form-grid">
                <div className="field-group">
                  <label htmlFor="initial-balance">Initial balance ({selected.pnl_currency})</label>
                  <input id="initial-balance" type="number" min="0.01" step="any" value={initialBalance} onChange={(event) => setInitialBalance(Number(event.target.value))} />
                </div>
                <div className="field-group">
                  <label htmlFor="spread">Full spread</label>
                  <input id="spread" type="number" min="0" step="any" value={spread} onChange={(event) => setSpread(Number(event.target.value))} />
                </div>
                <div className="field-group">
                  <label htmlFor="slippage">Slippage per execution</label>
                  <input id="slippage" type="number" min="0" step="any" value={slippage} onChange={(event) => setSlippage(Number(event.target.value))} />
                </div>
                <div className="field-group">
                  <label htmlFor="commission">Commission per quantity / side</label>
                  <input id="commission" type="number" min="0" step="any" value={commission} onChange={(event) => setCommission(Number(event.target.value))} />
                </div>
              </div>
            </fieldset>

            {formError && <p className="inline-message message-error" role="alert">{formError}</p>}
            <button className="button-primary full-width" type="submit" disabled={busy}>
              {busy ? "Creating session…" : "Create replay session"}
            </button>
          </form>
        )}
      </section>

      <section className="panel history-panel" aria-labelledby="history-heading">
        <div className="panel-heading">
          <div>
            <p className="section-kicker">HISTORY</p>
            <h2 id="history-heading">Saved sessions</h2>
          </div>
          <button className="button-quiet button-small" type="button" onClick={() => void loadSessions()} disabled={sessionsLoading}>
            Refresh
          </button>
        </div>

        {sessionsLoading && sessions.length === 0 ? (
          <p className="empty-state" role="status">Loading saved sessions…</p>
        ) : sessions.length === 0 ? (
          <div className="empty-state compact">
            <strong>No saved sessions</strong>
            <span>Sessions created here can be resumed without refreshing.</span>
          </div>
        ) : (
          <ul className="session-list">
            {sessions.map((session) => (
              <li key={session.id}>
                <div className="session-summary">
                  <strong>{session.symbol}</strong>
                  <span className={`status-chip status-${session.status}`}>{session.status}</span>
                  <span className="session-range">
                    {new Date(session.start).toLocaleString(undefined, { timeZone: "UTC" })} → {new Date(session.end).toLocaleString(undefined, { timeZone: "UTC" })} UTC
                  </span>
                  <time dateTime={session.updated_at}>Updated {new Date(session.updated_at).toLocaleString()}</time>
                  <span>Bar {Math.max(0, session.current_index + 1).toLocaleString()}</span>
                </div>
                {confirmDelete === session.id ? (
                  <div className="delete-confirm" role="group" aria-label={`Confirm deletion of ${session.symbol} session`}>
                    <span>Delete this replay permanently?</span>
                    <button className="button-danger button-small" type="button" onClick={() => void deleteSession(session.id)} disabled={deleting === session.id}>
                      {deleting === session.id ? "Deleting…" : "Yes, delete"}
                    </button>
                    <button className="button-small" type="button" onClick={() => setConfirmDelete(null)} disabled={deleting === session.id}>Cancel</button>
                  </div>
                ) : (
                  <div className="row-actions">
                    <button className="button-primary button-small" type="button" onClick={() => void action(() => api.getSessionState(session.id))} disabled={busy}>
                      Resume
                    </button>
                    <button className="button-quiet button-small" type="button" onClick={() => setConfirmDelete(session.id)} disabled={busy}>
                      Delete
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
        {deleteError && <p className="inline-message message-error" role="alert">{deleteError}</p>}
      </section>
    </section>
  );
}
