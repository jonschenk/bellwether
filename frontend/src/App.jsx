import { useCallback, useEffect, useRef, useState } from "react";
import {
  getHealth,
  getSchwabStatus,
  getSchwabAuthUrl,
  postSchwabCallback,
  postSchwabDisconnect,
  getPortfolio,
} from "./api.js";
import ScannerView from "./components/ScannerView.jsx";
import PortfolioView from "./components/PortfolioView.jsx";

const PORTFOLIO_POLL_MS = 90_000; // 1.5 min

export default function App() {
  const [backendUp, setBackendUp] = useState(null);
  const [tab, setTab] = useState("scanner");
  const [schwab, setSchwab] = useState({ configured: false, connected: false });
  const [portfolio, setPortfolio] = useState(null);
  const [portfolioUpdatedAt, setPortfolioUpdatedAt] = useState(null);
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState(null);
  const pollRef = useRef(null);

  const loadSchwab = useCallback(async () => {
    try {
      setSchwab(await getSchwabStatus());
    } catch {
      /* non-fatal */
    }
  }, []);

  const loadPortfolio = useCallback(async () => {
    try {
      const data = await getPortfolio();
      setPortfolio(data);
      if (data?.connected) setPortfolioUpdatedAt(data.as_of ?? Date.now() / 1000);
    } catch {
      /* non-fatal */
    }
  }, []);

  // Launch: health + Schwab status.
  useEffect(() => {
    (async () => {
      try {
        await getHealth();
        setBackendUp(true);
        await loadSchwab();
      } catch {
        setBackendUp(false);
      }
    })();
  }, [loadSchwab]);

  // Poll the portfolio every 1.5 min whenever Schwab is connected.
  useEffect(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (!schwab.connected) {
      setPortfolio(null);
      return;
    }
    loadPortfolio();
    pollRef.current = setInterval(loadPortfolio, PORTFOLIO_POLL_MS);
    return () => pollRef.current && clearInterval(pollRef.current);
  }, [schwab.connected, loadPortfolio]);

  const onConnect = useCallback(async () => {
    setConnecting(true);
    setConnectError(null);
    try {
      const { auth_url, redirect_uri } = await getSchwabAuthUrl();
      let callbackUrl = null;
      if (window.electronAPI?.schwabOAuth) {
        callbackUrl = await window.electronAPI.schwabOAuth(auth_url, redirect_uri);
      } else {
        window.open(auth_url, "_blank", "noopener");
        callbackUrl = window.prompt(
          "After approving in Schwab you'll land on a https://127.0.0.1 page that won't load. Copy that full URL from the address bar and paste it here:",
        );
      }
      if (!callbackUrl) return; // user cancelled
      await postSchwabCallback(callbackUrl);
      await loadSchwab();
      await loadPortfolio();
    } catch (e) {
      setConnectError(e.message);
    } finally {
      setConnecting(false);
    }
  }, [loadSchwab, loadPortfolio]);

  const onDisconnect = useCallback(async () => {
    try {
      await postSchwabDisconnect();
      setPortfolio(null);
      await loadSchwab();
    } catch {
      /* non-fatal */
    }
  }, [loadSchwab]);

  const liveBalance = portfolio?.connected ? portfolio.value : null;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-dot" />
          <h1>Swing Scanner</h1>
        </div>
        <nav className="tabs">
          <button className={`tab ${tab === "scanner" ? "active" : ""}`} onClick={() => setTab("scanner")}>
            Scanner
          </button>
          <button className={`tab ${tab === "portfolio" ? "active" : ""}`} onClick={() => setTab("portfolio")}>
            Portfolio
            {schwab.connected && <span className="tab-dot" title="Schwab connected" />}
          </button>
        </nav>
      </header>

      {backendUp === false && (
        <div className="banner error">
          Can't reach the backend at 127.0.0.1:8765 — start it and relaunch the app.
        </div>
      )}

      {tab === "scanner" ? (
        <ScannerView backendUp={backendUp} liveBalance={liveBalance} balanceUpdatedAt={portfolioUpdatedAt} />
      ) : (
        <PortfolioView
          portfolio={portfolio}
          schwab={schwab}
          connecting={connecting}
          connectError={connectError}
          onConnect={onConnect}
          onDisconnect={onDisconnect}
          updatedAt={portfolioUpdatedAt}
        />
      )}
    </div>
  );
}
