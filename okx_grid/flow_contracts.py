"""READ-ONLY V6 spot contracts; turnover/taker delta are NOT customer holdings."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
import hashlib
import re

from .domain import TradingError, canonical, number, positive, precise
from .public_contracts import TR_ORIGIN, _rows, milliseconds

@precise
def metric(value):
    """Bound derived metrics to 18 decimal places; keep ledger math separate."""
    value = value if isinstance(value, Decimal) else number(value)
    if not value.is_finite() or abs(value)>Decimal('1e36'):
        raise TradingError('INVALID_FLOW_METRIC')
    result=format(value.quantize(Decimal('1e-18'),rounding=ROUND_UP),'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


BARS = {"1m":60000, "5m":300000, "15m":900000, "1H":3600000}


def spot_symbol(value):
    if not isinstance(value,str) or not re.fullmatch(r"[A-Z0-9]{1,24}-[A-Z0-9]{2,12}",value):
        raise TradingError("INVALID_FLOW_SPOT_SYMBOL")
    return value


def fresh(at, now, age=30000):
    at, now = milliseconds(at), milliseconds(now)
    if now-at > age or at-now > 2000:
        raise TradingError("STALE_OR_FUTURE_FLOW_DATA")
    return at


def discover(reply):
    pairs, excluded, seen = {}, [], set()
    for row in _rows(reply):
        try:
            name = spot_symbol(row['instId'])
            if name in seen:
                raise TradingError('DUPLICATE_FLOW_INSTRUMENT')
            seen.add(name)
            if row['instType'] != 'SPOT' or row['state'] != 'live':
                continue
            if name != row['baseCcy']+'-'+row['quoteCcy']:
                raise TradingError('FLOW_INSTRUMENT_MISMATCH')
            if row['quoteCcy'] not in {'USDT','TRY'}:
                excluded.append({'instrument':name,'reason':'UNSUPPORTED_QUOTE_FX'})
                continue
            pairs[name] = {'base':row['baseCcy'],'quote':row['quoteCcy'],
                           'tick_size':metric(positive(row['tickSz'])), 'lot_size':metric(positive(row['lotSz'])),
                           'min_size':metric(positive(row['minSz']))}
        except (KeyError, TypeError):
            raise TradingError('INVALID_FLOW_INSTRUMENT') from None
    if not pairs:
        raise TradingError('NO_SUPPORTED_TR_SPOT_PAIRS')
    return pairs, excluded


@precise
def fx_rate(pairs, tickers, now):
    """Current USDT/TRY midpoint, not historical VWAP or true USD conversion."""
    if 'USDT-TRY' not in pairs:
        raise TradingError('TRY_FX_UNAVAILABLE')
    rows = [r for r in _rows(tickers) if isinstance(r,dict) and r.get('instId')=='USDT-TRY']
    if len(rows)!=1 or rows[0].get('instType')!='SPOT':
        raise TradingError('TRY_FX_UNAVAILABLE')
    row=rows[0]
    try:
        at=fresh(row['ts'],now)
        bid,ask=positive(row['bidPx']),positive(row['askPx'])
        if bid>=ask or (ask-bid)/((ask+bid)/2)*10000>10:
            raise TradingError('TRY_FX_SPREAD')
        return {'try_per_usdt':metric((bid+ask)/2),'source':TR_ORIGIN+'/api/v5/market/tickers',
                'instrument':'USDT-TRY','as_of_ms':at,'method':'CURRENT_MIDPOINT_ESTIMATE'}
    except (KeyError,TypeError):
        raise TradingError('TRY_FX_UNAVAILABLE') from None


@precise
def quote_to_usdt(value, quote, fx, now):
    value=number(metric(value)) if isinstance(value,Decimal) else number(value)
    if value<0:
        raise TradingError('NEGATIVE_FLOW_VOLUME')
    if quote=='USDT':
        return value
    if quote!='TRY' or not isinstance(fx,dict) or fx.get('source')!=TR_ORIGIN+'/api/v5/market/tickers' or fx.get('instrument')!='USDT-TRY':
        raise TradingError('TRY_FX_UNAVAILABLE')
    fresh(fx['as_of_ms'],now)
    return value/positive(fx['try_per_usdt'])


def candles(reply, bar, now):
    if bar not in BARS:
        raise TradingError('UNSUPPORTED_FLOW_BAR')
    period=BARS[bar]
    complete=[]
    previous=None
    for row in _rows(reply):
        if not isinstance(row,list) or len(row)!=9:
            raise TradingError('INVALID_FLOW_CANDLE')
        at=milliseconds(row[0])
        if at%period or at>now or previous is not None and previous-at!=period:
            raise TradingError('FLOW_CANDLE_GAP_OR_ORDER')
        previous=at
        opening,high,low,close=map(positive,row[1:5])
        if low>min(opening,close) or high<max(opening,close) or high<low or any(number(v)<0 for v in row[5:8]):
            raise TradingError('INVALID_FLOW_CANDLE')
        if not isinstance(row[8], str) or row[8] not in {'0','1'} or (row[8]=='1')!=(at+period<=now):
            raise TradingError('INVALID_FLOW_CANDLE_CONFIRM')
        if row[8]=='1':
            complete.append({'at_ms':at,'open':opening,'close':close,'quote_volume':number(row[7])})
    if not complete or complete[0]['at_ms']!=now//period*period-period:
        raise TradingError('MISSING_COMPLETED_FLOW_CANDLE')
    if bar=='5m' and len(complete)<21:
        raise TradingError('RVOL_REQUIRES_21_COMPLETE_BARS')
    return complete


@precise
def candle_indicators(replies, now):
    bars={bar:candles(replies[bar],bar,now) for bar in BARS}
    history=bars['5m'][1:21]
    mean=sum((c['quote_volume'] for c in history),Decimal(0))/20
    if mean<=0:
        raise TradingError('RVOL_BASELINE_ZERO')
    return {'rvol_5m':metric(bars['5m'][0]['quote_volume']/mean),
            'rvol_basis':'QUOTE_TURNOVER_20_PREVIOUS_COMPLETE_5M',
            'return_1m':metric(bars['1m'][0]['close']/bars['1m'][0]['open']-1),
            'return_15m':metric(bars['15m'][0]['close']/bars['15m'][0]['open']-1),
            'return_1h':metric(bars['1H'][0]['close']/bars['1H'][0]['open']-1),
            'bar_end_ms':{bar:rows[0]['at_ms']+BARS[bar] for bar,rows in bars.items()}}


@precise
def taker_flow(pages, instrument, quote, fx, *, start_ms, end_ms):
    """Window (start,end], de-duplicated by pair+trade ID. Coverage boundary required.

    ID differences do not prove missing trades; pagination must reach the start.
    CVD here is quote-notional taker delta, not external net capital inflow.
    """
    spot_symbol(instrument)
    if instrument.rsplit('-',1)[1]!=quote:
        raise TradingError('FLOW_QUOTE_MISMATCH')
    if not isinstance(pages,list) or not 1<=len(pages)<=11 or end_ms-start_ms!=300000:
        raise TradingError('INVALID_FLOW_TRADE_WINDOW')
    seen, boundary, latest = {}, False, None
    buy=sell=base_delta=Decimal(0)
    for page_index,page in enumerate(pages):
        previous=None
        rows=_rows(page)
        if len(rows)>(500 if page_index==0 else 100):
            raise TradingError("FLOW_TRADE_PAGE_TOO_LARGE")
        for row in rows:
            try:
                tid=row['tradeId']
                if row['instId']!=instrument or row['side'] not in {'buy','sell'} or not isinstance(tid,str) or not tid.isascii() or not tid.isdigit() or len(tid)>40:
                    raise TradingError('INVALID_FLOW_TRADE')
                at=milliseconds(row['ts'])
                if at>end_ms+2000 or previous is not None and at>previous:
                    raise TradingError('FLOW_TRADE_TIME_ORDER')
                previous=at
                px,size=positive(row['px']),positive(row['sz'])
                digest=hashlib.sha256(canonical(row).encode()).hexdigest()
                if tid in seen:
                    if seen[tid]!=digest:
                        raise TradingError('CONFLICTING_FLOW_TRADE')
                    continue
                seen[tid]=digest
                latest=max(latest or at,at)
                boundary=boundary or at<=start_ms
                if start_ms<at<=end_ms:
                    amount=quote_to_usdt(px*size,quote,fx,end_ms)
                    if row['side']=='buy':
                        buy+=amount
                        base_delta+=size
                    else:
                        sell+=amount
                        base_delta-=size
            except (KeyError,TypeError):
                raise TradingError('INVALID_FLOW_TRADE') from None
    if not boundary:
        raise TradingError('INCOMPLETE_FLOW_TRADE_WINDOW')
    fresh(latest or 0,end_ms)
    if buy+sell<=0:
        raise TradingError('NO_EXECUTED_FLOW_CONFIRMATION')
    return {'buy_usdt':metric(buy),'sell_usdt':metric(sell),'cvd_quote_usdt':metric(buy-sell),'base_taker_delta':metric(base_delta),
            'buy_fraction':metric(buy/(buy+sell)), 'buy_sell_ratio':metric(buy/sell) if sell else None,
            'ratio_state':'ALL_BUY' if not sell else 'FINITE','start_ms':start_ms,'end_ms':end_ms,
            'unique_trades':len(seen),'meaning':'EXECUTED_TAKER_IMBALANCE_NOT_NET_DEPOSITS'}


@precise
def book_snapshot(reply, instrument, now, *, previous=None, quote='USDT', fx=None):
    spot_symbol(instrument)
    if instrument.rsplit('-',1)[1]!=quote:
        raise TradingError('FLOW_QUOTE_MISMATCH')
    rows=_rows(reply)
    if len(rows)!=1 or not isinstance(rows[0],dict):
        raise TradingError('INVALID_FLOW_BOOK')
    row=rows[0]
    try:
        at=fresh(row['ts'],now)
        seq=row['seqId']
        if type(seq) is not int or seq<0:
            raise TradingError('INVALID_BOOK_SEQUENCE')
        sides={}
        for side in ('bids','asks'):
            levels=row[side]
            if not isinstance(levels,list) or not 1<=len(levels)<=400:
                raise TradingError('INVALID_FLOW_BOOK')
            normalized=[]
            for level in levels:
                if not isinstance(level,list) or len(level)!=4 or level[2]!='0' or not isinstance(level[3],str) or not level[3].isdigit():
                    raise TradingError('INVALID_FLOW_BOOK')
                normalized.append((positive(level[0]),positive(level[1])))
            if any((a[0]<=b[0] if side=='bids' else a[0]>=b[0]) for a,b in zip(normalized,normalized[1:])):
                raise TradingError('UNSORTED_FLOW_BOOK')
            sides[side]=normalized
        bid,ask=sides['bids'][0][0],sides['asks'][0][0]
        if bid>=ask:
            raise TradingError('CROSSED_FLOW_BOOK')
        digest=hashlib.sha256(canonical({'instrument':instrument,'bids':row['bids'],'asks':row['asks']}).encode()).hexdigest()
        cursor={'instrument':instrument,'sequence':seq,'at_ms':at,'digest':digest}
        if previous is not None and (previous['instrument']!=instrument or seq<previous['sequence'] or at<previous['at_ms'] or seq==previous['sequence'] and digest!=previous['digest']):
            raise TradingError('FLOW_BOOK_RESYNC_REQUIRED')
        mid=(bid+ask)/2
        depth={}
        for name,band in (('0.5pct',Decimal('.005')),('1pct',Decimal('.01'))):
            buy=quote_to_usdt(sum((p*q for p,q in sides['bids'] if p>=mid*(1-band)),Decimal(0)),quote,fx,now)
            sell=quote_to_usdt(sum((p*q for p,q in sides['asks'] if p<=mid*(1+band)),Decimal(0)),quote,fx,now)
            depth[name]={'bid_usdt':metric(buy),'ask_usdt':metric(sell),'imbalance':metric((buy-sell)/(buy+sell)) if buy+sell else '0',
                         'bid_band_covered':sides['bids'][-1][0]<=mid*(1-band),'ask_band_covered':sides['asks'][-1][0]>=mid*(1+band)}
        return {'cursor':cursor,'bid':metric(bid),'ask':metric(ask),'mid':metric(mid),'spread_bps':metric((ask-bid)/mid*10000),
                'depth':depth,'bid_levels':len(sides['bids']),'ask_levels':len(sides['asks']),'transport':'REST_SNAPSHOT'}
    except (KeyError,TypeError):
        raise TradingError('INVALID_FLOW_BOOK') from None


@dataclass(frozen=True)
class FlowPolicy:
    min_rvol: Decimal=Decimal('3')
    min_quote_volume: Decimal=Decimal('10000000')
    min_return: Decimal=Decimal('.01')
    max_return: Decimal=Decimal('.05')
    max_spread_bps: Decimal=Decimal('10')
    min_depth_usdt: Decimal=Decimal('2500')
    min_buy_fraction: Decimal=Decimal('.55')
    paper_notional: Decimal=Decimal('90')
    max_slippage: Decimal=Decimal('.002')

    def __post_init__(self):
        for key in self.__dataclass_fields__:
            object.__setattr__(self,key,positive(getattr(self,key)))
        if not self.min_return<self.max_return<=Decimal('.2') or not Decimal('.5')<self.min_buy_fraction<1 or self.paper_notional>500 or self.max_slippage>Decimal('.05'):
            raise TradingError('INVALID_FLOW_POLICY')


@precise
def score_flow(report, policy, btc_return):
    indicators,flow,book=report['indicators'],report['flow'],report['book']
    reasons=[]
    tests=((number(report['volume_24h_usdt'])>=policy.min_quote_volume,'LOW_QUOTE_VOLUME'),
           (number(indicators['rvol_5m'])>=policy.min_rvol,'LOW_RVOL'),
           (policy.min_return<=number(indicators['return_15m'])<=policy.max_return,'OUTSIDE_EARLY_MOMENTUM'),
           (number(indicators['return_1h'])>=0,'DOWNTREND'),(number(btc_return)>Decimal('-.02'),'BTC_SHOCK'),
           (number(book['spread_bps'])<=policy.max_spread_bps,'SPREAD_TOO_WIDE'),
           (all(number(book['depth']['0.5pct'][k])>=policy.min_depth_usdt for k in ('bid_usdt','ask_usdt')),'LOW_DEPTH'),
           (number(flow['cvd_quote_usdt'])>0 and number(flow['buy_fraction'])>=policy.min_buy_fraction,'NO_TAKER_CONFIRMATION'))
    reasons=[reason for passed,reason in tests if not passed]
    # Explainable heuristic, never a probability or a claim of alpha.
    parts={'rvol':min(30, int(number(indicators['rvol_5m'])/policy.min_rvol*15)),
           'momentum':20 if tests[2][0] and tests[3][0] else 0,
           'taker':int(number(flow['buy_fraction'])*30), 'liquidity':20 if tests[0][0] and tests[5][0] and tests[6][0] else 0}
    return {'eligible':not reasons,'watch_only':bool(reasons),'reasons':reasons,'score':sum(parts.values()),'score_breakdown':parts,
            'score_meaning':'UNVALIDATED_HEURISTIC_NOT_WIN_PROBABILITY','live_enabled':False}
