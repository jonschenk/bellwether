"""Per-position price alerts (stop-loss + profit-target levels), persisted to a
local JSON file. The frontend fires the actual desktop notification when a live
price crosses a level."""

import json
from pathlib import Path

ALERTS_PATH = Path(__file__).resolve().parents[1] / "alerts.json"


def load() -> dict:
    if ALERTS_PATH.exists():
        try:
            return json.loads(ALERTS_PATH.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def set_alert(symbol: str, stop: float | None, target: float | None) -> dict:
    alerts = load()
    symbol = symbol.upper()
    if stop is None and target is None:
        alerts.pop(symbol, None)
    else:
        alerts[symbol] = {"stop": stop, "target": target}
    ALERTS_PATH.write_text(json.dumps(alerts, indent=2))
    return alerts
