"""Telegram service: OKX TR analysis, money rotation, early longs and automatic GRID bots.

Run on the VPS with ``python3 -m okx_grid.live_bot``; settings come from the
environment (systemd EnvironmentFile). This process is the single
getUpdates consumer for its bot token. GRID_EXECUTION=paper (default) never
calls OKX private endpoints; GRID_EXECUTION=live requires OKX TR API keys.
"""
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal
import http.client
import json
import logging
import os
import re
from pathlib import Path
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, Request, build_opener

from .domain import TradingError
from .early_long import STRONG, EarlyLongScanner, features, score
from .grid_manager import GridBotManager, GridLimits
from .live_reports import (bots_text, decision_text, early_alert, early_text, event_text, help_text, reason,
                           rotation_text, usd)
from .rotation import RotationTracker
from .tr_public import TRPublic, coin_volumes

log = logging.getLogger("okx_tr_bot")
_BOT_AUTH_SHAPE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,64}")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_chats: frozenset
    allowed_users: frozenset
    notify_chat: int
    state_dir: Path
    execution: str = "paper"
    okx_access: str = ""
    okx_signing: str = ""
    okx_phrase: str = ""
    limits: GridLimits = GridLimits()
    min_early_volume: Decimal = Decimal("1000000")

    @classmethod
    def from_env(cls, env, allow_unregistered=False):
        def ids(name):
            try:
                return frozenset(int(v) for v in env.get(name, "").replace(" ", "").split(",") if v)
            except ValueError:
                raise TradingError("INVALID_" + name) from None
        chats = ids("TELEGRAM_ALLOWED_CHAT_IDS")
        if (env.get("TELEGRAM_BOT_TOKEN", "CHANGE_ME").strip() in {"", "CHANGE_ME"}
                or not chats and not allow_unregistered):
            raise TradingError("TELEGRAM_SETTINGS_MISSING")
        execution = env.get("GRID_EXECUTION", "paper").strip().lower()
        if execution not in {"paper", "live"}:
            raise TradingError("INVALID_EXECUTION_MODE")
        try:
            limits = GridLimits(total_capital=Decimal(env.get("GRID_TOTAL_CAPITAL", "600")),
                                bot_capital=Decimal(env.get("GRID_BOT_CAPITAL", "300")),
                                max_bots=int(env.get("GRID_MAX_BOTS", "2")),
                                risk_per_bot=Decimal(env.get("GRID_RISK_PER_BOT", "3")),
                                max_realized_loss=Decimal(env.get("GRID_MAX_REALIZED_LOSS", "12")),
                                cooldown_hours=int(env.get("GRID_COOLDOWN_HOURS", "6")))
            min_early = Decimal(env.get("EARLY_MIN_VOLUME_USDT", "1000000"))
        except (ArithmeticError, ValueError):
            raise TradingError("INVALID_GRID_SETTINGS") from None
        # A pasted secret may carry spaces, a newline or the whole BotFather message: use only the token.
        raw = env["TELEGRAM_BOT_TOKEN"]
        found = _BOT_AUTH_SHAPE.search(raw)
        value = found.group(0) if found else raw.strip()
        if not value or "/" in value or any(c.isspace() for c in value):
            raise TradingError("TELEGRAM_BAD_AUTH_FORMAT")
        return cls(bot_token=value, allowed_chats=chats,
                   allowed_users=ids("TELEGRAM_ALLOWED_USER_IDS"),
                   notify_chat=int(env.get("TELEGRAM_NOTIFY_CHAT_ID") or (sorted(chats)[0] if chats else 0)),
                   state_dir=Path(env.get("STATE_DIR", "/var/lib/okx-tr-bot")), execution=execution,
                   okx_access=env.get("OKX_API_KEY", ""), okx_signing=env.get("OKX_API_SECRET", ""),
                   okx_phrase=env.get("OKX_API_PASSPHRASE", ""), limits=limits,
                   min_early_volume=min_early)


