"""Event log — a chronological, append-only record of every move the engine makes.

The journal records TRADES; this records ACTIONS: when the engine classified the regime, ran a
scan, sized via Kelly, proposed tomorrow's tickets, what the human approved/skipped overnight,
the morning re-check decisions, executions, and any errors. It's the daily audit trail — so a
dev session (Claude Code) can read back exactly what happened on a given day if the user has a
concern ("why didn't it buy X?", "what fired this morning?"), and the phone Monitor can show a
live feed.

One JSON object per line (JSONL), append-only, ET-stamped. Volume is tiny (a few dozen lines a
day), but we soft-cap the file so it can't grow unbounded over months. events.jsonl is gitignored.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

EVENTS_PATH = Path(__file__).resolve().parents[1] / "events.jsonl"
_MAX_BYTES = 2_000_000   # ~2MB soft cap (months of events); trim oldest when exceeded
_KEEP_LINES = 4000       # how many recent lines to keep when trimming

# Categories used across the codebase, documented here so the vocabulary stays consistent:
#   engine   — lifecycle: enabled/disabled, mode change, a cycle starting/skipped
#   regime   — the daily regime classification + which strategy it routes to
#   scan     — a scan ran (how many names cleared)
#   sizing   — fractional-Kelly risk-% decision for the cycle
#   propose  — a ticket was proposed for review (evening build)
#   review   — the human approved/denied a ticket (nightly check-in)
#   recheck  — the morning AI/mechanical re-check verdict per name
#   execute  — a paper order was placed (morning)
#   skip     — a name was NOT traded, with the reason
#   close    — a position closed (stop/target/manual)
#   notify   — a push notification was sent
#   error    — something failed (with context)


def _now_et_iso() -> str:
    from . import alert_engine
    return alert_engine._now_et().isoformat(timespec="seconds")


def _maybe_trim() -> None:
    try:
        if not EVENTS_PATH.exists() or EVENTS_PATH.stat().st_size < _MAX_BYTES:
            return
        lines = EVENTS_PATH.read_text().splitlines()
        EVENTS_PATH.write_text("\n".join(lines[-_KEEP_LINES:]) + "\n")
    except OSError:
        log.exception("event log trim failed")


def log_event(category: str, message: str, **data) -> None:
    """Append one event. `category` is one of the vocabulary above; `message` is a short human
    line; `**data` is any structured context (tickers, prices, counts) for later analysis.
    Never raises — logging must not be able to break a cycle."""
    try:
        rec = {"et": _now_et_iso(), "category": category, "message": message}
        if data:
            rec["data"] = data
        with EVENTS_PATH.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        _maybe_trim()
    except Exception:
        log.exception("event log write failed (%s: %s)", category, message)


def _read() -> list[dict]:
    if not EVENTS_PATH.exists():
        return []
    out = []
    for line in EVENTS_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def tail(n: int = 200) -> list[dict]:
    """The most recent `n` events, newest first."""
    return list(reversed(_read()[-n:]))


def for_day(et_date: str) -> list[dict]:
    """Every event whose ET timestamp falls on `et_date` (YYYY-MM-DD), oldest first."""
    return [e for e in _read() if (e.get("et") or "").startswith(et_date)]
