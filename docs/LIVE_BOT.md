# OKX TR Telegram Bot — canlı servis ve kurulum

Bu servis mevcut Telegram botunun token'ıyla çalışır ve şunları yapar:

| Komut | Ne yapar |
| --- | --- |
| `/rotasyon` | Ana para takibi: son 24 saatte hangi coinlerde net agresif alış/satış var, para hangi coinden çıkıp hangisine gidiyor, USDT ↔ coin yönü, hacim payı değişimi, TRY paritelerinde (Türk tarafı) payı artanlar |
| `/erkenlong` | Büyük yükseliş öncesi birikim adayları (günlük + haftalık): hafif yükseliş, hacim artışı, OBV birikimi, volatilite sıkışması, trend dönüşü, yükselen haftalık dipler, BTC'den güç, agresif alıcı baskınlığı. Çok yükselmiş coinler "geç kalındı" diye elenir. |
| `/analiz COIN` | Tek coin: grid kriterleri (V7 kuralları), erken long skoru, 24s para akışı |
| `/grid COIN` | Bu coin için kurulacak grid planı (alt/üst fiyat, grid sayısı, stop, kâr al) |
| `/botlar` | Açık ve kapanan grid botları, kullanılan sermaye, gerçekleşen kâr/zarar |
| `/durum` | Mod, limitler, son veri zamanları |
| `/otomatik ac` / `/otomatik kapat` | Otomatik grid kurulumu |
| `/durdur COIN` | O coindeki botu durdurur (coin USDT'ye satılır) |
| `/acil_durdur` | Tüm botları durdurur, otomasyonu kapatır |
| `/devam` | Zarar sınırı veya acil durdurmadan sonra yeniden başlatmaya izin verir |

Otomatik bildirimler: yeni erken long adayı (skor ≥ 70, aynı coin için 3 günde bir),
kurulan/kapanan grid botları, zarar sınırı.

## Nasıl çalışır

- **Veri:** yalnız `https://tr.okx.com`. Binance veya başka borsa yok.
  Not: `tr.okx.com` herkese açık piyasa verisi global OKX emir defteriyle aynıdır
  (aynı fiyat, aynı hacim). Türk yatırımcıya özgü sinyal TRY pariteleridir; bu yüzden
  `/rotasyon` TRY tarafını ayrıca gösterir.
- **Para akışı:** OKX'in coin bazında toplu agresif alış/satış hacmi (taker volume)
  ve 24 saatlik hacim payı. Bunlar gerçekleşen işlemlerdir; müşteri bakiyesi veya
  borsaya giren para değildir.
- **Grid kararı:** daha önce kabul edilen V7 kuralları değişmeden kullanılır:
  10M USDT hacim, spread ≤ 10 bps, 400+400 emir defteri, ±%0,5 derinlik,
  15dk/1s/4s trend, BTC rejimi, komisyon sonrası net grid kârı ve risk sınırı.
  Her 15 dakikada en fazla 3 coin incelenir; uygun olan olursa grid botu kurulur.
- **Grid botu:** OKX TR'nin kendi Spot Grid botu (`tradingBot/grid`) kurulur:
  aritmetik grid, yatırım USDT, stop-loss ve kâr al tetikleri OKX'te tanımlı olur.
  Servis kapansa bile stop/kâr al OKX tarafında çalışır.

## Limitler (sizin seçiminiz)

| Ayar | Değer |
| --- | --- |
| Toplam sermaye | 600 USDT |
| Bot başı sermaye | 300 USDT |
| En fazla bot | 2 |
| Bot başı planlanan stop zararı | 3 USDT |
| Toplam gerçekleşen zarar sınırı | 12 USDT → otomasyon durur |
| Aynı paritede bekleme | bot kapandıktan sonra 6 saat |

**Önemli:** 300 USDT'lik bir botta 3 USDT stop zararı, grid aralığının yaklaşık
%0,7'den dar olmasını gerektirir. 27 Ekim 2026'da en hacimli 8 coinde planlanan
zarar 4,3 (BTC) ile 14,7 (ZEC) USDT arasındaydı, yani bu limitle hiçbiri uygun
çıkmadı. Bot böyle durumlarda **BEKLE** der ve işlem açmaz. Daha geniş grid
istenirse `GRID_RISK_PER_BOT` artırılmalı (ör. 10–15 USDT) ya da bot başı sermaye
düşürülmelidir. Bu karar size aittir; ayar `/etc/okx-tr-bot.env` içindedir.

Stop-loss bir hedef fiyattır; ani düşüş/likidite boşluğunda gerçek zarar daha
büyük olabilir.

## Kurulum — ücretsiz, telefondan veya bilgisayardan (3 adım)

Termius gerekmez. DigitalOcean'ın tarayıcıdaki ücretsiz konsolu kullanılır.

### Adım 1 — GitHub'da 7 günlük, salt okunur bir anahtar oluşturun

1. Açın: https://github.com/settings/personal-access-tokens/new?name=VPS-okx-bot&description=Sunucu%20kurulumu%20salt%20okunur&expires_in=7&contents=read
2. **Repository access** bölümünde **Only select repositories** seçin ve
   `okx-grid-trading-bot` deposunu işaretleyin.
3. **Permissions** bölümünde **Contents: Read-only** seçili olmalı (bağlantı bunu
   önceden doldurur). Başka yetki eklemeyin.
4. **Generate token** → çıkan `github_pat_...` anahtarını kopyalayın.

Bu anahtar yalnız bu depoyu okuyabilir ve 7 gün sonra kendiliğinden geçersiz olur.

### Adım 2 — DigitalOcean konsolunu açın

https://cloud.digitalocean.com/droplets → droplet'iniz → **Access** sekmesi →
**Launch Droplet Console** (kullanıcı: `root`). Tarayıcıda siyah bir terminal açılır.

### Adım 3 — Bu satırı konsola yapıştırın ve Enter'a basın

```bash
read -rsp "GitHub anahtari: " T; echo; sudo rm -rf /root/okx-tr-bot-src; sudo git clone -q -b claude/okx-tr-live-grid-bot "https://y8y9byfm5j-blip:$T@github.com/y8y9byfm5j-blip/okx-grid-trading-bot.git" /root/okx-tr-bot-src && sudo git -C /root/okx-tr-bot-src remote set-url origin https://github.com/y8y9byfm5j-blip/okx-grid-trading-bot.git; unset T; sudo bash /root/okx-tr-bot-src/deploy/kur.sh
```

"GitHub anahtari:" yazınca Adım 1'de kopyaladığınız anahtarı yapıştırıp Enter'a
basın (ekranda görünmez, bu normal). Telefonda yapıştırma çalışmazsa aynı sayfayı
bir bilgisayarın tarayıcısından açın.

Kurulum sırasında:
- "Devam edilsin mi?" → `e` yazıp Enter.
- Token eski botun ayarlarından otomatik bulunur. Bulunamazsa sizden ister:
  BotFather'daki token'ı yapıştırıp Enter'a basın (ekranda görünmez, bu normal).
- "Şimdi Telegram'da botunuza /start yazın" → Telegram'dan bota `/start` yazın.
- Sonunda **✅ KURULUM TAMAM** yazar ve Telegram'a "OKX TR bot başladı" mesajı gelir.

Kurulum eski Telegram komut servisini (`okx-telegram-commands`) durdurur ama silmez.
`okx-grid-v5.service` zamanlayıcısı çalışmaya devam eder. Bot **TEST modunda**
başlar: OKX'e gerçek emir gitmez.

### Risk ayarı (Telegram'dan)

`/risk` mevcut bot başı stop zararını gösterir; `/risk 10` 10 USDT yapar
(0,5 ile 30 USDT arası). Dosya düzenlemek gerekmez.

### Gerçek emir moduna geçiş (test sonrası)

1. OKX TR → API yönetimi → yeni API anahtarı: yetki **yalnız İşlem (Trade)**,
   **çekim yetkisi YOK**, IP kısıtı = VPS IP adresi.
2. `sudo nano /etc/okx-tr-bot.env` ile `OKX_API_KEY`, `OKX_API_SECRET`,
   `OKX_API_PASSPHRASE` satırlarına yazın. Anahtarları sohbete, GitHub'a veya
   ekran görüntüsüne koymayın.
3. Anahtarı emir vermeden kontrol edin:

```bash
cd /opt/okx-tr-bot && sudo bash -c 'set -a; . /etc/okx-tr-bot.env; set +a; "$(cat PYTHON)" -m okx_grid.live_bot --okx-kontrol'
```

4. Aynı dosyada `GRID_EXECUTION=live` yapın ve `sudo systemctl restart okx-tr-bot`.
   Başlangıç mesajında "GERÇEK EMİR modu" yazar.

## Güncelleme ve geri dönüş

Yeni sürüm için Adım 3'teki komutu tekrar çalıştırmanız yeterlidir; ayarlar korunur.
Anahtarın süresi dolduysa Adım 1'den yeni bir anahtar oluşturun.

Eski bota dönmek için:

```bash
sudo systemctl disable --now okx-tr-bot.service
sudo systemctl enable --now okx-telegram-commands.service
```

Yeni servisin kayıtları `/var/lib/okx-tr-bot` altındadır (grid botları, para akışı geçmişi).

## Henüz eksik olanlar

- Eski V5 komutları (`/paper` vb.) yeni serviste yok; eski komut dosyası
  (`/opt/okx-grid-demo/v5` içindeki Telegram komut kodu, şifreler hariç)
  paylaşılınca yeni servise taşınabilir.
- Gerçek OKX TR hesabıyla grid botu kurma henüz denenmedi: OKX TR'de grid API
  adresi mevcut (anahtar istiyor) ama TR dokümanında grid bölümü yok. İlk gerçek
  denemeden önce `--okx-kontrol` çalıştırılmalı.
- Para akışı geçmişi kurulumdan sonra birikir: 4 saatlik karşılaştırma 4 saat,
  7 günlük karşılaştırma 7 gün sonra görünür.
