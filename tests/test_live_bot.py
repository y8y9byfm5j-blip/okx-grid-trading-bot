"""Offline tests for the live Telegram service: data, rotation, early longs and GRID bots."""
import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import unittest

from okx_grid.domain import TradingError
from okx_grid.early_long import EarlyLongScanner, features, score
from okx_grid.grid_manager import GridBotManager, GridLimits, okx_grid_params
from okx_grid.live_bot import LiveBot, Settings, build_profile
from okx_grid.live_reports import bots_text, early_text, rotation_text
from okx_grid.rotation import RotationTracker
from okx_grid.tr_private import OKXTRPrivate, OutcomeUnknown
from okx_grid.tr_public import TRPublic, _Retryable, coin_volumes


class NoPacer:
    def wait(self):
        pass


def reader(routes):
    """TRPublic over a fake transport; routes maps a path fragment to a payload or exception."""
    calls = []

    def get(url):
        calls.append(url)
        for key, value in routes.items():
            if key in url:
                if isinstance(value, list):  # a scripted sequence of replies
                    value = value.pop(0) if len(value) > 1 else value[0]
                if isinstance(value, BaseException):
                    raise value
                return value
        raise AssertionError("unexpected URL " + url)
    r = TRPublic(transport=get, sleep=lambda s: None, pacer=NoPacer(), rubik_pacer=NoPacer())
    r.calls = calls
    return r


def ticker(inst, last, vol, open_=None):
    return {"instId": inst, "last": str(last), "open24h": str(open_ or last), "volCcy24h": str(vol), "ts": "1"}


def daily(closes, volumes=None, start=1_700_000_000_000):
    volumes = volumes or [1000] * len(closes)
    rows = []
    for i, (c, v) in enumerate(zip(closes, volumes)):
        rows.append((start + i * 86_400_000, Decimal(c), Decimal(c) * Decimal("1.01"), Decimal(c) * Decimal("0.99"),
                     Decimal(c), Decimal(v), Decimal(v) * Decimal(c), True))
    return rows


def decision(instrument="SOL-USDT", lower="99", upper="102", grids=10, price="100.5", loss="2.5",
             eligible=True, status="VERIFIED_TR_PUBLIC", decision_id="d1"):
    return {"decision_id": decision_id, "instrument": instrument, "eligible": eligible, "data_status": status,
            "planned_loss_usdt": loss, "market_summary": {"price": price},
            "plan": {"lower": lower, "upper": upper, "grids": grids, "capital": "300", "stop": "98.9",
                     "take_profit": "102.1", "tick_size": "0.01"}}


class FakeClient:
    def __init__(self):
        self.pending, self.history, self.placed, self.stopped = [], [], [], []
        self.balance, self.fail = "1000", None

    def usdt_available(self):
        return self.balance

    def place_spot_grid(self, **kw):
        self.placed.append(kw)
        if self.fail:
            raise self.fail
        algo = f"A{len(self.placed)}"
        self.pending.append({"algoId": algo, "algoClOrdId": kw["client_id"], "totalPnl": "0"})
        return algo

    def pending_grids(self):
        return list(self.pending)

    def grid_history(self):
        return list(self.history)

    def stop_spot_grid(self, algo_id, instrument, sell_base=True):
        self.stopped.append(algo_id)
        return True


class PublicReaderTests(unittest.TestCase):
    def test_tickers_retry_and_normalize(self):
        r = reader({"/market/tickers": [_Retryable(502), {"code": "0", "data": [
            ticker("SOL-USDT", 100, 5e7), ticker("SOL-TRY", 5000, 1e8), ticker("USDT-TRY", 50, 1e9),
            ticker("USDC-USDT", 1, 1e9), {"instId": "BAD"}]}]})
        rows = r.spot_tickers()  # the 502 is retried once
        self.assertEqual(len(r.calls), 2)
        self.assertEqual(len(rows), 4)
        coins, rate = coin_volumes(rows)
        self.assertEqual(rate, Decimal(50))
        self.assertEqual(set(coins), {"SOL"})
        self.assertEqual(coins["SOL"]["usdt_volume"], Decimal("5E+7"))
        self.assertEqual(coins["SOL"]["try_volume"], Decimal("2E+6"))

    def test_error_codes_and_allowlist(self):
        r = reader({"/market/ticker": {"code": "51001", "data": []}})
        with self.assertRaises(TradingError):
            r.last_price("SOL-USDT")
        with self.assertRaises(TradingError):
            r.candles("SOL-USDT", "3m")
        with self.assertRaises(TradingError):
            r.taker_volume("SO/L")

    def test_candles_and_taker_volume_are_oldest_first(self):
        r = reader({"/market/candles": {"code": "0", "data": [
            ["2", "1", "2", "0.5", "1.5", "10", "15", "15", "0"], ["1", "1", "2", "0.5", "1", "10", "10", "10", "1"]]},
            "/rubik/stat/taker-volume": {"code": "0", "data": [["2", "3", "5"], ["1", "1", "2"]]}})
        rows = r.candles("SOL-USDT", "1D", 2)
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertFalse(rows[1][7])
        self.assertEqual(r.taker_volume("SOL")[0], (1, Decimal(1), Decimal(2)))


