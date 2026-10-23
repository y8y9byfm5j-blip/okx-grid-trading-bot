"""V6 bounded REST scanner for discovered TR USDT/TRY pairs. No worker/orders."""
from dataclasses import asdict
from decimal import Decimal
import hashlib
import time
import threading
from functools import wraps

from .domain import TradingError, canonical, number, positive, precise
from .flow_contracts import (BARS, MAX_HISTORY_PAGES, TRADE_CAPTURE_BUDGET_MS, FlowPolicy, book_snapshot, candle_indicators, candles,
                             discover, fresh, fx_rate, metric, quote_to_usdt, score_flow, spot_symbol, taker_flow)
from .public_contracts import TR_ORIGIN, _one, _rows, milliseconds
from .public_http import ReadOnlyTRClient


class _IncoherentSnapshots(TradingError):
    def __init__(self, diagnostics):
        super().__init__('INCOHERENT_FLOW_SNAPSHOTS')
        self.diagnostics=diagnostics


def _alignment(ticker, at, book, now, attempts):
    bid,ask=positive(ticker['bidPx']),positive(ticker['askPx'])
    book_bid,book_ask=number(book['bid']),number(book['ask'])
    return {'attempts':attempts,'ticker_age_ms':now-at,'book_age_ms':now-book['cursor']['at_ms'],
            'timestamp_delta_ms':at-book['cursor']['at_ms'],
            'bid_matches':bid==book_bid,'ask_matches':ask==book_ask,
            'bid_gap_bps':metric((bid/book_bid-1)*10000),
            'ask_gap_bps':metric((ask/book_ask-1)*10000)}


def serialized(function):
    @wraps(function)
    def wrapped(self,*args,**kwargs):
        with self._lock:
            return function(self,*args,**kwargs)
    return wrapped


@precise
def execution_estimate(reply, quote, fx, now, *, notional, fee=Decimal('.001')):
    """Walk asks then bids for equal base inventory; finite snapshot capacity.

    Fee is an explicit assumption, not an authenticated account fee tier.
    """
    notional,fee=positive(notional),number(fee)
    if not 0<=fee<=Decimal('.02'):
        raise TradingError('INVALID_FLOW_FEE')
    book=_one(reply)
    mid=(positive(book['bids'][0][0])+positive(book['asks'][0][0]))/2
    native=notional if quote=='USDT' else notional*positive(fx['try_per_usdt'])
    cash=native/(1+fee)
    quantity=Decimal(0)
    for p,q,_,_ in book['asks']:
        p,q=positive(p),positive(q)
        used=min(cash,p*q)
        quantity+=used/p
        cash-=used
        if not cash:
            break
    if cash>0:
        raise TradingError('FLOW_ASK_CAPACITY')
    held=quantity
    proceeds=Decimal(0)
    for p,q,_,_ in book['bids']:
        p,q=positive(p),positive(q)
        sold=min(held,q)
        proceeds+=sold*p
        held-=sold
        if not held:
            break
    if held>0:
        raise TradingError('FLOW_BID_CAPACITY')
    buy_avg=native/(1+fee)/quantity
    sell_avg=proceeds/quantity
    return {'notional_usdt':metric(notional),'base_quantity':metric(quantity),
            'buy_slippage':metric(buy_avg/mid-1),'sell_slippage':metric(1-sell_avg/mid),
            'snapshot_roundtrip_net_usdt':metric(quote_to_usdt(proceeds*(1-fee),quote,fx,now)-notional),
            'fee_each_side':metric(fee),'fee_source':'EXPLICIT_PAPER_ASSUMPTION','meaning':'SNAPSHOT_ESTIMATE_NOT_REAL_FILL'}


