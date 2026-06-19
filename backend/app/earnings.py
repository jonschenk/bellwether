"""Upcoming-earnings lookup for the scan's surviving setups.

Holding a 2-5 day swing trade through an earnings report is a binary gap event the ATR stop can't
protect against, so the scanner flags (and the auto-trade gate will skip) names with earnings
inside the hold window. We only look up earnings for the handful of names that PASS the scan
(~max_results), never the whole universe, so it's a few cheap calls per scan.

Source: yfinance's Ticker.calendar (no extra dependency; get_earnings_dates would need lxml).
Results cache to earnings_cache.json (gitignored) for ~18h — earnings dates barely move — so a
re-scan or the alert engine's repeated cycles don't re-fetch. Unknown/missing earnings return None
(we flag, never hard-block, on missing data).
"""

import concurrent.futures
import datetime as dt
import json
import logging
from pathlib import Path

import yfinance as yf

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).resolve().parents[1] / "earnings_cache.json"
CACHE_TTL_HOURS = 18
_MAX_WORKERS = 8


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache, indent=2))
    except OSError:
        log.exception("could not write earnings cache")


def _next_earnings_date(ticker: str) -> str | None:
    """The next upcoming earnings date (ISO) for one ticker, or None if unknown/none upcoming."""
    try:
        cal = yf.Ticker(ticker).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return None
        today = dt.date.today()
        upcoming = sorted(d for d in dates if isinstance(d, dt.date) and d >= today)
        return upcoming[0].isoformat() if upcoming else None
    except Exception:
        log.debug("earnings lookup failed for %s", ticker, exc_info=True)
        return None


def _is_fresh(entry: dict) -> bool:
    try:
        fetched = dt.datetime.fromisoformat(entry["fetched_at"])
    except (KeyError, ValueError, TypeError):
        return False
    return (dt.datetime.now() - fetched).total_seconds() < CACHE_TTL_HOURS * 3600


def days_to_earnings(tickers: list[str]) -> dict[str, int | None]:
    """Map each ticker -> trading-agnostic calendar days until its next earnings (None if unknown).
    Uses the cache for fresh entries and fetches the rest concurrently."""
    tickers = list(dict.fromkeys(tickers))  # de-dupe, keep order
    if not tickers:
        return {}
    cache = _load_cache()
    now_iso = dt.datetime.now().isoformat(timespec="seconds")

    stale = [t for t in tickers if not _is_fresh(cache.get(t, {}))]
    if stale:
        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            fetched = dict(zip(stale, ex.map(_next_earnings_date, stale)))
        for t, date_iso in fetched.items():
            cache[t] = {"date": date_iso, "fetched_at": now_iso}
        _save_cache(cache)

    today = dt.date.today()
    out: dict[str, int | None] = {}
    for t in tickers:
        date_iso = cache.get(t, {}).get("date")
        if not date_iso:
            out[t] = None
            continue
        try:
            out[t] = (dt.date.fromisoformat(date_iso) - today).days
        except ValueError:
            out[t] = None
    return out
