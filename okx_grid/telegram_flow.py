"""V6 dispatch hook for the existing single V5 owner. Never polls or sends."""
from .automation import _paper_500_only
from .control import pin, queue_notice, receipt, record, snapshot
from .domain import TradingError, paper_only
from .telegram_control import handle_update
from .telegram_market import handle_market_update


def render_flow(scan, *, detailed=False, suppressed=None):
    lines=['V6 SMART MONEY FLOW | READ-ONLY / PAPER',
           'Kaynak https://tr.okx.com | REST snapshot | as-of ms '+str(scan['as_of_ms']),
           f"Keşfedilen {scan['discovered_pairs']} parite; bu batch {len(scan['batch_pairs'])}; sonraki offset {scan['next_offset']}",
           'Batch kapsamı; tüm piyasa eşzamanlı sıralaması değildir.']
    shown=scan['coins'][:1] if detailed else scan['top5']
    if not shown:
        lines.append('YENİ ALARM YOK; tekrarlar veya veri/aday filtreleri nedeniyle sinyal üretilmez.')
        lines.extend('İZLE/RET '+c['coin']+': '+','.join(c['reasons']) for c in scan['coins'][:5])
        lines.extend('VERİ HATASI '+name+': '+r['error'] for name,r in scan['pairs'].items() if 'error' in r)
    for coin in shown:
        leader=coin['leader']
        lines.extend([f"{coin['coin']} ({','.join(coin['pairs'])}) | skor {coin['score']} / 100 | "+('ADAY' if coin['eligible'] else 'İZLE/RET'),
                      f"Skor bileşenleri {coin['score_breakdown']} | {','.join(coin['reasons']) or 'filtreler geçti'}",
                      f"RVOL {leader['indicators']['rvol_5m']} | 15m {leader['indicators']['return_15m']} / 1H {leader['indicators']['return_1h']}",
                      f"CVD taker farkı {coin['cvd_quote_usdt']} USDT | buy oranı {leader['flow']['buy_fraction']}",
                      f"24h turnover {leader['volume_24h_usdt']} USDT | quote {leader['quote']} | FX {leader['fx']['method']}",
                      f"Spread {leader['book']['spread_bps']} bps | 0,5% bid/ask derinliği "+str(leader['book']['depth']['0.5pct']),
                      f"Hacim payı {coin['volume_share_24h']} / pay değişimi {coin['share_change']}"])
        if detailed:
            lines.extend(['Giriş koşulu: bütün veri/risk filtreleri ve gerçekleşen taker teyidi devam etmeli; emir/önerilen giriş fiyatı yok.',
                          'Geçersizlik: stale veri, BTC şoku, taker teyit kaybı, spread/slippage veya derinlik limiti ihlali.',
                          'Hedef/stop fiyatı ve risk/getiri: yapılandırılmış plan yok, HESAPLANMADI.'])
    if suppressed:
        lines.append('Cooldown/dedup nedeniyle tekrar alarm verilmedi: '+','.join(suppressed))
    lines.extend(['Hacim müşteri bakiyesi değildir; CVD net sermaye girişi değildir.',
                  'Skor bir heuristiktir; kazanç tahmini değildir. Gerçek emirler KAPALI.'])
    text='\n'.join(lines)
    if len(text)>3500:
        # Deterministic bounded output; retain complete structured result privately.
        text='\n'.join(lines[:4]+[f"{c['coin']} skor {c['score']} | "+','.join(c['reasons']) for c in shown]+lines[-2:])
    return text