@precise
def aggregate_coins(reports, pairs, ticker_volumes, *, now, previous=None, universe_hash=None):
    """Add executed turnover across distinct markets, group by base coin once.

    Coverage is explicit: an unscanned same-base pair cannot silently disappear.
    24h share change is relative activity, not a measurement of net new deposits.
    """
    total=sum((number(v) for v in ticker_volumes.values()),Decimal(0))
    if total<=0:
        raise TradingError('FLOW_UNIVERSE_VOLUME_ZERO')
    if previous is not None:
        if previous.get('universe_hash')!=universe_hash or set(previous.get('volumes',{}))!=set(ticker_volumes) or not 0<now-previous.get('at_ms',now)<=3600000:
            raise TradingError('INCOMPARABLE_FLOW_ROTATION_BASELINE')
    coins={}
    for pair,metadata in pairs.items():
        if metadata['base']=='USDT':
            continue  # FX bridge, not a speculative coin candidate.
        coin=coins.setdefault(metadata['base'],{'coin':metadata['base'],'pairs':[],'reports':[], 'volume':Decimal(0)})
        coin['pairs'].append(pair)
        if pair in ticker_volumes:
            coin['volume']+=number(ticker_volumes[pair])
        if pair in reports and 'error' not in reports[pair]:
            report=reports[pair]
            if now-report['as_of_ms']<=30000:
                coin['reports'].append(report)
    result=[]
    for value in coins.values():
        available=value.pop('reports')
        if not available:
            continue
        complete=len(available)==len(value['pairs']) and all(p in ticker_volumes for p in value['pairs'])
        cvd=sum((number(r['flow']['cvd_quote_usdt']) for r in available),Decimal(0))
        leader=max(available,key=lambda r:r['selection']['score'])
        share=value.pop('volume')/total
        delta=None
        if previous is not None:
            prior_total=sum((number(v) for v in previous['volumes'].values()),Decimal(0))
            if prior_total<=0:
                raise TradingError('FLOW_UNIVERSE_VOLUME_ZERO')
            old=sum((number(previous['volumes'][p]) for p in value['pairs']),Decimal(0))/prior_total
            delta=metric(share-old)
        reasons=list(leader['selection']['reasons'])
        if not complete:
            reasons.append('INCOMPLETE_COIN_PAIR_COVERAGE')
        if cvd<=0:
            reasons.append('NO_AGGREGATE_TAKER_CONFIRMATION')
        result.append({**value,'as_of_ms':min(r['as_of_ms'] for r in available),'market_origin':TR_ORIGIN,
                       'volume_share_24h':metric(share),'share_change':delta,'rotation_basis':'ROLLING_24H_TURNOVER_SHARE_NOT_CAPITAL_FLOW',
                       'cvd_quote_usdt':metric(cvd),'coverage_complete':complete,'eligible':not reasons,'reasons':reasons,
                       'score':leader['selection']['score'],'score_breakdown':leader['selection']['score_breakdown'],
                       'leader_pair':leader['instrument'],'leader':leader,'live_enabled':False})
    return sorted(result,key=lambda r:(not r['eligible'],-r['score'],r['coin']))


