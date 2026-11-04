"""Turkish Telegram texts for the live service. Plain text, no parse mode."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import html
import math

TR_TIME = timezone(timedelta(hours=3))  # Turkey has no DST
REASONS = {
    "HALTED": "otomasyon durduruldu", "UNRESOLVED_OKX_REQUEST": "OKX'e giden bir istek henüz doğrulanmadı",
    "PAIR_ALREADY_ACTIVE": "bu paritede zaten bot var", "MAX_BOTS_REACHED": "en fazla bot sayısına ulaşıldı",
    "TOTAL_CAPITAL_LIMIT": "toplam sermaye sınırı", "REALIZED_LOSS_LIMIT": "toplam zarar sınırına ulaşıldı",
    "PAIR_COOLDOWN": "bu paritede yakın zamanda bot kapandı (bekleme süresi)",
    "DOWNTREND": "düşüş trendi", "EXCESSIVE_SHORT_MOMENTUM": "15 dk'da aşırı hızlı yükseliş",
    "BTC_RISK_OFF": "BTC sert düşüşte", "LOW_QUOTE_VOLUME": "24s hacim 10M USDT altında",
    "WIDE_SPREAD": "spread geniş", "INSUFFICIENT_DEPTH": "emir defteri derinliği yetersiz",
    "BOT_RISK_LIMIT": "stop zararı bot başı risk sınırını aşıyor", "ENTRY_OUTSIDE_GRID": "fiyat grid aralığı dışında",
    "OKX_GRID_STEP_TOO_SMALL": "aralık çok dar, OKX grid adımı için yetersiz",
    "INSUFFICIENT_NET_GRID_RETURN": "komisyon sonrası grid kârı yetersiz", "DATA_UNVERIFIED": "veri doğrulanamadı",
    "INSUFFICIENT_USDT_BALANCE": "OKX TR hesabında yeterli USDT yok", "DECISION_NOT_ELIGIBLE": "kriterler sağlanmadı",
    "RISK_NOT_CALCULABLE": "risk hesaplanamadı", "STALE_MARKET": "veri eski", "UNKNOWN_SYMBOL": "parite OKX TR'de yok",
    "GRID_NOT_PROFITABLE_AFTER_FEES": "komisyon sonrası kâr yok",
    "PRIVATE_OUTCOME_UNKNOWN": "OKX yanıtı alınamadı; birkaç dakika sonra /botlar ile kontrol edin",
    "COMMAND_ARGUMENTS_REQUIRED": "coin adı yazın, ör. /analiz SOL", "INVALID_SPOT_SYMBOL": "geçersiz coin adı",
    "INVALID_RISK_VALUE": "geçersiz risk değeri (ör. /risk 10)",
    "INVALID_TELEGRAM_ALLOWED_CHAT_IDS": "TELEGRAM_ALLOWED_CHAT_IDS içinde sohbet kimliği bulunamadı: yalnız sayıyı yazın (ör. 123456789)",
    "INVALID_TELEGRAM_ALLOWED_USER_IDS": "TELEGRAM_ALLOWED_USER_IDS içinde sayı bulunamadı",
    "TELEGRAM_BAD_AUTH_FORMAT": "TELEGRAM_BOT_TOKEN biçimi geçersiz: yalnız BotFather'ın verdiği kodu yapıştırın",
    "TELEGRAM_ERROR_401": "Telegram token'ı kabul etmedi (yenilenmiş olabilir): BotFather'dan yeni token alıp GitHub secret'ını güncelleyin",
    "TELEGRAM_ERROR_404": "Telegram bu token'ı tanımıyor: GitHub secret'ını kontrol edin",
    "TELEGRAM_ERROR_409": "aynı bot token'ını başka bir program da okuyor", "NO_RUNNING_BOT": "bu coinde çalışan bot yok", "OKX_ACCESS_NOT_CONFIGURED": "OKX API anahtarı ayarlanmamış", "GRID_BELOW_MIN_NOTIONAL": "emir tutarı minimumun altında",
}
CLOSE = {"STOP_LOSS": "stop-loss", "TAKE_PROFIT": "kâr al", "MANUAL_STOP": "elle durduruldu",
         "EMERGENCY_STOP": "acil durdurma", "RISK_CONTROL": "OKX risk kontrolü", "STOPPED": "durdu"}
NOTE = ("Not: Veriler gerçekleşen işlemlerdir; müşteri bakiyesi veya borsaya para girişi değildir. "
        "tr.okx.com piyasa verisi global OKX defteriyle aynıdır; TRY pariteleri Türk tarafını gösterir.")


def when(ms):
    return datetime.fromtimestamp(ms / 1000, TR_TIME).strftime("%d.%m %H:%M")


def usd(value, signed=True):
    value = Decimal(value)
    sign = ("+" if value > 0 else "−" if value < 0 else "") if signed else ""
    a = abs(value)
    if a >= 1_000_000:
        text = f"{a / 1_000_000:.1f}M$"
    elif a >= 1_000:
        text = f"{a / 1_000:.0f}K$"
    else:
        text = f"{a:.0f}$"
    return sign + text.replace(".", ",")


def pct(value, digits=1):
    if value is None:
        return "?"
    value = float(value) * 100
    return ("+" if value > 0 else "−" if value < 0 else "") + f"%{abs(value):.{digits}f}".replace(".", ",")


def times(value):
    return f"{float(value):.1f}x".replace(".", ",")


def reason(code):
    return REASONS.get(code, code)


def help_text(mode):
    return "\n".join([
        "🤖 OKX TR Bot komutları",
        "/rotasyon — ana para nereden çıkıp nereye gidiyor",
        "/erkenlong — büyük yükseliş öncesi birikim adayları",
        "/gridsinyal (veya /radar) — yükselip yataya geçen, hacmi soğuyan coinler ve grid aralıkları",
        "/ozet — grid özeti (her gün 09:00 ve 18:00'de kendiliğinden gelir)",
        "/grid COIN — o coin için grid sinyali: uygun mu, aralık, grid sayısı",
        "/analiz COIN — /grid + para akışı + erken long skoru",
        "/botlar (veya /paper) — açık/kapanan grid botları ve kâr/zarar",
        "/durum — sistem durumu ve limitler",
        "/otomatik ac | /otomatik kapat — otomatik grid kurulumu",
        "/durdur COIN — o coindeki botu durdur (coin USDT'ye satılır)",
        "/acil_durdur — tüm botları durdur, otomasyonu kapat",
        "/devam — zarar sınırı/acil durdurmadan sonra otomasyonu yeniden aç",
        f"Mod: {'GERÇEK EMİR (OKX TR)' if mode == 'live' else 'TEST (paper) — gerçek emir yok'}",
    ])


def rotation_text(report):
    if not report:
        return "Ana para takibi için ilk veri toplanıyor. 15 dakika içinde tekrar deneyin."
    lines = ["💸 ANA PARA TAKİBİ — OKX TR", f"Veri: {when(report['at_ms'])}"]
    if "inflow" in report:
        lines += ["", f"🟢 PARA GİREN (24 saat, net agresif alış) — {when(report['flows_at_ms'])}"]
        lines += [f"{i}. {c}  {usd(f['net_24h'])}  (7g {usd(f['net_7d'])})"
                  for i, (c, f) in enumerate(report["inflow"], 1)] or ["—"]
        lines += ["", "🔴 PARA ÇIKAN (24 saat, net agresif satış)"]
        lines += [f"{i}. {c}  {usd(f['net_24h'])}  (7g {usd(f['net_7d'])})"
                  for i, (c, f) in enumerate(report["outflow"], 1)] or ["—"]
        if report["inflow"] and report["outflow"]:
            lines += ["", "🔁 Rotasyon: " + ", ".join(c for c, _ in report["outflow"][:3]) + " → "
                      + ", ".join(c for c, _ in report["inflow"][:3])]
        total = report["net_total_24h"]
        lines.append("💵 Stabil coin yönü (24s): " + (f"USDT'den coinlere {usd(total)}" if total >= 0
                     else f"coinlerden USDT'ye {usd(-total)}") + f" | 7g: {usd(report['net_total_7d'])}")
    else:
        lines += ["", "Agresif alış/satış verisi henüz toplanmadı (saatlik)."]
    for label, title in (("24s", "24 saat"), ("7g", "7 gün"), ("4s", "4 saat")):
        block = report["horizons"].get(label)
        if not block:
            continue
        lines += ["", f"📈 Hacim payı artanlar ({title}): " + ", ".join(
            f"{c} {float(d) * 100:+.2f}p" for c, d, _, _ in block["gainers"][:5])]
        if block["losers"]:
            lines.append(f"📉 Hacim payı düşenler ({title}): " + ", ".join(
                f"{c} {float(d) * 100:+.2f}p" for c, d, _, _ in block["losers"][:5]))
        break
    for label, title in (("24s", "24 saat"), ("7g", "7 gün"), ("4s", "4 saat")):
        block = report["try"].get(label)
        if block and block["gainers"]:
            lines += ["", f"🇹🇷 TRY paritelerinde payı artanlar ({title}): " + ", ".join(
                f"{c} {float(d) * 100:+.2f}p" for c, d, _, _ in block["gainers"][:5])]
            break
    if not report["horizons"]:
        lines += ["", "Hacim payı karşılaştırması için geçmiş veri birikiyor (en az 4 saat)."]
    lines += ["", NOTE]
    return "\n".join(lines)


def early_text(scan, top=10):
    if not scan:
        return "Erken long taraması henüz tamamlanmadı (ilk tarama ~1-2 dakika sürer)."
    picks = [r for r in scan["results"] if r["label"] in {"GÜÇLÜ", "İZLE"}][:top]
    lines = ["🚀 ERKEN LONG ADAYLARI (günlük + haftalık)",
             f"Taranan: {scan['scanned']} coin | {when(scan['at_ms'])}", ""]
    if not picks:
        lines.append("Şu an kriterleri sağlayan aday yok. BEKLE.")
    for i, r in enumerate(picks, 1):
        lines.append(f"{i}. {r['instrument']}  skor {r['score']} {r['label']} | 7g {pct(r['ret_7d'])} "
                     f"30g {pct(r['ret_30d'], 0)} hacim {times(r['volume_ratio'])}")
        lines += ["   • " + text for text in r["reasons"][:4]]
    lines += ["", "Skor bir izleme listesidir; yükselişi garanti etmez. Geç kalınmış (çok yükselmiş) coinler elenir."]
    return "\n".join(lines)


def num(value):
    return f"{float(value):.6g}".replace(".", ",")


class Html(str):
    """A message in Telegram's HTML mode, so tables arrive in an aligned monospace block."""


