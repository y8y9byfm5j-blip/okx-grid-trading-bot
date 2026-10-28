# OKX TR Telegram botu — sunucusuz (GitHub üzerinde)

> **Not:** GitHub'ın zamanlayıcısı güvenilir değil (saatlerce çalışmayabilir). Sürekli çalışan kurulum için
> [LIVE_BOT.md](LIVE_BOT.md) kullanılır. Sunucudaki bot başladıktan sonra bu iş akışı kapatılmalıdır, çünkü iki program aynı
> Telegram botunu okuyamaz.

Konsol ve sunucu gerekmez. GitHub'ın ücretsiz çalıştırıcısı (Actions) botu her
5 dakikada bir başlatır; her tur yaklaşık 4 dakika çalışır, Telegram komutlarına
cevap verir ve zamanı gelen işleri yapar:

- `/rotasyon`: ana para takibi (hangi coinden çıkıp hangisine gidiyor)
- `/erkenlong`: büyük yükseliş öncesi birikim adayları, uyarı olarak da gelir
- `/analiz COIN`, `/grid COIN`, `/radar`: grid uygunluğu ve plan
- `/botlar`, `/durum`, `/risk`, `/otomatik`, `/acil_durdur`: TEST (paper) grid botları

Komutlara cevap genelde 1 dakika içinde, en kötü durumda birkaç dakika içinde gelir.

## Bilmeniz gerekenler

- **Yalnız TEST modu.** Bu çalıştırıcıdan OKX'e gerçek emir gönderilmez, API anahtarı
  da girilmez; kod bunu kendisi zorunlu kılar. Gerçek emir için sunucu kurulumu gerekir
  ([LIVE_BOT.md](LIVE_BOT.md)).
- **GitHub kuralları.** GitHub'ın Actions şartları, çalıştırıcının depodaki yazılımın
  üretimi/testi dışındaki işler için kullanılmamasını ister. Telegram botu bu kapsamın
  kenarındadır; GitHub bu çalışmayı sınırlayabilir. Bu yol kalıcı çözüm değil, sunucu
  kurulumuna kadar köprüdür.
- **Gizlilik.** Depo herkese açıktır ve çalıştırma kayıtları (log) herkese görünür.
  Kayıtlara token, sohbet kimliği veya işlem ayrıntısı yazılmaz. Token ve kimlik GitHub
  "secret" olarak saklanır; kayıtlarda otomatik maskelenir. Bot durumu özel bir önbellekte
  tutulur.
- Eski bot (V5) aynı token'ı kullanıyorsa çakışır. Bu yüzden **yeni bir bot** açın.

## Kurulum (telefondan, 4 adım)

### 1. Yeni bot oluşturun (Telegram)

1. Telegram'da `@BotFather` ile sohbet açın, `/newbot` yazın.
2. Bir ad ve `bot` ile biten bir kullanıcı adı verin (ör. `okxtr_radar_bot`).
3. BotFather size bir **token** gönderir (`123456789:AA...`). Mesaja uzun basıp
   **Kopyala**'yı seçin.

### 2. Token'ı GitHub'a secret olarak ekleyin

1. Açın: https://github.com/y8y9byfm5j-blip/okx-grid-trading-bot/settings/secrets/actions/new
2. **Name:** `TELEGRAM_BOT_TOKEN`
3. **Secret:** kopyaladığınız token'ı yapıştırın → **Add secret**.

### 3. Botu başlatın

1. Açın: https://github.com/y8y9byfm5j-blip/okx-grid-trading-bot/actions/workflows/telegram-bot.yml
2. **Run workflow** → **Run workflow** düğmesine basın (yeşil).
3. Telegram'da yeni botunuzu açın ve `/start` yazın. Birkaç dakika içinde bot size
   **sohbet kimliğinizi** yazar.

### 4. Sohbet kimliğini ekleyin

Adım 2'deki sayfadan ikinci bir secret ekleyin:
**Name:** `TELEGRAM_ALLOWED_CHAT_IDS`, **Secret:** botun yazdığı sayı → **Add secret**.

Bir sonraki turdan itibaren bot yalnız sizin sohbetinize cevap verir. `/yardim` yazın.

## Sorun giderme

- **Bot cevap vermiyor:** Actions sekmesinde son çalıştırmaya bakın. Kırmızıysa
  ekran görüntüsünü gönderin. Yeşilse birkaç dakika bekleyin.
- **Çalıştırmalar durdu:** GitHub, 60 gün etkinlik olmayan depolarda zamanlanmış
  çalıştırmaları durdurur. Actions sekmesinden **Enable workflow** ile açın.
- **Bot durumunu sıfırlamak:** Actions → Caches bölümünden `tgbot-state-` önbelleklerini silin.
