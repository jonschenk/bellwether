"""In-app paper broker - simulated trading modeled on the SHAPE of Schwab's Trader
API order flow, so the execution path is faithful to the live target and going live
later is a broker swap, not a rewrite.

How it works:
- A paper BUY fills at the current live quote and opens a position. The trade is
  logged to the journal with its full entry snapshot (journal.log_trade).
- A background monitor marks open positions against the live quote every few seconds
  and runs the bracket: hit the stop -> close as a loss; hit the target -> close as a
  win. You can also close manually at the live price.
- A simple cash/equity ledger tracks the account like a Schwab account response.

Paper only: no real orders, no real money. Needs market hours for live quotes (off
hours the quote just doesn't move). Account + open positions persist to
paper_account.json (gitignored); closed trades live in the journal for the scoreboard.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from . import journal
from .config import ScanSettings
from .universe import bulk_quote

log = logging.getLogger(__name__)

ACCOUNT_PATH = Path(__file__).resolve().parents[1] / "paper_account.json"
TICK_SECONDS = 5  # how often the bracket monitor re-marks open positions

_regime_cache: tuple[float, str] | None = None  # (fetched_at, label), refreshed ~hourly


def _load() -> dict:
    if ACCOUNT_PATH.exists():
        try:
            return json.loads(ACCOUNT_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.exception("paper_account.json unreadable; starting fresh")
    return {"starting_cash": 0.0, "cash": 0.0, "positions": {}}


def _save(acct: dict) -> None:
    ACCOUNT_PATH.write_text(json.dumps(acct, indent=2))


def reset(capital: float) -> dict:
    """Start a fresh paper account with `capital` cash and no open positions.
    (Closed trades stay in the journal as history.)"""
    acct = {"starting_cash": round(capital, 2), "cash": round(capital, 2), "positions": {}}
    _save(acct)
    return account()


def _quote(tickers: list[str]) -> dict[str, float]:
    """Current price per ticker (Yahoo bulk quote). {} off-hours/unreachable."""
    out = {}
    for sym, pv in (bulk_quote(tickers) or {}).items():
        if pv and pv[0]:
            out[sym] = float(pv[0])
    return out


def buy(stock: dict) -> dict:
    """Place a paper market buy for one scanned setup. Fills at the current live
    price; stop/target come from the setup's plan. Returns the account snapshot or
    an {error} dict if it can't be sized/afforded."""
    plan = stock.get("plan") or {}
    shares = plan.get("shares") or 0
    if shares <= 0:
        return {"error": "No share plan for this setup."}

    fill = _quote([stock["ticker"]]).get(stock["ticker"]) or plan.get("entry")
    if not fill:
        return {"error": "Could not get a fill price (market may be closed)."}

    acct = _load()
    cost = round(shares * fill, 2)
    if cost > acct["cash"]:
        return {"error": f"Not enough paper cash (${acct['cash']:,.0f}) for {shares} x ${fill:.2f}."}

    # Fill the entry at the live price; the trade's stop/target are the planned levels.
    sized = dict(stock)
    sized["plan"] = {**plan, "entry": round(fill, 2)}
    vid, vparams = _active_variation()
    trade = journal.log_trade(
        sized, vid,
        variation_params=vparams,
        decision=(stock.get("trade_case") or {}).get("recommendation"),
        market_regime=_market_regime(),
    )

    acct["cash"] = round(acct["cash"] - cost, 2)
    acct["positions"][trade["id"]] = {
        "ticker": stock["ticker"],
        "name": stock.get("name", ""),
        "shares": shares,
        "entry": round(fill, 2),
        "stop": plan.get("stop"),
        "target": plan.get("target"),
        "opened_at": trade["opened_at"],
        "decision": trade.get("decision"),
        "current": round(fill, 2),
        "mae": round(fill, 2),  # lowest price seen while held
        "mfe": round(fill, 2),  # highest price seen while held
    }
    _save(acct)
    return account()


def auto_execute(results: list[dict], max_positions: int = 5, exclude: set[str] | None = None) -> dict:
    """Auto-open PAPER positions for the best setups, with guardrails. PAPER-ONLY by construction —
    this only ever calls the paper buy() path; it has no route to a real broker. Used by the alert
    engine's auto-trade mode. Guardrails: cap concurrent positions, skip names already held / in
    `exclude` / flagged for imminent earnings / unsizable. Returns what it bought + why it skipped."""
    exclude = exclude or set()
    held = {p["ticker"] for p in account().get("positions", [])}
    open_count = len(held)
    bought: list[str] = []
    skipped: list[dict] = []

    for r in sorted(results, key=lambda x: x.get("setup_score", 0), reverse=True):
        ticker = r["ticker"]
        if open_count >= max_positions:
            skipped.append({"ticker": ticker, "reason": "max positions reached"})
            continue
        if ticker in held or ticker in exclude:
            continue  # already held or already acted on today
        if r.get("earnings_soon"):
            skipped.append({"ticker": ticker, "reason": f"earnings in {r.get('days_to_earnings')}d"})
            continue
        if not (r.get("plan") or {}).get("shares"):
            skipped.append({"ticker": ticker, "reason": "not sizable"})
            continue
        res = buy(r)
        if res.get("error"):
            skipped.append({"ticker": ticker, "reason": res["error"]})
            continue
        bought.append(ticker)
        held.add(ticker)
        open_count += 1

    return {"bought": bought, "skipped": skipped, "account": account()}


