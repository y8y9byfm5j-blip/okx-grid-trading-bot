# OKX TR GRID Radar & Paper Trader

Bu dal, **bağımsız ve yalnızca paper işlem yapan** bir çekirdek ekler. İncelenen
`main` dalında VPS'teki v5 kaynakları yoktu. Bu kod, çalışan botun yerine
kurulmuş veya mevcut Telegram/OKX bağlantısına entegre edilmiş değildir.

Python 3.11–3.13 ve standart kütüphane yeterlidir; API anahtarı veya paket
kurulumu gerekmez. Komutlar harici servise bağlanmaz ve gerçek emir göndermez.

```bash
python3 tools/validate.py
python3 -m okx_grid demo
python3 -m okx_grid health
python3 -m okx_grid --profile v5-paper-500 demo
```

Profilesiz `demo` içindeki TEST-USDT, fiyatlar ve 2.000 USDT portföy tamamen sentetiktir.
Demo sonuçları piyasa performansı veya gerçek hesap bakiyesi değildir.

SOL P0/P1 düzeltmesi için [V5_COMPATIBILITY.md](V5_COMPATIBILITY.md) ve
[500 USDT profili](docs/RISK_PROFILE.md) eklendi. `--profile v5-paper-500`
açıkça seçilir: tek 500 USDT portföy, bot başına 5 USDT ve toplam 15 USDT
planlanan risk. Eski global varsayılan değiştirilmez. Profil kimliği deftere
sabitlenir; drift'te işlem reddedilir. Yeni çekirdek V5'in operasyonel alternatifi
olarak başlatılmaz; kaynakla uyumluluk/shadow kapıları hâlâ kapalıdır.

[Public veri sözleşmeleri](docs/PUBLIC_DATA_CONTRACTS.md) sentetik yanıtlarla test
edilir; HTTP transport'u veya canlı TR piyasa doğrulaması içermez.

| Parça | Davranış |
| --- | --- |
| Radar | Sağlanan market/plan/trend JSON önerilerini hacim, spread, yakın derinlik, 15 dakika/1 saat trendi ve BTC rejimiyle süzer. |
| Grid planı | Aritmetik seviyeler, tick/lot uyumu, minimum tutar, ücret sonrası pozitif grid getirisi ve risk bütçesi kontrol edilir. |
| Paper muhasebe | Decimal, SQLite atomik işlem, değiştirilemeyen olaylar, bakiye/envanter mutabakatı ve yeniden başlatma desteği. |
| Risk | Bot başına ve toplam tahsis/risk sınırı, bot sayısı, stop/TP, kalıcı durdurma ve gerçekleşen zarar kesicisi. |
| Güvenlik | Git index içeriği taranır; gizli değerler rapora yazılmaz; runtime dosyaları release'e girmez. |
| Dağıtım | Tam commit, dosya hash manifesti ve arşiv hash'i doğrulanır; test edilen sürüm yalnızca ayrı staging dizinine hazırlanır. |

Kullanım ve veri sözleşmesi: [USAGE.md](docs/USAGE.md).
VPS kaynak incelemesi ve dağıtım: [DEPLOYMENT.md](docs/DEPLOYMENT.md).
Araştırma bulguları ve tamamlanamayan entegrasyon: [STATUS.md](docs/STATUS.md).

`TRADING_MODE=live` veya `LIVE_TRADING=true` işlemi hata ile durdurur.
Bu projede özel OKX emir istemcisi yoktur; ortam değişkeniyle açılamaz.