class TelegramAPI:
    def __init__(self, bot_token, *, transport=None):
        self._base = "https://api.telegram.org/bot" + bot_token + "/"
        self._send = transport if transport is not None else self._urllib

    def __repr__(self):
        return "TelegramAPI(<redacted>)"

    @staticmethod
    def _urllib(url, body, timeout):
        opener = build_opener(HTTPSHandler(context=ssl.create_default_context()))
        request = Request(url, data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read(5_000_000).decode())
        except HTTPError as error:
            try:
                return json.loads(error.read(65536).decode())
            except ValueError:
                return {"ok": False, "error_code": error.code}
            finally:
                error.close()

    def call(self, method, body, timeout=20):
        failed = False
        try:
            data = self._send(self._base + method, body, timeout)
        except (URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
            failed = True
        if failed:
            # Raised outside the except block so no original error stays attached: its text can
            # contain the request URL, and the URL holds the token.
            raise TradingError("TELEGRAM_UNAVAILABLE")
        if not isinstance(data, dict) or not data.get("ok"):
            code = data.get("error_code") if isinstance(data, dict) else None
            raise TradingError(f"TELEGRAM_ERROR_{code}")
        return data.get("result")

    def updates(self, offset, timeout=50):
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["message"]}, timeout=timeout + 15)

    def send(self, chat_id, text):
        for start in range(0, len(text), 3900):
            self.call("sendMessage", {"chat_id": chat_id, "text": text[start:start + 3900],
                                      "disable_web_page_preview": True})


def build_profile(limits):
    """The agreed V7 rules with the owner's capital and risk limits."""
    from .v7_core import load_v7
    base = load_v7()
    def fraction(value):  # the policy accepts at most 18 decimals; round toward the safer side
        return min(value, Decimal(1)).quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
    policy = replace(base.policy, equity=limits.total_capital, max_bot_capital=limits.bot_capital,
                     max_bots=limits.max_bots, risk_fraction=fraction(limits.risk_per_bot / limits.total_capital),
                     total_risk_fraction=fraction(limits.risk_per_bot * limits.max_bots / limits.total_capital),
                     max_realized_loss_fraction=fraction(limits.max_realized_loss / limits.total_capital))
    return replace(base, name="okxtr-live-grid", policy=policy)


