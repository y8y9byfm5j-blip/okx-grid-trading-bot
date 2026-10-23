"""Caller-driven adapter for the observed V5 API JSON and send(text) contract.

No token, HTTP client, poll loop, scheduler, production imports or legacy-file
access. The existing owner alone obtains getUpdates and supplies send(text).
"""
import json
import math
import time

from .control import notices, pin, snapshot
from .domain import TradingError, paper_only, timestamp
from .v5_owner_bridge import V5OwnerBridge


class V5OwnerAdapter:
    def __init__(self, bridge, *, poll_timeout=20, transport_timeout=30,
                 max_updates=100, clock=time.time):
        paper_only()
        if not isinstance(bridge, V5OwnerBridge):
            raise TradingError("V5_OWNER_BRIDGE_REQUIRED")
        if (type(poll_timeout) is not int or type(transport_timeout) is not int or
                not 1 <= poll_timeout <= 50 or not poll_timeout + 10 <= transport_timeout <= 120 or
                type(max_updates) is not int or not 1 <= max_updates <= 100 or not callable(clock)):
            raise TradingError("INVALID_TELEGRAM_POLL_POLICY")
        self.bridge, self.trader, self.clock = bridge, bridge.trader, clock
        self.poll_timeout, self.max_updates = poll_timeout, max_updates
        with self.trader._transaction():
            pin(self.trader, "v5_owner_adapter_policy", {"revision": 1, "poll_timeout": poll_timeout,
                "transport_timeout": transport_timeout, "max_updates": max_updates})

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise TradingError("INVALID_TIMESTAMP")
        return timestamp(int(value))

    def poll_parameters(self):
        """Parameters only. The single existing owner makes the API call."""
        paper_only()
        return {"timeout": self.poll_timeout, "offset": self.bridge.next_offset(),
                "limit": self.max_updates, "allowed_updates": json.dumps(["message"])}

    def poll_failed(self):
        """Owner calls on any polling failure; no exception strings are stored."""
        paper_only()
        now = self._now()
        with self.trader._transaction():
            health = self.trader._meta("v5_owner_poll_health") or {}
            failures = min(health.get("consecutive_failures", 0) + 1, 6)
            self.trader._set_meta("v5_owner_poll_health", {"ok": False,
                "reason": "TELEGRAM_POLL_FAILED", "checked_at": now,
                "consecutive_failures": failures})
            self.trader.pause()
        # Delay instruction, not a sleep or retry loop. The owner schedules it.
        return {"ok": False, "reason": "TELEGRAM_POLL_FAILED",
                "retry_after_seconds": min(2 ** (failures - 1), 30), "live_enabled": False}

    def consume_updates(self, response):
        """Validate the entire envelope before preparing any update/checkpoint."""
        paper_only()
        received_at = self._now()
        try:
            response, _ = snapshot(response)
            items = response.get("result") if isinstance(response, dict) and response.get("ok") is True else None
            if not isinstance(items, list) or len(items) > self.max_updates:
                raise TradingError("TELEGRAM_UPDATES_REJECTED")
            previous = -1
            for item in items:
                if (not isinstance(item, dict) or type(item.get("update_id")) is not int or
                        item["update_id"] <= previous):
                    raise TradingError("TELEGRAM_UPDATES_REJECTED")
                previous = item["update_id"]
        except TradingError:
            self.poll_failed()
            raise TradingError("TELEGRAM_UPDATES_REJECTED") from None
        results = []
        for item in items:
            try:
                value = self.bridge.prepare(item, now=self._now())
            except TradingError:
                self.poll_failed()
                raise
            # No raw command, identity or API payload in the batch summary.
            results.append({"update_id": item["update_id"], "handled": value["handled"],
                "ok": value["ok"], "reason": value.get("reason"), "duplicate": value["duplicate"]})
        with self.trader._transaction():
            # A slow command must not make an old polling response look fresh.
            self.trader._set_meta("v5_owner_poll_health", {"ok": True, "checked_at": received_at,
                "consecutive_failures": 0})
        # A good poll never clears a persistent pause or an unknown delivery.
        return {"updates": results, "next_offset": self.bridge.next_offset(), "live_enabled": False}

    def deliver_pending(self, send, *, limit=10):
        """Invoke only the explicitly supplied existing sender, once per notice.

        No automatic resend on timeout, crash or ambiguous response. The batch
        stops at its first UNKNOWN. It never uses the legacy save_offset().
        """
        paper_only()
        if not callable(send) or type(limit) is not int or not 1 <= limit <= 100:
            raise TradingError("INVALID_TELEGRAM_DELIVERY_POLICY")
        results = []
        for notice in self.bridge.pending_notifications()[:limit]:
            paper_only()
            notice_id = notice["notice_id"]
            attempt = self.bridge.begin_notice_delivery(notice_id, now=self._now())
            if not attempt["send_allowed"]:
                continue
            try:
                reply = send(attempt["text"])
            except Exception:
                reply = None
            value = self.bridge.record_notice_delivery(notice_id, reply, now=self._now())
            results.append({"notice_id": notice_id, "delivery": value["delivery"]})
            if value["delivery"] == "UNKNOWN":
                break
        return {"deliveries": results, "live_enabled": False}

    def notification_ready(self):
        """Local ACK/freshness gate, not proof of production activation.

        A synthetic callback can satisfy this in tests. The existing owner may
        use it for its cycle only after actual transport/staging verification.
        """
        paper_only()
        now = self._now()
        with self.trader._transaction():
            health = self.trader._meta("v5_owner_poll_health") or {}
            ack = self.trader._meta("v5_owner_last_ack_at")
            return (health.get("ok") is True and 0 <= now - health["checked_at"] <= 30 and
                    ack is not None and 0 <= now - ack <= 30 and not notices(self.trader))
