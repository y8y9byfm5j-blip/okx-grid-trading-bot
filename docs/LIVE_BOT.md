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

## Kurulum (Termius ile VPS'te)

### 1. Kodu sunucuya indirin

Depo özel olduğu için sunucuya salt okunur bir anahtar verin:

```bash
sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/okx_tr_bot_deploy
sudo cat /root/.ssh/okx_tr_bot_deploy.pub
```

Çıkan satırı GitHub → depo → **Settings → Deploy keys → Add deploy key** ekranına
yapıştırın. "Allow write access" **kapalı** kalsın. Sonra:

```bash
sudo GIT_SSH_COMMAND="ssh -i /root/.ssh/okx_tr_bot_deploy -o StrictHostKeyChecking=accept-new" \
  git clone -b claude/okx-tr-live-grid-bot git@github.com:y8y9byfm5j-blip/okx-grid-trading-bot.git /root/okx-grid-trading-bot
cd /root/okx-grid-trading-bot
sudo bash deploy/install.sh
```

### 2. Token'ı girin

```bash
sudo nano /etc/okx-tr-bot.env
```

`TELEGRAM_BOT_TOKEN` satırına mevcut botun token'ını yazın (BotFather'daki ile
aynı). `GRID_EXECUTION=paper` olarak kalsın (test modu, OKX'e emir gitmez).

### 3. Eski komut servisini durdurun ve sohbet kimliğinizi bulun

Bir Telegram token'ını aynı anda yalnız bir program okuyabilir. Eski komut servisi
durdurulur ama silinmez:

```bash
sudo systemctl stop okx-telegram-commands.service
sudo systemctl disable okx-telegram-commands.service
```

Telegram'dan bota `/start` yazın, sonra:

```bash
cd /opt/okx-tr-bot && sudo bash -c 'set -a; . /etc/okx-tr-bot.env; set +a; python3 -m okx_grid.live_bot --kimlik'
```

Çıkan `chat_id` değerini `/etc/okx-tr-bot.env` içinde `TELEGRAM_ALLOWED_CHAT_IDS`
satırına yazın. Bot yalnız bu sohbetten gelen komutlara cevap verir.

### 4. Yeni servisi başlatın

```bash
sudo systemctl enable --now okx-tr-bot.service
sudo journalctl -u okx-tr-bot -f
```

Telegram'da "✅ OKX TR bot başladı" mesajı gelir. `/yardim` yazın.
`okx-grid-v5.service` zamanlayıcısı çalışmaya devam edebilir. Kayıtlarda
`TELEGRAM_ERROR_409` görürseniz başka bir program da aynı token'ı okuyordur.

### 5. Gerçek emir moduna geçiş (test sonrası)

1. OKX TR → API yönetimi → yeni API anahtarı: yetki **yalnız İşlem (Trade)**,
   **çekim yetkisi YOK**, IP kısıtı = VPS IP adresi.
2. `/etc/okx-tr-bot.env` içine `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE`
   yazın. Anahtarları sohbete, GitHub'a veya ekran görüntüsüne koymayın.
3. Anahtarı emir vermeden kontrol edin:

```bash
cd /opt/okx-tr-bot && sudo bash -c 'set -a; . /etc/okx-tr-bot.env; set +a; python3 -m okx_grid.live_bot --okx-kontrol'
```

4. `GRID_EXECUTION=live` yapın ve `sudo systemctl restart okx-tr-bot`.
   Başlangıç mesajında "GERÇEK EMİR modu" yazar.

## Güncelleme ve geri dönüş

```bash
cd /root/okx-grid-trading-bot && sudo GIT_SSH_COMMAND="ssh -i /root/.ssh/okx_tr_bot_deploy" git pull
sudo bash deploy/install.sh && sudo systemctl restart okx-tr-bot
```

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
