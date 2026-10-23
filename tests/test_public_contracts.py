from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import unittest
from unittest.mock import patch

from okx_grid.domain import TradingError
from okx_grid.profiles import load_profile
from okx_grid.public_contracts import (TR_ORIGIN, make_trend, normalize_market, normalize_return,
                                       request_spec, retry_delay)
from okx_grid.radar import screen
from okx_grid.scenarios import sample_plan

NOW = 1800000012000


def envelope(*rows):
    return {"code": "0", "msg": "", "data": list(rows)}


def public_fixture():
    instruments = envelope({"instId": "TEST-USDT", "instType": "SPOT", "state": "live",
                            "baseCcy": "TEST", "quoteCcy": "USDT", "tickSz": "0.01", "lotSz": "0.001", "minSz": "0.001"})
    ticker = envelope({"instId": "TEST-USDT", "instType": "SPOT", "last": "100", "bidPx": "99.99", "askPx": "100.01",
                       "volCcy24h": "20000000", "vol24h": "999999999", "ts": str(NOW - 125)})
    books = envelope({"seqId": 10, "ts": str(NOW - 100), "bids": [["99.99", "1000", "0", "1"]],
                      "asks": [["100.01", "1000", "0", "1"]]})
    return [instruments, ticker, books]


def candle_fixture(bar, opening="100", close="101", incomplete=True):
    period = {"15m": 900000, "1H": 3600000}[bar]
    current = NOW // period * period
    rows = []
    if incomplete:
        rows.append([str(current), "100", "102", "99", "100", "1", "1", "100", "0"])
    rows.append([str(current - period), opening, "102", "90", close, "10", "10", "1000", "1"])
    return envelope(*rows)


class PublicRequestsAndBackoff(unittest.TestCase):
    def test_only_known_public_get_paths_and_tr_host(self):
        for kind in ("instruments", "ticker", "books", "candles"):
            spec = request_spec(kind, "BTC-USDT", **({"bar": "15m"} if kind == "candles" else {}))
            self.assertEqual(spec["method"], "GET")
            self.assertEqual(spec["origin"], TR_ORIGIN)
            self.assertFalse(any("ACCESS" in str(k) for k in spec))
        for kind in ("/api/v5/trade/order", "account", "https://other.example", "POST", "ws-books"):
            with self.subTest(kind=kind), self.assertRaises(TradingError):
                request_spec(kind, "BTC-USDT")

    def test_candle_request_allows_only_explicit_15m_or_1h(self):
        for bar in ("15m", "1H"):
            self.assertEqual(request_spec("candles", "BTC-USDT", bar=bar)["params"]["bar"], bar)
        for bar in (None, "1m", "1h"):
            with self.subTest(bar=bar), self.assertRaises(TradingError):
                request_spec("candles", "BTC-USDT", bar=bar)

    def test_retry_backoff_is_bounded_and_respects_retry_after(self):
        self.assertEqual([retry_delay(a, status=429) for a in range(3)], [Decimal(1), Decimal(2), Decimal(4)])
        self.assertEqual(retry_delay(0, status=429, retry_after="15"), Decimal(15))
        self.assertEqual(retry_delay(0, status=200, api_code="50011"), Decimal(1))
        self.assertEqual(retry_delay(1, status=0), Decimal(2))
        self.assertIsNone(retry_delay(0, status=403))
        with self.assertRaisesRegex(TradingError, "PUBLIC_RETRY_EXHAUSTED"):
            retry_delay(3, status=429)
        with self.assertRaisesRegex(TradingError, "PUBLIC_COOLDOWN_REQUIRED"):
            retry_delay(0, status=429, retry_after="31")


