# OKX TR Telegram otomatik SPOT emirleri — güvenlik kapıları

**Hedef:** Telegram üzerinden gözlenen ve kontrol edilen, OKX TR hesabında ileride bağımsız spot GRID alış/satış emri yönetebilecek bir sistem.
**Bugünkü yetki:** Yalnızca tasarım, salt okunur bağlantılar ve PAPER/dry-run testleri. **GERÇEK EMİR YASAK.**
Bu belge tek başına canlı emir yetkilendirmez; çalışan V5 servislerinin değiştirilmesine veya PR'ın merge edilmesine izin vermez.

## Güncel durum (20 Ekim 2026)
- GitHub PR #1: paper-only referans çekirdek; 167 sentetik/çevrimdışı test yeşil, V5 ile üretim entegrasyonu kanıtlanmadı.
- `/opt/okx-grid-demo/v5` dosyalarına, mevcut Telegram komut sözleşmelerine, gerçek OKX TR public endpoint'lerine ve hesap API bağlantısına bu görev ortamından erişilemedi.
- `v5-paper-500` profili **yalnız sanal para** içindir; hiçbir gerçek bakiye tahsisi veya gerçek işlem yetkisi sağlamaz.
- V5 tek Telegram long-polling tüketicisi ve operasyonel servis olarak korunacak; ikinci çalışan bot veya ikinci Telegram tüketicisi eklenmeyecek.

## Zorunlu kural: güvenli varsayılan

| Kontrol | Kural / fail-closed durum |
|---|---|
| Live emir anahtarı | Ayrı, kalıcı `live_enabled=false`; env var tek başına bunu açamaz. Yetkili kullanıcının ayrı ve sonradan alınacak açık go-live onayı şart. |
| Emir oluşturma | Bugün ağ seviyesinde ve uygulama seviyesinde reddedilir; `POST /api/v5/trade/order` dahil bütün write endpoint'leri kapalı. |
| Hesap/kredi | Spot `tdMode=cash` dışında işlem yok; kaldıraç, margin, futures/swap ve short yok. |
| İşlem izni | Üretime geçişte kullanıcı kendi hesabından asgari Read+Trade kapsamlı, **Withdraw izni olmayan**, mümkünse ayrı/limitli hesap ve sabit VPS çıkış IP'sine bağlı API anahtarı oluşturur. Anahtar chat/PR/git/log içine konmaz. Trade yetkisi başka yazma endpoint'lerine erişebileceğinden uygulama tarafında yol+HTTP metod allowlist'i şarttır. |
| Telegram izinleri | Yetkili chat/user allowlist, deterministik komut parser'ı, kayıtlı audit ID, replay/rate limit, yalnızca tek long-polling tüketicisi. Mesaj metninden serbest biçimli emir çalıştırma yok. |
| Duraklatma | `pause` yeni alım/GRID botu açmayı engeller; `halt` state'i kalıcıdır. Açık emir iptali ve envanter tasfiyesi farklı işlemlerdir, otomatik piyasa emriyle likidasyon yapılmaz. |
| Muhasebe | Net pozisyon, kullanılabilir USDT, alış maliyeti, komisyon (ücretin hangi para biriminde kesildiği), açık emir rezervi, kısmi dolum ve gerçekleşen/gerçekleşmeyen PnL her döngüde mutabık olmalı. |
| Kesinti | Stale data, API/auth/rate-limit, zaman farkı, çift worker, DB hatası, eksik mutabakat, pozisyon uyuşmazlığı veya disk dolmasında **yeni emir verilmez**. Alarm ve müdahale prosedürü var. |

## Aşamalı uygulama ve kabul kriterleri

### G0 — V5 kaynak/doğrulama erişimi (blokaj)
- Dokuz V5 kaynak dosyasının, import bağımlılıklarının, aktif systemd/timer ve Telegram komut şemalarının **gizli bilgi ve işlem kaydı dışlanarak** salt okunur incelemesi.
- Tek operational V5 sahibi, kaynak envanter SHA-256 ve sürümlü uyumluluk matrisi.
- Kabul: `/durum`, `/analiz SYMBOL`, `/radar`, `/grid`, `/paper` sözleşmeleri gerçek kaynak üzerinden belgeli. Erişilemiyorsa blokaj açık yazılır; hazır iddiası yok.

### G1 — OKX TR public market data (no credentials)
- TR spot enstrüman evreni, REST ticker/book/15m/1H/BTC, server time, kaynak doğrulama, exchange tick/lot/minimum lot ve fiyat bandı; emir defteri sequence/timestamp/staleness ve hacim birimleri test edilir.
- Ağ limitleri, timeout, yeniden deneme/jitter; global OKX verisine sessiz fallback yasak.
- Kabul: Sentetik fixture + resmi TR uç noktalarında salt okunur smoke test kanıtı; veri yoksa otomatik giriş yok.

