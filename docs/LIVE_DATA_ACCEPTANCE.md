# Canlı OKX TR verisi ve aşamalı GRID kabulü

23 Ekim 2026 kullanıcı hedefi: Mevcut Telegram botu yalnız OKX TR'nin canlı
fiyat, alış/satış emir defteri ve gerçekleşen işlem verileriyle doğrulanabilir
analiz üretecek. Hesaba bağlanıp otomatik GRID kurma/yürütme sonraki aşamadır.
Yeni bağımsız Telegram botu, farklı borsa verisi veya eski defter reseti yoktur.

## 1. Öncelikli ürün: Telegram'da canlı izleme ve analiz

| Veri | Kaynak/sunum ve kabul şartı |
| --- | --- |
| Fiyat, bid/ask, spread | Sabit `https://tr.okx.com` public API; hedef parite, kaynak ve gözlem zamanı belirtilir |
| 400 alış + 400 satış | Gerçek dönen book seviyeleri, zaman/sıra/tutarlılık; GRID için tam 400/400 gerekir |
| Gerçekleşen alış/satış | Taker işlemleri, kapsamı belirtilmiş pencere/CVD ve hacim teyidi; bekleyen emirlerden ayrıdır |
| Para akışı göstergeleri | CVD, RVOL, book baskısı ve çoklu TF; turnover/CVD müşteri bakiyesi veya net mevduat diye sunulmaz |
| GRID / erken momentum yorumu | Aynı kaynaklı kimlikli snapshot, açık ücret/risk modeli ve zorunlu filtreler; gerekirse NO TRADE |

Global OKX, Binance, başka borsa, relay/proxy veya sentetik fiyatla kayıp veri
tamamlanmaz. Bağlantı, parite, zaman, book, mum veya işlem penceresi
doğrulanamazsa `DATA_UNVERIFIED`; ölçülemeyen alan N/A'dır. Yüksek heuristik
skor başarısız zorunlu filtreyi geçersiz kılamaz. Kaynak doğrulaması stratejinin
gelecekteki kârlılığını kanıtlamaz.

"Gerçek zamanlı" kabulü yalnız bir başarılı API çağrısı değildir. Sürekli
ölçümde kaynak zamanı, yerel alım zamanı, veri yaşı, kesinti/yeniden bağlantı
ve Telegram teslim gecikmesi gözlenmelidir. REST snapshot, sürekli stream
diye etiketlenmez. Kaynağın ürettiği veri zamanı, analizin bitiş zamanı veya
Telegram ACK zamanı ile yenilenmiş sayılmaz. Geçerli capture'ın kuyrukta
eskimesi de uçtan uca kabul senaryosudur; eski rapor canlı gösterilmemelidir.

Mevcut motorun zaman/yaş sınırları kodda doğrulanır; bunlar üretimde ölçülmüş
gecikme SLA'sı değildir. Sürekli veri edinme yöntemi ve scheduler sıklığı
VPS'te ölçülmeden anlık/sürekli hizmet kabulü verilmez. Yeni bir stream origin'i
otomatik eklenmez; yalnız resmî TR kaynağı olduğu ayrıca doğrulanır.

Üretim kabulünde şu kanıtlar birlikte gerekir:

1. Yetkili VPS'ten gerçek TR discovery/fiyat/400+400/mum/trade verisi ve
   kaynak/zaman tutarlılığı; global fallback olmadan kesinti/stale retleri.
2. Mevcut V5 kaynak/hash, gerçek komut yönlendiricisi, tek poller/sender ve
   scheduler sözleşmelerinin yerinde incelenmesi; ayrı staging/shadow.
3. Mevcut botta /durum, hedef coin /analiz ve /grid, /radar, /paraakisi ve
   /momentum yanıtlarının gerçek gönderim/ACK, hata ve gecikme kabulü.
   SOL/PEPE ortak karar ve SUI/parite/case regresyonları aynı veriyle sınanır.
4. Yeniden başlatma, kuyrukta eski rapor, mükerrer komut, belirsiz teslim ve
   acil durdurma; sürekli örneklerde fiyat/akış veri yaşı ve kaynak görünürlüğü.

Yerel/CI sonuçları, kullanıcının VPS veya Telegram oturumunun kanıtı değildir.
Bu kapılar tamamlanmadan ilk aşama operasyonel tamamlandı diye raporlanmaz.

## 2. Sonraki aşama: otomatik Spot GRID

Önce aynı doğrulanmış canlı veri ile ayrı 600 USDT paper defterinde iki ayrı
300 USDT pozisyonun yürütme/muhasebe kabulü tamamlanır. Bot başına 3 USDT ve
toplam 6 USDT hedef planlanan risk, ücret/kayma/minimum tutar/net getiri ve
atomik rezervasyon geçerlidir. Stop gerçekleşen zararı kesin sınırlamaz.

Gerçek hesaba bağlanma ve Spot GRID emirlerini oluşturma/yönetme için ayrıca
platform/API yeteneği, private emir yaşam döngüsü, least-privilege yetki,
partial-fill/cancel/UNKNOWN mutabakatı, kill switch ve dağıtım/geri dönüş
kabulü gerekir. OKX TR'nin programatik native GRID desteği varsayılmaz;
desteklenen Spot emir yöntemi ayrıca doğrulanır. Kaldıraç/margin kapsam dışıdır.

Kullanıcının sonraki aşama hedefi şu an gerçek emir açma yetkisi değildir.
`LIVE_TRADING=false` ve private emir istemcisinin kapalı olması korunur;
gerçek para aşamasının etkinleştirilmesi ayrı açık yetkilendirme gerektirir.

## Güncel uygulama sınırı

Kod: yalnız TR GET, anlık REST capture, GRID/flow hook'ları ve mevcut owner'a
çağrılabilir adapter; offline paper kabulü vardır. Sürekli stream, çalışan
V5'e bağlantı, gerçek Telegram e2e ve canlı veride iki pozisyon kabulü henüz
doğrulanmadı. Son kaynak ve CI/public sonuçları [STATUS.md](STATUS.md), tam
entegrasyon sınırı [V7_INTEGRATION.md](V7_INTEGRATION.md) içindedir.
