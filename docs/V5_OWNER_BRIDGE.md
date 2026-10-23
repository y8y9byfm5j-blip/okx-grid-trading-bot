# Gözlenen V5 Telegram döngüsü için staging bağlantısı

Durum: **IMPLEMENTED / SYNTHETIC_TESTED; ACTIVATED=false; TELEGRAM_VERIFIED=false**.
`V5OwnerBridge` ve `V5OwnerAdapter` yalnız mevcut owner tarafından çağrılır.
Telegram istemcisi, poller veya scheduler başlatmaz. Adaptör yalnız açıkça
verilen mevcut `send(text)` callback'ini çağırabilir; kendi sender/token'ı
yoktur. Üretim modüllerini import etmez; V5 dosyalarını/offset'ini/defterini
okumaz veya yazmaz. Yeni 600 USDT defterine ayrı özel yol gerekir.

## Kaynaktan gerçekten görülen bölüm

22 Ekim kullanıcı terminal görüntülerinde `okx-telegram-commands.service`
active, çalışma dizini `/opt/okx-grid-demo/v5`, çalışan Python executable'ı
`/usr/bin/python3.12` olarak görüldü. `run_v5.sh` ve
`telegram_commands_v5.py` mevcut. `docker` komutu bulunamadı; bu tek başına
container veya venv bulunmadığını kanıtlamaz. Kaynakların tam kopyası ve
sunucuya doğrudan erişim bu çalışma ortamında yoktur.

Maskelenmiş AST çıktısındaki kesin davranışlar:

| Fonksiyon | Gözlenen davranış | Bilinmeyen bölüm |
| --- | --- | --- |
| `find_plan(symbol)` | Uppercase, USDT son ekini çıkarma, `-USDT` hedefi ve `load_plans()` içinde arama | `replace` değeri ve plan alanının anahtarı maskeli |
| `handle(text)` | İlk token lowercase; `/analiz` ve `/grid` önce `refresh_v5()`, sonra `find_plan()`, sonra aynı `analysis_card()` | Yardım/hata metinleri ve gizli literal'ler maskeli |
| `main()` | `getUpdates`, her update için offset'i işleme öncesi kaydetme, chat ACL, `handle(text)`, `send(response)` | Tam kaynak/hash ve üretim bağlantısı görülmedi |
| `api(method, data=None)` | URL-encoded gövde, gövde varsa POST, `urlopen(timeout=30)`, `json.load` dönüşü | URL literal'leri/token maskeli; uygulanan origin doğrulanmadı |
| `send(text)` | `return api('sendMessage', {'chat_id': ALLOWED_CHAT, 'text': text})` | Gerçek teslim/ACL sabitleri doğrulanmadı |
| `load_offset()` / `save_offset(offset)` | Okuma hatasında 0; düz `Path.write_text` | Dosya yolu/değeri maskeli; atomik replacement yok |
| `refresh_v5()` / `load_plans()` | subprocess başarı kodu; JSON `plans` listesi, dosya yoksa boş liste | subprocess argümanları/timeout, dosya yolu ve plan alanları maskeli |

Son altı gövde 23 Ekim kullanıcı ekranlarında görüldü; önceki "send dönüşü
bilinmiyor" engeli kalktı. Telegram servisi active/running; grid timer'ı ve
yaklaşık 15 dakikalık başarılı koşular görüldü. Başarıyla bitip inactive
olmak timer/oneshot akışında arıza kanıtı değildir; gerçek `Type` okunmadı.
Telegram loglarındaki DNS/ağ/502/timeout geçmiş kayıtlardır; güncel bağlantı
kesintisi veya OKX kök nedeni olarak sunulmaz.

Son bağlantı testinde "OKX TR" etiketiyle `www.okx.com` çağrılmış ve 403
alınmış. Bu, `tr.okx.com` bağlantısının testi değildir. Telegram kök URL'sinin
200 HTML dönmesi de token'lı getUpdates/sendMessage kabulü değildir.

Bu V5 `/GRID` büyük harfini zaten tanır. SUI'nin plan listesinde bulunmaması
olası ayrı nedendir; katalog/plan şeması görülmeden kök neden kesinleştirilmez.
Yeni komutlar `/paraakisi`, `/momentum`, `/gridplan`, `/papergrid`,
`/emergency_stop` gösterilen handle'da yoktur. Son fallback içeriği maskelidir.
Gösterilen main yalnız chat kimliğini kontrol eder; sender kimliği, private
chat ve forwarded/edit güvenlik kontrolleri görünmez. Komut sonucu oluşmadan
offset kaydı, aradaki crash'te komut/yanıt kaybı penceresi yaratır.
Ham exception metninin kullanıcıya aktarılması da gösterilen akışta mevcuttur.

## Staging sözleşmesi

