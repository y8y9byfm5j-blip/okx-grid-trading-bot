# Paper çekirdeği ve veri sözleşmesi

## Çalıştırma

Depo kökünden `python3 tools/validate.py` tüm kontrolleri çalıştırır. Testler
sentetik girdiler ve geçici dizinler kullanır; OKX, Telegram veya VPS'e bağlanmaz.
Testlerin eksik olması ve sıfır test keşfi başarısızlık sayılır.

`python3 -m okx_grid demo` bellekte bir grid alım/satım döngüsü çalıştırır.
Çıktıda ücret, nakit, envanter, gerçekleşen/gerçekleşmeyen PnL ve kapanış nedeni
yer alır. Bu yeni çekirdek v5 dosyalarını içe aktarmadığı için onların import
yan etkilerini de çalıştırmaz.

Kalıcı işlemler için kendine ait yeni bir SQLite dosyası seçilir. Örnek komutlar:

```bash
python3 -m okx_grid --state /var/lib/okx-grid-paper-vnext/paper.sqlite3 --policy /private/policy.json open sample /private/plan.json /private/market.json
python3 -m okx_grid --state /var/lib/okx-grid-paper-vnext/paper.sqlite3 --policy /private/policy.json tick sample tick-1 99.9 1792440001
python3 -m okx_grid --state /var/lib/okx-grid-paper-vnext/paper.sqlite3 --policy /private/policy.json status
python3 -m okx_grid --state /var/lib/okx-grid-paper-vnext/paper.sqlite3 --policy /private/policy.json halt
```

Yollar örnektir. `tick` zaman damgası geçerli anın UTC Unix saniyesi olmalıdır;
yukarıdaki sabit damga güncel veri yerine kullanılmaz. CLI girdi dosyalarını
kendisi oluşturmaz. Dizin için gerekli yerel yazma yetkisi gerekir.
`--policy` her yeniden açılışta ilk kaydedilen ayarlarla aynı olmalıdır.
Varsayılanlar yalnızca sentetik örnekler içindir; gerçek hesapla senkronize değildir.

`halt` yeni botları engeller. Envanter, her botun bir sonraki geçerli tick'inde
kapatılır; veri yokken fiyat uydurularak kapatma yapılmaz. Otomatik yeniden
başlatma/durdurmayı kaldırma komutu yoktur. Sebep ve muhasebe incelenmeden yeni
bir dosya açmak, eski envanterin tasfiye edildiği anlamına gelmez.

## Plan JSON

Sayısal para/fiyat alanlarını metin olarak verin. Python API'si float kabul etmez.
JSON CLI'si ondalık sayıları doğrudan Decimal'a çevirir.

```json
{
  "instrument": "TEST-USDT",
  "lower": "98",
  "upper": "102",
  "grids": 4,
  "capital": "300",
  "stop": "97",
  "take_profit": "103",
  "fee_rate": "0.001",
  "slippage_rate": "0.001",
  "tick_size": "0.01",
  "lot_size": "0.001",
  "min_notional": "5"
}
```

Komisyon, tick, lot ve minimum tutar bu örnekte temsili değerlerdir. Gelecek
veri adaptörü bunları ilgili spot enstrümanın doğrulanmış güncel bilgilerinden
sağlamalıdır. Her grid için eşit quote bütçesi ayrılır ve miktar aşağı yuvarlanır.
Bir gridin alış bedeli ve alış komisyonu birlikte o bütçeyi aşamaz.

## Market JSON

```json
{
  "instrument": "TEST-USDT",
  "price": "100",
  "bid": "99.99",
  "ask": "100.01",
  "quote_volume_24h": "20000000",
  "observed_at": 1000,
  "bids": [["99.99", "1000"], ["99.95", "1000"]],
  "asks": [["100.01", "1000"], ["100.05", "1000"]]
}
```

`observed_at` UTC Unix saniyesidir. OKX'in milisaniye damgaları adaptörde
dönüştürülmelidir. `quote_volume_24h` USDT cinsinden hacimdir; baz coin miktarı
bu alana yazılmaz. Emir defterindeki miktarlar baz coin adedidir.
Bid seviyeleri kesin azalan, ask seviyeleri kesin artan ve miktarlar pozitif
olmalıdır. İlk seviyeler `bid`/`ask` ile eşleşmelidir. En fazla 400 seviye alınır.
İki tarafın yalnızca en iyi fiyattan %0,5 uzaklıktaki görüntülenen derinliği sayılır.
Bu veri bir anlık görüntüdür; duvarın kalıcılığı veya gerçekleşebilir hacim iddiası
üretilmez. Kaynak bağlantısı bu dalda bulunmadığından veri doğruluğunu adaptör
ayrıca doğrulamalıdır.

