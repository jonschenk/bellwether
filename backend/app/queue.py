"""Approve/deny review queue — phase 4 of the semi-auto vision.

The app PROPOSES fully-specified trade tickets (symbol / shares / entry / stop / target, tagged
with the regime and strategy they came from) and surfaces them for review. The human pulls the
trigger: Approve -> a paper buy opens the position (the existing safe plumbing); Deny -> the pass
is logged with the advisor's call so those calls can be graded later. This is the seed of the
alert engine (which will later auto-fill this queue) and the approve-before-execute workflow.

HARD BOUNDARY (unchanged): the app sets trades up and makes them executable; nothing is opened
until the human clicks Approve. No auto-approve. Paper only for now — going live is a broker swap
(phase 5), and the human click stays load-bearing.

Proposals persist to proposals.json (gitignored) so the queue survives a backend restart.
"""

import datetime as dt
import json
import logging
import uuid
from pathlib import Path

from . import journal, paper, strategy

log = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parents[1] / "proposals.json"
DEFAULT_TOP_N = 8


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _load() -> list[dict]:
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log.exception("proposals.json unreadable; starting fresh")
    return []


def _save(proposals: list[dict]) -> None:
    STORE_PATH.write_text(json.dumps(proposals, indent=2))


def _pending(proposals: list[dict]) -> list[dict]:
    return [p for p in proposals if p["status"] == "pending"]


def _summarize(p: dict) -> dict:
    """The compact ticket the UI renders (without the heavy snapshotted stock row)."""
    out = {k: p.get(k) for k in (
        "id", "ticker", "name", "strategy", "regime", "score", "call", "reason",
        "conviction", "bull", "bear", "plan", "status", "created_at", "decided_at",
        "trading_day", "exec_note",
    )}
    # carry the earnings flag from the snapshotted row so the ticket can warn
    out["days_to_earnings"] = (p.get("stock") or {}).get("days_to_earnings")
    out["earnings_soon"] = (p.get("stock") or {}).get("earnings_soon", False)
    return out


def view() -> dict:
    """Pending tickets (newest scan first) + the recently decided ones, for the panel."""
    proposals = _load()
    pending = [_summarize(p) for p in proposals if p["status"] == "pending"]
    decided = [_summarize(p) for p in proposals if p["status"] != "pending"]
    decided.sort(key=lambda p: p.get("decided_at") or "", reverse=True)
    return {"pending": pending, "decided": decided[:20]}


def build(results: list[dict], regime: str | None, scan_strategy: str,
          top_n: int = DEFAULT_TOP_N, exclude: set[str] | None = None,
          trading_day: str | None = None) -> dict:
    """Populate the queue from the current scan's best setups. Recommended picks (if a
    'Recommend top picks' pass was run) lead, by rank; otherwise the top setup scores. Tickers
    already pending in the queue or already held as a paper position are skipped (no dupes);
    `exclude` skips more (the alert engine passes today's already-alerted names). `trading_day`
    tags each ticket with the ET session it's intended for (the nightly model proposes tonight
    for tomorrow's open). Returns the view plus `added` and the tickers added."""
    proposals = _load()
    already = {p["ticker"] for p in proposals if p["status"] == "pending"} | (exclude or set())
    held = {pos["ticker"] for pos in paper.account().get("positions", [])}

    # Rank: recommended rows first (by their rank), then by setup score.
    ranked = sorted(
        results,
        key=lambda r: (r.get("recommendation") is None, r.get("recommendation", {}).get("rank", 1e9), -r.get("setup_score", 0)),
    )

    added_tickers: list[str] = []
    for r in ranked:
        if len(added_tickers) >= top_n:
            break
        ticker = r["ticker"]
        if ticker in already or ticker in held:
            continue
        if not (r.get("plan") or {}).get("shares"):
            continue
        rec = r.get("recommendation") or {}
        proposals.append({
            "id": uuid.uuid4().hex[:8],
            "ticker": ticker,
            "name": r.get("name", ""),
            "strategy": r.get("strategy", scan_strategy),
            "regime": regime,
            "score": r.get("setup_score"),
            "call": rec.get("call"),                # Claude's Take/Watch call, if recommended
            "reason": rec.get("reason"),            # Claude's verdict one-liner, if recommended
            "conviction": rec.get("conviction"),
            "bull": rec.get("bull"),                # the bull/bear debate, if recommended
            "bear": rec.get("bear"),
            "plan": r.get("plan"),
            "stock": r,                             # full snapshot so execution can paper-buy / journal it
            "status": "pending",
            "trading_day": trading_day,             # the ET session this ticket is for (nightly model)
            "exec_note": None,                      # filled by the morning re-check (executed/skipped reason)
            "created_at": _now(),
            "decided_at": None,
        })
        already.add(ticker)
        added_tickers.append(ticker)

    _save(proposals)
    return {**view(), "added": len(added_tickers), "added_tickers": added_tickers}