class PrivateClientTests(unittest.TestCase):
    def make(self, reply):
        sent = []

        def transport(method, url, headers, body):
            sent.append((method, url, headers, body))
            if isinstance(reply, BaseException):
                raise reply
            return reply
        clock = lambda: datetime(2026, 10, 27, 12, 0, 0, 123000, tzinfo=timezone.utc)
        return OKXTRPrivate("k1", "s1", "p1", transport=transport, clock=clock), sent

    def test_signature_matches_okx_scheme(self):
        client, sent = self.make((200, json.dumps({"code": "0", "data": [{"details": [{"ccy": "USDT", "availBal": "42"}]}]}).encode()))
        self.assertEqual(client.usdt_available(), "42")
        method, url, headers, body = sent[0]
        self.assertEqual(url, "https://tr.okx.com/api/v5/account/balance?ccy=USDT")
        expected = base64.b64encode(hmac.new(b"s1", b"2026-10-27T12:00:00.123ZGET/api/v5/account/balance?ccy=USDT",
                                             hashlib.sha256).digest()).decode()
        self.assertEqual(headers["OK-ACCESS-SIGN"], expected)
        self.assertEqual(headers["OK-ACCESS-TIMESTAMP"], "2026-10-27T12:00:00.123Z")
        self.assertNotIn("s1", repr(client))

    def test_place_grid_body_and_rejections(self):
        client, sent = self.make((200, json.dumps({"code": "0", "data": [{"algoId": "9", "sCode": "0"}]}).encode()))
        algo = client.place_spot_grid(instrument="SOL-USDT", lower=Decimal("99"), upper=Decimal("102"), grids=5,
                                      quote_size=Decimal("300"), stop=Decimal("98"), take_profit=Decimal("103"), client_id="tgabc")
        self.assertEqual(algo, "9")
        body = json.loads(sent[0][3])
        self.assertEqual((body["algoOrdType"], body["minPx"], body["gridNum"], body["quoteSz"], body["slTriggerPx"]),
                         ("grid", "99", "5", "300", "98"))
        client, _ = self.make((200, json.dumps({"code": "1", "data": [{"sCode": "51381", "sMsg": "x"}]}).encode()))
        with self.assertRaises(TradingError) as ctx:
            client.place_spot_grid(instrument="SOL-USDT", lower=1, upper=2, grids=2, quote_size=1, stop=0.5,
                                   take_profit=3, client_id="x")
        self.assertEqual(ctx.exception.code, "OKX_ERROR_1")
        client, _ = self.make((502, b"bad gateway"))
        with self.assertRaises(OutcomeUnknown):
            client.stop_spot_grid("9", "SOL-USDT")

    def test_disallowed_path(self):
        client, _ = self.make((200, b"{}"))
        with self.assertRaises(TradingError):
            client._request("POST", "/api/v5/asset/withdrawal", body={})
        with self.assertRaises(TradingError):
            OKXTRPrivate("", "s", "p")


class GridManagerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.now = [1_000_000]
        self.prices = {"SOL-USDT": Decimal("100.5"), "ETH-USDT": Decimal("100.5"), "BTC-USDT": Decimal("100.5")}

    def tearDown(self):
        self.dir.cleanup()

    def manager(self, mode="paper", client=None, **limits):
        return GridBotManager(Path(self.dir.name) / f"{mode}.sqlite3", mode=mode, client=client,
                              limits=GridLimits(**limits), clock_ms=lambda: self.now[0],
                              price=lambda inst: self.prices[inst])

    def test_params_reduce_grids_to_okx_step_and_enforce_risk(self):
        params = okx_grid_params(decision(grids=20), GridLimits())
        self.assertEqual(params["grids"], 9)  # 3 / 102 / 9 >= 0.3%
        with self.assertRaises(TradingError) as ctx:
            okx_grid_params(decision(loss="3.5"), GridLimits())
        self.assertEqual(ctx.exception.code, "BOT_RISK_LIMIT")
        with self.assertRaises(TradingError) as ctx:
            okx_grid_params(decision(lower="100.4", upper="100.6"), GridLimits())
        self.assertEqual(ctx.exception.code, "OKX_GRID_STEP_TOO_SMALL")
        with self.assertRaises(TradingError):
            okx_grid_params(decision(eligible=False), GridLimits())

    def test_paper_limits_stop_loss_and_halt(self):
        m = self.manager()
        m.open_from_decision(decision("SOL-USDT", decision_id="a"))
        with self.assertRaises(TradingError) as ctx:
            m.open_from_decision(decision("SOL-USDT", decision_id="b"))
        self.assertEqual(ctx.exception.code, "PAIR_ALREADY_ACTIVE")
        m.open_from_decision(decision("ETH-USDT", decision_id="c"))
        with self.assertRaises(TradingError) as ctx:
            m.open_from_decision(decision("BTC-USDT", decision_id="d"))
        self.assertEqual(ctx.exception.code, "MAX_BOTS_REACHED")
        self.prices["SOL-USDT"] = Decimal("98")
        events = m.reconcile()
        self.assertEqual([(e["kind"], e["reason"]) for e in events], [("CLOSED", "STOP_LOSS")])
        self.assertEqual(m.realized_pnl(), Decimal("-2.5"))
        self.assertIn("PAIR_COOLDOWN", m.blocking_reasons("SOL-USDT"))
        self.now[0] += 7 * 3_600_000
        self.assertNotIn("PAIR_COOLDOWN", m.blocking_reasons("SOL-USDT"))
        m2 = self.manager(max_realized_loss=Decimal("2"))
        m2._db = m._db  # same ledger, stricter fuse
        events = m2.reconcile()
        self.assertEqual(events[-1]["kind"], "HALT")
        self.assertFalse(m2.auto_enabled())
        self.assertIn("HALTED", m2.blocking_reasons("BTC-USDT"))

    def test_total_capital_limit(self):
        m = self.manager(total_capital=Decimal("500"), bot_capital=Decimal("300"), max_bots=1)
        m.open_from_decision(decision("SOL-USDT", decision_id="a"))
        self.assertIn("TOTAL_CAPITAL_LIMIT", m.blocking_reasons("ETH-USDT"))

    def test_live_unknown_outcome_is_resolved_before_new_bots(self):
        client = FakeClient()
        m = self.manager("live", client)
        with self.assertRaises(TradingError) as ctx:
            m.open_from_decision(decision(status="DATA_UNVERIFIED"))
        self.assertEqual(ctx.exception.code, "DATA_UNVERIFIED")
        client.fail = OutcomeUnknown()
        with self.assertRaises(OutcomeUnknown):
            m.open_from_decision(decision(decision_id="x"))
        self.assertIn("UNRESOLVED_OKX_REQUEST", m.blocking_reasons("ETH-USDT"))
        # OKX did create it: the client ID resolves the unknown request.
        client.fail = None
        client.pending.append({"algoId": "Z1", "algoClOrdId": m.active()[0]["client_id"], "totalPnl": "0.4"})
        events = m.reconcile()
        self.assertEqual(events[0]["kind"], "RESOLVED")
        self.assertEqual(m.active()[0]["state"], "RUNNING")
        self.assertEqual(m.active()[0]["algo_id"], "Z1")
        # Stopped on OKX by its stop-loss: history closes the local bot with OKX's PnL.
        client.pending.clear()
        client.history.append({"algoId": "Z1", "cancelType": "3", "totalPnl": "-2.9"})
        events = m.reconcile()
        self.assertEqual((events[0]["reason"], events[0]["pnl"]), ("STOP_LOSS", Decimal("-2.9")))

    def test_live_rejection_balance_and_emergency_stop(self):
        client = FakeClient()
        m = self.manager("live", client)
        client.balance = "100"
        with self.assertRaises(TradingError) as ctx:
            m.open_from_decision(decision())
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_USDT_BALANCE")
        client.balance = "1000"
        client.fail = TradingError("OKX_GRID_REJECTED_51381")
        with self.assertRaises(TradingError):
            m.open_from_decision(decision(decision_id="r"))
        self.assertEqual(m.closed()[-1]["state"], "REJECTED")
        client.fail = None
        m.open_from_decision(decision(decision_id="ok"))
        with self.assertRaises(TradingError) as ctx:
            m.open_from_decision(decision(decision_id="ok"))
        self.assertEqual(ctx.exception.code, "PAIR_ALREADY_ACTIVE")
        stopped, failed = m.emergency_stop()
        self.assertEqual((stopped, failed, client.stopped), (1, [], ["A2"]))
        self.assertFalse(m.auto_enabled())
        self.assertEqual(m.halted(), "EMERGENCY_STOP")

    def test_emergency_stop_also_stops_a_bot_resolved_afterwards(self):
        client = FakeClient()
        m = self.manager("live", client)
        client.fail = KeyError("unparseable reply")
        with self.assertRaises(OutcomeUnknown):
            m.open_from_decision(decision())
        self.assertEqual(m.active()[0]["state"], "UNKNOWN")
        m.emergency_stop()
        client.fail = None
        client.pending.append({"algoId": "Z9", "algoClOrdId": m.active()[0]["client_id"], "totalPnl": "0"})
        m.reconcile()
        self.assertEqual(client.stopped, ["Z9"])
        self.assertEqual(m.active()[0]["state"], "STOPPING")

    def test_unknown_request_that_never_reached_okx_is_released(self):
        client = FakeClient()
        m = self.manager("live", client)
        client.fail = OutcomeUnknown()
        with self.assertRaises(OutcomeUnknown):
            m.open_from_decision(decision())
        client.fail = None
        self.assertEqual(m.reconcile(), [])
        self.now[0] += 121_000
        self.assertEqual(m.reconcile()[0]["kind"], "NOT_CREATED")
        self.assertEqual(m.blocking_reasons("SOL-USDT"), [])

    def test_paper_bots_never_block_live(self):
        m = self.manager()
        m.open_from_decision(decision())
        live = GridBotManager(Path(self.dir.name) / "paper.sqlite3", mode="live", client=FakeClient(),
                              clock_ms=lambda: self.now[0])
        self.assertEqual(live.blocking_reasons("SOL-USDT"), [])


