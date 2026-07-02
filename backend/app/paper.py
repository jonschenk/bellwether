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
import datetime as dt
import json
import logging
import math
import os
import threading
import uuid
from pathlib import Path

from . import alert_engine
from . import journal
from . import notify
from .config import ScanSettings
from .universe import bulk_quote

log = logging.getLogger(__name__)


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _et_today() -> str:
    return alert_engine._now_et().date().isoformat()


def _market_open() -> bool:
    return alert_engine.market_open()

ACCOUNT_PATH = Path(__file__).resolve().parents[1] / "paper_account.json"
TICK_SECONDS = 5  # how often the bracket monitor re-marks open positions

# One lock for every load->mutate->save on the ledger. Writers run on BOTH the event-loop thread
# (endpoints, the monitor tick) and worker threads (the morning execute's to_thread buy, the daily
# interest post), so without it a slow quote fetch inside the monitor tick could save a stale
# snapshot over a buy/close that landed mid-tick (lost update on the money file). RLock because
# the monitor's bracket path calls close() re-entrantly on the same thread. Never held across a
# network call: quotes are always resolved before acquiring it.
_LEDGER_LOCK = threading.RLock()

# Per-side slippage haircut on market fills (matches the backtester's 5bps default), so the paper
# scoreboard isn't rosier than reality: a market buy pays UP, a sell/stop/target fills LOWER. Limit
# fills are exempt (you get your price or better — that's the point of using one).
SLIPPAGE_BPS = 5.0

# Idle cash isn't dead money at a real brokerage: it sweeps into a money-market fund / T-bills.
# Model that so the paper book mirrors reality and dry powder still earns its keep. Posted once per
# calendar day on the cash balance, at roughly the current T-bill yield.
CASH_APY = 0.045

# Progressive ("exponential") trail tighten. As a position extends in profit (R reached by its
# high-water mark), the trailing-stop distance decays from the full initial-stop distance toward a
# floor fraction of it: dist = init_dist * (floor_frac + (1-floor_frac)*exp(-K * R_reached)). This
# locks more of a stalled winner instead of giving the whole ATR distance back. The "medium" setting
# (K=0.7, floor=1/3 of the base, i.e. the 0.5x of a 1.5x base trail) was validated out-of-sample on
# 2007-2026 / 663 names: vs the pure trail it lifted OOS expectancy ~25% and cut max drawdown ~36%.
# Mirrors backtest.py's "trail_tighten" exit (TIGHTEN_K / TIGHTEN_FLOOR) so live and backtest agree.
TRAIL_TIGHTEN_K = 0.7
TRAIL_TIGHTEN_FLOOR_FRAC = 1.0 / 3.0


def _load() -> dict:
    if ACCOUNT_PATH.exists():
        try:
            acct = json.loads(ACCOUNT_PATH.read_text())
            acct.setdefault("orders", {})  # resting MOO/limit orders (added later; back-compat)
            acct.setdefault("interest_earned", 0.0)  # cumulative money-market interest on idle cash
            return acct
        except (json.JSONDecodeError, OSError):
            log.exception("paper_account.json unreadable; starting fresh")
    return {"starting_cash": 0.0, "cash": 0.0, "positions": {}, "orders": {}}


def _save(acct: dict) -> None:
    # Atomic write (tmp + rename): a truncate-in-place write can be read half-written by another
    # thread (equity log, API polls), and _load's "unreadable -> start fresh" guard would then hand
    # a ZEROED account to the next writer. os.replace makes readers see old-or-new, never torn.
    tmp = ACCOUNT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(acct, indent=2))
    os.replace(tmp, ACCOUNT_PATH)


def reset(capital: float) -> dict:
    """Start a fresh paper account with `capital` cash, no open positions, no resting orders.
    (Closed trades stay in the journal as history.)"""
    acct = {"starting_cash": round(capital, 2), "cash": round(capital, 2), "positions": {}, "orders": {},
            "interest_earned": 0.0, "last_interest_date": _et_today()}
    with _LEDGER_LOCK:
        _save(acct)
    return account()


