# V5 uyumluluk matrisi — SOL P0 denetimi

Kaynak: [SOL teknik denetimi, 20.10.2026](https://github.com/y8y9byfm5j-blip/okx-grid-trading-bot/pull/1#issuecomment-6283833803).
Dokuz kanonik dosyanın tam içerikleri bu çalışma ortamında veya PR dalında
yoktur. 22–23 Ekim ekranları servis/dizin/Python executable'ı, timer koşuları,
`find_plan`/`handle`/`main` ve API/send/offset/refresh/plans maskeli gövdelerini
kısmen doğruladı. Doğrudan VPS/SSH erişimi yoktur. Alan adları, maskeli sabitler
ve henüz görülmeyen kaynaklar tahmin edilmez. Güncel somut sınır ve staging
bağlantısı: [V5_OWNER_BRIDGE.md](docs/V5_OWNER_BRIDGE.md).

| Kanonik V5 bileşeni | Kaynaktan doğrulanması gereken sözleşme | Yeni çekirdekte aday eşleşme | Durum |
| --- | --- | --- | --- |
| `run_v5.sh` | Başlatılan modüller, env adları, timer/cron/systemd, tek örnek kilidi, restart davranışı | Operasyonel eşleşme yok; mevcut zamanlayıcı korunur | KISMİ — ExecStart yolu/timer koşuları görüldü; script ve unit gövdeleri yok |
| `grid_plan_engine.py` | Girdi ve dönüş tipleri, grid sayısının anlamı, tick/lot, TP/SL ve net getiri formatı | `GridPlan`, `build_plan`, `GridPlan.check` | BLOKE — arayüz/çıktı doğrulanmadı |
| `paper_trader_v5.py` | Dolum ve gap sırası, tick kimliği, başlangıç envanteri, stop/TP önceliği, durum şeması | `PaperTrader.open/tick/status`; yalnız referans paper motoru | BLOKE — davranış eşdeğerliği bilinmiyor |
| `accounting_v52.py` | Ücret para birimi, cost basis, gerçekleşen/gerçekleşmeyen PnL, nakit ve defter şeması | `_audit`, portföy korunum denklemleri | BLOKE — muhasebe/durum dönüşümü yok |
| `grid_reservation_v55.py` | Tek bakiye kaynağı, aktif rezervler, serbest bırakma, yarış ve crash atomikliği | Tek SQLite portföyü; atomik sermaye/risk rezervasyonu | BLOKE — V5 rezervasyon şeması bilinmiyor |
| `radar_v5_base.py` | TR enstrüman evreni, zaman damgası/birim, hacim, kline ve defter güncelleme sözleşmesi | `Market`, JSON `radar.screen`, offline public veri sözleşmeleri | BLOKE — canlı V5 adaptörü doğrulanmadı |
| `telegram_commands_v5.py` | Maskeli alanlar, tam kaynak/hash, hata formatları ve gerçek owner bağlantısı | `V5OwnerBridge` + `V5OwnerAdapter`: batch/ACL/checkpoint, mevcut send callback'i/ACK | KISMİ — API JSON/send dönüşü ve düz offset yazımı görüldü; staging testli, aktive değil |
| `telegram_notifier_v5_base.py` | Tek bildirim sahibi, escape/parse mode, dedup, rate limit, retry ve hedefler | Eşleşme yok; V5 tek bildirim sahibi kalır | BLOKE — kaynaktan doğrulanmadı |
| `telegram_plan_v5.py` | Alt/üst sınır, aritmetik grid, sermaye, TP/SL, ücret, planlanan risk/net getiri alanları | `GridPlan.to_dict`, `planned_loss`; doğrudan Telegram formatı değildir | BLOKE — çıktı uyumluluğu kanıtlanmadı |

## P0 kaynak aktarım sınırı

`tools/inspect_vps_source.py` dokuz adı allowlist olarak okur ve hiçbirini import
etmez. `.env`, SSH/private key, DB, runtime state/log ve trade geçmişi aktarım
kapsamı dışındadır. Salt okunur inceleme, secret-scan ve özel yerel export
araçlarının testleri vardır; bu işlemler VPS üzerinde çalıştırılmamıştır.
[DEPLOYMENT.md](docs/DEPLOYMENT.md) içindeki kaynak aktarım planı yalnızca
yetkilendirilmiş ayrı inceleme ortamında uygulanır. Kaynaklar özel inceleme
kanalında kalır; export dizini topluca Git'e eklenmez. Manuel secret incelemesi
ve sentetik test dönüşümü olmadan hiçbir ham V5 dosyası PR'a taşınmaz.

## Adaptör / shadow karşılaştırma tasarımı

1. V5 mevcut zamanlayıcı, durum deposu, Telegram long-polling ve bildirimlerin
   tek sahibi olarak çalışmaya devam eder. Yeni CLI ikinci bir operasyonel bot
   olarak başlatılmaz.
2. Kaynak incelemesinden sonra V5'in gerçek çıktısını normalize eden sürümlü bir
   adaptör tanımlanır. Girdi alanları ve hata durumları gerçek imzalardan çıkarılır;
   bilinmeyen alanlar veya şema farkında karşılaştırma kapalı kalır.
3. V5'in aynı doğrulanmış public fiyat/tick girdisi, yan etkisiz referans motora
   verilir. Ayrı ve özel SQLite dosyası kullanılır; V5 state'i açılmaz veya yazılmaz.
   Shadow, yeni public veri polling'i ya da Telegram tüketicisi başlatmaz.
4. Karşılaştırma: plan alanları, geçerli/elenen aday nedenleri, rezervasyon,
   dolum sırası, nakit, maliyet, fee ve PnL. Farklar yalnız özel inceleme raporuna
   yazılır; gerçek işlem kayıtları GitHub raporuna konmaz. Muhasebe için tolerans
   uydurulmaz; V5 hassasiyet politikası görülünce sürümlenir.

`V5OwnerBridge` gözlenen Telegram döngüsü için staging'de uygulanıp sentetik
test edildi. V5 plan/muhasebe normalizasyon adaptörü ve shadow zamanlayıcısı
uygulanmadı; gerçek Telegram uyumluluğu ve üretim bağlantısı doğrulanmadı.

## Geçiş ve geri dönüş kapıları

| Kapı | Kabul kanıtı | Mevcut durum |
| --- | --- | --- |
| G0 Kaynak | Dokuz dosya + import bağımlılıkları ve tutarlı hash envanteri | Kısmi ekran kanıtı; tam kaynak/hash hâlâ yok |
| G1 Sözleşme | Gerçek imza/çıktı/state/komut eşleştirmeleri ve sentetik regresyonlar | API/send/offset kısmen görüldü; batch/ACK adaptörü testli; legacy state ve üretim bağlantısı açık |
| G2 Risk | Açık `v5-paper-500` profili, 5 USDT bot/15 USDT toplam planlanan risk ve tek 500 USDT portföy testleri | Bu dalda yerel/CI kanıtı raporlanır; V5 eşdeğerliği değil |
| G3 Shadow | Aynı girdide V5/referans karşılaştırması, stale/gap/restart/race/rollback uyumu | G1 bekliyor |
| G4 Dağıtım | Tam commit/hash, ayrı staging, testler ve `activated:false` | Araç mevcut; aktivasyon izni yok |
| G5 Aktivasyon | G0–G4 kanıtları + ayrı açık aktivasyon talimatı | Bu görevde yasak |

Shadow hata verirse sadece karşılaştırma çağrısı devre dışı bırakılır; V5'e
restart/sinyal, Telegram token devri veya state geri yazımı yapılmaz. Ayrı shadow
defteri inceleme için korunur; açık pozisyonlar silinmez. Bu geri dönüşün gerçek
V5 test harness'i G1 sonrası yazılabilir. Bugünkü test edilebilir geri dönüş
sınırı release staging'dir: başarısız testte pending dizin temizlenir, mevcut
release üzerine yazılmaz ve servis aktive edilmez (`test_release.py`).
