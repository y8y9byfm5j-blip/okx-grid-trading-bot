# V5 / V6 / V7 — 600 USDT paper entegrasyon adayı

Devam kontrolü (22 Ekim): kullanıcı erişim işlemlerini tamamladığını bildirdi.
Bu oturumda V5 dizini/SSH config/agent hâlâ yok; DigitalOcean konsolu bu çalışma
tarayıcısında `Site Unavailable` verdi. Yerel gerçek V7 public smoke yeniden
`PUBLIC_HTTP_UNAVAILABLE` / exit 2 döndü. GitHub dalı değişmemişti; erişim veya
özgün kaynak aktarımı doğrulanamadı. Üretime dokunulmadı.

`.github/workflows/tr-public-probe.yml` ayrı GitHub runner'ında anahtarsız,
GET-only BTC/SOL V7 ve BTC V6 kabul testlerini çalıştırır. Bu, alternatif API
origin'i/proxy değildir; yalnız aynı TR istemcisinin farklı, izole bir test
ortamında çalıştırılmasıdır. Sonuç runner konumuyla sınırlıdır ve yerel/VPS
DATA_UNVERIFIED durumunu değiştirmez. Hiçbir servise bağlanmaz, emir veya paper
pozisyon açmaz. Sonuçlar çalışma logu/summary üzerinden raporlanır; VPS/Telegram
kapıları bağımsız olarak açık kalır.

İlk izole public probe (`c649f89928234984ce8d4040fd30e20b98b3aacf`, Actions
`39738156186`): üç istek yolu da `PUBLIC_RESPONSE_TOO_LARGE` / exit 2 ile
reddedildi. Aynı commit'in üç sürümlü offline CI'ları 344 testle geçti.
Transport'a yalnız allowlisted istek adı/yolu, HTTP durum kodu, normalize MIME,
sayısal byte boyutu ve HTML/JSON sınıfı teşhisi eklendi; yanıt gövdesi, cookie,
sunucu header'ları veya hata metinleri kaydedilmez. HTML, `PUBLIC_NON_JSON_RESPONSE`
ile reddedilir. JSON byte üst sınırı artırılmadı; gerçek API kapısı kapalıdır.

İkinci probe (`0687d36ffa07e2802b7fede1040e5cfdf342035d`, Actions
`39738793179`) teşhisi: `time` sonrası `/api/v5/public/instruments` HTTP 200,
JSON yanıtı 1.000.001 byte sınırında kesiliyor. HTML veya doğrulanmış piyasa
snapshot'ı söz konusu değil; katalog henüz tamamıyla okunamadı. Yalnız üç piyasa
consumer'ının enstrüman kataloğu için açık 8 MB bütçesi eklendi. Generic istemci
varsayılanı ve kitap/ticker/mum sınırları korunur; 5000 satır ve JSON şema kontrolü
devam eder. Katalog büyümesi global veri kaynağına geçiş gerekçesi değildir.

22 Ekim 2026. Bu kaynaklar mevcut V5 için çağrılabilir entegrasyon sınırıdır.
Yeni Telegram botu, poller, scheduler, gönderen veya üretim veritabanı kurulmaz.
**Üretim V5 entegrasyonu BLOKE; gerçek emirler KAPALI.**

## 1. Tamamlanan geliştirmeler

- `v7_market.py`: keşfedilen TR SPOT/USDT çiftleri, sabit TR origin, zaman kontrolü,
  tam 400 alış + 400 satış seviyesi, kurallar, 20 tamamlanmış 15m/1H/4H mum,
  BTC 1H/4H, ATR, destek/direnç ve kimliklendirilmiş REST snapshot.
- `v7_decision.py`: tek plan/karar kaynağı. Snapshot + sürümlü profil + sermaye +
  ücret/kayma varsayımı karar kimliğini belirler. `/analiz`, `/grid`, `/gridplan`
  ve GRID radarı aynı görüntü için aynı net getiri ve risk hesabını kullanır.
- Skor ağırlıkları: veri 20, likidite 15, trend 15, BTC 15, grid ekonomisi 20,
  planlanan risk 15. **Altı zorunlu filtrenin tamamı gerekir.** Yüksek gösterim
  puanı başarısız filtreyi geçersiz kılamaz. Gerçek veri kapısı ayrıca zorunludur.
- `commands.py` / `telegram_v7.py`: açık kayıt tablosu, harf duyarsız komutlar,
  sembol normalleştirmesi, ACL, receipt/replay ve NOT_SENT bildirim kuyruğu.
  Mevcut `/paper`, `/paper pause`, `/paper halt` yolları korunur.
- `v7_core.py`: GRID ve erken momentum radarları aynı portföye bağlıdır.
  Momentum adayı mevcut GRID riskini tekrarlayamaz. Yönlü yürütme planı henüz
  tanımlı olmadığından momentum çıktısı izleme içindir; emir açmaz.