def _quote(tickers: list[str]) -> dict[str, float]:
    """Current price per ticker (Yahoo bulk quote). {} off-hours/unreachable."""
    out = {}
    for sym, pv in (bulk_quote(tickers) or {}).items():
        if pv and pv[0]:
            out[sym] = float(pv[0])
    return out


def _open_position(acct: dict, stock: dict, fill: float,
                   variation_id: str | None = None, variation_params: dict | None = None) -> dict:
    """Open a position in `acct` at `fill` (filled price already incl. any slippage), logging the
    trade to the journal. Mutates acct; the caller saves. Returns {trade} or {error}.
    `variation_id`/`variation_params` tag the trade with what actually ran (the engine passes the
    router leg); a manual buy leaves them None and falls back to the active picker variation."""
    plan = stock.get("plan") or {}
    shares = plan.get("shares") or 0
    fill = round(fill, 2)
    cost = round(shares * fill, 2)
    if cost > acct["cash"]:
        return {"error": f"Not enough paper cash (${acct['cash']:,.0f}) for {shares} x ${fill:.2f}."}

    sized = dict(stock)
    sized["plan"] = {**plan, "entry": fill}
    vid, vparams = (variation_id, variation_params) if variation_id else _active_variation()
    trade = journal.log_trade(
        sized, vid,
        variation_params=vparams,
        decision=(stock.get("trade_case") or {}).get("recommendation"),
        market_regime=_market_regime(),
    )
    acct["cash"] = round(acct["cash"] - cost, 2)
    stop = plan.get("stop")
    acct["positions"][trade["id"]] = {
        "ticker": stock["ticker"],
        "name": stock.get("name", ""),
        "shares": shares,
        "entry": fill,
        "stop": stop,                 # the INITIAL stop — the risk denominator + display (unchanged by the trail)
        "target": plan.get("target"),
        "opened_at": trade["opened_at"],
        "decision": trade.get("decision"),
        "current": fill,
        "mae": fill,  # lowest price seen while held
        "mfe": fill,  # highest price seen while held
        # trailing-stop state: the trail rides `init_stop_dist` below the high-water mark, ratcheting
        # up only. Distance == the initial stop distance (the validated trail reused the stop multiple).
        "init_stop_dist": round(fill - stop, 2) if stop else None,
        "trail_stop": stop,           # starts at the initial stop, then ratchets up with the high
    }
    return {"trade": trade}


def buy(stock: dict, variation_id: str | None = None, variation_params: dict | None = None) -> dict:
    """Place a paper MARKET buy: fill immediately at the live quote (+slippage). Returns the
    account snapshot or an {error}."""
    plan = stock.get("plan") or {}
    if (plan.get("shares") or 0) <= 0:
        return {"error": "No share plan for this setup."}
    fill = _quote([stock["ticker"]]).get(stock["ticker"]) or plan.get("entry")
    if not fill:
        return {"error": "Could not get a fill price (market may be closed)."}
    fill = fill * (1 + SLIPPAGE_BPS / 10000)  # market buy pays up (slippage)
    with _LEDGER_LOCK:
        acct = _load()
        res = _open_position(acct, stock, fill, variation_id, variation_params)
        if res.get("error"):
            return res
        _save(acct)
    return account()


