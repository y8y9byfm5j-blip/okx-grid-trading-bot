# İnceleme ve teslim durumu — 22 Ekim 2026

## V7 görev emri — güncel durum

- Son kaynak kanıtı: kullanıcı görüntülerinde mevcut Telegram systemd servisi
  active, `/opt/okx-grid-demo/v5` çalışma dizini ve `/usr/bin/python3.12`
  executable'ı görüldü. `docker` komutu yok; container/venv yokluğu bundan
  çıkarılamaz. İmzalar ve `find_plan`/`handle`/`main` maskeli AST'si görüldü;
  tam kaynak, API/sender/offset sözleşmeleri ve doğrudan VPS erişimi hâlâ yok.
- Yeni `V5OwnerBridge` gözlenen mevcut owner akışı için staging'de hazır:
  tam update ACL, sonuç/outbox sonrası atomik checkpoint, restart/PENDING
  kurtarma, gönderim öncesi UNKNOWN, eşleşen JSON ACK ve sanitized hata.
  Poller/sender/servis yok. Eski `/paper` callback'i bağlanmış sayılmaz.
  Ayrıntılar: [V5_OWNER_BRIDGE.md](V5_OWNER_BRIDGE.md).
- Son düzeltme: bir parite hatasından sonra başka paritenin başarılı GET'inin
  durum ekranında arızayı örtmesi regresyonla yeniden üretildi. `/durum` artık
  son kalıcı çevrim receipt'inden hatayı/pariteyi gösterir; restart ve tek
  başarılı GET bunu temizlemez. Hatasız yeni çevrim pause'u otomatik kaldırmaz.
- Birleşik sentetik owner kabulü: komutlar → iki ayrı 300 USDT rezervi → partial
  fill → replay/restart → emergency stop → gap çıkışı; ayrıca min-order dust
  rezervleri, UNKNOWN teslim ve kesinti/resume doğrulandı. Bu gerçek Telegram
  veya canlı veride iki aktif pozisyon kabulü değildir.
- IMPLEMENTED / OFFLINE TESTED: #7 üstünde ayrı V7 geliştirmesi; ortak command
  registry, snapshot/karar kaynağı, GRID + V6 momentum hook'u, 4H/BTC/ATR,
  ayrı sürümlü 600 USDT paper profili, 2 x 300 USDT atomik rezervasyon,
  kısmi dolum/iptal/mutabakat, min order, replay ve kalıcı halt.
- Eski v5-paper-500 konfigürasyonu/defteri korunur. Yeni 600 profilinin
  hedef planlanan risk sınırları bot başına 3 / toplam 6 USDT; gerçekleşen
  stop zararı garanti değildir. Momentum yönlü emir planı olmadan izleme üretir.
- Yeni kod ve eski 295 test birlikte test edilir. Tam sonuçlar, commit SHA ve
  üç sürümlü CI/staging kanıtı draft PR teslimatında raporlanır.
- ISOLATED_TR_SNAPSHOT_VERIFIED=true: `c8753329b07d44b282853ea499753a0dfbdcc7ae`
  Actions `39740099724` üzerinde BTC-USDT ve SOL-USDT V7 GET-only smoke geçti;
  400/400, 15m/1H/4H/BTC, canlı TR origin ve timestamp/sequence doğrulandı.
  Bu yalnız runner snapshot'larıdır; VPS_REAL_API_VERIFIED=false. Yerelde
  PUBLIC_HTTP_UNAVAILABLE / DATA_UNVERIFIED; global fallback yok.
- V6 gerçek runner kabulünde BTC-USDT/SOL-USDT tam beş dakikalık flow geçti;
  BTC-TRY stale/ticker-book kontrollerinde reddedilmeye devam ediyor. İlk
  eksik pencere sonuçları aşağıda tarihçe olarak tutulur; eksik CVD kabul edilmez.
