# Açık `v5-paper-500` profili

Bu profil SOL denetimindeki paper başlangıç bütçesini uygular; VPS'teki gerçek
konfigürasyon dosyasının okunmuş veya değiştirilmiş olduğu anlamına gelmez.
Tam ve düzenlenebilir örnek: `okx_grid/config/v5-paper-500.toml`.

| Ayar | Açık değer | Anlam |
| --- | --- | --- |
| `equity` | 500 USDT | Bütün botlar için tek başlangıç özkaynağı |
| `risk_fraction` | 0.01 | Bot başına en fazla 5 USDT planlanan stop kaybı |
| `total_risk_fraction` | 0.03 | Aynı anda en fazla 15 USDT planlanan risk rezervi |
| `max_allocation_fraction` | 1 | Toplam sermaye rezervi en fazla 500 USDT; bot başına ayrı 500 yok |
| `max_bot_capital` | 500 USDT | Sermaye tavanı; risk sınırını veya toplam bakiye sınırını aşma izni değil |
| `max_bots` | 3 | En fazla üç açık paper bot |
| `max_realized_loss_fraction` | 0.02 | Toplam 10 USDT gerçekleşen zararda kalıcı halt; günlük sıfırlama yok |
| `mode`, `leverage` | paper, 1 | Kaldıraç/short veya gerçek emir yok |

Spread, hacim, veri yaşı ve derinlik dahil bütün RiskPolicy alanları TOML içinde
açıkça yazılıdır. Yeni alan eklenirse veya bir alan silinirse profil eksik sayılır;
2.000 USDT demo varsayılanıyla sessizce tamamlanmaz. Kullanıcı özelleştirmesi için
örneği özel bir konuma kopyalayın ve decimal alanları metin olarak düzenleyin.

```bash
python3 -m okx_grid --profile v5-paper-500 health
python3 -m okx_grid --profile v5-paper-500 demo
python3 -m okx_grid --profile v5-paper-500 --state /private/shadow-500.sqlite3 status
python3 -m okx_grid --profile-file /private/custom-paper.toml --state /private/custom-shadow.sqlite3 status
```

Bu komutlar kaynak inceleme ortamında veya yerelde referans paper çalışması
içindir; V5 yanında ikinci operasyonel bot başlatma talimatı değildir. `demo`
bellekte sentetik 90 USDT grid sermayesiyle çalışır, kalan bakiye aynı 500 USDT
portföyünde serbest kalır. Profilesiz eski demo ve `RiskPolicy.equity=2000`
değiştirilmedi. `--profile`, `--profile-file` ve eski `--policy` karşılıklı dışlanır.

## Drift ve defter sınırı

Profil adı, revizyonu ve bütün sayısal/çalıştırma ayarlarının SHA-256 kimliği yeni
deftere kaydedilir. CLI her çağrıda dosyayı yeniden okur. Sonradan alan/revizyon
değişmesi, profil yerine salt policy verilmesi veya profilli defterin profilesiz
açılması hata verir. Otomatik reset/migrasyon/adopt yapılmaz. Açık bir API nesnesi
yüklediği immutable profil snapshot'ını kullanır; her işlemde defter metadata'sını
kontrol eder, dosyayı arka planda hot-reload etmez.

Eski 2.000 USDT veya V5 defterini bu profille yeniden açmayın. Ayrı özel shadow
dosyası gereklidir. Profil değişikliği açık paper pozisyonları kapatılmış gibi
değerlendirilmez; orijinal konfigürasyonla muhasebe incelenmelidir.

## Tek bakiye ve korunum

`status.portfolio`, bot filtresi kullanılsa bile bütün portföyü raporlar:

`serbest nakit = 500 + kapanan botların gerçekleşen PnL'si − açık bot sermaye rezervi`

`toplam nakit = serbest nakit + açık botların nakdi`

`toplam nakit + envanter maliyeti = 500 + bütün botların gerçekleşen PnL'si`

`tasfiye özkaynağı = toplam nakit + net envanter değeri`

Kapanan botun başlangıç sermayesi ve nakdi iki kez eklenmez. Örneğin üç
166,66 USDT botta rezerv 499,98, serbest nakit 0,02 ve ilk özkaynak 500 USDT'dir.
Risk hesapları başlangıç bütçesiyle sınırlanır; kâr otomatik risk büyütmez, zarar
bütçeyi daraltır. Hem serbest bakiye hem tahsis/risk kontrolleri aynı SQLite
işleminde yapılır; yarış sırasında aynı 500 USDT tekrar harcanamaz.

Ücret alış maliyetine/satış netine dahildir. Planlanan kayıp, komisyon ve stop
kayması varsayımını içerir; 5 USDT garanti değildir. Stop altında büyük gap'te
gerçekleşen paper zarar 5 USDT'yi aşabilir. Testler bunu açıkça doğrular.

İlgili regresyonlar: `test_v5_profile.py` (20 test), profil CLI testleri,
`test_paper.py`, `test_domain.py`. Bunlar V5'in görülmemiş işleyişiyle eşdeğerlik
kanıtı değil, referans çekirdeğin açık risk/muhasebe sözleşmesidir.