1. Tek mevcut owner, gerçek Telegram update'ini **tam olarak**, içindeki
   `message.from`, chat türü, date ve güvenlik bayraklarını koruyarak verir.
   Metinden kimlik uydurulmaz; güvenlik bayrakları normalizasyonla silinmez.
   Ortak ACL yalnız atanmış özel chat + kullanıcı + insan gönderici kabul eder;
   forwarded/edited/via_bot ve eksik kimlik reddedilir. Unauthorized update için
   yanıt veya piyasa GET'i yoktur.
2. `initial_offset` yalnız ilk kurulumda açıkça verilen ve yerelde uzlaştırılan
   başlangıçtır. Buradan eski dosya okunmaz. Policy kimliği pin'lenir; sonraki
   açılışta eski offset'in daha ileri olması `UNRECONCILED_LEGACY_OFFSET`
   üretir. Defter cursor'ı sessizce atlatılmaz.
3. `prepare(update, now=...)` ortak registry/core'u çağırır. Komut sonucu,
   outbox ve `next_offset` bridge transaction'ında birlikte kalıcılaşır.
   İkinci poller/erken `save_offset()` bu akışa eklenmez. Core kabulünden sonra
   crash ve sonucu bilinmeyen replay yeni eylem üretmez; açık UNKNOWN yanıtı
   ve pause üretir. Storage başarısızsa offset commit edilmez.
4. `next_offset()` mevcut owner'ın sonraki GET parametresidir. Yeniden
   başlatmada `pending_deliveries()` yalnız bridge'e ait henüz gönderim
   girişimi yapılmamış yanıtları döndürür; böylece offset ilerlese bile yanıt
   unutulmaz. UNKNOWN yanıtlar bu listede yer almaz.
5. `begin_delivery(update_id, now=...)` **ağ çağrısından önce** UNKNOWN
   kaydeder. Yalnız `send_allowed=True` ise mevcut owner bir gönderim deneyebilir.
   ACK/UNKNOWN için otomatik yeniden gönderim izni vermez. Belirsizlikte yeni
   alımlar core kapısı üzerinden engellenir; bağlantı geri gelmesi resume değildir.
6. `record_delivery(update_id, actual_api_json, now=...)` yalnız `ok=true`,
   pozitif `message_id`, tam hedef chat ve aynı metinle ACK kabul eder. `None`,
   yanlış chat/metin veya belirsiz cevap UNKNOWN kalır ve alımları kalıcı pause
   eder. Raw API cevabı/hata/token saklanmaz. Bu yerel doğrulama internetten
   mesaj göndermez; sentetik JSON gerçek Telegram kanıtı sayılamaz.

`/help` ve `/start` staging yardımını döndürür. `/paper` ise mevcut V5
`paper_card` ve eski defter sözleşmesi görülene kadar açık
`LEGACY_PAPER_ROUTE_NOT_BOUND` üretir; eski defter yerine sessizce yeni
600 USDT defterini göstermez. Üretimdeki `/paper` değiştirilmedi.
GRID/analiz doğrudan yeni keşif/capture/ortak karar yoluna gider; eski radar
aday listesinde bulunmak ön koşul değildir. Türkçe klavyedeki `ı`/`İ` yalnız
komut girdisinde ASCII I'ya eşlenir; diğer Unicode benzerleri reddedilir.

Bu modül tek owner sözleşmesiyle kullanılır; servisler arası leader lease veya
tek poller kilidi kanıtı değildir. Ayrı yeni ledger korunur; scheduler yalnız
ayrıca doğrulanan public capture ve bildirim sağlığıyla çalıştırılabilir.
Gerçek emir istemcisi yoktur; `LIVE_TRADING=false` zorunludur.

## Otomatik kabul ve kalan kapılar

`tests/test_v5_owner_bridge.py` sentetik native update metadata'sı,
SUI/parite/Türkçe klavye, ACL, unknown komut, replay/conflict, storage rollback,
crash-after-accept, sanitized hata, offset, restart, PENDING kurtarma,
UNKNOWN/ACK, aynı saniyede acil durdurma ve kalıcı halt'ı kapsar. Önceki core,
paper ve risk testleri de korunur. Piyasaya, Telegram'a veya VPS'e bağlanmaz.

`tests/test_v7_owner_acceptance.py` birleşik staging kabulünü de kapsar:
native `/gridplan SOL 300`, `/gridplan Suı/USDT 300`, `/papergrid start`,
iki ayrı rezerv, komut/çevrim replay, kısmi dolum, defter+offset restart'ı,
`/emergency_stop`, koruyucu gap çıkışı ve kalıcı halt. İkinci senaryoda gap
sonrası minimum tutarın altında kalan envanter kapanmış sayılmaz; iki botun
600 USDT rezervi korunur. UNKNOWN teslim ve public kesinti iki girişi de
engeller; veri bağlantısının düzelmesi otomatik resume değildir.

Bu testte fiyatlar, ACK JSON'ları ve bildirim sağlığı **sentetiktir**;
çevrim açıkça `SYNTHETIC_TEST` döndürür. Gerçek Telegram, canlı veride iki
pozisyon kabulü veya üretim scheduler bağlantısı olarak sunulmaz.

