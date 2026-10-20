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

## Güncelleme: fiyat tick yaşam döngüsü, yarış güvenliği ve bildirim önizlemesi

- `advance_paper_tick` mevcut sanal botta sağlanan fiyat/tick ile **gerçek olmayan** alış/satış dolumlarını, TP/SL/kalıcı HALT ve muhasebe durumunu `PaperTrader.tick` üzerinden günceller.
- Tek sembolde iki farklı karar döngüsünün aynı anda açılış yapması, `PaperTrader.open(unique_instrument=True)` kontrolüyle **SQLite BEGIN IMMEDIATE işleminin içinde** reddedilir; eski manuel `open` davranışı değiştirilmez.
- `render_paper_notification` sanal giriş, güncelleme ve kapanış durumlarını Türkçe Telegram **mesaj önizlemesine** dönüştürür. Tekrar oynatmalarda bildirim üretmez. **Göndermez**, token/Telegram API kullanmaz.
- Tüm uyarılarda `PAPER` ve gerçek emir gönderilmediği belirtilir. Telegram gerçek komut yetkilendirmesi, chat allowlist, botun tek polling oturumu ve teslim edilmiş bildirimlerin kalıcı idempotency defteri **henüz yoktur**.
- Yaşam döngüsü testleri stok/kâr-zarar, TP/SL gap, restart, tekrar tick, stale, muhasebe ve eşzamanlı iki farklı cycle ID için tek sembol kısıtlamasını kapsar.

Bu dal **hala bir otomatik çalışan servis değildir**: ticaret sinyalleri, trend ve kitap verileri dışarıdan sağlanır; market feed, scheduler, Telegram send/receive veya gerçek emir aktarımı henüz bağlanmamıştır. İşlem/Telegram çiftliği oluşturulmaz; VPS kaynak koduyla gerçek uyumluluk görülmeden birleştirme yapılmaz.

## Yeni: salt okunur OKX TR HTTP transport'u

`ReadOnlyTRClient`, resmi `https://tr.okx.com` kökü üzerinde **yalnızca belgeli public GET** uç noktalarına istek göndermek için ayrı bir modüldür. Kişisel API anahtarı, passphrase, POST/order/cancel, Telegram çağrısı, zamanlayıcı veya otomatik canlı işleme dair kod içermez. Sabit host, HTTPS sertifika doğrulaması, redirect/proxy reddi, bounded response ve timeout, bozuk/nonzero/boş yanıtların fail-closed değerlendirmesi mock testlerle doğrulanır.

**Bu bağlantı henüz gerçek TR servisine canlı smoke test yapılarak doğrulanmadı.** Modül yalnız açık `fetch(...)` çağrısı üzerine GET yapar; bu dalda kendiliğinden çalışan servis veya bütünleşmiş radar pipeline'ı yoktur. Rate limit kontrolü, endpoint pacing, connection health, gerçek TR parite doğrulaması ve V5 adapter'i yapılmadan üretim radarına bağlanamaz.
