"""Atomic offline PAPER entry, reviewed from SOL PR #3; no scheduler/network."""
from decimal import Decimal
import hashlib

from .control import receipt, record, snapshot
from .domain import Market, TradingError, build_plan, identifier, paper_only, timestamp
from .radar import screen


def _bot_id(cycle_id):
    return "auto_" + hashlib.sha256(cycle_id.encode("ascii")).hexdigest()[:28]


def _paper_500_only(trader):
    identity, policy = getattr(trader, "config_identity", None), trader.policy
    if (not isinstance(identity, dict) or identity.get("name") != "v5-paper-500" or policy.equity != Decimal("500")
            or policy.risk_fraction > Decimal("0.01") or policy.total_risk_fraction > Decimal("0.03")
            or policy.max_bots > 3 or policy.max_bot_capital > 500 or policy.max_allocation_fraction > 1
            or policy.max_realized_loss_fraction > Decimal("0.02")
            or policy.min_quote_volume < 10000000 or policy.max_spread_bps > 10
            or policy.min_depth_multiple < 5 or policy.max_market_age > 30 or policy.max_future_seconds > 2):
        raise TradingError("AUTO_REQUIRES_EXPLICIT_500_PROFILE")


def consider_paper_entry(trader, proposals, *, cycle_id, now):
    """At most one entry; all outcomes and the whole input fingerprint are durable."""
    paper_only()
    _paper_500_only(trader)
    identifier(cycle_id)
    timestamp(now)
    if not isinstance(proposals, list) or not 1 <= len(proposals) <= 500:
        raise TradingError("AUTO_PROPOSALS_REQUIRED")
    proposals, digest = snapshot(proposals)
    with trader._transaction():
        trader._audit_all()
        previous = receipt(trader, "auto_cycle:" + cycle_id, "auto_cycle")
        if previous:
            if previous.get("digest") != digest:
                raise TradingError("AUTO_CYCLE_CONFLICT")
            return {**previous["result"], "decision": "DUPLICATE_CYCLE", "original_decision": previous["result"]["decision"]}
        result = _decide(trader, proposals, cycle_id, now)
        record(trader, "auto_cycle:" + cycle_id, "auto_cycle", now, {"digest": digest, "result": result})
        return result


def _decide(trader, proposals, cycle_id, now):
    state = trader.status()
    bot_id = _bot_id(cycle_id)
    if any(b["bot_id"] == bot_id for b in state["bots"]):
        raise TradingError("AUTO_LEGACY_CYCLE_UNVERIFIED")
    if state["halted"] or state["paused"]:
        return {"mode": "paper", "decision": "BLOCKED", "reason": "PAPER_HALTED" if state["halted"] else "PAPER_PAUSED",
                "portfolio": state["portfolio"]}
    candidates = screen(proposals, trader.policy, now)
    active = {b["instrument"] for b in state["bots"] if b["status"] == "OPEN"}
    for candidate in candidates["accepted"]:
        if candidate["instrument"] in active:
            continue
        original = next(p for p in proposals if isinstance(p, dict) and isinstance(p.get("market"), dict)
                        and p["market"].get("instrument") == candidate["instrument"])
        plan, market = build_plan(original["plan"]), Market.from_dict(original["market"])
        if plan.digest != candidate["plan_digest"]:
            raise TradingError("AUTO_PLAN_CHANGED")
        try:
            opened = trader.open(bot_id, plan, market, now)
        except TradingError as error:
            if error.code in {"MAX_BOTS", "TOTAL_RISK_LIMIT", "BOT_RISK_LIMIT", "ALLOCATION_LIMIT", "PAPER_HALTED", "PAPER_PAUSED"}:
                return {"mode": "paper", "decision": "BLOCKED", "reason": error.code, "portfolio": trader.status()["portfolio"]}
            raise
        return {"mode": "paper", "decision": "OPENED", "bot_id": bot_id, "instrument": candidate["instrument"],
                "plan": candidate["plan"], "portfolio": opened["portfolio"]}
    return {"mode": "paper", "decision": "NO_ELIGIBLE_CANDIDATE", "rejections": candidates["rejected"], "portfolio": state["portfolio"]}
