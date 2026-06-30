"""Weekly review, the level above the daily notes: a once-a-week Claude rollup of how the week went,
plus a DEV NOTE for Claude Code (the human-gated 'two Claudes' hand-off).

Fires on Saturday, once per ISO week, and reads the week's daily notes + closed trades + the running
scoreboard + the equity arc (vs SPY) + the regime mix. Writes a sample-aware markdown review to
analysis/weekly/YYYY-Www.md and pushes a notification. It always targets the most-recently-COMPLETED
Mon-Fri week, so a manual/forced run on any day reviews real data (not a partial current week).

DISCIPLINE (the whole point): the dev-note section DEFERS strategy change-orders while the sample is
too small to mean anything (the forward-proving phase). It only writes a real, implementable change-
order spec once there's a genuine track record; otherwise it says 'no change warranted yet' and names
the milestone that would unlock one. The running app never self-rewrites its strategy: this PROPOSES;
a human hands the spec to Claude Code, who DISPOSES with a deliberate, git-tracked commit.
"""

import logging
import os
from datetime import timedelta
from pathlib import Path

log = logging.getLogger(__name__)

WEEKLY_DIR = Path(__file__).resolve().parents[1] / "analysis" / "weekly"
DEFAULT_MODEL = "claude-opus-4-8"  # the highest-consequence note (drives strategy change-orders); weekly, so ~cents
LOW_SAMPLE_TRADES = 20             # below this many CLOSED trades, defer all change-orders (don't overfit noise)

