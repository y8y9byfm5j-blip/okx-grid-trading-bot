"""Public probe failure reporting only; these tests make no network requests."""
import io
import json
import unittest
from unittest.mock import patch

from tools.v7_paper_smoke import PaperAcceptanceDataRejected, main


class PaperAcceptanceDiagnosticsTests(unittest.TestCase):
    def test_rejection_retains_pair_and_engine_code_but_no_payload_or_url(self):
        error=PaperAcceptanceDataRejected({'data_errors':[
            {'instrument':'SOL-USDT','reason':'INCOHERENT_PUBLIC_TOP_OF_BOOK',
             'payload':{'private':'synthetic sensitive example'},'url':'https://invalid.example/token'},
            {'instrument':'BTC-USDT','reason':'PUBLIC_HTTP_UNAVAILABLE'},
        ]})
        output=io.StringIO()
        with patch('tools.v7_paper_smoke.acceptance',side_effect=error),patch('sys.stdout',output):
            self.assertEqual(main(['--allow-public-network']),2)
        result=json.loads(output.getvalue())
        self.assertEqual(result['data_status'],'DATA_UNVERIFIED')
        self.assertEqual(result['phase'],'PAPER_CYCLE')
        self.assertEqual(result['data_errors'],[
            {'instrument':'SOL-USDT','reason':'INCOHERENT_PUBLIC_TOP_OF_BOOK'},
            {'instrument':'BTC-USDT','reason':'PUBLIC_HTTP_UNAVAILABLE'},
        ])
        self.assertNotIn('synthetic sensitive',output.getvalue())
        self.assertNotIn('invalid.example',output.getvalue())
        self.assertFalse(result['live_enabled'])

    def test_unrecognized_reason_and_pair_are_not_printed(self):
        error=PaperAcceptanceDataRejected({'data_errors':[
            {'instrument':'private example','reason':'PRIVATE_SYNTHETIC_VALUE'},
        ]})
        self.assertEqual(error.data_errors,[{
            'instrument':'UNREPORTED_PAIR','reason':'UNREPORTED_DATA_REJECTION'}])

    def test_malformed_or_excess_error_details_are_bounded(self):
        for rows in (None,'private example',{'private':'example'}):
            self.assertEqual(PaperAcceptanceDataRejected({'data_errors':rows}).data_errors,[])
        row={'instrument':'BTC-USDT','reason':'STALE_PUBLIC_DATA'}
        self.assertEqual(len(PaperAcceptanceDataRejected({'data_errors':[row]*100}).data_errors),2)

    def test_no_detail_is_invented_when_cycle_has_no_usable_capture(self):
        error=PaperAcceptanceDataRejected({'data_errors':[],'data_mode':'DATA_UNVERIFIED'})
        self.assertEqual(error.data_errors,[])
        self.assertEqual(error.code,'PAPER_ACCEPTANCE_DATA_REJECTED')


if __name__=='__main__':
    unittest.main()