- `v7_paper.py`: ayrı defter, 300 USDT başına atomik sermaye/risk rezervi,
  en fazla iki bot, aynı coin'e ikinci bot engeli, kısmi dolum, iptal,
  TP/SL/gap, tekrar kontrolü, kalıcı durdurma ve olaylardan mutabakat.
- Eski `v5-paper-500` dosyası ve varsayılan risk profili değiştirilmedi.
  Eski defteri yeniden kullanma, dönüştürme veya resetleme yoktur.

## 2. Test edilen ve geçilen kontroller

Yerel Python 3.12.14: başlangıçtaki 295 test ve yeni V7 regresyonları çalıştırılır.
Son test sayısı, tam commit SHA ve üç Python sürümünün CI sonucu PR teslimatında
raporlanır. `python tools/validate.py` derleme, güvenlik taraması, bütün testler
ve çevrimdışı demoyu çalıştırır. Release kontrolü yeni modülleri zorunlu tutar.

Yeni testler: SOL 98,47 / 3-of-6 sınıfı zorunlu filtre reddi; PEPE aynı snapshot
üzerinde radar/grid eşitliği; 5,01 USDT'nin 5 USDT'ye yuvarlanmaması; SUI komut
biçimleri; 400/400 ve 4H; sahte transport'un gerçek veri durumuna yükselememesi;
iki 300 USDT rezervi; eş zamanlı üçüncü girişin reddi; kısmi ve dolmayan emirler;
stop/TP/gap; minimum emir altı çıkışın beklemesi; aynı sıra ile tekrar likidite
kullanmama; kesinti/reconnect; restart; iptal; muhasebe/defter bozulması; işlem
geri alma; bildirim belirsizliği; yetkisiz komut ve LIVE_TRADING engeli.

Bu vakalar **sentetik regresyonlardır**. V5 ekran görüntülerinin asıl üretim
hesaplarını çalıştırmak, V5 kaynakları olmadan mümkün değildir; üretim hatasının
giderildiği iddia edilmez. Tarihsel kârlılık/backtest veya strateji getirisi
kanıtlanmış değildir.

## 3. Başarısız kontroller

Gerçek `tools/public_smoke.py` ve `tools/v7_smoke.py --allow-public-network
--symbol BTC-USDT` denemeleri: `PUBLIC_HTTP_UNAVAILABLE`, exit 2.
**REAL_API_VERIFIED=false; DATA_UNVERIFIED.** Global OKX, relay, proxy veya
sentetik fiyatla bu başarısızlık örtülmez. Resmî dokümana erişilebilmesi gerçek
API bağlantısının çalıştığı anlamına gelmez.

`/opt/okx-grid-demo/v5` mevcut değil; SSH yapılandırması, SSH agent veya yetkili
uzak terminal aracı yok. V5 kaynakları, servis/scheduler, üretim Python sürümü,
bağımlılıklar, aktif commit, yönlendirici ve durum defterleri **DOĞRULANMADI**.

## 4. Telegram'da gerçekten etkin özellikler

Bu çalışma kapsamında gerçek bot üzerinde doğrulanan komut sayısı **0**.
`TELEGRAM_VERIFIED=false`, `ACTIVATED=false`. Aşağıdaki kayıtlar yalnız test
edilen hook içindir; mevcut botun kayıt tablosunun değiştiği anlamına gelmez.

| Komut | Hook işlevi |
| --- | --- |
| `/durum`, `/diagnostics` | Veri durumu, profil, portföy, risk, pause/halt; V5 bağlantısı doğrulanmadı |
| `/analiz SOL`, `/grid SOL` | Aynı snapshot üzerinde ortak analiz ve plan; rezerv/emir yok |
| `/radar`, `/radargrid` | Radar A: keşfedilen paritelerde en fazla üçlü batch |
| `/paraakisi`, `/momentum` | Radar B: V6 CVD/RVOL/taker/multi-TF; ortak risk, izleme çıktısı |
| `/gridplan SOL 300` | Aynı ücret/risk modeliyle 300 USDT önerisi; otomatik giriş değildir |
| `/papergrid start` | Veri kapısı geçerse paper motorunu hazırlar; scheduler başlatmaz |
| `/papergrid status`, `/gridstatus` | Emirler, PnL, rezervler ve motor durumu |
| `/papergrid stop` | Yeni girişleri/buy emirlerini durdurur; koruyucu çıkışlar sürer |
| `/emergency_stop` | Kalıcı halt; sonraki geçerli book ile sonlu likidite altında çıkış |

