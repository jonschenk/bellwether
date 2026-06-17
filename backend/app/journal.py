"""Trade journal — the feedback signal for strategy iteration.

Every trade is logged with the strategy variation that produced it, the plan at
entry, and (on close) the outcome. Analytics roll up per variation so you can see
which one actually wins — with an explicit small-sample flag, because a winrate
over a handful of trades is noise, not signal. Trades you *pass* on can be logged
too (status "passed"), so the advisor's Take/Wait/Pass calls can be graded, not
just the trades you took.

Outcome math is R-multiples: risk-per-share = entry - stop, and
R = (exit - entry) / risk-per-share. Expectancy = average R across closed trades
— the single number that says whether a variation makes money over many trades.
"""

import datetime as dt
import json
import logging
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parents[1] / "journal.json"
STRATEGY_DOC = Path(__file__).resolve().parents[1] / "STRATEGY.md"

# Below this many closed trades, a variation's winrate isn't trustworthy yet.
MIN_SAMPLE = 20


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _load() -> list[dict]:
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.exception("journal.json unreadable; starting fresh")
    return []


def _save(trades: list[dict]) -> None:
    STORE_PATH.write_text(json.dumps(trades, indent=2))


def log_trade(ticker: str, variation_id: str, plan: dict, decision: str | None = None, notes: str = "") -> dict:
    """Record an opened trade, tagged with the variation that produced it."""
    trades = _load()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "ticker": ticker,
        "variation_id": variation_id,
        "decision": decision,  # the trade-case call at entry, if any: Take/Wait/Pass
        "status": "open",
        "opened_at": _now(),
        "entry": plan.get("entry"),
        "stop": plan.get("stop"),
        "target": plan.get("target"),
        "shares": plan.get("shares"),
        "risk_dollars": plan.get("risk_dollars"),
        "closed_at": None,
        "exit": None,
        "pnl": None,
        "r_multiple": None,
        "outcome": None,
        "notes": notes,
    }
    trades.append(entry)
    _save(trades)
    return entry


def log_pass(ticker: str, variation_id: str, decision: str = "Pass", notes: str = "") -> dict:
    """Record a setup you passed on, so the advisor's calls can be graded later."""
    trades = _load()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "ticker": ticker,
        "variation_id": variation_id,
        "decision": decision,
        "status": "passed",
        "opened_at": _now(),
        "notes": notes,
    }
    trades.append(entry)
    _save(trades)
    return entry


def close_trade(trade_id: str, exit_price: float, notes: str = "") -> dict:
    """Close an open trade and compute P&L, R-multiple, and win/loss/scratch."""
    trades = _load()
    t = next((x for x in trades if x["id"] == trade_id), None)
    if t is None:
        raise KeyError(f"No such trade: {trade_id}")
    if t["status"] != "open":
        raise ValueError(f"Trade {trade_id} is not open (status: {t['status']})")

    entry, stop, shares = t["entry"], t["stop"], t["shares"] or 0
    risk_per_share = (entry - stop) if (entry is not None and stop is not None) else None
    t["status"] = "closed"
    t["closed_at"] = _now()
    t["exit"] = exit_price
    t["pnl"] = round((exit_price - entry) * shares, 2) if entry is not None else None
    t["r_multiple"] = round((exit_price - entry) / risk_per_share, 2) if risk_per_share else None
    if t["pnl"] is None:
        t["outcome"] = None
    elif t["pnl"] > 0:
        t["outcome"] = "win"
    elif t["pnl"] < 0:
        t["outcome"] = "loss"
    else:
        t["outcome"] = "scratch"
    if notes:
        t["notes"] = (t.get("notes", "") + " | " + notes).strip(" |")
    _save(trades)
    return t


def list_trades() -> list[dict]:
    return _load()


def summary_by_variation() -> dict[str, dict]:
    """Per-variation performance over CLOSED trades. The scoreboard for deciding
    which variation to keep evolving."""
    out: dict[str, dict] = {}
    for t in _load():
        if t["status"] != "closed" or t.get("r_multiple") is None:
            continue
        s = out.setdefault(
            t["variation_id"],
            {"trades": 0, "wins": 0, "total_r": 0.0, "total_pnl": 0.0},
        )
        s["trades"] += 1
        s["wins"] += 1 if t["outcome"] == "win" else 0
        s["total_r"] += t["r_multiple"]
        s["total_pnl"] += t.get("pnl") or 0.0

    for vid, s in out.items():
        n = s["trades"]
        s["winrate"] = round(s["wins"] / n * 100, 1) if n else 0.0
        s["expectancy_r"] = round(s["total_r"] / n, 2) if n else 0.0
        s["total_pnl"] = round(s["total_pnl"], 2)
        s["low_sample"] = n < MIN_SAMPLE
    return out


def render_strategy_md(variations: dict[str, dict], active_id: str | None) -> str:
    """Build the human + Claude-readable STRATEGY.md: the variation scoreboard
    plus an iteration-notes section. This is the file the advisor appends to and
    Claude Code reads to propose the next variation."""
    perf = summary_by_variation()
    lines = [
        "# Strategy",
        "",
        "Variations of the swing scan and how each has performed. The active "
        "variation drives current scans; every trade is tagged with the variation "
        "that produced it. Iterate by deriving a new variation, running it, and "
        "comparing the scoreboard over a meaningful sample "
        f"(>= {MIN_SAMPLE} closed trades before trusting a winrate).",
        "",
        "## Scoreboard",
        "",
        "| Variation | Name | Trades | Win% | Expectancy (R) | Net P&L | Active |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for vid, v in sorted(variations.items(), key=lambda kv: int(kv[0][1:])):
        p = perf.get(vid, {})
        n = p.get("trades", 0)
        win = f"{p['winrate']}%" if n else "—"
        exp = f"{p['expectancy_r']:+}" if n else "—"
        pnl = f"${p['total_pnl']:,}" if n else "—"
        flag = " ⚠ low sample" if p.get("low_sample") and n else ""
        active = "★" if vid == active_id else ""
        lines.append(f"| {vid} | {v['name']} | {n}{flag} | {win} | {exp} | {pnl} | {active} |")

    lines += ["", "## Variations", ""]
    for vid, v in sorted(variations.items(), key=lambda kv: int(kv[0][1:])):
        parent = f" (from {v['parent']})" if v.get("parent") else ""
        lines.append(f"### {vid} — {v['name']}{parent}")
        lines.append(f"_created {v['created_at']}_")
        if v.get("notes"):
            lines.append(f"\n{v['notes']}")
        params = ", ".join(f"{k}={val}" for k, val in v["params"].items())
        lines.append(f"\n`{params}`\n")

    lines += [
        "## Iteration notes",
        "",
        "<!-- The advisor appends observations and proposed tweaks here. Claude "
        "Code reads this section in a dev session and turns the good ones into the "
        "next variation via strategy.derive(). Nothing here changes the strategy "
        "automatically — every version bump is a deliberate, reviewed commit. -->",
        "",
    ]
    return "\n".join(lines)


def write_strategy_md(variations: dict[str, dict], active_id: str | None) -> Path:
    STRATEGY_DOC.write_text(render_strategy_md(variations, active_id))
    return STRATEGY_DOC
