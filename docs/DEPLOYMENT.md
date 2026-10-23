# Servisi kesmeden kaynak incelemesi ve staging

SOL P0/P1 kapısı: [V5_COMPATIBILITY.md](../V5_COMPATIBILITY.md) kanıtları
tamamlanana kadar VPS'e dağıtım ve aktivasyon yasaktır. Aşağıdaki staging,
yerel/CI geçici dizin provasıdır. Shadow tasarımı ikinci zamanlayıcı veya ikinci
Telegram tüketicisi değildir; V5'in aynı girdisini ayrı özel referans defterine
veren adaptör tasarımıdır. Bu adaptör henüz kaynak üzerinden doğrulanmadı.
[500 USDT profili](RISK_PROFILE.md) mevcut V5 state'ine uygulanmaz/migre edilmez.

Bu sürümde deploy veya aktivasyon workflow'u yoktur. GitHub Actions yalnızca
GitHub runner'ında doğrulama ve geçici staging provası yapar. SSH, VPS adresi,
OKX/Telegram anahtarı veya bulut kimliği kullanmaz.

## Devralma sürümünün kapıları

23 Ekim düzeltmeleri yalnız incelenen V7 staging çekirdeğine uygulanmıştır;
üretimdeki özgün V5 dosyalarının değiştiği anlamına gelmez. Dağıtım paketi
tam commit, dosya SHA-256 manifest'i, `mode=paper` ve `activation=disabled`
taşır. `tools/release.py` mevcut dosyanın üstüne yazmaz, üretim yoluna stage
etmez ve yeni regresyon paketten çıkarılmışsa işlemi reddeder.

Paketin hazır sayılması: 478 offline test, güvenlik taraması, derleme,
demo ve ayrı geçici staging'in test/health/demo kontrolleri başarılı olmalı.
GitHub'daki gerçek TR public koşusu ayrıca değerlendirilir. Eşleşmeyen
ticker/book veya eksik pencere `NO TRADE / DATA_UNVERIFIED` olarak kalır;
offline başarı bu kabulün yerine geçmez.

Yetkili VPS erişimi geldiğinde şu sıra uygulanır:

1. Salt okunur source/hash, tam unit/scheduler, tek Telegram owner ve
   üretimde gerçek emirlerin kapalı olduğunu yerinde doğrula.
2. Kaynak ve durumun özel, tutarlı yedeğini yalnız VPS'te al; özgün unit,
   çalışma yolu ve geri dönüş sürümünü kaydet. Özel state'i dışarı aktarma.
3. Paketi ayrı staging köküne hash ile kur; `create_observer` için yeni,
   ayrı ledger kullan. Mevcut 500/600 ledger'ı değiştirme veya migrate etme.
4. Aynı capture ile shadow, gerçek Telegram komut/ACK/timeout/restart ve
   sürekli veri kabulünü mevcut owner üzerinde doğrula. İkinci poller açma.
5. Sağlık ve geri dönüş kanıtı tamamlanınca yalnız salt okunur observer
   bağlantısını geçir. Hata varsa eski unit/sürüm/yol ve kendi ledger'ına
   geri dön; yeni ledger'ı eski üretim state'i yerine koyma.

Bu oturumun teknik erişimi 1. adımı yapamadığından aktivasyon uygulanmaz.
Gerçek emir yolu pakette yoktur; `LIVE_TRADING=true` veya canlı trading mode
çalıştırılması hata ile reddedilir. VPS'in gerçek runtime ayarı erişim
olmadan doğrulandı diye raporlanmaz.

## VPS kaynaklarını güvenli inceleme

23 Ekim için tek salt okunur hazırlık raporu, yalnız incelenmiş ayrı staging
checkout'undan ve yetkili VPS terminalinde:

```bash
python3 tools/vps_readiness.py --source /opt/okx-grid-demo/v5 --allow-public-network
```

Araç dokuz allowlisted kaynağı çalıştırmadan iki kez inceler ve disk hash'lerini
karşılaştırır. Yalnız üç bilinen systemd biriminin `show` özelliklerini okur;
komut satırı/Environment/log çıktısı yoktur. Çalışan Python executable adı ile
aracı çalıştıran Python sürümünü ayrı gösterir. Başarıyla bitmiş inactive
oneshot görev arıza sayılmaz. Script, `.env`, özel key veya defter okumaz,
export/backup/kurulum/restart yapmaz; Telegram'a bağlanmaz.

`--allow-public-network` verilirse yalnız sabit `https://tr.okx.com` GET
istemcisiyle BTC-USDT keşif/saat/ticker/400+400/mum/BTC capture'ı denenir.
Başarısızlık `DATA_UNVERIFIED` kalır; HTTP durumu gibi içeriksiz teşhisler
görülebilir. `www.okx.com` fallback veya kullanıcı tarafından seçilen origin
yoktur. Flag yokken ağ çağrısı yapılmaz. Exit 0 kaynak incelemesi ve istenmişse
o tek public capture'ın geçtiğini belirtir; üretim aktivasyonu değildir.

Her rapor `host_scope=EXECUTION_HOST_ONLY`, `production_ready=false`,
`telegram_e2e_verified=false`, `activated=false` taşır. Yerel/CI çıktısı VPS
kanıtı sayılamaz. `LIVE_TRADING=false` yalnız inspector prosesinin guard'ıdır;
üretim prosesinin özel ayarı okunmaz (`production_live_trading=NOT_INSPECTED`).
Kaynak hash'leri çalışan prosesin bellekteki kodunu veya
aktif Git commit'ini kanıtlamaz. Unit gövdeleri, bağımlılıklar, tek consumer,
V5 plan/muhasebe adaptörü, shadow ve gerçek Telegram e2e ayrıca incelenir.