- 392 offline regresyon (15 bridge ve 12 yeni çevrim/teşhis/birleşik kabul testi dahil). Son exact-head CI
  sonucu PR'da ayrıca raporlanır. Enstrüman kataloğunun 1 MB aşması için yalnız
  katalog bütçesi sınırlandırılarak ayrıldı; açık parite hedefi korunur;
  tüm pair'leri reddedilen V6 smoke JSON'da da `ok=false` döndürür.
- `c1b42511514dcee0d706ddb69706ae4ccad6cc12` public koşusunda V6 SOL-USDT
  gerçek beş dakikalık flow/multi-TF yolu geçti; düşük RVOL/düşüş nedeniyle
  aday yoktu. BTC pair'leri yine ticker/book uyumsuzluğuyla reddedildi.
  V6 bu uyumsuzlukta her iki endpoint'i en fazla üç kez taze alır; eşleşme,
  süre, sıra ve veri yaşı kontrolleri korunur. Kalıcı uyumsuzluk işlem açamaz.
- Pagination'ın son sayfası limit hatasından önce incelenir. En fazla 40 x 100
  history + 500 recent işlem ve 15 saniyelik trade bütçesi uygulanır; toplam
  30 saniye korunur. Mumlar pagination'dan sonra alınır; teyit bekleyen pencere
  bir kez yenilenir. Teyitsiz mum kabul edilmez; dakika sınırı tekrar denetlenir.
- Veri alınmayan/başarısız gerçek-mod çevrimi ve tüm pair'leri reddedilen
  momentum çıktısı artık VERIFIED_TR_PUBLIC etiketi üretmez.
- `tools/v7_paper_smoke.py` gerçek TR capture + ayrı geçici 600 USDT defterinde
  karar/çevrim replay, bildirim sağlığı yokken giriş engeli, muhasebe, restart
  ve kalıcı halt kabulünü çalıştırır. Telegram göndermez; pozisyon açmayı bilerek
  engeller. Canlı veride iki pozisyon açılması veya V5 aktivasyonu kanıtı değildir.
- Public paper probe reddi artık `phase=PAPER_CYCLE` ve en fazla iki paritenin
  allowlist edilmiş hata kodlarını verir. Payload/URL/özel state veya rastgele
  exception metni yazılmaz. Eksik neden uydurulmaz; ret başarılı sayılmaz.
- Önceki head `68a6fc9` gerçek public koşusu `39830584486`: attempt1 3/6,
  attempt2 4/6; toplam workflow başarısız. Paper safety ilk denemede
  top-of-book uyuşmazlığıyla reddedildi, bir kez tekrarında geçti. İki karar da
  risk >3 ve downtrend nedeniyle NO TRADE, 600 nakit/0 rezerv/0 pozisyon;
  restart/replay/halt geçti. Bağımsız SOL top-of-book ve BTC-TRY stale reddi
  sürdü. Sürekli veri akışı veya iki açık paper pozisyon kabulü değildir.
- TELEGRAM_VERIFIED=false; ACTIVATED=false. Bu çalışmada gerçek bot üzerinde
  uçtan uca doğrulanan komut **0**. Kısmi kaynak/runtime ekran kanıtı üretim
  aktivasyonu değildir; scheduler/bağımlılıklar/aktif commit/state doğrulanmadı.
- Yeni poller/sender veya üretim servisi değişikliği yok. main merge yok.
  Private API ve gerçek emirler yok; LIVE_TRADING=false korunur.
- Ayrıntılı sekiz bölümlü rapor, bağımlılık denetimi, çağrılabilir sınır,
  staging/rollback ve erişim ihtiyaçları: [V7_INTEGRATION.md](V7_INTEGRATION.md).

Aşağıdaki 20 Ekim kayıtları önceki artımlı teslimlerin tarihçesidir; eski
`/radar` yönlendirmesi yeni V7 hook'unun GRID radar tanımı yerine kullanılmaz.