### G2 — Telegram komut bütünlüğü ve shadow PAPER
- Mevcut Telegram tokenına ikinci polling bağlantısı açmadan, V5'in normalleştirilmiş input'ları ile paper motoru aynı fiyat/tick akışını izler.
- Botun **kendiliğinden PAPER plan açma**, sanal alış/satış, TP/SL, fee, hata ve muhasebe bildirimleri; `/durum` gerçekten sağlık/bakiye/uyarı durumu verir.
- Kabul: Restart/crash, duplicate Telegram update, çift bot açma, yetersiz bakiye, stale book, stop gap, kesilen bildirim senaryoları; sanal işlem kayıtları yalnız VPS özel storage.

### G3 — Kimlik doğrulamalı READ-ONLY hesap mutabakatı
- Kullanıcı tarafından kontrollü bir aşamada, VPS dışında paylaşılmayan **read-only API kimliği** ile hesap bakiyesi, açık emir, emir geçmişi ve fills sorgularının doğrulanması.
- Program önce gördüğü emrin kendisine ait olup olmadığını ayırt etmeli; kullanıcı/başka bot işlemlerini üstlenmemeli, değiştirmemeli.
- Kabul: Exchange balances, frozen amount, API response retcode ve rate limits; auth/logout/revoke/clock-skew/missing fills testleri. Yalnız GET.

### G4 — Sıfır gönderimli emir istemcisi ve simüle borsa
- Trade API adapter **allowlist'li ancak ağ gönderimi kesinlikle devre dışı** olacak. Aynı emir için kalıcı client ID, transactional intent/outbox, unknown-outcome state.
- REST `clOrdId` yalnız açık emirler arasında benzersiz garanti sunduğundan **kendi tarihsel idempotency/ledger** kaydı korunmalı. Timeout sonrası kör yeniden emir gönderme yerine önce exchange order/fill sorgusu.
- Limit buy/sell, cancel/amend, `partial_fill`, filled/canceled/rejected, ücretler, kullanılabilir bakiye; `expTime`, zaman uyumu ve beklenmeyen network return senaryoları.
- Kabul: Entegre sentetik borsa ile soak, concurrency, crash/restart replay ve çift emir=0 testi; hiçbir gerçek POST gitmez.

### G5 — Operasyonel güvenlik ve dış denetim
- Minimum yetki + IP allowlist + 2FA + secret store ve anahtar rotasyon prosedürü; API anahtarları yönetimi yalnız kullanıcı tarafından.
- Günlük/haftalık işlem adet/adet başına notional, maksimum açık alım emri, max allocated USDT, realized loss, fee/spread ve stale-data eşikleri konfigürasyonla sınırlandırılır. Varsayımlar *paper* bütçesinden otomatik kopyalanmaz.
- Yalnız Telegram'dan gelen serbest metin/sinyal hiçbir zaman authorization bypass oluşturamaz. Trade account order sahipliği ve ayrı hesap/alt hesap olanağı değerlendirilir.
- Kabul: Penetrasyon kötü-girdi testleri, Telegram ACL, IP/timeout/retry ve kill-switch dry-run, audit log redaksiyonu, backup/restore, geri dönüş; fail-closed yeşil.

### G6 — Kullanıcıdan AYRI GO-LIVE kararı (şimdi YASAK)
- Kullanıcı canlı hesap için **gerçek sermaye tavanı, bot başına risk, günlük gerçekleşen zarar, parite allowlist, işlem adedi, emrin açılma koşulu, kill switch davranışı ve hesap/API yetkilerini** ayrıca onaylar.
- Önce kontrollü ve çok düşük limitli pilot (ürün ve API uygunluğu doğrulandıktan sonra), önceden belirlenmiş izleme ve rollback, sonra bağımsızlık kademeli artırılır.
- Bu belge veya bugünkü mesaj kullanıcı adına canlı emir başlatma talimatı değildir. Explicit ikinci onay olmadan `live_enabled` açılmaz.

## Teknik güvenlik testleri / Go-Live öncesi veto kriterleri
- Bir adet bile düplikat/kimliği belirsiz emir, çift hesap rezervi, yeniden oynatmada bilanço sapması, sınırsız retry, yetkisiz Telegram komutu veya maskelenmemiş sır bulunursa GO-LIVE YASAK.
- Stop-loss **garanti edilen azami zarar değildir**; gap, likidite ve ücret riski pilot ve kullanım belgelerinde açıklanmalı.
- Telegram botun yönetim/kontrol yüzeyidir; finansal karar/uygulama motoru yalnız deterministik, sürümlü risk kontrolleriyle çalışır. Telegram kaybında yeni işlemler fail-closed olmalıdır.
- Açık emirleri iptal etmek ile önceden alınmış envanteri kapatmak ayrı eylemlerdir; acil durdurma bunları sessizce eşitlememelidir.

## Teslim formatı
Her kapı için: `IMPLEMENTED` / `TESTED` / `DESIGNED_ONLY` / `BLOCKED`, SHA commit, CI run, test kanıtı, açık riskler. Yeni geliştirme yalnız `codex/paper-safety-ci` veya ayrı test dalında; `main` merge/servis restart gerçek emir olmadan.
