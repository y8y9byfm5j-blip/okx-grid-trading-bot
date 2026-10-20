"""Offline, fail-closed automatic PAPER grid entry decisions.

No Telegram transport, network adapter, broker order client, timer or service
side effects are present. The caller must supply market and trend snapshots;
this is NOT proof of their exchange provenance.
"""
from decimal import Decimal
import hashlib

from .domain import Market, TradingError, build_plan, identifier, paper_only, timestamp
from .radar import screen


def _bot_id(cycle_id):
    # One durable identity per decision cycle, independent of selected instrument.
    return "auto_" + hashlib.sha256(cycle_id.encode("ascii")).hexdigest()[:28]


def _paper_500_only(trader):
    identity = getattr(trader, "config_identity", None)
    if (not isinstance(identity, dict)
            or identity.get("name") != "v5-paper-500"
            or trader.policy.equity != Decimal("500")):
        raise TradingError("AUTO_REQUIRES_EXPLICIT_500_PROFILE")


def consider_paper_entry(trader, proposals, *, cycle_id, now):
    """Evaluate supplied proposals and open at most ONE paper bot per cycle.

    Idempotence is pinned to cycle_id across process restarts. A replay with
    a different candidate is rejected rather than silently opening another.
    No market collection or unattended scheduler is implemented here.
    """
    paper_only()
    _paper_500_only(trader)
    identifier(cycle_id)
    timestamp(now)
    if not isinstance(proposals, list) or not proposals:
        raise TradingError("AUTO_PROPOSALS_REQUIRED")

    candidates = screen(proposals, trader.policy, now)
    snapshot = trader.status()
    bot_id = _bot_id(cycle_id)
    existing = next((b for b in snapshot["bots"] if b["bot_id"] == bot_id), None)
    if existing:
        if candidates["accepted"]:
            selected = candidates["accepted"][0]
            if (existing["instrument"] != selected["instrument"]
                    or existing["plan_digest"] != selected["plan_digest"]):
                raise TradingError("AUTO_CYCLE_CONFLICT")
        return {"mode": "paper", "decision": "DUPLICATE_CYCLE", "bot_id": bot_id,
                "portfolio": snapshot["portfolio"]}

    active = {b["instrument"] for b in snapshot["bots"] if b["status"] == "OPEN"}
    for candidate in candidates["accepted"]:
        instrument = candidate["instrument"]
        if instrument in active:
            continue
        original = next((p for p in proposals
                         if isinstance(p, dict)
                         and isinstance(p.get("market"), dict)
                         and p["market"].get("instrument") == instrument), None)
        if original is None:
            raise TradingError("AUTO_INPUT_MISMATCH")
        plan = build_plan(original["plan"])
        if plan.digest != candidate["plan_digest"]:
            raise TradingError("AUTO_PLAN_CHANGED")
        market = Market.from_dict(original["market"])
        try:
            result = trader.open(bot_id, plan, market, now, unique_instrument=True)
        except TradingError as error:
            if error.code == "DUPLICATE_BOT":
                # Race: never try a second order or a different candidate.
                return {"mode": "paper", "decision": "DUPLICATE_CYCLE", "bot_id": bot_id,
                        "portfolio": trader.status()["portfolio"]}
            if error.code == "INSTRUMENT_ALREADY_ACTIVE":
                return {"mode": "paper", "decision": "NO_ELIGIBLE_CANDIDATE",
                        "reason": error.code, "portfolio": trader.status()["portfolio"]}
            if error.code in {"MAX_BOTS", "TOTAL_RISK_LIMIT", "BOT_RISK_LIMIT",
                              "ALLOCATION_LIMIT", "PAPER_HALTED"}:
                return {"mode": "paper", "decision": "BLOCKED", "reason": error.code,
                        "portfolio": trader.status()["portfolio"]}
            raise
        return {"mode": "paper", "decision": "OPENED", "bot_id": bot_id,
                "instrument": instrument, "plan": candidate["plan"],
                "portfolio": result["portfolio"]}

    return {"mode": "paper", "decision": "NO_ELIGIBLE_CANDIDATE",
            "rejections": candidates["rejected"],
            "portfolio": snapshot["portfolio"]}


def advance_paper_tick(trader, bot_id, tick_id, price, *, observed_at, now):
    """Advance one existing PAPER position with a supplied, validated tick.

    The persisted trader records fills, fees, TP/SL and halted state atomically.
    This returns a Telegram-ready *data object*, not a sent message.
    """
    paper_only()
    _paper_500_only(trader)
    identifier(bot_id)
    identifier(tick_id)
    outcome = trader.tick(bot_id, tick_id, price, observed_at, now)
    bot = outcome["bots"][0]
    return {"mode": "paper", "event": "REPLAY" if outcome["duplicate"] else
            "CLOSED" if bot["status"] == "CLOSED" else "UPDATED",
            "duplicate": outcome["duplicate"], "bot_id": bot_id,
            "instrument": bot["instrument"], "position_status": bot["status"],
            "close_reason": bot["reason"], "last_at": bot["last_at"],
            "cash": bot["cash"], "quantity": bot["quantity"], "fees": bot["fees"],
            "realized_pnl": bot["realized_pnl"],
            "unrealized_pnl": bot["unrealized_pnl"],
            "portfolio": outcome["portfolio"], "halted": outcome["halted"]}
