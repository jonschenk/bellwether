"""Portfolio risk read: a concise Claude pass over the OPEN book as a whole (not per trade).

Assesses the current positions for concentration, how much of the gains are LOCKED by trailing stops
above entry vs still at-risk, deployment / cash, and the regime context, then flags the one thing worth
watching. It is DECISION-SUPPORT, not order-giving: it surfaces exposure facts, it never says buy or sell
(the human decides, and the running app never trades on it). Cached to analysis/portfolio_insight.json;
regenerated once a day (post-open) when there are positions, or on demand from the Monitor.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

INSIGHT_PATH = Path(__file__).resolve().parents[1] / "analysis" / "portfolio_insight.json"
DEFAULT_MODEL = "claude-sonnet-4-6"  # frequent-ish observational read; Sonnet is plenty, ~1c/call

_PRICES = {  # $/M tokens (in, out)
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _model() -> str:
    return os.environ.get("PORTFOLIO_INSIGHT_MODEL", DEFAULT_MODEL).strip()


def latest() -> dict:
    """The cached insight, or {} if none yet."""
    if not INSIGHT_PATH.exists():
        return {}
    try:
        return json.loads(INSIGHT_PATH.read_text())
    except Exception:
        return {}


def _days_held(opened_at: str | None) -> int | None:
    if not opened_at:
        return None
    try:
        dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return None


def _build_prompt(acct: dict, regime: str | None) -> str:
    positions = acct.get("positions", [])
    equity = acct.get("equity") or 0
    lines, deployed, n_locked, n_green, biggest = [], 0.0, 0, 0, 0.0
    for p in positions:
        cur, entry = p.get("current") or 0, p.get("entry") or 0
        shares = p.get("shares") or 0
        active_stop = p.get("trail_stop") if p.get("trail_stop") is not None else p.get("stop")
        value = cur * shares
        pct = (value / equity * 100) if equity else 0
        deployed += value
        biggest = max(biggest, pct)
        locked = active_stop is not None and entry and active_stop > entry
        n_locked += 1 if locked else 0
        n_green += 1 if (p.get("unrealized") or 0) >= 0 else 0
        cushion = ((cur - active_stop) / cur * 100) if (cur and active_stop is not None) else None
        held = _days_held(p.get("opened_at"))
        lines.append(
            f"- {p.get('ticker')}: ${value:,.0f} ({pct:.0f}% of equity), entry {entry} -> {cur}, "
            f"{p.get('r')}R, P&L ${p.get('unrealized')}, active stop ${active_stop} "
            f"(price {cushion:.1f}% above stop)" if cushion is not None else
            f"- {p.get('ticker')}: ${value:,.0f} ({pct:.0f}% of equity), {p.get('r')}R"
        )
        if locked:
            lines[-1] += ", WIN LOCKED"
        if held is not None:
            lines[-1] += f", held {held}d"
    deploy_pct = (deployed / equity * 100) if equity else 0
    pos_txt = "\n".join(lines) or "(no open positions)"
    return f"""You are giving a concise PORTFOLIO RISK read of the CURRENT open book for a paper swing-trading
account (the validated regime router: bull -> leader-pullback, chop -> mean-reversion, bear -> cash; exits
are an ATR trailing stop + a time stop). This is DECISION-SUPPORT, not order-giving: surface exposure and
risk facts and what to watch. Do NOT tell the user to buy or sell anything; the human owns every decision.

Account: equity ${equity}, cash ${acct.get('cash')}, {len(positions)}/{acct.get('slots','?')} slots used,
{deploy_pct:.0f}% of equity deployed. Regime: {regime or 'unknown'}.
Open positions ({len(positions)}; {n_green} green, {n_locked} with a win locked, largest is {biggest:.0f}% of equity):
{pos_txt}

Write a SHORT risk read (~150 to 250 words) in markdown, at most a couple of "**bold**" sub-labels:
- Concentration: is any single name an outsized share of the book, or is it reasonably balanced for the
  account size? (A 4 to 6 name swing book is expected; flag only genuine lopsidedness.)
- Locked vs at-risk: how much of the current gains are secured (trailing stop above entry) vs still exposed
  to a pullback, and which names are still at-risk (stop still below entry).
- Deployment and cash: fully invested or dry powder, and whether that is reasonable in this regime (observe,
  do not prescribe).
- The ONE thing to watch: a name near its stop, an outsized position, a laggard, an old position, etc.

Be plain, honest, and sample-aware (this is a small early book). Natural sentence capitalization. Do NOT use
em dashes (use commas, colons, periods). No buy/sell calls, no strategy changes. Start directly, no title."""


async def generate() -> dict:
    """Build a fresh risk read over the open book and cache it. No-op (returns cached/empty) without an
    Anthropic key or with no open positions."""
    try:
        from . import paper, regime as regime_mod
        from .ai import _anthropic_key
        if not _anthropic_key():
            return latest()
        acct = paper.account()
        if not acct.get("positions"):
            return {"content": "", "generated_at": None, "positions": 0}

        reg = regime_mod.current_regime()
        regime_label = reg.get("regime") if reg.get("available") else None
        prompt = _build_prompt(acct, regime_label)

        import anthropic
        model = _model()
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=model, max_tokens=700, messages=[{"role": "user", "content": prompt}],
        )
        body = next((b.text for b in resp.content if b.type == "text"), "").strip()
        tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
        pin, pout = _PRICES.get(model, _PRICES["claude-sonnet-4-6"])
        cost = round(tin / 1e6 * pin + tout / 1e6 * pout, 4)

        data = {
            "content": body,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "positions": len(acct.get("positions", [])),
            "equity": acct.get("equity"),
            "model": model,
            "cost_usd": cost,
        }
        INSIGHT_PATH.parent.mkdir(parents=True, exist_ok=True)
        INSIGHT_PATH.write_text(json.dumps(data))
        log.info("portfolio insight written ($%s)", cost)
        return data
    except Exception:
        log.exception("portfolio_insight generation failed")
        return latest()


async def maybe_generate_daily() -> None:
    """Refresh the read once per ET trading day (post-open) when positions exist. Self-dedups by the
    cached generated_at date, so it's safe to call every loop tick."""
    try:
        from . import alert_engine, paper
        now_et = alert_engine._now_et()
        if now_et.weekday() >= 5 or now_et.hour < 10:  # weekday, after the open settles
            return
        if not paper.account().get("positions"):
            return
        cur = latest()
        ga = (cur.get("generated_at") or "")[:10]
        if ga == now_et.date().isoformat():
            return  # already refreshed today
        await generate()
    except Exception:
        log.exception("portfolio_insight daily refresh failed")
