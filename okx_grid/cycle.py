"""Single-consumer, supplied-data paper cycle. No poller, HTTP or Telegram sender."""
import hashlib

from .automation import _paper_500_only, consider_paper_entry
from .control import notices, pin, queue_notice, receipt, record, snapshot
from .domain import TradingError, identifier, paper_only, symbol, timestamp


def run_cycle(trader, *, cycle_id, now, proposals, quotes, allowed_symbols, notification_healthy=False):
    paper_only()
    _paper_500_only(trader)
    identifier(cycle_id)
    timestamp(now)
    if type(notification_healthy) is not bool:
        raise TradingError("INVALID_NOTIFICATION_HEALTH")
    if not isinstance(allowed_symbols, (list, tuple)) or not 1 <= len(allowed_symbols) <= 100:
        raise TradingError("SYMBOL_ALLOWLIST_REQUIRED")
    allowed = sorted({symbol(item) for item in allowed_symbols})
    if not isinstance(proposals, list) or len(proposals) > 500 or not isinstance(quotes, dict) or len(quotes) > 3:
        raise TradingError("INVALID_CYCLE_INPUT")
    data, digest = snapshot({"proposals": proposals, "quotes": quotes, "healthy": notification_healthy})
    with trader._transaction():
        trader._audit_all()
        pin(trader, "cycle_symbol_policy", allowed)
        previous = receipt(trader, "paper_cycle:" + cycle_id, "paper_cycle")
        if previous:
            if previous["digest"] != digest:
                raise TradingError("PAPER_CYCLE_CONFLICT")
            return {**previous["result"], "duplicate": True}
        last = trader._meta("cycle_last_at")
        if last is not None and now <= last:
            raise TradingError("OUT_OF_ORDER_CYCLE")
        state = trader.status()
        active = {b["bot_id"]: b for b in state["bots"] if b["status"] == "OPEN"}
        if set(data["quotes"]) - set(active):
            raise TradingError("UNKNOWN_CYCLE_BOT")
        problems = []
        # Validate every quote before any fill. A bad snapshot pauses buys, but
        # valid quotes still drive existing sell/stop exits in this transaction.
        for key, bot in active.items():
            quote = data["quotes"].get(key)
            try:
                if not isinstance(quote, dict) or set(quote) != {"price", "observed_at"}:
                    raise TradingError("MISSING_CYCLE_QUOTE")
                from .domain import positive
                positive(quote["price"])
                at = timestamp(quote["observed_at"])
                if now - at > trader.policy.max_market_age or at - now > trader.policy.max_future_seconds or at <= bot["last_at"]:
                    raise TradingError("INVALID_CYCLE_QUOTE_TIME")
            except TradingError as error:
                problems.append({"bot_id": key, "reason": error.code})
        if problems or not notification_healthy or any(n["status"] == "UNKNOWN" for n in notices(trader)):
            trader.pause()
        invalid = {p["bot_id"] for p in problems}
        tick_id = "cycle_" + hashlib.sha256(cycle_id.encode()).hexdigest()[:28]
        for key in active:
            if key not in invalid:
                quote = data["quotes"][key]
                trader.tick(key, tick_id, quote["price"], quote["observed_at"], now)
        filtered = [p for p in data["proposals"] if isinstance(p, dict) and isinstance(p.get("market"), dict)
                    and p["market"].get("instrument") in allowed]
        decision = consider_paper_entry(trader, filtered, cycle_id=cycle_id, now=now) if filtered else {
            "mode": "paper", "decision": "NO_ELIGIBLE_CANDIDATE", "reason": "NO_ALLOWLISTED_PROPOSALS"}
        result = {"mode": "paper", "duplicate": False, "entry": decision, "quote_errors": problems, "state": trader.status()}
        # Detailed source-backed summary; no raw proposal or arbitrary text echoed.
        summary = {"cycle": cycle_id, "entry": decision, "state": result["state"], "quote_errors": problems}
        import json
        message = "PAPER 500 USDT | " + json.dumps(summary, sort_keys=True, separators=(",", ":"))
        # A bounded notification always includes full ledger summary; long filter
        # lists remain in the private cycle receipt instead of truncating JSON.
        if len(message) > 3500:
            message = "PAPER 500 USDT | " + json.dumps({"cycle": cycle_id, "decision": decision["decision"],
                "portfolio": result["state"]["portfolio"], "halted": result["state"]["halted"], "paused": result["state"]["paused"]}, sort_keys=True)
        queue_notice(trader, tick_id, message, now)
        record(trader, "paper_cycle:" + cycle_id, "paper_cycle", now, {"digest": digest, "result": result})
        trader._set_meta("cycle_last_at", now)
        return result