Durum ekranı en son kalıcı çevrim receipt'inin veri hatalarını okur. Daha
sonraki başarılı tek parite GET'i önceki çevrim hatasını örtemez; API geri
gelmesi veya yalnız BTC start kontrolü tam çevrim mutabakatının yerine geçmez.
Hatasız yeni çevrim veri arızasını temizleyebilir fakat pause'u otomatik açmaz.
Yeni state şeması/reset veya eski defter dönüşümü gerekmez.

Sonraki gerekli kaynaklar: maskeli alanların özel ortamda incelenmesi,
kanonik V5 modüllerinin tam kaynak/hash ve arayüz/durum sözleşmeleri; tam
servis/timer tanımları, bağımlılıklar ve aktif commit. Yukarıda görülen altı
gövde yeniden eksik sayılmaz. Token, .env, SSH private key ve
gerçek özel defter/log paylaşımı gerekmez. Maskeli alanlar tahmin edilmez.

Bu kanıtlar gelmeden üretime uygulanacak replacement `main()` hazırlanmaz.
Tam inceleme sonrası mevcut owner'a küçük bağlantı, aynı capture shadow,
gerçek Telegram e2e, ayrı staging ve geri dönüş provası gerekir. Üretim
dosyası/DB/servis değişikliği ve restart ayrıca açık dağıtım onayı bekler.

## Mevcut API/send için çağrılabilir adaptör

`V5OwnerAdapter.poll_parameters()` yalnız mevcut owner'a parametre verir;
ağ çağrısı yapmaz. Gözlenen 30 saniyelik HTTP timeout için varsayılan
long-poll 20 saniyedir. En az 10 saniye fark yerel politika tercihidir;
geçmiş timeout'ların kök nedeni olarak kanıtlanmadı. Negatif offset yoktur.

`consume_updates(actual_getupdates_json)` bütün zarfı önce doğrular:
`ok=true`, en fazla 100 update, artan/eşsiz integer kimlikler. Bozuk son
satır önceki komutun çalışmasına veya offset atlanmasına yol açmaz.
Metadata/ACL bayrakları korunur. Sonuç+outbox kalıcılaşınca cursor ilerler;
legacy `save_offset` çağrılmaz. Batch ortasında storage hatası olursa daha
önce tamamlanan kayıtlar korunur, yarım kalan kayıt ACK'lenmez.

`deliver_pending(existing_send)` yalnız yeni V7 yanıtlarını ve çevrim
bildirimlerini mevcut callback'e geçirir. Ağdan önce UNKNOWN yazılır;
gerçek JSON aynı chat/metin/message_id ile eşleşirse ACK olur. Hata metni
ve API gövdesi saklanmaz. Timeout/crash sonrası UNKNOWN yeniden gönderilmez.
Restart'ta henüz denenmemiş PENDING yanıtlar kurtarılır. V5/V6 bildirim
kuyrukları devralınmaz.

`poll_failed()` kalıcı pause ve 1/2/4/8/16/30 saniye sınırlı bekleme önerisi
üretir; uyuma/bağlanma döngüsü başlatmaz. İyi poll pause'u açmaz.
`notification_ready()` son 30 saniyede poll+ACK ve boş outbox ister;
saat geri sıçraması, PENDING ve UNKNOWN'da false döner. Poll yaşı zarfın
alındığı andan ölçülür; uzun analiz bitince eski zarf tazelenmiş sayılmaz. Bu yerel kapı
sentetik callback ile test edilebilir; gerçek Telegram veya aktivasyon
kanıtı değildir. Scheduler ancak gerçek transport/staging kabulünden sonra
bu sonucu `run_cycle(notification_healthy=...)` için kullanabilir.

İncelenmiş owner'daki sıra: restart PENDING kurtarma → mevcut owner'ın tek
getUpdates çağrısı (`poll_parameters`) → `consume_updates` →
`deliver_pending(send)`. Hata yolunda `poll_failed`; tekrar deneme zamanını
mevcut owner belirler. Bu, üretime yapıştırılacak replacement main değildir;
legacy `/paper` ve tek owner sözleşmeleri hâlâ inceleme bekler. Batch işleme
senkrondur; gerçek komut gecikmesi ve yoğun kuyrukta emergency-stop kabulü
staging'de ölçülmelidir.

Mevcut yaklaşık 15 dakikalık grid timer'ı V7'nin 30 saniyelik veri/bildirim
kapısına uygun sürekli yürütme olarak varsayılmaz. Uygun scheduler sıklığı,
tek owner ve heartbeat düzeni üretim kaynaklarıyla ayrıca doğrulanmalıdır.

`tests/test_v5_owner_adapter.py` gerçek biçimli fakat sentetik API/ACK ile
batch, ACL, SUI, replay, timeout/crash, disk hatası, restart, poll pause,
heartbeat yaşı ve iki 300 USDT rezervin ACK kapısına bağlanmasını doğrular.
Hiçbir test gerçek Telegram mesajı göndermez.
