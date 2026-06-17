"""Backtester (phase 1) — strict no-lookahead replay of the leader-pullback scan.

The R&D workbench: run a strategy variation over years of daily history and measure
its edge (win rate + R-multiple expectancy). Claude Code runs it in dev sessions; we
review the numbers and decide the next variation. Results here are a HYPOTHESIS to
validate forward in paper, never proof (survivorship bias, idealized fills — see below).

No-lookahead discipline (the thing that makes results valid):
  * every indicator at day T uses only bars <= T (they're backward-looking by construction)
  * cross-sectional RS rank at day T uses every name's momentum as-of T (past prices only)
  * a signal on day T's close is ENTERED at day T+1's OPEN — never T's close
  * the exit walk only ever looks at bars on/after the entry

Phase-1 scope: curated universe, R-multiple stats (capital-independent). Equity curve,
drawdown, profit-factor slicing, CSV/STRATEGY.md output, variation comparison, and the
train/test split come in later phases. Honest limits: daily bars only; the universe is
TODAY's listings (survivorship-biased -> results overstated); fills are idealized (stop/
target fill exactly at the level, no slippage/gaps).
"""

import argparse
import datetime as dt
import logging
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

from .config import ScanSettings
from .indicators import adx, atr, rsi, sma
from .risk import position_plan
from .universe import load_universe

log = logging.getLogger(__name__)

MIN_BARS = 220          # need ~1y of history before a name is eligible (matches the live scan)
DEFAULT_MAX_HOLD = 10   # time-stop: close at this many trading days if neither stop nor target hits
# High default capital so the affordability/price ceiling doesn't distort a strategy backtest.
# R-multiple stats are capital-independent anyway; capital only affects the max_price filter.
DEFAULT_CAPITAL = 1_000_000


# ----------------------------------------------------------------- data

