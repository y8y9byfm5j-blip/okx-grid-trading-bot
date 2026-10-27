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

## Kurulum (Termius ile VPS'te) — 3 adım

Termius'ta sunucuya bağlanın. Aşağıdaki kutuları sırayla kopyalayıp yapıştırın.

### Adım 1 — Sunucuya GitHub okuma anahtarı (bir kez)

```bash
sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/okx_tr_bot_deploy -q; sudo cat /root/.ssh/okx_tr_bot_deploy.pub
```

Ekranda `ssh-ed25519 AAAA...` ile başlayan tek bir satır çıkar. Bu satırın tamamını
kopyalayın. (Dosya zaten var diye sorarsa `n` yazıp Enter'a basın.)

### Adım 2 — Anahtarı GitHub'a ekleyin (telefondan)

1. Açın: https://github.com/y8y9byfm5j-blip/okx-grid-trading-bot/settings/keys/new
2. **Title:** `VPS`
3. **Key:** kopyaladığınız `ssh-ed25519 ...` satırı
4. **Allow write access** kutusu **boş** kalsın → **Add key**

Bu anahtar sunucunun yalnız bu depoyu okumasına izin verir.

### Adım 3 — Kur ve başlat (Termius)

```bash
sudo rm -rf /root/okx-tr-bot-src; sudo env GIT_SSH_COMMAND="ssh -i /root/.ssh/okx_tr_bot_deploy -o StrictHostKeyChecking=accept-new" git clone -q -b claude/okx-tr-live-grid-bot git@github.com:y8y9byfm5j-blip/okx-grid-trading-bot.git /root/okx-tr-bot-src && sudo bash /root/okx-tr-bot-src/deploy/kur.sh
```

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
