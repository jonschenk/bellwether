#!/usr/bin/env bash
# Build "Swing Scanner.app" — a real macOS app you can keep in your Dock.
# The .app is a launcher around the existing backend/frontend in this folder,
# so keep this project where it is after building.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

# --- prerequisites (same as start.sh, minus launching) ---
if [ ! -d backend/.venv ]; then
  echo "▸ Creating Python venv…"
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q --upgrade pip
  backend/.venv/bin/pip install -q -r backend/requirements.txt
  touch backend/.venv/.deps-installed
fi
[ -f .env ] || cp .env.example .env

echo "▸ Building frontend…"
(cd frontend && { [ -d node_modules ] || npm install --silent; } && npm run build --silent)

echo "▸ Installing packaging tools…"
(cd electron && { [ -d node_modules/electron-builder ] || npm install --silent; })

# Bake this folder's absolute path so the packaged app finds the backend/frontend.
printf '{\n  "projectRoot": "%s"\n}\n' "$ROOT" > electron/app-config.json

echo "▸ Packaging Swing Scanner.app…"
(cd electron && npx --no-install electron-builder --mac --dir)

APP="$(/usr/bin/find dist-app -maxdepth 2 -name 'Swing Scanner.app' -print -quit)"
if [ -z "$APP" ]; then
  echo "✗ Build finished but the .app wasn't found under dist-app/." >&2
  exit 1
fi

echo
echo "✓ Built: $ROOT/$APP"
echo
echo "To install it:"
echo "  • Drag 'Swing Scanner.app' into your Applications folder, then to the Dock."
echo "  • Or run:  cp -R \"$APP\" /Applications/"
echo
echo "Opening it in Finder…"
open -R "$APP"