class RotationTests(unittest.TestCase):
    def test_share_moves_and_net_taker_flows(self):
        now = [10 * 86_400_000]
        payload = {"code": "0", "data": [ticker("AAA-USDT", 1, 10e6), ticker("BBB-USDT", 2, 10e6),
                                          ticker("USDT-TRY", 50, 1e9), ticker("AAA-TRY", 50, 1e8)]}
        routes = {"/market/tickers": payload,
                  "ccy=AAA": {"code": "0", "data": [["1", "100", "300"]] * 3},
                  "ccy=BBB": {"code": "0", "data": [["1", "200", "50"]] * 3}}
        with tempfile.TemporaryDirectory() as d:
            t = RotationTracker(Path(d) / "m.sqlite3", reader(routes), clock_ms=lambda: now[0])
            self.assertIsNone(t.analyze())
            t.collect()
            now[0] += 24 * 3_600_000
            payload["data"][0] = ticker("AAA-USDT", 1.1, 30e6, 1)
            t.collect()
            t.collect_flows()
            report = t.analyze()
            gainers = report["horizons"]["24s"]["gainers"]
            self.assertEqual(gainers[0][0], "AAA")
            self.assertEqual([c for c, _ in report["inflow"]], ["AAA"])
            self.assertEqual([c for c, _ in report["outflow"]], ["BBB"])
            self.assertEqual(report["inflow"][0][1]["net_24h"], Decimal("660.0"))
            self.assertGreater(t.accumulation_ratio("AAA"), 0)
            text = rotation_text(report)
            self.assertIn("PARA GİREN", text)
            self.assertIn("AAA", text)
            self.assertIn("müşteri bakiyesi", text)


class EarlyLongTests(unittest.TestCase):
    def test_accumulation_scores_high_and_pumped_coins_are_excluded(self):
        closes = [100 + (i % 3) * 0.2 for i in range(80)] + [100 + i * 0.6 for i in range(1, 11)]
        volumes = [1000] * 80 + [3000] * 10
        btc = daily([100] * 90)
        f = features(daily(closes, volumes), btc)
        points, label, reasons = score(f, Decimal("0.1"))
        self.assertGreaterEqual(points, 70, reasons)
        self.assertEqual(label, "GÜÇLÜ")
        pumped = [100] * 80 + [100 * 1.15 ** i for i in range(1, 11)]
        points, label, reasons = score(features(daily(pumped), btc))
        self.assertEqual((points, label), (0, "GEÇ"))
        with self.assertRaises(TradingError):
            features(daily([100] * 30))

    def test_scanner_universe_and_text(self):
        closes = [100 + (i % 3) * 0.2 for i in range(80)] + [100 + i * 0.6 for i in range(1, 11)]
        candles = [[str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5]), str(r[6]), str(r[6]), "1"]
                   for r in daily(closes, [1000] * 80 + [3000] * 10)]
        r = reader({"/market/tickers": {"code": "0", "data": [ticker("AAA-USDT", 1, 5e6), ticker("TINY-USDT", 1, 1e5),
                                                               ticker("BTC3L-USDT", 1, 9e6)]},
                    "/market/candles": {"code": "0", "data": candles}})
        scan = EarlyLongScanner(r).scan()
        self.assertEqual([x["instrument"] for x in scan["results"]], ["AAA-USDT"])
        self.assertIn("AAA-USDT", early_text(scan))


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send(self, chat, text):
        self.sent.append((chat, text))


class FakeMarket:
    last_health = {"ok": False, "data_status": "DATA_UNVERIFIED", "reason": "NOT_CHECKED"}

    def capture(self, instrument, refresh=False):
        raise TradingError("UNKNOWN_SYMBOL")


class LiveBotTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        env = {"TELEGRAM_BOT_TOKEN": "t:1", "TELEGRAM_ALLOWED_CHAT_IDS": "5, 6", "STATE_DIR": self.dir.name}
        self.settings = Settings.from_env(env)
        self.bot = LiveBot(self.settings, telegram=FakeTelegram(), reader=reader({}), market=FakeMarket())

    def tearDown(self):
        self.dir.cleanup()

    def message(self, text, chat=5, bot=False):
        return {"chat": {"id": chat}, "from": {"id": 1, "is_bot": bot}, "text": text}

    def test_settings_validation(self):
        self.assertEqual(self.settings.execution, "paper")
        self.assertEqual(self.settings.notify_chat, 5)
        for env in ({"TELEGRAM_BOT_TOKEN": "CHANGE_ME", "TELEGRAM_ALLOWED_CHAT_IDS": "5"},
                    {"TELEGRAM_BOT_TOKEN": "t:1", "TELEGRAM_ALLOWED_CHAT_IDS": "CHANGE_ME"},
                    {"TELEGRAM_BOT_TOKEN": "t:1", "TELEGRAM_ALLOWED_CHAT_IDS": "5", "GRID_EXECUTION": "real"},
                    {"TELEGRAM_BOT_TOKEN": "t:1", "TELEGRAM_ALLOWED_CHAT_IDS": "5", "GRID_BOT_CAPITAL": "400"}):
            with self.assertRaises(TradingError):
                Settings.from_env(env)

    def test_first_start_skips_backlog(self):
        calls = []

        class Updates(FakeTelegram):
            def updates(inner, offset, timeout=50):
                calls.append(offset)
                if offset == -1:
                    return [{"update_id": 41, "message": self.message("/acil_durdur")}]
                self.bot._stop.set()
                return []
        self.bot.telegram = Updates()
        self.bot.poll()
        self.assertEqual(calls, [-1, 42])
        self.assertTrue(self.bot.grid.auto_enabled())
        with self.assertRaises(TradingError):
            Settings.from_env({"TELEGRAM_BOT_TOKEN": "t:1", "TELEGRAM_ALLOWED_CHAT_IDS": "5", "GRID_MAX_BOTS": "two"})

    def test_profile_uses_owner_limits(self):
        policy = build_profile(GridLimits()).policy
        self.assertEqual((policy.equity, policy.max_bot_capital, policy.max_bots), (Decimal(600), Decimal(300), 2))
        self.assertEqual(policy.equity * policy.risk_fraction, Decimal(3))

    def test_acl_and_commands(self):
        self.assertTrue(self.bot.allowed(self.message("/durum")))
        self.assertFalse(self.bot.allowed(self.message("/durum", chat=7)))
        self.assertFalse(self.bot.allowed(self.message("/durum", bot=True)))
        self.assertIn("/rotasyon", self.bot.handle(self.message("/yardim@OkxBot")))
        self.assertIn("TEST", self.bot.handle(self.message("/durum")))
        self.assertIn("KAPALI", self.bot.handle(self.message("/otomatik kapat")))
        self.assertFalse(self.bot.grid.auto_enabled())
        self.assertIn("parite OKX TR'de yok", self.bot.handle(self.message("/grid sol")))
        self.assertIn("Açık bot yok", self.bot.handle(self.message("/botlar")))
        self.assertIn("ACİL", self.bot.handle(self.message("/acil_durdur")))
        self.assertIn("/devam", self.bot.handle(self.message("/otomatik ac")))
        self.assertIn("Bilinmeyen", self.bot.handle(self.message("/xyz")))
        self.assertIsNone(self.bot.handle(self.message("merhaba")))
        self.assertEqual(LiveBot.pair(["solusdt"]), "SOL-USDT")
        self.assertEqual(LiveBot.pair(["eth/usdt"]), "ETH-USDT")

    def test_bots_text_shows_limits(self):
        text = bots_text(self.bot.grid.summary(), [])
        self.assertIn("600", text)
        self.assertIn("TEST", text)


if __name__ == "__main__":
    unittest.main()


class SetupHelperTests(unittest.TestCase):
    def test_token_discovery_env_writes_and_chat_capture(self):
        from okx_grid import setup_helper
        good, other = "123456789:" + "A" * 35, "987654321:" + "B" * 35

        class Api:
            def __init__(self, value=None, updates=None):
                self.value, self.updates = value, updates or []

            def call(self, method, body, timeout=20):
                if method == "getMe":
                    if self.value != good:
                        raise TradingError("TELEGRAM_ERROR_401")
                    return {"username": "okx_test_bot"}
                return [u for u in self.updates if u["update_id"] >= body.get("offset", 0)]

        texts = ["[Service]\nEnvironmentFile=-/nonexistent/env\nEnvironment=BOT=" + good,
                 "x = '" + other + "'"]
        self.assertEqual(setup_helper.find_tokens(texts), sorted([good, other]))
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / "bot.env"
            env.write_text("TELEGRAM_BOT_TOKEN=CHANGE_ME\nGRID_EXECUTION=paper\n")
            # Only the token Telegram accepts is used; it is never printed.
            self.assertEqual(setup_helper.token_auto(env, texts, api_factory=Api), 0)
            values = setup_helper.read_env(env)
            self.assertEqual((values["TELEGRAM_BOT_TOKEN"], values["GRID_EXECUTION"]), (good, "paper"))
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            self.assertEqual(setup_helper.token_auto(env, ["nothing here"], api_factory=Api), 3)
            # Two working bots: the owner picks one by number.
            third = "555555555:" + "C" * 35
            original_api = Api

            class TwoBots(original_api):
                def call(inner, method, body, timeout=20):
                    if method == "getMe" and inner.value == third:
                        return {"username": "second_bot"}
                    return original_api.call(inner, method, body, timeout)
            self.assertEqual(setup_helper.token_auto(env, [good, third], api_factory=TwoBots, choose=lambda _: "2"), 0)
            self.assertEqual(setup_helper.read_env(env)["TELEGRAM_BOT_TOKEN"], third)
            self.assertEqual(setup_helper.token_auto(env, [good, third], api_factory=TwoBots, choose=lambda _: "9"), 3)
            old = Path(d) / "old" / "v5"
            (old / "venv" / "lib").mkdir(parents=True)
            (old / ".env").write_text("BOT=" + good)
            (old / "venv" / "lib" / "x.py").write_text("ignored")
            self.assertEqual(setup_helper.tree_files(str(Path(d) / "old")), [str(old / ".env")])
            answers = iter(["wrong", good])
            self.assertEqual(setup_helper.token_ask(env, prompt=lambda _: next(answers), api_factory=Api), 0)
            api = Api(updates=[{"update_id": 7, "message": {"text": "/start", "chat": {"id": 55, "first_name": "E"},
                                                              "from": {"id": 1}}}])
            api.updates.insert(0, {"update_id": 6, "message": {"text": "eski", "chat": {"id": 9}, "from": {"id": 1}}})
            # The old message (6) is the starting point; only a newer /start is accepted.
            api_calls = {"n": 0}
            original = api.call

            def staged(method, body, timeout=20):
                api_calls["n"] += 1
                if api_calls["n"] == 1:
                    return [api.updates[0]]
                return original(method, body, timeout)
            api.call = staged
            self.assertEqual(setup_helper.chat_ask(env, api=api, sleep=lambda s: None), 0)
            self.assertEqual(setup_helper.read_env(env)["TELEGRAM_ALLOWED_CHAT_IDS"], "55")