def _download(tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """Daily OHLCV per ticker over [start, end]. Split/div-adjusted (auto_adjust)."""
    raw = yf.download(
        tickers, start=start, end=end, interval="1d",
        auto_adjust=True, group_by="ticker", progress=False, threads=True,
    )
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
            if len(df) >= MIN_BARS:
                out[t] = df
        except (KeyError, Exception):
            continue
    return out


# ----------------------------------------------------------------- as-of indicator frames

def _indicator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Per-ticker frame of every series the filter needs, indexed by date. Each value
    at row T uses only bars <= T, so reading row T is inherently as-of correct."""
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    sma200 = sma(close, 200)
    f = pd.DataFrame(
        {
            "open": df["Open"], "high": high, "low": low, "close": close,
            "sma20": sma(close, 20), "sma50": sma(close, 50), "sma200": sma200,
            "sma200_prior": sma200.shift(22),               # ~1 month ago (200-SMA slope)
            "avgvol": vol.rolling(21).mean(),
            "rsi": rsi(close, 14), "atr": atr(high, low, close, 14), "adx": adx(high, low, close, 14),
            "high52": high.rolling(252, min_periods=200).max(),
            "low52": low.rolling(252, min_periods=200).min(),
            # blended multi-timeframe momentum (the RS-rank input), as a series
            "mom": 0.2 * (close / close.shift(21) - 1)
            + 0.5 * (close / close.shift(63) - 1)
            + 0.3 * (close / close.shift(126) - 1),
        }
    )
    return f


def _rs_table(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Cross-sectional RS rating (0-100) per date: each name's momentum percentile-ranked
    against the whole universe AS OF that day. dates x tickers."""
    mom = pd.DataFrame({t: f["mom"] for t, f in frames.items()})
    return mom.rank(axis=1, pct=True) * 100


# ----------------------------------------------------------------- signals + simulation

@dataclass
class Trade:
    ticker: str
    signal_date: str
    entry_date: str
    entry: float
    stop: float
    target: float
    exit_date: str
    exit: float
    exit_reason: str   # target | stop | time
    r_multiple: float
    hold_days: int
    outcome: str       # win | loss | scratch


def _signal_mask(ind: pd.DataFrame, rs: pd.Series, s: ScanSettings) -> pd.Series:
    """Boolean series: does this name pass the full leader-pullback filter on each day?
    Same conditions as the live scanner.evaluate_ticker, vectorized. NaN warmup rows
    compare False, so they're excluded automatically."""
    atr_pct = ind["atr"] / ind["close"] * 100
    pct_from_high = (ind["high52"] - ind["close"]) / ind["high52"] * 100
    pct_above_low = (ind["close"] / ind["low52"] - 1) * 100
    return (
        (ind["close"] > s.min_price) & (ind["close"] <= s.max_price) & (ind["avgvol"] > s.min_avg_volume)
        & (ind["close"] > ind["sma50"]) & (ind["close"] > ind["sma200"])
        & (ind["sma20"] > ind["sma50"]) & (ind["sma50"] > ind["sma200"])
        & (ind["sma200"] > ind["sma200_prior"])
        & (rs >= s.min_rs_rating)
        & (pct_from_high <= s.near_high_pct) & (pct_above_low >= s.min_above_low_pct)
        & (ind["adx"] >= s.adx_min) & (atr_pct >= s.atr_pct_min)
        & (ind["rsi"] >= s.rsi_floor) & (ind["rsi"] < s.rsi_threshold)
    )


def _simulate(ind: pd.DataFrame, loc: int, s: ScanSettings, max_hold: int) -> Trade | None:
    """Enter at the NEXT bar's open after the signal at position `loc`; walk forward up to
    max_hold bars applying the bracket (stop/target) then a time-stop. Stop is checked before
    target on a same-day touch of both (conservative)."""
    if loc + 1 >= len(ind):
        return None  # no next bar to enter on
    entry = float(ind["open"].iloc[loc + 1])
    atrv = float(ind["atr"].iloc[loc])           # ATR as of the signal day
    high52 = float(ind["high52"].iloc[loc])
    plan = position_plan(entry, atrv, s, high52)
    if plan is None:
        return None
    stop, target = plan["stop"], plan["target"]

    exit_price = exit_reason = exit_loc = None
    last = min(loc + max_hold, len(ind) - 1)
    for j in range(loc + 1, last + 1):
        lo, hi = float(ind["low"].iloc[j]), float(ind["high"].iloc[j])
        if lo <= stop:
            exit_price, exit_reason, exit_loc = stop, "stop", j
            break
        if hi >= target:
            exit_price, exit_reason, exit_loc = target, "target", j
            break
    if exit_price is None:  # time-stop at the close of the last bar in the window
        exit_price, exit_reason, exit_loc = float(ind["close"].iloc[last]), "time", last

    rps = entry - stop
    r = (exit_price - entry) / rps if rps > 0 else 0.0
    return Trade(
        ticker="",  # filled by caller
        signal_date=str(ind.index[loc].date()),
        entry_date=str(ind.index[loc + 1].date()),
        entry=round(entry, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        exit_date=str(ind.index[exit_loc].date()),
        exit=round(exit_price, 2),
        exit_reason=exit_reason,
        r_multiple=round(r, 2),
        hold_days=exit_loc - (loc + 1),
        outcome="win" if r > 0 else ("loss" if r < 0 else "scratch"),
    )


def _trades_for(ticker: str, ind: pd.DataFrame, rs: pd.Series, s: ScanSettings, max_hold: int) -> list[Trade]:
    """All non-overlapping trades for one ticker: take each signal, but don't re-enter the
    same name while a position in it is still open."""
    sig = _signal_mask(ind, rs.reindex(ind.index), s)
    trades: list[Trade] = []
    in_until_loc = -1
    locs = [ind.index.get_loc(d) for d in sig.index[sig.fillna(False)]]
    for loc in locs:
        if loc < MIN_BARS or loc <= in_until_loc:
            continue
        t = _simulate(ind, loc, s, max_hold)
        if t is None:
            continue
        t.ticker = ticker
        trades.append(t)
        in_until_loc = ind.index.get_loc(pd.Timestamp(t.exit_date))
    return trades


# ----------------------------------------------------------------- stats

def _stats(trades: list[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in trades if t.r_multiple > 0]
    losses = [t for t in trades if t.r_multiple < 0]
    gross_win = sum(t.r_multiple for t in wins)
    gross_loss = -sum(t.r_multiple for t in losses)
    total_r = sum(t.r_multiple for t in trades)
    reasons = {r: sum(1 for t in trades if t.exit_reason == r) for r in ("target", "stop", "time")}
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "expectancy_r": round(total_r / n, 3),
        "total_r": round(total_r, 1),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "avg_win_r": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss_r": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "avg_hold_days": round(sum(t.hold_days for t in trades) / n, 1),
        "exits": reasons,
    }


# ----------------------------------------------------------------- orchestration

def run_backtest(
    settings: ScanSettings,
    start: str,
    end: str,
    universe: str = "curated",
    tickers: list[str] | None = None,
    max_hold: int = DEFAULT_MAX_HOLD,
) -> dict:
    names = tickers or load_universe(universe)
    log.info("Downloading %d names %s -> %s …", len(names), start, end)
    frames_raw = _download(names, start, end)
    if not frames_raw:
        return {"error": "No data downloaded."}

    frames = {t: _indicator_frame(df) for t, df in frames_raw.items()}
    rs = _rs_table(frames)

    all_trades: list[Trade] = []
    for t, ind in frames.items():
        all_trades.extend(_trades_for(t, ind, rs[t], settings, max_hold))
    all_trades.sort(key=lambda x: x.entry_date)

    return {
        "names_with_data": len(frames),
        "start": start, "end": end, "max_hold": max_hold,
        "stats": _stats(all_trades),
        "trades": all_trades,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Backtest a strategy variation (phase 1).")
    p.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="end", default=dt.date.today().isoformat(), help="YYYY-MM-DD")
    p.add_argument("--universe", default="curated", choices=["curated", "full"])
    p.add_argument("--tickers", default=None, help="comma list to override the universe (quick tests)")
    p.add_argument("--variation", default=None, help="strategy variation id (default: active, else baseline)")
    p.add_argument("--max-hold", type=int, default=DEFAULT_MAX_HOLD)
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    args = p.parse_args()

    # Build settings from the chosen variation's params (or defaults), with a high capital so
    # the affordability ceiling doesn't filter names during a strategy backtest.
    params: dict = {}
    try:
        from . import strategy
        v = strategy.list_variations().get(args.variation) if args.variation else strategy.get_active()
        if v:
            params = v["params"]
            print(f"Variation {v['id']} ({v['name']})")
    except Exception:
        pass
    settings = ScanSettings(capital=args.capital, **params)

    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    res = run_backtest(settings, args.start, args.end, args.universe, tickers, args.max_hold)
    if res.get("error"):
        print(res["error"]); return

    s = res["stats"]
    print(f"\n=== Backtest {res['start']} -> {res['end']} | {res['names_with_data']} names | max-hold {res['max_hold']}d ===")
    if not s.get("trades"):
        print("No trades generated."); return
    print(f"Trades:        {s['trades']}")
    print(f"Win rate:      {s['win_rate']}%")
    print(f"Expectancy:    {s['expectancy_r']:+}R per trade")
    print(f"Profit factor: {s['profit_factor']}")
    print(f"Total:         {s['total_r']:+}R")
    print(f"Avg win/loss:  {s['avg_win_r']:+}R / {s['avg_loss_r']:+}R   avg hold {s['avg_hold_days']}d")
    print(f"Exits:         target {s['exits']['target']} | stop {s['exits']['stop']} | time {s['exits']['time']}")
    print("\n(Hypothesis only — survivorship-biased universe, idealized fills. Validate forward in paper.)")


if __name__ == "__main__":
    main()