class RestMarketContracts(unittest.TestCase):
    def setUp(self):
        self.policy = load_profile("v5-paper-500").policy

    def normalize(self, replies=None, **changes):
        arguments = {"source_origin": TR_ORIGIN, "now_ms": NOW, "policy": self.policy} | changes
        return normalize_market("TEST-USDT", *(replies or public_fixture()), **arguments)

    def test_spot_quote_volume_is_not_base_volume_or_multiplied_again(self):
        capture = self.normalize()
        self.assertEqual(capture.market.quote_volume_24h, Decimal("20000000"))
        self.assertEqual(capture.market.observed_at, (NOW - 125) // 1000)
        self.assertEqual(capture.market.spread_bps, 2)
        self.assertEqual(capture.rules.min_size, Decimal("0.001"))

    def test_unknown_region_and_unavailable_pair_are_rejected(self):
        with self.assertRaisesRegex(TradingError, "UNVERIFIED_TR_ORIGIN"):
            self.normalize(source_origin="https://www.okx.com")
        replies = public_fixture()
        replies[0]["data"][0]["instId"] = "OTHER-USDT"
        with self.assertRaisesRegex(TradingError, "INSTRUMENT_NOT_AVAILABLE_IN_TR_CAPTURE"):
            self.normalize(replies)

    def test_suspended_derivative_or_wrong_quote_is_not_spot_eligibility(self):
        for field, value in (("state", "suspend"), ("instType", "SWAP"), ("quoteCcy", "TRY")):
            replies = public_fixture()
            replies[0]["data"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(TradingError, "INSTRUMENT_NOT_LIVE_SPOT"):
                self.normalize(replies)

    def test_ticker_symbol_mismatch_rejected(self):
        replies = public_fixture()
        replies[1]["data"][0]["instId"] = "OTHER-USDT"
        with self.assertRaisesRegex(TradingError, "PUBLIC_INSTRUMENT_MISMATCH"):
            self.normalize(replies)

    def test_freshness_checked_in_milliseconds_before_flooring(self):
        for reply_index in (1, 2):
            replies = public_fixture()
            replies[reply_index]["data"][0]["ts"] = str(NOW - 30001)
            with self.subTest(reply_index=reply_index), self.assertRaisesRegex(TradingError, "STALE_PUBLIC_DATA"):
                self.normalize(replies)

    def test_seconds_mislabelled_as_milliseconds_are_rejected_as_stale(self):
        replies = public_fixture()
        for index in (1, 2):
            replies[index]["data"][0]["ts"] = str(NOW // 1000)
        with self.assertRaisesRegex(TradingError, "STALE_PUBLIC_DATA"):
            self.normalize(replies)

    def test_future_and_incoherent_timestamps_rejected(self):
        replies = public_fixture()
        replies[2]["data"][0]["ts"] = str(NOW + 2001)
        with self.assertRaisesRegex(TradingError, "FUTURE_PUBLIC_DATA"):
            self.normalize(replies)
        replies[2]["data"][0]["ts"] = str(NOW - 5000)
        with self.assertRaisesRegex(TradingError, "INCOHERENT_PUBLIC_SNAPSHOTS"):
            self.normalize(replies)

    def test_empty_zero_crossed_unsorted_and_mismatched_books_rejected(self):
        updates = [{"bids": []}, {"bids": [["99.99", "0", "0", "1"]]},
                   {"bids": [["100.01", "1", "0", "1"]]},
                   {"bids": [["99.99", "1", "0", "1"], ["100", "1", "0", "1"]]},
                   {"asks": [["100.02", "1", "0", "1"]]}]
        for update in updates:
            replies = public_fixture()
            replies[2]["data"][0].update(update)
            with self.subTest(update=update), self.assertRaises(TradingError):
                self.normalize(replies)

    def test_top_of_book_mismatch_is_diagnostic_and_still_rejected(self):
        replies = public_fixture()
        replies[1]['data'][0]['bidPx'] = '99.98'
        with self.assertRaisesRegex(TradingError, 'INCOHERENT_PUBLIC_TOP_OF_BOOK'):
            self.normalize(replies)

    def test_sequence_duplicate_conflict_regression_and_snapshot_gap(self):
        first = self.normalize()
        self.assertTrue(self.normalize(previous=first.cursor).duplicate_book)
        replies = public_fixture()
        replies[2]["data"][0]["bids"][0][1] = "2000"
        with self.assertRaisesRegex(TradingError, "CONFLICTING_BOOK_SEQUENCE"):
            self.normalize(replies, previous=first.cursor)
        replies = public_fixture()
        replies[2]["data"][0]["seqId"] = 9
        with self.assertRaisesRegex(TradingError, "OUT_OF_ORDER_BOOK"):
            self.normalize(replies, previous=first.cursor)
        replies[2]["data"][0]["seqId"] = 100
        self.assertFalse(self.normalize(replies, previous=first.cursor).duplicate_book)

    def test_plan_must_match_tick_lot_and_base_minimum_rules(self):
        capture = self.normalize()
        plan = sample_plan(capital="90")
        capture.rules.check_plan(plan)
        with self.assertRaisesRegex(TradingError, "INSTRUMENT_RULE_MISMATCH"):
            replace(capture.rules, min_size=Decimal("10")).check_plan(plan)
        with self.assertRaisesRegex(TradingError, "INSTRUMENT_RULE_MISMATCH"):
            replace(capture.rules, tick_size=Decimal("0.1")).check_plan(plan)

    def test_normalization_does_not_bypass_capital_dependent_near_depth_check(self):
        replies = public_fixture()
        replies[2]["data"][0]["bids"] = [["99.99", "1", "0", "1"], ["90", "1000000", "0", "1"]]
        capture = self.normalize(replies)
        with self.assertRaisesRegex(TradingError, "INSUFFICIENT_DEPTH"):
            sample_plan(capital="90").check(self.policy, capture.market, NOW // 1000)

    def test_api_error_empty_data_and_missing_quote_volume_fail_closed(self):
        for reply in ({"code": "50011", "msg": "untrusted", "data": []}, envelope(), {"code": "0", "data": None}):
            replies = public_fixture()
            replies[1] = reply
            with self.subTest(reply=reply), self.assertRaises(TradingError):
                self.normalize(replies)
        replies = public_fixture()
        del replies[1]["data"][0]["volCcy24h"]
        with self.assertRaises(TradingError):
            self.normalize(replies)


class CandlesAndBtcRegime(unittest.TestCase):
    def normalize(self, instrument="TEST-USDT", bar="15m", reply=None, **updates):
        return normalize_return(instrument, bar, reply or candle_fixture(bar),
                                **({"source_origin": TR_ORIGIN, "now_ms": NOW} | updates))

    def test_completed_bar_used_instead_of_incomplete_front_row(self):
        window = self.normalize()
        self.assertEqual(window.value, Decimal("0.01"))
        self.assertEqual(window.end_ms, NOW // 900000 * 900000)

    def test_15m_1h_and_btc_are_separate_verified_windows(self):
        policy = load_profile("v5-paper-500").policy
        trend = make_trend(self.normalize(), self.normalize(bar="1H"), self.normalize("BTC-USDT", "1H"), now_ms=NOW, policy=policy)
        self.assertEqual(trend["return_15m"], "0.010000000000")
        self.assertEqual(trend["btc_return_1h"], "0.010000000000")
        self.assertEqual(trend["observed_at"], NOW // 1000)

    def test_completed_window_staleness_and_missing_completed_candle(self):
        replies = candle_fixture("15m")
        replies["data"].pop()
        with self.assertRaisesRegex(TradingError, "STALE_OR_MISSING_COMPLETED_CANDLE"):
            self.normalize(reply=replies)
        replies = candle_fixture("15m", incomplete=False)
        replies["data"][0][0] = str(int(replies["data"][0][0]) - 900000)
        with self.assertRaisesRegex(TradingError, "STALE_OR_MISSING_COMPLETED_CANDLE"):
            self.normalize(reply=replies)

    def test_repeated_or_out_of_order_candles_rejected(self):
        reply = candle_fixture("15m")
        reply["data"].append(deepcopy(reply["data"][-1]))
        with self.assertRaisesRegex(TradingError, "OUT_OF_ORDER_CANDLE"):
            self.normalize(reply=reply)

    def test_unconfirmed_old_candle_or_invalid_ohlc_rejected(self):
        for position, value in ((8, "0"), (2, "99"), (5, "-1"), (8, [])):
            reply = candle_fixture("15m")
            reply["data"][-1][position] = value
            with self.subTest(position=position), self.assertRaises(TradingError):
                self.normalize(reply=reply)

    def test_reusing_windows_across_time_or_wrong_btc_is_rejected(self):
        policy = load_profile("v5-paper-500").policy
        short, hour, btc = self.normalize(), self.normalize(bar="1H"), self.normalize("BTC-USDT", "1H")
        with self.assertRaises(TradingError):
            make_trend(short, hour, hour, now_ms=NOW, policy=policy)
        with self.assertRaisesRegex(TradingError, "STALE_PUBLIC_DATA"):
            make_trend(short, hour, btc, now_ms=NOW + 30001, policy=policy)

    def test_normalized_trend_roundtrips_into_existing_radar(self):
        policy = load_profile("v5-paper-500").policy
        capture = normalize_market("TEST-USDT", *public_fixture(), source_origin=TR_ORIGIN, now_ms=NOW, policy=policy)
        short = self.normalize(reply=candle_fixture("15m", opening="99", close="100"))
        hour, btc = self.normalize(bar="1H"), self.normalize("BTC-USDT", "1H")
        trend = make_trend(short, hour, btc, now_ms=NOW, policy=policy)
        market = capture.market
        candidate = {"market": {"instrument": market.instrument, "price": str(market.price), "bid": str(market.bid),
                                "ask": str(market.ask), "quote_volume_24h": str(market.quote_volume_24h),
                                "observed_at": market.observed_at,
                                "bids": [[str(p), str(q)] for p, q in market.bids],
                                "asks": [[str(p), str(q)] for p, q in market.asks]},
                     "plan": sample_plan(capital="90").to_dict(), "trend": trend}
        with patch("socket.create_connection", side_effect=AssertionError("offline only")):
            self.assertEqual(len(screen([candidate], policy, NOW // 1000)["accepted"]), 1)
        btc = self.normalize("BTC-USDT", "1H", reply=candle_fixture("1H", close="97"))
        candidate["trend"] = make_trend(short, hour, btc, now_ms=NOW, policy=policy)
        self.assertEqual(screen([candidate], policy, NOW // 1000)["rejected"][0]["reason"], "BTC_RISK_OFF")

    def test_tiny_negative_return_does_not_round_to_zero(self):
        reply = candle_fixture("15m", opening="100", close="99.9999999999999999")
        self.assertEqual(self.normalize(reply=reply).value, Decimal("-0.000000000001"))
