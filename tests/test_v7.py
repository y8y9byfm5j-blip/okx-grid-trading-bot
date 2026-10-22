"""Synthetic-only acceptance and screenshot-class regressions; no live claims."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.commands import REGISTRY, pair_name, parse_command
from okx_grid.control import mark_notice, notices, queue_notice
from okx_grid.domain import GridPlan, Market, TradingError, build_plan
from okx_grid.paper import PaperTrader
from okx_grid.profiles import load_profile
from okx_grid.public_contracts import PERIODS, TR_ORIGIN, request_spec
from okx_grid.public_http import ReadOnlyTRClient
from okx_grid.telegram_control import handle_update
from okx_grid.telegram_v7 import handle_v7_update, render_decision
from okx_grid.v7_core import V7Core
from okx_grid.v7_decision import PlanningPolicy, decide, score_checks
from okx_grid.v7_market import V7MarketData
from okx_grid.v7_paper import V7PaperTrader
from tests.test_public_contracts import NOW, envelope


class SyntheticV7:
    """Invented prices, bars and books. Never eligible for real-data promotion."""
    def __init__(self):
        self.at, self.sequence = NOW, 100
        self.names = ["BTC-USDT", "PEPE-USDT", "SOL-USDT", "SUI-USDT", "TEST-USDT"]
        self.price, self.quantity = Decimal("100"), "1000"
        self.volume, self.btc_close = "20000000", "100"
        self.fail, self.short_book, self.gap_candles = None, False, False
        self.calls = []

    def advance(self, seconds=1, price=None):
        self.at += seconds * 1000
        self.sequence += 1
        if price is not None:
            self.price = Decimal(price)

    def fetch(self, kind, instrument=None, *, bar=None):
        self.calls.append((kind, instrument, bar))
        if self.fail == kind:
            raise TradingError("PUBLIC_HTTP_UNAVAILABLE")
        if kind == "time":
            return envelope({"ts": str(self.at)})
        if kind == "instruments":
            return envelope(*[{"instId": n, "instType": "SPOT", "state": "live", "baseCcy": n.split("-")[0],
                               "quoteCcy": "USDT", "tickSz": "0.01", "lotSz": "0.001", "minSz": "0.001"} for n in self.names])
        if kind == "ticker":
            return envelope({"instId": instrument, "instType": "SPOT", "last": str(self.price),
                             "bidPx": str(self.price - Decimal(".01")), "askPx": str(self.price + Decimal(".01")),
                             "volCcy24h": self.volume, "vol24h": "999999999999", "ts": str(self.at)})
        if kind == "books":
            count = 399 if self.short_book else 400
            return envelope({"ts": str(self.at), "seqId": self.sequence,
                             "bids": [[str(self.price - Decimal(".01") * (i + 1)), self.quantity, "0", "1"] for i in range(count)],
                             "asks": [[str(self.price + Decimal(".01") * (i + 1)), self.quantity, "0", "1"] for i in range(count)]})
        if kind == "grid_candles":
            period = PERIODS[bar]
            at = self.at // period * period
            rows = []
            for i in range(1, 21):
                end = at - period * (i + (1 if self.gap_candles and i > 10 else 0))
                close = self.btc_close if instrument == "BTC-USDT" else "100"
                rows.append([str(end), "100", "100.3", "99.7", close, "100", "100", "10000", "1"])
            return envelope(*rows)
        raise AssertionError(kind)


def make_system(*, synthetic=True, path=":memory:", client=None):
    client = client or SyntheticV7()
    trader = V7PaperTrader(path, synthetic=synthetic)
    market = V7MarketData(trader.policy, client=client, clock_ms=lambda: client.at)
    return client, trader, V7Core(trader, market)


def update(text, uid=1, at=NOW // 1000):
    return {"update_id": uid, "message": {"date": at, "text": text,
            "chat": {"id": 42, "type": "private"}, "from": {"id": 43, "is_bot": False}}}


class CommandRegistryTests(unittest.TestCase):
    def test_sui_variations_and_supported_pair_formats(self):
        for text in ("/grid SUI", "/GRID SUI", "/GrId sui-usdt", "/grid sui/usdt", "/grid SUI_USDT", "/grid SUIUSDT"):
            self.assertEqual(parse_command(text).text, "/grid SUI-USDT")

    def test_registry_covers_every_requested_command(self):
        self.assertTrue({"/durum", "/analiz", "/grid", "/radar", "/paraakisi", "/momentum", "/gridplan", "/papergrid", "/emergency_stop"} <= REGISTRY)
        self.assertEqual(parse_command("/GRIDPLAN sol 300").text, "/gridplan SOL-USDT 300")
        self.assertEqual(parse_command("/PAPERGRID START").text, "/papergrid start")
        self.assertEqual(parse_command("/gridstatus").text, "/papergrid status")
        self.assertEqual(parse_command("/radargrid").text, "/radar")

    def test_unknown_and_bad_arguments_are_explicit(self):
        for text, code in (("/gird SUI", "UNKNOWN_COMMAND"), ("/papergrid", "COMMAND_ARGUMENTS_REQUIRED"),
                           ("/gridplan SOL 0", "NON_POSITIVE_NUMBER"), ("/grid BTC-USDT-SWAP", "INVALID_SPOT_SYMBOL"),
                           ("/durum extra", "COMMAND_ARGUMENTS_REJECTED")):
            with self.subTest(text=text), self.assertRaisesRegex(TradingError, code):
                parse_command(text)


class CaptureAndDecisionTests(unittest.TestCase):
    def setUp(self):
        self.client, self.trader, self.core = make_system()
        self.addCleanup(self.trader.close)

    def test_capture_400_each_4h_and_real_provenance_not_fixture_label(self):
        report = self.core.market.capture("SUI-USDT")
        self.assertEqual(report["exchange"], "OKX_TR_SPOT")
        self.assertEqual(report["data_status"], "DATA_UNVERIFIED")
        self.assertFalse(self.core.market.real_transport)
        self.assertEqual(report["book"]["bid_levels"], 400)
        self.assertEqual(report["book"]["ask_levels"], 400)
        self.assertIn("return_4h", report["trend"])
        self.assertEqual(set(report["completed_bar_end_ms"]), {"15m", "1H", "4H"})
        self.assertEqual(report["market"]["quote_volume_24h"], "20000000")

    def test_shared_cached_snapshot_decision_and_fee_configuration(self):
        first = self.core.decision("SOL-USDT")
        self.client.calls.clear()
        second = self.core.decision("SOL-USDT")
        self.assertEqual(first, second)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(first["profile"]["name"], "v7-paper-600")
        self.assertEqual(first["risk_caps"], {"per_bot": "3.000", "total": "6.00"})
        self.assertTrue(first["technical_eligible"])
        self.assertFalse(first["eligible"])
        self.assertEqual(first["action"], "NO TRADE")

    def test_copy_mutation_does_not_change_cached_snapshot(self):
        first = self.core.market.capture("SOL-USDT")
        first["market"]["price"] = "999"
        self.assertFalse(self.core.market.authentic(first))
        self.assertEqual(self.core.market.capture("SOL-USDT")["market"]["price"], "100")

    def test_high_score_cannot_override_failed_mandatory_filter(self):
        result = score_checks({"data": True, "liquidity": True, "trend": True, "btc_regime": True,
                               "grid_economics": True, "planned_risk": False})
        self.assertEqual(result["score"], 85)
        self.assertEqual(result["passed"], 5)
        self.assertFalse(result["eligible"])

    def test_sol_9847_three_of_six_is_rejected_despite_display_score(self):
        report = self.core.market.capture("SOL-USDT")
        # Screenshot-class synthetic case; original V5 scoring code is unavailable.
        report["legacy_display_score"] = "98.47"
        report["market"]["quote_volume_24h"] = "1"
        report["trend"]["return_15m"] = "-0.01"
        report["trend"]["btc_return_1h"] = "-0.03"
        result = decide(report, load_profile("v7-paper-600"), PlanningPolicy(), now_ms=self.client.at)
        self.assertEqual(result["scoring"]["passed"], 3)
        self.assertEqual(result["action"], "NO TRADE")
        self.assertIn("BTC_RISK_OFF", result["reasons"])

    def test_pepe_radar_grid_have_same_decision_id_net_and_risk(self):
        scanned = next(r for r in self.core.radar()["decisions"] if r["instrument"] == "PEPE-USDT")
        detailed = self.core.decision("PEPE-USDT")
        for key in ("snapshot_id", "decision_id", "net_grid_returns", "planned_loss_usdt", "action", "reasons"):
            self.assertEqual(scanned[key], detailed[key])

    def test_exact_legacy_501_boundary_is_rejected_without_rounding_down(self):
        plan = GridPlan("PEPE-USDT", "99", "100", 2, "250", "97.996", "101", "0", "0", tick_size=".001")
        self.assertEqual(plan.planned_loss, Decimal("5.01"))
        market = Market.from_dict({**self.core.market.capture("PEPE-USDT")["market"], "price": "99.5"})
        with self.assertRaisesRegex(TradingError, "BOT_RISK_LIMIT"):
            plan.check(load_profile("v5-paper-500").policy, market, NOW // 1000)

    def test_unavailable_grid_values_render_unavailable_not_zero(self):
        report = self.core.market.capture("BTC-USDT")
        report["range"] = {"support": "99.99", "resistance": "100.01"}
        decision = decide(report, load_profile("v7-paper-600"), PlanningPolicy(), now_ms=self.client.at)
        self.assertIsNone(decision["plan"])
        self.assertIsNone(decision["planned_loss_usdt"])
        self.assertIn("HESAPLANAMADI", render_decision(decision))
        self.assertNotIn("risk 0.000", render_decision(decision))

    def test_bad_and_missing_candle_book_data_fail_closed(self):
        self.client.short_book = True
        with self.assertRaisesRegex(TradingError, "INCOMPLETE_400_LEVEL_BOOK"):
            self.core.market.capture("SOL-USDT")
        self.client.short_book = False
        self.client.gap_candles = True
        with self.assertRaisesRegex(TradingError, "INCOMPLETE_GRID_CANDLE_WINDOW"):
            self.core.market.capture("SOL-USDT")
        self.assertEqual(self.core.market.last_health["data_status"], "DATA_UNVERIFIED")

    def test_snapshot_refresh_and_network_error_never_return_cached_old_analysis(self):
        first = self.core.market.capture("SOL-USDT")
        self.client.advance(6)
        second = self.core.market.capture("SOL-USDT")
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.client.fail = "books"
        self.client.advance(6)
        with self.assertRaisesRegex(TradingError, "PUBLIC_HTTP_UNAVAILABLE"):
            self.core.market.capture("SOL-USDT")
        self.assertEqual(self.core.market.last_health["data_status"], "DATA_UNVERIFIED")
        self.assertFalse(self.core.market.authentic(first))

    def test_non_synthetic_profile_cannot_arm_with_mock_data(self):
        client, trader, core = make_system(synthetic=False)
        with trader, self.assertRaisesRegex(TradingError, "DATA_UNVERIFIED"):
            core.start(now=client.at // 1000)
        self.assertFalse(core.market.real_transport)

    def test_raw_trader_api_cannot_open_or_fill_without_verified_source(self):
        value = self.core.decision("SOL-USDT")
        report = self.core.market.capture("SOL-USDT")
        with V7PaperTrader(":memory:") as raw:
            with self.assertRaisesRegex(TradingError, "DATA_UNVERIFIED"):
                raw.open("one", build_plan(value["plan"]), Market.from_dict(report["market"]), NOW // 1000)
            report["data_status"] = "VERIFIED_TR_PUBLIC"
            with self.assertRaisesRegex(TradingError, "DATA_UNVERIFIED"):
                raw.process_book("one", report, now=NOW // 1000)

    def test_4h_public_contract_is_only_get_tr_candles(self):
        spec = request_spec("grid_candles", "SOL-USDT", bar="4H")
        self.assertEqual(spec, {"method": "GET", "origin": TR_ORIGIN, "path": "/api/v5/market/candles",
                                "params": {"instId": "SOL-USDT", "bar": "4H", "limit": "22"}})
        with self.assertRaises(TradingError):
            request_spec("grid_candles", "SOL-USDT-SWAP", bar="4H")

    def test_discovered_single_letter_spot_symbol_is_supported(self):
        self.client.names.append("X-USDT")
        self.assertEqual(self.core.decision("X-USDT")["instrument"], "X-USDT")


class PaperAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.client, self.trader, self.core = make_system()
        self.addCleanup(self.trader.close)

    def prepare(self, names=("SOL-USDT", "SUI-USDT")):
        for name in names:
            self.core.decision(name)
        self.core.start(now=self.client.at // 1000)
        return self.core.run_cycle(cycle_id="entry", now=self.client.at // 1000, notification_healthy=True)

    def book(self, price, *, quantity="1000", bot="one"):
        self.client.advance(price=price)
        self.client.quantity = quantity
        report = self.core.market.capture("SOL-USDT", refresh=True)
        return report, self.trader.process_book(bot, report, now=self.client.at // 1000)

    def open_one(self):
        d = self.core.decision("SOL-USDT")
        report = self.core.market.capture("SOL-USDT")
        self.trader.open("one", build_plan(d["plan"]), Market.from_dict(report["market"]), self.client.at // 1000)

    def assert_conservation(self, trader=None):
        p = (trader or self.trader).status()["portfolio"]
        self.assertEqual(Decimal(p["cash"]) + Decimal(p["inventory_cost"]), Decimal("600") + Decimal(p["realized_pnl"]))
        self.assertEqual(Decimal(p["equity"]), Decimal(p["cash"]) + Decimal(p["net_inventory_value"]))
        self.assertGreaterEqual(Decimal(p["unallocated_cash"]), 0)
        return p

    def test_two_300_bots_and_600_balance_atomic_reservations(self):
        result = self.prepare()
        self.assertEqual(len(result["opened"]), 2)
        p = self.assert_conservation()
        self.assertEqual(Decimal(p["reserved_capital"]), 600)
        self.assertEqual(Decimal(p["unallocated_cash"]), 0)
        self.assertLessEqual(Decimal(p["reserved_planned_risk"]), 6)
        self.assertTrue(all(Decimal(b["planned_stop_loss"]) <= 3 for b in result["state"]["bots"]))

    def test_automatic_cycle_idempotency_has_no_second_network_or_reservation(self):
        self.prepare()
        before = self.trader.status()
        self.client.calls.clear()
        result = self.core.run_cycle(cycle_id="entry", now=self.client.at // 1000, notification_healthy=True)
        self.assertTrue(result["duplicate"])
        self.assertEqual(before, self.trader.status())
        self.assertEqual(self.client.calls, [])

    def test_new_cycle_same_coin_does_not_create_another_bot(self):
        self.prepare(("SOL-USDT",))
        self.client.advance()
        result = self.core.run_cycle(cycle_id="second", now=self.client.at // 1000, notification_healthy=True)
        self.assertEqual(result["opened"], [])
        self.assertIn("DUPLICATE_INSTRUMENT", str(result["rejections"]))
        self.assertEqual(self.trader.status()["portfolio"]["active_bots"], 1)

    def test_partial_fill_and_nonfill_book_capacity_not_last_price_touch(self):
        self.open_one()
        _, nofill = self.book("100")
        self.assertEqual(nofill["fills"], 0)
        report, partial = self.book("99.99", quantity=".5")
        bot = partial["state"]["bots"][0]
        self.assertEqual(Decimal(bot["quantity"]), Decimal(".05"))
        self.assertGreater(Decimal(bot["orders"]["slots"][1]["remaining"]), 0)
        self.assertEqual(bot["orders"]["slots"][1]["side"], "BUY")
        before = self.trader.status()
        self.assertTrue(self.trader.process_book("one", report, now=self.client.at // 1000)["duplicate"])
        self.assertEqual(before, self.trader.status())
        self.assert_conservation()

    def test_gap_stop_can_exceed_3_and_releases_only_after_actual_simulated_exit(self):
        self.open_one()
        self.book("99.7")
        _, result = self.book("80")
        bot = result["state"]["bots"][0]
        self.assertEqual(bot["status"], "CLOSED")
        self.assertEqual(bot["reason"], "STOP_LOSS")
        self.assertLess(Decimal(bot["realized_pnl"]), -3)
        self.assertTrue(result["state"]["halted"])
        self.assertEqual(Decimal(self.assert_conservation()["reserved_capital"]), 0)

    def test_partial_stop_keeps_reservations_until_reconciled(self):
        self.open_one()
        self.book("99.7")
        _, result = self.book("99.6", quantity=".001")
        self.assertEqual(result["state"]["bots"][0]["status"], "OPEN")
        self.assertEqual(Decimal(self.assert_conservation()["reserved_capital"]), 300)
        self.assertEqual(result["state"]["bots"][0]["orders"]["exit_reason"], "STOP_LOSS")
        _, final = self.book("99.6", quantity="1000")
        self.assertEqual(final["state"]["bots"][0]["status"], "CLOSED")
        self.assertEqual(Decimal(self.assert_conservation()["reserved_capital"]), 0)

    def test_take_profit_gap_respects_finite_book_and_actual_accounting(self):
        self.open_one()
        self.book("99.7")
        _, final = self.book("101")
        self.assertEqual(final["state"]["bots"][0]["reason"], "TAKE_PROFIT")
        self.assertGreater(Decimal(self.assert_conservation()["realized_pnl"]), 0)

    def test_pause_cancel_buys_and_authorized_restart_restores_empty_slots(self):
        self.open_one()
        self.trader.pause()
        orders = self.trader.status()["bots"][0]["orders"]
        self.assertTrue(all(o["side"] == "CANCELLED" for o in orders["slots"]))
        self.core.start(now=self.client.at // 1000)
        self.assertTrue(all(o["side"] == "BUY" for o in self.trader.status()["bots"][0]["orders"]["slots"]))

    def test_connection_loss_pauses_and_reconnect_does_not_auto_resume(self):
        self.prepare(("SOL-USDT",))
        self.client.advance()
        self.client.fail = "books"
        result = self.core.run_cycle(cycle_id="fail", now=self.client.at // 1000, notification_healthy=True)
        self.assertTrue(result["state"]["paused"])
        self.assertEqual(result["data_errors"][0]["reason"], "PUBLIC_HTTP_UNAVAILABLE")
        self.client.fail = None
        self.client.advance()
        self.assertTrue(self.core.run_cycle(cycle_id="recover", now=self.client.at // 1000, notification_healthy=True)["state"]["paused"])

    def test_default_bad_notification_health_blocks_new_entry(self):
        self.core.decision("SOL-USDT")
        self.core.start(now=self.client.at // 1000)
        result = self.core.run_cycle(cycle_id="no_ack", now=self.client.at // 1000)
        self.assertEqual(result["opened"], [])
        self.assertTrue(result["state"]["paused"])

    def test_unknown_notification_blocks_start(self):
        queue_notice(self.trader, "uncertain", "synthetic", NOW // 1000)
        mark_notice(self.trader, "uncertain", "UNKNOWN", NOW // 1000)
        with self.assertRaisesRegex(TradingError, "NOTIFICATION_OUTCOME_UNKNOWN"):
            self.core.start(now=NOW // 1000)

    def test_order_corruption_detected(self):
        self.open_one()
        orders = self.trader._orders("one")
        orders["slots"][0]["remaining"] = "0"
        self.trader._set_meta("v7_orders_one", orders)
        with self.assertRaisesRegex(TradingError, "ORDER_RECONCILIATION_MISMATCH"):
            self.trader.status()

    def test_failure_rolls_back_fills_and_order_frame(self):
        self.open_one()
        before = self.trader.status()
        self.client.advance(price="99.7")
        report = self.core.market.capture("SOL-USDT", refresh=True)
        original = self.trader._event
        def injected(event_id, bot_id, kind, at, payload):
            if kind == "v7_order_frame":
                raise sqlite3.OperationalError("synthetic disk failure")
            original(event_id, bot_id, kind, at, payload)
        with patch.object(self.trader, "_event", side_effect=injected), self.assertRaisesRegex(TradingError, "STORAGE_ERROR"):
            self.trader.process_book("one", report, now=self.client.at // 1000)
        self.assertEqual(before, self.trader.status())
        self.assert_conservation()

    def test_stale_book_cannot_fill(self):
        self.open_one()
        report = self.core.market.capture("SOL-USDT")
        with self.assertRaisesRegex(TradingError, "STALE_OR_FUTURE_BOOK"):
            self.trader.process_book("one", report, now=NOW // 1000 + 31)

    def test_same_sequence_cannot_reuse_displayed_liquidity(self):
        self.open_one()
        self.book("99.99", quantity=".5")
        self.client.advance()
        self.client.sequence -= 1
        report = self.core.market.capture("SOL-USDT", refresh=True)
        before = self.trader.status()
        with self.assertRaisesRegex(TradingError, "REUSED_OR_OLD_BOOK_SEQUENCE"):
            self.trader.process_book("one", report, now=self.client.at // 1000)
        self.assertEqual(before, self.trader.status())

    def test_dust_stop_is_pending_not_a_fabricated_below_minimum_exit(self):
        self.open_one()
        self.book("99.99", quantity=".01")
        _, result = self.book("90", quantity="1000")
        bot = result["state"]["bots"][0]
        self.assertEqual(bot["status"], "OPEN")
        self.assertIn("EXIT_BELOW_MINIMUM_ORDER", str(bot["orders"]))
        self.assertEqual(Decimal(self.assert_conservation()["reserved_capital"]), 300)


class PersistenceAndConcurrencyTests(unittest.TestCase):
    def test_new_profile_does_not_adopt_v5_or_reset_it(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / "v5.sqlite3"
            with PaperTrader(old, profile=load_profile("v5-paper-500")) as v5:
                initial = v5.status()
            with self.assertRaisesRegex(TradingError, "POLICY_MISMATCH"):
                V7PaperTrader(old)
            with PaperTrader(old, profile=load_profile("v5-paper-500")) as v5:
                self.assertEqual(initial, v5.status())
            with V7PaperTrader(Path(d) / "v7.sqlite3") as v7:
                self.assertEqual(v7.status()["portfolio"]["initial_equity"], "600")

    def test_restart_after_partial_fill_and_persistent_emergency_stop(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "v7.sqlite3"
            client, trader, core = make_system(path=path)
            decision = core.decision("SOL-USDT")
            trader.open("one", build_plan(decision["plan"]), Market.from_dict(core.market.capture("SOL-USDT")["market"]), NOW // 1000)
            client.advance(price="99.99")
            client.quantity = ".5"
            report = core.market.capture("SOL-USDT", refresh=True)
            trader.process_book("one", report, now=client.at // 1000)
            trader.halt()
            before = trader.status()
            trader.close()
            with V7PaperTrader(path, synthetic=True) as reopened:
                self.assertEqual(before, reopened.status())
                with self.assertRaisesRegex(TradingError, "PAPER_HALTED"):
                    reopened.resume_entries()

    def test_synthetic_ledger_cannot_be_reopened_as_real_public_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "v7.sqlite3"
            V7PaperTrader(path, synthetic=True).close()
            with self.assertRaisesRegex(TradingError, "PAPER_DATA_MODE_MISMATCH"):
                V7PaperTrader(path)

    def test_concurrent_three_300_reservations_admit_only_two(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "v7.sqlite3"
            client, trader, core = make_system(path=path)
            decisions = [core.decision(n) for n in ("SOL-USDT", "SUI-USDT", "PEPE-USDT")]
            reports = [core.market.capture(d["instrument"]) for d in decisions]
            trader.close()
            def reserve(i):
                with V7PaperTrader(path, synthetic=True) as worker:
                    try:
                        worker.open("b" + str(i), build_plan(decisions[i]["plan"]), Market.from_dict(reports[i]["market"]), NOW // 1000)
                        return "OPEN"
                    except TradingError as error:
                        return error.code
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(reserve, range(3)))
            self.assertEqual(sorted(results), ["MAX_BOTS", "OPEN", "OPEN"])
            with V7PaperTrader(path, synthetic=True) as worker:
                self.assertEqual(Decimal(worker.status()["portfolio"]["reserved_capital"]), 600)


class UnifiedHookTests(unittest.TestCase):
    def setUp(self):
        self.client, self.trader, self.core = make_system()
        self.addCleanup(self.trader.close)

    def handle(self, text, uid=1):
        return handle_v7_update(self.core, update(text, uid, NOW // 1000 + uid - 1), allowed_chat=42, allowed_user=43, now=NOW // 1000 + uid - 1)

    def test_all_three_target_routes_share_plan_and_snapshot(self):
        results = [self.handle(t, i + 1)["value"] for i, t in enumerate(("/analiz SOL", "/GRID SOL/USDT", "/gridplan SOL 300"))]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_grid_sui_is_registered_and_unknown_symbol_is_not_help_fallback(self):
        self.assertTrue(self.handle("/GRID SUI")["ok"])
        rejected = self.handle("/grid NONEXISTENT", 2)
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["reason"], "UNKNOWN_SYMBOL")

    def test_duplicate_command_after_restart_does_not_fetch_or_repeat_action(self):
        first = self.handle("/grid SOL")
        self.client.calls.clear()
        duplicate = self.handle("/grid SOL")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["message"], duplicate["message"])
        self.assertEqual(self.client.calls, [])

    def test_acl_precedes_network_and_stop(self):
        bad = update("/emergency_stop")
        bad["message"]["from"]["id"] = 99
        with self.assertRaisesRegex(TradingError, "TELEGRAM_UNAUTHORIZED"):
            handle_v7_update(self.core, bad, allowed_chat=42, allowed_user=43, now=NOW // 1000)
        self.assertFalse(self.trader.status()["halted"])
        self.assertEqual(self.client.calls, [])

    def test_crash_after_accept_needs_new_update_and_does_not_fetch(self):
        handle_update(self.trader, update("/grid SOL"), allowed_chat=42, allowed_user=43, now=NOW // 1000)
        result = self.handle("/grid SOL")
        self.assertEqual(result["reason"], "COMMAND_OUTCOME_UNKNOWN_NEW_UPDATE_REQUIRED")
        self.assertEqual(self.client.calls, [])

    def test_status_stop_and_emergency_do_not_claim_v5_is_live(self):
        status = self.handle("/durum")
        self.assertIn("DATA_UNVERIFIED", status["message"])
        self.assertEqual(status["value"]["telegram"], "NOT_VERIFIED")
        self.assertTrue(self.handle("/papergrid stop", 2)["value"]["paper"]["paused"])
        self.assertTrue(self.handle("/emergency_stop", 3)["value"]["paper"]["halted"])
        self.assertEqual(status["delivery"], "NOT_SENT")

    def test_emergency_stop_bypasses_rate_limit_after_same_second_status(self):
        self.handle("/durum")
        result = handle_v7_update(self.core, update("/emergency_stop", uid=2), allowed_chat=42, allowed_user=43, now=NOW // 1000)
        self.assertTrue(result["value"]["paper"]["halted"])
        self.assertFalse(result["value"]["engine_armed"])

    def test_live_env_rejected_before_any_dispatch(self):
        with patch.dict("os.environ", {"LIVE_TRADING": "true"}), self.assertRaisesRegex(TradingError, "LIVE_TRADING_DISABLED"):
            self.handle("/papergrid start")
        self.assertEqual(self.client.calls, [])

    def test_momentum_and_flow_use_shared_risk_and_do_not_open_a_second_coin_exposure(self):
        from tests.test_money_flow import FakeFlowAPI
        from okx_grid.money_flow import MoneyFlowScanner
        from okx_grid.flow_contracts import FlowPolicy
        value = self.core.decision("SOL-USDT")
        self.trader.open("one", build_plan(value["plan"]), Market.from_dict(self.core.market.capture("SOL-USDT")["market"]), NOW // 1000)
        api = FakeFlowAPI(names=("BTC-USDT", "SOL-USDT"))
        self.core.flow = MoneyFlowScanner(client=api, policy=FlowPolicy(paper_notional="300"), clock_ms=lambda:NOW)
        first = self.handle("/momentum")
        self.assertTrue(first["ok"])
        scan = first["value"]
        sol = next(r for r in scan["risk_admission"] if r["coin"] == "SOL")
        self.assertEqual(sol["reason"], "CROSS_RADAR_EXPOSURE_CONFLICT")
        self.assertEqual(scan["execution_action"], "NO TRADE")
        self.assertEqual(scan["data_status"], "DATA_UNVERIFIED")
        self.assertTrue(all(c["leader"]["execution"]["notional_usdt"] == "300" for c in scan["coins"]))
        self.assertTrue(self.handle("/paraakisi", 2)["ok"])
        self.assertEqual(self.trader.status()["portfolio"]["active_bots"], 1)

    def test_v7_smoke_requires_opt_in_and_sanitizes_failure(self):
        from tools.v7_smoke import main
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            main([])
        output = io.StringIO()
        with patch("tools.v7_smoke.V7MarketData") as source, patch("sys.stdout", output):
            source.return_value.capture.side_effect = TradingError("PUBLIC_HTTP_UNAVAILABLE")
            self.assertEqual(main(["--allow-public-network"]), 2)
        self.assertIn('"data_status": "DATA_UNVERIFIED"', output.getvalue())
        self.assertNotIn("price", output.getvalue())

    def test_existing_v6_fee_model_default_is_preserved_and_v7_fee_is_explicit(self):
        from okx_grid.money_flow import MoneyFlowScanner
        from tests.test_money_flow import FakeFlowAPI
        self.assertEqual(MoneyFlowScanner().fee_rate, Decimal(".001"))
        planning = PlanningPolicy(fee_rate=".0005")
        api, trader, old_core = make_system()
        with trader:
            core = V7Core(trader, old_core.market)
            self.assertEqual(core.flow.policy.paper_notional, 300)
            self.assertEqual(core.flow.fee_rate, Decimal(core.planning.fee_rate))
        api = FakeFlowAPI()
        flow = MoneyFlowScanner(client=api, fee_rate=planning.fee_rate, clock_ms=lambda:NOW)
        scan = flow.scan_batch()
        self.assertTrue(all(Decimal(c["leader"]["execution"]["fee_each_side"]) == Decimal(".0005") for c in scan["coins"]))

    def test_other_budget_cannot_be_auto_reserved_by_600_profile(self):
        result = self.core.decision("SOL-USDT", "100")
        self.assertFalse(result["technical_eligible"])
        self.assertIn("PROFILE_REQUIRES_300_USDT_PER_BOT", result["reasons"])


if __name__ == "__main__":
    unittest.main()
