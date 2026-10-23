"""Offline setup/command/delivery regressions; fixtures are never live proof."""
from dataclasses import asdict
from decimal import Decimal
import os
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.control import notices, receipt
from okx_grid.domain import TradingError
from okx_grid.flow_contracts import FlowPolicy
from okx_grid.money_flow import MoneyFlowScanner
from okx_grid.paper import PaperTrader
from okx_grid.profiles import load_profile
from okx_grid.v5_setup import SETUP_NAME, create_observer, manual_planning
from okx_grid.v7_core import V7Core
from okx_grid.v7_decision import PlanningPolicy
from tests.test_v5_owner_bridge import native
from tests.test_v7 import NOW, SyntheticV7, make_system
from tests.test_money_flow import FakeFlowAPI
from tools.v5_observer_smoke import acceptance, main as probe_main


class ManualSetupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "observe-v1.sqlite3"
        self.api = SyntheticV7()
        self.runtime = self.open()
        self.addCleanup(lambda: self.runtime.close())

    def open(self, **kwargs):
        return create_observer(ledger=self.path, allowed_chat=42, allowed_user=43,
                               initial_offset=0, client=self.api, clock_ms=lambda: self.api.at, **kwargs)

    def command(self, text, uid=1):
        item = native(text, uid)
        item["message"]["date"] = self.api.at // 1000
        return self.runtime.bridge.prepare(item, now=self.api.at // 1000)

    def ack(self, text):
        return {"ok": True, "result": {"message_id": 7, "chat": {"id": 42}, "text": text}}

    def test_setup_is_isolated_non_synthetic_unarmed_and_performs_no_get(self):
        state = self.runtime.core.status()
        self.assertEqual(self.api.calls, [])
        self.assertFalse(self.runtime.trader.synthetic)
        self.assertEqual(state["execution_stage"], "OBSERVATION_ONLY")
        self.assertFalse(state["engine_armed"])
        self.assertEqual(state["market"]["data_status"], "DATA_UNVERIFIED")
        self.assertEqual(state["paper"]["portfolio"]["initial_equity"], "600")
        self.assertEqual(state["paper"]["portfolio"]["reserved_capital"], "0")
        self.assertEqual(state["paper"]["bots"], [])
        self.assertFalse(state["live_enabled"])

    def test_criteria_are_active_policy_values_with_explicit_manual_net_floor(self):
        criteria = self.runtime.core.criteria()
        self.assertEqual(criteria["name"], SETUP_NAME)
        grid = criteria["grid"]
        for key, expected in {"min_quote_volume_usdt": "10000000", "max_spread_bps": "10",
                              "capital_per_bot_usdt": "300", "planned_risk_per_bot_usdt": "3.000",
                              "total_capital_usdt": "600", "total_planned_risk_usdt": "6.00"}.items():
            self.assertEqual(Decimal(grid[key]), Decimal(expected))
        self.assertEqual(Decimal(grid["max_spread_percent"]), Decimal("0.1"))
        self.assertEqual(grid["required_book_levels_per_side"], 400)
        self.assertEqual(Decimal(grid["min_depth_usdt_per_side"]), 1500)
        self.assertEqual(grid["planning"], asdict(manual_planning()))
        self.assertEqual(Decimal(grid["min_net_grid_return_percent"]), Decimal("0.1"))
        self.assertTrue(grid["all_mandatory_filters_required"])
        self.assertEqual(criteria["criteria_id"], self.runtime.core.criteria()["criteria_id"])
        self.assertEqual(self.api.calls, [])

    def test_native_criteria_command_handles_case_and_renders_without_gets(self):
        result = self.command("/KRİTERLER")
        self.assertTrue(result["ok"])
        self.assertIn("OBSERVATION_ONLY", result["message"])
        self.assertIn("Net/grid >= %0.100", result["message"])
        self.assertLess(len(result["message"]), 3501)
        self.assertEqual(self.api.calls, [])

    def test_grid_and_gridplan_share_snapshot_and_create_no_pending_execution(self):
        first = self.command("/GRID Suı/USDT")
        self.api.advance()
        second = self.command("/gridplan SUI 300", 2)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertIn("İZLEME", first["message"])
        self.assertIn("DATA_UNVERIFIED", first["message"])
        values = [receipt(self.runtime.trader, "v7_command:" + str(uid), "v7_command")["result"]["value"]
                  for uid in (1, 2)]
        self.assertEqual(values[0]["snapshot_id"], values[1]["snapshot_id"])
        self.assertEqual(values[0]["decision_id"], values[1]["decision_id"])
        self.assertEqual(values[0]["market_summary"]["ticker_ts_ms"], NOW)
        self.assertIsNone(self.runtime.trader._meta("v7_pending_plans"))
        self.assertEqual(self.runtime.trader.status()["portfolio"]["active_bots"], 0)

    def test_observation_blocks_command_cycle_and_direct_open_before_get(self):
        result = self.command("/papergrid start")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "OBSERVATION_ONLY_EXECUTION_DISABLED")
        for call in (lambda: self.runtime.core.start(now=NOW // 1000),
                     lambda: self.runtime.core.run_cycle(cycle_id="blocked", now=NOW // 1000),
                     lambda: self.runtime.trader.open("blocked", None, None, NOW // 1000)):
            with self.assertRaisesRegex(TradingError, "OBSERVATION_ONLY_EXECUTION_DISABLED"):
                call()
        self.assertEqual(self.api.calls, [])
        self.assertIsNone(self.runtime.trader._meta("v7_started"))

    def test_narrow_band_is_rejected_by_manual_net_floor(self):
        value = self.runtime.core.decision("SOL-USDT")
        self.assertIn("INSUFFICIENT_NET_GRID_RETURN", value["reasons"])
        self.assertFalse(value["eligible"])
        self.assertIsNone(value["plan"])
        self.assertEqual(value["action"], "NO TRADE")

    def test_stage_cannot_be_switched_to_paper_on_same_ledger(self):
        with self.assertRaisesRegex(TradingError, "CONTROL_POLICY_MISMATCH"):
            V7Core(self.runtime.trader, self.runtime.core.market, planning=manual_planning())
        self.assertFalse(self.runtime.core.status()["engine_armed"])

    def test_restart_keeps_stage_criteria_offset_and_halt(self):
        identity = self.runtime.core.criteria()["criteria_id"]
        self.command("/emergency_stop", 9)
        self.runtime.close()
        self.runtime = self.open()
        self.assertEqual(self.runtime.core.criteria()["criteria_id"], identity)
        self.assertTrue(self.runtime.trader.status()["halted"])
        self.assertEqual(self.runtime.bridge.next_offset(), 10)
        self.assertEqual(self.runtime.adapter.poll_parameters()["offset"], 10)
        self.assertFalse(self.runtime.core.status()["engine_armed"])

    def test_reference_planning_and_legacy_500_profile_are_unchanged(self):
        self.assertEqual(PlanningPolicy().min_net_grid_return, "0.0001")
        self.assertEqual(load_profile("v5-paper-500").policy.equity, 500)
        self.assertEqual(load_profile("v5-paper-500").policy.max_bot_capital, 500)
        other = Path(self.folder.name) / "existing-paper.sqlite3"
        _, trader, _ = make_system(path=other, synthetic=False)
        trader.close()
        before = other.read_bytes()
        with self.assertRaisesRegex(TradingError, "DEDICATED_OBSERVATION_LEDGER_REQUIRED"):
            create_observer(ledger=other, allowed_chat=42, allowed_user=43, initial_offset=0)
        self.assertEqual(before, other.read_bytes())

    def test_legacy_500_ledger_is_rejected_without_byte_or_permission_changes(self):
        other = Path(self.folder.name) / "legacy-500.sqlite3"
        with PaperTrader(other, profile=load_profile("v5-paper-500")):
            pass
        other.chmod(0o640)
        before = other.read_bytes()
        with self.assertRaisesRegex(TradingError, "DEDICATED_OBSERVATION_LEDGER_REQUIRED"):
            create_observer(ledger=other, allowed_chat=42, allowed_user=43, initial_offset=0)
        self.assertEqual(other.read_bytes(), before)
        self.assertEqual(other.stat().st_mode & 0o777, 0o640)

    def test_acl_drift_and_recovery_sidecar_are_rejected_before_writable_open(self):
        self.runtime.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(TradingError, "CONTROL_POLICY_MISMATCH"):
            create_observer(ledger=self.path, allowed_chat=42, allowed_user=99, initial_offset=0)
        self.assertEqual(self.path.read_bytes(), before)
        sidecar = Path(str(self.path) + "-journal")
        sidecar.touch()
        with self.assertRaisesRegex(TradingError, "OBSERVATION_LEDGER_RECOVERY_REQUIRED"):
            self.open()
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_acl_offset_or_production_path_cannot_create_ledger(self):
        args = {"ledger": Path(self.folder.name) / "invalid.sqlite3", "allowed_chat": 42,
                "allowed_user": 43, "initial_offset": 0}
        for change in ({"allowed_user": True}, {"allowed_chat": -42}, {"initial_offset": -1},
                       {"ledger": "/opt/okx-grid-demo/staging.sqlite3"}, {"ledger": "relative.sqlite3"},
                       {"ledger": str(Path(self.folder.name) / "legacy.json")}):
            with self.subTest(change=change), self.assertRaises(TradingError):
                create_observer(**(args | change))
        self.assertFalse(args["ledger"].exists())

    def test_symlink_and_hardlink_cannot_adopt_another_state_file(self):
        link = Path(self.folder.name) / "link.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(TradingError, "DEDICATED_OBSERVATION_LEDGER_REQUIRED"):
            create_observer(ledger=link, allowed_chat=42, allowed_user=43, initial_offset=0)
        link.unlink()
        os.link(self.path, link)
        with self.assertRaisesRegex(TradingError, "DEDICATED_OBSERVATION_LEDGER_REQUIRED"):
            create_observer(ledger=link, allowed_chat=42, allowed_user=43, initial_offset=0)

    def test_live_environment_refuses_setup_before_database_or_get(self):
        path = Path(self.folder.name) / "never-created.sqlite3"
        with patch.dict(os.environ, {"LIVE_TRADING": "true"}), self.assertRaisesRegex(TradingError, "LIVE_TRADING_DISABLED"):
            create_observer(ledger=path, allowed_chat=42, allowed_user=43, initial_offset=0)
        self.assertFalse(path.exists())
        self.assertEqual(self.api.calls, [])

    def test_public_probe_refuses_network_without_explicit_flag(self):
        with patch("tools.v5_observer_smoke.acceptance", side_effect=AssertionError("no network")), \
                patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as result:
            probe_main([])
        self.assertEqual(result.exception.code, 2)

    def test_public_probe_cannot_promote_injected_data_to_verified(self):
        with self.assertRaisesRegex(TradingError, "DATA_UNVERIFIED"):
            acceptance(client=self.api, clock_ms=lambda: self.api.at)

    def test_expired_price_is_replaced_before_sender_and_warning_ack_matches(self):
        original = self.command("/grid SOL")["message"]
        self.api.advance(31)
        sent = []
        def send(text):
            sent.append(text)
            self.assertEqual(notices(self.runtime.trader)[0]["status"], "UNKNOWN")
            return self.ack(text)
        result = self.runtime.adapter.deliver_pending(send)
        self.assertEqual(result["deliveries"][0]["delivery"], "ACK")
        self.assertIn("STALE_TELEGRAM_REPORT", sent[0])
        self.assertNotIn("Fiyat 100", sent[0])
        self.assertNotEqual(sent[0], original)
        self.assertEqual(self.runtime.adapter.deliver_pending(send)["deliveries"], [])
        self.assertEqual(len(sent), 1)
        self.assertTrue(self.runtime.trader.status()["paused"])

    def test_fresh_market_reply_preserves_text_and_has_matching_ack(self):
        original = self.command("/grid SOL")["message"]
        wire = self.runtime.bridge.begin_delivery(1, now=self.api.at // 1000)
        self.assertEqual(wire["text"], original)
        self.assertEqual(self.runtime.bridge.record_delivery(1, self.ack(original), now=self.api.at // 1000)["delivery"], "ACK")

    def test_source_age_is_checked_even_when_command_receipt_is_recent(self):
        self.runtime.core.market.capture("SOL-USDT")
        self.api.advance(4)  # The cached source is still usable when prepared.
        self.command("/grid SOL")
        self.api.advance(27)  # Command age 27s, actual source age 31s.
        wire = self.runtime.bridge.begin_delivery(1, now=self.api.at // 1000)
        self.assertIn("STALE_TELEGRAM_REPORT", wire["text"])

    def test_restart_pending_reply_cannot_replay_old_quote_as_current(self):
        self.command("/grid SOL")
        self.runtime.close()
        self.api.advance(31)
        self.runtime = self.open()
        wire = self.runtime.bridge.begin_delivery(1, now=self.api.at // 1000)
        self.assertIn("STALE_TELEGRAM_REPORT", wire["text"])
        self.assertEqual(self.api.calls.count(("books", "SOL-USDT", None)), 1)

    def test_clock_rollback_replaces_price_but_preserves_emergency_confirmation(self):
        self.command("/grid SOL")
        self.command("/emergency_stop", 2)
        wire = self.runtime.bridge.begin_delivery(1, now=NOW // 1000 - 3)
        self.assertIn("STALE_TELEGRAM_REPORT", wire["text"])
        confirmation = self.runtime.bridge.begin_delivery(2, now=NOW // 1000 + 100)
        self.assertNotIn("STALE_TELEGRAM_REPORT", confirmation["text"])
        self.assertTrue(self.runtime.trader.status()["halted"])

    def test_delayed_configuration_report_is_not_misclassified_as_price_data(self):
        original = self.command("/kriterler")["message"]
        wire = self.runtime.bridge.begin_delivery(1, now=NOW // 1000 + 3600)
        self.assertEqual(wire["text"], original)

    def test_radar_batch_prices_expire_before_delivery(self):
        result = self.command("/radar")
        self.assertTrue(result["ok"])
        self.api.advance(31)
        wire = self.runtime.bridge.begin_delivery(1, now=self.api.at // 1000)
        self.assertIn("STALE_TELEGRAM_REPORT", wire["text"])

    def test_flow_book_timestamps_expire_before_delivery(self):
        api = FakeFlowAPI(("BTC-USDT", "SOL-USDT"))
        self.runtime.core.flow = MoneyFlowScanner(client=api, policy=FlowPolicy(paper_notional="300"),
                                                 clock_ms=lambda: self.api.at)
        result = self.command("/paraakisi SOL")
        self.assertTrue(result["ok"])
        self.assertIn("CVD", result["message"])
        self.api.advance(31)
        wire = self.runtime.bridge.begin_delivery(1, now=self.api.at // 1000)
        self.assertIn("STALE_TELEGRAM_REPORT", wire["text"])

    def test_ack_for_withheld_price_does_not_ack_expiry_warning_or_resend(self):
        original = self.command("/grid SOL")["message"]
        self.runtime.bridge.begin_delivery(1, now=NOW // 1000 + 31)
        ack = self.runtime.bridge.record_delivery(1, self.ack(original), now=NOW // 1000 + 31)
        self.assertEqual(ack["delivery"], "UNKNOWN")
        self.assertFalse(self.runtime.bridge.begin_delivery(1, now=NOW // 1000 + 32)["send_allowed"])

    def test_storage_failure_cannot_send_or_commit_partial_warning_intent(self):
        self.command("/grid SOL")
        self.api.advance(31)
        original = self.runtime.trader._event
        def fail(event_id, bot_id, kind, at, payload):
            if kind == "paper_notice_unknown":
                raise sqlite3.OperationalError("synthetic disk failure")
            return original(event_id, bot_id, kind, at, payload)
        with patch.object(self.runtime.trader, "_event", side_effect=fail), self.assertRaisesRegex(TradingError, "STORAGE_ERROR"):
            self.runtime.adapter.deliver_pending(lambda text: self.fail("sender must not run"))
        self.assertEqual(notices(self.runtime.trader)[0]["status"], "PENDING")
        self.assertIsNone(receipt(self.runtime.trader, "v5_delivery_text:v7u_1", "v5_delivery_text"))


if __name__ == "__main__":
    unittest.main()