class LiveBot:
    def __init__(self, settings, *, telegram=None, reader=None, market=None, private=None, clock_ms=None):
        self.s = settings
        self.clock_ms = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1_000_000
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.telegram = telegram if telegram is not None else TelegramAPI(settings.bot_token)
        self.reader = reader if reader is not None else TRPublic()
        self.profile = build_profile(settings.limits)
        from .v7_decision import PlanningPolicy
        self.planning = PlanningPolicy()
        if market is None:
            from .v7_market import V7MarketData
            market = V7MarketData(self.profile.policy)
        self.market = market
        if settings.execution == "live" and private is None:
            from .tr_private import OKXTRPrivate
            private = OKXTRPrivate(settings.okx_access, settings.okx_signing, settings.okx_phrase)
        self.grid = GridBotManager(settings.state_dir / "grid.sqlite3", mode=settings.execution, client=private,
                                   limits=settings.limits, clock_ms=self.clock_ms, price=self.reader.last_price)
        self.rotation = RotationTracker(settings.state_dir / "market.sqlite3", self.reader, clock_ms=self.clock_ms)
        self.scanner = EarlyLongScanner(self.reader, rotation=self.rotation, min_volume=settings.min_early_volume,
                                        clock_ms=self.clock_ms)
        self._stop = threading.Event()
        override = self.grid._meta("risk_per_bot")
        if override is not None:
            try:
                self.set_risk(Decimal(override))
            except TradingError:
                log.warning("stored risk override ignored")

    def last_scan(self):
        """Latest early-long scan: this process's, else the one a previous run stored."""
        return self.scanner.last or self.grid._meta("early_scan")

    def set_risk(self, value):
        """Per-bot planned stop loss, changed from Telegram; capped at 10% of a bot's capital."""
        limits = self.grid.limits
        if not value.is_finite() or not Decimal("0.5") <= value <= limits.bot_capital / 10:
            raise TradingError("INVALID_RISK_VALUE")
        limits = replace(limits, risk_per_bot=value)
        self.profile = build_profile(limits)
        self.grid.limits = limits
        self.grid._set_meta("risk_per_bot", str(value))

    # -- outgoing --------------------------------------------------------------------
    def notify(self, text, chat=None):
        if not (chat or self.s.notify_chat):
            return  # not registered yet: no destination
        try:
            self.telegram.send(chat or self.s.notify_chat, text)
        except TradingError as error:
            log.warning("telegram send failed: %s", error.code)

    # -- commands --------------------------------------------------------------------
    def allowed(self, message):
        chat = (message.get("chat") or {}).get("id")
        user = (message.get("from") or {}).get("id")
        return (chat in self.s.allowed_chats and not (message.get("from") or {}).get("is_bot")
                and (not self.s.allowed_users or user in self.s.allowed_users))

    def handle(self, message):
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        parts = text.replace("ı", "i").replace("İ", "I").split()
        name, args = parts[0].split("@")[0].lower(), parts[1:]
        try:
            return self.command(name, args)
        except TradingError as error:
            return "⚠️ " + reason(error.code)
        except Exception:  # never let one command stop the service
            log.exception("command failed")
            return "⚠️ Komut çalışırken beklenmeyen bir hata oldu. Kayıtlara yazıldı."

    @staticmethod
    def pair(args):
        if len(args) != 1:
            raise TradingError("COMMAND_ARGUMENTS_REQUIRED")
        value = args[0].upper().replace("/", "-").replace("_", "-")
        if "-" not in value:
            value = value[:-4] + "-USDT" if value.endswith("USDT") and len(value) > 4 else value + "-USDT"
        if not value.endswith("-USDT") or not value.replace("-", "").isalnum():
            raise TradingError("INVALID_SPOT_SYMBOL")
        return value

    def command(self, name, args):
        if name in {"/start", "/yardim", "/help"}:
            return help_text(self.s.execution)
        if name == "/rotasyon":
            return rotation_text(self.rotation.analyze())
        if name == "/erkenlong":
            return early_text(self.last_scan())
        if name in {"/analiz", "/grid"}:
            instrument = self.pair(args)
            decision = self.decide(instrument)
            text = decision_text(decision, title="GRID PLANI" if name == "/grid" else "ANALİZ")
            if name == "/analiz":
                text += "\n\n" + self.coin_extras(instrument)
            return text
        if name in {"/botlar", "/paper"}:
            return bots_text(self.grid.summary(), self.grid.closed())
        if name == "/radar":
            return self.radar_text()
        if name == "/durum":
            return self.status_text()
        if name == "/otomatik":
            if not args or args[0].lower() not in {"ac", "aç", "kapat"}:
                return "Kullanım: /otomatik ac  veya  /otomatik kapat"
            on = args[0].lower() in {"ac", "aç"}
            if on and self.grid.halted():
                return "Otomasyon durdurulmuş (" + str(self.grid.halted()) + "). Önce /devam yazın."
            self.grid.set_auto(on)
            return "Otomatik grid " + ("AÇIK" if on else "KAPALI") + (" (TEST modu)" if self.s.execution == "paper" else "")
        if name == "/risk":
            limits = self.grid.limits
            if args:
                try:
                    value = Decimal(args[0].replace(",", "."))
                except ArithmeticError:
                    raise TradingError("INVALID_RISK_VALUE") from None
                self.set_risk(value)
                limits = self.grid.limits
                head = f"✅ Bot başı risk {limits.risk_per_bot} USDT olarak ayarlandı."
            else:
                head = f"Bot başı risk: {limits.risk_per_bot} USDT ({limits.bot_capital} USDT'lik botta)."
            width = limits.risk_per_bot / limits.bot_capital * 100
            return "\n".join([head, f"Yaklaşık en geniş grid aralığı: %{max(width - Decimal('0.3'), Decimal(0)):.1f}".replace(".", ","),
                              f"Toplam zarar sınırı: {limits.max_realized_loss} USDT (aşılınca otomasyon durur).",
                              f"Değiştirmek için: /risk 10  (0,5 ile {limits.bot_capital / 10:.0f} USDT arası)"])
        if name == "/durdur":
            count = self.grid.stop(self.pair(args))
            return f"{count} bot için durdurma gönderildi; coin USDT'ye satılacak. Sonuç birkaç dakika içinde bildirilir."
        if name in {"/acil_durdur", "/emergency_stop"}:
            stopped, failed = self.grid.emergency_stop()
            text = f"⛔ ACİL DURDURMA: {stopped} bot durduruluyor, otomasyon kapatıldı."
            if failed:
                text += "\nDurdurulamayanlar: " + ", ".join(f"{i} ({c})" for i, c in failed) + " — OKX uygulamasından kontrol edin."
            return text
        if name == "/devam":
            self.grid.resume()
            return "Durdurma kaldırıldı. Otomatik kurulum için /otomatik ac yazın."
        return "Bilinmeyen komut. /yardim yazın."

    def decide(self, instrument):
        from .v7_decision import decide
        report = self.market.capture(instrument, refresh=True)
        return decide(report, self.profile, self.planning, capital=str(self.grid.limits.bot_capital),
                      now_ms=self.market._now())

    def radar_text(self, limit=3):
        """Same V7 grid filters on the most traded USDT pairs; the scheduler rotates through the rest."""
        coins = self.rotation.coins() or coin_volumes(self.reader.spot_tickers())[0]
        floor = self.profile.policy.min_quote_volume
        names = [c + "-USDT" for c, v in sorted(coins.items(), key=lambda kv: kv[1]["usdt_volume"], reverse=True)
                 if v["usdt_volume"] >= floor][:limit]
        lines = [f"📡 GRID RADARI — en hacimli {len(names)} coin (24s ≥ {usd(floor, signed=False)})"]
        for instrument in names:
            try:
                decision = self.decide(instrument)
                lines.append(("✅ " if decision["eligible"] else "⛔ ") + instrument + ": " +
                             ("UYGUN" if decision["eligible"] else "; ".join(reason(r) for r in decision["reasons"][:2])))
            except TradingError as error:
                lines.append(f"⚠️ {instrument}: {reason(error.code)}")
        lines.append("Otomatik tarama diğer coinleri 15 dakikada bir sırayla inceler. Ayrıntı: /grid COIN")
        return "\n".join(lines)

    def coin_extras(self, instrument):
        coin = instrument.split("-")[0]
        lines = []
        try:
            daily = self.reader.candles(instrument, "1D", 100)
            btc = self.reader.candles("BTC-USDT", "1D", 100)
            points, label, reasons = score(features(daily, btc), self.rotation.accumulation_ratio(coin))
            lines.append(f"🚀 Erken long skoru: {points} {label}")
            lines += ["   • " + r for r in reasons[:5]]
        except TradingError as error:
            lines.append("🚀 Erken long skoru: hesaplanamadı (" + reason(error.code) + ")")
        try:
            hourly = self.reader.taker_volume(coin, "1H")[-24:]
            price = self.reader.last_price(instrument)
            net = (sum(r[2] for r in hourly) - sum(r[1] for r in hourly)) * price
            lines.append(f"💸 24s net agresif alış: {usd(net)}")
        except TradingError:
            lines.append("💸 Para akışı verisi alınamadı.")
        return "\n".join(lines)

    def status_text(self):
        summary = self.grid.summary()
        health = self.market.last_health
        scan = self.last_scan()
        flows_at, _ = self.rotation.latest_flows()
        limits = self.grid.limits
        from .live_reports import when
        return "\n".join([
            "ℹ️ DURUM", f"Mod: {'GERÇEK EMİR' if self.s.execution == 'live' else 'TEST (paper)'} | "
            f"otomatik {'AÇIK' if summary['auto'] else 'KAPALI'}" + (f" | DURDURULDU {summary['halt']}" if summary["halt"] else ""),
            f"Limitler: toplam {limits.total_capital} USDT, bot başı {limits.bot_capital}, en fazla {limits.max_bots} bot, "
            f"bot başı risk {limits.risk_per_bot}, toplam zarar sınırı {limits.max_realized_loss} USDT",
            f"Açık bot: {len(summary['active'])} | kullanılan {summary['reserved']} USDT | K/Z {summary['realized_pnl']:.2f}",
            f"Son grid verisi: {health.get('data_status')} ({reason(health.get('reason', '')) if not health.get('ok') else 'tamam'})",
            f"Para akışı: {when(flows_at) if flows_at else 'bekleniyor'} | Erken long: {when(scan['at_ms']) if scan else 'bekleniyor'}",
            "Kaynak: tr.okx.com"])

    # -- scheduled jobs ----------------------------------------------------------------
    def job_rotation(self):
        self.rotation.collect()

    def job_flows(self):
        self.rotation.collect_flows()

    def job_early(self):
        scan = self.scanner.scan()
        self.grid._set_meta("early_scan", {**scan, "results": scan["results"][:40]})
        now = self.clock_ms()
        sent = self.grid._meta("early_alerts", {})
        for row in scan["results"]:
            if row["score"] < STRONG:
                break
            if now - sent.get(row["instrument"], 0) > 3 * 86_400_000:
                self.notify(early_alert(row))
                sent[row["instrument"]] = now
        self.grid._set_meta("early_alerts", {k: v for k, v in sent.items() if now - v < 7 * 86_400_000})

    def job_reconcile(self):
        for event in self.grid.reconcile():
            self.notify(event_text(event, self.s.execution))

    def job_grid(self):
        if not self.grid.auto_enabled() or self.grid.halted():
            return
        if len(self.grid.active()) >= self.grid.limits.max_bots:
            return
        coins = self.rotation.coins()
        floor = self.profile.policy.min_quote_volume
        names = sorted((c + "-USDT" for c, v in coins.items() if v["usdt_volume"] >= floor),
                       key=lambda n: coins[n[:-5]]["usdt_volume"], reverse=True)
        if not names:
            return
        offset = self.grid._meta("grid_offset", 0) % len(names)
        batch = (names[offset:] + names[:offset])[:3]
        self.grid._set_meta("grid_offset", (offset + len(batch)) % len(names))
        for instrument in batch:
            if self.grid.blocking_reasons(instrument):
                continue
            try:
                decision = self.decide(instrument)
                if not decision["eligible"]:
                    continue
                bot = self.grid.open_from_decision(decision)
                self.notify(event_text({"kind": "OPENED", "bot": bot}, self.s.execution))
            except TradingError as error:
                if error.code == "PRIVATE_OUTCOME_UNKNOWN":
                    self.notify(f"⚠️ {instrument} için OKX yanıtı alınamadı; birkaç dakika içinde doğrulanacak.")
                    return
                if error.code.startswith(("OKX_", "INSUFFICIENT_USDT")):
                    self.notify(f"⚠️ {instrument} grid kurulamadı: {reason(error.code)}")
                continue
            if len(self.grid.active()) >= self.grid.limits.max_bots:
                return

    def scheduler(self):
        jobs = [(self.job_rotation, 15 * 60), (self.job_flows, 60 * 60), (self.job_early, 4 * 3600),
                (self.job_reconcile, 2 * 60), (self.job_grid, 15 * 60)]
        due = {job: 0.0 for job, _ in jobs}
        due[self.job_grid] = time.monotonic() + 120  # first collect market-wide data
        while not self._stop.is_set():
            for job, every in jobs:
                if time.monotonic() >= due[job]:
                    due[job] = time.monotonic() + every
                    try:
                        job()
                    except TradingError as error:
                        log.warning("%s failed: %s", job.__name__, error.code)
                    except Exception:
                        log.exception("%s failed", job.__name__)
            self._stop.wait(5)

    def poll(self):
        offset = self.grid._meta("telegram_offset", 0)
        if not offset:
            # First start: skip the backlog left by the previous consumer instead of replaying it.
            try:
                backlog = self.telegram.updates(-1, timeout=0) or []
                offset = max([int(u["update_id"]) + 1 for u in backlog] or [0])
                self.grid._set_meta("telegram_offset", offset)
            except TradingError as error:
                log.warning("getUpdates failed: %s", error.code)
        while not self._stop.is_set():
            try:
                updates = self.telegram.updates(offset)
            except TradingError as error:
                log.warning("getUpdates failed: %s", error.code)
                self._stop.wait(10)
                continue
            for update in updates or []:
                offset = max(offset, int(update["update_id"]) + 1)
                self.grid._set_meta("telegram_offset", offset)
                message = update.get("message")
                if not message or not self.allowed(message):
                    continue
                answer = self.handle(message)
                if answer:
                    self.notify(answer, chat=message["chat"]["id"])

    def run(self):
        thread = threading.Thread(target=self.scheduler, name="scheduler", daemon=True)
        thread.start()
        self.notify("✅ OKX TR bot başladı. " + ("GERÇEK EMİR modu." if self.s.execution == "live"
                                                 else "TEST modu: gerçek emir gönderilmez.") + " /yardim")
        try:
            self.poll()
        finally:
            self._stop.set()


