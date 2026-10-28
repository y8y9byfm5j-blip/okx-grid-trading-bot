"""Guided VPS setup steps used by deploy/kur.sh. Never prints a token.

Commands (run as root, ENV is /etc/okx-tr-bot.env):
  token-auto ENV   find the existing bot's token in the old service files
  token-ask ENV    ask for the token with hidden input
  chat-ask ENV     wait for /start in Telegram and store the chat ID
  old-units ENV    list systemd units / PIDs still running with the stored token
  yaz-bot ENV      type the token by hand (no paste needed); add --yeniden to replace a stored one
  yaz-sohbet ENV   type the chat ID by hand; verified with a test message
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

# Also matches a token glued to "bot" in an API URL (https://api.telegram.org/bot123:ABC/...).
BOT_AUTH_SHAPE = re.compile(r"(?<![\d:])(\d{6,12}:[A-Za-z0-9_-]{30,64})(?![\w-])")
OLD_SERVICES = ("okx-telegram-commands.service", "okx-grid-v5.service")
OLD_GLOBS = ("/etc/okx-grid*", "/etc/systemd/system/okx*.service", "/root/.env", "/root/*.env")
OLD_TREES = ("/opt/okx-grid-demo",)
SKIP_PARTS = {".git", "site-packages", "venv", ".venv", "__pycache__", "node_modules", "state", "logs", "backups"}
# The old bot's own folder only: any file name, including odd ones such as "telegram.token".
BINARY_SUFFIXES = {".so", ".pyc", ".gz", ".xz", ".zip", ".tar", ".png", ".jpg", ".db", ".sqlite", ".sqlite3",
                   ".bin", ".deb", ".whl", ".log"}
MAX_FILE_BYTES = 200_000


def tree_files(root, limit=5000):
    """Small text files of the old bot, skipping environments, history and runtime data."""
    found = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_PARTS]
        for name in files:
            path = os.path.join(directory, name)
            try:
                skip = (os.path.splitext(name)[1].lower() in BINARY_SUFFIXES or os.path.islink(path)
                        or os.path.getsize(path) > MAX_FILE_BYTES)
            except OSError:
                skip = True
            if not skip:
                found.append(path)
                if len(found) >= limit:
                    return found
    return found


def process_sources(proc_root="/proc"):
    """Environment and command line of running processes that carry a bot token.

    The running old bot always holds its own token, even when its files are
    elsewhere. Only token-shaped strings are used; nothing is printed.
    """
    sources, roots = [], set()
    for pid in os.listdir(proc_root):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as handle:
                argv = [a.decode(errors="ignore") for a in handle.read().split(b"\0") if a]
        except OSError:
            continue
        try:
            with open(os.path.join(proc_root, pid, "environ"), "rb") as handle:
                env = handle.read().replace(b"\0", b"\n").decode(errors="ignore")
        except OSError:
            env = ""
        label = "çalışan süreç " + pid + " (" + " ".join(os.path.basename(a) for a in argv[:2]) + ")"
        text = env + "\n" + "\n".join(argv)
        if BOT_AUTH_SHAPE.search(text):
            sources.append((label, text))
        if argv and "python" in os.path.basename(argv[0]) and re.search(r"(?i)okx|telegram|bot|grid", " ".join(argv)):
            try:
                roots.add(os.readlink(os.path.join(proc_root, pid, "cwd")))
            except OSError:
                pass
    return sources, roots


def old_sources(service_text=None, proc_root="/proc"):
    """(where, text) pairs of the old bot's settings, files and running processes."""
    sources, paths = [], set()
    if service_text is None:
        service_text = ""
        for name in OLD_SERVICES:
            try:
                service_text += subprocess.run(["systemctl", "cat", name], capture_output=True, text=True,
                                               timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                pass
    sources.append(("eski servis tanımı", service_text))
    paths.update(m.group(1) for m in re.finditer(r"EnvironmentFile=-?(\S+)", service_text))
    running, roots = process_sources(proc_root) if os.path.isdir(proc_root) else ([], set())
    sources += running
    for pattern in OLD_GLOBS:
        paths.update(glob.glob(pattern))
    for root in (*OLD_TREES, *sorted(r for r in roots if r not in {"/", "/root", "/home"})):
        paths.update(tree_files(root))
    for path in sorted(paths):
        try:
            if os.path.isfile(path) and os.path.getsize(path) <= 2_000_000:
                with open(path, encoding="utf-8", errors="ignore") as handle:
                    sources.append((path, handle.read()))
        except OSError:
            continue
    return sources


def find_tokens(texts):
    return sorted({m.group(1) for text in texts for m in BOT_AUTH_SHAPE.finditer(text)})


def holders(value, proc_root="/proc"):
    """systemd units (or bare PIDs) of running processes that hold this token."""
    found = set()
    # Only a complete, unique token may select processes; never a fragment.
    if not isinstance(value, str) or not BOT_AUTH_SHAPE.fullmatch(value) or not os.path.isdir(proc_root):
        return found
    protected = {str(os.getpid())}
    pid = os.getpid()
    while pid > 1:  # never stop this setup run or the shell that started it
        try:
            with open(os.path.join("/proc", str(pid), "stat"), encoding="utf-8") as handle:
                pid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
        protected.add(str(pid))
    for pid in os.listdir(proc_root):
        if not pid.isdigit() or pid in protected or pid == "1":
            continue
        try:
            text = b""
            for part in ("environ", "cmdline"):
                with open(os.path.join(proc_root, pid, part), "rb") as handle:
                    text += handle.read() + b"\0"
        except OSError:
            continue
        if value.encode() not in text:
            continue
        unit = None
        try:
            with open(os.path.join(proc_root, pid, "cgroup"), encoding="utf-8", errors="ignore") as handle:
                match = re.search(r"/([\w@.-]+\.service)\s*$", handle.read(), re.M)
                unit = match.group(1) if match else None
        except OSError:
            pass
        if unit != "okx-tr-bot.service":
            found.add(unit or "pid:" + pid)
    return found


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


def check_bot(value, api=None):
    """(username, None) when Telegram accepts the token, else (None, error code)."""
    from .live_bot import TelegramAPI
    api = api if api is not None else TelegramAPI(value)
    try:
        result = api.call("getMe", {})
    except TradingError as error:
        return None, error.code
    return (result or {}).get("username"), None


def bot_name(value, api=None):
    return check_bot(value, api)[0]


def token_auto(env_path, texts=None, api_factory=None, choose=input):
    sources = old_sources() if texts is None else [("?", t) for t in texts]
    where = {}
    for label, text in sources:
        for match in BOT_AUTH_SHAPE.finditer(text):
            where.setdefault(match.group(1), set()).add(label)
    valid, unreachable = [], []
    for value in sorted(where):
        name, error = check_bot(value, api_factory(value) if api_factory else None)
        if name:
            valid.append((value, name))
        elif error not in {"TELEGRAM_ERROR_401", "TELEGRAM_ERROR_404"}:
            unreachable.append((value, error))
    if not valid and len(unreachable) == 1:
        # The token exists but Telegram could not be reached from here right now.
        set_env(env_path, "TELEGRAM_BOT_TOKEN", unreachable[0][0])
        print("Token bulundu ve kaydedildi, ancak Telegram'a şu an ulaşılamadı (" + str(unreachable[0][1]) + ").")
        print("Bulunduğu yer: " + ", ".join(sorted(where[unreachable[0][0]]))[:200])
        return 0
    if not valid:
        print(f"Çalışan bir bot token'ı bulunamadı ({len(sources)} yere bakıldı, "
              f"{len(where)} token biçimli değer, {len(unreachable)} doğrulanamayan).")
        return 3
    pick = valid[0]
    if len(valid) > 1:
        print("Sunucuda birden fazla bot bulundu:")
        for i, (_, name) in enumerate(valid, 1):
            print(f"  {i}) @{name}")
        try:
            answer = choose("Komutları alacak botun numarasını yazın ve Enter'a basın: ").strip()
        except EOFError:
            print("Birden fazla bot var; 'bash deploy/kur.sh' ile (-y olmadan) tekrar çalıştırıp numara seçin.")
            return 3
        if not answer.isdigit() or not 1 <= int(answer) <= len(valid):
            print("Geçersiz seçim.")
            return 3
        pick = valid[int(answer) - 1]
    set_env(env_path, "TELEGRAM_BOT_TOKEN", pick[0])
    print(f"Mevcut botun token'ı bulundu ve kaydedildi: @{pick[1]}")
    print("Bulunduğu yer: " + ", ".join(sorted(where[pick[0]]))[:200])
    return 0


def old_units(env_path):
    for target in sorted(holders(read_env(env_path).get("TELEGRAM_BOT_TOKEN", ""))):
        print(target)
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


def token_type(env_path, again=None, prompt=input, api_factory=None, attempts=5):
    """Typed (visible) token entry for consoles without paste, in as many pieces as needed.

    Pieces are appended until the token is complete, then it is validated with Telegram. Progress is kept
    next to the env file, so a console that closes mid-way resumes where it stopped.
    """
    if again != "--yeniden":
        try:
            existing = read_env(env_path).get("TELEGRAM_BOT_TOKEN", "")
        except OSError:
            existing = ""
        if BOT_AUTH_SHAPE.fullmatch(existing) and bot_name(existing, api_factory(existing) if api_factory else None):
            print("Bot token'ı zaten kayıtlı ve geçerli.")
            return 0
    progress = str(env_path) + ".part"
    complete = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{35}")

    def load():
        try:
            with open(progress, encoding="utf-8") as handle:
                return [re.sub(r"\s+", "", line) for line in handle.read().splitlines() if line.strip()]
        except OSError:
            return []

    def save(items):
        fd = os.open(progress, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(items) + ("\n" if items else ""))

    pieces = load()
    print("Token'ı BotFather'dan görerek parça parça yazabilirsiniz (ör. 10-12 karakter, Enter, devam).")
    print("Büyük/küçük harfe dikkat. 'x' + Enter: baştan başla, '-' + Enter: son parçayı sil.")
    print("Konsol kapanırsa 'bash k/token' ile kaldığınız yerden devam edersiniz. Bu ekranı paylaşmayın.")
    failures = 0
    while failures < attempts:
        joined = "".join(pieces)
        if complete.fullmatch(joined):
            name = bot_name(joined, api_factory(joined) if api_factory else None)
            if name:
                set_env(env_path, "TELEGRAM_BOT_TOKEN", joined)
                try:
                    os.remove(progress)
                except OSError:
                    pass
                print(f"Token doğrulandı ve kaydedildi: @{name}")
                return 0
            print("Token Telegram'da doğrulanamadı (yazım hatası ya da yenilenmiş token). Baştan yazın.")
            pieces, failures = [], failures + 1
            save(pieces)
            continue
        if len(joined) > 46:
            print("Fazla karakter girildi. Baştan yazın.")
            pieces, failures = [], failures + 1
            save(pieces)
            continue
        hint = f", son 3: {joined[-3:]}" if joined else ""
        piece = prompt(f"Token [{len(joined)} karakter girildi{hint}] devam: ").strip()
        if piece.lower() == "x":
            pieces = []
        elif piece == "-":
            pieces = pieces[:-1]
        elif piece:
            pieces.append(re.sub(r"\s+", "", piece))
        save(pieces)
    return 3


def chat_type(env_path, prompt=input, api=None, attempts=5):
    """Typed chat ID, verified by sending that chat a test message through the bot."""
    from .live_bot import TelegramAPI
    api = api if api is not None else TelegramAPI(read_env(env_path)["TELEGRAM_BOT_TOKEN"])
    existing = read_env(env_path).get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    if re.fullmatch(r"-?\d{5,15}", existing.strip()):
        print("Sohbet kimliği zaten kayıtlı:", existing.strip())
        return 0
    print("Botun size yazdığı sohbet kimliğini (yalnız sayı) yazın. Bilmiyorsanız önce botunuza Telegram'dan /start yazın.")
    for _ in range(attempts):
        raw = prompt("Sohbet kimliği: ").strip()
        if not re.fullmatch(r"-?\d{5,15}", raw):
            print("Yalnız sayı yazın (ör. 123456789).")
            continue
        try:
            api.call("sendMessage", {"chat_id": int(raw), "text": "✅ Sunucu bağlantısı kuruldu. Kurulum sürüyor."})
        except TradingError:
            print("Bu sohbete mesaj gönderilemedi. Sayıyı kontrol edin ve botunuza önce /start yazmış olun.")
            continue
        set_env(env_path, "TELEGRAM_ALLOWED_CHAT_IDS", raw)
        print("Sohbet kimliği doğrulandı ve kaydedildi. Telegram'a bir test mesajı geldi.")
        return 0
    return 3


def chat_ask(env_path, api=None, wait_seconds=None, sleep=time.sleep, clock=time.monotonic):
    if wait_seconds is None:
        wait_seconds = int(os.environ.get("CHAT_WAIT", "180"))
    from .live_bot import TelegramAPI
    api = api if api is not None else TelegramAPI(read_env(env_path)["TELEGRAM_BOT_TOKEN"])
    try:
        start = max([u["update_id"] for u in api.call("getUpdates", {"timeout": 0}) or []] or [0])
    except TradingError as error:
        print("Telegram'a bağlanılamadı:", error.code,
              "(TELEGRAM_ERROR_409 ise eski komut servisi hâlâ çalışıyor demektir)")
        return 3
    print(f"Şimdi Telegram'da botunuza /start yazın. {wait_seconds // 60} dakika bekleniyor...")
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
    commands = {"token-auto": token_auto, "token-ask": token_ask, "chat-ask": chat_ask, "old-units": old_units,
                "yaz-bot": token_type, "yaz-sohbet": chat_type}
    if not 2 <= len(argv) <= 3 or argv[0] not in commands or (len(argv) == 3 and argv[0] != "yaz-bot"):
        print(__doc__)
        return 2
    return commands[argv[0]](*argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
