# OKX TR canlı defter ve gerçekleşen akış — V7 owner bağlantısı

23 Ekim 2026. Durum: kod ve sentetik testler hazır; gerçek runner sonuçları
draft PR #10'da exact SHA ile raporlanır. Üretim V5 bağlantısı ve gerçek
Telegram e2e henüz doğrulanmadı; **ACTIVATED=false**.

Öncelik sırası: [yalnız canlı TR verisiyle Telegram kabulü](LIVE_DATA_ACCEPTANCE.md)
→ canlı veride paper GRID kabulü → ayrıca yetkilendirilen gerçek Spot GRID.
Bugünkü anlık REST görüntüleri sürekli stream veya üretim aktivasyonu değildir.

## Kaynak ve ölçümlerin anlamı

Mobil uygulama ekranını okumak yerine, sabit `https://tr.okx.com` origin'ine
izinli salt okunur GET istekleri kullanılır. Global OKX, başka borsa, proxy
veya sentetik veri canlı TR kaynağı yerine geçirilemez. Endpoint kataloğu:
`/api/v5/public/instruments`, `/api/v5/public/time`, `/api/v5/market/tickers`,
`ticker`, `books`, `trades`, `history-trades` ve `candles`.
Kaynak, saat, yaş, parite, book sıra/tutarlılığı, tamamlanmış mum ve beş
dakikalık işlem penceresi kontrolleri korunur. Kaynak bağlantısı ayrı ayrı
doğrulanır; bir başarılı capture sürekli erişilebilirlik değildir.

| Ölçüm | Gösterdiği | Göstermediği |
| --- | --- | --- |
| Bid/ask, spread, 0,5%/1% derinlik | Gözlenen REST defterindeki fiyat/likidite | Gerçek fill veya kalıcı likidite garantisi |
| Agresif alış/satış | Gerçekleşen taker işlemlerinin fiyat × miktar toplamı | Borsadaki müşteri bakiyeleri |
| Pencere CVD | Ölçülen aralıktaki alış eksi satış USDT eşdeğeri | Net mevduat veya yeni dış sermaye |
| RVOL | Son tamamlanmış 5m hacmin önceki 20 tamamlanmış 5m baza oranı | Fiyatın yükseleceği garantisi |
| 24h turnover payı | Desteklenen keşfedilmiş evrendeki göreli işlem aktivitesi | Coin içinde tutulan para miktarı |
| Pay değişimi | Karşılaştırılabilir iki snapshot arasındaki yüzde puan farkı | Coinler arasında izlenmiş gerçek para transferi |

REST book istenen kapasitesi 400 alış/400 satıştır; flow raporu gerçekten
dönen seviye sayısını açıkça yazar. GRID yürütmesindeki tam 400/400 şartı
aynen korunur. Defter baskısı, gerçekleşen taker teyidinin yerine geçmez.
Resmî alan tanımları: https://tr.okx.com/docs-v5/en/ (23 Ekim 2026 kontrolü).

## Mevcut owner için komutlar

| Komut | V7 hook davranışı |
| --- | --- |
| `/paraakisi`, `/momentum` | Keşfedilen evrende en fazla üçlü dönen batch; tüm piyasa eşzamanlı top listesi değildir |
| `/paraakisi SOL`, `/momentum SOL-USDT` | Doğrudan SOL-USDT; başka coine/quote'a fallback yok |
| `/paraakisi SOL/USDT` | Aynı normalize hedef; case ve Türkçe ı/İ destekli |
| `/momentum SOL-TRY` | Yalnız keşfedilirse ve TRY/USDT dönüşümü doğrulanırsa; stale durumda ret |
| `/durum` | GRID veri sağlığı ile flow veri sağlığını ayrı gösterir |

Sinyal bulunmadığında **NO TRADE** açıkça kalır; geçerli izleme ölçümleri
gizlenmez. Rapor bid/ask, spread, gerçek seviye sayısı, book timestamp/sequence,
derinlik, agresif alış/satış, CVD, işlem penceresi/eşsiz işlem, RVOL ve 24h
aktiviteyi içerir. Coin CVD kapsamı ve parite CVD birbirinden ayrıdır.
Geçersiz hedef açıklayıcı `FLOW_SYMBOL_NOT_DISCOVERED` verir.
V6 eski callback sözleşmesi yerine kullanılan bu hedefli komutlar V7 hook'una
aittir; üretimdeki V5 handle'ın değiştirildiği anlamına gelmez.

## Kalıcı karşılaştırma ve hata davranışı

Tam ve taze hacim evreninin baseline'ı yalnız ayrı V7 defterinde korunur.
İkinci karşılaştırma aynı enstrüman evrenini, aynı payda kapsamını ve farklı,
en fazla bir saat aralıklı snapshot'ları gerektirir. İlk/eskimiş baseline'da
değişim **N/A**; sıfır diye sunulmaz. Hedefli inceleme dönen radar cursor'ını
ilerletmez. Duplicate Telegram receipt yeni GET veya ikinci karşılaştırma
üretmez.

Eksik/eski evren hacminde geçerli tek parite ölçümleri gösterilebilir; pay ve
değişim N/A, aday sıralaması kapalıdır. Kısmi baseline tam baseline'ın üzerine
yazılmaz. Evren değiştiğinde karşılaştırma reddedilir ve yalnız tarama
metadata'sı temizlenir; sermaye, pozisyon veya eski V5 defteri resetlenmez.

Flow sağlığı kalıcı son ölçüm zamanı ile raporlanır ve yaş sınırını aşınca
DATA_UNVERIFIED olur. Kesinti önceki başarılı flow sağlığını korumaz.
Flow doğrulaması GRID doğrulaması olarak gösterilmez; GRID başlangıç kapısını
açmaz. İzleme kendi başına paper motorunu hazırlamaz, rezerv veya emir açmaz.
Momentum yönlü execution planı olmadan izleme üretir; gerçek emirler kapalıdır.

## Kabul ve kalan üretim kapısı

Offline: `tests/test_v7_flow_tracking.py` hedef/case/parite, NO TRADE'de gerçek
alanların gösterilmesi, restart baseline/pay değişimi, eksik payda, evren
değişimi, receipt replay, kaynak fixture reddi ve yaş/kesintiyi kapsar.

İzole yetkili ortamda:

```sh
python tools/flow_smoke.py --allow-public-network --target SOL-USDT --v7-report
```

Bu komut sadece kendi geçici 600 USDT defterini ve gerçek default TR istemcisini
kullanır. Core/rapor yolunu test eder; bot poller/sender/scheduler veya mevcut
defter yolu kabul etmez. Engine hazırlanmaz, pozisyon açılmaz. Çıktının
`telegram_verified=false`, `production_activated=false` alanları korunur.
GitHub public probe BTC-USDT ve SOL-USDT için bu yolu ayrıca sınar.

23 Ekim ekranlarında V5 `api`, `send`, `load_offset`, `save_offset`,
`refresh_v5`, `load_plans` maskeli gövdeleri görüldü; yeniden eksik sayılmaz.
Tam kaynak/hash, maskeli alanlar ve kalan kaynak/scheduler sözleşmeleri
yerinde inceleme bekler. Ayrıntı: [V5_OWNER_BRIDGE.md](V5_OWNER_BRIDGE.md).
İkinci Telegram botu kurulmaz. Shadow, gerçek Telegram ACK/e2e ve ayrı staging
tamamlanmadan aktivasyon doğrulanmış sayılmaz; servis değişikliği/restart
ayrıca açık dağıtım onayı gerektirir. Token/.env/private key aktarılmaz.
