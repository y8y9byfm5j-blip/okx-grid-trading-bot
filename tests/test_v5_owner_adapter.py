"""Synthetic tests of the observed V5 API/sender contract; no Telegram calls."""
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.control import notices, queue_notice
from okx_grid.domain import TradingError
from okx_grid.v5_owner_adapter import V5OwnerAdapter
from okx_grid.v5_owner_bridge import V5OwnerBridge
from tests.test_v5_owner_bridge import native
from tests.test_v7 import NOW, make_system


class V5OwnerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.api, self.trader, self.core = make_system()
        self.addCleanup(self.trader.close)
        self.bridge = V5OwnerBridge(self.core, allowed_chat=42, allowed_user=43, initial_offset=0)
        self.at = NOW // 1000
        self.adapter = V5OwnerAdapter(self.bridge, clock=lambda: self.at)
        self.sent = []

    def response(self, *items):
        return {"ok": True, "result": list(items)}

    def send(self, text):
        # This fixture models the observed return api('sendMessage', ...) shape.
        # Its ACK is invented and is not proof of actual Telegram delivery.
        self.sent.append(text)
        self.assertTrue(any(n["text"] == text and n["status"] == "UNKNOWN" for n in notices(self.trader)))
        return {"ok": True, "result": {"message_id": len(self.sent), "chat": {"id": 42}, "text": text}}

    def test_parameters_only_use_durable_offset_and_safe_timeout_margin(self):
        self.assertEqual(self.adapter.poll_parameters(), {"timeout": 20, "offset": 0,
            "limit": 100, "allowed_updates": '["message"]'})
        self.adapter.consume_updates(self.response(native("/help", 8)))
        self.assertEqual(self.adapter.poll_parameters()["offset"], 9)
        self.assertEqual(self.api.calls, [])

    def test_unsafe_poll_policy_is_rejected_before_use(self):
        for kwargs in ({"poll_timeout": 25}, {"max_updates": 101}, {"max_updates": True},
                       {"transport_timeout": 29}, {"clock": None}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(TradingError, "INVALID_TELEGRAM_POLL_POLICY"):
                V5OwnerAdapter(self.bridge, **kwargs)

    def test_invalid_batch_is_rejected_before_any_command_or_checkpoint(self):
        first = native("/emergency_stop", 1)
        cases = [None, {"ok": False, "description": "private value", "result": [first]},
                 {"ok": 1, "result": []}, self.response(first, {"update_id": True}),
                 self.response(first, first), self.response(first, {"update_id": 0}),
                 self.response(*[native("/help", i) for i in range(101)])]
        for response in cases:
            with self.subTest(response_type=type(response).__name__), self.assertRaisesRegex(TradingError, "TELEGRAM_UPDATES_REJECTED"):
                self.adapter.consume_updates(response)
            self.assertEqual(self.bridge.next_offset(), 0)
            self.assertFalse(self.trader.status()["halted"])
            self.assertTrue(self.trader.status()["paused"])
            self.assertEqual(self.api.calls, [])
        self.assertNotIn("private value", str(self.trader._meta("v5_owner_poll_health")))

    def test_native_security_flags_and_sender_are_preserved(self):
        forwarded = native("/grid SUI", 1)
        forwarded["message"]["forward_origin"] = {"type": "hidden_user"}
        other = native("/emergency_stop", 2)
        other["message"]["from"]["id"] = 99
        values = self.adapter.consume_updates(self.response(forwarded, other))["updates"]
        self.assertTrue(all(v["reason"] == "TELEGRAM_UNAUTHORIZED" for v in values))
        self.assertFalse(self.trader.status()["halted"])
        self.assertEqual(self.bridge.next_offset(), 3)
        self.assertEqual(self.adapter.deliver_pending(self.send)["deliveries"], [])
        self.assertEqual(self.api.calls, [])

    def test_actual_sender_return_shape_acks_once_without_early_legacy_offset_write(self):
        item = native("/GRID Suı/USDT", 1)
        before = copy.deepcopy(item)
        self.adapter.consume_updates(self.response(item))
        self.assertEqual(item, before)
        result = self.adapter.deliver_pending(self.send)
        self.assertEqual(result["deliveries"], [{"notice_id": "v7u_1", "delivery": "ACK"}])
        self.assertIn("SUI-USDT", self.sent[0])
        self.assertIn("DATA_UNVERIFIED", self.sent[0])
        self.assertTrue(self.adapter.notification_ready())
        self.api.calls.clear()
        self.assertTrue(self.adapter.consume_updates(self.response(item))["updates"][0]["duplicate"])
        self.assertEqual(self.adapter.deliver_pending(self.send)["deliveries"], [])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.api.calls, [])

    def test_cycle_summaries_use_the_same_sender_ack_journal(self):
        self.adapter.consume_updates(self.response())
        self.core.run_cycle(cycle_id="not_armed", now=self.at)
        pending = self.bridge.pending_notifications()
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["notice_id"].startswith("v7c_"))
        self.assertFalse(self.adapter.notification_ready())
        self.assertEqual(self.adapter.deliver_pending(self.send)["deliveries"][0]["delivery"], "ACK")
        self.assertIn("PAPER 600 USDT", self.sent[0])
        self.assertTrue(self.adapter.notification_ready())
        self.assertEqual(self.trader.status()["portfolio"]["active_bots"], 0)

    def test_sender_timeout_stays_unknown_and_does_not_resend_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "separate-v7.sqlite3"
            _, trader, core = make_system(path=path)
            with trader:
                bridge = V5OwnerBridge(core, allowed_chat=42, allowed_user=43, initial_offset=0)
                adapter = V5OwnerAdapter(bridge, clock=lambda: self.at)
                adapter.consume_updates(self.response(native("/help", 1)))
                def timeout(text):
                    raise TimeoutError("private request value")
                result = adapter.deliver_pending(timeout)
                self.assertEqual(result["deliveries"][0]["delivery"], "UNKNOWN")
                self.assertTrue(trader.status()["paused"])
            _, trader, core = make_system(path=path)
            with trader:
                bridge = V5OwnerBridge(core, allowed_chat=42, allowed_user=43, initial_offset=0)
                adapter = V5OwnerAdapter(bridge, clock=lambda: self.at)
                self.assertEqual(adapter.poll_parameters()["offset"], 2)
                self.assertEqual(adapter.deliver_pending(self.send)["deliveries"], [])
                self.assertFalse(adapter.notification_ready())
                raw = str(trader._db.execute("SELECT payload FROM events").fetchall())
                self.assertNotIn("private request value", raw)

    def test_invalid_ack_stops_batch_without_attempting_later_pending_reply(self):
        self.adapter.consume_updates(self.response(native("/help", 1), native("/help", 2)))
        self.assertFalse(self.adapter.notification_ready())
        with patch("builtins.print", side_effect=AssertionError("no raw errors")):
            result = self.adapter.deliver_pending(lambda text: None)
        self.assertEqual(len(result["deliveries"]), 1)
        self.assertEqual(result["deliveries"][0]["delivery"], "UNKNOWN")
        self.assertEqual([n["status"] for n in notices(self.trader)], ["UNKNOWN", "PENDING"])
        self.assertTrue(self.trader.status()["paused"])

    def test_storage_failure_before_send_never_calls_sender_and_keeps_reply_pending(self):
        self.adapter.consume_updates(self.response(native("/help", 1)))
        original = self.trader._event
        def fail(event_id, bot_id, kind, at, payload):
            if kind == "paper_notice_unknown":
                raise sqlite3.OperationalError("synthetic full disk")
            return original(event_id, bot_id, kind, at, payload)
        with patch.object(self.trader, "_event", side_effect=fail), self.assertRaisesRegex(TradingError, "STORAGE_ERROR"):
            self.adapter.deliver_pending(self.send)
        self.assertEqual(self.sent, [])
        self.assertEqual(notices(self.trader)[0]["status"], "PENDING")
        self.assertEqual(self.bridge.next_offset(), 2)

    def test_two_paper_positions_are_gated_by_actual_adapter_ack_state(self):
        for uid, command in enumerate(("/gridplan SOL 300", "/gridplan Suı/USDT 300", "/papergrid start"), 1):
            self.api.advance()
            self.at = self.api.at // 1000
            item = native(command, uid)
            item["message"]["date"] = self.at
            result = self.adapter.consume_updates(self.response(item))
            self.assertTrue(result["updates"][0]["ok"])
            self.adapter.deliver_pending(self.send)
        self.assertTrue(self.adapter.notification_ready())
        self.api.advance()
        self.at = self.api.at // 1000
        cycle = self.core.run_cycle(cycle_id="adapter_two", now=self.at,
                                   notification_healthy=self.adapter.notification_ready())
        self.assertEqual(len(cycle["opened"]), 2)
        self.assertEqual(cycle["data_mode"], "SYNTHETIC_TEST")
        self.assertEqual(cycle["state"]["portfolio"]["reserved_capital"], "600")
        self.assertFalse(self.adapter.notification_ready())  # Cycle summary is still pending.
        self.adapter.deliver_pending(self.send)
        self.assertTrue(self.adapter.notification_ready())
        self.adapter.poll_failed()
        self.api.advance()
        self.at = self.api.at // 1000
        blocked = self.core.run_cycle(cycle_id="adapter_poll_failed", now=self.at,
                                     notification_healthy=self.adapter.notification_ready())
        self.assertTrue(blocked["state"]["paused"])
        self.assertEqual(blocked["opened"], [])
        self.assertEqual(blocked["state"]["portfolio"]["reserved_capital"], "600")

    def test_crash_during_sender_leaves_write_ahead_unknown(self):
        self.adapter.consume_updates(self.response(native("/help", 1)))
        def crash(text):
            raise SystemExit
        with self.assertRaises(SystemExit):
            self.adapter.deliver_pending(crash)
        self.assertEqual(notices(self.trader)[0]["status"], "UNKNOWN")
        self.assertFalse(self.adapter.notification_ready())
        self.assertEqual(self.adapter.deliver_pending(self.send)["deliveries"], [])

    def test_poll_failure_pause_persists_and_backoff_is_bounded_without_sleep(self):
        self.core.start(now=self.at)
        self.assertFalse(self.trader.status()["paused"])
        with patch("time.sleep", side_effect=AssertionError("caller owns scheduling")):
            delays = [self.adapter.poll_failed()["retry_after_seconds"] for _ in range(9)]
        self.assertEqual(delays, [1, 2, 4, 8, 16, 30, 30, 30, 30])
        self.assertTrue(self.trader.status()["paused"])
        self.adapter.consume_updates(self.response(native("/help", 1)))
        self.adapter.deliver_pending(self.send)
        self.assertTrue(self.adapter.notification_ready())
        self.assertTrue(self.trader.status()["paused"])
        self.assertFalse(self.core.status()["engine_armed"])

    def test_local_health_expires_and_clock_rollback_is_not_healthy(self):
        self.adapter.consume_updates(self.response(native("/help", 1)))
        self.adapter.deliver_pending(self.send)
        self.assertTrue(self.adapter.notification_ready())
        self.at += 31
        self.assertFalse(self.adapter.notification_ready())
        self.at -= 32
        self.assertFalse(self.adapter.notification_ready())

    def test_slow_command_does_not_refresh_age_of_old_poll_response(self):
        original = self.bridge.prepare
        def slow(item, *, now):
            result = original(item, now=now)
            self.at += 31
            return result
        with patch.object(self.bridge, "prepare", side_effect=slow):
            self.adapter.consume_updates(self.response(native("/help", 1)))
        self.adapter.deliver_pending(self.send)
        self.assertFalse(self.adapter.notification_ready())  # Fresh ACK, old polling response.

    def test_legacy_outboxes_are_not_sent_or_silently_marked_delivered(self):
        queue_notice(self.trader, "legacy_notice", "Synthetic legacy notice", self.at)
        self.assertEqual(self.adapter.deliver_pending(self.send)["deliveries"], [])
        self.assertEqual(len(notices(self.trader)), 1)
        with self.assertRaisesRegex(TradingError, "TELEGRAM_NOTICE_NOT_OWNED"):
            self.bridge.begin_notice_delivery("legacy_notice", now=self.at)

    def test_live_environment_cannot_trigger_supplied_sender(self):
        self.adapter.consume_updates(self.response(native("/help", 1)))
        with patch.dict("os.environ", {"LIVE_TRADING": "true"}), self.assertRaisesRegex(TradingError, "LIVE_TRADING_DISABLED"):
            self.adapter.deliver_pending(self.send)
        self.assertEqual(self.sent, [])
        self.assertEqual(notices(self.trader)[0]["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
