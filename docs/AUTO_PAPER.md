# Otomatik PAPER karar kapısı — izole geliştirme

Bu modül **gerçek OKX TR işlem gönderimi yapmaz**. Telegram'a bağlanmaz, zamanlayıcı başlatmaz, fiyat veya emir defteri çekmez. `consider_paper_entry`, çağıranın verdiği sentetik veya ileride **doğrulanmış public market** girdilerini yalnızca mevcut `radar.screen` risk filtreleri ve `PaperTrader.open` muhasebe motoruyla değerlendirir.

- `cycle_id` her değerlendirme için kalıcı ve benzersiz olmalıdır; kimlikten türetilmiş paper bot ID yeniden başlatmada tekrar açılışı önler.
- Tek çağrıda **en fazla bir yeni sanal bot** açılır; hâlihazırda aktif aynı coin için yeni bot açılmaz.
- `v5-paper-500` sürümlü profili zorunludur; aksi durumda `AUTO_REQUIRES_EXPLICIT_500_PROFILE` hatası verir.
- Veri eksikliği, geçersiz trend, stale fiyat, risk limiti ve kalıcı halt işlemi durdurur. Yalnızca önceden sağlanan veri üzerinden karar verir; verinin gerçek TR piyasasından geldiğini kendisi ispatlamaz.
- Tekrarlanan `cycle_id` ve farklı coin/plan çakışması sessizce yeni açılış yapmak yerine hata üretir. Sadece paper ledger kayıtları etkilenir; canlı borsa veya Telegram'a istekte bulunulmaz.

## Sentetik kullanım örneği

```python
from okx_grid.automation import consider_paper_entry
from okx_grid.paper import PaperTrader
from okx_grid.profiles import load_profile
from tests.helpers import proposal
from okx_grid.scenarios import sample_plan

candidate = proposal()  # TEST-USDT ve tamamıyla sentetik book / trend
candidate["plan"] = sample_plan(capital="90").to_dict()
with PaperTrader(":memory:", profile=load_profile("v5-paper-500")) as trader:
    decision = consider_paper_entry(trader, [candidate], cycle_id="synthetic_1", now=1000)
    assert decision["decision"] == "OPENED"
    assert consider_paper_entry(trader, [candidate], cycle_id="synthetic_1", now=1000)["decision"] == "DUPLICATE_CYCLE"
```

**Bu örnek üretim feed'i veya Telegram komutları değildir.** İşlem bildirimlerini ayrı bir Telegram adaptörü yayınlamalıdır; motorun içine bir ikinci polling istemcisi eklenmemelidir. VPS V5 imzaları ve gerçek OKX TR market-data kaynağı doğrulanmadan otomatik üretim servisinde kullanılmaz.

## Hâlâ gerekenler

1. VPS'teki özgün dokuz V5 dosyasının güvenli incelenmesi ve komut/format uyumu.
2. OKX TR public HTTP/WebSocket gerçek piyasa verisi için salt okunur doğrulama.
3. Her açık paper bot için takip eden tick'ler, TP/SL, muhasebe ve tek Telegram sahibi üzerinden bildirimlerin uçtan uca testi.
4. Tek zamanlayıcıyla uzun süreli shadow çalışma, çökme sonrası toparlanma, sağlık göstergesi, strateji etkinlik analizi.
5. Gerçek emirler için ayrı branch, minimum yetki, emir eşleştirme ve ayrıca kullanıcı go-live onayı.

Referans: [V5 uyumluluk matrisi](../V5_COMPATIBILITY.md), [canlı işlem hazırlık kapıları](LIVE_TRADING_READINESS.md).

## Reviewed integration: atomic cycle and offline command boundary

SOL PR #3 entry logic is incorporated with additional safeguards. Every decision,
including rejection, has a durable whole-input fingerprint. A single SQLite write
transaction covers portfolio checks, same-symbol exclusion and opening. Nested
operations use savepoints; a cycle failure rolls back its fills, entry, notice
and receipt together. Existing legacy entry events without receipts are rejected.

`okx_grid.cycle.run_cycle` is a callable supplied-data boundary, not a scheduler.
It requires the explicit 500 profile and a pinned symbol allowlist. It processes
existing bots' valid quotes before at most one new entry and writes a private
notification outbox. Missing/stale/out-of-order quotes, unhealthy notifications
or UNKNOWN delivery persistently pause new entries and buys; valid sell/TP/SL
handling continues. No automatic resume is provided. `halt` separately closes
paper positions on their next valid quote. Pending notifications are not sent by
this package. An UNKNOWN delivery must be reconciled, never blindly retransmitted.

`okx_grid.telegram_control.handle_update` is an offline reference interface.
Both user and private-chat identity must match the pinned ACL; forwarded, edited,
bot-origin, stale and unsupported commands are rejected. Durable update IDs,
content fingerprints, cursor and rate checks prevent duplicate side effects.
The reference accepts `/durum`, `/analiz SYMBOL-USDT`, `/radar`, `/grid`, `/paper`,
`/paper pause`, `/paper halt`. Responses contain actual paper ledger state and
explicit `V5_NOT_CONNECTED` / `NOT_SENT` markers. Analysis/radar results are not
fabricated. These syntax choices do not prove existing V5 command compatibility.

No Telegram poller/sender, real order client, V5 adapter, automatic service, daily
UTC loss policy or future exchange intent/reconciliation state machine is included.
The existing lifetime realized-loss fuse is 10 USDT; it is not a tested daily-loss
guard. Those remaining gates are tracked in `docs/LIVE_TRADING_READINESS.md` and
Issue #2. There is no claim of a live feed, live readiness or completed V5 migration.
