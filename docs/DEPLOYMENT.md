# Servisi kesmeden kaynak incelemesi ve staging

Bu sürümde deploy veya aktivasyon workflow'u yoktur. GitHub Actions yalnızca
GitHub runner'ında doğrulama ve geçici staging provası yapar. SSH, VPS adresi,
OKX/Telegram anahtarı veya bulut kimliği kullanmaz.

## VPS kaynaklarını güvenli inceleme

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
