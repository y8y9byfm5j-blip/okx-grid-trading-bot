# Manuel şablonun mevcut Telegram owner'ına bağlanması

`okxtr-manual-observe-v1` yeni, ayrı bir **yalnız izleme** kurulumudur.
`create_observer` mevcut V5 owner'ına verilecek core/bridge/adapter nesnelerini
birlikte kurar. Token okumaz, GET yapmaz, poll döngüsü veya sender oluşturmaz,
V5 kaynaklarını import etmez, servis başlatmaz. Üretim bağlantısı tamamlanmadı.

## Etkin kurallar

`/kriterler` aşağıdaki değerleri karar motorunun gerçek policy nesnelerinden
okur; kopyalanmış bir yardım şablonundan üretmez. Trend sabitleri kararın
configuration kimliğine de katılır. Aynı snapshot, sermaye ve konfigürasyon
aynı karar kimliğini verir.

| Kontrol | Bu kurulumdaki eşik |
| --- | --- |
| Kaynak | Yalnız `https://tr.okx.com`, public Spot, GET; global fallback yok |
| 24h quote hacmi | En az 10.000.000 USDT |
| Spread | En fazla 10 bps = %0,10 |
| Defter | 400 alış + 400 satış; fiyat/zaman/sıra doğrulaması |
| Yakın derinlik | ±%0,5 bantta her yönde en az 1.500 USDT |
| Coin trendi | 15m, 1H ve 4H getirilerin her biri >= 0; 15m <= %5 |
| BTC rejimi | 1H <= −%2 veya 4H <= −%4 ise ret |
| Net/grid | Her kademede iki yönlü komisyon ve kayma sonrası en az %0,10 |
| Maliyet modeli | Her yönde %0,10 ücret ve %0,02 kayma varsayımı |
| Kademe hesabı | Bant, tick/lot/min size ve minimum tutarla en fazla 20 kademe |
| Minimum tutar | 5 USDT model tabanı; ayrıca public min size/tick/lot uygulanır |
| Hedef paper profili | 600 USDT; 2 x 300; bot başına 3 / toplam 6 USDT planlanan risk |
| Veri yaşı | En fazla 30 saniye; 2 saniyeden fazla gelecek zaman kabul edilmez |
| Momentum | Ayrı RVOL/agresif alış/CVD/kırılım/çoklu zaman filtresi; yönlü emir planı olmadan yalnız izleme |

4H >= 0 şartı, eski manuel tarifteki “güçlü düşüş olmasın” ifadesinden daha
katıdır; mevcut güvenlik kuralı gevşetilmedi. Skor altı zorunlu filtreyi aşamaz.
Ücret ve 5 USDT tutarı hesaba ait doğrulanmış gerçek ücret/minimum iddiası
değildir. Gerçek stop zararı 3 USDT ile garanti edilemez; fiyat boşluğu,
kayma veya likidite kaybı zararı büyütebilir.

Eski `PlanningPolicy` referans varsayılanı %0,01 ve `v5-paper-500` profili
değiştirilmedi. Bu yeni kurulum net/grid eşiğini açıkça %0,10 seçer ve ayrı
kurulum kimliğiyle sabitler. Dar bir bandın maliyetleri karşılamaması
`INSUFFICIENT_NET_GRID_RETURN` / `NO TRADE` üretebilir; işlem zorlanmaz.

## İncelenmiş staging owner'ındaki bağlantı sınırı

Kaynak/hash/unit/scheduler incelemesi tamamlandıktan sonra mevcut owner,
`okx_grid.v5_setup.create_observer` fonksiyonuna şu değerleri verir:

- Üretim ağacının dışında, yeni ve özel bir mutlak `.sqlite3` dosya yolu.
- Gerçek özel chat ve kullanıcı ID'leri; örnek veya chat-only ACL kullanılmaz.
- İnceleme sırasında mutabık kalınmış Telegram başlangıç offset'i.

