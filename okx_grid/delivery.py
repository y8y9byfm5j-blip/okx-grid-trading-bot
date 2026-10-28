"""Encrypted hand-over of the bot settings from GitHub secrets to the server.

A workflow encrypts the settings with a code the owner chose and published only
as a GitHub secret; the ciphertext goes to a branch. The server asks for the
code, downloads the ciphertext and decrypts it. No long value is ever typed.
"""
import os
import re
import subprocess
import sys
import urllib.request

from .domain import TradingError

URL = "https://raw.githubusercontent.com/y8y9byfm5j-blip/okx-grid-trading-bot/teslim/payload.enc"
MAGIC = "OKXTR1"
ITERATIONS = "600000"
_AUTH_SHAPE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{35}")


def _openssl(args, data, code):
    try:
        done = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", ITERATIONS, *args,
                               "-pass", "env:OKX_DELIVERY_CODE"], input=data, capture_output=True,
                              env={**os.environ, "OKX_DELIVERY_CODE": code}, timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise TradingError("DELIVERY_OPENSSL_MISSING") from None
    return done


def build(auth_raw, chat_raw):
    """Clean KEY=value lines from possibly messy secret values."""
    auth = _AUTH_SHAPE.search(auth_raw or "")
    chats = re.findall(r"-?\d{5,}", chat_raw or "")
    if not auth or not chats:
        raise TradingError("DELIVERY_INCOMPLETE")
    return MAGIC + "\nTELEGRAM_BOT_TOKEN=" + auth.group(0) + "\nTELEGRAM_ALLOWED_CHAT_IDS=" + ",".join(chats) + "\n"


def encrypt(text, code):
    if not code or len(code) < 10:
        raise TradingError("DELIVERY_CODE_TOO_SHORT")
    done = _openssl(["-a", "-salt"], text.encode(), code)
    if done.returncode:
        raise TradingError("DELIVERY_ENCRYPT_FAILED")
    return done.stdout.decode()


def decrypt(blob, code):
    done = _openssl(["-d", "-a"], blob.encode(), code)
    text = done.stdout.decode(errors="ignore") if not done.returncode else ""
    if not text.startswith(MAGIC + "\n"):
        raise TradingError("DELIVERY_BAD_CODE")
    values = {}
    for line in text.splitlines()[1:]:
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if not _AUTH_SHAPE.fullmatch(values.get("TELEGRAM_BOT_TOKEN", "")) or not values.get("TELEGRAM_ALLOWED_CHAT_IDS"):
        raise TradingError("DELIVERY_BAD_CODE")
    return values


def fetch(url=URL, opener=None):
    try:
        with (opener or urllib.request.urlopen)(url, timeout=20) as response:
            return response.read(100_000).decode()
    except Exception:
        raise TradingError("DELIVERY_NOT_FOUND") from None


def main(argv=None):
    """python -m okx_grid.delivery encrypt OUT   (reads BOT_AUTH, CHAT_ID, DELIVERY_CODE from the environment)"""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != "encrypt":
        print(main.__doc__)
        return 2
    try:
        blob = encrypt(build(os.environ.get("BOT_AUTH", ""), os.environ.get("CHAT_ID", "")),
                       os.environ.get("DELIVERY_CODE", ""))
    except TradingError as error:
        print("Teslim hazırlanamadı:", error.code)
        return 1
    with open(argv[1], "w", encoding="utf-8") as handle:
        handle.write(blob)
    print("Şifreli teslim dosyası hazır.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