def whoami(env):
    """Print chat/user IDs of recent messages so the owner can fill the allow list."""
    api = TelegramAPI(env["TELEGRAM_BOT_TOKEN"].strip())
    seen = set()
    for update in api.call("getUpdates", {"timeout": 0}) or []:
        message = update.get("message") or {}
        key = ((message.get("chat") or {}).get("id"), (message.get("from") or {}).get("id"))
        if key[0] is not None and key not in seen:
            seen.add(key)
            print(f"chat_id={key[0]} user_id={key[1]} text={(message.get('text') or '')[:20]!r}")
    if not seen:
        print("Mesaj bulunamadı: bota Telegram'dan /start yazıp tekrar deneyin "
              "(eski komut servisi çalışıyorsa önce durdurun).")


def check_okx(env):
    """Read-only key check: USDT balance and running grid bots. Never places orders."""
    from .tr_private import OKXTRPrivate
    client = OKXTRPrivate(env.get("OKX_API_KEY", ""), env.get("OKX_API_SECRET", ""), env.get("OKX_API_PASSPHRASE", ""))
    print("USDT kullanılabilir:", client.usdt_available())
    print("Çalışan grid botları:", [(g.get("instId"), g.get("state"), g.get("investment")) for g in client.pending_grids()])


def main(argv=None):
    import sys
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if argv == ["--kimlik"]:
            return whoami(os.environ)
        if argv == ["--okx-kontrol"]:
            return check_okx(os.environ)
        settings = Settings.from_env(os.environ)
    except TradingError as error:
        print("Hata:", error.code)
        raise SystemExit(2)
    LiveBot(settings).run()


if __name__ == "__main__":
    main()
