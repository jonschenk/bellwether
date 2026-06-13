import { useCallback, useEffect, useRef, useState } from "react";
import { getAlerts, setAlert as apiSetAlert, fetchInsight } from "../api.js";
import { money, signedMoney, pct, formatClock } from "../format.js";
import PositionCard from "./PositionCard.jsx";

function Metric({ label, value, sub, tone }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <span className={`metric-value ${tone === "up" ? "pl-up-text" : tone === "down" ? "pl-down-text" : ""}`}>
        {value}
      </span>
      {sub && <span className={`metric-sub ${tone === "up" ? "pl-up-text" : tone === "down" ? "pl-down-text" : "muted"}`}>{sub}</span>}
    </div>
  );
}

function ConnectPrompt({ configured, connecting, error, onConnect, redirectUri }) {
  return (
    <div className="empty">
      {!configured ? (
        <>
          <p><strong>Schwab not configured</strong></p>
          <p className="muted" style={{ maxWidth: 520, margin: "0 auto" }}>
            Register an app at <strong>developer.schwab.com</strong>, set its callback URL to{" "}
            <code>https://127.0.0.1</code>, then add your credentials to <code>.env</code>:
          </p>
          <pre className="env-hint">SCHWAB_APP_KEY=your-app-key{"\n"}SCHWAB_APP_SECRET=your-app-secret</pre>
          <p className="muted small">Restart the app after editing .env.</p>
        </>
      ) : (
        <>
          <p>Connect your Schwab account to see live balances, positions, and P&amp;L.</p>
          <button className="btn primary" onClick={onConnect} disabled={connecting} style={{ marginTop: 12 }}>
            {connecting ? <span className="spinner" /> : null}
            {connecting ? "Connecting…" : "Connect Schwab Account"}
          </button>
          {error && <p className="banner error" style={{ marginTop: 14 }}>{error}</p>}
          <p className="muted small" style={{ marginTop: 14 }}>
            A Schwab login window will open. Callback: <code>{redirectUri}</code>
          </p>
        </>
      )}
    </div>
  );
}

function OrdersTable({ orders }) {
  if (!orders.length) return <p className="muted">No recent filled orders.</p>;
  return (
    <div className="orders">
      {orders.map((o, i) => (
        <div className="order-row" key={i}>
          <span className="muted small">{o.date ? new Date(o.date).toLocaleDateString() : "—"}</span>
          <span className="order-sym">{o.symbol}</span>
          <span className={`badge ${(o.side || "").toUpperCase().startsWith("BUY") ? "pl-up" : "pl-down"}`}>{o.side}</span>
          <span>{o.shares} sh</span>
          <span className="muted">{o.price != null ? money(o.price) : "—"}</span>
        </div>
      ))}
    </div>
  );
}

export default function PortfolioView({ portfolio, schwab, connecting, connectError, onConnect, onDisconnect, updatedAt }) {
  const [insights, setInsights] = useState({});
  const [alerts, setAlerts] = useState({});
  const fetchedRef = useRef(new Set());
  const notifiedRef = useRef(new Set());

  useEffect(() => {
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    getAlerts().then(setAlerts).catch(() => {});
  }, []);

  const connected = portfolio?.connected;
  const positions = portfolio?.positions ?? [];
  const symbolsKey = positions.map((p) => p.symbol).join(",");

  // Fetch an AI insight once per held symbol.
  useEffect(() => {
    if (!connected) return;
    for (const p of positions) {
      if (fetchedRef.current.has(p.symbol)) continue;
      fetchedRef.current.add(p.symbol);
      setInsights((m) => ({ ...m, [p.symbol]: { loading: true } }));
      fetchInsight({ symbol: p.symbol, entry: p.entry, current: p.current, pl_pct: p.pl_pct, days_held: p.days_held })
        .then((ins) => setInsights((m) => ({ ...m, [p.symbol]: ins })))
        .catch(() => setInsights((m) => ({ ...m, [p.symbol]: { insight: "AI insight unavailable.", status: "Caution" } })));
    }
  }, [connected, symbolsKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // Fire a desktop notification when a price crosses an alert level.
  useEffect(() => {
    if (!connected) return;
    const fire = (key, title, body) => {
      if (notifiedRef.current.has(key)) return;
      notifiedRef.current.add(key);
      if ("Notification" in window && Notification.permission === "granted") new Notification(title, { body });
    };
    for (const p of positions) {
      const a = alerts[p.symbol];
      if (!a) continue;
      if (a.stop != null && p.current <= a.stop)
        fire(`${p.symbol}-stop`, `⚠ ${p.symbol} hit stop`, `Now ${money(p.current)} ≤ stop ${money(a.stop)}`);
      if (a.target != null && p.current >= a.target)
        fire(`${p.symbol}-target`, `🎯 ${p.symbol} hit target`, `Now ${money(p.current)} ≥ target ${money(a.target)}`);
    }
  }, [portfolio, alerts]); // eslint-disable-line react-hooks/exhaustive-deps

  const onSetAlert = useCallback(async (symbol, stop, target) => {
    const updated = await apiSetAlert(symbol, stop, target);
    setAlerts(updated);
    notifiedRef.current.delete(`${symbol}-stop`);
    notifiedRef.current.delete(`${symbol}-target`);
  }, []);

  if (!schwab?.configured) return <ConnectPrompt configured={false} />;
  if (!connected)
    return (
      <ConnectPrompt
        configured
        connecting={connecting}
        error={connectError}
        onConnect={onConnect}
        redirectUri={schwab?.redirect_uri}
      />
    );

  const pl = portfolio;
  return (
    <>
      <div className="portfolio-top">
        <Metric label="Account Value" value={money(pl.value)} />
        <Metric label="Buying Power" value={money(pl.buying_power)} sub={`${money(pl.cash)} cash`} />
        <Metric label="Day P&L" value={signedMoney(pl.day_pl)} sub={pct(pl.day_pl_pct)} tone={pl.day_pl >= 0 ? "up" : "down"} />
        <Metric label="Total P&L" value={signedMoney(pl.total_pl)} sub={pct(pl.total_pl_pct)} tone={pl.total_pl >= 0 ? "up" : "down"} />
      </div>

      <div className="scan-meta muted small portfolio-meta">
        {pl.account_mask && <span>Account {pl.account_mask}</span>}
        {updatedAt && <span> · updated {formatClock(updatedAt)} · polls every 1.5 min</span>}
        <button className="btn copy disconnect-btn" onClick={onDisconnect}>Disconnect</button>
      </div>

      <h3 className="section-title">Active Positions</h3>
      {positions.length === 0 ? (
        <p className="muted">No open positions.</p>
      ) : (
        <div className="grid">
          {positions.map((p) => (
            <PositionCard key={p.symbol} position={p} insight={insights[p.symbol]} alert={alerts[p.symbol]} onSetAlert={onSetAlert} />
          ))}
        </div>
      )}

      <h3 className="section-title">Recent Orders</h3>
      <OrdersTable orders={pl.orders ?? []} />
    </>
  );
}