def place_order(stock: dict, order_type: str, limit_price: float | None = None,
                variation_id: str | None = None, variation_params: dict | None = None) -> dict:
    """Place a RESTING paper order that fills later: 'moo' at the next market open, 'limit' when
    the price reaches the planned entry. The monitor loop fills it (see _process_orders)."""
    plan = stock.get("plan") or {}
    shares = plan.get("shares") or 0
    if shares <= 0:
        return {"error": "No share plan for this setup."}
    if order_type == "limit" and not limit_price:
        limit_price = plan.get("entry")
    with _LEDGER_LOCK:
        acct = _load()
        oid = uuid.uuid4().hex[:8]
        acct["orders"][oid] = {
            "id": oid,
            "ticker": stock["ticker"],
            "name": stock.get("name", ""),
            "type": order_type,                       # "moo" | "limit"
            "limit_price": round(limit_price, 2) if (order_type == "limit" and limit_price) else None,
            "shares": shares,
            "stop": plan.get("stop"),
            "target": plan.get("target"),
            "stock": stock,                           # snapshot so the fill can open + journal it
            "variation_id": variation_id,             # carried to the fill so the journal tags it right
            "variation_params": variation_params,
            "placed_at": _now(),
            # the ET session date if placed during market hours, else None (placed while closed)
            "placed_session": _et_today() if _market_open() else None,
        }
        _save(acct)
    return account()


def submit(stock: dict, settings, variation_id: str | None = None, variation_params: dict | None = None) -> dict:
    """Dispatch a paper order per the account's order-type preference: market fills now;
    moo/limit rest until their condition. The one entry point used by manual buys, the queue's
    Approve, and auto-trade — so all three honour the chosen order type."""
    otype = getattr(settings, "paper_order_type", "market")
    if otype in ("moo", "limit"):
        return place_order(stock, otype, variation_id=variation_id, variation_params=variation_params)
    return buy(stock, variation_id, variation_params)


def cancel_order(order_id: str) -> dict:
    with _LEDGER_LOCK:
        acct = _load()
        if acct["orders"].pop(order_id, None) is None:
            return {"error": "No such resting order."}
        _save(acct)
    return account()


def auto_execute(results: list[dict], max_positions: int = 5, exclude: set[str] | None = None,
                 variation_id: str | None = None, variation_params: dict | None = None) -> dict:
    """Auto-open PAPER positions for the best setups, with guardrails. PAPER-ONLY by construction —
    this only ever calls the paper buy() path; it has no route to a real broker. Used by the alert
    engine's auto-trade mode. `variation_id`/`variation_params` tag each trade with what ran (the
    router leg). Guardrails: cap concurrent positions, skip names already held / in `exclude` /
    flagged for imminent earnings / unsizable. Returns what it bought + why it skipped."""
    from .config import load_settings
    from . import risk
    settings = load_settings()
    exclude = exclude or set()
    acct = account()
    # open positions AND resting orders both count against the cap / dedup
    held = {p["ticker"] for p in acct.get("positions", [])} | {o["ticker"] for o in acct.get("orders", [])}
    open_count = len(acct.get("positions", [])) + len(acct.get("orders", []))
    equity, cash = acct["equity"], acct["cash"]
    # Equity-scaled slot count, still under any explicit caller ceiling (the intraday auto-trade knob).
    cap = min(risk.target_slots(equity, settings), max_positions)
    free_slots = max(0, cap - open_count)
    skipped: list[dict] = []

    # Eligible names (skip held / excluded / earnings-soon / unsizable), ranked by setup score.
    eligible = []
    for r in sorted(results, key=lambda x: x.get("setup_score", 0), reverse=True):
        ticker = r["ticker"]
        if ticker in held or ticker in exclude:
            continue
        if r.get("earnings_soon"):
            skipped.append({"ticker": ticker, "reason": f"earnings in {r.get('days_to_earnings')}d"})
            continue
        if not (r.get("plan") or {}).get("shares"):
            skipped.append({"ticker": ticker, "reason": "not sizable"})
            continue
        eligible.append(r)

    # Budget-aware allocation: size off REAL equity/cash, per-position cap, fill best-first into the
    # free slots (same allocator the nightly path uses — no more sizing each as if full cash were free).
    taken, dropped = risk.allocate(eligible, settings, equity, cash, free_slots)
    for d in dropped:
        skipped.append({"ticker": d["ticker"], "reason": d["reason"]})

    bought: list[str] = []
    for r in taken:  # r already carries the budget-sized plan
        res = submit(r, settings, variation_id, variation_params)  # honours the order type; tags the trade
        if res.get("error"):
            skipped.append({"ticker": r["ticker"], "reason": res["error"]})
            continue
        bought.append(r["ticker"])

    return {"bought": bought, "skipped": skipped, "account": account()}


