"""Explicit read-only V6 REST smoke; no poller, credentials, sender or orders."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from okx_grid.domain import TradingError, paper_only
from okx_grid.money_flow import MoneyFlowScanner


def main(argv=None):
    parser=argparse.ArgumentParser(description='Explicit V6 OKX TR public flow smoke')
    parser.add_argument('--allow-public-network',action='store_true',required=True)
    parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--limit',type=int,choices=range(1,4),default=1)
    parser.add_argument('--target',help='Actual discovered coin or pair; no fallback symbol')
    args=parser.parse_args(argv)
    try:
        paper_only()
        scanner=MoneyFlowScanner()
        result=scanner.scan_batch(offset=args.offset,limit=args.limit,target=args.target)
        summary={k:result[k] for k in ('source_origin','as_of_ms','server_ts_ms','scope','discovered_pairs','excluded_quotes',
                                      'batch_pairs','next_offset','universe_hash','warnings')}
        summary.update(ok=True,live_enabled=False,candidates=[{k:c[k] for k in ('coin','pairs','score','score_breakdown','reasons','as_of_ms')}
                         for c in result['top5']],pair_status={name:({'error':r['error']} if 'error' in r else
                         {'quote':r['quote'],'volume_24h_usdt':r['volume_24h_usdt'],'fx':r['fx'],'indicators':r['indicators'],
                          'flow':r['flow'],'spread_bps':r['book']['spread_bps'],'depth':r['book']['depth'],
                          'book_ts_ms':r['book']['cursor']['at_ms'],'selection':r['selection']}) for name,r in result['pairs'].items()})
        print(json.dumps(summary,sort_keys=True))
        return 0 if any('error' not in r for r in result['pairs'].values()) else 2
    except TradingError as error:
        diagnostics=getattr(getattr(locals().get('scanner'),'client',None),'last_response',None)
        summary={'ok':False,'live_enabled':False,'error':error.code}
        if isinstance(diagnostics,dict):
            summary['request_diagnostics']=diagnostics
        print(json.dumps(summary))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