---

## Issue #6 — V6 Smart Money Flow, artımlı teslim 1

- IMPLEMENTED / OFFLINE TESTED: PR #1'in `8618eab` tabanından ayrı
  `codex/v6-money-flow` dalı; PR hedefi `codex/paper-safety-ci`, main değil.
  PR #4 transport'u tabanda zaten entegre; eski sürümle üzerine yazılmadı.
- TR live SPOT discovery, USDT/TRY quote ve kaynaklı/fresh USDT-TRY dönüşümü;
  diğer quote'lar açıkça kapsam dışı. Aynı coin tek satırda; hacim ve CVD
  müşteri bakiyesi/net sermaye girişi olarak sunulmaz.
- 1m/5m/15m/1H tamamlanmış mum, 20 geçmiş complete 5m RVOL, paginated executed
  taker flow/duplicate/conflict/window, book sıra/reconnect/depth/spread/slippage,
  BTC şoku ve explainable heuristik filtreleri eklendi.
- `/paraakisi`, `/radar`, `/analiz COIN[-USDT|-TRY]` tek-owner hook'u; mevcut
  `/grid` delege edilir. Kalıcı receipt/cooldown/private notice outbox; sender yok.
- 295 offline test başarılı: önceki 242 korundu, 53 flow regresyonu eklendi.
  CI/SHA/staging kanıtı yeni V6 PR raporunda. Fee/depth net execution replay'i
  stratejinin tarihsel kârlılığını kanıtlamaz.
- REAL API VERIFIED / TELEGRAM VERIFIED: HAYIR. V6 explicit smoke exit 2,
  PUBLIC_HTTP_UNAVAILABLE. Gerçek V5/SSH erişimi yok; botta doğrulanan komut 0.
- BLOCKED / FOLLOW-UP: Issue #5 kapıları, gerçek V5 tek owner ve timeout/rollback,
  actual Telegram ACK/UNKNOWN, WS availability/stream continuity, tüm evrende
  eşzamanlı fresh top5 ve tarihsel backtest. İlk teslim top5 yalnız güncel batch.
- Gerçek order/private API, yeni poller, servis aktivasyonu/restart ve main merge
  yok. Ayrıntılar: `docs/V6_MONEY_FLOW.md`. Issue #6 bütünü kapanmış sayılmaz.

## Issue #5 P0 — salt okunur canlı veri katmanı

- IMPLEMENTED: SOL PR #4 GET katmanı incelenip aynı geliştirme dalına alındı;
  server time/tickers/catalogue, TLS/final-origin, shared pacing, bounded retry,
  capture normalizasyonu, 400/side REST book ölçümleri ve command hook eklendi.
- OFFLINE TESTED: 242 test; 400+400 sentetik defter, timestamp/seq/volume/15m/1H,
  BTC risk-off, HTTP→normalizasyon→ACL komut hattı, retry/TLS/proxy/redirect,
  restart/replay/UNKNOWN ve hata çıktıları. Son CI/staging SHA/bağlantısı PR'da.
- `/analiz SYMBOL-USDT`, `/radar`, `/grid`, `/durum` referans hook'u hazırdır.
  Tek mevcut V5 consumer'a bağlanacak adaydır; ikinci poller/sender yoktur.
  `/grid` onaylı template ister, sadece sanal öneri verir; rezerv/emir vermez.
- REAL API VERIFIED: HAYIR. Bu ortamdan explicit BTC smoke exit 2 ve
  PUBLIC_HTTP_UNAVAILABLE verdi. Gerçek fiyat/hacim/derinlik/trend ölçümü yok.
- TELEGRAM VERIFIED: HAYIR. Gerçek V5 kaynakları, VPS/SSH ve Telegram oturumu
  erişilebilir değil. Gerçek botta doğrulanan komut sayısı 0; aktivasyon yok.
