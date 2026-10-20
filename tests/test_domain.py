from dataclasses import replace
from decimal import Decimal, getcontext
import unittest
from unittest.mock import patch

from okx_grid.domain import Market, RiskPolicy, TradingError, build_plan, number, paper_only, timestamp
from okx_grid.scenarios import sample_market, sample_plan
from tests.helpers import market_dict


class DecimalInputs(unittest.TestCase):
    def test_invalid_numbers_rejected(self):
        for value in (True, None, 0.1, "NaN", "Infinity", "-Infinity", "bad", "1e999999", "1e-19"):
            with self.subTest(value=value), self.assertRaises(TradingError):
                number(value)

    def test_decimal_strings_remain_exact(self):
        self.assertEqual(number("0.00000001"), Decimal("0.00000001"))

    def test_invalid_timestamps(self):
        for value in (-1, True, "1000", 1.0):
            with self.subTest(value=value), self.assertRaises(TradingError):
                timestamp(value)

    def test_live_mode_is_rejected(self):
        with patch.dict("os.environ", {"TRADING_MODE": "live"}), self.assertRaisesRegex(TradingError, "LIVE_TRADING_DISABLED"):
            paper_only()

    def test_live_flag_is_rejected(self):
        with patch.dict("os.environ", {"LIVE_TRADING": "true"}), self.assertRaises(TradingError):
            paper_only()


class MarketValidation(unittest.TestCase):
    def test_valid_order_book(self):
        self.assertEqual(sample_market().spread_bps, Decimal("2"))

    def test_order_book_structure(self):
        cases = [dict(bids=[]), dict(asks=[["100.01", "0"]]), dict(bids=[["99.99", "1", "extra"]]),
                 dict(bids=[["99.99", "1"], ["100", "1"]]), dict(asks=[["100.01", "1"], ["100", "1"]]),
                 dict(bid="100.01"), dict(asks=[["99", "1"]]), dict(quote_volume_24h="-1")]
        for update in cases:
            with self.subTest(update=update), self.assertRaises(TradingError):
                Market.from_dict(market_dict(**update))

    def test_direct_constructor_does_not_bypass_checks(self):
        with self.assertRaises(TradingError):
            replace(sample_market(), bids=())

    def test_missing_data(self):
        with self.assertRaisesRegex(TradingError, "INVALID_MARKET"):
            Market.from_dict({})

    def test_non_spot_symbol(self):
        for value in ("BTC-USDT-SWAP", "BTC-TRY", "../../etc", "BTC_USDT"):
            with self.subTest(value=value), self.assertRaises(TradingError):
                Market.from_dict(market_dict(instrument=value))

    def test_old_market(self):
        with self.assertRaisesRegex(TradingError, "STALE_MARKET"):
            RiskPolicy().check_market(sample_market(), 1031, Decimal("300"))

    def test_future_market(self):
        with self.assertRaisesRegex(TradingError, "FUTURE_MARKET"):
            RiskPolicy().check_market(sample_market(), 997, Decimal("300"))

    def test_low_quote_volume(self):
        with self.assertRaisesRegex(TradingError, "LOW_QUOTE_VOLUME"):
            RiskPolicy().check_market(replace(sample_market(), quote_volume_24h="9999999"), 1000, Decimal("300"))

    def test_spread_limit(self):
        market = Market.from_dict(market_dict(bid="99", ask="101", bids=[["99", "1000"]], asks=[["101", "1000"]]))
        with self.assertRaisesRegex(TradingError, "WIDE_SPREAD"):
            RiskPolicy().check_market(market, 1000, Decimal("300"))

    def test_depth_is_required_on_both_sides(self):
        market = Market.from_dict(market_dict(asks=[["100.01", "1"]]))
        with self.assertRaisesRegex(TradingError, "INSUFFICIENT_DEPTH"):
            RiskPolicy().check_market(market, 1000, Decimal("300"))

    def test_far_order_walls_do_not_count_as_near_depth(self):
        market = Market.from_dict(market_dict(bids=[["99.99", "1"], ["90", "1000000"]]))
        with self.assertRaisesRegex(TradingError, "INSUFFICIENT_DEPTH"):
            RiskPolicy().check_market(market, 1000, Decimal("300"))

    def test_last_price_divergence(self):
        with self.assertRaisesRegex(TradingError, "PRICE_BOOK_DIVERGENCE"):
            RiskPolicy().check_market(replace(sample_market(), price="110"), 1000, Decimal("300"))