class MoneyFlowScanner:
    def __init__(self, *, client=None, policy=None, clock_ms=None, fee_rate="0.001"):
        self.client=client if client is not None else ReadOnlyTRClient(max_catalog_bytes=8_000_000)
        self.policy=policy if policy is not None else FlowPolicy()
        self.fee_rate=number(fee_rate)
        if not 0<=self.fee_rate<=Decimal('.02'):
            raise TradingError('INVALID_FLOW_FEE')
        self.clock_ms=clock_ms if clock_ms is not None else lambda:time.time_ns()//1000000
        self._lock=threading.RLock()
        self.cursors={}
        self.resync_required=set()

    def _now(self):
        return milliseconds(self.clock_ms())

    @property
    def real_transport(self):
        return type(self.client) is ReadOnlyTRClient and self.client._default_transport is True

    @property
    def data_status(self):
        return 'VERIFIED_TR_PUBLIC' if self.real_transport else 'DATA_UNVERIFIED'

    def _trade_pages(self,instrument,end):
        page=self.client.fetch('trades',instrument)
        pages=[page]
        seen_cursors=set()
        for index in range(MAX_HISTORY_PAGES+1):
            if not 0<=self._now()-end<=TRADE_CAPTURE_BUDGET_MS:
                raise TradingError('FLOW_TRADE_CAPTURE_TOO_SLOW')
            rows=_rows(page)
            if len(rows)>(500 if index==0 else 100):
                raise TradingError('FLOW_TRADE_PAGE_TOO_LARGE')
            try:
                oldest=min(milliseconds(r['ts']) for r in rows)
                cursor=min((r['tradeId'] for r in rows),key=int)
            except (KeyError,TypeError,ValueError):
                raise TradingError('INVALID_FLOW_TRADE') from None
            if oldest<=end-300000:
                return pages
            if index==MAX_HISTORY_PAGES:
                break
            if cursor in seen_cursors:
                raise TradingError('FLOW_PAGINATION_STALLED')
            seen_cursors.add(cursor)
            page=self.client.fetch('history_trades',instrument,after=cursor)
            pages.append(page)
        raise TradingError('INCOMPLETE_FLOW_TRADE_WINDOW')

    def _indicators(self,instrument,start):
        # Candles are read after pagination, near the current book. A candle
        # still marked 0 after its boundary stays unusable; re-read once rather
        # than treating elapsed wall time as exchange confirmation.
        for attempt in range(2):
            replies={}
            for bar in BARS:
                if not 0<=self._now()-start<=30000:
                    raise TradingError('FLOW_CAPTURE_TOO_SLOW')
                replies[bar]=self.client.fetch('flow_candles',instrument,bar=bar)
            now=self._now()
            if not 0<=now-start<=30000:
                raise TradingError('FLOW_CAPTURE_TOO_SLOW')
            try:
                return candle_indicators(replies,now)
            except TradingError as error:
                if attempt or error.code not in {'FLOW_CANDLE_FINALIZATION_PENDING','MISSING_COMPLETED_FLOW_CANDLE'}:
                    raise

    @serialized
    def reconnect(self,instrument):
        """A caller-observed reconnect invalidates old continuity. No auto-resume.

        REST has no WS checksum/prevSeqId guarantees. Review and construct a new
        scanner after successful explicit resnapshot; don't silently clear state.
        """
        self.resync_required.add(spot_symbol(instrument))

    @serialized
    @precise
    def analyze_pair(self,instrument,pairs,fx,btc_return):
        instrument=spot_symbol(instrument)
        if instrument not in pairs:
            raise TradingError('FLOW_PAIR_NOT_DISCOVERED')
        if instrument in self.resync_required:
            raise TradingError('FLOW_BOOK_RESYNC_REQUIRED')
        try:
            start=self._now()
            metadata=pairs[instrument]
            pages=self._trade_pages(instrument,start)
            indicators=self._indicators(instrument,start)
            tickers,books=[],[]
            for attempt in range(3):
                ticker=_one(self.client.fetch('flow_ticker',instrument))
                rawbook=self.client.fetch('flow_books',instrument)
                now=self._now()
                if now-start>30000 or now<start:
                    raise TradingError('FLOW_CAPTURE_TOO_SLOW')
                if ticker['instId']!=instrument or ticker['instType']!='SPOT':
                    raise TradingError('FLOW_INSTRUMENT_MISMATCH')
                at=fresh(ticker['ts'],now)
                book=book_snapshot(rawbook,instrument,now,previous=self.cursors.get(instrument),quote=metadata['quote'],fx=fx)
                alignment=_alignment(ticker,at,book,now,attempt+1)
                tickers.append((ticker,at,attempt+1))
                books.append((rawbook,book,attempt+1))
                matched=None
                # Independent REST caches can interleave observations. Join only
                # this capture's <=3 tickers/books, preserving exact bid/ask,
                # <=2s timestamp distance, freshness and the prior book cursor.
                # No extra GETs, price substitution, tolerance or old-call cache.
                for candidate_raw,candidate_book,book_index in sorted(books,
                        key=lambda item:(item[1]['cursor']['sequence'],item[1]['cursor']['at_ms']),reverse=True):
                    for candidate_ticker,candidate_at,ticker_index in reversed(tickers):
                        try:
                            fresh(candidate_at,now)
                            fresh(candidate_book['cursor']['at_ms'],now)
                        except TradingError:
                            continue
                        candidate=_alignment(candidate_ticker,candidate_at,candidate_book,now,attempt+1)
                        if (abs(candidate['timestamp_delta_ms'])<=2000 and
                                candidate['bid_matches'] and candidate['ask_matches']):
                            matched=(candidate_ticker,candidate_at,candidate_raw,candidate_book,
                                     {**candidate,'ticker_observation':ticker_index,'book_observation':book_index})
                            break
                    if matched:
                        break
                if matched:
                    ticker,at,rawbook,book,alignment=matched
                    break
                if attempt==2:
                    raise _IncoherentSnapshots(alignment)
                # Independent REST endpoints can move between GETs. Re-read
                # both, never substitute a book price into the old ticker.
            positive(ticker['last'])
            if any(end!=now//BARS[bar]*BARS[bar] for bar,end in indicators['bar_end_ms'].items()):
                # The ticker/book capture itself crossed a bar boundary. Do
                # not combine a stale indicator window with the current book.
                indicators=self._indicators(instrument,start)
                now=self._now()
                fresh(at,now)
                fresh(book['cursor']['at_ms'],now)
            flow=taker_flow(pages,instrument,metadata['quote'],fx,start_ms=start-300000,end_ms=start)
            report={'instrument':instrument,'base':metadata['base'],'quote':metadata['quote'],'source_origin':TR_ORIGIN,
                    'data_status':self.data_status,
                    'as_of_ms':now,'ticker_ts_ms':at,'flow_window_end_ms':start,
                    'last_price_native':metric(positive(ticker['last'])),
                    'last_price_usdt_estimate':metric(quote_to_usdt(ticker['last'],metadata['quote'],fx,now)),
                    'volume_24h_native_quote':metric(number(ticker['volCcy24h'])),
                    'volume_24h_usdt':metric(quote_to_usdt(ticker['volCcy24h'],metadata['quote'],fx,now)),
                    'fx':fx if metadata['quote']=='TRY' else {'method':'USDT_IDENTITY','rate':'1'},
                    'indicators':indicators,'flow':flow,'book':book,'rules':metadata,
                    'capture_alignment':alignment,'live_enabled':False}
            report['execution']=execution_estimate(rawbook,metadata['quote'],fx,now,notional=self.policy.paper_notional,fee=self.fee_rate)
            report['selection']=score_flow(report,self.policy,btc_return)
            if max(number(report['execution'][k]) for k in ('buy_slippage','sell_slippage'))>self.policy.max_slippage:
                report['selection']['eligible']=False
                report['selection']['watch_only']=True
                report['selection']['reasons'].append('EXCESSIVE_SLIPPAGE')
            self.cursors[instrument]=book['cursor']
            return report
        except TradingError as error:
            if error.code=='FLOW_BOOK_RESYNC_REQUIRED':
                self.resync_required.add(instrument)
            raise
        except (KeyError,TypeError,ValueError):
            raise TradingError('INVALID_FLOW_DATA') from None

    @serialized
    @precise
    def scan_batch(self, *, offset=0, limit=3, expected_universe=None, previous=None, target=None):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=10:
            raise TradingError('INVALID_FLOW_BATCH')
        started=self._now()
        server=milliseconds(_one(self.client.fetch('time'))['ts'])
        if abs(server-self._now())>2000:
            raise TradingError('PUBLIC_CLOCK_SKEW')
        pairs,excluded=discover(self.client.fetch('instruments'))
        universe_hash=hashlib.sha256(canonical(pairs).encode()).hexdigest()
        if expected_universe is not None and expected_universe!=universe_hash:
            raise TradingError('FLOW_UNIVERSE_CHANGED_RESTART_SCAN')
        if offset>=len(pairs):
            raise TradingError('INVALID_FLOW_BATCH')
        tickers=self.client.fetch('tickers')
        fx=None
        try:
            fx=fx_rate(pairs,tickers,self._now())
        except TradingError:
            pass  # TRY pairs fail closed individually; USDT pairs remain usable.
        volume,volume_at,errors={},{},[]
        grouped={}
        for row in _rows(tickers):
            if isinstance(row,dict):
                grouped.setdefault(row.get('instId'),[]).append(row)
        for name,metadata in pairs.items():
            try:
                rows=grouped.get(name,[])
                if len(rows)!=1 or rows[0].get('instType')!='SPOT':
                    raise TradingError('FLOW_TICKER_MISSING_OR_DUPLICATE')
                fresh(rows[0]['ts'],self._now())
                volume_at[name]=milliseconds(rows[0]['ts'])
                volume[name]=metric(quote_to_usdt(rows[0]['volCcy24h'],metadata['quote'],fx,self._now()))
            except TradingError as error:
                errors.append({'instrument':name,'reason':error.code})
            except (KeyError,TypeError):
                errors.append({'instrument':name,'reason':'INVALID_FLOW_DATA'})
        if 'BTC-USDT' not in pairs:
            raise TradingError('BTC_REGIME_UNAVAILABLE')
        btc_bar=candles(self.client.fetch('flow_candles','BTC-USDT',bar='1H'),'1H',self._now())[0]
        # Derived division may repeat; serialize through the bounded metric
        # contract before score_flow validates it as an input. Raw exchange
        # price precision remains strictly checked by candles().
        btc_return=number(metric(btc_bar['close']/btc_bar['open']-1))
        names=sorted(pairs)
        if target is not None:
            # A pair is an exact request; only a bare coin aggregates its quotes.
            names=([target] if target in pairs else
                   [name for name in names if pairs[name]['base']==target])
            if not names:
                raise TradingError('FLOW_SYMBOL_NOT_DISCOVERED')
            if offset:
                raise TradingError('INVALID_FLOW_BATCH')
        batch=names[offset:offset+limit]
        reports={}
        for name in batch:
            try:
                reports[name]=self.analyze_pair(name,pairs,fx,btc_return)
            except TradingError as error:
                reports[name]={'error':error.code}
                if isinstance(error,_IncoherentSnapshots):
                    reports[name]['capture_alignment']=error.diagnostics
        now=self._now()
        if now-started>30000:
            raise TradingError('FLOW_BATCH_STALE_USE_SMALLER_BATCH')
        if any(now-r['as_of_ms']>30000 for r in reports.values() if 'error' not in r):
            raise TradingError('STALE_OR_FUTURE_FLOW_DATA')
        if btc_bar['at_ms']+BARS['1H'] != now//BARS['1H']*BARS['1H']:
            raise TradingError('BTC_REGIME_STALE')
        for name in list(volume):
            try:
                fresh(volume_at[name],now)
                if pairs[name]['quote']=='TRY':
                    fresh(fx['as_of_ms'],now)
            except TradingError as error:
                del volume[name]
                errors.append({'instrument':name,'reason':error.code})
        for name,report in list(reports.items()):
            if 'error' not in report:
                try:
                    fresh(report['book']['cursor']['at_ms'],now)
                except TradingError as error:
                    reports[name]={'error':error.code}
        if previous is not None and previous.get('universe_hash')!=universe_hash:
            raise TradingError('INCOMPARABLE_FLOW_ROTATION_BASELINE')
        # A newly missing/stale denominator must not erase otherwise valid
        # observed pair data. Shares/rotation remain unavailable and no coin
        # can rank; the last complete baseline is not replaced by partial data.
        comparison=previous if len(volume)==len(pairs) else None
        coins=aggregate_coins(reports,pairs,volume,now=now,previous=comparison,universe_hash=universe_hash)
        # An incomplete turnover denominator cannot produce trustworthy share/rank.
        if len(volume)!=len(pairs):
            for coin in coins:
                coin.update(eligible=False)
                coin['reasons'].append('INCOMPLETE_UNIVERSE_VOLUME')
                coin['volume_share_24h']=None
                coin['share_change']=None
        verified = self.real_transport and any('error' not in row for row in reports.values())
        return {'source_origin':TR_ORIGIN,'as_of_ms':now,'server_ts_ms':server,'live_enabled':False,
                'data_status':'VERIFIED_TR_PUBLIC' if verified else 'DATA_UNVERIFIED',
                'scope':'DISCOVERED_UNIVERSE_BOUNDED_BATCH_NOT_ALL_MARKET_SIMULTANEOUS',
                'universe_hash':universe_hash,'discovered_pairs':len(pairs),'excluded_quotes':excluded,
                'offset':offset,'next_offset':offset+len(batch) if offset+len(batch)<len(names) else None,
                'batch_pairs':batch,'pairs':reports,'coins':coins,'top5':[c for c in coins if c['eligible']][:5],
                'volume_errors':errors,'policy':{k:metric(v) for k,v in asdict(self.policy).items()},
                'rotation_baseline':{'universe_hash':universe_hash,'at_ms':now,'volumes':volume},
                'warnings':['Turnover müşteri bakiyesi değildir.','CVD net sermaye girişi değildir.',
                            'REST örneği; WS streaming yok.','Skor doğrulanmamış bir heuristiktir; kazanç olasılığı değildir.']}