Desteklenen yazımlar: `SOL`, `SOL-USDT`, `SOL/USDT`, `SOL_USDT`, `SOLUSDT`.
Keşfedilmeyen sembol `UNKNOWN_SYMBOL`; bilinmeyen komut `UNKNOWN_COMMAND`.
TRY normalize edilir fakat GRID yürütmesi USDT ile sınırlıdır; V6 TRY analizi
yalnız doğrulanmış taze USDT/TRY dönüşümüyle devam eder.

## 5. Entegrasyon ve dağıtım durumu

### İncelenen bağımlılıklar

| Dal / PR | İncelenen head | Sonuç |
| --- | --- | --- |
| main | `ba4f932f7820391dbd1cc75f7198a1979bc00f34` | İskelet; özgün V5 kaynakları yok |
| codex/paper-safety-ci / #1 | `8618eab5b836c7d840a3d86eaaef47e424842f23` | 500 USDT muhasebe, iç içe atomik işlemler, outbox ve public veri temeli |
| sol/paper-auto-decision-guard / #3 | `94a7ec4cb0972fff6ece747c10661ee47258de87` | Eski fork, #1 ile çakışıyor; toplu merge edilmedi |
| sol/public-tr-readonly-g1 / #4 | `3f9b172aa445c691b418e2af892c79b57f6700eb` | GET taşıma katmanı sonraki #1/#7'de geliştirilmiş; eski fork geri kopyalanmadı |
| codex/v6-money-flow / #7 | `c3a6fe312d81e79579674e8d52128fa5643079cd` | V7 için üst bağımlılık; V6 testleri korunur |

Issue #5/#6/#8/#9 ve PR metadata/kaynak farkları incelendi. Yeni draft PR #7
üstüne istiflenir. main merge, mevcut PR'ların kapatılması veya üretim aktivasyonu
yapılmaz. #3/#4'ü ayrıca merge etmek eski transaction/transport sürümlerini geri
getirebilir; inceleme sırasında güncel #1/#7 uygulaması korunmalıdır.

### V5 erişiminden sonraki gerçek entegrasyon

1. Dokuz kanonik kaynağı salt okunur incele; `tools/inspect_vps_source.py`
   yalnız adlandırılmış kaynaklara bakar. .env, token ve özel defter aktarılmaz.
2. Üretimin aktif commit, Python/bağımlılık, tek Telegram consumer, dispatcher,
   scheduler ve durum yollarını belirle. API yüzeylerini tahmin ederek doldurma.
3. Kaynak ve durum yedeğini VPS üzerinde yerel ve özel tut. Yeni 600 USDT
   defterine ayrı yol ver; eski deftere dokunma. Restore adımını staging'de dene.
4. Review edilmiş commit'i `tools/release.py` ile ayrı staging alanına çıkar.
   Mevcut servislerin dosyalarını veya çalışma dizinlerini değiştirme.
5. Gerçek salt okunur TR smoke: keşif, saat, ticker, 400/400, 15m/1H/4H/BTC,
   yaş, timeout/retry ve kopma/yeniden snapshot kanıtını kaydet. DATA_UNVERIFIED
   ise paper başlangıcı dahil otomatik pozisyon açma.
6. Aşağıdaki çağrılabilir sınırı gerçek V5 sözleşmelerine uyumlayan adaptörü
   ancak kaynakları gördükten sonra yaz. Shadow'da iki motoru **aynı** capture
   üzerinde karşılaştır. Fark varsa işlem açma ve farkı kaydet.
7. Var olan consumer üzerinde tüm komutların gerçek Telegram yanıtlarını,
   bot kayıtlarını, ACL, tekrarlar, sender ACK/UNKNOWN ve tek consumer'ı doğrula.
8. Açık dağıtım onayı gelmeden üretim servisini değiştirme veya yeniden başlatma.
   Onay sonrası rollback: önceki sürüm/yol ve eski özel defterin yedeğini kullan;
   iki poller çalıştırma. Yeni defteri eski V5 şemasına zorla dönüştürme.