class Plans(unittest.TestCase):
    def test_arithmetic_levels_and_lot_rounding(self):
        plan = sample_plan()
        self.assertEqual(plan.levels, tuple(map(Decimal, [98, 99, 100, 101, 102])))
        self.assertEqual(plan.quantity(Decimal("100")), Decimal("0.749"))

    def test_each_buy_includes_fees_in_budget(self):
        plan = sample_plan()
        cost = sum(plan.quantity(p) * p * (1 + plan.fee_rate) for p in plan.levels[:-1])
        self.assertLessEqual(cost, plan.capital)

    def test_invalid_plan_parameters(self):
        for update in (dict(lower="103"), dict(stop="99"), dict(take_profit="101"), dict(grids=True),
                       dict(grids=1), dict(grids=101), dict(capital="0"), dict(fee_rate="-1"), dict(slippage_rate="0.1")):
            with self.subTest(update=update), self.assertRaises(TradingError):
                sample_plan(**update)

    def test_tick_alignment(self):
        with self.assertRaisesRegex(TradingError, "UNALIGNED_PRICE"):
            sample_plan(lower="98.001")

    def test_grid_smaller_than_tick_rejected(self):
        with self.assertRaisesRegex(TradingError, "GRID_BELOW_TICK_SIZE"):
            sample_plan(grids=100, tick_size="1")

    def test_tiny_grid_notional_rejected(self):
        with self.assertRaisesRegex(TradingError, "GRID_BELOW_MIN_NOTIONAL"):
            sample_plan(capital="10")

    def test_fee_negative_profit_rejected(self):
        with self.assertRaisesRegex(TradingError, "GRID_NOT_PROFITABLE_AFTER_FEES"):
            sample_plan(fee_rate="0.01")

    def test_risk_includes_exit_costs(self):
        plan = sample_plan()
        simple = plan.capital * (plan.upper - plan.stop) / plan.upper
        self.assertGreater(plan.planned_loss, simple)
        self.assertLess(plan.planned_loss, Decimal("20"))

    def test_bot_risk_limit(self):
        with self.assertRaisesRegex(TradingError, "BOT_RISK_LIMIT"):
            sample_plan().check(RiskPolicy(equity="1000"), sample_market(), 1000)

    def test_bot_capital_limit(self):
        with self.assertRaisesRegex(TradingError, "BOT_CAPITAL_LIMIT"):
            sample_plan(capital="600").check(RiskPolicy(), sample_market(), 1000)

    def test_entry_at_boundary_rejected(self):
        for price in ("98", "102"):
            with self.subTest(price=price), self.assertRaises(TradingError):
                plan = sample_plan()
                plan.check(RiskPolicy(), replace(sample_market(), price=price), 1000)

    def test_hash_is_stable_across_json_roundtrip(self):
        plan = sample_plan()
        self.assertEqual(plan.digest, build_plan(plan.to_dict()).digest)
        self.assertNotEqual(plan.digest, sample_plan(capital="301").digest)

    def test_unknown_field_is_not_silently_ignored(self):
        with self.assertRaisesRegex(TradingError, "INVALID_PLAN"):
            build_plan({**sample_plan().to_dict(), "live": True})

    def test_invalid_risk_policy(self):
        for update in (dict(max_bots=True), dict(risk_fraction="2"), dict(max_allocation_fraction="1.1"),
                       dict(max_market_age=0), dict(max_future_seconds=30)):
            with self.subTest(update=update), self.assertRaises(TradingError):
                RiskPolicy(**update)

    def test_library_does_not_change_decimal_context(self):
        before = getcontext().prec
        sample_plan().planned_loss
        self.assertEqual(getcontext().prec, before)
