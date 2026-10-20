from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.domain import GridPlan, Market, RiskPolicy, TradingError
from okx_grid.paper import PaperTrader
from okx_grid.profiles import load_profile, load_profile_file
from okx_grid.scenarios import sample_market, sample_plan
from tests.helpers import market_dict


def boundary_plan(capital="250", stop="98"):
    return GridPlan("TEST-USDT", "99", "100", 4, capital, stop, "101", fee_rate="0", slippage_rate="0")


def boundary_market(at=1000):
    return Market.from_dict(market_dict(price="99.5", bid="99.49", ask="99.51", observed_at=at,
                                       bids=[["99.49", "1000"]], asks=[["99.51", "1000"]]))


class ProfileContract(unittest.TestCase):
    def test_explicit_profile_leaves_legacy_defaults_unchanged(self):
        profile = load_profile("v5-paper-500")
        self.assertEqual(RiskPolicy().equity, 2000)
        self.assertEqual(profile.policy.equity, 500)
        self.assertEqual(profile.policy.equity * profile.policy.risk_fraction, 5)
        self.assertEqual(profile.policy.equity * profile.policy.total_risk_fraction, 15)
        self.assertEqual(profile.leverage, "1")
        self.assertEqual(profile.mode, "paper")

    def test_unknown_name_does_not_fall_back(self):
        with self.assertRaisesRegex(TradingError, "UNKNOWN_PAPER_PROFILE"):
            load_profile("v5-paper-500-typo")

    def test_incomplete_custom_profile_does_not_use_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "profile.toml"
            file.write_text('name="v5-paper-500"\n[policy]\nequity="500"\n')
            with self.assertRaisesRegex(TradingError, "INCOMPLETE_PAPER_PROFILE"):
                load_profile_file(file)

    def test_custom_profile_is_visible_and_semantic_drift_changes_digest(self):
        profile = load_profile("v5-paper-500")
        modified = replace(profile, policy=replace(profile.policy, max_spread_bps="9"))
        self.assertNotEqual(profile.identity, modified.identity)
        self.assertEqual(profile.identity, load_profile("v5-paper-500").identity)

    def test_non_paper_or_leverage_profile_rejected(self):
        for updates in (dict(mode="live"), dict(leverage="2"), dict(revision=True)):
            with self.subTest(updates=updates), self.assertRaises(TradingError):
                replace(load_profile("v5-paper-500"), **updates)

    def test_persistent_profile_drift_is_rejected(self):
        profile = load_profile("v5-paper-500")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.sqlite3"
            PaperTrader(path, profile=profile).close()
            changed = replace(profile, revision=2)
            with self.assertRaisesRegex(TradingError, "PROFILE_MISMATCH"):
                PaperTrader(path, profile=changed)
            with self.assertRaisesRegex(TradingError, "POLICY_MISMATCH"):
                PaperTrader(path, profile=replace(profile, policy=replace(profile.policy, equity="501")))

    def test_same_policy_without_named_profile_does_not_bypass_identity(self):
        profile = load_profile("v5-paper-500")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.sqlite3"
            PaperTrader(path, profile=profile).close()
            with self.assertRaisesRegex(TradingError, "PROFILE_MISMATCH"):
                PaperTrader(path, profile.policy)

    def test_profile_cannot_silently_adopt_legacy_ledger(self):
        profile = load_profile("v5-paper-500")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            PaperTrader(path, profile.policy).close()
            with self.assertRaisesRegex(TradingError, "PROFILE_MISMATCH"):
                PaperTrader(path, profile=profile)

    def test_in_process_metadata_drift_fails_before_mutation(self):
        with PaperTrader(":memory:", profile=load_profile("v5-paper-500")) as trader:
            trader._set_meta("profile", {"name": "changed"})
            with self.assertRaisesRegex(TradingError, "PROFILE_MISMATCH"):
                trader.open("one", boundary_plan(), boundary_market(), 1000)
            self.assertEqual(trader._db.execute("SELECT count(*) FROM bots").fetchone()[0], 0)


