"""Synthetic live-flow routing, watch reports and durable turnover comparisons."""
from copy import deepcopy
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.commands import parse_command
from okx_grid.domain import TradingError
from okx_grid.flow_contracts import FlowPolicy
from okx_grid.money_flow import MoneyFlowScanner
from okx_grid.telegram_flow import render_flow
from okx_grid.telegram_v7 import handle_v7_update
from tests.test_money_flow import FakeFlowAPI
from tests.test_v7 import NOW, make_system, update


class V7FlowTrackingTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'separate-v7.sqlite3'
        self.now=NOW
        self.api=FakeFlowAPI(('BTC-USDT','SOL-USDT'))
        self.open()
        self.addCleanup(lambda:self.trader.close())

    def open(self):
        _,self.trader,self.core=make_system(path=self.path)
        self.core.flow=MoneyFlowScanner(client=self.api,policy=FlowPolicy(paper_notional='300'),clock_ms=lambda:self.now)

    def advance(self):
        self.now+=1000
        self.api.seq+=1

    def test_optional_flow_target_uses_the_same_symbol_and_keyboard_rules(self):
        for text in ('/PARAAKISI SOL','/paraakisi sol/usdt','/momentum SOLUSDT','/momentum soL_usdt'):
            self.assertEqual(parse_command(text).args,('SOL-USDT',))
        self.assertEqual(parse_command('/PARAAKİSİ Suı').args,('SUI-USDT',))
        self.assertEqual(parse_command('/paraakisi').args,())
        with self.assertRaisesRegex(TradingError,'COMMAND_ARGUMENTS_REJECTED'):
            parse_command('/paraakisi SOL SUI')

    def test_no_signal_still_renders_observed_book_and_executed_flow_without_orders(self):
        self.api.rvol='.2'
        result=handle_v7_update(self.core,update('/paraakisi SOL'),allowed_chat=42,allowed_user=43,now=self.now//1000)
        self.assertTrue(result['ok'])
        self.assertEqual(result['value']['batch_pairs'],['SOL-USDT'])
        self.assertEqual(result['value']['data_status'],'DATA_UNVERIFIED')
        self.assertEqual(result['value']['top5'],[])
        for value in ('NO TRADE','bid/ask','Agresif alış','satış','CVD','İşlem penceresi','RVOL','bakiyesi değildir','N/A'):
            self.assertIn(value,result['message'])
        self.assertLessEqual(len(result['message']),3500)
        self.assertEqual(self.trader.status()['bots'],[])
        self.assertFalse(self.core.status()['engine_armed'])
        self.assertTrue(all(call[1]!='BTC-USDT' for call in self.api.calls if call[0]=='flow_books'))

    def test_target_scan_does_not_advance_the_shared_radar_cursor(self):
        with self.trader._transaction():
            self.trader._set_meta('v7_flow_offset',1)
        self.core.momentum(target='SOL-USDT')
        with self.trader._transaction():
            self.assertEqual(self.trader._meta('v7_flow_offset'),1)

    def test_complete_turnover_baseline_survives_restart_and_compares_relative_share(self):
        first=self.core.momentum(target='SOL-USDT')
        self.assertEqual(first['rotation_status'],'BASELINE_STARTED')
        self.assertIsNone(first['coins'][0]['share_change'])
        self.assertEqual(first['coins'][0]['volume_share_24h'],'0.5')
        self.trader.close()
        self.open()
        original=self.api.ticker
        def ticker(name):
            result=original(name)
            if name=='SOL-USDT':result['volCcy24h']='40000000'
            return result
        self.api.ticker=ticker
        self.advance()
        second=self.core.momentum(target='SOL-USDT')
        self.assertEqual(second['rotation_status'],'COMPARABLE')
        self.assertEqual(second['rotation_reference_at_ms'],first['as_of_ms'])
        self.assertGreater(Decimal(second['coins'][0]['share_change']),0)
        self.assertNotEqual(second['snapshot_id'],first['snapshot_id'])
        message=render_flow(second,include_watch=True)
        self.assertIn('yüzde puan',message)
        self.assertIn('net sermaye girişi değildir',message)

    def test_partial_volume_denominator_neither_replaces_baseline_nor_erases_valid_pair_data(self):
        self.api.names.append('OTHER-USDT')
        self.core.momentum(target='SOL-USDT')
        with self.trader._transaction():baseline=deepcopy(self.trader._meta('v7_flow_rotation_baseline'))
        original=self.api.fetch
        def fetch(kind,*args,**kwargs):
            result=original(kind,*args,**kwargs)
            if kind=='tickers':
                next(row for row in result['data'] if row['instId']=='OTHER-USDT')['ts']=str(NOW-31000)
            return result
        self.api.fetch=fetch
        self.advance()
        result=self.core.momentum(target='SOL-USDT')
        self.assertNotIn('error',result['pairs']['SOL-USDT'])
        self.assertEqual(result['rotation_status'],'INCOMPLETE_UNIVERSE_VOLUME')
        self.assertIsNone(result['coins'][0]['volume_share_24h'])
        self.assertIsNone(result['coins'][0]['share_change'])
        self.assertFalse(result['coins'][0]['eligible'])
        self.assertIn('N/A',render_flow(result,include_watch=True))
        with self.trader._transaction():self.assertEqual(self.trader._meta('v7_flow_rotation_baseline'),baseline)

    def test_universe_change_clears_only_scan_metadata_and_never_resets_capital(self):
        self.core.momentum(target='SOL-USDT')
        state=self.trader.status()
        self.api.names.append('NEW-USDT')
        self.advance()
        with self.assertRaisesRegex(TradingError,'INCOMPARABLE_FLOW_ROTATION_BASELINE'):
            self.core.momentum(target='SOL-USDT')
        self.assertEqual(self.trader.status(),state)
        with self.trader._transaction():
            self.assertIsNone(self.trader._meta('v7_flow_rotation_baseline'))
            self.assertEqual(self.trader._meta('v7_flow_offset'),0)

    def test_duplicate_target_command_uses_receipt_without_gets_or_a_second_comparison(self):
        item=update('/momentum SOL')
        first=handle_v7_update(self.core,item,allowed_chat=42,allowed_user=43,now=self.now//1000)
        self.api.calls.clear()
        self.advance()
        replay=handle_v7_update(self.core,item,allowed_chat=42,allowed_user=43,now=self.now//1000)
        self.assertTrue(replay['duplicate'])
        self.assertEqual(replay['message'],first['message'])
        self.assertEqual(self.api.calls,[])

    def test_unknown_flow_target_is_explicit_and_never_substitutes_another_pair(self):
        result=handle_v7_update(self.core,update('/paraakisi MISSING'),allowed_chat=42,allowed_user=43,now=self.now//1000)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'],'FLOW_SYMBOL_NOT_DISCOVERED')
        self.assertFalse(any(call[0]=='flow_books' for call in self.api.calls))

    def test_three_watch_rows_keep_observed_metrics_within_the_telegram_limit(self):
        self.api.names.append('SUI-USDT')
        self.api.rvol='.2'
        scan=self.core.momentum()
        message=render_flow(scan,include_watch=True)
        self.assertLessEqual(len(message),3300)
        for pair in ('BTC-USDT','SOL-USDT','SUI-USDT'):
            self.assertIn(pair,message)
        for value in ('CVD','bid/ask','Agresif','NO TRADE','bakiyesi değildir'):
            self.assertIn(value,message)

    def test_flow_health_is_reported_independently_and_expires_without_any_get(self):
        scan=self.core.momentum(target='SOL-USDT')
        state=self.core.status()
        self.assertEqual(state['market']['reason'],'NOT_CHECKED')
        self.assertEqual(state['flow']['checked_at_ms'],scan['as_of_ms'])
        self.assertTrue(state['flow']['ok'])
        self.assertEqual(state['flow']['data_status'],'DATA_UNVERIFIED')  # Fixture never becomes live data.
        self.api.calls.clear()
        self.now+=31000
        stale=self.core.status()
        self.assertFalse(stale['flow']['ok'])
        self.assertEqual(stale['flow']['reason'],'STALE_FLOW_HEALTH')
        self.assertEqual(self.api.calls,[])

    def test_connection_failure_invalidates_previous_flow_health(self):
        self.core.momentum(target='SOL-USDT')
        self.api.fail='time'
        self.advance()
        with self.assertRaisesRegex(TradingError,'PUBLIC_HTTP_UNAVAILABLE'):
            self.core.momentum(target='SOL-USDT')
        self.assertFalse(self.core.status()['flow']['ok'])
        self.assertEqual(self.core.status()['flow']['reason'],'PUBLIC_HTTP_UNAVAILABLE')

    def test_clock_rollback_cannot_keep_a_future_flow_snapshot_healthy(self):
        self.core.momentum(target='SOL-USDT')
        self.api.calls.clear()
        self.now-=3000
        self.assertFalse(self.core.status()['flow']['ok'])
        self.assertEqual(self.core.status()['flow']['reason'],'FUTURE_FLOW_HEALTH')
        self.assertEqual(self.api.calls,[])


class V7FlowProbeTests(unittest.TestCase):
    def test_probe_rejects_live_mode_before_network_or_ledger_creation(self):
        from tools.flow_smoke import main
        output=io.StringIO()
        with patch.dict('os.environ',{'LIVE_TRADING':'true'}),patch('tools.flow_smoke.v7_snapshot',side_effect=AssertionError('no call')),patch('sys.stdout',output):
            self.assertEqual(main(['--allow-public-network','--v7-report','--target','SOL']),2)
        self.assertEqual(json.loads(output.getvalue())['error'],'LIVE_TRADING_DISABLED')

    def test_probe_requires_one_explicit_target(self):
        from tools.flow_smoke import main
        for args in ([],['--target','SOL','--offset','1'],['--target','SOL','--limit','2']):
            with patch('tools.flow_smoke.v7_snapshot',side_effect=AssertionError('no call')),patch('sys.stdout',io.StringIO()) as output:
                self.assertEqual(main(['--allow-public-network','--v7-report',*args]),2)
                self.assertEqual(json.loads(output.getvalue())['error'],'V7_FLOW_PROBE_REQUIRES_ONE_TARGET')

    def test_probe_fixture_source_is_rejected_before_fetch_and_never_sends(self):
        from tools.flow_smoke import v7_snapshot
        api,trader,core=make_system()
        with trader,patch('okx_grid.v7_market.V7MarketData',return_value=core.market),patch('socket.create_connection',side_effect=AssertionError('no network')):
            with self.assertRaisesRegex(TradingError,'DATA_UNVERIFIED'):
                v7_snapshot('SOL')
        self.assertEqual(api.calls,[])


if __name__=='__main__':
    unittest.main()
