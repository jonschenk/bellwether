"""Alert engine — phase 1 of the vision, the "it watches for you" half.

A background scheduler that, when enabled, periodically runs the regime-appropriate scan during
market hours and auto-fills the review queue with new setups — so the user doesn't have to sit
and click Run Scan. It honours the validated router: bull -> leader-pullback, chop -> mean-reversion,
bear -> CASH (the engine sits out and queues nothing — the kill-switch in action).

This module holds the STATE + POLICY (enabled/interval, market-hours, regime->strategy, per-day
dedup so the same name isn't re-alerted all session). The actual loop lives in main.py, where the
scan orchestration and the queue live. Opt-in (off by default) since it does network scans; nothing
it does opens a trade — it only fills the review queue, which the human still approves/denies.

State persists to alert_engine.json (gitignored).
"""

import datetime as dt
import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parents[1] / "alert_engine.json"
_ET = ZoneInfo("America/New_York")

DEFAULTS = {
    "enabled": False,
    "interval_minutes": 30,
    # "review" = fill the queue for approval (intraday); "auto" = auto-open PAPER positions (intraday);
    # "nightly" = the validated model: build tomorrow's tickets after the close, the human reviews
    # overnight, and the approved ones are re-checked and executed at the next open.
    "mode": "nightly",
    "max_positions": 5,      # cap on concurrent open paper positions
    "open_buffer_min": 30,   # intraday modes: don't act until this many min after the 9:30 ET open
    "ai_picks": True,        # run Claude over the mechanical finalists, trade only its "Take" picks (always on)
    "last_run": None,        # ISO timestamp of the last completed run
    "last_status": "idle",   # idle | watching | warming-up | market-closed | bear-cash | auto-traded | built | executed | error
    "last_regime": None,
    "last_strategy": None,
    "last_new_count": 0,
    "alerted": [],           # tickers already alerted today (dedup)
    "alerted_date": None,    # ET date the alerted list belongs to
    "nightly_build_day": None,  # ET date the evening build last ran (once-per-day dedup)
    "nightly_exec_day": None,   # ET date the morning execute last ran (once-per-day dedup)
}

# Nightly morning execute fires this many minutes after the 9:30 open — just long enough for the
# opening prints to settle into a usable quote, while still entering at "the open" (backtest-faithful).
NIGHTLY_EXEC_BUFFER_MIN = 2


