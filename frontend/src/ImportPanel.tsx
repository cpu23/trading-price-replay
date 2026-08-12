import { FormEvent, useState } from "react";
import { api, errorMessage } from "./api";
import { useReplayStore } from "./store";
import type { ImportResponse, InspectPathResponse } from "./types";

export function ImportPanel() {
  const loadSymbols = useReplayStore((state) => state.loadSymbols);
  const [path, setPath] = useState("");
  const [files, setFiles] = useState<string[]>([]);
  const [selectedFile, setSelectedFile] = useState("");
  const [symbol, setSymbol] = useState("");
  const [assetClass, setAssetClass] = useState("other");
  const [pnlCurrency, setPnlCurrency] = useState("USD");
  const [pricePrecision, setPricePrecision] = useState(2);
  const [contractMultiplier, setContractMultiplier] = useState(1);
  const [profile, setProfile] = useState("utc_aligned");
  const [busyAction, setBusyAction] = useState<"inspect" | "import" | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function inspectPath() {
    if (!path.trim()) {
      setError("Enter a CSV file or folder path first.");
      return;
    }
    setBusyAction("inspect");
    setError("");
    setMessage("");
    try {
      const response = await api.post<InspectPathResponse>("/api/imports/inspect-path", { path: path.trim() });
      setFiles(response.files);
      setSelectedFile(response.files[0] ?? "");
      if (response.files.length === 0) {
        setMessage("No supported CSV files were found at that path.");
      } else if (response.files.length === 1) {
        setMessage("One CSV is ready to import.");
      } else {
        setMessage(`${response.files.length} CSV files found. Choose the instrument file to import.`);
      }
    } catch (caught) {
      setFiles([]);
      setSelectedFile("");
      setError(errorMessage(caught));
    } finally {
      setBusyAction(null);
    }
  }

  async function importInstrument(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedSymbol = symbol.trim().toUpperCase();
    if (!selectedFile) {
      setError("Inspect the path and select a CSV file before importing.");
      return;
    }
    if (!/^[A-Z0-9._-]+$/.test(normalizedSymbol)) {
      setError("Symbol may contain letters, numbers, dots, underscores, and hyphens.");
      return;
    }
    if (!assetClass.trim() || !/^[A-Z]{3,8}$/.test(pnlCurrency.trim().toUpperCase())) {
      setError("Enter an asset class and a 3–8 letter P&L currency code.");
      return;
    }
    if (!Number.isInteger(pricePrecision) || pricePrecision < 0 || pricePrecision > 12) {
      setError("Price precision must be a whole number from 0 to 12.");
      return;
    }
    if (!Number.isFinite(contractMultiplier) || contractMultiplier <= 0) {
      setError("Contract multiplier must be greater than zero.");
      return;
    }

    setBusyAction("import");
    setError("");
    setMessage("");
    try {
      const response = await api.post<ImportResponse>("/api/imports", {
        path: selectedFile,
        symbol: normalizedSymbol,
        asset_class: assetClass.trim(),
        pnl_currency: pnlCurrency.trim().toUpperCase(),
        price_precision: pricePrecision,
        contract_multiplier: contractMultiplier,
        default_profile: profile,
      });
      const gaps = response.validation?.gap_count;
      setMessage(
        `Imported ${response.rows_imported.toLocaleString()} one-minute bars${
          gaps === undefined ? "." : ` with ${gaps.toLocaleString()} detected gap${gaps === 1 ? "" : "s"}.`
        }`,
      );
      await loadSymbols();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusyAction(null);
    }
  }

  return (
    <section className="panel import-panel" aria-labelledby="import-heading">
      <div className="panel-heading">
        <div>
          <p className="section-kicker">DATA</p>
          <h2 id="import-heading">Import instrument</h2>
        </div>
        <span className="panel-note">One-minute CSV</span>
      </div>

      <div className="field-group">
        <label htmlFor="source-path">Local CSV or folder path</label>
        <div className="input-action">
          <input
            id="source-path"
            value={path}
            onChange={(event) => {
              setPath(event.target.value);
              setFiles([]);
              setSelectedFile("");
            }}
            placeholder="/home/me/data"
            autoComplete="off"
          />
          <button type="button" onClick={inspectPath} disabled={busyAction !== null}>
            {busyAction === "inspect" ? "Inspecting…" : "Inspect"}
          </button>
        </div>
        <p className="field-hint">Folders are inspected locally; select one discovered CSV below.</p>
      </div>

      {files.length > 0 && (
        <div className="field-group inspected-files">
          <label htmlFor="source-file">CSV to import</label>
          <select id="source-file" value={selectedFile} onChange={(event) => setSelectedFile(event.target.value)}>
            {files.map((file) => <option key={file} value={file}>{file}</option>)}
          </select>
        </div>
      )}

      <form onSubmit={importInstrument} noValidate>
        <div className="form-grid">
          <div className="field-group">
            <label htmlFor="symbol">Symbol</label>
            <input id="symbol" value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} placeholder="ESM6" />
          </div>
          <div className="field-group">
            <label htmlFor="asset-class">Asset class</label>
            <input id="asset-class" value={assetClass} onChange={(event) => setAssetClass(event.target.value)} placeholder="futures" />
          </div>
          <div className="field-group">
            <label htmlFor="pnl-currency">P&amp;L currency</label>
            <input id="pnl-currency" value={pnlCurrency} onChange={(event) => setPnlCurrency(event.target.value.toUpperCase())} />
          </div>
          <div className="field-group">
            <label htmlFor="price-precision">Price decimals</label>
            <input id="price-precision" type="number" min="0" max="12" step="1" value={pricePrecision} onChange={(event) => setPricePrecision(Number(event.target.value))} />
          </div>
          <div className="field-group">
            <label htmlFor="contract-multiplier">Contract multiplier</label>
            <input id="contract-multiplier" type="number" min="0.00000001" step="any" value={contractMultiplier} onChange={(event) => setContractMultiplier(Number(event.target.value))} />
          </div>
          <div className="field-group">
            <label htmlFor="default-profile">Default alignment</label>
            <select id="default-profile" value={profile} onChange={(event) => setProfile(event.target.value)}>
              <option value="utc_aligned">UTC aligned</option>
              <option value="new_york_close">New York close</option>
            </select>
          </div>
        </div>
        <button className="button-primary full-width" type="submit" disabled={busyAction !== null || !selectedFile}>
          {busyAction === "import" ? "Importing…" : "Import selected CSV"}
        </button>
      </form>

      {error && <p className="inline-message message-error" role="alert">{error}</p>}
      {message && <p className="inline-message" role="status">{message}</p>}
    </section>
  );
}