class RiskCommandTests(LiveBotTests):
    def test_risk_command_updates_limits_and_survives_restart(self):
        text = self.bot.handle(self.message("/risk 10"))
        self.assertIn("10", text)
        self.assertEqual(self.bot.grid.limits.risk_per_bot, Decimal(10))
        cap = self.bot.profile.policy.equity * self.bot.profile.policy.risk_fraction
        self.assertTrue(Decimal("9.99999999") < cap <= Decimal(10))
        self.assertIn("geçersiz risk", self.bot.handle(self.message("/risk 50")))
        self.assertIn("geçersiz risk", self.bot.handle(self.message("/risk abc")))
        again = LiveBot(self.settings, telegram=FakeTelegram(), reader=reader({}), market=FakeMarket())
        self.assertEqual(again.grid.limits.risk_per_bot, Decimal(10))
        self.assertIn("10", again.handle(self.message("/risk")))


class OldBotDiscoveryTests(unittest.TestCase):
    def test_running_old_bot_token_and_its_unit_are_found(self):
        from okx_grid import setup_helper
        value = "123456789:" + "D" * 35
        with tempfile.TemporaryDirectory() as d:
            proc = Path(d) / "proc"
            for pid, env, cmd, cgroup in (
                    ("41", "HOME=/root\0BOT_TOKEN=" + value + "\0", "python3\0telegram_commands.py\0",
                     "0::/system.slice/okx-telegram-commands.service\n"),
                    ("42", "PATH=/usr/bin\0", "bash\0", "0::/user.slice/user-0.slice/session-1.scope\n"),
                    ("43", "T=" + value + "\0", "python3\0bot.py\0", "0::/user.slice/user-0.slice/session-1.scope\n"),
                    ("44", "T=" + value + "\0", "python3\0-m\0okx_grid.live_bot\0", "0::/system.slice/okx-tr-bot.service\n")):
                (proc / pid).mkdir(parents=True)
                (proc / pid / "environ").write_bytes(env.encode())
                (proc / pid / "cmdline").write_bytes(cmd.encode())
                (proc / pid / "cgroup").write_text(cgroup)
            sources, _ = setup_helper.process_sources(str(proc))
            self.assertEqual(setup_helper.find_tokens([t for _, t in sources]), [value])
            # A token glued to "bot" inside an API URL; a longer digit run is not a token.
            self.assertEqual(setup_helper.find_tokens(["curl https://api.telegram.org/bot" + value + "/getMe"]), [value])
            self.assertEqual(setup_helper.find_tokens(["9999999" + value]), [])
            odd = Path(d) / "tree"
            (odd / "cfg").mkdir(parents=True)
            (odd / "cfg" / "telegram.token").write_text(value)
            (odd / "cfg" / "big.bin").write_bytes(b"x" * 10)
            self.assertEqual([os.path.basename(p) for p in setup_helper.tree_files(str(odd))], ["telegram.token"])
            self.assertIn("telegram_commands.py", sources[0][0])
            self.assertEqual(setup_helper.holders(value, str(proc)), {"okx-telegram-commands.service", "pid:43"})
            self.assertEqual(setup_helper.holders("", str(proc)), set())
            self.assertEqual(setup_helper.holders("t:1", str(proc)), set())
            self.assertEqual(setup_helper.holders(value[:20], str(proc)), set())

    def test_unreachable_telegram_still_saves_a_single_found_token(self):
        from okx_grid import setup_helper
        value = "123456789:" + "E" * 35

        class Down:
            def __init__(self, v):
                pass

            def call(self, method, body, timeout=20):
                raise TradingError("TELEGRAM_UNAVAILABLE")
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / "bot.env"
            self.assertEqual(setup_helper.token_auto(env, ["x=" + value], api_factory=Down), 0)
            self.assertEqual(setup_helper.read_env(env)["TELEGRAM_BOT_TOKEN"], value)


class RadarAliasTests(LiveBotTests):
    def test_radar_and_paper_aliases(self):
        self.bot.rotation.latest_coins = {"SOL": {"usdt_volume": Decimal("5e7")}, "LOW": {"usdt_volume": Decimal("1")}}
        text = self.bot.handle(self.message("/radar"))
        self.assertIn("SOL-USDT", text)
        self.assertNotIn("LOW-USDT", text)
        self.assertIn("parite OKX TR'de yok", text)
        self.assertIn("GRID BOTLARI", self.bot.handle(self.message("/paper")))


