const { contextBridge, ipcRenderer } = require("electron");

// Minimal, safe bridge: the renderer can ask the main process to run the Schwab
// OAuth window and get back the redirect URL (which contains the auth code).
contextBridge.exposeInMainWorld("electronAPI", {
  schwabOAuth: (authUrl, redirectUri) => ipcRenderer.invoke("schwab-oauth", { authUrl, redirectUri }),
});
