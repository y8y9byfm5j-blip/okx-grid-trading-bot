from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.domain import RiskPolicy, TradingError
from okx_grid.paper import PaperTrader
from okx_grid.scenarios import sample_market, sample_plan


class PaperScenarios(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(":memory:")
        self.trader.open("bot", sample_plan(), sample_market(), 1000)
        self.addCleanup(self.trader.close)

    def tick(self, tick_id, price, at=1001, **kw):
        return self.trader.tick("bot", tick_id, price, at, at, **kw)

    def bot(self, result=None):
        return (result or self.trader.status())["bots"][0]

    def test_opens_as_cash_without_invented_initial_inventory(self):
        self.assertEqual(self.bot()["cash"], "300")
        self.assertEqual(self.bot()["quantity"], "0")
        self.assertEqual(self.bot()["realized_pnl"], "0")

    def test_buy_uses_lot_size_and_includes_fees(self):
        bot = self.bot(self.tick("down", "99.9"))
        self.assertEqual(Decimal(bot["quantity"]), Decimal("0.749"))
        self.assertEqual(Decimal(bot["cash"]), Decimal("225.025100"))
        self.assertEqual(Decimal(bot["fees"]), Decimal("0.074900"))

    def test_one_completed_grid_has_independent_expected_profit(self):
        self.tick("down", "99.9")
        bot = self.bot(self.tick("up", "101", 1002))
        self.assertEqual(Decimal(bot["realized_pnl"]), Decimal("0.598451"))
        self.assertEqual(Decimal(bot["cash"]), Decimal("300.598451"))
        self.assertEqual(Decimal(bot["quantity"]), 0)

    def test_multiple_grid_crossings_have_expected_accounting(self):
        self.tick("down", "98")
        bot = self.bot(self.tick("up", "102", 1002))
        self.assertEqual(Decimal(bot["realized_pnl"]), Decimal("1.817499"))
        self.assertEqual(Decimal(bot["fees"]), Decimal("0.451501"))
        self.assertEqual(Decimal(bot["cash"]), Decimal("301.817499"))

    def test_same_price_does_not_churn_fees(self):
        self.tick("down", "99.9")
        before = self.bot()
        after = self.bot(self.tick("flat", "99.9", 1002))
        for key in ("cash", "fees", "quantity", "realized_pnl"):
            self.assertEqual(before[key], after[key])

    def test_unrealized_profit_includes_liquidation_costs(self):
        bot = self.bot(self.tick("down", "99.9"))
        self.assertLess(Decimal(bot["unrealized_pnl"]), 0)
        self.assertEqual(Decimal(bot["equity"]) - Decimal("300"), Decimal(bot["total_pnl"]))

    def test_gap_stop_exits_at_observed_price_and_adverse_slippage(self):
        self.tick("down", "98")
        bot = self.bot(self.tick("gap", "80", 1002))
        self.assertEqual(bot["reason"], "STOP_LOSS")
        self.assertEqual(bot["status"], "CLOSED")
        self.assertEqual(Decimal(bot["quantity"]), 0)
        self.assertLess(Decimal(bot["realized_pnl"]), -Decimal(bot["planned_stop_loss"]))
        prices = [Decimal(json.loads(r[0])["price"]) for r in self.trader._db.execute(
            "SELECT payload FROM events WHERE kind='fill' AND id LIKE '%gap%'")]
        self.assertTrue(prices)
        self.assertEqual(set(prices), {Decimal("79.920")})

    def test_stop_before_any_fill_does_not_buy_into_gap(self):
        bot = self.bot(self.tick("gap", "90"))
        self.assertEqual(bot["reason"], "STOP_LOSS")
        self.assertEqual(Decimal(bot["fees"]), 0)
        self.assertEqual(Decimal(bot["cash"]), 300)

    def test_take_profit_gap_does_not_improve_resting_limit_prices(self):
        self.tick("down", "98")
        bot = self.bot(self.tick("gapup", "105", 1002))
        self.assertEqual(bot["reason"], "TAKE_PROFIT")
        self.assertEqual(Decimal(bot["realized_pnl"]), Decimal("1.817499"))

    def test_manual_exit_releases_inventory(self):
        self.tick("down", "98")
        bot = self.bot(self.tick("exit", "98", 1002, force_close=True))
        self.assertEqual(bot["reason"], "MANUAL")
        self.assertEqual(Decimal(bot["quantity"]), 0)

    def test_halt_is_persistent_and_uses_next_valid_quote(self):
        self.tick("down", "98")
        self.trader.halt()
        self.assertEqual(self.bot()["status"], "OPEN")
        bot = self.bot(self.tick("halted", "97.5", 1002))
        self.assertEqual(bot["reason"], "HALTED")
        self.assertTrue(self.trader.status()["halted"])
        with self.assertRaisesRegex(TradingError, "PAPER_HALTED"):
            self.trader.open("new", sample_plan(), sample_market(1003), 1003)

    def test_duplicate_tick_is_idempotent(self):
        self.tick("first", "98")
        before = self.trader._db.execute("SELECT count(*) FROM events").fetchone()[0]
        result = self.trader.tick("bot", "first", "98.0", 1001, 2000)
        self.assertTrue(result["duplicate"])
        self.assertEqual(before, self.trader._db.execute("SELECT count(*) FROM events").fetchone()[0])

    def test_conflicting_duplicate_is_rejected(self):
        self.tick("first", "98")
        with self.assertRaisesRegex(TradingError, "TICK_ID_CONFLICT"):
            self.tick("first", "99")

    def test_tick_order_is_monotonic(self):
        self.tick("first", "98", 1002)
        for at in (1000, 1001, 1002):
            with self.subTest(at=at), self.assertRaisesRegex(TradingError, "OUT_OF_ORDER_TICK"):
                self.tick("other", "99", at)

    def test_stale_tick_is_rejected_without_mutation(self):
        before = self.trader.status()
        with self.assertRaisesRegex(TradingError, "STALE_TICK"):
            self.trader.tick("bot", "old", "98", 1001, 1032)
        self.assertEqual(before, self.trader.status())

    def test_future_tick_rejected(self):
        with self.assertRaisesRegex(TradingError, "FUTURE_TICK"):
            self.trader.tick("bot", "future", "98", 1010, 1000)

    def test_invalid_tick_price_rejected(self):
        for price in ("0", "-1", "NaN", "Infinity", 98.1):
            with self.subTest(price=price), self.assertRaises(TradingError):
                self.tick("bad", price)

    def test_closed_bot_never_reopens_on_a_tick(self):
        self.tick("close", "103")
        with self.assertRaisesRegex(TradingError, "BOT_CLOSED"):
            self.tick("again", "100", 1002)

    def test_partial_write_rolls_back_cash_fills_and_receipt(self):
        before = self.trader.status()
        original = self.trader._event
        def fail_on_receipt(event_id, bot_id, kind, at, payload):
            if kind == "tick":
                raise RuntimeError("injected crash")
            return original(event_id, bot_id, kind, at, payload)
        with patch.object(self.trader, "_event", side_effect=fail_on_receipt), self.assertRaises(RuntimeError):
            self.tick("crash", "98")
        self.assertEqual(before, self.trader.status())
        self.assertFalse(self.tick("crash", "98")["duplicate"])

    def test_state_tampering_fails_accounting_audit(self):
        state = self.trader._state("bot")
        state["cash"] = "301"
        self.trader._db.execute("UPDATE bots SET state=? WHERE id='bot'", (json.dumps(state),))
        with self.assertRaisesRegex(TradingError, "ACCOUNTING_MISMATCH"):
            self.trader.status()

    def test_plan_tampering_fails_hash_audit(self):
        state = self.trader._state("bot")
        state["plan"]["capital"] = "301"
        self.trader._save("bot", state)
        with self.assertRaisesRegex(TradingError, "ACCOUNTING_MISMATCH"):
            self.trader.status()

    def test_last_price_tampering_is_detected(self):
        state = self.trader._state("bot")
        state["last_price"] = "99"
        self.trader._save("bot", state)
        with self.assertRaisesRegex(TradingError, "ACCOUNTING_MISMATCH"):
            self.trader.status()

    def test_event_history_is_append_only(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.trader._db.execute("DELETE FROM events")
        with self.assertRaises(sqlite3.IntegrityError):
            self.trader._db.execute("UPDATE events SET payload='{}'")

    def test_missing_bot_is_safe_error(self):
        with self.assertRaisesRegex(TradingError, "BOT_NOT_FOUND"):
            self.trader.status("missing")

    def test_duplicate_open_rejected(self):
        with self.assertRaisesRegex(TradingError, "DUPLICATE_BOT"):
            self.trader.open("bot", sample_plan(), sample_market(), 1000)

    def test_random_tick_paths_preserve_accounting_and_nonnegative_cash(self):
        rng = random.Random(20261020)
        for index in range(150):
            price = str(Decimal(rng.randrange(9750, 10250)) / 100)
            bot = self.bot(self.tick("r" + str(index), price, 1001 + index))
            self.assertGreaterEqual(Decimal(bot["cash"]), 0)
            self.assertGreaterEqual(Decimal(bot["quantity"]), 0)
        self.tick("finish", "100", 1200, force_close=True)
        self.assertEqual(Decimal(self.bot()["quantity"]), 0)


class PortfolioAndStorage(unittest.TestCase):
    def test_portfolio_allocation_is_reserved(self):
        policy = RiskPolicy(max_allocation_fraction="0.2")
        with PaperTrader(":memory:", policy) as trader:
            trader.open("one", sample_plan(), sample_market(), 1000)
            with self.assertRaisesRegex(TradingError, "ALLOCATION_LIMIT"):
                trader.open("two", sample_plan(), sample_market(), 1000)

    def test_aggregate_risk_limit(self):
        policy = RiskPolicy(total_risk_fraction="0.01")
        with PaperTrader(":memory:", policy) as trader:
            trader.open("one", sample_plan(), sample_market(), 1000)
            with self.assertRaisesRegex(TradingError, "TOTAL_RISK_LIMIT"):
                trader.open("two", sample_plan(), sample_market(), 1000)

    def test_maximum_open_bots(self):
        with PaperTrader(":memory:", RiskPolicy(max_bots=1)) as trader:
            trader.open("one", sample_plan(), sample_market(), 1000)
            with self.assertRaisesRegex(TradingError, "MAX_BOTS"):
                trader.open("two", sample_plan(), sample_market(), 1000)

    def test_close_releases_reservation(self):
        with PaperTrader(":memory:", RiskPolicy(max_bots=1)) as trader:
            trader.open("one", sample_plan(), sample_market(), 1000)
            trader.tick("one", "tp", "103", 1001, 1001)
            trader.open("two", sample_plan(), sample_market(1002), 1002)
            self.assertEqual(len(trader.status()["bots"]), 2)

    def test_realized_loss_fuse_prevents_new_bots(self):
        with PaperTrader(":memory:", RiskPolicy(max_realized_loss_fraction="0.001")) as trader:
            trader.open("one", sample_plan(), sample_market(), 1000)
            trader.tick("one", "buy", "98", 1001, 1001)
            result = trader.tick("one", "stop", "96", 1002, 1002)
            self.assertTrue(result["halted"])
            with self.assertRaisesRegex(TradingError, "PAPER_HALTED"):
                trader.open("two", sample_plan(), sample_market(1003), 1003)

    def test_restart_preserves_cash_inventory_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            with PaperTrader(path) as trader:
                trader.open("one", sample_plan(), sample_market(), 1000)
                trader.tick("one", "buy", "98", 1001, 1001)
                before = trader.status()
            with PaperTrader(path) as trader:
                self.assertEqual(before, trader.status())
                self.assertTrue(trader.tick("one", "buy", "98", 1001, 1002)["duplicate"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_changed_policy_does_not_reset_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            PaperTrader(path).close()
            with self.assertRaisesRegex(TradingError, "POLICY_MISMATCH"):
                PaperTrader(path, RiskPolicy(equity="3000"))

    def test_corrupt_database_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            content = b"corrupt-but-important"
            path.write_bytes(content)
            with self.assertRaisesRegex(TradingError, "STORAGE_ERROR"):
                PaperTrader(path)
            self.assertEqual(path.read_bytes(), content)

    def test_unrelated_sqlite_database_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            database = sqlite3.connect(path)
            database.execute("CREATE TABLE legacy (value TEXT)")
            database.execute("INSERT INTO legacy VALUES ('keep')")
            database.commit()
            database.close()
            before = path.read_bytes()
            with self.assertRaisesRegex(TradingError, "UNSUPPORTED_STATE"):
                PaperTrader(path)
            self.assertEqual(before, path.read_bytes())

    def test_symlink_database_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            source, link = Path(directory) / "source.sqlite3", Path(directory) / "link.sqlite3"
            source.touch()
            link.symlink_to(source)
            with self.assertRaisesRegex(TradingError, "SYMLINK_STATE_PATH"):
                PaperTrader(link)

    def test_sqlite_extension_required(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(TradingError, "INVALID_STATE_PATH"):
            PaperTrader(Path(directory) / "paper.txt")

    def test_concurrent_open_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.sqlite3"
            PaperTrader(path).close()
            def attempt(_):
                try:
                    with PaperTrader(path) as trader:
                        trader.open("same", sample_plan(), sample_market(), 1000)
                    return "OPEN"
                except TradingError as error:
                    return error.code
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(attempt, range(2)))
            self.assertEqual(sorted(results), ["DUPLICATE_BOT", "OPEN"])
