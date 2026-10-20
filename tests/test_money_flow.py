"""V6 synthetic fixtures: no holdings, real APIs, Telegram delivery or orders."""
from copy import deepcopy
from decimal import Decimal
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.control import notices
from okx_grid.domain import TradingError
from okx_grid.flow_contracts import (BARS, FlowPolicy, book_snapshot, candle_indicators, candles, discover,
                                     fx_rate, quote_to_usdt, score_flow, taker_flow)
from okx_grid.flow_replay import replay_roundtrip
from okx_grid.money_flow import MoneyFlowScanner, aggregate_coins
from okx_grid.paper import PaperTrader
from okx_grid.profiles import load_profile
from okx_grid.public_contracts import TR_ORIGIN, request_spec
from okx_grid.telegram_flow import handle_flow_update
from tests.test_public_contracts import NOW, envelope
from tests.test_public_http import FakeOpener, make_client


def info(name):
    base,quote=name.split('-')
    return {'instId':name,'baseCcy':base,'quoteCcy':quote,'state':'live','instType':'SPOT',
            'tickSz':'0.01','lotSz':'0.001','minSz':'0.001'}


def bar_reply(bar, *, close='102', rvol='4'):
    period=BARS[bar]
    edge=NOW//period*period
    result=[[str(edge),'100','105','95','100','1','100','100','0']]
    for i in range(1,22 if bar=='5m' else 2):
        volume=str(Decimal('100')*Decimal(rvol)) if i==1 else '100'
        result.append([str(edge-i*period),'100','105','95',close,'1',volume,volume,'1'])
    return envelope(*result)


def trade_reply(name='TEST-USDT', *, quote_scale=1):
    return envelope({'instId':name,'tradeId':'30','side':'buy','px':str(100*quote_scale),'sz':'9','ts':str(NOW-100)},
                    {'instId':name,'tradeId':'29','side':'sell','px':str(100*quote_scale),'sz':'1','ts':str(NOW-200)},
                    {'instId':name,'tradeId':'28','side':'buy','px':str(100*quote_scale),'sz':'1','ts':str(NOW-300001)})


def book_reply(*, scale=1, at=NOW-100, seq=10):
    return envelope({'ts':str(at),'seqId':seq,'bids':[[str(Decimal('99.99')*scale),'1000','0','2']],
                     'asks':[[str(Decimal('100.01')*scale),'1000','0','2']]})


class FakeFlowAPI:
    def __init__(self,names=('BTC-USDT','TEST-USDT')):
        self.names=list(names)
        self.calls=[]
        self.fail=None
        self.rvol='4'
        self.close='102'
        self.btc_close='101'
        self.volume='20000000'
        self.seq=10
        self.book_at=NOW-100
        self.book_size='1000'
        self.recent_boundary=True
        self.history_stalled=False

    def ticker(self,name):
        if name=='USDT-TRY':
            bid,ask,last='39.999','40.001','40'
        else:
            scale=40 if name.endswith('-TRY') else 1
            bid,ask,last=(str(Decimal(v)*scale) for v in ('99.99','100.01','100'))
        return {'instId':name,'instType':'SPOT','last':last,'bidPx':bid,'askPx':ask,'ts':str(NOW-100),
                'vol24h':'999999999','volCcy24h':str(Decimal(self.volume)*(40 if name.endswith('-TRY') else 1))}

    def fetch(self,kind,instrument=None,*,bar=None,after=None):
        self.calls.append((kind,instrument,bar,after))
        if kind==self.fail:
            raise TradingError('PUBLIC_HTTP_UNAVAILABLE')
        if kind=='time':
            return envelope({'ts':str(NOW)})
        if kind=='instruments':
            return envelope(*(info(n) for n in self.names))
        if kind=='tickers':
            return envelope(*(self.ticker(n) for n in self.names))
        if kind=='flow_ticker':
            return envelope(self.ticker(instrument))
        if kind=='flow_candles':
            return bar_reply(bar,close=self.btc_close if instrument=='BTC-USDT' and bar=='1H' else self.close,rvol=self.rvol)
        if kind=='flow_books':
            book=book_reply(scale=40 if instrument.endswith('-TRY') else 1,at=self.book_at,seq=self.seq)
            for side in ('bids','asks'):
                book['data'][0][side][0][1]=self.book_size
            return book
        if kind=='trades':
            reply=trade_reply(instrument,quote_scale=40 if instrument.endswith('-TRY') else 1)
            if not self.recent_boundary:
                reply['data'].pop()
            return reply
        if kind=='history_trades':
            reply=trade_reply(instrument,quote_scale=40 if instrument.endswith('-TRY') else 1)
            if self.history_stalled:
                reply['data'].pop()
            return reply
        raise AssertionError(kind)


