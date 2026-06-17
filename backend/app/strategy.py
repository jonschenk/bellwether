"""Strategy variations — named parameter sets the scanner runs, so we can
experiment across them and iterate toward what wins over time.

A *variation* is a snapshot of the tunable scan parameters plus metadata: a
stable id (v1, v2, …), a name, when it was created, which variation it was
derived from, and free-form notes. One variation is "active" at a time; every
scan and every logged trade is tagged with the active variation's id, so
performance can be compared across variations (see journal.py). Iterating the
strategy = clone a variation, tweak a few params, run it, and compare winrate /
expectancy over a meaningful sample.

This is the substrate for the long game. The in-app advisor proposes tweaks into
STRATEGY.md; a human (with Claude Code in a dev session) turns the good ones into
the next variation — a deliberate, git-tracked version bump, never a silent
self-rewrite by the running app.
"""

import datetime as dt
import json
import logging
from pathlib import Path

from .config import ScanSettings

log = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parents[1] / "strategies.json"

# The scan knobs that define a strategy. Account fields (capital, universe,
# ai_top_n, cache_minutes) are NOT strategy — they live in settings.json.
STRATEGY_PARAMS = [
    "min_price",
    "min_avg_volume",
    "adx_min",
    "rsi_threshold",
    "rsi_floor",
    "atr_pct_min",
    "near_high_pct",
    "min_above_low_pct",
    "min_rs_rating",
    "atr_stop_mult",
    "reward_mult",
    "risk_pct",
    "max_position_pct",
]


def params_from_settings(settings: ScanSettings) -> dict:
    """Pull just the strategy knobs out of a full settings object."""
    return {k: getattr(settings, k) for k in STRATEGY_PARAMS}


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _load() -> dict:
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.exception("strategies.json unreadable; starting fresh")
    return {"active": None, "variations": {}}


def _save(data: dict) -> None:
    STORE_PATH.write_text(json.dumps(data, indent=2))


def _next_id(variations: dict) -> str:
    n = 1 + max((int(k[1:]) for k in variations if k.startswith("v") and k[1:].isdigit()), default=0)
    return f"v{n}"


def list_variations() -> dict:
    return _load()["variations"]


def active_id() -> str | None:
    return _load().get("active")


def get_active() -> dict | None:
    data = _load()
    return data["variations"].get(data.get("active"))


def set_active(variation_id: str) -> None:
    data = _load()
    if variation_id not in data["variations"]:
        raise KeyError(f"No such variation: {variation_id}")
    data["active"] = variation_id
    _save(data)


def create_variation(name: str, params: dict, parent: str | None = None, notes: str = "") -> dict:
    """Add a new variation and make it active. `params` is validated against the
    strategy knobs (extra keys dropped, full ScanSettings validation applied)."""
    clean = {k: params[k] for k in STRATEGY_PARAMS if k in params}
    ScanSettings(**clean)  # raises on an out-of-range value before we persist it

    data = _load()
    vid = _next_id(data["variations"])
    variation = {
        "id": vid,
        "name": name,
        "created_at": _now(),
        "parent": parent,
        "params": clean,
        "notes": notes,
    }
    data["variations"][vid] = variation
    data["active"] = vid
    _save(data)
    return variation


def derive(parent_id: str, name: str, changes: dict, notes: str = "") -> dict:
    """Clone an existing variation with a few params changed — the core
    'experiment with a tweak' operation."""
    data = _load()
    parent = data["variations"].get(parent_id)
    if parent is None:
        raise KeyError(f"No such variation: {parent_id}")
    params = {**parent["params"], **{k: v for k, v in changes.items() if k in STRATEGY_PARAMS}}
    return create_variation(name, params, parent=parent_id, notes=notes)


def ensure_seeded(settings: ScanSettings) -> dict:
    """Create the baseline 'v1' from the current settings if nothing exists yet."""
    data = _load()
    if data["variations"]:
        return data["variations"][data["active"]]
    return create_variation("baseline", params_from_settings(settings), notes="Seeded from current settings.")


def apply_active(settings: ScanSettings) -> ScanSettings:
    """Return a copy of `settings` with the active variation's params overlaid,
    so a scan runs under the active strategy while keeping account fields."""
    active = get_active()
    if not active:
        return settings
    return settings.model_copy(update=active["params"])
