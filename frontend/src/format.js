export function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.round(totalSeconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${String(s % 60).padStart(2, "0")}s`;
}

export function formatClock(epochSeconds) {
  return new Date(epochSeconds * 1000).toLocaleTimeString();
}

export function money(n) {
  if (n == null || isNaN(n)) return "—";
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
}

export function signedMoney(n) {
  if (n == null || isNaN(n)) return "—";
  return (n < 0 ? "-" : "+") + money(Math.abs(n));
}

export function pct(n) {
  if (n == null || isNaN(n)) return "—";
  return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
}
