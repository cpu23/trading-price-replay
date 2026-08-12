import { useEffect, useRef } from "react";
import { ImportPanel } from "./ImportPanel";
import { ReplayWorkspace } from "./ReplayWorkspace";
import { SessionPanel } from "./SessionPanel";
import { useReplayStore } from "./store";

export function App() {
  const replay = useReplayStore((state) => state.replay);
  const restoring = useReplayStore((state) => state.restoring);
  const error = useReplayStore((state) => state.error);
  const clearError = useReplayStore((state) => state.clearError);
  const restoreSavedSession = useReplayStore((state) => state.restoreSavedSession);
  const loadSymbols = useReplayStore((state) => state.loadSymbols);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    void Promise.all([restoreSavedSession(), loadSymbols()]);
  }, [loadSymbols, restoreSavedSession]);

  if (restoring) {
    return (
      <main className="app app-loading" aria-busy="true">
        <p className="eyebrow">LOCAL PRACTICE TERMINAL</p>
        <h1>Restoring your replay…</h1>
        <p className="muted" role="status">Checking the last session saved in this browser.</p>
      </main>
    );
  }

  if (replay) return <ReplayWorkspace />;

  return (
    <main className="app">
      <header className="hero">
        <p className="eyebrow">LOCAL PRACTICE TERMINAL</p>
        <h1>Price Replay Lab</h1>
        <p className="hero-copy">
          Import one-minute market data, reveal it causally, and practise execution with explicit costs.
        </p>
      </header>
      {error && (
        <div className="alert alert-error alert-dismissible" role="alert">
          <span>{error}</span>
          <button type="button" onClick={clearError} aria-label="Dismiss error">Dismiss</button>
        </div>
      )}
      <div className="setup">
        <ImportPanel />
        <SessionPanel />
      </div>
    </main>
  );
}
