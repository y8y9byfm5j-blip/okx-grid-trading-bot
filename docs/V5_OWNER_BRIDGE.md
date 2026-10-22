# Gözlenen V5 Telegram döngüsü için staging bağlantısı

Durum: **IMPLEMENTED / SYNTHETIC_TESTED; ACTIVATED=false; TELEGRAM_VERIFIED=false**.
`okx_grid.v5_owner_bridge.V5OwnerBridge` yalnız mevcut owner tarafından
çağrılacak bir sınırdır. Telegram istemcisi, poller, sender veya scheduler
başlatmaz. Üretim modüllerini import etmez; V5 dosyalarını/offset'ini/defterini
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
| `main()` | `getUpdates`, her update için offset'i işleme öncesi kaydetme, chat ACL, `handle(text)`, `send(response)` | API/sender dönüşü ve dosya atomikliği görülmedi |

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

Sonraki gerekli kaynaklar: `api(method,data)`, `send(text)`, `load_offset()`,
`save_offset(offset)`, `refresh_v5()`, `load_plans()` gövdeleri; sekiz diğer
kanonik V5 modülünün arayüz/durum sözleşmeleri; gerçek servis ExecStart,
scheduler, bağımlılıklar ve aktif commit. Token, .env, SSH private key ve
gerçek özel defter/log paylaşımı gerekmez. Maskeli alanlar tahmin edilmez.

Bu kanıtlar gelmeden üretime uygulanacak replacement `main()` hazırlanmaz.
Tam inceleme sonrası mevcut owner'a küçük bağlantı, aynı capture shadow,
gerçek Telegram e2e, ayrı staging ve geri dönüş provası gerekir. Üretim
dosyası/DB/servis değişikliği ve restart ayrıca açık dağıtım onayı bekler.
