"""Account-aware "trade case": a deeper, Claude-only read on a single setup.

This is the heart of the semi-automatic alert flow. Unlike the per-card scan
blurb in ai.py (which only summarizes one stock's news), a trade case weighs the
setup against the user's actual account — capital, risk settings, and current
open positions — and returns a structured case the user can approve or deny:
thesis, bull case, key risks, how it fits the existing portfolio, and a
Take / Wait / Pass call with a conviction level.

It always uses Claude: the local 3B model is too weak for this kind of
multi-factor reasoning. Needs ANTHROPIC_API_KEY in .env. Every call reports its
token usage and an estimated cost so spend stays visible.

This is decision support, not financial advice. It informs the user's decision;
the user places any trade themselves.
"""

import asyncio
import json
import logging
import os

from .ai import _anthropic_key, _fetch_news
from .config import ScanSettings

log = logging.getLogger(__name__)

# Default to the smartest model — this is real-money decision support and the
# cost delta over Sonnet is ~1c/call. Override with TRADE_CASE_MODEL in .env
# (e.g. claude-sonnet-4-6 to economize).
DEFAULT_TRADE_CASE_MODEL = "claude-opus-4-8"

# $ per million tokens (input, output) for the visible cost estimate.
_PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _model() -> str:
    return os.environ.get("TRADE_CASE_MODEL", DEFAULT_TRADE_CASE_MODEL).strip()

TRADE_CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "thesis": {
            "type": "string",
            "description": "2-4 sentences: the core reason this is (or isn't) a clean 2-5 day swing entry right now.",
        },
        "bull_case": {
            "type": "string",
            "description": "What has to go right for this to work, and why it plausibly could.",
        },
        "key_risks": {
            "type": "string",
            "description": "The main risks plus any near-term catalysts (earnings dates, events, macro exposure) the trader must know.",
        },
        "portfolio_fit": {
            "type": "string",
            "description": "How this trade fits the existing open positions: sector/factor overlap, concentration, correlation, and whether it adds or duplicates risk. If there are no positions, say so and comment on position size vs. account.",
        },
        "recommendation": {
            "type": "string",
            "enum": ["Take", "Wait", "Pass"],
            "description": "Take = clean entry now; Wait = good name but needs a better entry/trigger; Pass = skip it.",
        },
        "conviction": {
            "type": "string",
            "enum": ["High", "Medium", "Low"],
            "description": "Confidence in the recommendation, based on how well technicals, news, and portfolio fit align.",
        },
        "bottom_line": {
            "type": "string",
            "description": "One sentence the trader can read at a glance to decide approve or deny.",
        },
    },
    "required": [
        "thesis",
        "bull_case",
        "key_risks",
        "portfolio_fit",
        "recommendation",
        "conviction",
        "bottom_line",
    ],
    "additionalProperties": False,
}


def _positions_block(positions: list[dict] | None) -> str:
    if not positions:
        return "(no open positions — this would be a fresh entry)"
    lines = []
    for p in positions:
        bits = [f"{p.get('shares', '?')} sh {p['ticker']}"]
        if p.get("avg_price") is not None:
            bits.append(f"@ ${p['avg_price']}")
        if p.get("sector"):
            bits.append(f"[{p['sector']}]")
        if p.get("unrealized_pct") is not None:
            bits.append(f"({p['unrealized_pct']:+.1f}% open)")
        lines.append("- " + " ".join(bits))
    return "\n".join(lines)


def _build_prompt(stock: dict, settings: ScanSettings, positions: list[dict] | None, news: list[dict]) -> str:
    plan = stock.get("plan", {})
    news_block = (
        "\n".join(f"- {n['title']}" + (f" — {n['summary'][:240]}" if n["summary"] else "") for n in news)
        if news
        else "(no recent news found)"
    )

    return f"""You are a disciplined swing-trading analyst helping a trader decide whether to take a 2-5 day trade. This is decision support: lay out the case clearly and honestly so the trader can approve or deny it. Do not hype. If it's a weak setup or a poor portfolio fit, say so plainly.

ACCOUNT
- Trading capital: ${settings.capital:,.0f}
- Max risk per trade: {settings.risk_pct}% of capital
- Reward target: {settings.reward_mult}x risk (target capped at the 52-week high)

CANDIDATE: {stock['ticker']} @ ${stock['price']}
- Relative strength rank: {stock.get('rs_rating', '?')}/100 vs the scanned market
- {stock.get('pct_from_high', '?')}% below its 52-week high
- RSI(14) {stock['rsi']} (pulled back), ADX {stock['adx']} (trend strength), ATR% {stock['atr_pct']} (volatility)
- Relative volume {stock['rel_volume']}x its 21-day average
- Trend: price vs 50-SMA +{stock['pct_above_sma50']}%; 20/50/200 SMA = ${stock['sma20']}/${stock['sma50']}/${stock.get('sma200', '?')}

PROPOSED PLAN (pre-sized to the account)
- Buy {plan.get('shares', '?')} shares (~${plan.get('position_cost', '?'):,} ≈ {plan.get('position_pct', '?')}% of capital)
- Entry ${plan.get('entry', '?')}, stop ${plan.get('stop', '?')}, target ${plan.get('target', '?')} ({plan.get('reward_risk', '?')}:1 reward:risk)
- Risk if stopped out: ${plan.get('risk_dollars', '?')} ({plan.get('risk_pct', '?')}% of capital)

CURRENT OPEN POSITIONS
{_positions_block(positions)}

RECENT NEWS
{news_block}

Weigh the technical setup, the news, the proposed plan, AND how this trade fits the existing positions (sector/factor concentration, correlation, total risk on). Then give your structured case. Be specific to THIS account and THESE positions, not generic. Write in your own words; never copy headlines verbatim."""


async def trade_case(
    stock: dict,
    settings: ScanSettings,
    positions: list[dict] | None = None,
    news: list[dict] | None = None,
) -> dict:
    """Generate an account-aware trade case for one setup. Returns the structured
    case plus a `_meta` block with token usage and an estimated cost in USD."""
    if not _anthropic_key():
        return {
            "error": True,
            "bottom_line": "No ANTHROPIC_API_KEY set in .env — add one to use the trade-case generator.",
        }

    if news is None:
        news = await asyncio.to_thread(_fetch_news, stock["ticker"])

    import anthropic

    model = _model()
    client = anthropic.AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=1500,
            output_config={"format": {"type": "json_schema", "schema": TRADE_CASE_SCHEMA}},
            messages=[{"role": "user", "content": _build_prompt(stock, settings, positions, news)}],
        )
    except anthropic.APIError as e:
        log.error("Trade case failed for %s: %s", stock["ticker"], e)
        return {
            "error": True,
            "bottom_line": f"Trade-case analysis failed ({e.__class__.__name__}). Check your API key and credits.",
        }

    case = json.loads(next(b.text for b in resp.content if b.type == "text"))
    tin, tout = resp.usage.input_tokens, resp.usage.output_tokens
    price_in, price_out = _PRICES.get(model, _PRICES[DEFAULT_TRADE_CASE_MODEL])
    case["_meta"] = {
        "model": model,
        "input_tokens": tin,
        "output_tokens": tout,
        "cost_usd": round(tin / 1e6 * price_in + tout / 1e6 * price_out, 4),
        "news_count": len(news),
    }
    return case
