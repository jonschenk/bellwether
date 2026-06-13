"""Position sizing and risk management for swing trades.

Uses the textbook approach: an ATR-based protective stop combined with the
1-2% rule (never risk more than a fixed % of capital on a single trade). This
turns a chart setup into concrete, account-sized instructions: how many shares,
where the stop goes, the profit target, and the exact dollars at risk.
"""

import math

from .config import ScanSettings


def position_plan(price: float, atr_value: float, settings: ScanSettings) -> dict | None:
    """Build a sized trade plan for one stock, or None if it can't be sized."""
    stop_distance = settings.atr_stop_mult * atr_value
    if stop_distance <= 0 or price <= 0:
        return None

    stop_price = price - stop_distance
    target_price = price + settings.reward_mult * stop_distance

    risk_budget = settings.capital * settings.risk_pct / 100  # $ you're willing to lose
    shares_by_risk = math.floor(risk_budget / stop_distance)
    shares_affordable = math.floor(settings.capital / price)

    # The risk rule is the real position size, but never more than you can afford.
    shares = min(shares_by_risk, shares_affordable)

    # If the risk rule says 0 (stop is wider than your whole risk budget) but you
    # can still afford a share, fall back to 1 and flag it as oversized risk.
    undersized = shares == 0 and shares_affordable >= 1
    sized = shares if shares > 0 else (1 if undersized else 0)
    if sized == 0:
        return None

    position_cost = sized * price
    dollars_at_risk = sized * stop_distance

    return {
        "shares": sized,
        "shares_by_risk": shares_by_risk,
        "shares_affordable": shares_affordable,
        "entry": round(price, 2),
        "stop": round(stop_price, 2),
        "target": round(target_price, 2),
        "stop_distance": round(stop_distance, 2),
        "position_cost": round(position_cost, 2),
        "position_pct": round(position_cost / settings.capital * 100, 1),
        "risk_dollars": round(dollars_at_risk, 2),
        "risk_pct": round(dollars_at_risk / settings.capital * 100, 1),
        "reward_risk": settings.reward_mult,
        # True when a proper stop would risk more than your risk budget on even
        # one share — the trade is too volatile for this account size.
        "undersized": undersized,
    }