class FiveHundredPortfolio(unittest.TestCase):
    def setUp(self):
        self.profile = load_profile("v5-paper-500")
        self.trader = PaperTrader(":memory:", profile=self.profile)
        self.addCleanup(self.trader.close)

    def assert_conservation(self, trader=None):
        portfolio = (trader or self.trader).status()["portfolio"]
        self.assertEqual(Decimal(portfolio["cash"]) + Decimal(portfolio["inventory_cost"]),
                         Decimal("500") + Decimal(portfolio["realized_pnl"]))
        self.assertEqual(Decimal(portfolio["cash"]) + Decimal(portfolio["net_inventory_value"]),
                         Decimal(portfolio["equity"]))
        self.assertGreaterEqual(Decimal(portfolio["unallocated_cash"]), 0)
        return portfolio

    def test_exact_five_dollar_risk_boundary_and_just_above(self):
        plan = boundary_plan()
        self.assertEqual(plan.planned_loss, 5)
        self.trader.open("at-limit", plan, boundary_market(), 1000)
        with self.assertRaisesRegex(TradingError, "BOT_RISK_LIMIT"):
            self.trader.open("over-limit", boundary_plan("250.000001"), boundary_market(), 1000)

    def test_three_bots_share_one_five_hundred_balance(self):
        plan = sample_plan(capital="166.66", upper="100.50", stop="97.99")
        for name in ("one", "two", "three"):
            self.trader.open(name, plan, sample_market(), 1000)
        portfolio = self.assert_conservation()
        self.assertEqual(Decimal(portfolio["initial_equity"]), 500)
        self.assertEqual(Decimal(portfolio["equity"]), 500)
        self.assertEqual(Decimal(portfolio["reserved_capital"]), Decimal("499.98"))
        self.assertEqual(Decimal(portfolio["unallocated_cash"]), Decimal("0.02"))
        self.assertLessEqual(Decimal(portfolio["reserved_planned_risk"]), 15)
        self.assertEqual(self.trader.status("one")["portfolio"], portfolio)
        with self.assertRaisesRegex(TradingError, "MAX_BOTS"):
            self.trader.open("four", plan, sample_market(), 1000)

    def test_two_250_bots_exhaust_cash_without_double_counting(self):
        for name in ("one", "two"):
            self.trader.open(name, boundary_plan(), boundary_market(), 1000)
        self.assertEqual(Decimal(self.assert_conservation()["unallocated_cash"]), 0)
        with self.assertRaisesRegex(TradingError, "ALLOCATION_LIMIT"):
            self.trader.open("three", boundary_plan("40"), boundary_market(), 1000)

    def test_three_bots_exact_fifteen_dollar_total_risk(self):
        plan = GridPlan("TEST-USDT", "98", "100", 4, "100", "95", "101", fee_rate="0", slippage_rate="0")
        for name in ("one", "two", "three"):
            self.trader.open(name, plan, boundary_market(), 1000)
        self.assertEqual(Decimal(self.assert_conservation()["reserved_planned_risk"]), 15)
        custom = replace(self.profile, policy=replace(self.profile.policy, max_bots=4))
        with PaperTrader(":memory:", profile=custom) as trader:
            for name in ("one", "two", "three"):
                trader.open(name, plan, boundary_market(), 1000)
            with self.assertRaisesRegex(TradingError, "TOTAL_RISK_LIMIT"):
                trader.open("four", plan, boundary_market(), 1000)

    def test_entry_exit_fees_and_global_cash_have_independent_expected_values(self):
        self.trader.open("one", sample_plan(capital="90"), sample_market(), 1000)
        self.trader.tick("one", "buy", "99.9", 1001, 1001)
        portfolio = self.assert_conservation()
        self.assertEqual(Decimal(portfolio["cash"]), Decimal("477.5776"))
        self.assertEqual(Decimal(portfolio["inventory_cost"]), Decimal("22.4224"))
        self.trader.tick("one", "sell", "101", 1002, 1002)
        portfolio = self.assert_conservation()
        self.assertEqual(Decimal(portfolio["equity"]), Decimal("500.178976"))
        self.assertEqual(Decimal(portfolio["fees"]), Decimal("0.045024"))

    def test_gap_stop_loss_is_not_capped_to_planned_five_dollars(self):
        plan = sample_plan(capital="90")
        self.trader.open("one", plan, sample_market(), 1000)
        self.trader.tick("one", "buy", "98", 1001, 1001)
        result = self.trader.tick("one", "gap", "80", 1002, 1002)
        portfolio = self.assert_conservation()
        self.assertLess(Decimal(portfolio["realized_pnl"]), -5)
        self.assertEqual(result["bots"][0]["reason"], "STOP_LOSS")
        self.assertTrue(result["halted"])
        self.assertEqual(Decimal(portfolio["reserved_capital"]), 0)

    def test_tp_gap_keeps_resting_limit_prices_and_does_not_add_capital_twice(self):
        self.trader.open("one", sample_plan(capital="90"), sample_market(), 1000)
        self.trader.tick("one", "buy", "98", 1001, 1001)
        self.trader.tick("one", "tp", "105", 1002, 1002)
        portfolio = self.assert_conservation()
        expected = Decimal("500") + Decimal(self.trader.status()["bots"][0]["realized_pnl"])
        self.assertEqual(Decimal(portfolio["equity"]), expected)
        self.assertEqual(Decimal(portfolio["unallocated_cash"]), expected)
        self.assertEqual(Decimal(portfolio["inventory_cost"]), 0)

    def test_rollbacks_preserve_global_funds_and_reservations(self):
        self.trader.open("one", sample_plan(capital="90"), sample_market(), 1000)
        before = self.trader.status()
        original = self.trader._event
        def fail(event_id, bot, kind, at, payload):
            if kind == "tick":
                raise sqlite3.OperationalError("injected failure")
            original(event_id, bot, kind, at, payload)
        with patch.object(self.trader, "_event", side_effect=fail), self.assertRaisesRegex(TradingError, "STORAGE_ERROR"):
            self.trader.tick("one", "buy", "98", 1001, 1001)
        self.assertEqual(before, self.trader.status())
        self.assert_conservation()

    def test_restart_duplicate_conflict_and_corrupt_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.sqlite3"
            with PaperTrader(path, profile=self.profile) as trader:
                trader.open("one", sample_plan(capital="90"), sample_market(), 1000)
                trader.tick("one", "buy", "98", 1001, 1001)
                before = trader.status()
            with PaperTrader(path, profile=self.profile) as trader:
                self.assertEqual(before, trader.status())
                self.assertTrue(trader.tick("one", "buy", "98", 1001, 1100)["duplicate"])
                with self.assertRaisesRegex(TradingError, "TICK_ID_CONFLICT"):
                    trader.tick("one", "buy", "99", 1001, 1001)
                state = trader._state("one")
                state["cash"] = "NaN"
                trader._save("one", state)
                with self.assertRaises(TradingError):
                    trader.status()

    def test_concurrent_reservation_cannot_spend_the_same_500_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.sqlite3"
            PaperTrader(path, profile=self.profile).close()
            def attempt(index):
                try:
                    with PaperTrader(path, profile=self.profile) as trader:
                        trader.open("bot" + str(index), boundary_plan(), boundary_market(), 1000)
                    return "OPEN"
                except TradingError as error:
                    return error.code
            with ThreadPoolExecutor(max_workers=3) as executor:
                result = list(executor.map(attempt, range(3)))
            self.assertEqual(sorted(result), ["ALLOCATION_LIMIT", "OPEN", "OPEN"])
            with PaperTrader(path, profile=self.profile) as trader:
                portfolio = self.assert_conservation(trader)
                self.assertEqual(Decimal(portfolio["reserved_capital"]), 500)

    def test_zero_negative_parameters_remain_rejected_in_500_profile(self):
        for values in (dict(capital="0"), dict(capital="-1"), dict(fee_rate="-0.001"),
                       dict(lot_size="0"), dict(tick_size="0"), dict(min_notional="-1")):
            with self.subTest(values=values), self.assertRaises(TradingError):
                sample_plan(**({"capital": "90"} | values))
