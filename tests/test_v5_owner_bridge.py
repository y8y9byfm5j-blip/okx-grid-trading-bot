"""Synthetic ingress/ACK acceptance for the observed V5 owner shape.

No production source, credentials, polling or actual Telegram delivery.
"""
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_grid.commands import parse_command
from okx_grid.control import notices
from okx_grid.domain import TradingError
from okx_grid.v5_owner_bridge import V5OwnerBridge
from tests.test_v7 import NOW, make_system, update


def native(text='/durum', uid=1):
    item=update(text, uid)
    item['message'].update(message_id=9, entities=[{'offset':0,'length':len(text.split()[0]),'type':'bot_command'}])
    item['message']['from'].update(first_name='Synthetic', language_code='tr')
    return item


class V5OwnerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.api, self.trader, self.core=make_system()
        self.addCleanup(self.trader.close)
        self.bridge=V5OwnerBridge(self.core,allowed_chat=42,allowed_user=43,initial_offset=0)

    def prepare(self, item=None):
        return self.bridge.prepare(item or native(),now=NOW//1000)

    def ack(self, text, *, chat=42):
        return {'ok':True,'result':{'message_id':123,'chat':{'id':chat,'type':'private'},'text':text}}

    def test_native_update_routes_grid_without_legacy_plan_membership(self):
        result=self.prepare(native('/GRID SUI/USDT'))
        self.assertTrue(result['handled'])
        self.assertIn('SUI-USDT',result['message'])
        self.assertIn('DATA_UNVERIFIED',result['message'])
        self.assertEqual(result['next_offset'],2)
        self.assertEqual(result['delivery'],'PENDING')
        self.assertEqual(len(notices(self.trader)),1)

    def test_turkish_keyboard_i_is_normalized_without_general_unicode_folding(self):
        for command in ('/grid Suı','/GRİD SUİ','/grıd sui'):
            self.assertEqual(parse_command(command).text,'/grid SUI-USDT')
        with self.assertRaisesRegex(TradingError,'TELEGRAM_COMMAND_REJECTED'):
            parse_command('/grid SУI')  # Cyrillic lookalike stays rejected.

    def test_unauthorized_chat_sender_and_forwarded_updates_never_get_a_reply(self):
        for uid,kind in enumerate(('chat','sender','forward','edited'),1):
            item=native('/grid SUI',uid)
            if kind=='chat':item['message']['chat']['id']=99
            if kind=='sender':item['message']['from']['id']=99
            if kind=='forward':item['message']['forward_origin']={'type':'hidden_user'}
            if kind=='edited':item['message']['edit_date']=NOW//1000
            result=self.prepare(item)
            self.assertFalse(result['handled'])
            self.assertEqual(result['reason'],'TELEGRAM_UNAUTHORIZED')
            self.assertEqual(result['next_offset'],uid+1)
        self.assertEqual(self.api.calls,[])
        self.assertEqual(notices(self.trader),[])

    def test_unknown_command_has_explicit_durable_error_and_legacy_paper_is_not_adopted(self):
        unknown=self.prepare(native('/gird SUI'))
        self.assertEqual(unknown['reason'],'UNKNOWN_COMMAND')
        self.assertIn('UNKNOWN_COMMAND',unknown['message'])
        legacy=self.prepare(native('/paper',2))
        self.assertEqual(legacy['reason'],'LEGACY_PAPER_ROUTE_NOT_BOUND')
        self.assertEqual(self.api.calls,[])
        self.assertEqual(self.trader.status()['portfolio']['initial_equity'],'600')

    def test_duplicate_replays_outcome_without_gets_or_new_notice(self):
        first=self.prepare(native('/grid SUI'))
        self.api.calls.clear()
        second=self.prepare(native('/grid SUI'))
        self.assertTrue(second['duplicate'])
        self.assertEqual(second['message'],first['message'])
        self.assertEqual(self.api.calls,[])
        self.assertEqual(len(notices(self.trader)),1)
        altered=native('/grid SOL')
        with self.assertRaisesRegex(TradingError,'TELEGRAM_UPDATE_CONFLICT'):
            self.prepare(altered)

    def test_storage_failure_never_checkpoints_an_uncommitted_bridge_outcome(self):
        original=self.trader._set_meta
        def fail(key,value):
            if key=='v5_owner_next_offset':raise sqlite3.OperationalError('synthetic storage failure')
            return original(key,value)
        with patch.object(self.trader,'_set_meta',side_effect=fail):
            with self.assertRaisesRegex(TradingError,'STORAGE_ERROR'):
                self.prepare(native('/grid SUI'))
        self.assertEqual(self.bridge.next_offset(),0)
        self.api.calls.clear()
        recovered=self.prepare(native('/grid SUI'))
        self.assertEqual(recovered['next_offset'],2)
        self.assertEqual(self.api.calls,[])
        self.assertEqual(len(notices(self.trader)),1)

    def test_unknown_processing_outcome_is_sanitized_and_pauses_entries(self):
        with patch.object(self.core,'decision',side_effect=RuntimeError('private example URL value')):
            result=self.prepare(native('/grid SUI'))
        self.assertEqual(result['reason'],'COMMAND_PROCESSING_OUTCOME_UNKNOWN')
        self.assertNotIn('private example',result['message'])
        self.assertTrue(self.trader.status()['paused'])

    def test_crash_after_acceptance_requires_new_update_without_repeating_action(self):
        with patch.object(self.core,'decision',side_effect=SystemExit):
            with self.assertRaises(SystemExit):self.prepare(native('/grid SUI'))
        self.assertEqual(self.bridge.next_offset(),0)
        self.api.calls.clear()
        recovered=self.prepare(native('/grid SUI'))
        self.assertEqual(recovered['reason'],'COMMAND_OUTCOME_UNKNOWN_NEW_UPDATE_REQUIRED')
        self.assertEqual(self.api.calls,[])
        self.assertTrue(self.trader.status()['paused'])
        self.assertEqual(recovered['next_offset'],2)

    def test_matching_ack_requires_write_ahead_delivery_journal(self):
        result=self.prepare()
        with self.assertRaisesRegex(TradingError,'TELEGRAM_DELIVERY_NOT_BEGUN'):
            self.bridge.record_delivery(1,self.ack(result['message']),now=NOW//1000)
        attempt=self.bridge.begin_delivery(1,now=NOW//1000)
        self.assertTrue(attempt['send_allowed'])
        self.assertEqual(notices(self.trader)[0]['status'],'UNKNOWN')
        response=self.bridge.record_delivery(1,self.ack(attempt['text']),now=NOW//1000)
        self.assertEqual(response['delivery'],'ACK')
        self.assertEqual(notices(self.trader),[])
        self.assertFalse(self.bridge.begin_delivery(1,now=NOW//1000)['send_allowed'])
        self.assertEqual(self.prepare()['delivery'],'ACK')

    def test_none_wrong_chat_and_wrong_text_are_unknown_and_never_auto_resent(self):
        for uid,kind in enumerate(('none','chat','text'),1):
            item=native('/help',uid)
            self.prepare(item)
            attempt=self.bridge.begin_delivery(uid,now=NOW//1000)
            reply=None if kind=='none' else self.ack(attempt['text'],chat=99 if kind=='chat' else 42)
            if kind=='text':reply['result']['text']='Unrelated synthetic response'
            result=self.bridge.record_delivery(uid,reply,now=NOW//1000)
            self.assertEqual(result['delivery'],'UNKNOWN')
            self.assertTrue(self.trader.status()['paused'])
            self.assertFalse(self.bridge.begin_delivery(uid,now=NOW//1000)['send_allowed'])

    def test_stale_command_and_out_of_order_update_do_not_fetch(self):
        item=native('/grid SUI',2)
        item['message']['date']-=31
        self.assertEqual(self.prepare(item)['reason'],'TELEGRAM_STALE_UPDATE')
        self.assertEqual(self.prepare(native('/grid SOL',1))['reason'],'TELEGRAM_OLD_UPDATE')
        self.assertEqual(self.api.calls,[])
        self.assertEqual(self.bridge.next_offset(),3)

    def test_emergency_stop_bypasses_same_second_rate_limit_and_replay_cannot_clear_it(self):
        self.prepare(native('/durum',1))
        result=self.prepare(native('/EMERGENCY_STOP',2))
        self.assertTrue(result['ok'])
        self.assertTrue(self.trader.status()['halted'])
        self.assertTrue(self.prepare(native('/EMERGENCY_STOP',2))['duplicate'])
        self.assertTrue(self.trader.status()['halted'])
        self.assertEqual(self.api.calls,[])

    def test_pending_reply_survives_restart_and_is_excluded_as_soon_as_delivery_begins(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'new-v7.sqlite3'
            _,trader,core=make_system(path=path)
            with trader:
                bridge=V5OwnerBridge(core,allowed_chat=42,allowed_user=43,initial_offset=100)
                prepared=bridge.prepare(native('/durum',100),now=NOW//1000)
            _,trader,core=make_system(path=path)
            with trader:
                bridge=V5OwnerBridge(core,allowed_chat=42,allowed_user=43,initial_offset=100)
                self.assertEqual(bridge.pending_deliveries(),[{
                    'update_id':100,'notice_id':'v7u_100','text':prepared['message']}])
                bridge.begin_delivery(100,now=NOW//1000)
                self.assertEqual(bridge.pending_deliveries(),[])

    def test_malformed_update_id_cannot_advance_checkpoint(self):
        for uid in (-1,True,'1',None):
            with self.assertRaisesRegex(TradingError,'TELEGRAM_UPDATE_REJECTED'):
                self.prepare(native('/durum',uid))
        self.assertEqual(self.bridge.next_offset(),0)
        self.assertEqual(notices(self.trader),[])

    def test_restart_preserves_checkpoint_and_unknown_delivery_without_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'new-v7.sqlite3'
            api,trader,core=make_system(path=path)
            with trader:
                bridge=V5OwnerBridge(core,allowed_chat=42,allowed_user=43,initial_offset=100)
                bridge.prepare(native('/durum',100),now=NOW//1000)
                self.assertTrue(bridge.begin_delivery(100,now=NOW//1000)['send_allowed'])
            api,trader,core=make_system(path=path)
            with trader:
                bridge=V5OwnerBridge(core,allowed_chat=42,allowed_user=43,initial_offset=100)
                self.assertEqual(bridge.next_offset(),101)
                self.assertFalse(bridge.begin_delivery(100,now=NOW//1000)['send_allowed'])
                with self.assertRaisesRegex(TradingError,'NOTIFICATION_OUTCOME_UNKNOWN'):
                    core.start(now=NOW//1000)
                with self.assertRaisesRegex(TradingError,'UNRECONCILED_LEGACY_OFFSET'):
                    V5OwnerBridge(core,allowed_chat=42,allowed_user=43,initial_offset=102)
                with self.assertRaisesRegex(TradingError,'CONTROL_POLICY_MISMATCH'):
                    V5OwnerBridge(core,allowed_chat=42,allowed_user=99,initial_offset=100)


if __name__=='__main__':
    unittest.main()
