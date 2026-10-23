"""Offline regressions for source provenance, quote age and liquidity risk."""
from copy import deepcopy
from decimal import Decimal
import unittest
from unittest.mock import PropertyMock, patch

from okx_grid.domain import GridPlan, Market, TradingError
from okx_grid.market_data import TRMarketData
from okx_grid.money_flow import MoneyFlowScanner
from okx_grid.profiles import load_profile
from okx_grid.telegram_market import render
from okx_grid.v7_decision import PlanningPolicy, decide
from okx_grid.v7_market import V7MarketData
from tests.test_market_data import SyntheticTR
from tests.test_money_flow import FakeFlowAPI
from tests.test_public_contracts import NOW
from tests.test_v7 import SyntheticV7, make_system


class DepthRiskRegressions(unittest.TestCase):
    def test_liquidity_outside_reported_mid_band_cannot_pass_risk(self):
        market = Market("TEST-USDT", "100", "99.95", "100.05", "20000000", NOW // 1000,
                        (("99.95", ".01"), ("99.48", "1000")),
                        (("100.05", ".01"), ("100.52", "1000")))
        with self.assertRaisesRegex(TradingError, "INSUFFICIENT_DEPTH"):
            load_profile("v7-paper-600").policy.check_market(market, NOW // 1000, Decimal("300"))

    def test_exact_mid_band_edges_count_on_both_sides(self):
        market = Market("TEST-USDT", "100", "99.95", "100.05", "20000000", NOW // 1000,
                        (("99.95", ".01"), ("99.5", "100")),
                        (("100.05", ".01"), ("100.5", "100")))
        load_profile("v7-paper-600").policy.check_market(market, NOW // 1000, Decimal("300"))

    def test_screenshot_wide_irys_band_cannot_be_resized_into_eligibility(self):
        plan = GridPlan("IRYS-USDT", ".02708849", ".15712998", 10, "300", ".02668216", ".15791563",
                        ".001", ".0002", ".00000001", ".00000001", "5")
        market = Market("IRYS-USDT", ".12103", ".12102", ".12104", "20000000", NOW // 1000,
                        ((".12102", "1000000"),), ((".12104", "1000000"),))
        self.assertGreater(plan.planned_loss, Decimal("200"))
        with self.assertRaisesRegex(TradingError, "BOT_RISK_LIMIT"):
            plan.check(load_profile("v7-paper-600").policy, market, NOW // 1000)
        self.assertEqual(plan.capital, Decimal("300"))


class SourceAgeRegressions(unittest.TestCase):
    def setUp(self):
        self.api, self.trader, self.core = make_system()
        self.addCleanup(self.trader.close)

    def test_decision_rejects_future_book_or_stale_ticker_even_if_completion_is_fresh(self):
        original = self.core.market.capture("SOL-USDT")
        for key, at in (("book_ts_ms", NOW + 2001), ("ticker_ts_ms", NOW - 30001),
                        ("book_ts_ms", True), ("ticker_ts_ms", "invalid")):
            with self.subTest(key=key, at=at):
                report = deepcopy(original)
                report[key] = at
                result = decide(report, load_profile("v7-paper-600"), PlanningPolicy(), now_ms=NOW)
                self.assertFalse(result["scoring"]["checks"]["data"])
                self.assertFalse(result["technical_eligible"])

    def test_quote_source_age_rechecked_before_paper_gate(self):
        original = self.api.fetch
        def delayed(kind, instrument=None, **kwargs):
            value = original(kind, instrument, **kwargs)
            if kind in {"ticker", "books"}:
                value["data"][0]["ts"] = str(NOW - 29900)
            return value
        self.api.fetch = delayed
        report = self.core.market.capture("SOL-USDT")
        self.api.at += 101
        with self.assertRaisesRegex(TradingError, "STALE_PUBLIC_DATA"):
            self.core._data_gate(report)

    def test_status_does_not_arm_with_expired_source_or_future_clock(self):
        self.core.start(now=NOW // 1000)
        self.assertTrue(self.core.status()["engine_armed"])
        for delta in (-2001, 30001):
            with self.subTest(delta=delta):
                self.api.at = NOW + delta
                state = self.core.status()
                self.assertFalse(state["market"]["ok"])
                self.assertFalse(state["engine_armed"])

    def test_status_does_not_arm_after_candle_window_changes(self):
        self.api.at = (NOW // 900000 + 1) * 900000 - 1000
        self.core.start(now=self.api.at // 1000)
        self.api.advance(seconds=2)
        state = self.core.status()
        self.assertFalse(state["engine_armed"])
        self.assertEqual(state["market"]["source_reason"], "STALE_TREND_WINDOW")

    def test_cached_snapshot_refreshes_at_completed_candle_boundary(self):
        self.api.at = (NOW // 900000 + 1) * 900000 - 1000
        first = self.core.market.capture("SOL-USDT")
        self.api.advance(seconds=2)
        second = self.core.market.capture("SOL-USDT")
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(second["completed_bar_end_ms"]["15m"], self.api.at // 900000 * 900000)

    def test_decision_cannot_reuse_previous_completed_trend_window(self):
        self.api.at = (NOW // 900000 + 1) * 900000 - 1000
        report = self.core.market.capture("SOL-USDT")
        self.api.advance(seconds=2)
        result = decide(report, load_profile("v7-paper-600"), PlanningPolicy(), now_ms=self.api.at)
        self.assertFalse(result["scoring"]["checks"]["data"])

    def test_direct_paper_capture_gate_rechecks_quote_age(self):
        api, trader, core = make_system(synthetic=False)
        self.addCleanup(trader.close)
        original = api.fetch
        def delayed(kind, instrument=None, **kwargs):
            value = original(kind, instrument, **kwargs)
            if kind in {"ticker", "books"}:
                value["data"][0]["ts"] = str(NOW - 29900)
            return value
        api.fetch = delayed
        # A fixture remains offline: this patch only exercises the final guard.
        with patch.object(V7MarketData, "real_transport", new_callable=PropertyMock, return_value=True):
            report = core.market.capture("SOL-USDT")
            api.at += 101
            with self.assertRaisesRegex(TradingError, "DATA_UNVERIFIED"):
                trader._require_capture(report)


class RestAlignmentRegressions(unittest.TestCase):
    def test_v7_joins_exact_interleaved_quotes_inside_one_capture(self):
        api = SyntheticV7()
        policy = load_profile("v7-paper-600").policy
        original, tickers, books = api.fetch, 0, 0
        def interleaved(kind, instrument=None, **kwargs):
            nonlocal tickers, books
            value = original(kind, instrument, **kwargs)
            if kind == "ticker":
                tickers += 1
                if tickers != 1:
                    value["data"][0].update(bidPx="100.00", askPx="100.02")
            if kind == "books":
                books += 1
                if books == 1:
                    value["data"][0]["asks"][0][0] = "100.015"
            return value
        api.fetch = interleaved
        market = V7MarketData(policy, client=api, clock_ms=lambda: api.at)
        result = market.capture("SOL-USDT")
        self.assertEqual((tickers, books), (2, 2))
        self.assertEqual(result["market"]["bid"], "99.99")
        self.assertTrue(market.authentic(result))
        self.assertEqual(result["data_status"], "DATA_UNVERIFIED")


class LegacyProvenanceRegressions(unittest.TestCase):
    def test_v5_injected_data_is_never_labeled_live_verified(self):
        service = TRMarketData(load_profile("v5-paper-500").policy, client=SyntheticTR(), clock_ms=lambda: NOW)
        report = service.analyze("TEST-USDT")
        self.assertEqual(report.get("data_status"), "DATA_UNVERIFIED")
        text = render({"ok": True, "command": "/analiz TEST-USDT", "analysis": report})
        self.assertIn("DATA_UNVERIFIED", text)
        self.assertNotIn("CANLI PİYASA VERİSİ", text)

    def test_v6_injected_data_carries_unverified_provenance(self):
        scanner = MoneyFlowScanner(client=FakeFlowAPI(), clock_ms=lambda: NOW)
        result = scanner.scan_batch(limit=1, target="TEST-USDT")
        self.assertEqual(result.get("data_status"), "DATA_UNVERIFIED")
        self.assertEqual(result["pairs"]["TEST-USDT"].get("data_status"), "DATA_UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
