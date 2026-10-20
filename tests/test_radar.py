from copy import deepcopy
import unittest

from okx_grid.domain import RiskPolicy, TradingError
from okx_grid.radar import screen
from tests.helpers import proposal


class RadarScreening(unittest.TestCase):
    def reason(self, candidate):
        result = screen([candidate], RiskPolicy(), 1000)
        self.assertFalse(result["accepted"])
        return result["rejected"][0]["reason"]

    def test_valid_proposal_includes_complete_plan(self):
        result = screen([proposal()], RiskPolicy(), 1000)
        self.assertFalse(result["rejected"])
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["accepted"][0]["plan"]["grids"], 4)
        self.assertEqual(result["accepted"][0]["plan"]["capital"], "300")

    def test_downtrend_in_either_timeframe_is_rejected(self):
        for key in ("return_15m", "return_1h"):
            candidate = proposal()
            candidate["trend"][key] = "-0.001"
            self.assertEqual(self.reason(candidate), "DOWNTREND")

    def test_btc_risk_off(self):
        candidate = proposal()
        candidate["trend"]["btc_return_1h"] = "-0.02"
        self.assertEqual(self.reason(candidate), "BTC_RISK_OFF")

    def test_no_chasing_short_pump(self):
        candidate = proposal()
        candidate["trend"]["return_15m"] = "0.051"
        self.assertEqual(self.reason(candidate), "EXCESSIVE_SHORT_MOMENTUM")

    def test_stale_trend_is_not_masked_by_fresh_book(self):
        candidate = proposal()
        candidate["trend"]["observed_at"] = 900
        self.assertEqual(self.reason(candidate), "STALE_TREND")

    def test_missing_trend_is_rejected(self):
        candidate = proposal()
        del candidate["trend"]
        self.assertEqual(self.reason(candidate), "INVALID_RADAR_INPUT")

    def test_future_trend_rejected(self):
        candidate = proposal()
        candidate["trend"]["observed_at"] = 1010
        self.assertEqual(self.reason(candidate), "STALE_TREND")

    def test_invalid_candidate_does_not_stop_batch(self):
        result = screen([{}, proposal()], RiskPolicy(), 1000)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(len(result["rejected"]), 1)

    def test_duplicate_instrument_rejected(self):
        result = screen([proposal(), proposal()], RiskPolicy(), 1000)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["rejected"][0]["reason"], "DUPLICATE_INSTRUMENT")

    def test_batch_size_bounded(self):
        with self.assertRaisesRegex(TradingError, "INVALID_RADAR_INPUT"):
            screen([{}] * 501, RiskPolicy(), 1000)

    def test_candidates_are_ranked_by_spread_then_volume(self):
        first, second = proposal(), deepcopy(proposal())
        second["market"]["instrument"] = second["plan"]["instrument"] = "OTHER-USDT"
        second["market"]["quote_volume_24h"] = "30000000"
        result = screen([first, second], RiskPolicy(), 1000)
        self.assertEqual([r["instrument"] for r in result["accepted"]], ["OTHER-USDT", "TEST-USDT"])