def _load() -> dict:
    data = dict(DEFAULTS)
    if STORE_PATH.exists():
        try:
            data.update(json.loads(STORE_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            log.exception("alert_engine.json unreadable; using defaults")
    return data


def _save(data: dict) -> None:
    STORE_PATH.write_text(json.dumps(data, indent=2))


def _now_et() -> dt.datetime:
    return dt.datetime.now(_ET)


def market_open(now: dt.datetime | None = None) -> bool:
    """True during US regular hours (Mon-Fri, 9:30-16:00 ET). Ignores market holidays —
    a scan on a holiday just re-finds the prior session's data, which is harmless."""
    now = now or _now_et()
    if now.weekday() >= 5:  # Sat/Sun
        return False
    t = now.time()
    return dt.time(9, 30) <= t <= dt.time(16, 0)


def state() -> dict:
    """The persisted state plus a couple of computed, display-only fields."""
    d = _load()
    return {**d, "market_open": market_open(), "et_now": _now_et().isoformat(timespec="seconds")}


def configure(enabled: bool | None = None, interval_minutes: int | None = None,
              mode: str | None = None, max_positions: int | None = None,
              open_buffer_min: int | None = None, ai_picks: bool | None = None) -> dict:
    d = _load()
    if ai_picks is not None:
        d["ai_picks"] = bool(ai_picks)
    if enabled is not None:
        d["enabled"] = bool(enabled)
        d["last_status"] = "watching" if enabled else "idle"
    if interval_minutes is not None:
        d["interval_minutes"] = max(5, min(int(interval_minutes), 240))
    if mode in ("review", "auto", "nightly"):
        d["mode"] = mode
    if max_positions is not None:
        d["max_positions"] = max(1, min(int(max_positions), 50))
    if open_buffer_min is not None:
        d["open_buffer_min"] = max(0, min(int(open_buffer_min), 120))
    _save(d)
    return state()


def minutes_since_open(now: dt.datetime | None = None) -> int:
    """Minutes since the 9:30 ET open today (negative before the open)."""
    now = now or _now_et()
    return now.hour * 60 + now.minute - (9 * 60 + 30)


def in_open_warmup(now: dt.datetime | None = None) -> bool:
    """True during the post-open warmup window (skip the volatile open). 0 buffer disables it."""
    buf = _load().get("open_buffer_min", 0)
    return buf > 0 and 0 <= minutes_since_open(now) < buf


def auto_mode() -> bool:
    return _load().get("mode") == "auto"


def mode() -> str:
    return _load().get("mode", "review")


def next_session_date(now: dt.datetime | None = None) -> str:
    """The next trading session's ET date (skips weekends; ignores holidays, same harmless caveat
    as market_open). Used by the evening build to tag tonight's tickets for tomorrow's open."""
    now = now or _now_et()
    d = now.date() + dt.timedelta(days=1)
    while d.weekday() >= 5:  # roll Sat/Sun forward to Monday
        d += dt.timedelta(days=1)
    return d.isoformat()


def nightly_build_due(now: dt.datetime | None = None) -> bool:
    """Evening build fires once per weekday after the close (>=16:00 ET), if it hasn't run today."""
    now = now or _now_et()
    if now.weekday() >= 5 or now.time() < dt.time(16, 0):
        return False
    return _load().get("nightly_build_day") != now.date().isoformat()


def nightly_exec_due(now: dt.datetime | None = None) -> bool:
    """Morning execute fires once per weekday shortly after the open, if it hasn't run today."""
    now = now or _now_et()
    if not market_open(now):
        return False
    if minutes_since_open(now) < NIGHTLY_EXEC_BUFFER_MIN:
        return False
    return _load().get("nightly_exec_day") != now.date().isoformat()


def mark_nightly_build() -> None:
    d = _load()
    d["nightly_build_day"] = _now_et().date().isoformat()
    _save(d)


def mark_nightly_exec() -> None:
    d = _load()
    d["nightly_exec_day"] = _now_et().date().isoformat()
    _save(d)


def due(now: dt.datetime | None = None) -> bool:
    """Should the engine run a cycle now? Enabled + interval elapsed since the last run.
    (Market-hours gating is handled in the loop so it can report 'market-closed' distinctly.)"""
    d = _load()
    if not d["enabled"]:
        return False
    if not d["last_run"]:
        return True
    try:
        last = dt.datetime.fromisoformat(d["last_run"])
    except (ValueError, TypeError):
        return True
    elapsed_min = (dt.datetime.now() - last).total_seconds() / 60
    return elapsed_min >= d["interval_minutes"]


def enabled() -> bool:
    return _load()["enabled"]


def exclude_today() -> set[str]:
    """Tickers already alerted today (reset each ET trading day) — passed to queue.build so a
    name surfaced earlier in the session isn't re-queued every cycle."""
    d = _load()
    today = _now_et().date().isoformat()
    if d.get("alerted_date") != today:
        return set()
    return set(d.get("alerted", []))


def mark(status: str) -> None:
    """Update just the status (not last_run) — used for 'market-closed'/'error' so `due()` stays
    true and the loop re-checks each tick (cheap) and fires promptly when conditions clear."""
    d = _load()
    if d["last_status"] != status:
        d["last_status"] = status
        _save(d)


def record(status: str, regime: str | None = None, strategy: str | None = None,
           new_tickers: list[str] | None = None) -> dict:
    """Stamp a completed cycle: status + regime/strategy + merge the newly-alerted tickers
    into today's dedup set (resetting it when the ET date rolls over)."""
    d = _load()
    today = _now_et().date().isoformat()
    if d.get("alerted_date") != today:
        d["alerted"] = []
        d["alerted_date"] = today
    new_tickers = new_tickers or []
    d["alerted"] = sorted(set(d["alerted"]) | set(new_tickers))
    d["last_run"] = dt.datetime.now().isoformat(timespec="seconds")
    d["last_status"] = status
    d["last_regime"] = regime
    d["last_strategy"] = strategy
    d["last_new_count"] = len(new_tickers)
    _save(d)
    return state()
