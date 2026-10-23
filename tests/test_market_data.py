"""Synthetic TR transport -> analysis -> authenticated command hook regression."""
from copy import deepcopy
from decimal import Decimal
import io
import json
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from okx_grid.domain import TradingError
from okx_grid.market_data import TRMarketData
from okx_grid.paper import PaperTrader
from okx_grid.profiles import load_profile
from okx_grid.public_contracts import TR_ORIGIN, request_spec
from okx_grid.public_http import ReadOnlyTRClient, RequestPacer
from okx_grid.scenarios import sample_plan
from okx_grid.telegram_market import handle_market_update
from tests.test_public_contracts import NOW, candle_fixture, envelope, public_fixture
from tests.test_public_http import FakeOpener, FakeResponse, InstantPacer, make_client


class SyntheticTR:
    def __init__(self):
        self.calls = []
        self.instrument, self.ticker, self.books = public_fixture()
        # Exercise the complete 400 bids / 400 asks contract, each > 0.
        self.books["data"][0]["bids"] = [[str(Decimal('99.99') - Decimal(i) / 100), '10', '0', '1'] for i in range(400)]
        self.books["data"][0]["asks"] = [[str(Decimal('100.01') + Decimal(i) / 100), '10', '0', '1'] for i in range(400)]
        btc = deepcopy(self.instrument["data"][0])
        btc.update(instId='BTC-USDT', baseCcy='BTC')
        self.instrument["data"].append(btc)
        self.server = NOW
        self.fail_kind = None
        self.short_close = '101'
        self.btc_close = '101'

    def fetch(self, kind, instrument=None, *, bar=None):
        self.calls.append((kind, instrument, bar))
        if self.fail_kind == kind:
            raise TradingError('PUBLIC_HTTP_UNAVAILABLE')
        if kind == 'time':
            return envelope({'ts': str(self.server)})
        if kind == 'instruments':
            return deepcopy(self.instrument)
        if kind == 'tickers':
            return deepcopy(self.ticker)
        if kind == 'ticker':
            reply = deepcopy(self.ticker)
            reply['data'][0]['instId'] = instrument
            return reply
        if kind == 'books':
            return deepcopy(self.books)
        if kind == 'candles':
            return candle_fixture(bar, close=self.btc_close if instrument == 'BTC-USDT' else self.short_close)
        raise AssertionError(kind)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_profile('v5-paper-500').policy
        self.client = SyntheticTR()
        self.service = TRMarketData(self.policy, client=self.client, clock_ms=lambda: NOW)

    def test_full_400_level_snapshot_volume_trend_and_depth(self):
        report = self.service.analyze('TEST-USDT')
        self.assertEqual(report['transport'], 'REST_SNAPSHOT')
        self.assertEqual(report['market']['quote_volume_24h'], '20000000')
        self.assertEqual(report['book']['bid_levels'], 400)
        self.assertEqual(report['book']['ask_levels'], 400)
        self.assertEqual(report['spread_bps'], '2.0000')
        self.assertEqual(report['trend']['return_15m'], '0.010000000000')
        self.assertEqual(report['book']['0.5pct']['bid'], str(sum((Decimal('99.99') - Decimal(i)/100)*10 for i in range(50))))
        self.assertTrue(report['book']['1pct']['ask_band_covered'])
        self.assertEqual(report['book']['top5']['bids']['notional'], '4998.50')
        self.assertEqual(report['filter_reasons'], [])
        self.assertFalse(report['live_enabled'])

    def test_shallow_capture_does_not_claim_full_band(self):
        self.client.books['data'][0]['bids'] = self.client.books['data'][0]['bids'][:1]
        report = self.service.analyze('TEST-USDT')
        self.assertFalse(report['book']['1pct']['bid_band_covered'])
        self.assertEqual(report['book']['bid_levels'], 1)

    def test_grid_uses_supplied_reviewed_template_with_fee_and_risk(self):
        result = self.service.grid(sample_plan(capital='90').to_dict())
        self.assertEqual(result['bot_risk_cap_usdt'], '5.00')
        self.assertEqual(result['total_risk_cap_usdt'], '15.00')
        self.assertEqual(result['action'], 'PROPOSAL_ONLY_NO_RESERVATION')
        self.assertTrue(all(Decimal(r) > 0 for r in result['net_grid_returns']))
        self.assertEqual(result['plan']['capital'], '90')

    def test_grid_cannot_bypass_instrument_rules_or_5usdt_cap(self):
        with self.assertRaisesRegex(TradingError, 'INSTRUMENT_RULE_MISMATCH'):
            self.service.grid(sample_plan(capital='90', tick_size='0.1').to_dict())
        with self.assertRaisesRegex(TradingError, 'BOT_RISK_LIMIT'):
            self.service.grid(sample_plan(capital='500').to_dict())

    def test_low_volume_and_downtrend_are_reported_and_filtered(self):
        self.client.ticker['data'][0]['volCcy24h'] = '100'
        self.client.short_close = '99'
        report = self.service.analyze('TEST-USDT')
        self.assertIn('LOW_QUOTE_VOLUME', report['filter_reasons'])
        self.assertIn('DOWNTREND', report['filter_reasons'])
        radar = self.service.radar(['TEST-USDT'])
        self.assertEqual(radar['accepted'], [])
        self.assertEqual(radar['rejected'][0]['instrument'], 'TEST-USDT')

    def test_btc_risk_off_blocks_grid(self):
        self.client.btc_close = '97'
        with self.assertRaisesRegex(TradingError, 'BTC_RISK_OFF'):
            self.service.grid(sample_plan(capital='90').to_dict())

    def test_clock_skew_and_missing_server_timestamp_fail_closed(self):
        self.client.server = NOW - 10000
        with self.assertRaisesRegex(TradingError, 'PUBLIC_CLOCK_SKEW'):
            self.service.analyze('TEST-USDT')
        self.assertFalse(self.service.last_health['ok'])
        with patch.object(self.client, 'fetch', return_value=envelope({})):
            with self.assertRaisesRegex(TradingError, 'INVALID_PUBLIC_DATA'):
                self.service.analyze('TEST-USDT')

    def test_stale_books_and_crossed_books_are_rejected(self):
        self.client.books['data'][0]['ts'] = str(NOW - 30001)
        with self.assertRaisesRegex(TradingError, 'STALE_PUBLIC_DATA'):
            self.service.analyze('TEST-USDT')
        self.client.books['data'][0]['ts'] = str(NOW - 100)
        self.client.books['data'][0]['bids'][0][0] = '100.02'
        with self.assertRaises(TradingError):
            self.service.analyze('TEST-USDT')

    def test_cursor_conflict_and_regression_no_silent_resync(self):
        self.service.analyze('TEST-USDT')
        self.client.books['data'][0]['seqId'] = 9
        with self.assertRaisesRegex(TradingError, 'OUT_OF_ORDER_BOOK'):
            self.service.analyze('TEST-USDT')
        self.client.books['data'][0]['seqId'] = 10
        self.client.books['data'][0]['bids'][0][1] = '11'
        with self.assertRaisesRegex(TradingError, 'CONFLICTING_BOOK_SEQUENCE'):
            self.service.analyze('TEST-USDT')

    def test_failed_fetch_has_no_fallback_or_old_analysis(self):
        self.service.analyze('TEST-USDT')
        self.client.fail_kind = 'books'
        with self.assertRaisesRegex(TradingError, 'PUBLIC_HTTP_UNAVAILABLE'):
            self.service.analyze('TEST-USDT')
        self.assertFalse(self.service.last_health['ok'])
        self.assertFalse(any(c[0] in {'account', 'order'} for c in self.client.calls))

    def test_btc_reuses_its_own_hour_and_universe_bounded(self):
        self.service.analyze('BTC-USDT')
        self.assertEqual(self.client.calls.count(('candles', 'BTC-USDT', '1H')), 1)
        for universe in ([], ['TEST-USDT']*2, ['TEST-USDT']*11):
            with self.assertRaises(TradingError):
                self.service.radar(universe)

    def test_capture_duration_bounded(self):
        with patch.object(self.service, 'clock_ms', side_effect=[NOW, NOW, NOW+10001, NOW+10001]):
            with self.assertRaisesRegex(TradingError, 'PUBLIC_CAPTURE_TOO_SLOW'):
                self.service.analyze('TEST-USDT')


