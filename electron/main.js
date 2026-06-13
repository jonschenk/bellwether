const { app, BrowserWindow, dialog } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");

const BACKEND_PORT = 8765;
const HEALTH_URL = `http://127.0.0.1:${BACKEND_PORT}/api/health`;

// Where the project (backend venv + frontend build) lives. In dev that's the
// parent dir; in a packaged .app __dirname is inside the bundle, so we read the
// absolute path baked in at build time (overridable via env for portability).
function resolveProjectRoot() {
  if (process.env.SWING_SCANNER_HOME) return process.env.SWING_SCANNER_HOME;
  if (app.isPackaged) {
    try {
      const cfg = JSON.parse(fs.readFileSync(path.join(__dirname, "app-config.json"), "utf8"));
      if (cfg.projectRoot) return cfg.projectRoot;
    } catch {
      /* fall through to dev default */
    }
  }
  return path.join(__dirname, "..");
}

const ROOT = resolveProjectRoot();

let backendProcess = null;

function checkHealth() {
  return new Promise((resolve) => {
    const req = http.get(HEALTH_URL, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(1000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

function startBackend() {
  const backendDir = path.join(ROOT, "backend");
  const venvPython = path.join(backendDir, ".venv", "bin", "python");
  if (!fs.existsSync(venvPython)) {
    dialog.showErrorBox(
      "Backend not set up",
      `Python venv not found at ${venvPython}.\n\nRun ./start.sh (or follow the README setup) first.`,
    );
    app.quit();
    return null;
  }
  const proc = spawn(
    venvPython,
    ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(BACKEND_PORT)],
    { cwd: backendDir, stdio: "inherit" },
  );
  proc.on("exit", (code) => {
    if (code !== null && code !== 0 && !app.isQuitting) {
      console.error(`Backend exited with code ${code}`);
    }
  });
  return proc;
}

async function waitForBackend(attempts = 60) {
  for (let i = 0; i < attempts; i++) {
    if (await checkHealth()) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

async function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: "#0b0e14",
    title: "Swing Scanner",
    titleBarStyle: "hiddenInset",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (process.env.DEV) {
    // Dev mode: expects `npm run dev` running in frontend/
    await win.loadURL("http://localhost:5173");
    win.webContents.openDevTools({ mode: "detach" });
  } else {
    await win.loadFile(path.join(ROOT, "frontend", "dist", "index.html"));
  }
}

app.whenReady().then(async () => {
  // Reuse an already-running backend (e.g. started manually for debugging),
  // otherwise spawn our own.
  const alreadyRunning = await checkHealth();
  if (!alreadyRunning) {
    backendProcess = startBackend();
    if (!backendProcess) return;
    const up = await waitForBackend();
    if (!up) {
      dialog.showErrorBox(
        "Backend failed to start",
        "The Python backend did not respond on port 8765 within 30 seconds. Check the terminal output.",
      );
      app.quit();
      return;
    }
  }
  await createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("before-quit", () => {
  app.isQuitting = true;
  if (backendProcess) backendProcess.kill();
});

app.on("window-all-closed", () => {
  app.quit();
});
