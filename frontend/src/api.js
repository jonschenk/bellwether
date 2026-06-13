const BASE_URL = "http://127.0.0.1:8765";

async function request(path, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json();
}

export const getSettings = () => request("/api/settings");
export const saveSettings = (settings) =>
  request("/api/settings", { method: "PUT", body: JSON.stringify(settings) });
export const startScan = (fresh = false) =>
  request(`/api/scan?fresh=${fresh ? "true" : "false"}`, { method: "POST" });
export const refreshScan = () => request("/api/refresh", { method: "POST" });
export const analyzeTicker = (ticker) =>
  request("/api/analyze", { method: "POST", body: JSON.stringify({ ticker }) });
export const getScanStatus = () => request("/api/scan/status");
export const getHealth = () => request("/api/health");

// --- Schwab / portfolio ---
export const getSchwabStatus = () => request("/api/schwab/status");
export const getSchwabAuthUrl = () => request("/api/schwab/auth-url");
export const postSchwabCallback = (callbackUrl) =>
  request("/api/schwab/callback", { method: "POST", body: JSON.stringify({ callback_url: callbackUrl }) });
export const postSchwabDisconnect = () => request("/api/schwab/disconnect", { method: "POST" });
export const getPortfolio = () => request("/api/portfolio");
export const fetchInsight = (position) =>
  request("/api/portfolio/insight", { method: "POST", body: JSON.stringify(position) });
export const getAlerts = () => request("/api/alerts");
export const setAlert = (symbol, stop, target) =>
  request("/api/alerts", { method: "PUT", body: JSON.stringify({ symbol, stop, target }) });