def close(trade_id: str, exit_price: float | None = None, reason: str = "manual") -> dict:
    """Close an open paper position at exit_price (default: current live price)."""
    acct = _load()
    pos = acct["positions"].get(trade_id)
    if pos is None:
        return {"error": "No such open paper position."}
    px = exit_price or _quote([pos["ticker"]]).get(pos["ticker"]) or pos["current"]
    acct["cash"] = round(acct["cash"] + pos["shares"] * px, 2)
    try:
        journal.close_trade(trade_id, round(px, 2), exit_reason=reason, mae=pos.get("mae"), mfe=pos.get("mfe"))
    except (KeyError, ValueError):
        log.exception("journal close failed for %s", trade_id)
    del acct["positions"][trade_id]
    _save(acct)
    return account()


def _mark_and_bracket(acct: dict, prices: dict[str, float]) -> bool:
    """Update marks + MAE/MFE and fire stop/target. Returns True if anything changed."""
    changed = False
    for tid, pos in list(acct["positions"].items()):
        px = prices.get(pos["ticker"])
        if not px:
            continue
        pos["current"] = round(px, 2)
        pos["mae"] = round(min(pos["mae"], px), 2)
        pos["mfe"] = round(max(pos["mfe"], px), 2)
        changed = True
        if pos["stop"] and px <= pos["stop"]:
            _save(acct)  # persist the mark before the close re-reads
            close(tid, pos["stop"], reason="stop")
            acct.update(_load())
        elif pos["target"] and px >= pos["target"]:
            _save(acct)
            close(tid, pos["target"], reason="target")
            acct.update(_load())
    return changed


async def monitor_loop() -> None:
    """Background task: every few seconds, mark open positions to the live quote and
    run the bracket. Does nothing while there are no open positions."""
    while True:
        try:
            acct = _load()
            tickers = [p["ticker"] for p in acct["positions"].values()]
            if tickers:
                prices = await asyncio.to_thread(_quote, tickers)
                if _mark_and_bracket(acct, prices):
                    _save(acct)
        except Exception:
            log.exception("paper monitor tick failed")
        await asyncio.sleep(TICK_SECONDS)


def account() -> dict:
    """Marked account snapshot: cash, open positions with unrealized P&L, equity."""
    acct = _load()
    positions = []
    open_pnl = 0.0
    invested = 0.0
    for tid, p in acct["positions"].items():
        cur = p.get("current") or p["entry"]
        upnl = round((cur - p["entry"]) * p["shares"], 2)
        rps = (p["entry"] - p["stop"]) if p.get("stop") else None
        positions.append(
            {
                "id": tid,
                **{k: p[k] for k in ("ticker", "name", "shares", "entry", "stop", "target", "opened_at", "decision")},
                "current": cur,
                "unrealized": upnl,
                "unrealized_pct": round((cur / p["entry"] - 1) * 100, 2) if p["entry"] else 0.0,
                "r": round((cur - p["entry"]) / rps, 2) if rps else None,
            }
        )
        open_pnl += upnl
        invested += cur * p["shares"]
    positions.sort(key=lambda x: x["opened_at"])
    equity = round(acct["cash"] + invested, 2)
    total_pnl = round(equity - acct["starting_cash"], 2) if acct["starting_cash"] else 0.0
    return {
        "starting_cash": acct["starting_cash"],
        "cash": acct["cash"],
        "equity": equity,
        "open_pnl": round(open_pnl, 2),
        "realized_pnl": round(total_pnl - open_pnl, 2),  # closed-trade P&L = total minus unrealized
        "total_pnl": total_pnl,
        "positions": positions,
    }


def _active_variation() -> tuple[str, dict | None]:
    """The active strategy variation's id + param snapshot (seed one if needed), so each paper
    trade records the exact strategy knobs it ran under."""
    try:
        from . import strategy

        v = strategy.get_active() or strategy.ensure_seeded(ScanSettings())
        return v["id"], v.get("params")
    except Exception:
        return "v1", None


def _market_regime() -> str | None:
    """Broad-market regime label from SPY (uptrend / downtrend / choppy), cached ~1h. Stored on
    each trade so outcomes can later be sliced by the regime they were taken in."""
    global _regime_cache
    if _regime_cache and time.time() - _regime_cache[0] < 3600:
        return _regime_cache[1]
    try:
        import yfinance as yf

        close = yf.Ticker("SPY").history(period="1y")["Close"]
        sma200 = close.rolling(200).mean()
        c, s = float(close.iloc[-1]), float(sma200.iloc[-1])
        rising = sma200.iloc[-1] > sma200.iloc[-22]
        label = "uptrend" if (c > s and rising) else "downtrend" if (c < s and not rising) else "choppy"
        _regime_cache = (time.time(), label)
        return label
    except Exception:
        log.exception("market regime fetch failed")
        return None
