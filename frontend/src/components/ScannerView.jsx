import { useCallback, useEffect, useRef, useState } from "react";
import { getScanStatus, getSettings, saveSettings, startScan, refreshScan, analyzeTicker } from "../api.js";
import { formatDuration, formatClock, money } from "../format.js";
import StockCard from "./StockCard.jsx";
import SettingsPanel from "./SettingsPanel.jsx";

const POLL_INTERVAL_MS = 1500;
const REFRESH_INTERVAL_MS = 180_000;

// Build a full settings payload (computed fields like max_price are read-only).
function settingsPayload(s) {
  const { max_price, ...rest } = s;
  return rest;
}

export default function ScannerView({ backendUp, liveBalance, balanceUpdatedAt }) {
  const [scan, setScan] = useState({ status: "idle", progress: "", results: [] });
  const [settings, setSettings] = useState(null);
  const [capital, setCapital] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [error, setError] = useState(null);
  const [now, setNow] = useState(Date.now() / 1000);
  const pollRef = useRef(null);
  const refreshingRef = useRef(false);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const refreshSettings = useCallback(async () => {
    try {
      const s = await getSettings();
      setSettings(s);
      setCapital(String(s.capital));
    } catch {
      /* non-fatal */
    }
  }, []);

  const poll = useCallback(async () => {
    try {
      const state = await getScanStatus();
      setScan(state);
      if (state.status !== "running" && state.status !== "analyzing") stopPolling();
    } catch (e) {
      setError(e.message);
      stopPolling();
    }
  }, []);

  const beginPolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(poll, POLL_INTERVAL_MS);
  }, [poll]);

  useEffect(() => {
    (async () => {
      await refreshSettings();
      try {
        const state = await getScanStatus();
        setScan(state);
        if (state.status === "running" || state.status === "analyzing") beginPolling();
      } catch {
        /* non-fatal */
      }
    })();
    return stopPolling;
  }, [beginPolling, refreshSettings]);

  // Keep the scanner's capital in sync with the live Schwab balance.
  useEffect(() => {
    if (liveBalance == null || !settings) return;
    if (Math.abs(liveBalance - settings.capital) < 0.5) return;
    (async () => {
      try {
        const saved = await saveSettings({ ...settingsPayload(settings), capital: liveBalance });
        setSettings(saved);
      } catch {
        /* non-fatal */
      }
    })();
  }, [liveBalance, settings]);

  const running = scan.status === "running";
  const analyzing = scan.status === "analyzing";
  const busy = running || analyzing;

  useEffect(() => {
    if (!busy) return;
    setNow(Date.now() / 1000);
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, [busy]);

  const doRefresh = useCallback(async () => {
    if (refreshingRef.current) return;
    refreshingRef.current = true;
    try {
      setScan(await refreshScan());
    } catch {
      /* best-effort */
    } finally {
      refreshingRef.current = false;
    }
  }, []);

  useEffect(() => {
    if (scan.status !== "done" || (scan.results ?? []).length === 0) return;
    const id = setInterval(doRefresh, REFRESH_INTERVAL_MS);
    return () => clearInterval(id);
  }, [scan.status, scan.results, doRefresh]);

  const commitCapital = async () => {
    const value = Number(capital);
    if (!settings || !value || value === settings.capital) return;
    try {
      const saved = await saveSettings({ ...settingsPayload(settings), capital: value });
      setSettings(saved);
      setCapital(String(saved.capital));
    } catch (e) {
      setError(e.message);
      setCapital(String(settings.capital));
    }
  };

  const onAnalyze = useCallback(async (ticker) => {
    setScan((s) => ({
      ...s,
      results: s.results.map((r) => (r.ticker === ticker ? { ...r, ai_status: "pending" } : r)),
    }));
    try {
      setScan(await analyzeTicker(ticker));
    } catch {
      setScan((s) => ({
        ...s,
        results: s.results.map((r) => (r.ticker === ticker && !r.ai ? { ...r, ai_status: "idle" } : r)),
      }));
    }
  }, []);

  const onRunScan = async (fresh = false) => {
    setError(null);
    try {
      await startScan(fresh);
      setScan((s) => ({ ...s, status: "running", progress: "Starting scan…", started_at: Date.now() / 1000 }));
      beginPolling();
    } catch (e) {
      setError(e.message);
    }
  };

  const results = scan.results ?? [];
  const elapsed = busy && scan.started_at ? now - scan.started_at : 0;
  const analyzedCount = results.filter((r) => r.ai).length;
  const scanDuration = scan.started_at && scan.scanned_at ? scan.scanned_at - scan.started_at : null;
  const loadDuration =
    scan.status === "done" && scan.started_at && scan.finished_at ? scan.finished_at - scan.started_at : null;
  const lastUpdated = scan.refreshed_at ?? scan.finished_at;

  return (
    <>
      <div className="subbar">
        <div className="subbar-left">
          {liveBalance != null ? (
            <span className="balance-live" title="Live Schwab account value — drives position sizing">
              {money(liveBalance)}
              <span className="muted small">
                live{balanceUpdatedAt ? ` · ${formatClock(balanceUpdatedAt)}` : ""}
              </span>
            </span>
          ) : (
            <label className="capital-input" title="Your trading capital — drives position sizing and the price ceiling">
              <span>$</span>
              <input
                type="number"
                min="1"
                step="100"
                value={capital}
                onChange={(e) => setCapital(e.target.value)}
                onBlur={commitCapital}
                onKeyDown={(e) => e.key === "Enter" && e.target.blur()}
                disabled={!settings}
              />
            </label>
          )}
          {settings && (
            <span className="ceiling muted small" title="Max share price = capital × max position %">
              ≤ ${settings.max_price?.toLocaleString()}/share
            </span>
          )}
          {settings && (
            <span
              className="universe-chip"
              title={
                settings.universe === "full"
                  ? "Scanning the full US market (~5,900 stocks). Change in Settings."
                  : "Scanning the curated list (~675 stocks). Change in Settings."
              }
            >
              {settings.universe === "full" ? "🌐 Full market" : "★ Curated"}
            </span>
          )}
        </div>
        <div className="subbar-right">
          <button className="btn ghost" onClick={() => setShowSettings(true)}>
            Settings
          </button>
          <button
            className="btn ghost"
            onClick={() => onRunScan(true)}
            disabled={busy || backendUp === false}
            title="Force a full re-download of fresh prices (ignore the cache)"
          >
            ↻ Fresh
          </button>
          <button className="btn primary" onClick={() => onRunScan(false)} disabled={busy || backendUp === false}>
            {busy ? <span className="spinner" /> : null}
            {running ? "Scanning…" : analyzing ? "Analyzing…" : "Run Scan"}
          </button>
        </div>
      </div>

      {error && <div className="banner error">{error}</div>}

      {busy && (
        <div className="banner progress">
          <span className="spinner" />
          <span>{scan.progress || "Scanning…"}</span>
          <span className="elapsed">{formatDuration(elapsed)} elapsed</span>
        </div>
      )}

      {scan.status === "error" && <div className="banner error">Scan failed: {scan.error}</div>}

      {analyzing && results.length > 0 && (
        <div className="scan-meta muted small">
          {scanDuration != null && <span>Found {results.length} setups in {formatDuration(scanDuration)}</span>}
          <span> · <span className="spinner tiny" /> AI analyzing {analyzedCount}/{results.length}…</span>
        </div>
      )}

      {scan.status === "done" && results.length > 0 && (
        <div className="scan-meta muted small">
          {loadDuration != null && <span>Loaded {results.length} setups in {formatDuration(loadDuration)}</span>}
          {scan.from_cache && <span className="cache-tag"> · ⚡ cached prices</span>}
          {lastUpdated && <span> · updated {formatClock(lastUpdated)}</span>}
          {scan.refreshing ? (
            <span className="refreshing"> · <span className="spinner tiny" /> refreshing…</span>
          ) : (
            <span> · auto-refreshes every 3 min</span>
          )}
        </div>
      )}

      <main>
        {!running && scan.status === "done" && results.length === 0 && (
          <div className="empty">
            <p>No stocks passed the scan with the current criteria.</p>
            <p className="muted">
              Try raising your capital, loosening the RSI/ADX thresholds, or lowering the min ATR% in Settings.
            </p>
          </div>
        )}

        {!running && scan.status === "idle" && (
          <div className="empty">
            <p>Hit <strong>Run Scan</strong> to find leader pullbacks sized to your account.</p>
            <p className="muted">
              Market leaders (high relative strength · near 52w highs · 20&gt;50&gt;200 SMA, rising)
              taking a healthy breather (RSI 40–60 · strong ADX · tradeable ATR%).
            </p>
          </div>
        )}

        <div className="grid">
          {results.map((stock) => (
            <StockCard key={stock.ticker} stock={stock} onAnalyze={onAnalyze} />
          ))}
        </div>
      </main>

      {showSettings && (
        <SettingsPanel
          onClose={() => {
            setShowSettings(false);
            refreshSettings();
          }}
        />
      )}
    </>
  );
}