- BLOCKED: izinli ortamda başarılı TR smoke, özgün V5 sözleşmesi, tek consumer,
  gerçek Telegram e2e ve V5 rollout/rollback. `live_enabled=false` korunur.
- WebSocket streaming, private/account/order API, yeni servis ve main merge yok.
  Ayrıntılı sınırlar ve smoke kullanımı: `docs/LIVE_MARKET_DATA.md`.

## Son ek kontrol — otomatik paper ve Telegram referans sınırı

- IMPLEMENTED / TESTED: SOL PR #3 girişi incelenip aynı dala alındı. Aynı parite
  eşzamanlı giriş, bütün kararların kalıcı fingerprint'i, nested transaction,
  rollback ve restart korumaları eklendi. 500 USDT profil limiti gevşetilemez.
- IMPLEMENTED / TESTED: çağrılabilir tek çevrim, sağlanan fiyatlarla fill/TP/SL,
  sembol allowlist'i, kalıcı pause, private bildirim outbox'ı ve UNKNOWN teslim
  durumunda yeni alımları durdurma. Poller veya gerçek gönderici yoktur.
- IMPLEMENTED / TESTED: offline Telegram referansında kullanıcı+özel chat ACL,
  stale/forward/edited/bot reddi, komut allowlist'i, kalıcı update replay/conflict,
  cursor ve rate-limit. V5_NOT_CONNECTED / NOT_SENT açıkça döndürülür.
- Yerel doğrulama: 199 test, kaynak güvenlik taraması ve offline demo başarılı.
  Son commit için CI/staging sonucu PR #1 raporuna eklenir.
- DESIGNED_ONLY: gerçek V5 adapter/poller/sender, günlük UTC zarar limiti,
  gelecekteki gerçek exchange intent/outbox/UNKNOWN/partial-fill/cancel
  reconciliation yaşam döngüsü. Mevcut 10 USDT fuse yalnız ömür boyu net
  gerçekleşen zarardır; günlük limit diye sunulmaz.
- BLOCKED: V5 kaynakları ve canlı servis incelemesi için VPS/SSH erişimi yok;
  TR API domain'i ağ izin listesinde yok. Gerçek V5 komut sözleşmeleri ve
  canlı public smoke doğrulanmadı. Proje operasyonel tamamlandı sayılmaz.
- MAIN/PR merge, VPS aktivasyonu, gerçek emir ve gerçek Telegram gönderimi yok.
  `docs/LIVE_TRADING_READINESS.md` SOL sürümü korundu; PR #3 main'e birleştirilmedi.

## SOL P0/P1 düzeltmesi — önceki kabul durumu