def esc(text):
    return html.escape(str(text), quote=False)


def price(value):
    """Grid entry price with 5 significant digits, never in exponent form."""
    value = float(value)
    digits = max(0, 4 - math.floor(math.log10(value))) if value > 0 else 2
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def share(value):
    return f"%{value * 100:.2f}".replace(".", ",")


def table(headers, rows, left=(1,)):
    """Monospace table; columns in `left` are left aligned, the rest right aligned (numbers)."""
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    lines = [" ".join(c.ljust(w) if i in left else c.rjust(w) for i, (c, w) in enumerate(zip(line, widths))).rstrip()
             for line in [headers, *rows]]
    return "<pre>" + esc("\n".join(lines)) + "</pre>"


def entry_tables(picks):
    """Two short tables (fit a phone screen): grid settings, then entry and exit prices. Best score first."""
    setup, entry = [], []
    for i, r in enumerate(picks, 1):
        p, coin = r["plan"], r["instrument"].split("-")[0]
        setup.append([str(i), coin, price(p["lower"]), price(p["upper"]), str(p["grids"]), share(p["step_pct"])])
        entry.append([str(i), coin, price(r["price"]), price(p["stop"]), price(p["take_profit"]), str(r["score"])])
    return ["<b>Grid kurulumu</b> (OKX Spot Grid, aritmetik, USDT):",
            table(["#", "Coin", "Alt", "Üst", "Grid", "Adım"], setup),
            "<b>Giriş ve çıkış:</b>",
            table(["#", "Coin", "Şimdi", "Stop", "Kâr al", "Skor"], entry)]