class ActionsRunTests(unittest.TestCase):
    def setUp(self):
        from okx_grid import actions_run
        self.run_mod = actions_run
        self.dir = tempfile.TemporaryDirectory()
        self.now = [1_800_000_000]
        self.env = {"TELEGRAM_BOT_TOKEN": "t:1", "TELEGRAM_ALLOWED_CHAT_IDS": "5", "STATE_DIR": self.dir.name,
                    "GRID_EXECUTION": "live", "OKX_API_KEY": "abc", "RUNNER_TEMP": self.dir.name}

    def tearDown(self):
        self.dir.cleanup()

    def bot(self, chats="5"):
        env = dict(self.env, TELEGRAM_ALLOWED_CHAT_IDS=chats)
        bot = LiveBot(self.run_mod.run_settings(env), telegram=FakeTelegram(), reader=reader({}), market=FakeMarket(),
                      clock_ms=lambda: self.now[0] * 1000)
        self.calls = []
        for name, _ in self.run_mod.JOBS:
            setattr(bot, name, (lambda n: lambda: self.calls.append(n))(name))
        return bot

    def message(self, text, chat=5, age=0):
        return {"chat": {"id": chat}, "from": {"id": 1}, "text": text, "date": self.now[0] - age}

    def test_runner_is_forced_to_paper_and_never_holds_okx_keys(self):
        settings = self.run_mod.run_settings(self.env)
        self.assertEqual(settings.execution, "paper")
        self.assertEqual((settings.okx_access, settings.okx_signing, settings.okx_phrase), ("", "", ""))
        empty = self.run_mod.run_settings(dict(self.env, TELEGRAM_ALLOWED_CHAT_IDS=""))
        self.assertEqual((empty.allowed_chats, empty.notify_chat), (frozenset(), 0))
        with self.assertRaises(TradingError):
            Settings.from_env(dict(self.env, TELEGRAM_ALLOWED_CHAT_IDS=""))

    def test_token_is_extracted_from_a_messy_secret_and_never_leaks_in_errors(self):
        sample = "123456789:" + "A" * 35
        for messy in (" " + sample + " \n", "Token: " + sample + " güvende tutun", "x\n" + sample):
            settings = Settings.from_env({"TELEGRAM_BOT_TOKEN": messy, "TELEGRAM_ALLOWED_CHAT_IDS": "5"})
            self.assertEqual(settings.bot_token, sample)
        for bad in ("abc def", "not/a/token", "12345:short thing"):
            with self.assertRaises(TradingError) as ctx:
                Settings.from_env({"TELEGRAM_BOT_TOKEN": bad, "TELEGRAM_ALLOWED_CHAT_IDS": "5"})
            self.assertEqual(ctx.exception.code, "TELEGRAM_BAD_AUTH_FORMAT")
        import http.client
        from okx_grid.live_bot import TelegramAPI

        def broken(url, body, timeout):
            raise http.client.InvalidURL("URL can't contain control characters. " + url)
        with self.assertRaises(TradingError) as ctx:
            TelegramAPI(sample, transport=broken).call("getUpdates", {})
        self.assertEqual(ctx.exception.code, "TELEGRAM_UNAVAILABLE")
        self.assertNotIn(sample, repr(ctx.exception) + str(ctx.exception.__cause__) + str(ctx.exception.__context__ or ""))

    def test_chat_id_secret_may_be_a_pasted_sentence(self):
        base = {"TELEGRAM_BOT_TOKEN": "123456789:" + "A" * 35}
        cases = {"5, 6": {5, 6}, " 123456789 \n": {123456789}, "-1001234567890": {-1001234567890},
                 "Sohbet kimliğiniz: 123456789 (4. adım)": {123456789}}
        for raw, expected in cases.items():
            settings = Settings.from_env(dict(base, TELEGRAM_ALLOWED_CHAT_IDS=raw))
            self.assertEqual(set(settings.allowed_chats), expected, raw)
        for bad in ("CHANGE_ME", "sohbet kimliği yok"):
            with self.assertRaises(TradingError) as ctx:
                Settings.from_env(dict(base, TELEGRAM_ALLOWED_CHAT_IDS=bad))
            self.assertEqual(ctx.exception.code, "INVALID_TELEGRAM_ALLOWED_CHAT_IDS")

    def test_swapped_secrets_are_repaired_and_a_bare_number_is_named(self):
        sample = "123456789:" + "A" * 35
        swapped = dict(self.env, TELEGRAM_BOT_TOKEN="987654321", TELEGRAM_ALLOWED_CHAT_IDS=sample)
        settings = self.run_mod.run_settings(swapped)
        self.assertEqual((settings.bot_token, set(settings.allowed_chats)), (sample, {987654321}))
        ok = self.run_mod.run_settings(dict(self.env, TELEGRAM_BOT_TOKEN=sample, TELEGRAM_ALLOWED_CHAT_IDS="55555"))
        self.assertEqual((ok.bot_token, set(ok.allowed_chats)), (sample, {55555}))
        bare = " 987654321 "
        self.assertIn("yalnız bir sayı", self.run_mod.auth_hint({"TELEGRAM_BOT_TOKEN": bare}))

    def test_auth_hint_shows_sizes_only_and_getme_fails_fast(self):
        sample = "123456789:" + "A" * 35
        self.assertEqual(self.run_mod.auth_hint({"TELEGRAM_BOT_TOKEN": " " + sample}),
                         "bot no 123456789, gizli kısım 35 karakter (35 olmalı)")
        self.assertIn("34 karakter", self.run_mod.auth_hint({"TELEGRAM_BOT_TOKEN": sample[:-1]}))
        self.assertIn("biçiminde değil", self.run_mod.auth_hint({"TELEGRAM_BOT_TOKEN": "abc"}))
        self.assertNotIn("A" * 5, self.run_mod.auth_hint({"TELEGRAM_BOT_TOKEN": sample}))
        bot = self.bot()
        jobs = []

        class Rejecting(FakeTelegram):
            def call(inner, method, body, timeout=20):
                raise TradingError("TELEGRAM_ERROR_401")
        bot.telegram = Rejecting()
        for name, _ in self.run_mod.JOBS:
            setattr(bot, name, (lambda n: lambda: jobs.append(n))(name))
        with self.assertRaises(TradingError):
            self.run_mod.run({}, seconds=200, bot=bot, now=lambda: self.now[0], sleep=lambda s: None)
        self.assertEqual(jobs, [])  # failed before any job ran

    def test_rejected_token_stops_the_run_with_a_clear_message(self):
        bot = self.bot()

        class Revoked(FakeTelegram):
            def updates(inner, offset, timeout=50):
                raise TradingError("TELEGRAM_ERROR_401")
        bot.telegram = Revoked()
        ticks = iter(range(0, 10_000, 40))
        with self.assertRaises(TradingError) as ctx:
            self.run_mod.run({}, seconds=200, bot=bot, now=lambda: self.now[0], monotonic=lambda: next(ticks),
                             sleep=lambda s: None)
        self.assertEqual(ctx.exception.code, "TELEGRAM_ERROR_401")

    def test_missing_token_ends_green_without_running_anything(self):
        import io
        from contextlib import redirect_stdout
        old = dict(os.environ)
        try:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(self.run_mod.main(), 0)
            self.assertIn("bekleniyor", out.getvalue())
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_unregistered_chat_only_learns_its_own_id(self):
        bot = self.bot(chats="")
        now = lambda: self.now[0]
        self.assertTrue(self.run_mod.handle_update(bot, self.message("/start", chat=77), now))
        self.assertIn("77", bot.telegram.sent[-1][1])
        self.assertEqual(bot.telegram.sent[-1][0], 77)
        sent = len(bot.telegram.sent)
        self.assertFalse(self.run_mod.handle_update(bot, self.message("/acil_durdur", chat=77), now))
        self.assertFalse(self.run_mod.handle_update(bot, self.message("/start", chat=77, age=3600), now))
        self.assertEqual(len(bot.telegram.sent), sent)
        # Only history collection runs before registration; alerts would have no destination.
        self.assertEqual(self.run_mod.run_jobs(bot, now), ["job_rotation"])

    def test_registered_chat_commands_acl_and_stale_messages(self):
        bot = self.bot()
        now = lambda: self.now[0]
        self.assertTrue(self.run_mod.handle_update(bot, self.message("/durum"), now))
        self.assertIn("TEST", bot.telegram.sent[-1][1])
        sent = len(bot.telegram.sent)
        self.assertFalse(self.run_mod.handle_update(bot, self.message("/durum", chat=9), now))
        self.assertFalse(self.run_mod.handle_update(bot, self.message("/durum", age=16 * 60), now))
        self.assertFalse(self.run_mod.handle_update(bot, self.message("selam"), now))
        self.assertEqual(len(bot.telegram.sent), sent)

    def test_jobs_run_when_due_and_not_again_inside_their_interval(self):
        bot = self.bot()
        now = lambda: self.now[0]
        self.assertEqual(self.run_mod.run_jobs(bot, now), [n for n, _ in self.run_mod.JOBS])
        self.assertEqual(self.run_mod.run_jobs(bot, now), [])
        self.now[0] += 130
        self.assertEqual(self.run_mod.run_jobs(bot, now), ["job_reconcile"])
        self.now[0] += 16 * 60
        self.assertEqual(self.run_mod.run_jobs(bot, now), ["job_rotation", "job_reconcile", "job_grid"])

    def test_run_answers_pending_commands_and_persists_the_offset(self):
        bot = self.bot()
        queue = [[{"update_id": 10, "message": self.message("/botlar")}], []]

        class Telegram(FakeTelegram):
            def updates(inner, offset, timeout=50):
                return queue.pop(0) if queue else []
        bot.telegram = Telegram()
        ticks = iter(range(0, 10_000, 40))
        result = self.run_mod.run({}, seconds=200, bot=bot, now=lambda: self.now[0], monotonic=lambda: next(ticks),
                                  sleep=lambda s: None)
        self.assertEqual(result["answered"], 1)
        self.assertIn("job_rotation", result["jobs"])
        self.assertIn("GRID BOTLARI", bot.telegram.sent[-1][1])
        again = LiveBot(self.run_mod.run_settings(self.env), telegram=FakeTelegram(), reader=reader({}),
                        market=FakeMarket())
        self.assertEqual(again.grid._meta("telegram_offset"), 11)
        # A scan stored by an earlier run is what /erkenlong shows in a fresh process.
        again.grid._set_meta("early_scan", {"at_ms": 1_800_000_000_000, "scanned": 3, "errors": 0, "results": [
            {"instrument": "AAA-USDT", "score": 80, "label": "GÜÇLÜ", "reasons": ["x"], "ret_7d": 0.1,
             "ret_30d": 0.2, "volume_ratio": 2.0, "price": 1.0}]})
        self.assertIn("AAA-USDT", again.handle({"chat": {"id": 5}, "from": {"id": 1}, "text": "/erkenlong"}))