# ---- nightly-model lifecycle: approve = INTENT (no buy); the morning execute pulls the trigger ----

def decide(proposal_id: str, decision: str, reason: str = "") -> dict:
    """Record the human's overnight call WITHOUT executing (nightly model). 'approve' marks the
    ticket approved (the morning re-check will then re-validate and execute it at the open);
    'deny' logs the pass (with the advisor's call) for grading. Distinct from approve() below,
    which is the intraday review path that buys immediately on the click."""
    proposals = _load()
    p = next((x for x in proposals if x["id"] == proposal_id and x["status"] == "pending"), None)
    if p is None:
        return {"error": "No such pending proposal."}
    if decision == "approve":
        p["status"] = "approved"
    elif decision == "deny":
        try:
            vid = (strategy.get_active() or {}).get("id", "v1")
            journal.log_pass(p["stock"], vid, decision=p.get("call") or "Pass", notes=reason or "denied at nightly review")
        except Exception:
            log.exception("logging the passed trade failed for %s", p["ticker"])
        p["status"] = "denied"
    else:
        return {"error": f"Unknown decision '{decision}'."}
    p["decided_at"] = _now()
    _save(proposals)
    return view()


def approved_for_execution(trading_day: str) -> list[dict]:
    """Approved tickets whose trading_day is on or before `trading_day` — the morning execute's
    work list (the full snapshot, not the summary, so it can paper-buy)."""
    return [p for p in _load()
            if p["status"] == "approved" and (p.get("trading_day") or trading_day) <= trading_day]


def mark(proposal_id: str, status: str, exec_note: str | None = None) -> None:
    """Stamp a ticket's terminal status from the morning execute (executed | skipped | expired)."""
    proposals = _load()
    p = next((x for x in proposals if x["id"] == proposal_id), None)
    if p is None:
        return
    p["status"] = status
    if exec_note is not None:
        p["exec_note"] = exec_note
    p["decided_at"] = p.get("decided_at") or _now()
    _save(proposals)


def expire_pending(on_or_before_day: str) -> list[str]:
    """Mark any still-pending tickets for `on_or_before_day` or earlier as expired (the human
    never reviewed them by the open — the safe default is to NOT trade). Returns the tickers."""
    proposals = _load()
    expired = []
    for p in proposals:
        if p["status"] == "pending" and (p.get("trading_day") or "") and p["trading_day"] <= on_or_before_day:
            p["status"] = "expired"
            p["exec_note"] = "not reviewed before the open"
            p["decided_at"] = _now()
            expired.append(p["ticker"])
    if expired:
        _save(proposals)
    return expired


def approve(proposal_id: str) -> dict:
    """The human pulls the trigger: open a paper position from the proposal's ticket. The buy
    fills at the live price and logs to the journal; the proposal is marked approved."""
    proposals = _load()
    p = next((x for x in proposals if x["id"] == proposal_id and x["status"] == "pending"), None)
    if p is None:
        return {"error": "No such pending proposal."}
    from .config import load_settings
    acct = paper.submit(p["stock"], load_settings())  # honours the order-type setting (market/moo/limit)
    if acct.get("error"):
        return {"error": acct["error"]}  # leave it pending so the user can retry
    p["status"] = "approved"
    p["decided_at"] = _now()
    _save(proposals)
    return {**view(), "account": acct}


def deny(proposal_id: str, reason: str = "") -> dict:
    """Pass on a proposal: log it (with the advisor's call) so the call can be graded later."""
    proposals = _load()
    p = next((x for x in proposals if x["id"] == proposal_id and x["status"] == "pending"), None)
    if p is None:
        return {"error": "No such pending proposal."}
    try:
        vid = (strategy.get_active() or {}).get("id", "v1")
        journal.log_pass(p["stock"], vid, decision=p.get("call") or "Pass", notes=reason)
    except Exception:
        log.exception("logging the passed trade failed for %s", p["ticker"])
    p["status"] = "denied"
    p["decided_at"] = _now()
    _save(proposals)
    return view()


def clear() -> dict:
    """Drop every still-pending proposal (decided ones stay as history)."""
    proposals = [p for p in _load() if p["status"] != "pending"]
    _save(proposals)
    return view()
