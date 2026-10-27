"""Guided VPS setup steps used by deploy/kur.sh. Never prints a token.

Commands (run as root, ENV is /etc/okx-tr-bot.env):
  token-auto ENV   find the existing bot's token in the old service files
  token-ask ENV    ask for the token with hidden input
  chat-ask ENV     wait for /start in Telegram and store the chat ID
Each validates with Telegram before writing and exits non-zero when it could not.
"""
import getpass
import glob
import os
import re
import subprocess
import sys
import time

from .domain import TradingError

BOT_AUTH_SHAPE = re.compile(r"(?<![\w:])(\d{6,12}:[A-Za-z0-9_-]{30,64})(?![\w-])")
OLD_SERVICES = ("okx-telegram-commands.service", "okx-grid-v5.service")
OLD_GLOBS = ("/opt/okx-grid-demo/v5/.env*", "/opt/okx-grid-demo/.env*", "/opt/okx-grid-demo/v5/*.env",
             "/opt/okx-grid-demo/v5/*.py", "/opt/okx-grid-demo/v5/*.json", "/opt/okx-grid-demo/v5/*.ini",
             "/opt/okx-grid-demo/v5/*.conf", "/opt/okx-grid-demo/v5/*/*.env", "/etc/okx-grid*")


def old_sources(service_text=None):
    """Files and inline settings of the old bot that may hold its token."""
    texts, paths = [], set()
    if service_text is None:
        service_text = ""
        for name in OLD_SERVICES:
            try:
                service_text += subprocess.run(["systemctl", "cat", name], capture_output=True, text=True,
                                               timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                pass
    texts.append(service_text)
    paths.update(m.group(1) for m in re.finditer(r"EnvironmentFile=-?(\S+)", service_text))
    for pattern in OLD_GLOBS:
        paths.update(glob.glob(pattern))
    for path in sorted(paths):
        try:
            if os.path.isfile(path) and os.path.getsize(path) <= 2_000_000:
                with open(path, encoding="utf-8", errors="ignore") as handle:
                    texts.append(handle.read())
        except OSError:
            continue
    return texts


def find_tokens(texts):
    return sorted({m.group(1) for text in texts for m in BOT_AUTH_SHAPE.finditer(text)})


def set_env(path, key, value):
    """Replace or append KEY=value, keeping the file private."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        lines = []
    out, done = [], False
    for line in lines:
        if line.startswith(key + "="):
            if not done:
                out.append(f"{key}={value}")
                done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out) + "\n")
    os.chmod(path, 0o600)


def read_env(path):
    values = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.rstrip("\n").split("=", 1)
                values[key.strip()] = value.strip()
    return values


def bot_name(value, api=None):
    from .live_bot import TelegramAPI
    api = api if api is not None else TelegramAPI(value)
    try:
        result = api.call("getMe", {})
    except TradingError:
        return None
    return (result or {}).get("username")


def token_auto(env_path, texts=None, api_factory=None):
    found = []
    for value in find_tokens(texts if texts is not None else old_sources()):
        name = bot_name(value, api_factory(value) if api_factory else None)
        if name:
            found.append((value, name))
    if len(found) != 1:
        print("Eski botun ayarlarında " + ("token bulunamadı." if not found else
              f"{len(found)} farklı bot bulundu: " + ", ".join("@" + n for _, n in found)))
        return 3
    set_env(env_path, "TELEGRAM_BOT_TOKEN", found[0][0])
    print(f"Mevcut botun token'ı bulundu ve kaydedildi: @{found[0][1]}")
    return 0


def token_ask(env_path, prompt=getpass.getpass, api_factory=None):
    for _ in range(3):
        value = prompt("BotFather'daki bot token'ını yapıştırın (ekranda görünmez) ve Enter'a basın: ").strip()
        name = bot_name(value, api_factory(value) if api_factory else None) if BOT_AUTH_SHAPE.fullmatch(value) else None
        if name:
            set_env(env_path, "TELEGRAM_BOT_TOKEN", value)
            print(f"Token doğrulandı ve kaydedildi: @{name}")
            return 0
        print("Bu token Telegram'da doğrulanamadı, tekrar deneyin.")
    return 3


def chat_ask(env_path, api=None, wait_seconds=180, sleep=time.sleep, clock=time.monotonic):
    from .live_bot import TelegramAPI
    api = api if api is not None else TelegramAPI(read_env(env_path)["TELEGRAM_BOT_TOKEN"])
    try:
        start = max([u["update_id"] for u in api.call("getUpdates", {"timeout": 0}) or []] or [0])
    except TradingError as error:
        print("Telegram'a bağlanılamadı:", error.code,
              "(TELEGRAM_ERROR_409 ise eski komut servisi hâlâ çalışıyor demektir)")
        return 3
    print("Şimdi Telegram'da botunuza /start yazın. 3 dakika bekleniyor...")
    deadline = clock() + wait_seconds
    while clock() < deadline:
        try:
            updates = api.call("getUpdates", {"offset": start + 1, "timeout": 0}) or []
        except TradingError:
            updates = []
        chats = {}
        for update in updates:
            message = update.get("message") or {}
            if (message.get("text") or "").startswith("/start") and not (message.get("from") or {}).get("is_bot"):
                chat = message.get("chat") or {}
                chats[chat.get("id")] = chat.get("title") or chat.get("first_name") or ""
        if chats:
            chat_ids = ",".join(str(c) for c in chats)
            set_env(env_path, "TELEGRAM_ALLOWED_CHAT_IDS", chat_ids)
            print("Sohbet kaydedildi: " + ", ".join(f"{name} ({cid})" for cid, name in chats.items()))
            return 0
        sleep(3)
    print("/start mesajı gelmedi. Komutu tekrar çalıştırın.")
    return 3


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    commands = {"token-auto": token_auto, "token-ask": token_ask, "chat-ask": chat_ask}
    if len(argv) != 2 or argv[0] not in commands:
        print(__doc__)
        return 2
    return commands[argv[0]](argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
