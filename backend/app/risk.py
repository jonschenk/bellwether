"""Position sizing and risk management for swing trades.

Uses the textbook approach: an ATR-based protective stop combined with the
1-2% rule (never risk more than a fixed % of capital on a single trade). This
turns a chart setup into concrete, account-sized instructions: how many shares,
where the stop goes, the profit target, and the exact dollars at risk.
"""

import math

from .config import ScanSettings

# --- Fractional-Kelly position sizing -------------------------------------------------
# Once a variation has a real track record, size each trade by its MEASURED edge instead
# of a fixed risk %. Quant consensus (and hard experience): never deploy full Kelly — it
# assumes you KNOW the true edge, and on sampling error it over-bets and blows up. Deploy a
# fraction (¼ here), and hard-cap it. This is a no-op until there's a trustworthy sample, so
# it can't do anything reckless early in the forward test — it only kicks in once the data
# earns it.
KELLY_FRACTION = 0.25       # deploy 1/4 of full Kelly — robust to estimation error
KELLY_MIN_SAMPLE = 20       # need this many CLOSED trades before sizing on a measured edge
KELLY_MAX_RISK_PCT = 2.0    # hard ceiling: dynamic sizing can never risk more than this per trade
KELLY_MIN_RISK_PCT = 0.5    # floor: below this a position is too small to bother with


def kelly_risk_pct(base_risk_pct: float, stats: dict | None) -> tuple[float, str]:
    """Fractional-Kelly per-trade risk %, derived from a variation's MEASURED edge.

    `stats` is a summary_by_variation() entry (winrate + avg_win_r + avg_loss_r). Returns
    (risk_pct, note). Falls back to `base_risk_pct` whenever the sample is too small or the
    edge isn't positive — so it stays a no-op until real data accumulates, then sizes on the
    evidence. Kelly fraction f* = p - q/b maps directly onto "fraction of capital to risk per
    trade" in an R-multiple system, where b = avg_win_R / |avg_loss_R| is the payoff ratio."""
    if not stats:
        return base_risk_pct, "base (no history)"
    n = stats.get("trades", 0)
    if n < KELLY_MIN_SAMPLE:
        return base_risk_pct, f"base ({n}/{KELLY_MIN_SAMPLE} trades — sample too small)"
    p = (stats.get("winrate") or 0) / 100.0
    avg_win = stats.get("avg_win_r")
    avg_loss = abs(stats.get("avg_loss_r") or 0)
    if not avg_win or not avg_loss or p <= 0:
        return base_risk_pct, "base (no positive edge yet)"
    b = avg_win / avg_loss                 # payoff ratio: R won per R lost
    f_star = p - (1 - p) / b                # full-Kelly fraction of capital to risk per trade
    if f_star <= 0:
        return base_risk_pct, f"base (Kelly f*={f_star:.3f} <= 0 — edge not positive)"
    risk = KELLY_FRACTION * f_star * 100    # ¼-Kelly, as a percent of capital
    risk = max(KELLY_MIN_RISK_PCT, min(risk, KELLY_MAX_RISK_PCT))
    return round(risk, 2), f"¼-Kelly p={p:.0%} b={b:.2f} f*={f_star:.3f}"


def position_plan(
    price: float, atr_value: float, settings: ScanSettings,
    high_52w: float | None = None, target_override: float | None = None,
) -> dict | None:
    """Build a sized trade plan for one stock, or None if it can't be sized.

    `target_override` sets the profit target directly (used by mean-reversion, where the exit
    is the snap-back to the 5-SMA, not a reward multiple). Otherwise the target is the reward
    multiple off the stop, capped at the 52-week high."""
    stop_distance = settings.atr_stop_mult * atr_value
    if stop_distance <= 0 or price <= 0:
        return None

    stop_price = price - stop_distance
    if target_override is not None and target_override > price:
        # Mean-reversion: the target IS the reversion level (the 5-SMA above an oversold dip).
        target_price = target_override
    else:
        # Projected target from the reward multiple, but capped at the 52-week high:
        # for a 2-5 day swing the prior high is the natural overhead resistance, and a
        # raw ATR projection can otherwise land above any level the stock has reached.
        # If the stock is already at/above its highs there's no overhead, so no cap.
        target_price = price + settings.reward_mult * stop_distance
        if settings.cap_target_at_high and high_52w and high_52w > price:
            target_price = min(target_price, high_52w)

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
        # Actual reward:risk after the target cap, not the input multiple — so the
        # card shows the honest ratio you're deciding on (often below reward_mult).
        "reward_risk": round((target_price - price) / stop_distance, 1),
        # True when a proper stop would risk more than your risk budget on even
        # one share — the trade is too volatile for this account size.
        "undersized": undersized,
    }