Çağrılabilir sınır (V5'e bağlanmış kod değildir):

```python
from okx_grid.v7_market import V7MarketData
from okx_grid.v7_paper import V7PaperTrader
from okx_grid.v7_core import V7Core
from okx_grid.telegram_v7 import handle_v7_update

# Separate, private staging path supplied by the verified existing owner.
trader = V7PaperTrader(separate_v7_state_path)
market = V7MarketData(trader.policy)
core = V7Core(trader, market)
# Existing owner supplies a normalized private update, ACL and current time.
result = handle_v7_update(core, update, allowed_chat=chat_id,
                          allowed_user=user_id, now=current_time)
# result['delivery'] == 'NOT_SENT': existing V5 sender must deliver and record ACK.
# Existing scheduler may call core.run_cycle only after staging acceptance.
```

`notification_healthy` cycle için varsayılan false'tur. Sender'ın ACK/UNKNOWN
durumu mevcut outbox ile uzlaştırılmalıdır. Bağlantı hatası yeni alışları kalıcı
pause eder; başarılı yeni GET otomatik resume etmez. Başlangıç explicit ve veri
kontrollüdür. Özgün V5'in daha geniş Telegram update şeması önce normalize
edilmelidir; hook key whitelist'i sessizce gevşetilmemiştir.

## 6. Engeller ve gereken kullanıcı işlemleri

- Mevcut V5'in bulunduğu VPS'e yetkili salt okunur terminal/SSH erişimi gerekir.
  Gizli anahtarları veya Telegram erişim bilgilerini sohbete/GitHub'a göndermeyin.
  Alternatif kaynak incelemesi için sadece dokuz redakte edilmiş kanonik kaynak
  ve gizli bilgi içermeyen servis/dispatcher bilgileri sağlanabilir; bu yöntem
  gerçek Telegram/servis erişiminin yerine geçmez.
- Gerçek public smoke için çalıştırma ortamının `https://tr.okx.com` çıkışı
  çalışmalıdır. Bu ortamda gerçek bağlantı sağlanamadı.
- Kaynaklar ve staging testleri hazır olunca üretim dağıtımı ayrıca açık onay
  gerektirir. Bu görevde dağıtım veya gerçek para emri izni yoktur.

## 7. Bir sonraki öncelikli iş

Özgün V5 kaynak ve runtime incelemesi → API smoke → uyumluluk adaptörü →
aynı snapshot shadow karşılaştırması → gerçek Telegram e2e → staging geri dönüş
denemesi. Bu kapılar geçmeden başarı kriteri tamamlandı sayılamaz.

Plan varsayımları: gözlenen son 20 adet 15m mumun min/max bandı; 15m/1H/4H
nonnegative trend; BTC 1H -%2 / 4H -%4 risk-off; aritmetik grid; her yönde
%0,1 fee, %0,02 kayma; asgari net/grid %0,01; varsayımsal min quote 5 USDT ve
keşfedilen tick/lot/minSz. Stop bandın altında en az tick, %0,05 veya ATR'nin
%10'u kadar tampon kullanır. TP/SL **fiyat seviyeleridir**, toplam bot PnL
hedefleri değildir. Gerçek hesap ücret kademesi okunmaz; varsayımlar açıkça
raporlanır. Parametreler kâr/alpha garantisi değildir; sık NO TRADE beklenebilir.

Paper dolum modeli REST karşı defterinin %10'uyla sınırlıdır; sıra önceliği veya
gerçekleşen fill kanıtı değildir. Aynı book sequence ikinci kez tüketilmez.
Minimum emrin altındaki çıkış kapanmış sayılmaz; açık envanter ve rezerv kalır.
Stop gap, kayma ve likidite nedeniyle **3 USDT üzerinde zarar** üretebilir.
400/400'den az dönen kitap V7'de reddedilir; eski V5/V6 modellerinin veri
sözleşmeleri değiştirilmiş sayılmaz. Tarama üçlü batch'tir; tüm piyasanın aynı
anda sıralandığı veya sürekli WebSocket akışı bulunduğu iddia edilmez.

## 8. Gerçek para işlemlerinin kapalı olduğunun teyidi

`LIVE_TRADING=false`, `paper_only()` ve default kapalı veri/başlangıç kapıları
korunur. Private API, signing veya order gönderme uygulaması eklenmedi.
`synthetic=True` sadece ayrı sentetik test defterleri içindir; defterin moda
bağlı kimliği bu defterin gerçek public-paper olarak yeniden açılmasını reddeder.
Gerçek modda ham trader çağrıları bile doğrulanmış kaynak capture'ı olmadan açılmaz.

İleride private API hazırlığı: izinli bölgesel API yeteneğini doğrula; withdrawal
yetkisi olmayan en düşük izinli erişimi yalnız VPS secrets üzerinden kullan;
cash-only spot, bakiye ve exchange filtrelerini preflight'ta denetle; benzersiz
client order intent'i önce kalıcılaştır; gönderim sonucu belirsizse yeniden
göndermeden borsa emir/fill kayıtlarıyla uzlaştır; kısmi fill, cancel/replace,
rate limit, restart ve kill-switch'i test et. Private ağ istemcisi bu PR'da yoktur.
Gerçek emir aşaması ayrıca açık kullanıcı yetkilendirmesi ve tamamlanmış paper
kabul testleri gerektirir.

Resmî sözleşme referansı: https://tr.okx.com/docs-v5/en/ (22 Ekim 2026 incelendi).
Kitap `seqId` ve REST snapshot sözleşmesi kullanılır. WS eklendiğinde güncel
`prevSeqId/seqId` kuralları ayrıca doğrulanmalıdır; eski checksum varsayımıyla
REST veya WS doğrulandı denmez.