def close(trade_id: str, exit_price: float | None = None, reason: str = "manual") -> dict:
    """Close an open paper position at exit_price (default: current live price)."""
    if exit_price is None:
        # Resolve the live quote BEFORE taking the ledger lock (never hold it across a network call).
        peek = _load()["positions"].get(trade_id)
        if peek is None:
            return {"error": "No such open paper position."}
        exit_price = _quote([peek["ticker"]]).get(peek["ticker"]) or peek["current"]
    with _LEDGER_LOCK:
        acct = _load()
        pos = acct["positions"].get(trade_id)
        if pos is None:
            return {"error": "No such open paper position."}
        px = round(exit_price * (1 - SLIPPAGE_BPS / 10000), 2)  # sell/stop/target fills lower (slippage)
        acct["cash"] = round(acct["cash"] + pos["shares"] * px, 2)
        closed = None
        try:
            closed = journal.close_trade(trade_id, round(px, 2), exit_reason=reason, mae=pos.get("mae"), mfe=pos.get("mfe"))
        except (KeyError, ValueError):
            log.exception("journal close failed for %s", trade_id)
        del acct["positions"][trade_id]
        _save(acct)
    # Push a phone alert when the engine closed it (not for manual closes — you did those yourself).
    if reason in ("stop", "target", "trail", "time") and closed:
        r, pnl = closed.get("r_multiple"), closed.get("pnl")
        win = (pnl or 0) >= 0
        emoji = "white_check_mark" if win else "octagonal_sign"
        label = {"stop": "stopped out", "target": "hit target", "trail": "trailing stop", "time": "time-stop"}[reason]
        notify.send(
            f"{pos['ticker']} {label} — {('+' if (r or 0) >= 0 else '')}{r}R "
            f"({'+' if win else '−'}${abs(pnl or 0):,.0f})",
            title=f"Paper · {label}", tags=emoji,
            priority="high" if not win else "default",
        )
    return account()


def _trading_days_held(opened_at: str) -> int:
    """Weekday count from the open date to today (ET). Approximates the backtest's trading-day
    max-hold (ignores holidays — harmless, it just trims a hair late)."""
    try:
        d0 = dt.date.fromisoformat(opened_at[:10])
    except (ValueError, TypeError):
        return 0
    d1 = alert_engine._now_et().date()
    days, d = 0, d0
    while d < d1:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _mark_and_bracket(acct: dict, prices: dict[str, float]) -> bool:
    """Update marks + MAE/MFE, then run the exit policy. With trailing_stop on (default, the
    validated exit): ratchet a stop that rides init_stop_dist below the high-water mark, exit when
    price hits it, and time-stop after max_hold_days — NO fixed target (winners run). With it off:
    the legacy fixed stop+target. Returns True if anything changed."""
    try:
        from .config import load_settings
        s = load_settings()
        trailing, max_hold = s.trailing_stop, s.max_hold_days
    except Exception:
        trailing, max_hold = True, 10
    changed = False
    for tid, pos in list(acct["positions"].items()):
        px = prices.get(pos["ticker"])
        if not px:
            continue
        pos["current"] = round(px, 2)
        pos["mae"] = round(min(pos["mae"], px), 2)
        pos["mfe"] = round(max(pos["mfe"], px), 2)
        changed = True

        if trailing and pos.get("init_stop_dist"):
            # ratchet the trail up; never down. The trail distance tightens progressively as the
            # high-water mark extends in R (validated medium tighten), locking more of a stalled run.
            rps = pos["init_stop_dist"]
            r_reached = max(0.0, (pos["mfe"] - pos["entry"]) / rps) if rps else 0.0
            eff_dist = rps * (TRAIL_TIGHTEN_FLOOR_FRAC + (1 - TRAIL_TIGHTEN_FLOOR_FRAC) * math.exp(-TRAIL_TIGHTEN_K * r_reached))
            new_trail = round(pos["mfe"] - eff_dist, 2)
            if pos.get("trail_stop") is None or new_trail > pos["trail_stop"]:
                pos["trail_stop"] = new_trail
            if px <= pos["trail_stop"]:
                # "stop" if the trail never moved above the initial stop (a loss); else "trail" (locked gain)
                reason = "stop" if (pos.get("stop") and pos["trail_stop"] <= pos["stop"]) else "trail"
                _save(acct)
                close(tid, pos["trail_stop"], reason=reason)
                acct.update(_load())
                continue
            if _trading_days_held(pos["opened_at"]) >= max_hold:
                _save(acct)
                close(tid, px, reason="time")  # time-stop at the live price
                acct.update(_load())
            continue

        # legacy fixed bracket (trailing disabled)
        if pos["stop"] and px <= pos["stop"]:
            _save(acct)
            close(tid, pos["stop"], reason="stop")
            acct.update(_load())
        elif pos["target"] and px >= pos["target"]:
            _save(acct)
            close(tid, pos["target"], reason="target")
            acct.update(_load())
        elif _trading_days_held(pos["opened_at"]) >= max_hold:
            # time-stop applies on this path too — otherwise a position with no stop/target
            # (or trailing turned off) could sit open forever
            _save(acct)
            close(tid, px, reason="time")
            acct.update(_load())
    return changed


