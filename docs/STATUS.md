# İnceleme ve teslim durumu — 20 Ekim 2026

## SOL P0/P1 düzeltmesi — güncel kabul durumu

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
