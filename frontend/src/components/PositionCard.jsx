import { useState } from "react";
import { money, signedMoney, pct } from "../format.js";

export default function PositionCard({ position, insight, alert, onSetAlert }) {
  const p = position;
  const up = (p.pl ?? 0) >= 0;
  const [stop, setStop] = useState(alert?.stop ?? "");
  const [target, setTarget] = useState(alert?.target ?? "");
  const [saved, setSaved] = useState(false);

  const ins = insight ?? {};
  const insLoading = !insight || insight.loading;

  const save = async (e) => {
    e.stopPropagation();
    await onSetAlert(p.symbol, stop === "" ? null : Number(stop), target === "" ? null : Number(target));
    setSaved(true);
    setTimeout(() => setSaved(false), 1200);
  };

  return (
    <article className="card position-card">
      <div className="card-head">
        <div className="ticker-row">
          <h2 className="ticker">{p.symbol}</h2>
          <span className="muted small">
            {p.shares} sh{p.days_held != null ? ` · ${p.days_held}d held` : ""}
          </span>
        </div>
        <span className={`badge ${up ? "pl-up" : "pl-down"}`}>
          {signedMoney(p.pl)} ({pct(p.pl_pct)})
        </span>
      </div>

      <div className="stats">
        <div className="stat">
          <span className="stat-label">Entry</span>
          <span className="stat-value">{money(p.entry)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Current</span>
          <span className="stat-value">{money(p.current)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Value</span>
          <span className="stat-value">{money(p.market_value)}</span>
        </div>
        <div className="stat">
          <span className="stat-label">Day P&amp;L</span>
          <span className={`stat-value ${(p.day_pl ?? 0) >= 0 ? "pl-up-text" : "pl-down-text"}`}>
            {signedMoney(p.day_pl)}
          </span>
        </div>
      </div>

      <div className="insight">
        {insLoading ? (
          <span className="summary-pending">Reviewing setup…</span>
        ) : (
          <>
            <span className={`badge status-${(ins.status || "caution").toLowerCase()}`}>{ins.status}</span>
            <span className="insight-text">{ins.insight}</span>
          </>
        )}
      </div>

      <div className="alerts">
        <label className="alert-field">
          <span>Stop</span>
          <input type="number" step="0.01" value={stop} placeholder="—" onChange={(e) => setStop(e.target.value)} />
        </label>
        <label className="alert-field">
          <span>Target</span>
          <input type="number" step="0.01" value={target} placeholder="—" onChange={(e) => setTarget(e.target.value)} />
        </label>
        <button className={`btn copy ${saved ? "copied" : ""}`} onClick={save}>
          {saved ? "Set ✓" : "Set alert"}
        </button>
      </div>
    </article>
  );
}
