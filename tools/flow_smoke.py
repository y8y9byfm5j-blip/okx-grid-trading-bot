"""Explicit read-only V6 REST smoke; no poller, credentials, sender or orders."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from okx_grid.domain import TradingError, paper_only
from okx_grid.money_flow import MoneyFlowScanner


def v7_snapshot(target):
    """Exercise the real shared core and report, never the Telegram transport."""
    from okx_grid.commands import pair_name
    from okx_grid.telegram_flow import render_flow
    from okx_grid.v7_core import V7Core
    from okx_grid.v7_market import V7MarketData
    from okx_grid.v7_paper import V7PaperTrader
    paper_only()
    target=pair_name(target)
    with tempfile.TemporaryDirectory(prefix='okx-v7-public-flow-') as folder:
        with V7PaperTrader(Path(folder)/'acceptance.sqlite3') as trader:
            market=V7MarketData(trader.policy)
            if not market.real_transport:
                raise TradingError('DATA_UNVERIFIED')
            core=V7Core(trader,market)
            scan=core.momentum(target=target)
            state=core.status()
            if state['engine_armed'] or state['paper']['bots']:
                raise TradingError('READ_ONLY_FLOW_ACCEPTANCE_FAILED')
            message=render_flow(scan,detailed=True,include_watch=True)
            if len(message)>3300:
                raise TradingError('TELEGRAM_REPORT_TOO_LARGE')
            return scan,{'data_status':scan['data_status'],'snapshot_id':scan['snapshot_id'],
                'rotation_status':scan['rotation_status'],'rotation_reference_at_ms':scan['rotation_reference_at_ms'],
                'watch_report':message,'telegram_verified':False,'production_activated':False,
                'positions_opened':0,'reserved_capital_usdt':state['paper']['portfolio']['reserved_capital']}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Explicit V6 OKX TR public flow smoke')
    parser.add_argument('--allow-public-network',action='store_true',required=True)
    parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--limit',type=int,choices=range(1,4),default=1)
    parser.add_argument('--target',help='Actual discovered coin or pair; no fallback symbol')
    parser.add_argument('--v7-report',action='store_true',help='Validate the existing-owner V7 flow/report path in a temporary ledger; no sender')
    args=parser.parse_args(argv)
    try:
        paper_only()
        extra={}
        if args.v7_report:
            if args.target is None or args.offset or args.limit!=1:
                raise TradingError('V7_FLOW_PROBE_REQUIRES_ONE_TARGET')
            result,extra=v7_snapshot(args.target)
        else:
            scanner=MoneyFlowScanner()
            result=scanner.scan_batch(offset=args.offset,limit=args.limit,target=args.target)
        summary={k:result[k] for k in ('source_origin','as_of_ms','server_ts_ms','scope','discovered_pairs','excluded_quotes',
                                      'batch_pairs','next_offset','universe_hash','warnings')}
        accepted=any('error' not in r for r in result['pairs'].values())
        if args.v7_report and extra.get('data_status')!='VERIFIED_TR_PUBLIC':
            accepted=False
        summary.update(ok=accepted,live_enabled=False,candidates=[{k:c[k] for k in ('coin','pairs','score','score_breakdown','reasons','as_of_ms')}
                         for c in result['top5']],pair_status={name:(({'error':r['error']} |
                         ({'capture_alignment':r['capture_alignment']} if 'capture_alignment' in r else {})) if 'error' in r else
                         {'quote':r['quote'],'volume_24h_usdt':r['volume_24h_usdt'],'fx':r['fx'],'indicators':r['indicators'],
                          'flow':r['flow'],'spread_bps':r['book']['spread_bps'],'depth':r['book']['depth'],
                          'book_ts_ms':r['book']['cursor']['at_ms'],'selection':r['selection']}) for name,r in result['pairs'].items()})
        summary.update(extra)
        print(json.dumps(summary,sort_keys=True))
        return 0 if accepted else 2
    except TradingError as error:
        diagnostics=getattr(getattr(locals().get('scanner'),'client',None),'last_response',None)
        summary={'ok':False,'live_enabled':False,'error':error.code}
        if isinstance(diagnostics,dict):
            summary['request_diagnostics']=diagnostics
        print(json.dumps(summary))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