Factory başlangıçta public istemciyi kurar, ağ isteği göndermez. Test istemcisi
enjekte edilirse veri her zaman `DATA_UNVERIFIED` kalır. Mevcut bir dosya
yeniden açılacaksa önce immutable salt okunur metadata incelemesi yapılır:
kurulum/planning/trend/stage/ACL kimliği uyuşmalı; recovery sidecar'ı varsa
durulur. Eski veya başka bir defter constructor çalışmadan reddedilir;
başlık/izin/muhasebe resetlenmez. Symlink, hardlink, üretim ağacı ve göreli yol
kabul edilmez. Yarım kalmış ilk kurulum otomatik benimsenmez.

Mevcut owner sırası [V5_OWNER_BRIDGE.md](V5_OWNER_BRIDGE.md) içindedir:
PENDING kurtarma; `adapter.poll_parameters()` ile kendi mevcut API çağrısı;
gerçek getUpdates JSON'unu `adapter.consume_updates(...)` içine verme;
`adapter.deliver_pending(existing_send)` ile aynı mevcut sender'ı kullanma.
Poll hatası `adapter.poll_failed()` üzerinden kaydedilir. Mevcut V5 `main`
gövdesi kaynaklar olmadan yeniden yazılmaz. Eski `/paper` rotası ayrıca
bağlanmadığından açık hata verir; yeni defteri eski bakiye olarak göstermez.

İzleme profili `/analiz`, `/grid`, `/gridplan`, `/radar`, `/paraakisi`,
`/momentum`, `/durum`, `/kriterler`, `/papergrid status/stop` ve kalıcı
`/emergency_stop` rotalarını hazırlar. `/papergrid start`, doğrudan core
start/cycle ve doğrudan trader open **OBSERVATION_ONLY_EXECUTION_DISABLED**
ile, yeni GET veya rezervasyon olmadan engellenir. Analizler pending execution
planına dönüşmez. Aynı defter paper aşamasına çevrilemez; sonraki aşama ayrı
incelenmiş defter ve kabul gerektirir.

## Telegram tesliminde veri yaşı

PENDING fiyat/GRID/radar/flow yanıtı gönderilmeden hemen önce komut zamanı,
as-of, ticker, defter ve varsa flow pencere sonu yeniden denetlenir. Veri
eskimişse veya saat geri gitmişse fiyat metni saklanır fakat gönderilmez;
`DATA_UNVERIFIED | STALE_TELEGRAM_REPORT` uyarısı hazırlanır ve girişler pause
edilir. Gerçekte gönderilecek metin UNKNOWN ile aynı transaction'da kaydedilir.
ACK yalnız bu metin için geçerlidir; eski fiyatın ACK'i uyarıyı onaylayamaz.
UNKNOWN/ACK otomatik tekrar gönderilmez. Konfigürasyon ve emergency confirmation
gibi fiyat içermeyen yanıtlar bu kuralla silinmez. Ağ gecikmesi sıfır değildir;
rapordaki kaynak zamanları kullanıcıya görünür kalır.

## Doğrulama

Offline kabul: `python3 tools/validate.py`. Yeni setup suite'i gerçek Telegram
veya gerçek piyasa kanıtı sayılmaz.

İzinli, izole ağ ortamındaki salt okunur gerçek TR kabulü:

```bash
python3 tools/v5_observer_smoke.py --allow-public-network --symbol SOL-USDT
```

Bu araç yalnız geçici ayrı defter oluşturur; sentetik bir native update
zarfından **gerçek TR transport** ile komut hazırlar, gözlem kilitlerini ve
600 nakit / 0 rezerv / 0 pozisyonu doğrular. Telegram'a göndermez; çıktı
`native_update_envelope=SYNTHETIC_TEST_INPUT`, `telegram_verified=false` ve
`production_activated=false` taşır. Enjekte edilen veri gerçek diye kabul
edilmez. Kaynak doğrulanmazsa exit 2 ve `DATA_UNVERIFIED` döner.

Bu tek REST snapshot kabulüdür. Sürekli veri, VPS erişimi, gerçek Telegram
ACK, V5 shadow ve üretim aktivasyonu ayrı [kabul kapılarıdır](LIVE_DATA_ACCEPTANCE.md).
`LIVE_TRADING=false`; private API/emir göndericisi yoktur.