Resmî TR REST kaynağı: [OKX TR API production services](https://tr.okx.com/docs-v5/en/#overview-production-trading-services).
Telegram cursor davranışı: [getUpdates](https://core.telegram.org/bots/api#getupdates).

Bu oturumdan DigitalOcean VPS'e erişilemedi. Aşağıdaki araç yalnızca sunucuda
yetkili bir çalışma ortamından çalıştırılabilecek şekilde hazırlanmıştır:

```bash
python3 tools/inspect_vps_source.py /opt/okx-grid-demo/v5
```

Araç dokuz açık dosya adını okur; `.env`, işlem günlükleri, veritabanları veya
backup dizinlerini dolaşmaz. Kaynakları import etmez/çalıştırmaz. Boyut, normal
dosya, hardlink/symlink, Python/shell sözdizimi ve gömülü credential kontrolleri
yapar. Çıktıda dosya adı, hash, boyut ve değer içermeyen bulgular bulunur.
`--export /private/new-source-review` ancak bütün dosyalar bulunur ve kontrol
geçerse yeni bir yerel dizine kaynak kopyası çıkarır; kendisi Git veya ağ çağırmaz.
Tarayıcı her gizli bilgi türünü saptayamaz; export manuel kod incelemesinden
geçmeden Git'e eklenmez. Eksik import bağımlılıkları ayrıca, isim bazında incelenir.

Çalışan dizinde `git init`, `git add .`, toplu `scp`, `.env` okuma, çalışan
prosese sinyal gönderme veya dosyaları yerinde değiştirme işlemi yapılmaz.
Kaynak envanteri iki ayrı okumada karşılaştırılarak aktif geliştirme sırasında
dosyaların değişmediği doğrulanmalıdır.

## İncelenmiş commit'i paketleme

Temiz ve test edilmiş bir geliştirme checkout'undan, tam 40 karakterlik commit
SHA'sı kullanılır. Tag veya dal adı kabul edilmez. Çıktı dizini önceden var olmalıdır.

```bash
python3 tools/release.py build --commit COMMIT_SHA_40_HEX --output /private/paper-release.tar.gz
```

Paketleme önce bütün doğrulamaları çalıştırır. Yalnızca commit'teki izin verilen
kaynak/test/dokümantasyon dosyaları alınır. Çalışma dizininin izlenmeyen dosyaları,
ignored durum kayıtları ve `.github` release'e dahil edilmez. Her içerik yeniden
taranır; hash manifesti yazılır ve arşivin SHA-256 değeri döndürülür. Yeni çıktı
zorunludur; mevcut arşiv üzerine yazılmaz.

## İzole staging

Arşiv SHA-256 değeri CI veya güvenilir build çıktısından ayrı doğrulanır.
Rastgele bir arşiv ve onunla birlikte gelen hash, güvenilir kaynak kanıtı sayılmaz.

```bash
python3 tools/release.py stage --archive /private/paper-release.tar.gz --sha256 TRUSTED_SHA256 --root /opt/okx-grid-staging
```

Arşiv boyutu, üye yolları, symlink/hardlink, mükerrer üye, manifest ve dosya hash'leri
denetlenir. Açma işlemi `extractall` kullanmaz. Testler, paper guard ve demo yeni
dizinde ve credential içermeyen sınırlı ortamla çalıştırılır. Başarısız provada
geçici dizin temizlenir. Başarıda `/opt/okx-grid-staging/releases/<commit>` oluşur.
Bu yol tekrar kullanılamaz. Kilit ikinci staging işlemini engeller.

`/opt/okx-grid-demo`, alt dizinleri ve üst dizinlerine staging yasaktır. Araç
`current` bağlantısı oluşturmaz, systemd/supervisor çağırmaz, servis başlatmaz veya
yeniden başlatmaz. Çalışan v5'in durum dosyalarını açmaz ve migrasyon yapmaz.
Yalnızca bu yeni çekirdeğin kendi şemasını tanır; başka SQLite dosyasını reddeder.

## Entegrasyon ve aktivasyon kapısı

VPS kaynak erişimi sağlandıktan sonra tamamlanması gereken işler:

1. v5 kaynak/import envanteri, servis tanımı, timer/cron ve yalnızca ilgili prosesin
   durum dosyası yollarını değerleri ifşa etmeden tespit et.
2. Güncel public piyasa adaptörünü ve Telegram komut sözleşmesini incele. USDT quote
   hacmi, spot ürün durumu, borsa zamanı, fee/tick/lot/minimum tutar dönüşümlerini
   sentetik fixture'larla doğrula. Özel emir istemcisi ekleme.
3. Muhasebe/durum formatını karşılaştır. Gerçek kayıtların yalnızca sunucudaki
   özel ve tutarlı kopyasında yeniden oynatma/mutabakat yap; kayıtları commit etme.
4. Ayrı durum dosyasıyla shadow paper koşusu yap. Aynı Telegram token'ını iki
   long-polling tüketicisine verme ve bildirimleri yanlışlıkla çoğaltma.
5. Eski/yeni gözlemleri, hata oranını, kesintide yeniden oynatmayı ve disk dolmasını
   doğrula. Sürüm, sağlık kriteri ve geri dönüş planı somutlaştırılmadan servis
   aktivasyonu veya şema değişikliği yapma.

Bu oturumda erişim olmadığı için bu adımlar uygulanmış sayılmaz. Staging provası
üretimde kesintisiz geçişin doğrulandığı anlamına gelmez. Çalışan servisin
başlatılması/durdurulması bu değişiklik kapsamına eklenmemiştir.
