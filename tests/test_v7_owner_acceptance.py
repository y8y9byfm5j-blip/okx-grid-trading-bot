"""Synthetic existing-owner -> core -> two-position ledger acceptance.

All prices and Telegram acknowledgements here are invented fixtures. The caller
supplies simulated notification health. No sender, scheduler or real-data
verification is added or claimed.
"""
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from okx_grid.domain import TradingError
from okx_grid.v5_owner_bridge import V5OwnerBridge
from tests.test_v7 import make_system, update


class OwnerPaperAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/'separate-v7.sqlite3'
        self.api,self.trader,self.core=make_system(path=self.path)
        self.addCleanup(lambda:self.trader.close())
        self.bridge=V5OwnerBridge(self.core,allowed_chat=42,allowed_user=43,initial_offset=0)
        self.uid=0

    def command(self,text,*,ack=True):
        self.api.advance()
        self.uid+=1
        item=update(text,self.uid,at=self.api.at//1000)
        prepared=self.bridge.prepare(item,now=self.api.at//1000)
        self.assertTrue(prepared['ok'],prepared)
        attempt=self.bridge.begin_delivery(self.uid,now=self.api.at//1000)
        self.assertTrue(attempt['send_allowed'])
        reply={'ok':True,'result':{'message_id':self.uid,'chat':{'id':42},'text':attempt['text']}} if ack else None
        delivered=self.bridge.record_delivery(self.uid,reply,now=self.api.at//1000)
        self.assertEqual(delivered['delivery'],'ACK' if ack else 'UNKNOWN')
        return item

    def planned(self):
        sol=self.command('/GRIDPLAN SOL 300')
        self.command('/gridplan Suı/USDT 300')
        self.command('/PAPERGRID START')
        return sol

    def cycle(self,name):
        self.api.advance()
        result=self.core.run_cycle(cycle_id=name,now=self.api.at//1000,notification_healthy=True)
        self.assertEqual(result['data_mode'],'SYNTHETIC_TEST')
        self.assertFalse(result['live_enabled'])
        return result

    def restart(self):
        self.trader.close()
        self.api,self.trader,self.core=make_system(path=self.path,client=self.api)
        self.bridge=V5OwnerBridge(self.core,allowed_chat=42,allowed_user=43,initial_offset=0)

    def assert_two_reserved(self,state):
        portfolio=state['portfolio']
        self.assertEqual(portfolio['active_bots'],2)
        self.assertEqual(Decimal(portfolio['reserved_capital']),600)
        self.assertEqual(Decimal(portfolio['unallocated_cash']),0)
        self.assertLessEqual(Decimal(portfolio['reserved_planned_risk']),6)
        self.assertEqual({b['instrument'] for b in state['bots']},{'SOL-USDT','SUI-USDT'})
        self.assertTrue(all(Decimal(b['equity'])-Decimal(b['total_pnl'])==300 for b in state['bots']))

    def test_two_positions_partial_fill_command_and_cycle_replay_restart_and_halt(self):
        original=self.planned()
        opened=self.cycle('two_open')
        self.assertEqual(len(opened['opened']),2)
        self.assert_two_reserved(opened['state'])
        self.api.calls.clear()
        self.assertTrue(self.bridge.prepare(original,now=self.api.at//1000)['duplicate'])
        replay=self.core.run_cycle(cycle_id='two_open',now=self.api.at//1000,notification_healthy=True)
        self.assertTrue(replay['duplicate'])
        self.assertEqual(replay['state'],opened['state'])
        self.assertEqual(self.api.calls,[])
        self.command('/papergrid STATUS')

        self.api.price=Decimal('99.99')
        self.api.quantity='5'
        partial=self.cycle('partial')
        self.assert_two_reserved(partial['state'])
        for bot in partial['state']['bots']:
            self.assertGreater(Decimal(bot['quantity']),0)
            self.assertLess(Decimal(bot['quantity']),1)
        before=self.trader.status()
        checkpoint=self.bridge.next_offset()
        self.restart()
        self.assertEqual(self.trader.status(),before)
        self.assertEqual(self.bridge.next_offset(),checkpoint)
        self.command('/EMERGENCY_STOP')
        self.assertTrue(self.trader.status()['halted'])

        self.api.price=Decimal('80')
        self.api.quantity='1000'
        stopped=self.cycle('protective_gap_exit')
        self.assertEqual(stopped['opened'],[])
        self.assertEqual(stopped['state']['portfolio']['active_bots'],0)
        self.assertEqual(Decimal(stopped['state']['portfolio']['reserved_capital']),0)
        self.assertLess(Decimal(stopped['state']['portfolio']['realized_pnl']),-6)
        self.assertFalse(stopped['state']['stop_loss_is_guaranteed'])
        self.assertTrue(all(b['status']=='CLOSED' for b in stopped['state']['bots']))
        stopped_state=self.trader.status()
        self.restart()
        self.assertEqual(self.trader.status(),stopped_state)
        self.assertTrue(self.trader.status()['halted'])
        with self.assertRaisesRegex(TradingError,'PAPER_HALTED'):
            self.core.start(now=self.api.at//1000)

    def test_unknown_telegram_delivery_prevents_both_pending_positions(self):
        self.planned()
        self.command('/durum',ack=False)
        blocked=self.cycle('unknown_delivery')
        self.assertEqual(blocked['opened'],[])
        self.assertTrue(blocked['state']['paused'])
        self.assertEqual(blocked['state']['portfolio']['active_bots'],0)
        self.assertEqual(Decimal(blocked['state']['portfolio']['reserved_capital']),0)
        self.assertEqual(Decimal(blocked['state']['portfolio']['cash']),600)
        self.restart()
        self.assertFalse(self.bridge.begin_delivery(self.uid,now=self.api.at//1000)['send_allowed'])
        with self.assertRaisesRegex(TradingError,'NOTIFICATION_OUTCOME_UNKNOWN'):
            self.core.start(now=self.api.at//1000)

    def test_emergency_stop_does_not_report_min_notional_dust_as_closed(self):
        self.planned()
        self.cycle('open')
        self.api.price=Decimal('99.99')
        self.api.quantity='.5'
        self.cycle('tiny_partial')
        self.command('/emergency_stop')
        self.api.price=Decimal('80')
        self.api.quantity='1000'
        blocked=self.cycle('gap_dust')
        self.assertEqual(blocked['opened'],[])
        self.assertTrue(blocked['state']['halted'])
        self.assert_two_reserved(blocked['state'])
        for bot in blocked['state']['bots']:
            self.assertGreater(Decimal(bot['quantity']),0)
            self.assertTrue(any(order.get('blocked_reason') for order in bot['orders']['slots']))

    def test_api_failure_blocks_both_plans_and_recovery_requires_explicit_start(self):
        self.planned()
        self.api.fail='books'
        rejected=self.cycle('network_failure')
        self.assertEqual(len(rejected['data_errors']),2)
        self.assertEqual(rejected['opened'],[])
        self.assertTrue(self.core.status()['paper']['paused'])
        self.assertFalse(self.core.status()['market']['ok'])
        self.api.fail=None
        recovered=self.cycle('network_restored')
        self.assertEqual(recovered['data_errors'],[])
        self.assertEqual(recovered['opened'],[])
        self.assertTrue(recovered['state']['paused'])
        self.command('/papergrid start')
        resumed=self.cycle('explicit_resume')
        self.assertEqual(len(resumed['opened']),2)
        self.assert_two_reserved(resumed['state'])

    def test_status_command_exposes_unresolved_cycle_pair_errors_without_new_gets(self):
        self.planned()
        self.api.fail='books'
        self.cycle('failed_capture')
        self.api.fail=None
        self.api.advance()
        self.core.market.capture('BTC-USDT')
        self.api.calls.clear()
        item=self.command('/durum')
        report=self.bridge.prepare(item,now=self.api.at//1000)
        self.assertIn('UNRESOLVED_CYCLE_DATA_ERROR',report['message'])
        self.assertIn('SOL-USDT | PUBLIC_HTTP_UNAVAILABLE',report['message'])
        self.assertIn('SUI-USDT | PUBLIC_HTTP_UNAVAILABLE',report['message'])
        self.assertIn('hazır False',report['message'])
        self.assertEqual(self.api.calls,[])


if __name__=='__main__':
    unittest.main()
