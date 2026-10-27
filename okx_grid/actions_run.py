"""One time-boxed run of the Telegram bot for a GitHub Actions runner (no server).

A scheduled workflow starts this every few minutes. A run restores the
private state cache, answers pending Telegram commands, runs the jobs that
are due (market-wide money tracking, early longs, paper grid bots) and exits
before the next run starts. This mode is paper only: real OKX orders are never
placed from a runner, whatever the settings say.
"""
import logging
import os
import sys
import time

from .domain import TradingError
from .live_bot import LiveBot, Settings
from .live_reports import reason

log = logging.getLogger("okx_tr_actions")

# (method, seconds between runs of the job)
JOBS = (("job_rotation", 15 * 60), ("job_flows", 60 * 60), ("job_early", 4 * 3600),
        ("job_reconcile", 2 * 60), ("job_grid", 15 * 60))
MAX_MESSAGE_AGE = 15 * 60  # older commands are dropped, never replayed after a cache loss
POLL_SECONDS = 20
FIRST_START_TEXT = ("Merhaba! Sohbet kimliğiniz: {chat}\n"
                    "Botu yalnız sizin kullanmanız için GitHub'da depo → Settings → Secrets and variables → "
                    "Actions → New repository secret ekranından şunu ekleyin:\n"
                    "Name: TELEGRAM_ALLOWED_CHAT_IDS\nSecret: {chat}\n"
                    "Ekledikten sonra birkaç dakika içinde komutlarınıza cevap verir.")


def run_settings(env):
    """Settings for a runner: unregistered chats allowed, execution forced to paper."""
    safe = dict(env)
    safe["GRID_EXECUTION"] = "paper"
    safe.setdefault("STATE_DIR", os.path.join(env.get("RUNNER_TEMP", "/tmp"), "okx-tr-state"))
    for key in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"):
        safe[key] = ""
    return Settings.from_env(safe, allow_unregistered=True)


def due(bot, name, every, now):
    last = bot.grid._meta("last:" + name, 0)
    return now - last >= every


def run_jobs(bot, now, *, jobs=JOBS):
    ran = []
    for name, every in jobs:
        # Until a chat is registered only the history collection runs: alerts would have no destination.
        if not due(bot, name, every, now()) or (not bot.s.allowed_chats and name != "job_rotation"):
            continue
        bot.grid._set_meta("last:" + name, now())
        try:
            getattr(bot, name)()
            ran.append(name)
        except TradingError as error:
            log.warning("%s failed: %s", name, error.code)
        except Exception:  # one failing job never stops the others
            log.exception("%s failed", name)
    return ran


def handle_update(bot, message, now):
    """Answer one Telegram message; returns True when a reply was sent."""
    text = (message.get("text") or "").strip()
    chat = (message.get("chat") or {}).get("id")
    if not text.startswith("/") or chat is None or (message.get("from") or {}).get("is_bot"):
        return False
    if now() - int(message.get("date") or 0) > MAX_MESSAGE_AGE:
        return False
    if not bot.s.allowed_chats:
        # Not registered yet: the only thing the bot does is tell this chat its own ID.
        if text.split()[0].split("@")[0].lower() in {"/start", "/yardim", "/kimlik"}:
            bot.notify(FIRST_START_TEXT.format(chat=chat), chat=chat)
            return True
        return False
    if not bot.allowed(message):
        return False
    answer = bot.handle(message)
    if answer:
        bot.notify(answer, chat=chat)
    return bool(answer)


def run(env, *, seconds=240, bot=None, now=time.time, monotonic=time.monotonic, sleep=time.sleep):
    bot = bot if bot is not None else LiveBot(run_settings(env))
    deadline = monotonic() + seconds
    answered, jobs_done = 0, []
    offset = bot.grid._meta("telegram_offset", 0)
    try:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 3:
                break
            jobs_done += run_jobs(bot, now)
            remaining = deadline - monotonic()
            if remaining <= 3:
                break
            try:
                updates = bot.telegram.updates(offset, timeout=int(min(POLL_SECONDS, remaining - 2))) or []
            except TradingError as error:
                log.warning("getUpdates failed: %s", error.code)
                sleep(5)
                continue
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                bot.grid._set_meta("telegram_offset", offset)
                message = update.get("message")
                if message and handle_update(bot, message, now):
                    answered += 1
    finally:
        bot.grid.close()
        bot.rotation.close()
    return {"answered": answered, "jobs": jobs_done}


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        result = run(os.environ, seconds=int(os.environ.get("RUN_SECONDS", "240")))
    except TradingError as error:
        if error.code == "TELEGRAM_SETTINGS_MISSING":
            # Not configured yet: finish green so the owner gets no failure mail every five minutes.
            print("TELEGRAM_BOT_TOKEN henüz eklenmemiş; bekleniyor.")
            return 0
        print("Hata:", reason(error.code))
        return 2
    print(f"tur tamam: {result['answered']} komut, {len(result['jobs'])} iş")
    return 0


if __name__ == "__main__":
    sys.exit(main())