class CommandHookTests(unittest.TestCase):
    def setUp(self):
        self.trader = PaperTrader(':memory:', profile=load_profile('v5-paper-500'))
        self.addCleanup(self.trader.close)
        self.client = SyntheticTR()
        self.service = TRMarketData(self.trader.policy, client=self.client, clock_ms=lambda: NOW)

    def update(self, text, uid=1):
        return {'update_id':uid, 'message':{'chat':{'id':42,'type':'private'}, 'from':{'id':43,'is_bot':False},
                'date':NOW//1000, 'text':text}}

    def handle(self, update, **kwargs):
        return handle_market_update(self.trader, self.service, update, allowed_chat=42, allowed_user=43,
                 now=NOW//1000, universe=['TEST-USDT','BTC-USDT'], **kwargs)

    def test_analysis_command_fields_and_no_ledger_trades(self):
        result = self.handle(self.update('/analiz TEST-USDT'))
        self.assertTrue(result['ok'])
        for text in ('DATA_UNVERIFIED', '400/400', 'Spread', '24h quote', '15m', 'BTC 1H', '0.5pct', 'Top5', 'duvarı', 'KAPALI'):
            self.assertIn(text, result['message'])
        self.assertNotIn('CANLI PİYASA VERİSİ', result['message'])
        self.assertLess(len(result['message']),3500)
        self.assertEqual(self.trader.status()['bots'],[])
        self.assertEqual(result['delivery'],'NOT_SENT')

    def test_radar_command_and_grid_command_template(self):
        radar=self.handle(self.update('/radar'))
        self.assertTrue(radar['ok'])
        self.assertIn('KABUL TEST-USDT',radar['message'])
        # Separate ledger prevents rate limiter/configuration drift affecting this independent command.
        with PaperTrader(':memory:',profile=load_profile('v5-paper-500')) as trader:
            grid=handle_market_update(trader,self.service,self.update('/grid'),allowed_chat=42,allowed_user=43,
                    now=NOW//1000,universe=['TEST-USDT'],grid_template=sample_plan(capital='90').to_dict())
            self.assertTrue(grid['ok'])
            self.assertIn('Sanal sermaye 90',grid['message'])
            self.assertIn('bot risk 5.00 / toplam 15.00',grid['message'])
            self.assertEqual(trader.status()['portfolio']['reserved_capital'],'0')

    def test_unauthorized_command_never_fetches(self):
        update=self.update('/analiz TEST-USDT')
        update['message']['from']['id']=44
        with self.assertRaisesRegex(TradingError,'TELEGRAM_UNAUTHORIZED'):
            self.handle(update)
        self.assertEqual(self.client.calls,[])

    def test_duplicate_returns_same_analysis_without_network(self):
        first=self.handle(self.update('/analiz TEST-USDT'))
        self.client.calls.clear()
        second=self.handle(self.update('/analiz TEST-USDT'))
        self.assertTrue(second['duplicate'])
        self.assertEqual(first['message'],second['message'])
        self.assertEqual(self.client.calls,[])

    def test_crash_after_accept_does_not_blindly_refetch(self):
        from okx_grid.telegram_control import handle_update
        update=self.update('/analiz TEST-USDT')
        handle_update(self.trader,update,allowed_chat=42,allowed_user=43,now=NOW//1000)
        result=self.handle(update)
        self.assertFalse(result['ok'])
        self.assertEqual(self.client.calls,[])
        self.assertIn('belirsiz',result['message'])

    def test_network_failure_returns_no_analysis_not_old_quote(self):
        self.client.fail_kind='ticker'
        result=self.handle(self.update('/analiz TEST-USDT'))
        self.assertFalse(result['ok'])
        self.assertIn('VERİ ALINAMADI - ANALİZ YOK',result['message'])
        self.assertNotIn('analysis',result)

    def test_unknown_symbol_and_missing_grid_template_no_network(self):
        result=self.handle(self.update('/analiz ALT-USDT'))
        self.assertEqual(result['reason'],'SYMBOL_NOT_ALLOWED')
        self.assertEqual(self.client.calls,[])

    def test_grid_missing_configuration_does_not_invent_prices(self):
        result=self.handle(self.update('/grid'))
        self.assertEqual(result['reason'],'REVIEWED_GRID_TEMPLATE_REQUIRED')
        self.assertEqual(self.client.calls,[])

    def test_status_not_checked_is_not_live_health(self):
        result=self.handle(self.update('/durum'))
        self.assertFalse(result['analysis']['market_connection']['ok'])
        self.assertIn('NOT_VERIFIED',result['message'])


class TransportExtensions(unittest.TestCase):
    def test_catalogue_tickers_time_requests_have_no_symbol_injection(self):
        self.assertEqual(request_spec('instruments')['params'],{'instType':'SPOT'})
        self.assertEqual(request_spec('tickers')['params'],{'instType':'SPOT'})
        self.assertEqual(request_spec('time')['params'],{})
        for kind in ('time','tickers'):
            with self.assertRaises(TradingError):
                request_spec(kind,'BTC-USDT')
        opener=FakeOpener()
        make_client(opener=opener).fetch('time')
        self.assertEqual(opener.calls[0][0].full_url,TR_ORIGIN+'/api/v5/public/time')

    def test_pacer_enforces_aggregate_quarter_second(self):
        clock=[0.0]
        sleeps=[]
        def sleep(delay):
            sleeps.append(delay)
            clock[0]+=delay
        pacer=RequestPacer(clock=lambda:clock[0],sleep=sleep)
        for _ in range(4):
            pacer.wait()
        self.assertEqual(sleeps,[0.25]*3)

    def test_timeout_retries_are_bounded(self):
        opener=FakeOpener(exception=TimeoutError())
        sleeps=[]
        client=ReadOnlyTRClient(opener=opener,pacer=InstantPacer(),sleep=sleeps.append,jitter=lambda:0.1)
        with self.assertRaisesRegex(TradingError,'PUBLIC_HTTP_UNAVAILABLE'):
            client.fetch('time')
        self.assertEqual(len(opener.calls),4)
        self.assertEqual(sleeps,[1.1,2.1,4.1])

    def test_tls_verification_failure_not_retried(self):
        opener=FakeOpener(exception=URLError(ssl.SSLError('bad certificate')))
        with self.assertRaisesRegex(TradingError,'PUBLIC_TLS_ERROR'):
            make_client(opener=opener).fetch('time')
        self.assertEqual(len(opener.calls),1)

    def test_default_tls_and_proxy_settings(self):
        from okx_grid.public_http import _default_opener
        from urllib.request import HTTPSHandler, ProxyHandler
        opener=_default_opener()
        https=next(h for h in opener.handlers if isinstance(h,HTTPSHandler))
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https._context.check_hostname)
        self.assertFalse(any(isinstance(h,ProxyHandler) and h.proxies for h in opener.handlers))

    def test_final_origin_rejected_even_if_injected_opener_follows_redirect(self):
        opener=FakeOpener()
        original=opener.open
        def opened(request,**kwargs):
            reply=original(request,**kwargs)
            reply.geturl=lambda:'https://www.okx.com/api/v5/public/time'
            return reply
        opener.open=opened
        with self.assertRaisesRegex(TradingError,'PUBLIC_REDIRECT_FORBIDDEN'):
            make_client(opener=opener).fetch('time')

    def test_429_retry_after_and_too_long_cooldown(self):
        opener=FakeOpener(exception=HTTPError(TR_ORIGIN,429,'ignored',{'Retry-After':'3'},io.BytesIO()))
        sleeps=[]
        client=ReadOnlyTRClient(opener=opener,pacer=InstantPacer(),sleep=sleeps.append,jitter=lambda:0)
        with self.assertRaisesRegex(TradingError,'PUBLIC_RETRY_EXHAUSTED'):
            client.fetch('time')
        self.assertEqual(sleeps,[3.0,3.0,4.0])
        opener.exception=HTTPError(TR_ORIGIN,429,'ignored',{'Retry-After':'31'},io.BytesIO())
        with self.assertRaisesRegex(TradingError,'PUBLIC_COOLDOWN_REQUIRED'):
            make_client(opener=opener).fetch('time')

    def test_nan_json_and_nonretryable_403_rejected(self):
        with self.assertRaisesRegex(TradingError,'PUBLIC_INVALID_JSON'):
            make_client(opener=FakeOpener(FakeResponse(b'{"code":"0","data":[NaN]}'))).fetch('time')
        opener=FakeOpener(exception=HTTPError(TR_ORIGIN,403,'ignored',{},io.BytesIO()))
        with self.assertRaisesRegex(TradingError,'PUBLIC_HTTP_UNAVAILABLE'):
            make_client(opener=opener).fetch('time')
        self.assertEqual(len(opener.calls),1)

class EndToEndAndSmoke(unittest.TestCase):
    def test_get_transport_normalization_to_authorized_command(self):
        from urllib.parse import parse_qs, urlsplit
        upstream = SyntheticTR()
        class Router:
            def open(self, request, *, timeout):
                parsed = urlsplit(request.full_url)
                params = parse_qs(parsed.query)
                self_case.assertEqual(request.get_method(), 'GET')
                self_case.assertEqual(parsed.netloc, 'tr.okx.com')
                if parsed.path.endswith('/books'):
                    self_case.assertEqual(params['sz'], ['400'])
                kind = parsed.path.rsplit('/',1)[-1]
                response = FakeResponse(upstream.fetch(kind, params.get('instId',[None])[0], bar=params.get('bar',[None])[0]))
                response.geturl = lambda: request.full_url
                return response
        self_case = self
        client = make_client(opener=Router())
        with PaperTrader(':memory:', profile=load_profile('v5-paper-500')) as trader:
            service = TRMarketData(trader.policy, client=client, clock_ms=lambda:NOW)
            update = {'update_id':1,'message':{'chat':{'id':42,'type':'private'},'from':{'id':43,'is_bot':False},'date':NOW//1000,'text':'/analiz TEST-USDT'}}
            with patch('socket.create_connection',side_effect=AssertionError('offline only')):
                result = handle_market_update(trader, service, update, allowed_chat=42, allowed_user=43, now=NOW//1000, universe=['TEST-USDT'])
            self.assertTrue(result['ok'])
            self.assertEqual(result['analysis']['book']['bid_levels'],400)
            self.assertEqual(trader.status()['bots'],[])

    def test_smoke_requires_explicit_network_flag(self):
        from tools.public_smoke import main
        with patch('sys.stderr',io.StringIO()), patch('socket.create_connection',side_effect=AssertionError('no network')):
            with self.assertRaises(SystemExit) as ctx:
                main([])
        self.assertEqual(ctx.exception.code,2)

    def test_smoke_failure_is_sanitized_and_not_success(self):
        from tools.public_smoke import main
        output = io.StringIO()
        with patch('tools.public_smoke.TRMarketData') as service, patch('sys.stdout',output):
            service.return_value.analyze.side_effect=TradingError('PUBLIC_HTTP_UNAVAILABLE')
            code=main(['--allow-public-network','--symbol','BTC-USDT'])
        self.assertEqual(code,2)
        value=json.loads(output.getvalue())
        self.assertFalse(value['ok'])
        self.assertFalse(value['live_enabled'])
        self.assertNotIn('price',value)

    def test_status_health_expires(self):
        with PaperTrader(':memory:',profile=load_profile('v5-paper-500')) as trader:
            service=TRMarketData(trader.policy,client=SyntheticTR(),clock_ms=lambda:NOW+40000)
            service.last_health={'ok':True,'checked_at_ms':NOW,'live_enabled':False}
            update={'update_id':1,'message':{'chat':{'id':42,'type':'private'},'from':{'id':43,'is_bot':False},'date':NOW//1000,'text':'/durum'}}
            result=handle_market_update(trader,service,update,allowed_chat=42,allowed_user=43,now=NOW//1000,universe=['TEST-USDT'])
            self.assertEqual(result['analysis']['market_connection']['reason'],'STALE_CONNECTION_HEALTH')
            self.assertFalse(result['analysis']['market_connection']['ok'])


if __name__ == '__main__':
    unittest.main()
