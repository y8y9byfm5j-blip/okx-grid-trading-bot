# Public veri sözleşmeleri — offline doğrulama sınırı

Resmî kaynak: [OKX TR API dokümanı](https://tr.okx.com/docs-v5/en/),
20.10.2026 tarihinde kontrol edildi. Referans bölümler: Production Trading
Services, Public Data/Get instruments, Market Data/Ticker, Order book ve
Candlesticks. REST kökü `https://tr.okx.com`.

| Allowlist GET | Normalize edilen bilgi |
| --- | --- |
| `/api/v5/public/instruments` | TR yanıtındaki live SPOT/USDT, base/quote, tickSz/lotSz/minSz |
| `/api/v5/market/ticker` | last/bid/ask, quote hacmi volCcy24h, milisaniye ts |
| `/api/v5/market/books` | Tam defter, dört elemanlı seviye, baz miktar, seqId ve ts |
| `/api/v5/market/candles` | 15m/1H, dokuz alan, tamamlanma bayrağı ve OHLC/volume |

Spot `volCcy24h` zaten quote cinsindedir; `vol24h` ile değiştirilmez veya tekrar
fiyatla çarpılmaz. `minSz` baz miktarıdır, USDT minimum tutar gibi etiketlenmez.
Fee/minimum notional profil/plan ayarıdır; private fee endpoint'i çağrılmaz.

Bu dokümandan sonraki kurallar referans çekirdeğin yerel kabul politikasıdır.
`okx_grid/public_contracts.py` yalnız supplied response normalizasyonu ve GET
istek tanımı üretir. Bu dalın ayrı `okx_grid/public_http.py` modülü, yalnızca
açıkça çağrıldığında sabit OKX TR origin ve allowlist GET için HTTP isteği
yapabilir. API anahtarı, imza, WebSocket, arka plan polling'i veya private/order
yolu yoktur. `source_origin` alanı zorunlu metadata
olsa da tek başına gerçek origin kanıtı değildir; gelecekteki transport isteği ve
yanıtını TLS/origin doğrulamasıyla bağlamalı, redirect'i reddetmelidir.

## Referans doğrulama politikası

- Ticker ve book zamanları önce milisaniye hassasiyetinde kontrol edilir;
  birleştirilen snapshot'ta en eski damga saniyeye çevrilir. Şimdiki zaman
  verilerek eski veri yeniden damgalanmaz. İki veri arasında en fazla 2 saniye
  fark kabul edilir; eski/future/boş/crossed/unsorted/mismatched book reddedilir.
- Tam REST snapshot seqId'si geriye gidemez. Aynı seqId/farklı içerik conflict'tir.
  Tam snapshot'ta atlanan seqId tek başına hata değildir; WebSocket delta
  birleştirme veya prevSeqId sürekliliği uygulanmış gibi sunulmaz.
- Yakın derinlik ve sermayeye bağlı risk kontrolü `GridPlan.check` sırasında da
  zorunludur. Başarılı normalizasyon, tek başına bot açma izni değildir.
- Yalnız en son tamamlanmış 15m/1H bar kullanılır; sıralama, zaman hizası,
  confirm, OHLC ve hacimler doğrulanır. Getiri bar close/open oranından üretilir;
  ayrı BTC-USDT 1H barı gerekir. Pencere bitişi ve yeniden kullanma yaşı denetlenir.
  Normalize oranlar 12 ondalığa sıfırdan uzağa yuvarlanır; küçük negatif getiri
  sıfıra dönüşüp trend filtresini aşmaz.
- `retry_delay` sadece test edilen scheduling modelidir: 429, geçici 5xx,
  network hata işareti veya 50011 için en fazla üç retry; 1/2/4 saniye, Retry-After
  varsa ona uyar. 30 saniyeden büyük cooldown'da otomatik devam etmez. Transport,
  endpoint'e özel pacing, jitter ve gerçek timeout davranışı uygulanmış değildir.

## Canlı doğrulama blokajı

Bu yürütme ortamının ağ izin listesinde `tr.okx.com` yoktur; doğrudan TR public
API çağrısı veya canlı piyasa smoke testi yapılmadı. Resmî dokümanın okunması
ve sentetik TR yanıtı, bir paritenin bugün TR'de mevcut olduğu kanıtı değildir.
Global OKX verisi TR verisi yerine kullanılmadı. Mevcut `radar.py` hâlâ JSON
önerisi filtresidir; bu düzeltme onu canlı radar yapmaz.

Salt okunur HTTP kodu mock yanıtlarla doğrulanmıştır; gerçek OKX TR bağlantısında
başarılı smoke test veya üretim scheduler entegrasyonu yapılmamıştır. V5 adaptörü,
V5 kaynak sözleşmesi ve yetkili ağ erişimi sağlandıktan sonra ayrı kapıdan geçmelidir. Gerçek response kayıtları
GitHub'a taşınmaz; deterministik sentetik fixture'larla sözleşme test edilir.
Duvarlardan kesin fiyat veya gerçekleşebilir getiri tahmini üretilmez.
