"""Offline reference command boundary, NOT the unverified V5 Telegram adapter."""
from .commands import parse_command

from .control import pin, receipt, record, snapshot
from .domain import TradingError, paper_only, timestamp


def handle_update(trader, update, *, allowed_chat, allowed_user, now):
    paper_only()
    timestamp(now)
    if type(allowed_chat) is not int or type(allowed_user) is not int or allowed_chat <= 0 or allowed_user <= 0:
        raise TradingError("TELEGRAM_ACL_REQUIRED")
    update, digest = snapshot(update)
    if not isinstance(update, dict) or set(update) != {"update_id", "message"}:
        raise TradingError("TELEGRAM_UPDATE_REJECTED")
    uid, message = update["update_id"], update["message"]
    if type(uid) is not int or uid < 0 or not isinstance(message, dict):
        raise TradingError("TELEGRAM_UPDATE_REJECTED")
    chat, sender = message.get("chat", {}), message.get("from", {})
    if (not isinstance(chat, dict) or not isinstance(sender, dict)
            or type(chat.get("id")) is not int or type(sender.get("id")) is not int
            or chat.get("id") != allowed_chat or sender.get("id") != allowed_user
            or chat.get("type") != "private" or sender.get("is_bot") is not False
            or any(k in message for k in ("forward_origin", "forward_from", "sender_chat", "via_bot", "edit_date"))):
        raise TradingError("TELEGRAM_UNAUTHORIZED")
    text = parse_command(message.get("text")).text
    at = timestamp(message.get("date"))
    with trader._transaction():
        trader._audit_all()
        pin(trader, "telegram_acl", {"chat": allowed_chat, "user": allowed_user})
        old = receipt(trader, "telegram:" + str(uid), "telegram_update")
        if old:
            if old["digest"] != digest:
                raise TradingError("TELEGRAM_UPDATE_CONFLICT")
            return {**old["result"], "duplicate": True}
        if now - at > 30 or at - now > 2:
            raise TradingError("TELEGRAM_STALE_UPDATE")
        cursor = trader._meta("telegram_cursor")
        if cursor is not None and uid <= cursor:
            raise TradingError("TELEGRAM_OLD_UPDATE")
        last = trader._meta("telegram_last_at")
        if last is not None and now <= last and text not in {"/emergency_stop", "/paper halt", "/paper pause", "/papergrid stop"}:
            raise TradingError("TELEGRAM_RATE_LIMIT")
        if text in {"/paper pause", "/papergrid stop"}:
            trader.pause()
        elif text in {"/paper halt", "/emergency_stop"}:
            trader.halt()
        state = trader.status()
        result = {"mode": "paper", "duplicate": False, "command": text, "state": state,
                  "source_status": "V5_NOT_CONNECTED", "delivery": "NOT_SENT"}
        record(trader, "telegram:" + str(uid), "telegram_update", now, {"digest": digest, "result": result})
        trader._set_meta("telegram_cursor", uid)
        trader._set_meta("telegram_last_at", now)
        return result