def _process_orders(acct: dict, prices: dict[str, float]) -> bool:
    """Fill resting MOO/limit orders whose condition is met. MOO fills at the next session's open
    (the first marked price once the market is open, after any configured buffer); limit fills when
    the price reaches the planned entry. Returns True if anything filled."""
    orders = acct.get("orders") or {}
    if not orders:
        return False
    open_now = _market_open()
    now_et = alert_engine._now_et()
    try:
        from .config import load_settings
        buffer_min = load_settings().open_buffer_minutes
    except Exception:
        buffer_min = 0
    fill_after = (dt.datetime.combine(now_et.date(), dt.time(9, 30)) + dt.timedelta(minutes=buffer_min)).time()

    changed = False
    for oid, o in list(orders.items()):
        px = prices.get(o["ticker"])
        if not px:
            continue
        fill_px = None
        if o["type"] == "limit":
            if open_now and o.get("limit_price") and px <= o["limit_price"]:
                fill_px = round(min(px, o["limit_price"]), 2)  # at the limit or better; no slippage
        elif o["type"] == "moo":
            new_session = o.get("placed_session") is None or _et_today() > o["placed_session"]
            if open_now and new_session and now_et.time() >= fill_after:
                fill_px = round(px * (1 + SLIPPAGE_BPS / 10000), 2)  # market-on-open pays up
        if fill_px is None:
            continue
        res = _open_position(acct, o["stock"], fill_px, o.get("variation_id"), o.get("variation_params"))
        if res.get("error"):
            log.warning("dropping paper order for %s: %s", o["ticker"], res["error"])
        del acct["orders"][oid]
        changed = True
    return changed


async def monitor_loop() -> None:
    """Background task: every few seconds, mark open positions, run the bracket, and fill any
    resting MOO/limit orders. Idle when there are no positions and no orders.

    Ordering matters: quotes are fetched FIRST (slow, network), and only then is the account
    loaded — fresh, under the ledger lock — for the mutate+save. Loading before the quote fetch
    would leave a seconds-wide window where a buy/close landing mid-tick gets clobbered by this
    tick's save of the stale snapshot (a real lost-update on the money file)."""
    while True:
        try:
            peek = _load()  # read-only peek just to learn which tickers need quotes
            tickers = list({p["ticker"] for p in peek["positions"].values()}
                           | {o["ticker"] for o in peek.get("orders", {}).values()})
            if tickers:
                prices = await asyncio.to_thread(_quote, tickers)
                with _LEDGER_LOCK:
                    acct = _load()  # reload fresh: the quote fetch may have taken seconds
                    changed = _mark_and_bracket(acct, prices)
                    changed = _process_orders(acct, prices) or changed
                    if changed:
                        _save(acct)
        except Exception:
            log.exception("paper monitor tick failed")
        await asyncio.sleep(TICK_SECONDS)


