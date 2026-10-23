"""Staging bridge for the observed V5 getUpdates -> handle(text) -> send loop.

The existing owner passes the full update. This module never polls, sends,
imports production V5, or reads its offset/state files. Its checkpoint and
outbox live only in the separate V7 ledger. Deployment remains unverified.
"""
import re

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
        return self._notice_delivery("v7u_" + str(uid))

    def _notice_delivery(self, notice):
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
                result = {"ok": True, "message": ("İZLEME" if self.core.observation_only else "PAPER") + " | /durum /analiz /grid /radar /paraakisi /momentum "
                          "/gridplan /kriterler /papergrid /emergency_stop\nGerçek emirler KAPALI."}
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
            return self.begin_notice_delivery("v7u_" + str(uid), now=now)

    def _notice_text(self, notice_id):
        # Only this bridge's replies and the V7 core's cycle summaries may be
        # handed to the existing sender. Legacy/V6 outboxes are not adopted.
        if not isinstance(notice_id, str) or not re.fullmatch(r"v7u_[0-9]+|v7c_[0-9a-f]{28}", notice_id):
            raise TradingError("TELEGRAM_NOTICE_NOT_OWNED")
        value = receipt(self.trader, "notice:" + notice_id, "paper_notice")
        if value is None:
            raise TradingError("TELEGRAM_REPLY_NOT_PREPARED")
        if notice_id.startswith("v7u_"):
            prepared = receipt(self.trader, "v5_owner_update:" + notice_id[4:], "v5_owner_update")
            if not prepared or not prepared["result"]["handled"] or prepared["result"]["message"] != value["text"]:
                raise TradingError("TELEGRAM_REPLY_NOT_PREPARED")
        return value["text"]

    def pending_notifications(self):
        """Unattempted replies and cycle summaries; no legacy outbox adoption."""
        paper_only()
        with self.trader._transaction():
            return [{"notice_id": n["notice_id"], "text": self._notice_text(n["notice_id"])}
                    for n in notices(self.trader) if n["status"] == "PENDING" and
                    n["notice_id"].startswith(("v7u_", "v7c_"))]

    def begin_notice_delivery(self, notice_id, *, now):
        """Journal UNKNOWN before the owner invokes its existing send(text)."""
        paper_only()
        timestamp(now)
        with self.trader._transaction():
            text = self._notice_text(notice_id)
            status = self._notice_delivery(notice_id)
            if status != "PENDING":
                return {"send_allowed": False, "delivery": status, "live_enabled": False}
            expired = self._market_reply_expired(notice_id, now)
            if expired:
                text = ("DATA_UNVERIFIED | STALE_TELEGRAM_REPORT\n"
                        "Fiyat/defter raporu teslimden önce eskidi veya saat tutarsız. "
                        "Güncel veri için komutu yeniden gönder.\nGerçek emirler KAPALI.")
                self.trader.pause()
            # Freeze the actual wire text atomically with UNKNOWN. A stale
            # warning has its own matching ACK; never ACK the withheld quote.
            record(self.trader, "v5_delivery_text:" + notice_id, "v5_delivery_text", now,
                   {"text": text, "market_reply_expired": expired})
            mark_notice(self.trader, notice_id, "UNKNOWN", now)
            return {"send_allowed": True, "delivery": "UNKNOWN", "text": text, "live_enabled": False}

    def _market_reply_expired(self, notice_id, now):
        if not notice_id.startswith("v7u_"):
            return False  # Cycle summaries are historical event notifications.
        entry = receipt(self.trader, "v7_command:" + notice_id[4:], "v7_command")
        if not entry:
            return False
        result = entry["result"]
        command = result.get("command", "").split()
        if not command or command[0] not in {"/analiz", "/grid", "/gridplan", "/radar", "/paraakisi", "/momentum"}:
            return False
        row = self.trader._db.execute("SELECT at FROM events WHERE id=?",
            ("v7_command:" + notice_id[4:],)).fetchone()
        times = [timestamp(row[0]) * 1000]
        def collect(value):
            if not isinstance(value, dict):
                return
            for key in ("as_of_ms", "ticker_ts_ms", "book_ts_ms", "flow_window_end_ms"):
                if key in value:
                    times.append(timestamp(value[key]))
            summary = value.get("market_summary", {})
            for key in ("ticker_ts_ms", "book_ts_ms"):
                if key in summary:
                    times.append(timestamp(summary[key]))
            book = value.get("book", {})
            if "cursor" in book:
                times.append(timestamp(book["cursor"]["at_ms"]))
        value = result.get("value", {})
        collect(value)
        for pair in value.get("pairs", {}).values():
            collect(pair)
        for decision in value.get("decisions", []):
            collect(decision)
        # Check real source timestamps, not just analysis completion/ACK time.
        return (any(at - now * 1000 > 2000 for at in times) or
                now * 1000 - min(times) > self.trader.policy.max_market_age * 1000)

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

        The observed V5 send(text) returns the actual API JSON. None cannot
        establish success. Unknown deliveries must not be automatically resent.
        """
        paper_only()
        timestamp(now)
        if type(uid) is not int or uid < 0:
            raise TradingError("TELEGRAM_UPDATE_REJECTED")
        with self.trader._transaction():
            prepared = receipt(self.trader, "v5_owner_update:" + str(uid), "v5_owner_update")
            if not prepared or not prepared["result"]["handled"]:
                raise TradingError("TELEGRAM_REPLY_NOT_PREPARED")
            return self.record_notice_delivery("v7u_" + str(uid), reply, now=now)

    def record_notice_delivery(self, notice_id, reply, *, now):
        """Validate the same ACK contract for replies and cycle summaries."""
        paper_only()
        timestamp(now)
        with self.trader._transaction():
            text = self._notice_text(notice_id)
            wire = receipt(self.trader, "v5_delivery_text:" + notice_id, "v5_delivery_text")
            if wire is not None:
                text = wire["text"]
            status = self._notice_delivery(notice_id)
            if status == "PENDING":
                raise TradingError("TELEGRAM_DELIVERY_NOT_BEGUN")
            if status == "ACK":
                return {"delivery": "ACK", "live_enabled": False}
            result = reply.get("result") if isinstance(reply, dict) and reply.get("ok") is True else None
            chat = result.get("chat") if isinstance(result, dict) else None
            valid = (isinstance(chat, dict) and type(chat.get("id")) is int and chat["id"] == self.allowed_chat
                     and type(result.get("message_id")) is int and result["message_id"] > 0
                     and result.get("text") == text)
            if valid:
                mark_notice(self.trader, notice_id, "ACK", now)
                self.trader._set_meta("v5_owner_last_ack_at", now)
            else:
                self.trader.pause()
            return {"delivery": "ACK" if valid else "UNKNOWN", "live_enabled": False}