def entry_card(row):
    p = row["plan"]
    rows = [["Coin", row["instrument"]], ["Alt fiyat", price(p["lower"])], ["Üst fiyat", price(p["upper"])],
            ["Grid sayısı", f"{p['grids']} (aritmetik)"],
            ["Grid adımı", f"{share(p['step_pct'])} (net {share(p['step_net_pct'])})"],
            ["Şimdi", price(row["price"])], ["Stop", price(p["stop"])], ["Kâr al", price(p["take_profit"])],
            ["Tahmini/gün", f"≈ {share(p['est_daily_pct'])}"], ["Skor", f"{row['score']} {row['label']}"]]
    return table(["Ayar", "Değer"], rows, left=(0, 1))


def range_text(scan, top=8):
    if not scan:
        return "Grid sinyal taraması henüz tamamlanmadı (ilk tarama birkaç dakika sürer)."
    picks = [r for r in scan["results"] if r["label"] in {"GÜÇLÜ", "İZLE"}][:top]
    lines = ["📐 <b>GRID SİNYALLERİ</b> — yükseldi, yataya geçti, hacim soğuyor",
             esc(f"Taranan: {scan['scanned']} coin | {when(scan['at_ms'])}"), ""]
    if picks:
        lines += entry_tables(picks)
    else:
        lines.append("Şu an kriterleri sağlayan coin yok. BEKLE.")
        lines += _near_lines(scan)
    lines += ["", esc("Kriterler: öncesinde ≥%20 yükseliş, trend sağlam, %4-20 yatay bant, hacim yükselişten düşük "
                      "ama canlı, OBV düşmüyor. Coin ayrıntısı: /grid COIN")]
    return Html("\n".join(lines))


def _near_lines(scan):
    near = scan.get("near") or []
    if not near:
        return []
    return ["🔎 Kritere en yakın:", *(esc(f"• {r['instrument']} {r['passed']}/8 kural — {r['reasons'][0]}") for r in near)]