def maybe_post_interest() -> dict | None:
    """Post money-market interest on idle cash, once per calendar day (deduped by last_interest_date).
    Models a real brokerage cash sweep at ~CASH_APY so dry powder isn't a dead 0%. Credits every
    calendar day since the last posting (covers weekends + downtime). No-op if already posted today,
    on the first run (just anchors the date, no baseline period to credit), or with no cash. Safe to
    call every loop tick. Returns the account snapshot when it posts, else None."""
    with _LEDGER_LOCK:
        acct = _load()
        today = alert_engine._now_et().date()
        last = acct.get("last_interest_date")
        if last == today.isoformat():
            return None
        if not last:  # first ever call: anchor the date, nothing to credit yet
            acct["last_interest_date"] = today.isoformat()
            _save(acct)
            return None
        try:
            days = max(1, (today - dt.date.fromisoformat(last)).days)
        except (ValueError, TypeError):
            days = 1
        cash = acct.get("cash") or 0.0
        interest = round(cash * CASH_APY / 365 * days, 2)
        acct["last_interest_date"] = today.isoformat()
        if interest > 0:
            acct["cash"] = round(cash + interest, 2)
            acct["interest_earned"] = round((acct.get("interest_earned") or 0.0) + interest, 2)
            _save(acct)
            log.info("paper cash interest: +$%.2f (%dd @ %.1f%% on $%.2f)", interest, days, CASH_APY * 100, cash)
            return account()
        _save(acct)
    return None


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
                "trail_stop": p.get("trail_stop"),  # live ratcheting exit level (None on legacy positions)
                "current": cur,
                "unrealized": upnl,
                "unrealized_pct": round((cur / p["entry"] - 1) * 100, 2) if p["entry"] else 0.0,
                "r": round((cur - p["entry"]) / rps, 2) if rps else None,
            }
        )
        open_pnl += upnl
        invested += cur * p["shares"]
    positions.sort(key=lambda x: x["opened_at"])
    orders = [
        {"id": oid, **{k: o.get(k) for k in ("ticker", "name", "type", "limit_price", "shares", "stop", "target", "placed_at")}}
        for oid, o in acct.get("orders", {}).items()
    ]
    orders.sort(key=lambda x: x["placed_at"])
    equity = round(acct["cash"] + invested, 2)
    total_pnl = round(equity - acct["starting_cash"], 2) if acct["starting_cash"] else 0.0
    # Equity-scaled target slot count (single source of truth for the Monitor's "used/N slots").
    from .config import load_settings
    from . import risk
    slots = risk.target_slots(equity, load_settings())
    return {
        "starting_cash": acct["starting_cash"],
        "cash": acct["cash"],
        "equity": equity,
        "open_pnl": round(open_pnl, 2),
        "realized_pnl": round(total_pnl - open_pnl, 2),  # closed-trade P&L = total minus unrealized
        "total_pnl": total_pnl,
        "slots": slots,
        "cash_apy": CASH_APY,
        "interest_earned": round(acct.get("interest_earned") or 0.0, 2),
        "positions": positions,
        "orders": orders,
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
    """The router's regime label (bull / chop / bear) for the trade tag — the SAME classifier that
    drove the strategy choice, so outcomes slice consistently against the logic. Cached ~1h in
    regime.current_regime(). None if SPY can't be classified."""
    try:
        from . import regime
        r = regime.current_regime()
        return r["regime"] if r.get("available") else None
    except Exception:
        log.exception("market regime lookup failed")
        return None
