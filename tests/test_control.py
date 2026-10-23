"""Synthetic single-consumer cycle, ACL and durable replay regressions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.automation import consider_paper_entry
from okx_grid.control import mark_notice, notices, queue_notice
from okx_grid.cycle import run_cycle
from okx_grid.domain import TradingError
from okx_grid.paper import PaperTrader
from okx_grid.profiles import load_profile
from okx_grid.telegram_control import handle_update
from tests.test_automation import candidate


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(":memory:", profile=load_profile("v5-paper-500"))
        self.addCleanup(self.trader.close)

    def cycle(self, **kwargs):
        values = dict(cycle_id="c1", now=1000, proposals=[candidate()], quotes={},
                      allowed_symbols=["TEST-USDT"], notification_healthy=True)
        values.update(kwargs)
        return run_cycle(self.trader, **values)

    def update(self, text="/durum", uid=1, at=1000, **kwargs):
        value = {"update_id": uid, "message": {"chat": {"id": 42, "type": "private"},
                 "from": {"id": 43, "is_bot": False}, "date": at, "text": text}}
        value["message"].update(kwargs)
        return value

    def handle(self, update, now=1000):
        return handle_update(self.trader, update, allowed_chat=42, allowed_user=43, now=now)

    def test_cycle_entry_ticks_tp_and_conservation(self):
        opened = self.cycle()
        bot = opened["entry"]["bot_id"]
        self.cycle(cycle_id="c2", now=1001, proposals=[], quotes={bot: {"price": "99", "observed_at": 1001}})
        closed = self.cycle(cycle_id="c3", now=1002, proposals=[], quotes={bot: {"price": "103", "observed_at": 1002}})
        self.assertEqual(closed["state"]["bots"][0]["reason"], "TAKE_PROFIT")
        self.assertEqual(closed["state"]["portfolio"]["reserved_capital"], "0")
        self.assertEqual(len(notices(self.trader)), 3)
        self.assertIn("PAPER 500", notices(self.trader)[0]["text"])
        self.assertIn('"plan"', notices(self.trader)[0]["text"])

    def test_cycle_stop_gap(self):
        bot = self.cycle()["entry"]["bot_id"]
        self.cycle(cycle_id="c2", now=1001, proposals=[], quotes={bot: {"price": "99", "observed_at": 1001}})
        stopped = self.cycle(cycle_id="c3", now=1002, proposals=[], quotes={bot: {"price": "1", "observed_at": 1002}})
        self.assertEqual(stopped["state"]["bots"][0]["reason"], "STOP_LOSS")
        self.assertTrue(stopped["state"]["halted"])

    def test_replay_notice_and_conflict(self):
        self.cycle()
        self.assertTrue(self.cycle()["duplicate"])
        self.assertEqual(len(notices(self.trader)), 1)
        with self.assertRaisesRegex(TradingError, "PAPER_CYCLE_CONFLICT"):
            self.cycle(proposals=[])

    def test_missing_quote_pauses_without_inventing_liquidation(self):
        self.cycle()
        result = self.cycle(cycle_id="c2", now=1001)
        self.assertTrue(result["state"]["paused"])
        self.assertEqual(result["state"]["bots"][0]["status"], "OPEN")
        self.assertEqual(result["entry"]["reason"], "PAPER_PAUSED")

    def test_pause_blocks_buys_but_allows_stop(self):
        bot = self.cycle()["entry"]["bot_id"]
        self.trader.pause()
        result = self.cycle(cycle_id="c2", now=1001, proposals=[], quotes={bot: {"price": "99", "observed_at": 1001}})
        self.assertEqual(result["state"]["bots"][0]["quantity"], "0")
        result = self.cycle(cycle_id="c3", now=1002, proposals=[], quotes={bot: {"price": "90", "observed_at": 1002}})
        self.assertEqual(result["state"]["bots"][0]["reason"], "STOP_LOSS")

    def test_notification_unknown_blocks_entries(self):
        queue_notice(self.trader, "n1", "paper summary", 999)
        mark_notice(self.trader, "n1", "UNKNOWN", 999)
        self.assertEqual(notices(self.trader)[0]["status"], "UNKNOWN")
        self.assertEqual(self.cycle()["entry"]["reason"], "PAPER_PAUSED")
        mark_notice(self.trader, "n1", "ACK", 1001)
        self.assertTrue(self.trader.status()["paused"])

    def test_notification_health_defaults_safe(self):
        result = self.cycle(notification_healthy=False)
        self.assertEqual(result["entry"]["decision"], "BLOCKED")

    def test_allowlist_policy_pinned(self):
        self.assertEqual(self.cycle(proposals=[candidate("ALT-USDT")])["entry"]["decision"], "NO_ELIGIBLE_CANDIDATE")
        with self.assertRaisesRegex(TradingError, "CONTROL_POLICY_MISMATCH"):
            self.cycle(cycle_id="c2", now=1001, allowed_symbols=["ALT-USDT"])

    def test_atomic_rollback_includes_open_notice_and_receipts(self):
        with patch("okx_grid.cycle.queue_notice", side_effect=TradingError("SIMULATED_DISK_ERROR")):
            with self.assertRaisesRegex(TradingError, "SIMULATED_DISK_ERROR"):
                self.cycle()
        self.assertEqual(self.trader.status()["bots"], [])
        self.assertEqual(notices(self.trader), [])
        self.assertEqual(self.cycle()["entry"]["decision"], "OPENED")

    def test_old_cycle_does_not_repeat_ticks(self):
        self.cycle()
        with self.assertRaisesRegex(TradingError, "OUT_OF_ORDER_CYCLE"):
            self.cycle(cycle_id="different")

    def test_filtered_decision_receipt_persists(self):
        p = candidate(at=900)
        first = consider_paper_entry(self.trader, [p], cycle_id="filtered", now=1000)
        self.assertEqual(first["decision"], "NO_ELIGIBLE_CANDIDATE")
        p["market"]["observed_at"] = 1000
        with self.assertRaisesRegex(TradingError, "AUTO_CYCLE_CONFLICT"):
            consider_paper_entry(self.trader, [p], cycle_id="filtered", now=1000)

    def test_commands_return_real_ledger_without_network(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            for index, command in enumerate(("/durum", "/analiz TEST-USDT", "/radar", "/grid", "/paper")):
                result = self.handle(self.update(command, index + 1, 1000 + index), now=1000 + index)
                self.assertEqual(result["state"]["portfolio"]["initial_equity"], "500")
                self.assertEqual(result["source_status"], "V5_NOT_CONNECTED")
                self.assertEqual(result["delivery"], "NOT_SENT")

    def test_telegram_acl_before_replay(self):
        self.handle(self.update())
        for change in ({"chat": {"id": 44, "type": "private"}}, {"from": {"id": 44, "is_bot": False}},
                       {"chat": {"id": 42, "type": "group"}}, {"from": {"id": 43, "is_bot": True}},
                       {"forward_origin": {}}, {"sender_chat": {}}, {"edit_date": 1000}):
            with self.subTest(change=change), self.assertRaisesRegex(TradingError, "TELEGRAM_UNAUTHORIZED"):
                self.handle(self.update(**change))

    def test_telegram_replay_does_not_repeat_halt(self):
        original = self.handle(self.update("/paper halt"))
        with patch.object(self.trader, "halt", side_effect=AssertionError("duplicate side effect")):
            self.assertTrue(self.handle(self.update("/paper halt"), now=2000)["duplicate"])
        self.assertTrue(original["state"]["halted"])
        with self.assertRaisesRegex(TradingError, "TELEGRAM_UPDATE_CONFLICT"):
            self.handle(self.update("/paper pause"))

    def test_stale_rate_and_old_updates(self):
        with self.assertRaisesRegex(TradingError, "TELEGRAM_STALE_UPDATE"):
            self.handle(self.update(at=900))
        self.handle(self.update(uid=2))
        with self.assertRaisesRegex(TradingError, "TELEGRAM_RATE_LIMIT"):
            self.handle(self.update(uid=3))
        with self.assertRaisesRegex(TradingError, "TELEGRAM_OLD_UPDATE"):
            self.handle(self.update(uid=1), now=1001)

    def test_telegram_rejects_live_free_text_and_edited_update(self):
        commands = {"/buy TEST-USDT": "UNKNOWN_COMMAND", "/paper live": "COMMAND_ARGUMENTS_REQUIRED",
                    "/paper resume": "COMMAND_ARGUMENTS_REQUIRED", "/durum@otherbot": "UNKNOWN_COMMAND",
                    "sell all": "TELEGRAM_COMMAND_REJECTED"}
        for command, reason in commands.items():
            with self.subTest(command=command), self.assertRaisesRegex(TradingError, reason):
                self.handle(self.update(command))
        with self.assertRaisesRegex(TradingError, "TELEGRAM_UPDATE_REJECTED"):
            self.handle({"update_id": 1, "edited_message": self.update()["message"]})

    def test_private_restart_receipts_and_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.sqlite3"
            for iteration in range(2):
                with PaperTrader(path, profile=load_profile("v5-paper-500")) as trader:
                    result = handle_update(trader, self.update("/paper pause"), allowed_chat=42, allowed_user=43, now=1000)
                    self.assertEqual(result["duplicate"], bool(iteration))
                    self.assertTrue(trader.status()["paused"])

    def test_concurrent_cycles_same_pair_open_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.sqlite3"
            with PaperTrader(path, profile=load_profile("v5-paper-500")):
                pass
            def enter(index):
                with PaperTrader(path, profile=load_profile("v5-paper-500")) as trader:
                    return consider_paper_entry(trader, [candidate()], cycle_id="parallel_" + str(index), now=1000)
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(enter, range(2)))
            self.assertEqual(sum(r["decision"] == "OPENED" for r in outcomes), 1)
            with PaperTrader(path, profile=load_profile("v5-paper-500")) as trader:
                self.assertEqual(trader.status()["portfolio"]["reserved_capital"], "90")


if __name__ == "__main__":
    unittest.main()