def handle_flow_update(trader, scanner, update, *, allowed_chat, allowed_user, now,
                       market_service=None, universe=None, grid_template=None, cooldown_seconds=900):
    paper_only()
    _paper_500_only(trader)
    update,_=snapshot(update)
    # Existing grid/status/paper routes retain their tested semantics and ACL.
    text=update.get('message',{}).get('text') if isinstance(update,dict) else None
    if text not in {'/paraakisi','/radar'} and not (isinstance(text,str) and text.startswith('/analiz ')):
        if market_service is None:
            return handle_update(trader,update,allowed_chat=allowed_chat,allowed_user=allowed_user,now=now)
        return handle_market_update(trader,market_service,update,allowed_chat=allowed_chat,allowed_user=allowed_user,
                                    now=now,universe=universe,grid_template=grid_template)
    if type(cooldown_seconds) is not int or not 60<=cooldown_seconds<=86400:
        raise TradingError('INVALID_FLOW_COOLDOWN')
    accepted=handle_update(trader,update,allowed_chat=allowed_chat,allowed_user=allowed_user,now=now)
    uid=update['update_id']
    with trader._transaction():
        pin(trader,'flow_policy',{k:str(getattr(scanner.policy,k)) for k in scanner.policy.__dataclass_fields__}|{'cooldown':cooldown_seconds})
        previous=receipt(trader,'flow_command:'+str(uid),'flow_command')
        if previous:
            return {**previous['result'],'duplicate':True}
        if accepted['duplicate']:
            return {'ok':False,'duplicate':True,'reason':'FLOW_OUTCOME_UNKNOWN_NEW_UPDATE_REQUIRED',
                    'message':'VERİ ALINAMADI - ANALİZ YOK\nÖnceki flow sonucu belirsiz; yeni komut gerekli.', 'live_enabled':False,'delivery':'NOT_SENT'}
        offset=trader._meta('flow_scan_offset') or 0
        expected=trader._meta('flow_scan_universe') if offset else None
    try:
        # Sequential bounded batches cover the discovered universe over repeated
        # existing-owner cycles; top5 is explicitly scoped to each current batch.
        detailed=text.startswith('/analiz ')
        with trader._transaction():
            baseline=trader._meta('flow_rotation_baseline')
        if baseline is not None and scanner._now()<=baseline['at_ms']:
            baseline=None
        scan=scanner.scan_batch(offset=0 if detailed else offset,limit=3,expected_universe=None if detailed else expected,
                                previous=baseline,target=text.split()[1] if detailed else None)
        if not any('error' not in r for r in scan['pairs'].values()):
            raise TradingError('NO_VALID_FLOW_DATA')
        if detailed:
            wanted=text.split()[1]
            found=[c for c in scan['coins'] if c['coin']==wanted or wanted in c['pairs']]
            # Never silently show another coin. Target discovery is a follow-up
            # fetch hook; until the relevant batch is reached no analysis claimed.
            if not found:
                raise TradingError('SYMBOL_NOT_IN_CURRENT_FLOW_BATCH')
            scan={**scan,'coins':found,'top5':[c for c in found if c['eligible']]}
        result={'ok':True,'duplicate':False,'scan':scan,'live_enabled':False,'delivery':'NOT_SENT'}
        with trader._transaction():
            suppressed=[]
            alerts=[]
            for coin in scan['top5']:
                key='flow_alert_last_'+coin['coin']
                last=trader._meta(key)
                if text=='/radar' and last is not None and now-last<cooldown_seconds:
                    suppressed.append(coin['coin'])
                else:
                    alerts.append(coin['coin'])
                    if text=='/radar':
                        trader._set_meta(key,now)
            result['alerts']=alerts
            result['suppressed']=suppressed
            rendered_scan={**scan,'top5':[c for c in scan['top5'] if c['coin'] in alerts]} if text=='/radar' else scan
            result['message']=render_flow(rendered_scan,detailed=detailed,suppressed=suppressed)
            if not detailed:
                trader._set_meta('flow_scan_offset',scan['next_offset'] or 0)
                trader._set_meta('flow_scan_universe',scan['universe_hash'])
            if not scan['volume_errors']:
                trader._set_meta('flow_rotation_baseline',scan['rotation_baseline'])
            queue_notice(trader,'flow_'+str(uid),result['message'],now)
            record(trader,'flow_command:'+str(uid),'flow_command',now,{'result':result})
            return result
    except TradingError as error:
        result={'ok':False,'duplicate':False,'reason':error.code,'live_enabled':False,'delivery':'NOT_SENT',
                'message':'VERİ ALINAMADI - ANALİZ YOK\n'+error.code}
        with trader._transaction():
            if error.code in {'FLOW_UNIVERSE_CHANGED_RESTART_SCAN','INCOMPARABLE_FLOW_ROTATION_BASELINE'}:
                trader._set_meta('flow_rotation_baseline',None)
                trader._set_meta('flow_scan_offset',0)
            record(trader,'flow_command:'+str(uid),'flow_command',now,{'result':result})
        return result
