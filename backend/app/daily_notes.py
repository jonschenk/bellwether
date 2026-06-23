"""Daily trading-journal notes — a small Claude pass that observes each trading day.

Once per ET trading day (after the close), reads the day's trades + open positions + the equity/regime
context and writes a short, OBSERVATIONAL markdown note to analysis/daily/YYYY-MM-DD.md. It is
deliberately NON-PRESCRIPTIVE: it summarizes and flags things to watch, but it never proposes strategy
changes — the sample is far too small in the forward-proving phase, and that's the (later) weekly
review's job. The prompt also tells the model to stay sample-aware so it doesn't over-narrate noise.

These daily observations can't be backfilled (a point-in-time read of the day), which is why they start
from day one. They become the raw material the weekly review later reasons over.
"""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DAILY_DIR = Path(__file__).resolve().parents[1] / "analysis" / "daily"
DEFAULT_MODEL = "claude-sonnet-4-6"  # observational summary; Sonnet is plenty, ~cents/day

_PRICES = {  # $/M tokens (in, out) — for the cost line in the note
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-8": (15.0, 75.0),
}


def _model() -> str:
    return os.environ.get("DAILY_NOTES_MODEL", DEFAULT_MODEL).strip()


def list_notes(limit: int = 60) -> list[dict]:
    """Recent daily notes, newest first: [{date, content}]."""
    if not DAILY_DIR.is_dir():
        return []
    files = sorted(DAILY_DIR.glob("*.md"), reverse=True)[:limit]
    return [{"date": f.stem, "content": f.read_text()} for f in files]


def _today_trades(today: str) -> tuple[list, list]:
    """(opened today, closed today) from the journal."""
    from . import journal
    trades = journal.list_trades()
    opened = [t for t in trades if (t.get("opened_at") or "").startswith(today)]
    closed = [t for t in trades if (t.get("closed_at") or "").startswith(today)]
    return opened, closed


def _slot_note(acct: dict) -> str:
    """Factual one-liner on equity-scaled position capacity + distance to the next slot (or ceiling).
    Informational only — the note stays non-prescriptive; this just states what the rule currently allows."""
    try:
        from .config import load_settings
        s = load_settings()
        slots, equity = acct.get("slots"), acct.get("equity") or 0
        if slots is None:
            return ""
        if slots >= s.max_concurrent_positions:
            return f"{slots} slots — at the ceiling ({s.max_concurrent_positions}); capacity won't grow with equity from here."
        next_at = (slots + 1 - s.position_slot_base) * s.capital_per_slot
        return f"{slots} slots at this equity; the next unlocks at ${next_at:,.0f} (${next_at - equity:,.0f} away)."
    except Exception:
        return ""


def _build_prompt(today: str, regime: str | None, acct: dict, equity_tail: list,
                  opened: list, closed: list) -> str:
    def trade_line(t):
        return (f"- {t.get('ticker')} [{t.get('strategy','?')}] entry {t.get('entry')} "
                f"stop {t.get('stop')} target {t.get('target')} score {(t.get('entry_snapshot') or {}).get('setup_score')}")
    def close_line(t):
        return (f"- {t.get('ticker')} {t.get('exit_reason')} → "
                f"{('+' if (t.get('r_multiple') or 0) >= 0 else '')}{t.get('r_multiple')}R "
                f"({t.get('outcome')}), held {t.get('hold_days')}d")
    def pos_line(p):
        return (f"- {p.get('ticker')} entry {p.get('entry')} now {p.get('current')} "
                f"{('+' if (p.get('unrealized') or 0) >= 0 else '')}{p.get('unrealized')} "
                f"({p.get('r')}R)")

    positions = acct.get("positions", [])
    eq = "; ".join(f"{r['date']}: ${r.get('equity')}" for r in equity_tail[-6:]) or "n/a"
    return f"""You are keeping a concise DAILY journal for an autonomous, PAPER-TRADED swing-trading system in its
FORWARD-PROVING phase (building a track record; no real money; no strategy changes yet). The system uses a
validated regime router: bull → leader-pullback, chop → mean-reversion, bear → cash.

Date: {today}. Regime today: {regime or 'unknown'}.
Account: equity ${acct.get('equity')} (started ${acct.get('starting_cash')}), cash ${acct.get('cash')}, \
open P&L {acct.get('open_pnl')}, realized {acct.get('realized_pnl')}.
Position capacity: {_slot_note(acct) or 'n/a'}
Equity (recent days): {eq}

OPENED TODAY ({len(opened)}):
{chr(10).join(trade_line(t) for t in opened) or '(none)'}

CLOSED TODAY ({len(closed)}):
{chr(10).join(close_line(t) for t in closed) or '(none)'}

STILL OPEN ({len(positions)}):
{chr(10).join(pos_line(p) for p in positions) or '(none)'}

Write a SHORT daily note in markdown (~120–220 words):
- Factually summarize the day: what opened, what closed and why, how the open positions are behaving.
- Note anything observationally interesting — a hard stop, an extended/gapping name, the regime context,
  which strategy leg ran, an outlier R.
- You may flag patterns to WATCH, but you MUST NOT make strategy recommendations or change orders: the
  sample here is tiny and statistically meaningless this early, so any such call would be noise. Stay
  sample-aware. If little happened, say so briefly. Be honest and plain — no hype, no false confidence.
- Position capacity scales with equity by a FIXED rule (shown above). You MAY note it as a plain FACT when
  it just changed or is close to a threshold (e.g. "equity crossed $2k, so capacity is now 5 slots"). Do
  NOT frame it as advice, a target, or something to act on — it's automatic; you're only observing it.

Do NOT add a title or top header line (a date header is added for you). Start directly with the note.
Use short paragraphs and at most a couple of "**bold**" sub-labels or "-" bullets — keep it scannable."""


async def maybe_generate_eod() -> None:
    """Generate today's note once, after the close, if there's anything to observe. Self-dedups by
    the date-stamped file, so it's safe to call every loop tick. No-op without an Anthropic key."""
    try:
        from . import alert_engine, paper, regime, equity_log
        from .ai import _anthropic_key
        now_et = alert_engine._now_et()
        if now_et.weekday() >= 5 or now_et.hour < 16:
            return  # weekday, post-close only
        today = now_et.date().isoformat()
        note_path = DAILY_DIR / f"{today}.md"
        if note_path.exists() or not _anthropic_key():
            return

        acct = paper.account()
        opened, closed = _today_trades(today)
        if not opened and not closed and not acct.get("positions"):
            return  # nothing to observe — skip the call on a flat day

        reg = regime.current_regime()
        regime_label = reg.get("regime") if reg.get("available") else None
        prompt = _build_prompt(today, regime_label, acct, equity_log.rows(), opened, closed)

        import anthropic
        model = _model()
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=model, max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        body = next((b.text for b in resp.content if b.type == "text"), "").strip()
        tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
        pin, pout = _PRICES.get(model, _PRICES["claude-sonnet-4-6"])
        cost = round(tin / 1e6 * pin + tout / 1e6 * pout, 4)

        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        header = (f"# Daily note — {today}\n\n"
                  f"_regime {regime_label or '—'} · equity ${acct.get('equity')} · "
                  f"{acct.get('slots', '?')} slots · "
                  f"{len(opened)} opened · {len(closed)} closed · {model} ${cost}_\n\n")
        note_path.write_text(header + body + "\n")
        log.info("daily note written for %s ($%s)", today, cost)
    except Exception:
        log.exception("daily_notes generation failed")