## Radar

`python3 -m okx_grid --policy /private/policy.json radar /private/proposals.json`
bir öneri listesi alır. Her öneri `market`, `plan` ve aşağıdaki `trend` alanlarını
içerir. `plan` komutu yalnız plan ve market dosyalarını doğrular.

```json
{
  "return_15m": "0.001",
  "return_1h": "0.005",
  "btc_return_1h": "0",
  "observed_at": 1000
}
```

Getiriler orandır: `0.01` %1 anlamındadır. 15 dakika veya 1 saat getirisinin
negatif olması, BTC'nin 1 saatte %2 veya daha fazla düşmesi, 15 dakikada %5'ten
fazla yükseliş ve eski trend verisi elenir. Bu eşikler mevcut stratejinin
doğrulandığı iddiasını taşımaz; yeni çekirdeğin açık, test edilen başlangıç
kurallarıdır. Geçenler spread'e, sonra quote hacmine göre sıralanır. Radar hiçbir
botu kendiliğinden açmaz ve bir AI/kâr tahmini skoru üretmez.

## Risk ve muhasebe

Örnek RiskPolicy: `equity=2000`, `max_bot_capital=500`,
`max_allocation_fraction=0.8`, `risk_fraction=0.01`,
`total_risk_fraction=0.03`, `max_bots=3`,
`max_realized_loss_fraction=0.02`, `min_quote_volume=10000000`,
`max_spread_bps=10`, `max_market_age=30`, `max_future_seconds=2`,
`min_depth_multiple=5`. Ondalık alanlar policy JSON'unda metin olarak verilir.

Planlanan stop kaybı, bütün sermayenin üst sınırdan alındığı, alış ve stop
çıkışında kayma/komisyon ödendiği senaryodan hesaplanır; 8 ondalığa yukarı
yuvarlanır. Bu değer fiyat boşluğunda garanti edilen azami kayıp değildir.
Gerçekleşen toplam zarar başlangıç paper özkaynağının %2'sine ulaştığında kalıcı
durdurma devreye girer. Bu, takvim gününde sıfırlanan günlük limit değildir.
Gerçekleşen kâr risk bütçesini kendiliğinden artırmaz; gerçekleşen zarar bütçeyi
daraltır. Risk rezervi bot kapanıncaya kadar tutulur.

Her mutasyonda olaylar yeniden hesaplanır ve şu eşitlik kontrol edilir:

`nakit + taşınan envanter maliyeti = başlangıç sermayesi + gerçekleşen PnL`.

Ücretler alış maliyetine ve net satış tutarına dahildir. Gerçekleşmeyen PnL,
son fiyat üzerinden varsayılan tasfiye komisyonu ve olumsuz kayma düşülerek
hesaplanır. Bot ve tick kimlikleri sınırlı karakter kümesine sahiptir. Aynı
kimlik/içerik tekrarında yeni dolum oluşmaz; değişik içerik hata verir.
SQLite `BEGIN IMMEDIATE` ile rezervasyon/dolum/olay kaydı atomiktir; hata halinde
tamamı geri alınır. Olay güncelleme/silme SQL trigger'larıyla engellenir.
Bu, dosyaya tam erişimi olan bir saldırgana karşı kriptografik koruma değildir.

## Simülasyon sınırları

- Başlangıçta bütün sermaye nakittir; hazır envanter uydurulmaz.
- Düşen tick'in geçtiği boş gridler limitten alınır; yükselen tick'in geçtiği
  mevcut pozisyonlar satış limitinden satılır. Bir slot aynı tick'te hem alınmaz
  hem satılmaz. Fiyatın tick aralarında izlediği yol tahmin edilmez.
- TP üzerindeki boşlukta mevcut satış limitleri önce limit fiyatından dolar;
  daha yüksek fiyatla ek kâr yazılmaz. Stop altındaki boşlukta görülen fiyat ve
  olumsuz kayma kullanılır.
- Kuyruk önceliği, kısmi dolum, transfer, vergi, funding ve borsa mutabakatı
  modellenmez. Kaldıraç ve short yoktur. Bu sonuçlar canlı performans testi değildir.
- Ledger ve tick geçmişi özel dosyada tutulur; saklama politikası burada geçmişi
  silmez. Uzun süreli yük ve diskin dolması VPS üzerinde ayrıca sınanmalıdır.