Talimat: [SOL denetim yorumu](https://github.com/y8y9byfm5j-blip/okx-grid-trading-bot/pull/1#issuecomment-6283833803).
Aynı `codex/paper-safety-ci` dalında devam edildi. PR DRAFT kalır; merge,
VPS dağıtımı/aktivasyonu, systemd müdahalesi ve gerçek emir yoktur.

### Tamamlandı

- Kökte `V5_COMPATIBILITY.md`: dokuz bileşenin bilinen dosya adları, doğrulanmamış
  arayüz/çıktı/state/zamanlayıcı/Telegram sözleşmeleri, adaptör aday eşleştirmeleri
  ve bloke eden kaynak erişimi. V5 içeriği tahmin edilmedi.
- `v5-paper-500` tam TOML profili ve görünür CLI seçimi. Tek 500 USDT başlangıç
  özkaynağı; bot başına %1/5 USDT, üç bot toplam %3/15 USDT planlanan risk.
  Global 2.000 USDT varsayılan korunur. Config eksikliği ve kimlik/revizyon/policy
  drift'i defter resetlenmeden reddedilir.
- Portföy nakit/envanter/rezerv/fee/PnL özeti ve küresel korunum denetimi. Bot
  filtresi bütün portföyü değiştirmez; kapanan bot sermayesi çift sayılmaz.
- Sadece offline public sözleşme modülü: allowlist GET tanımı, TR-origin
  metadata'sı, spot uygunluk, quote hacmi, ms→s, REST book cursor, kline 15m/1H,
  BTC rejimi ve sınırlı backoff modeli. HTTP/WS transport'u eklenmedi.

### Test edildi

- Önceki 121 test korundu; yeni profile, CLI ve public sözleşme regresyonları
  eklendi. Son toplam sayı, tam commit ve üç sürümlü CI bağlantıları PR sonuç
  yorumunda verilir; eski 121 testlik CI bu düzeltmenin kanıtı yerine gösterilmez.
- 5 USDT tam sınır/üstü, üç bot 499,98 USDT tahsis/0,02 serbest bakiye, 15 USDT
  toplam risk, eşzamanlı çift harcamayı engelleme, giriş/çıkış ücretleri,
  duplicate/conflict, restart, rollback, bozuk state, gap ve TP/stop senaryoları.
- Public sentetik sözleşmelerde kayıp/boş/yanlış parite, birim/zaman/sıra sorunu,
  eski/uyumsuz defter, tamamlanmamış bar ve BTC risk-off reddi. Bunlar canlı
  API/transport/rate-limit davranışının ölçümü değildir.
- Paketlemede yeni profil, sözleşme modülü ve uyumluluk dosyası zorunludur.
  İzole staging testi `activated:false` ve başarısız provada geri alma sınırını
  doğrular; son commit'e ait CI kanıtı PR'a eklenir.

### Yalnızca tasarlandı

- V5 tek operasyonel bot olarak kalacak adaptör/shadow karşılaştırması; aynı
  public girdinin ayrı özel deftere verilmesi, tek Telegram tüketicisi ve
  bildirimi, sürümlü geçiş kapıları. Gerçek V5 adaptörü veya ikinci servis yoktur.
- V5 harness'inde shadow çağrısını kapatıp eski sisteme dokunmayan geri dönüş.
  Kaynak olmadan çalıştırılabilir V5 rollback testi uydurulmadı. Mevcut otomatik
  rollback testi yalnız izole release staging hatasını kapsar.
- Gelecekteki public transport'ta TLS/origin, redirect reddi, endpoint pacing,
  jitter ve timeout. Bugünkü backoff testleri scheduling modeline aittir.

### VPS erişimi / ağ nedeniyle yapılamadı

- Yeniden kontrol edilen ortamda V5 dizini, SSH yapılandırması/agent/standart
  anahtar ve açık tarayıcı konsolu yoktu. Dokuz kaynağın gerçek imzaları,
  servisleri, durum şemaları ve `/durum`, `/analiz SYMBOL`, `/radar`, `/grid`,
  `/paper` sözleşmeleri okunamadı; entegrasyon ve Telegram uyumluluğu kanıtlanmadı.
- Ağ izin listesinde TR API alan adı yok. Resmî doküman okunabildi; canlı TR
  ticker/books/instrument/candle smoke testi ve güncel parite mevcudiyeti
  doğrulanamadı. `radar.py` hâlâ JSON filtresidir. Global veriye sessiz geçiş yok.
- VPS üzerinde kaynak aktarımı, shadow koşusu, servis restart'ı veya aktivasyon
  yapılmadı. Secret/.env/anahtar/DB/runtime/trade geçmişi GitHub'a gönderilmedi.

## İlk PR sürümüne ait tarihsel inceleme

## GitHub Actions kök nedenleri

| Çalıştırma | Commit | Sonuç ve neden |
| --- | --- | --- |
| [#1](https://github.com/y8y9byfm5j-blip/okx-grid-trading-bot/actions/runs/39328924439) | `4d49ba31` | Workflow `tools/check_repo_safety.py` çağırıyordu; dosya henüz commit'te yoktu. `No such file or directory`. |
| [#2](https://github.com/y8y9byfm5j-blip/okx-grid-trading-bot/actions/runs/39328924960) | `378fe383` | Güvenlik aracı eklenmişti, test klasörü henüz yoktu. `Start directory is not importable: 'tests'`. |
| [#3](https://github.com/y8y9byfm5j-blip/okx-grid-trading-bot/actions/runs/39328924998) | `ba4f932f` | Test dosyası da eklenince yeşil. Yalnızca bir başlangıç testi çalışıyordu. |

İki kırmızı kayıt eski eksik commit'lere aittir; aynı eski SHA'ları tekrar
çalıştırmak eksik dosyaları düzeltmez. İncelenen son `main` commit'i yukarıdaki
üçüncü commit'ti ve beş dosyadan oluşuyordu. v5 bot kaynakları bu commit'te yoktu.
Her iki başarısız işin adımları ve tam logları okunarak neden doğrulandı.

## Bu dalda tamamlananlar

- İzole `okx_grid` paper paketi, normalize snapshot radar filtresi, grid planlama,
  tek/çoklu bot risk ve sermaye rezervasyonu, Decimal muhasebe ve SQLite defteri.
- Ücret/lot/tick/minimum tutar, spot sembolü, spread/hacim/iki taraflı yakın
  derinlik, veri yaşı ve trend doğrulamaları. Snapshot kaynağı uydurulmaz.
- Stop/TP, fiyat boşluğunda kayma, kalıcı halt, gerçekleşen zarar kesicisi,
  duplicate/conflict/out-of-order tick, çökme sırasında atomik geri alma,
  yeniden başlatma ve muhasebe sapmasının tespiti.
- Staged blob içeriği ve çalışma kopyası için kaynak güvenlik taraması; özel
  dosya adlarının, credential kalıplarının ve gömülü değerlerin reddi.
- Dokuz bilinen v5 kaynak dosyası için çalıştırmadan inceleme ve isteğe bağlı
  özel yerel export. Bu araç henüz VPS üzerinde çalıştırılmadı.
- Tam commit ve hash'lerle paketleme, güvenli arşiv açma, ayrı dizinde test edilen
  staging. Servis aktivasyonu yapacak kod eklenmedi.
- Eksik dosya/suite veya sıfır testte başarısız olan tek doğrulama komutu; Python
  3.11, 3.12 ve 3.13 CI matrisi, sabitlenmiş Actions commit'leri ve salt okunur CI
  yetkileri. Python 3.12 işinde gerçek paketleme/staging provası da çalışır.

## Erişim ve kapsam sınırları

GitHub deposu okunabiliyor ve geliştirme dalına yazma erişimi var. Bu çalışma
ortamında `/opt/okx-grid-demo/v5`, VPS SSH yapılandırması veya SSH anahtarı
bulunmadı. Kullanılabilir araçlarda uzaktan shell/DigitalOcean bağlantısı yoktu;
bağlantı kataloğunda DigitalOcean/SSH araması da sonuç vermedi.

Dolayısıyla mevcut v5'in kodları, muhasebe kayıtları, prosesleri, servis tanımı,
runtime sağlığı, OKX veri doğruluğu ve Telegram komutları incelenemedi. Sunucuya
kurulum, geçiş ve mevcut botla uçtan uca test yapılamadı. Bu dal yeni bir paper
çekirdeğidir; özgün projenin tamamlanmış entegrasyonu olarak sunulmaz.

Gerçek para emirleri, OKX özel API bağlantısı ve Telegram gönderimi eklenmedi veya
çalıştırılmadı. VPS'e bağlanılmadı; çalışan servise, ana dala veya gerçek işlem
kayıtlarına müdahale edilmedi. GitHub'a yalnızca bu yeni kaynak kodu, sentetik
testler, CI ve dokümantasyon taşınır. İnceleme sırasında VPS credential veya
işlem kaydı elde edilmedi.