_PRICES = {  # $/M tokens (in, out)
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _model() -> str:
    return os.environ.get("WEEKLY_REVIEW_MODEL", DEFAULT_MODEL).strip()


def list_notes(limit: int = 26) -> list[dict]:
    """Recent weekly reviews, newest first: [{week, content}]."""
    if not WEEKLY_DIR.is_dir():
        return []
    files = sorted(WEEKLY_DIR.glob("*.md"), reverse=True)[:limit]
    return [{"week": f.stem, "content": f.read_text()} for f in files]


def _build_prompt(week_label, eq_rows, closed_week, scoreboard, daily_notes_week, n_closed_total) -> str:
    def cl(t):
        r = t.get("r_multiple")
        return (f"- {t.get('ticker')} {t.get('outcome')} {('+' if (r or 0) >= 0 else '')}{r}R "
                f"({t.get('exit_reason')}, held {t.get('hold_days')}d)")
    closed_txt = "\n".join(cl(t) for t in closed_week) or "(none closed this week)"
    sb = "\n".join(
        f"- {v}: {s.get('trades')} trades, win {s.get('winrate')}%, expectancy {s.get('expectancy_r')}R, "
        f"P&L ${s.get('total_pnl')}{' [LOW SAMPLE]' if s.get('low_sample') else ''}"
        for v, s in (scoreboard or {}).items()) or "(no closed trades yet)"
    if eq_rows:
        e0, e1 = eq_rows[0], eq_rows[-1]
        eq_line = (f"equity ${e0.get('equity')} to ${e1.get('equity')} ({e0.get('date')} .. {e1.get('date')}); "
                   f"SPY {e0.get('spy')} to {e1.get('spy')}")
        regs: dict = {}
        for r in eq_rows:
            regs[r.get("regime")] = regs.get(r.get("regime"), 0) + 1
        reg_line = ", ".join(f"{k}: {v}d" for k, v in regs.items())
    else:
        eq_line = reg_line = "n/a"
    notes_txt = "\n\n---\n\n".join(f"[{n['date']}]\n{n['content']}" for n in daily_notes_week) \
        or "(no daily notes this week)"
    enough = n_closed_total >= LOW_SAMPLE_TRADES

    data_clause = (
        "there is now enough of a sample to start drawing TENTATIVE conclusions, but still flag "
        "low-confidence items as such"
        if enough else
        "the sample is far too small to conclude anything about the strategy; say so plainly and "
        "resist reading signal into noise")
    dev_clause = (
        "IF AND ONLY IF the evidence genuinely supports a change, write ONE precise, implementable "
        "change-order spec: the exact change, the file or knob it touches, the evidence and the expected "
        "effect, and how to validate it (backtest out-of-sample first, then paper). Keep it conservative "
        "and singular, not a grab-bag."
        if enough else
        f"State clearly that NO change is warranted yet, and why (insufficient closed-trade sample: "
        f"{n_closed_total} of ~{LOW_SAMPLE_TRADES}). Name the milestone that would unlock a real review "
        f"(around {LOW_SAMPLE_TRADES} closed trades, and ideally a chop or bear regime sample, since the "
        f"leader-pullback leg has so far only run in a bull tape). Do NOT fabricate a change-order from "
        f"noise. It is correct and valuable to say 'hold course, keep collecting data'.")

    return f"""You are writing the WEEKLY REVIEW for an autonomous, PAPER-TRADED swing-trading system in its
FORWARD-PROVING phase (building a track record, no real money). The system runs a validated regime router:
bull -> leader-pullback, chop -> mean-reversion, bear -> cash. Exits are an ATR trailing stop plus a time stop.

This review has TWO audiences: (1) the user, for a quick read on the week; (2) Claude Code, a developer in a
separate session who may implement strategy changes. You PROPOSE; a human decides and hands any spec to Claude
Code, who implements it as a deliberate, git-tracked commit. The running app never changes its own strategy.

Week: {week_label}.
Equity arc: {eq_line}
Regime days this week: {reg_line}
Total CLOSED trades to date (whole run): {n_closed_total}  (a meaningful sample starts around {LOW_SAMPLE_TRADES})

CLOSED THIS WEEK:
{closed_txt}

RUNNING SCOREBOARD (per strategy variation):
{sb}

THIS WEEK'S DAILY NOTES (the raw material):
{notes_txt}

Write the weekly review in markdown with exactly these three sections (use "## " headers):

## The week
A short, honest summary: the equity arc and how it compares to SPY, the regime, what opened and closed and
why, how the open book looks, and the standout behavior (a trailing stop that locked a win, a morning re-check
that vetoed a gap-down, etc.). Plain and factual, no hype.

## What the data says
What, if anything, the numbers support YET. Be ruthlessly sample-aware: with {n_closed_total} closed trades,
{data_clause}.

## For Claude Code (dev note)
{dev_clause}

Rules: natural sentence capitalization. NO em dashes (use commas, colons, or periods). No title or top header
line (one is added for you). Be concise, roughly 250 to 450 words total. Honesty over confidence."""


async def maybe_generate_weekly(force: bool = False) -> dict:
    """Generate the review for the most-recently-completed Mon-Fri week. Scheduled: fires only on
    Saturday and self-dedups by the week-stamped file (safe to call every loop tick). force=True
    (the manual endpoint) ignores the day gate and regenerates. Returns {ok, week, ...}."""
    try:
        from . import alert_engine, journal, equity_log, daily_notes, notify
        from .ai import _anthropic_key
        now_et = alert_engine._now_et()
        if not force and now_et.weekday() != 5:   # scheduled: Saturday only
            return {"ok": False, "reason": "not saturday"}
        if not _anthropic_key():
            return {"ok": False, "reason": "no api key"}

        # the most-recently-COMPLETED trading week (Mon..Fri ending on the last Friday on/before today)
        friday = now_et.date() - timedelta(days=(now_et.weekday() - 4) % 7)
        monday = friday - timedelta(days=4)
        iso = friday.isocalendar()
        week_label = f"{iso[0]}-W{iso[1]:02d}"
        note_path = WEEKLY_DIR / f"{week_label}.md"
        if note_path.exists() and not force:
            return {"ok": False, "reason": "already written", "week": week_label}

        week_days = {(monday + timedelta(days=i)).isoformat() for i in range(5)}
        notes = [n for n in daily_notes.list_notes() if n["date"] in week_days]
        trades = journal.list_trades()
        closed_week = [t for t in trades if (t.get("closed_at") or "")[:10] in week_days]
        n_closed_total = sum(1 for t in trades if t.get("status") == "closed")
        try:
            scoreboard = journal.summary_by_variation()
        except Exception:
            scoreboard = {}
        eq_rows = [r for r in equity_log.rows() if r.get("date") in week_days]
        if not notes and not closed_week and not eq_rows:
            return {"ok": False, "reason": "no activity this week", "week": week_label}

        prompt = _build_prompt(week_label, eq_rows, closed_week, scoreboard, notes, n_closed_total)

        import anthropic
        model = _model()
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=model, max_tokens=1800, thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        )
        body = next((b.text for b in resp.content if b.type == "text"), "").strip()
        tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
        pin, pout = _PRICES.get(model, _PRICES["claude-opus-4-8"])
        cost = round(tin / 1e6 * pin + tout / 1e6 * pout, 4)

        eq_end = eq_rows[-1].get("equity") if eq_rows else "n/a"
        WEEKLY_DIR.mkdir(parents=True, exist_ok=True)
        header = (f"# Weekly review, {week_label}\n\n"
                  f"_{monday.isoformat()} to {friday.isoformat()} · equity ${eq_end} · "
                  f"{len(closed_week)} closed this week · {n_closed_total} closed total · {model} ${cost}_\n\n")
        note_path.write_text(header + body + "\n")
        notify.send(f"Weekly review ready for {week_label}. Open the Monitor to read it.",
                    title="Bellwether weekly", tags="bar_chart")
        log.info("weekly review written for %s ($%s)", week_label, cost)
        return {"ok": True, "week": week_label, "cost_usd": cost}
    except Exception:
        log.exception("weekly_review generation failed")
        return {"ok": False, "reason": "error"}
