"""Regression tests for offline automatic paper entry; no OKX or Telegram calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.automation import consider_paper_entry
from okx_grid.domain import TradingError
from okx_grid.paper import PaperTrader
from okx_grid.profiles import load_profile
from okx_grid.scenarios import sample_plan
from tests.helpers import proposal


def candidate(symbol="TEST-USDT", *, at=1000):
    item = proposal()
    item["plan"] = sample_plan(instrument=symbol, capital="90").to_dict()
    item["market"]["instrument"] = symbol
    item["market"]["observed_at"] = at
    item["trend"]["observed_at"] = at
    return item


class AutomaticPaperEntry(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(":memory:", profile=load_profile("v5-paper-500"))
        self.addCleanup(self.trader.close)

    def decide(self, proposals=None, *, cycle_id="round_1", now=1000):
        return consider_paper_entry(self.trader, [candidate()] if proposals is None else proposals,
                                    cycle_id=cycle_id, now=now)

    def test_opens_one_grid_with_single_500_portfolio(self):
        result = self.decide()
        self.assertEqual(result["decision"], "OPENED")
        self.assertEqual(result["mode"], "paper")
        self.assertEqual(result["portfolio"]["initial_equity"], "500")
        self.assertEqual(result["portfolio"]["reserved_capital"], "90")
        self.assertEqual(result["portfolio"]["unallocated_cash"], "410")
        self.assertEqual(len(self.trader.status()["bots"]), 1)

    def test_replayed_decision_cannot_open_a_second_bot(self):
        first = self.decide()
        second = self.decide()
        self.assertEqual(second["decision"], "DUPLICATE_CYCLE")
        self.assertEqual(first["bot_id"], second["bot_id"])
        self.assertEqual(len(self.trader.status()["bots"]), 1)

    def test_cycle_key_collision_with_changed_instrument_is_rejected(self):
        self.decide()
        with self.assertRaisesRegex(TradingError, "AUTO_CYCLE_CONFLICT"):
            self.decide([candidate("ANOTHER-USDT")])
        self.assertEqual(len(self.trader.status()["bots"]), 1)

    def test_cycle_key_collision_with_changed_grid_is_rejected(self):
        self.decide()
        changed = candidate()
        changed["plan"]["capital"] = "80"
        with self.assertRaisesRegex(TradingError, "AUTO_CYCLE_CONFLICT"):
            self.decide([changed])

    def test_new_cycle_does_not_duplicate_active_symbol(self):
        self.decide()
        second = self.decide(cycle_id="round_2")
        self.assertEqual(second["decision"], "NO_ELIGIBLE_CANDIDATE")
        self.assertEqual(len(self.trader.status()["bots"]), 1)

    def test_two_symbols_open_in_two_cycles_with_shared_budget(self):
        self.decide()
        second = self.decide([candidate("ALT-USDT", at=1001)], cycle_id="round_2", now=1001)
        self.assertEqual(second["decision"], "OPENED")
        self.assertEqual(second["portfolio"]["reserved_capital"], "180")
        self.assertEqual(second["portfolio"]["unallocated_cash"], "320")
        self.assertEqual(second["portfolio"]["active_bots"], 2)

    def test_stale_and_downtrending_proposals_fail_closed(self):
        stale = self.decide([candidate(at=900)], now=1000)
        self.assertEqual(stale["decision"], "NO_ELIGIBLE_CANDIDATE")
        downtrend = candidate()
        downtrend["trend"]["return_15m"] = "-0.01"
        result = self.decide([downtrend])
        self.assertEqual(result["decision"], "NO_ELIGIBLE_CANDIDATE")
        self.assertEqual(len(self.trader.status()["bots"]), 0)

    def test_empty_feed_is_error_not_fabricated_quote(self):
        with self.assertRaisesRegex(TradingError, "AUTO_PROPOSALS_REQUIRED"):
            self.decide([])

    def test_missing_explicit_profile_is_rejected(self):
        with PaperTrader(":memory:") as trader:
            with self.assertRaisesRegex(TradingError, "AUTO_REQUIRES_EXPLICIT_500_PROFILE"):
                consider_paper_entry(trader, [candidate()], cycle_id="round_1", now=1000)

    def test_halted_account_does_not_open(self):
        self.trader.halt()
        result = self.decide()
        self.assertEqual(result["decision"], "BLOCKED")
        self.assertEqual(result["reason"], "PAPER_HALTED")

    def test_offline_never_uses_network(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            self.assertEqual(self.decide()["decision"], "OPENED")

    def test_live_environment_cannot_enable_orders(self):
        with patch.dict("os.environ", {"TRADING_MODE": "live"}):
            with self.assertRaisesRegex(TradingError, "LIVE_TRADING_DISABLED"):
                self.decide()

    def test_completed_cycle_is_still_deduplicated(self):
        first = self.decide()
        self.trader.tick(first["bot_id"], "tp", "103", 1001, 1001)
        again = self.decide()
        self.assertEqual(again["decision"], "DUPLICATE_CYCLE")
        self.assertEqual(len(self.trader.status()["bots"]), 1)

    def test_restart_keeps_cycle_deduplication(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.sqlite3"
            profile = load_profile("v5-paper-500")
            with PaperTrader(path, profile=profile) as trader:
                a = consider_paper_entry(trader, [candidate()], cycle_id="round_1", now=1000)
                self.assertEqual(a["decision"], "OPENED")
            with PaperTrader(path, profile=profile) as trader:
                b = consider_paper_entry(trader, [candidate()], cycle_id="round_1", now=1000)
                self.assertEqual(b["decision"], "DUPLICATE_CYCLE")
                self.assertEqual(len(trader.status()["bots"]), 1)


if __name__ == "__main__":
    unittest.main()