def range_alert(row, auto=False):
    lines = [f"📐 <b>GRID SİNYALİ: {esc(row['instrument'])}</b> — skor {row['score']}", entry_card(row),
             *(esc("• " + t) for t in row["reasons"][:5]),
             "TEST botu otomatik açılır (gerçek emir yok)." if auto else "Bilgi amaçlıdır; otomatik işlem açılmaz."]
    return Html("\n".join(lines))


def digest_text(scan, bots, *, at_ms, early=()):
    """Twice-daily grid summary: what passed, what came closest, open test bots, early longs (morning)."""
    lines = [f"📋 <b>GRID ÖZETİ</b> — {when(at_ms)}"]
    if not scan:
        lines.append("Grid taraması henüz tamamlanmadı.")
    else:
        took = f" ({scan['seconds']:g} sn)".replace(".", ",") if scan.get("seconds") is not None else ""
        picks = [r for r in scan["results"] if r["label"] in {"GÜÇLÜ", "İZLE"}][:5]
        lines.append(esc(f"Son tarama {when(scan['at_ms'])}{took} | {scan['scanned']} coin | uygun: {len(picks)}"))
        if picks:
            lines += entry_tables(picks)
        else:
            lines.append("Şu an kriterleri sağlayan coin yok. BEKLE.")
        lines += _near_lines(scan)
    pnl = f"{bots['realized_pnl']:.2f}".replace(".", ",")
    lines.append(f"🤖 Açık TEST botu: {len(bots['active'])} | gerçekleşen K/Z {pnl} USDT")
    if early:
        lines.append(esc("🚀 Erken long (bilgi): " + ", ".join(f"{r['instrument']} {r['score']}" for r in early)
                         + " — ayrıntı /erkenlong"))
    return Html("\n".join(lines))


def range_analysis(row):
    head = f"📐 <b>GRID SİNYALİ: {esc(row['instrument'])}</b> — skor {row['score']} {esc(row['label'])}"
    if "plan" not in row:
        return Html("\n".join([head, esc("Uygun değil: " + row["reasons"][0]),
                                esc("Aranan: öncesinde ≥%20 yükseliş, trend sağlam, %4-20 yatay bant, hacim soğuyor.")]))
    return Html("\n".join([head, entry_card(row), *(esc("• " + t) for t in row["reasons"][:6])]))


def bots_text(summary, closed):
    limits = summary["limits"]
    lines = [f"🧱 GRID BOTLARI — {'GERÇEK (OKX TR)' if summary['mode'] == 'live' else 'TEST (paper)'}",
             f"Otomatik: {'AÇIK' if summary['auto'] else 'KAPALI'}" + (f" | DURDURULDU: {summary['halt']}" if summary["halt"] else ""),
             f"Kullanılan: {summary['reserved']} / {limits.total_capital} USDT | bot {len(summary['active'])}/{limits.max_bots}",
             f"Gerçekleşen K/Z: {summary['realized_pnl']:.2f} USDT (sınır −{limits.max_realized_loss})", ""]
    if not summary["active"]:
        lines.append("Açık bot yok.")
    for b in summary["active"]:
        lines.append(f"• {b['instrument']} [{b['state']}] {b['lower']}–{b['upper']} {b['grids']} grid, "
                     f"{b['capital']} USDT, stop {b['stop']}, K/Z {b['pnl'] or '0'}")
    if closed:
        lines += ["", "Son kapananlar:"]
        lines += [f"• {b['instrument']} {CLOSE.get(b['close_reason'], reason(b['close_reason'] or ''))} "
                  f"K/Z {b['pnl'] or '-'} ({when(b['updated_ms'])})" for b in closed[-5:]]
    return "\n".join(lines)


def event_text(event, mode):
    tag = "" if mode == "live" else " [TEST]"
    kind = event["kind"]
    if kind == "OPENED":
        b = event["bot"]
        return (f"🟢 GRID BOTU KURULDU{tag}: {b['instrument']}\n{b['lower']} – {b['upper']} | {b['grids']} grid | "
                f"{b['capital']} USDT\nStop {b['stop']} | Kâr al {b['take_profit']}")
    if kind == "CLOSED":
        return f"🔔 GRID BOTU KAPANDI{tag}: {event['instrument']} — {CLOSE.get(event['reason'], event['reason'])}, K/Z {event['pnl']} USDT"
    if kind == "HALT":
        return "⛔ Toplam zarar sınırına ulaşıldı. Otomatik grid kapatıldı. Devam için /devam ve /otomatik ac."
    if kind == "RESOLVED":
        return f"✅ OKX'e gönderilen {event['instrument']} grid isteği doğrulandı (bot çalışıyor)."
    if kind == "NOT_CREATED":
        return f"ℹ️ {event['instrument']} grid isteği OKX'te bulunamadı; bot kurulmamış sayıldı."
    if kind == "ERROR":
        return f"⚠️ {event['text']}"
    return str(event)
