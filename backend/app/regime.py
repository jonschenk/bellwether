"""Live market-regime classifier for the regime badge.

Computes TODAY's market regime from live SPY data using the SAME bull/chop/bear taxonomy the
backtester's router uses (`backtest._regime_series`), so the live badge and the validated
backtest always agree:
  * bull — SPY above a RISING 200-SMA            -> leader-pullback (momentum)
  * chop — anything in between (range/transition) -> mean-reversion (buy the deep dip)
  * bear — SPY below a FALLING 200-SMA            -> CASH (both edges bleed in a downtrend)

The regime->strategy mapping mirrors backtest.DEFAULT_ROUTER. This is display/decision-support
only — it tells you what the validated router WOULD be doing right now; it never trades.
"""

import logging
import math
import time

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# regime -> (badge label, the strategy the router runs, one-line plain-English what/why)
_REGIME_INFO = {
    "bull": ("Bull", "Leader pullback",
             "Momentum regime: buy pullbacks in market leaders near their highs."),
    "chop": ("Chop", "Mean reversion",
             "Range/transition: buy deep oversold dips in quality names, sell the bounce."),
    "bear": ("Bear", "Cash",
             "Downtrend: sit out. Momentum has nothing to ride and dip-buying catches knives."),
}

_TTL = 3600.0  # the 200-SMA regime barely moves intraday; refresh hourly
_cache: dict = {"ts": 0.0, "data": None}


def current_regime(force: bool = False) -> dict:
    """Today's regime + the strategy the router would run. Cached ~1h; returns an
    'available: False' payload (never raises) if SPY can't be fetched."""
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < _TTL:
        return _cache["data"]
    data = _compute()
    if data:
        _cache.update(ts=now, data=data)
        return data
    return {
        "regime": "unknown", "label": "Unknown", "strategy": "—",
        "description": "Market regime unavailable (SPY fetch failed).", "available": False,
    }


def _compute() -> dict | None:
    try:
        spy = yf.download("SPY", period="2y", interval="1d", auto_adjust=True, progress=False)
        close = spy["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna()  # drop trailing/empty NaN bars (e.g. today's bar before it prints)
        if len(close) < 220:
            return None
        sma200 = close.rolling(200).mean()
        last = float(close.iloc[-1])
        sma_now = float(sma200.iloc[-1])
        sma_prior = float(sma200.iloc[-22])  # ~1 month ago (same 21-bar slope as the backtester)
        # A NaN anywhere here means a bad/partial fetch — report UNAVAILABLE, never a false "bear"
        # (which would wrongly tell the user / the alert engine to hold cash).
        if not all(math.isfinite(x) for x in (last, sma_now, sma_prior)):
            return None
        above = last > sma_now
        rising = sma_now > sma_prior
        if above and rising:
            regime = "bull"
        elif (not above) and (not rising):
            regime = "bear"
        else:
            regime = "chop"
        label, strategy, desc = _REGIME_INFO[regime]
        return {
            "regime": regime,
            "label": label,
            "strategy": strategy,
            "description": desc,
            "spy_price": round(last, 2),
            "spy_sma200": round(sma_now, 2),
            "spy_pct_vs_sma200": round((last / sma_now - 1) * 100, 1),
            "sma200_rising": bool(rising),
            "available": True,
        }
    except Exception:
        log.exception("current_regime computation failed")
        return None