def scanner(api=None,policy=None):
    return MoneyFlowScanner(client=api or FakeFlowAPI(),policy=policy,clock_ms=lambda:NOW)


class FlowContracts(unittest.TestCase):
    def test_discovery_only_actual_live_supported_quote_pairs(self):
        reply=envelope(info('TEST-USDT'),info('TEST-TRY'),info('OTHER-EUR'))
        pairs,excluded=discover(reply)
        self.assertEqual(set(pairs),{'TEST-USDT','TEST-TRY'})
        self.assertEqual(excluded,[{'instrument':'OTHER-EUR','reason':'UNSUPPORTED_QUOTE_FX'}])
        reply['data'][0]['state']='suspend'
        self.assertNotIn('TEST-USDT',discover(reply)[0])

    def test_discovery_duplicate_and_derivative_fail_closed(self):
        with self.assertRaisesRegex(TradingError,'DUPLICATE_FLOW_INSTRUMENT'):
            discover(envelope(info('TEST-USDT'),info('TEST-USDT')))
        with self.assertRaises(TradingError):
            discover(envelope({**info('TEST-USDT'),'instId':'TEST-USDT-SWAP'}))

    def test_try_midpoint_conversion_not_base_volume_multiplication(self):
        api=FakeFlowAPI(('BTC-USDT','USDT-TRY','TEST-TRY'))
        pairs,_=discover(api.fetch('instruments'))
        fx=fx_rate(pairs,api.fetch('tickers'),NOW)
        self.assertEqual(quote_to_usdt('4000','TRY',fx,NOW),100)
        self.assertEqual(quote_to_usdt('4000','USDT',None,NOW),4000)
        self.assertEqual(fx['source'],TR_ORIGIN+'/api/v5/market/tickers')

    def test_fx_missing_stale_untrusted_and_spread_rejected(self):
        with self.assertRaisesRegex(TradingError,'TRY_FX_UNAVAILABLE'):
            quote_to_usdt('1','TRY',None,NOW)
        fx={'source':'https://www.okx.com','instrument':'USDT-TRY','try_per_usdt':'40','as_of_ms':NOW}
        with self.assertRaises(TradingError):
            quote_to_usdt('1','TRY',fx,NOW)
        fx['source']=TR_ORIGIN+'/api/v5/market/tickers'
        fx['as_of_ms']=NOW-30001
        with self.assertRaisesRegex(TradingError,'STALE_OR_FUTURE_FLOW_DATA'):
            quote_to_usdt('1','TRY',fx,NOW)
        api=FakeFlowAPI(('USDT-TRY',))
        pairs,_=discover(api.fetch('instruments'))
        tickers=api.fetch('tickers');tickers['data'][0]['askPx']='42'
        with self.assertRaisesRegex(TradingError,'TRY_FX_SPREAD'):
            fx_rate(pairs,tickers,NOW)

    def test_cvd_taker_buy_is_positive_and_quote_window_exact(self):
        result=taker_flow([trade_reply()],'TEST-USDT','USDT',None,start_ms=NOW-300000,end_ms=NOW)
        self.assertEqual(result['buy_usdt'],'900')
        self.assertEqual(result['sell_usdt'],'100')
        self.assertEqual(result['cvd_quote_usdt'],'800')
        self.assertEqual(result['base_taker_delta'],'8')
        self.assertEqual(result['buy_sell_ratio'],'9')
        self.assertIn('NOT_NET_DEPOSITS',result['meaning'])

    def test_sell_dominance_negative_and_all_buy_ratio_explicit(self):
        reply=trade_reply()
        reply['data'][0]['side']='sell'
        negative=taker_flow([reply],'TEST-USDT','USDT',None,start_ms=NOW-300000,end_ms=NOW)
        self.assertEqual(negative['cvd_quote_usdt'],'-1000')
        reply['data'][0]['side']='buy';reply['data'][1]['side']='buy'
        result=taker_flow([reply],'TEST-USDT','USDT',None,start_ms=NOW-300000,end_ms=NOW)
        self.assertIsNone(result['buy_sell_ratio'])
        self.assertEqual(result['ratio_state'],'ALL_BUY')

    def test_duplicate_trades_not_counted_twice_and_conflicts_rejected(self):
        first=trade_reply()
        result=taker_flow([first,deepcopy(first)],'TEST-USDT','USDT',None,start_ms=NOW-300000,end_ms=NOW)
        self.assertEqual(result['cvd_quote_usdt'],'800')
        self.assertEqual(result['unique_trades'],3)
        changed=deepcopy(first);changed['data'][0]['sz']='10'
        with self.assertRaisesRegex(TradingError,'CONFLICTING_FLOW_TRADE'):
            taker_flow([first,changed],'TEST-USDT','USDT',None,start_ms=NOW-300000,end_ms=NOW)

    def test_incomplete_trade_window_is_not_full_5min_cvd(self):
        reply=trade_reply();reply['data'].pop()
        with self.assertRaisesRegex(TradingError,'INCOMPLETE_FLOW_TRADE_WINDOW'):
            taker_flow([reply],'TEST-USDT','USDT',None,start_ms=NOW-300000,end_ms=NOW)

    def test_bad_taker_side_or_other_pair_rejected(self):
        for key,value in (('side','maker'),('instId','OTHER-USDT'),('sz','-1'),('tradeId','../x')):
            reply=trade_reply();reply['data'][0][key]=value
            with self.subTest(key=key),self.assertRaises(TradingError):
                taker_flow([reply],'TEST-USDT','USDT',None,start_ms=NOW-300000,end_ms=NOW)

    def test_rvol_excludes_current_incomplete_and_target_from_baseline(self):
        replies={bar:bar_reply(bar) for bar in BARS}
        replies['5m']['data'][0][7]='999999999'
        result=candle_indicators(replies,NOW)
        self.assertEqual(result['rvol_5m'],'4')
        self.assertEqual(Decimal(result['return_15m']),Decimal('.02'))
        self.assertEqual(result['bar_end_ms']['5m'],NOW//300000*300000)

    def test_missing_history_gap_zero_baseline_and_confirmation_rejected(self):
        for issue in ('short','gap','zero','confirm'):
            reply=bar_reply('5m')
            if issue=='short': reply['data'].pop()
            if issue=='gap': reply['data'].pop(2)
            if issue=='zero':
                for row in reply['data'][2:]: row[7]='0'
            if issue=='confirm':reply['data'][1][8]='0'
            with self.subTest(issue=issue),self.assertRaises(TradingError):
                if issue=='zero':
                    candle_indicators({bar:reply if bar=='5m' else bar_reply(bar) for bar in BARS},NOW)
                else: candles(reply,'5m',NOW)

    def test_book_sequence_regression_conflict_and_reconnect(self):
        first=book_snapshot(book_reply(),'TEST-USDT',NOW)
        for seq,size in ((9,'1000'),(10,'1001')):
            reply=book_reply(seq=seq);reply['data'][0]['bids'][0][1]=size
            with self.assertRaisesRegex(TradingError,'FLOW_BOOK_RESYNC_REQUIRED'):
                book_snapshot(reply,'TEST-USDT',NOW,previous=first['cursor'])
        self.assertEqual(book_snapshot(book_reply(seq=100),'TEST-USDT',NOW,previous=first['cursor'])['cursor']['sequence'],100)

    def test_book_empty_crossed_unsorted_and_stale(self):
        for change in ({'bids':[]},{'bids':[['101','1','0','1']]},{'ts':str(NOW-30001)},
                       {'bids':[['99.99','1','0','1'],['100','1','0','1']]}):
            reply=book_reply();reply['data'][0].update(change)
            with self.subTest(change=change),self.assertRaises(TradingError):
                book_snapshot(reply,'TEST-USDT',NOW)

    def test_config_thresholds_validated(self):
        self.assertEqual(FlowPolicy(min_rvol='4').min_rvol,4)
        for change in ({'min_rvol':'0'},{'min_return':'.1','max_return':'.01'},{'paper_notional':'501'},{'min_buy_fraction':'1'}):
            with self.assertRaises(TradingError): FlowPolicy(**change)


class FlowScannerTests(unittest.TestCase):
    def test_scan_top_rank_has_breakdown_without_any_live_permission(self):
        result=scanner().scan_batch()
        self.assertEqual(result['discovered_pairs'],2)
        self.assertEqual(len(result['top5']),2)
        for coin in result['top5']:
            self.assertEqual(coin['score'],sum(coin['score_breakdown'].values()))
            self.assertGreater(coin['leader']['execution']['base_quantity'],'0')
            self.assertFalse(coin['live_enabled'])
        self.assertIn('BOUNDED_BATCH',result['scope'])

    def test_target_coin_fetches_its_actual_pairs_not_another_batch(self):
        api=FakeFlowAPI(('BTC-USDT','OTHER-USDT','TEST-USDT'))
        result=scanner(api).scan_batch(target='TEST')
        self.assertEqual(result['batch_pairs'],['TEST-USDT'])
        self.assertEqual(result['coins'][0]['coin'],'TEST')
        with self.assertRaisesRegex(TradingError,'FLOW_SYMBOL_NOT_DISCOVERED'):
            scanner(api).scan_batch(target='MISSING')

    def test_aggregate_try_usdt_same_coin_once(self):
        api=FakeFlowAPI(('BTC-USDT','TEST-USDT','TEST-TRY','USDT-TRY'))
        result=scanner(api).scan_batch(target='TEST')
        self.assertEqual(len(result['coins']),1)
        coin=result['coins'][0]
        self.assertEqual(coin['pairs'],['TEST-USDT','TEST-TRY'])
        self.assertEqual(coin['cvd_quote_usdt'],'1600')
        self.assertTrue(coin['coverage_complete'])
        self.assertEqual(coin['leader']['volume_24h_usdt'],'20000000')
        self.assertNotIn('USDT',[c['coin'] for c in result['coins']])

    def test_partial_coin_pair_coverage_cannot_rank(self):
        api=FakeFlowAPI(('BTC-USDT','TEST-USDT','TEST-TRY','USDT-TRY'))
        result=scanner(api).scan_batch(offset=1,limit=1)
        self.assertFalse(result['coins'][0]['eligible'])
        self.assertIn('INCOMPLETE_COIN_PAIR_COVERAGE',result['coins'][0]['reasons'])

    def test_unavailable_fx_does_not_invent_rate_or_rank_try(self):
        api=FakeFlowAPI(('BTC-USDT','TEST-TRY'))
        result=scanner(api).scan_batch()
        self.assertEqual(result['pairs']['TEST-TRY']['error'],'TRY_FX_UNAVAILABLE')
        self.assertEqual(result['top5'],[])
        self.assertTrue(result['volume_errors'])

    def test_pagination_reaches_boundary_and_deduplicates_overlap(self):
        api=FakeFlowAPI();api.recent_boundary=False
        result=scanner(api).scan_batch(target='TEST')
        self.assertTrue(any(c[0]=='history_trades' and c[3]=='29' for c in api.calls))
        self.assertEqual(result['pairs']['TEST-USDT']['flow']['cvd_quote_usdt'],'800')

    def test_pagination_stall_blocks_instead_of_fabricating_cvd(self):
        api=FakeFlowAPI();api.recent_boundary=False;api.history_stalled=True
        result=scanner(api).scan_batch(target='TEST')
        self.assertEqual(result['pairs']['TEST-USDT']['error'],'FLOW_PAGINATION_STALLED')
        self.assertEqual(result['top5'],[])

    def test_reconnect_latches_resync_and_does_not_silently_resume(self):
        flow=scanner();flow.reconnect('TEST-USDT')
        first=flow.scan_batch(target='TEST')
        self.assertEqual(first['pairs']['TEST-USDT']['error'],'FLOW_BOOK_RESYNC_REQUIRED')
        self.assertEqual(flow.scan_batch(target='TEST')['top5'],[])

    def test_btc_shock_and_extreme_chase_rejected(self):
        for field,value,reason in (('btc_close','97','BTC_SHOCK'),('close','105.1','OUTSIDE_EARLY_MOMENTUM'),('rvol','2','LOW_RVOL')):
            api=FakeFlowAPI();setattr(api,field,value)
            # Expand synthetic OHLC upper bound for the deliberately excessive return.
            if field=='close':
                original=api.fetch
                def fetch(*args,**kwargs):
                    reply=original(*args,**kwargs)
                    if args[0]=='flow_candles':
                        for row in reply['data']:row[2]='110'
                    return reply
                api.fetch=fetch
            result=scanner(api).scan_batch(target='TEST')
            self.assertIn(reason,result['coins'][0]['reasons'])
            self.assertEqual(result['top5'],[])

    def test_low_depth_and_excessive_slippage_rejected(self):
        api=FakeFlowAPI();api.book_size='0.01'
        result=scanner(api).scan_batch(target='TEST')
        self.assertEqual(result['pairs']['TEST-USDT']['error'],'FLOW_ASK_CAPACITY')
        self.assertEqual(result['top5'],[])

    def test_universe_change_restarts_no_shifted_cursor(self):
        api=FakeFlowAPI();flow=scanner(api)
        result=flow.scan_batch(limit=1)
        api.names.append('NEW-USDT')
        with self.assertRaisesRegex(TradingError,'FLOW_UNIVERSE_CHANGED_RESTART_SCAN'):
            flow.scan_batch(offset=1,expected_universe=result['universe_hash'])

    def test_rotation_compares_same_universe_and_rejects_incompatible_baseline(self):
        flow=scanner();first=flow.scan_batch()
        baseline=deepcopy(first['rotation_baseline']);baseline['at_ms']-=1000
        result=flow.scan_batch(previous=baseline)
        self.assertEqual(Decimal(result['coins'][0]['share_change']),0)
        baseline['universe_hash']='other'
        with self.assertRaisesRegex(TradingError,'INCOMPARABLE_FLOW_ROTATION_BASELINE'):
            flow.scan_batch(previous=baseline)

    def test_transport_error_never_old_rank_or_global_fallback(self):
        api=FakeFlowAPI();api.fail='flow_books'
        result=scanner(api).scan_batch()
        self.assertEqual(result['top5'],[])
        self.assertTrue(all(r['error']=='PUBLIC_HTTP_UNAVAILABLE' for r in result['pairs'].values()))
        self.assertFalse(any(c[0] in {'order','account'} for c in api.calls))

    def test_invalid_batch_and_missing_btc(self):
        for offset,limit in ((-1,1),(0,0),(0,11),(True,1)):
            with self.assertRaises(TradingError):scanner().scan_batch(offset=offset,limit=limit)
        with self.assertRaisesRegex(TradingError,'BTC_REGIME_UNAVAILABLE'):
            scanner(FakeFlowAPI(('TEST-USDT',))).scan_batch()


class FlowCommands(unittest.TestCase):
    def setUp(self):
        self.trader=PaperTrader(':memory:',profile=load_profile('v5-paper-500'))
        self.addCleanup(self.trader.close)
        self.api=FakeFlowAPI()
        self.flow=scanner(self.api)

    def update(self,text='/paraakisi',uid=1,now=NOW//1000):
        return {'update_id':uid,'message':{'chat':{'id':42,'type':'private'},'from':{'id':43,'is_bot':False},'date':now,'text':text}}

    def handle(self,update,now=NOW//1000,**kwargs):
        return handle_flow_update(self.trader,self.flow,update,allowed_chat=42,allowed_user=43,now=now,**kwargs)

    def test_paraakisi_source_timestamp_breakdown_and_outbox(self):
        with patch('socket.create_connection',side_effect=AssertionError('offline only')):
            result=self.handle(self.update())
        self.assertTrue(result['ok'])
        for value in ('https://tr.okx.com','as-of ms','bileşenleri','CVD','bakiyesi değildir','KAPALI'):
            self.assertIn(value,result['message'])
        self.assertEqual(result['delivery'],'NOT_SENT')
        self.assertEqual(len(notices(self.trader)),1)
        self.assertEqual(self.trader.status()['bots'],[])

    def test_symbol_analysis_does_not_guess_targets_or_reward(self):
        result=self.handle(self.update('/analiz TEST'))
        self.assertTrue(result['ok'])
        self.assertIn('HESAPLANMADI',result['message'])
        self.assertIn('Fiyat 100 USDT',result['message'])
        self.assertIn('Book ts',result['message'])
        self.assertIn('TEST (TEST-USDT)',result['message'])

    def test_unauthorized_forwarded_or_old_never_fetch(self):
        for change in ({'from':{'id':44,'is_bot':False}},{'forward_origin':{}},{'date':0}):
            update=self.update();update['message'].update(change)
            with self.assertRaises(TradingError):self.handle(update)
        self.assertEqual(self.api.calls,[])

    def test_duplicate_does_not_fetch_or_queue_again(self):
        self.handle(self.update())
        self.api.calls.clear()
        result=self.handle(self.update())
        self.assertTrue(result['duplicate'])
        self.assertEqual(self.api.calls,[])
        self.assertEqual(len(notices(self.trader)),1)

    def test_conflicting_update_rejected(self):
        self.handle(self.update())
        with self.assertRaisesRegex(TradingError,'TELEGRAM_UPDATE_CONFLICT'):
            self.handle(self.update('/radar'))

    def test_cooldown_is_per_coin_durable_and_not_successful_delivery(self):
        first=self.handle(self.update('/radar'))
        second=self.handle(self.update('/radar',uid=2,now=NOW//1000+1),now=NOW//1000+1)
        self.assertTrue(first['alerts'])
        self.assertEqual(second['alerts'],[])
        self.assertEqual(set(second['suppressed']),set(first['alerts']))
        self.assertNotIn('ADAY\n',second['message'])
        self.assertEqual(second['delivery'],'NOT_SENT')

    def test_failure_message_contains_no_old_score(self):
        self.api.fail='time'
        result=self.handle(self.update())
        self.assertFalse(result['ok'])
        self.assertIn('VERİ ALINAMADI - ANALİZ YOK',result['message'])
        self.assertNotIn('scan',result)

    def test_grid_is_delegated_to_existing_market_handler(self):
        service=object()
        with patch('okx_grid.telegram_flow.handle_market_update',return_value={'ok':True,'route':'grid'}) as route:
            result=self.handle(self.update('/grid'),market_service=service,universe=['TEST-USDT'])
            self.assertEqual(result['route'],'grid')
            self.assertEqual(route.call_args.args[1],service)
        self.assertEqual(self.api.calls,[])

    def test_restart_keeps_receipt_and_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'paper.sqlite3'
            for iteration in range(2):
                with PaperTrader(path,profile=load_profile('v5-paper-500')) as trader:
                    result=handle_flow_update(trader,self.flow,self.update(),allowed_chat=42,allowed_user=43,now=NOW//1000)
                    self.assertEqual(result['duplicate'],bool(iteration))
                    self.assertEqual(len(notices(trader)),1)

    def test_live_environment_rejected_before_scan(self):
        with patch.dict('os.environ',{'TRADING_MODE':'live'}):
            with self.assertRaisesRegex(TradingError,'LIVE_TRADING_DISABLED'):
                self.handle(self.update())
        self.assertEqual(self.api.calls,[])


class FlowTransportAndReplay(unittest.TestCase):
    def test_new_get_contracts_and_numeric_history_cursor(self):
        opener=FakeOpener();client=make_client(opener=opener)
        client.fetch('history_trades','TEST-TRY',after='123')
        self.assertIn('after=123',opener.calls[-1][0].full_url)
        self.assertIn('type=1',opener.calls[-1][0].full_url)
        self.assertEqual(request_spec('trades','TEST-TRY')['params']['limit'],'500')
        for bar in BARS:
            self.assertEqual(request_spec('flow_candles','TEST-TRY',bar=bar)['params']['limit'],'22')
        for kind,after in (('ticker','1'),('history_trades','1&url=x'),('history_trades',True)):
            with self.assertRaises(TradingError):client.fetch(kind,'TEST-USDT',after=after)
        with self.assertRaises(TradingError):client.fetch('trades','TEST-USDT-SWAP')

    def test_replay_flat_price_loses_fees_and_spread(self):
        result=replay_roundtrip(book_reply(),book_reply(at=NOW+1000),instrument='TEST-USDT',entry_ms=NOW,exit_ms=NOW+1000)
        self.assertLess(Decimal(result['net_pnl_usdt']),0)
        no_fee=replay_roundtrip(book_reply(),book_reply(at=NOW+1000),instrument='TEST-USDT',entry_ms=NOW,exit_ms=NOW+1000,fee='0')
        self.assertGreater(Decimal(no_fee['net_pnl_usdt']),Decimal(result['net_pnl_usdt']))
        self.assertFalse(result['live_enabled'])

    def test_replay_profitable_exit_and_adverse_gap(self):
        good=replay_roundtrip(book_reply(),book_reply(scale=Decimal('1.02'),at=NOW+1000),instrument='TEST-USDT',entry_ms=NOW,exit_ms=NOW+1000)
        bad=replay_roundtrip(book_reply(),book_reply(scale=Decimal('.9'),at=NOW+1000),instrument='TEST-USDT',entry_ms=NOW,exit_ms=NOW+1000)
        self.assertGreater(Decimal(good['net_pnl_usdt']),0)
        self.assertLess(Decimal(bad['net_pnl_usdt']),Decimal('-5'))

    def test_replay_never_uses_future_for_entry_and_rejects_missing_exit_depth(self):
        with self.assertRaises(TradingError):
            replay_roundtrip(book_reply(),book_reply(),instrument='TEST-USDT',entry_ms=NOW,exit_ms=NOW)
        exit_book=book_reply(at=NOW+1000);exit_book['data'][0]['bids'][0][1]='0.0001'
        with self.assertRaisesRegex(TradingError,'REPLAY_EXIT_DEPTH_INCOMPLETE'):
            replay_roundtrip(book_reply(),exit_book,instrument='TEST-USDT',entry_ms=NOW,exit_ms=NOW+1000)

class FlowEdgeAndIntegrationTests(unittest.TestCase):
    def test_try_cvd_matches_equivalent_usdt_executions(self):
        api=FakeFlowAPI(('BTC-USDT','TEST-TRY','USDT-TRY'))
        pairs,_=discover(api.fetch('instruments'))
        fx=fx_rate(pairs,api.fetch('tickers'),NOW)
        result=taker_flow([trade_reply('TEST-TRY',quote_scale=40)],'TEST-TRY','TRY',fx,start_ms=NOW-300000,end_ms=NOW)
        self.assertEqual(result['cvd_quote_usdt'],'800')
        with self.assertRaisesRegex(TradingError,'FLOW_QUOTE_MISMATCH'):
            taker_flow([trade_reply('TEST-TRY')],'TEST-TRY','USDT',None,start_ms=NOW-300000,end_ms=NOW)

    def test_spoofable_positive_resting_book_without_taker_confirmation_cannot_rank(self):
        flow=scanner();result=flow.scan_batch(target='TEST')
        report=result['pairs']['TEST-USDT']
        report['flow']['cvd_quote_usdt']='-100'
        report['flow']['buy_fraction']='0.2'
        report['book']['depth']['0.5pct']['imbalance']='0.99'
        result=score_flow(report,FlowPolicy(),'0')
        self.assertFalse(result['eligible'])
        self.assertIn('NO_TAKER_CONFIRMATION',result['reasons'])

    def test_spread_and_slippage_filter(self):
        api=FakeFlowAPI()
        original=api.fetch
        def fetch(kind,instrument=None,**kwargs):
            reply=original(kind,instrument,**kwargs)
            if kind=='flow_books':reply['data'][0]['asks'][0][0]='101'
            if kind=='flow_ticker':reply['data'][0]['askPx']='101'
            return reply
        api.fetch=fetch
        result=scanner(api).scan_batch(target='TEST')
        self.assertIn('SPREAD_TOO_WIDE',result['coins'][0]['reasons'])
        self.assertIn('EXCESSIVE_SLIPPAGE',result['coins'][0]['reasons'])

    def test_trade_pagination_limit_fails_closed(self):
        api=FakeFlowAPI();api.recent_boundary=False
        original=api.fetch
        counter=[28]
        def fetch(kind,instrument=None,**kwargs):
            reply=original(kind,instrument,**kwargs)
            if kind=='history_trades':
                counter[0]-=1
                reply=envelope({'instId':instrument,'tradeId':str(counter[0]),'side':'buy','px':'100','sz':'1','ts':str(NOW-1000)})
            return reply
        api.fetch=fetch
        result=scanner(api).scan_batch(target='TEST')
        self.assertEqual(result['pairs']['TEST-USDT']['error'],'INCOMPLETE_FLOW_TRADE_WINDOW')
        self.assertEqual(sum(c[0]=='history_trades' for c in api.calls),10)

    def test_stale_clock_or_ticker_no_rank(self):
        api=FakeFlowAPI()
        original=api.fetch
        def fetch(kind,*args,**kwargs):
            reply=original(kind,*args,**kwargs)
            if kind=='time':reply['data'][0]['ts']=str(NOW-10000)
            return reply
        api.fetch=fetch
        with self.assertRaisesRegex(TradingError,'PUBLIC_CLOCK_SKEW'):
            scanner(api).scan_batch()

    def test_http_get_to_flow_command_end_to_end(self):
        from urllib.parse import urlsplit,parse_qs
        upstream=FakeFlowAPI()
        testcase=self
        class Router:
            def open(self,request,*,timeout):
                parts=urlsplit(request.full_url)
                testcase.assertEqual(parts.netloc,'tr.okx.com')
                testcase.assertEqual(request.get_method(),'GET')
                testcase.assertNotIn('Authorization',request.headers)
                query=parse_qs(parts.query)
                kind=parts.path.rsplit('/',1)[1]
                kind={'ticker':'flow_ticker','books':'flow_books','candles':'flow_candles','history-trades':'history_trades'}.get(kind,kind)
                from tests.test_public_http import FakeResponse
                response=FakeResponse(upstream.fetch(kind,query.get('instId',[None])[0],bar=query.get('bar',[None])[0],after=query.get('after',[None])[0]))
                response.geturl=lambda:request.full_url
                return response
        flow=MoneyFlowScanner(client=make_client(opener=Router()),clock_ms=lambda:NOW)
        with PaperTrader(':memory:',profile=load_profile('v5-paper-500')) as trader:
            update={'update_id':1,'message':{'chat':{'id':42,'type':'private'},'from':{'id':43,'is_bot':False},'date':NOW//1000,'text':'/paraakisi'}}
            with patch('socket.create_connection',side_effect=AssertionError('offline only')):
                result=handle_flow_update(trader,flow,update,allowed_chat=42,allowed_user=43,now=NOW//1000)
            self.assertTrue(result['ok'])
            self.assertTrue(result['scan']['top5'])
            self.assertEqual(trader.status()['portfolio']['reserved_capital'],'0')

    def test_crash_after_auth_does_not_refetch(self):
        from okx_grid.telegram_control import handle_update
        with PaperTrader(':memory:',profile=load_profile('v5-paper-500')) as trader:
            api=FakeFlowAPI();flow=scanner(api)
            update={'update_id':1,'message':{'chat':{'id':42,'type':'private'},'from':{'id':43,'is_bot':False},'date':NOW//1000,'text':'/paraakisi'}}
            handle_update(trader,update,allowed_chat=42,allowed_user=43,now=NOW//1000)
            result=handle_flow_update(trader,flow,update,allowed_chat=42,allowed_user=43,now=NOW//1000)
            self.assertFalse(result['ok'])
            self.assertEqual(api.calls,[])
            self.assertIn('UNKNOWN',result['reason'])

    def test_outbox_failure_rolls_back_cooldown_and_does_not_send(self):
        with PaperTrader(':memory:',profile=load_profile('v5-paper-500')) as trader:
            flow=scanner()
            update={'update_id':1,'message':{'chat':{'id':42,'type':'private'},'from':{'id':43,'is_bot':False},'date':NOW//1000,'text':'/radar'}}
            with patch('okx_grid.telegram_flow.queue_notice',side_effect=TradingError('SIMULATED_STORAGE_FAILURE')):
                result=handle_flow_update(trader,flow,update,allowed_chat=42,allowed_user=43,now=NOW//1000)
            self.assertFalse(result['ok'])
            self.assertEqual(notices(trader),[])
            with trader._transaction():
                self.assertIsNone(trader._meta('flow_alert_last_TEST'))
            self.assertEqual(trader.status()['bots'],[])

    def test_grid_real_handler_regression_with_flow_dispatch(self):
        from okx_grid.market_data import TRMarketData
        from okx_grid.scenarios import sample_plan
        from tests.test_market_data import SyntheticTR
        with PaperTrader(':memory:',profile=load_profile('v5-paper-500')) as trader:
            service=TRMarketData(trader.policy,client=SyntheticTR(),clock_ms=lambda:NOW)
            update={'update_id':1,'message':{'chat':{'id':42,'type':'private'},'from':{'id':43,'is_bot':False},'date':NOW//1000,'text':'/grid'}}
            result=handle_flow_update(trader,scanner(),update,allowed_chat=42,allowed_user=43,now=NOW//1000,
                                      market_service=service,universe=['TEST-USDT'],grid_template=sample_plan(capital='90').to_dict())
            self.assertTrue(result['ok'])
            self.assertIn('Sanal sermaye 90',result['message'])
            self.assertEqual(trader.status()['bots'],[])


class FlowSmokeTests(unittest.TestCase):
    def test_network_flag_required_before_fetch(self):
        from tools.flow_smoke import main
        with patch('sys.stderr',io.StringIO()),patch('socket.create_connection',side_effect=AssertionError('no network')):
            with self.assertRaises(SystemExit) as result:
                main([])
        self.assertEqual(result.exception.code,2)

    def test_network_failure_summary_does_not_leak_or_claim_success(self):
        from tools.flow_smoke import main
        import json
        output=io.StringIO()
        with patch('tools.flow_smoke.MoneyFlowScanner') as service,patch('sys.stdout',output):
            service.return_value.scan_batch.side_effect=TradingError('PUBLIC_HTTP_UNAVAILABLE')
            result=main(['--allow-public-network'])
        self.assertEqual(result,2)
        self.assertEqual(json.loads(output.getvalue()),{'ok':False,'live_enabled':False,'error':'PUBLIC_HTTP_UNAVAILABLE'})


if __name__ == '__main__':
    unittest.main()
