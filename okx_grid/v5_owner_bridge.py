"""Staging bridge for the observed V5 getUpdates -> handle(text) -> send loop.

The existing owner passes the full update. This module never polls, sends,
imports production V5, or reads its offset/state files. Its checkpoint and
outbox live only in the separate V7 ledger. Deployment remains unverified.
"""
from .control import mark_notice, notices, pin, queue_notice, receipt, record, snapshot
from .domain import TradingError, paper_only, timestamp
from .telegram_control import authorize_update
from .telegram_v7 import handle_v7_update
from .v7_core import V7Core


class V5OwnerBridge:
    def __init__(self, core, *, allowed_chat, allowed_user, initial_offset):
        paper_only()
        if not isinstance(core, V7Core):
            raise TradingError("V7_CORE_REQUIRED")
        if (type(allowed_chat) is not int or type(allowed_user) is not int or
                allowed_chat <= 0 or allowed_user <= 0):
            raise TradingError("TELEGRAM_ACL_REQUIRED")
        if type(initial_offset) is not int or initial_offset < 0:
            raise TradingError("TELEGRAM_OFFSET_REQUIRED")
        self.core, self.trader = core, core.trader
        self.allowed_chat, self.allowed_user = allowed_chat, allowed_user
        with self.trader._transaction():
            self.trader._audit_all()
            pin(self.trader, "v5_bridge_policy", {"revision": 1, "chat": allowed_chat, "user": allowed_user})
            saved = self.trader._meta("v5_owner_next_offset")
            if saved is None:
                self.trader._set_meta("v5_owner_next_offset", initial_offset)
            elif initial_offset > saved:
                raise TradingError("UNRECONCILED_LEGACY_OFFSET")

    def next_offset(self):
        with self.trader._transaction():
            self.trader._audit_all()
            return self.trader._meta("v5_owner_next_offset")

    def _delivery(self, uid):
        notice = "v7u_" + str(uid)
        if receipt(self.trader, "notice_ack:" + notice, "paper_notice_ack"):
            return "ACK"
        if receipt(self.trader, "notice_unknown:" + notice, "paper_notice_unknown"):
            return "UNKNOWN"
        return "PENDING"

    def _finish(self, uid, digest, result, now):
        with self.trader._transaction():
            key = "v5_owner_update:" + str(uid)
            old = receipt(self.trader, key, "v5_owner_update")
            if old:
                if old["digest"] != digest:
                    raise TradingError("TELEGRAM_UPDATE_CONFLICT")
                result = old["result"]
                duplicate = True
            else:
                if result["handled"]:
                    # Queue and checkpoint commit atomically; no early offset
                    # save as in the observed legacy main().
                    queue_notice(self.trader, "v7u_" + str(uid), result["message"], now)
                record(self.trader, key, "v5_owner_update", now, {"digest": digest, "result": result})
                self.trader._set_meta("v5_owner_next_offset", max(self.next_offset(), uid + 1))
                duplicate = False
            return {**result, "duplicate": duplicate, "next_offset": self.next_offset(),
                    "notice_id": "v7u_" + str(uid) if result["handled"] else None,
                    "delivery": self._delivery(uid) if result["handled"] else "NOT_SENT",
                    "live_enabled": False}

    def prepare(self, item, *, now):
        """Journal a reply before acknowledging the update; never send it."""
        paper_only()
        timestamp(now)
        item, digest = snapshot(item)
        if not isinstance(item, dict) or type(item.get("update_id")) is not int or item["update_id"] < 0:
            raise TradingError("TELEGRAM_UPDATE_REJECTED")
        uid = item["update_id"]
        with self.trader._transaction():
            old = receipt(self.trader, "v5_owner_update:" + str(uid), "v5_owner_update")
            if old:
                return self._finish(uid, digest, old["result"], now)
            if uid < self.next_offset():
                return self._finish(uid, digest, {"ok": False, "handled": False,
                    "reason": "TELEGRAM_OLD_UPDATE", "message": None}, now)
        try:
            authorize_update(item, allowed_chat=self.allowed_chat, allowed_user=self.allowed_user)
        except TradingError as error:
            return self._finish(uid, digest, {"ok": False, "handled": False,
                "reason": error.code, "message": None}, now)
        message = item["message"]
        text = message.get("text")
        if not isinstance(text, str) or not text.strip().startswith("/"):
            return self._finish(uid, digest, {"ok": True, "handled": False,
                "reason": "NON_COMMAND_IGNORED", "message": None}, now)
        try:
            at = timestamp(message.get("date"))
            if now - at > 30 or at - now > 2:
                raise TradingError("TELEGRAM_STALE_UPDATE")
            name = text.strip().split()[0].lower()
            if name in {"/help", "/start"}:
                result = {"ok": True, "message": "PAPER | /durum /analiz /grid /radar /paraakisi /momentum "
                          "/gridplan /papergrid /emergency_stop\nGerçek emirler KAPALI."}
            elif name == "/paper":
                # The legacy paper_card/state contract has not been reviewed.
                # Never silently show the new 600 ledger as the old V5 ledger.
                raise TradingError("LEGACY_PAPER_ROUTE_NOT_BOUND")
            else:
                value = handle_v7_update(self.core, item, allowed_chat=self.allowed_chat,
                                         allowed_user=self.allowed_user, now=now)
                result = {key: value[key] for key in ("ok", "message")}
                if value.get("reason"):
                    result["reason"] = value["reason"]
                    if value["reason"] == "COMMAND_OUTCOME_UNKNOWN_NEW_UPDATE_REQUIRED":
                        self.trader.pause()
        except TradingError as error:
            if error.code == "STORAGE_ERROR":
                raise  # No checkpoint if a durable outcome cannot be saved.
            result = {"ok": False, "reason": error.code, "message": "NO TRADE | " + error.code}
        except Exception:
            self.trader.pause()
            result = {"ok": False, "reason": "COMMAND_PROCESSING_OUTCOME_UNKNOWN",
                      "message": "NO TRADE | COMMAND_PROCESSING_OUTCOME_UNKNOWN"}
        return self._finish(uid, digest, {**result, "handled": True}, now)

    def begin_delivery(self, uid, *, now):
        """Write UNKNOWN before the existing sender can touch the network."""
        paper_only()
        timestamp(now)
        if type(uid) is not int or uid < 0:
            raise TradingError("TELEGRAM_UPDATE_REJECTED")
        with self.trader._transaction():
            prepared = receipt(self.trader, "v5_owner_update:" + str(uid), "v5_owner_update")
            if not prepared or not prepared["result"]["handled"]:
                raise TradingError("TELEGRAM_REPLY_NOT_PREPARED")
            status = self._delivery(uid)
            if status != "PENDING":
                return {"send_allowed": False, "delivery": status, "live_enabled": False}
            mark_notice(self.trader, "v7u_" + str(uid), "UNKNOWN", now)
            return {"send_allowed": True, "delivery": "UNKNOWN", "text": prepared["result"]["message"],
                    "live_enabled": False}

    def pending_deliveries(self):
        """Recover unsent bridge replies after restart; UNKNOWN is never retried."""
        paper_only()
        with self.trader._transaction():
            result = []
            for notice in notices(self.trader):
                name = notice["notice_id"]
                suffix = name.removeprefix("v7u_")
                if notice["status"] != "PENDING" or not name.startswith("v7u_") or not suffix.isdecimal():
                    continue
                uid = int(suffix)
                prepared = receipt(self.trader, "v5_owner_update:" + str(uid), "v5_owner_update")
                if prepared and prepared["result"]["handled"]:
                    result.append({"update_id": uid, "notice_id": name, "text": notice["text"]})
            return result

    def record_delivery(self, uid, reply, *, now):
        """Only a matching Telegram JSON acknowledgement can establish ACK.

        A legacy send(text) returning None cannot establish success. Unknown
        deliveries pause buys and must not be automatically resent.
        """
        paper_only()
        timestamp(now)
        if type(uid) is not int or uid < 0:
            raise TradingError("TELEGRAM_UPDATE_REJECTED")
        with self.trader._transaction():
            prepared = receipt(self.trader, "v5_owner_update:" + str(uid), "v5_owner_update")
            if not prepared or not prepared["result"]["handled"]:
                raise TradingError("TELEGRAM_REPLY_NOT_PREPARED")
            status = self._delivery(uid)
            if status == "PENDING":
                raise TradingError("TELEGRAM_DELIVERY_NOT_BEGUN")
            if status == "ACK":
                return {"delivery": "ACK", "live_enabled": False}
            result = reply.get("result") if isinstance(reply, dict) and reply.get("ok") is True else None
            chat = result.get("chat") if isinstance(result, dict) else None
            valid = (isinstance(chat, dict) and type(chat.get("id")) is int and chat["id"] == self.allowed_chat
                     and type(result.get("message_id")) is int and result["message_id"] > 0
                     and result.get("text") == prepared["result"]["message"])
            if valid:
                mark_notice(self.trader, "v7u_" + str(uid), "ACK", now)
            else:
                self.trader.pause()
            return {"delivery": "ACK" if valid else "UNKNOWN", "live_enabled": False}
