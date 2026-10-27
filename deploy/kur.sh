#!/usr/bin/env bash
# Guided one-shot setup on the VPS:  sudo bash deploy/kur.sh
# Installs the code, finds the existing bot's token, swaps the old Telegram
# command service for the new one (test mode) and checks that it started.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/okx-tr-bot
ENV_FILE=/etc/okx-tr-bot.env
OLD=okx-telegram-commands.service
NEW=okx-tr-bot.service

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
helper() { (cd "$APP" && "$(cat "$APP/PYTHON")" -m okx_grid.setup_helper "$@"); }
value() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- || true; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Bu komutu sudo ile çalıştırın: sudo bash $0" >&2
  exit 1
fi

say "OKX TR bot kurulumu (TEST modu: gerçek emir gönderilmez)"
echo "Bu kurulum: kodu $APP içine kurar, eski Telegram komut servisini ($OLD) durdurur"
echo "(silmez), yeni servisi ($NEW) aynı bot ile başlatır."
# -y: no questions, for a background run that survives a closed console.
AUTO=0
[ "${1:-}" = "-y" ] && AUTO=1
if [ "$AUTO" -eq 0 ]; then
  read -r -p "Devam edilsin mi? (e/h): " ok
  case "$ok" in e|E|evet|Evet) ;; *) echo "İptal edildi."; exit 0 ;; esac
fi

say "1/5 Kod kuruluyor"
bash "$SRC/deploy/install.sh"

say "2/5 Bot token'ı"
current="$(value TELEGRAM_BOT_TOKEN)"
if [ -n "$current" ] && [ "$current" != "CHANGE_ME" ]; then
  echo "Ayar dosyasında token zaten var."
elif ! helper token-auto "$ENV_FILE" </dev/null && [ "$AUTO" -eq 0 ]; then
  helper token-ask "$ENV_FILE"
elif [ -z "$(value TELEGRAM_BOT_TOKEN)" ] || [ "$(value TELEGRAM_BOT_TOKEN)" = "CHANGE_ME" ]; then
  echo "Token otomatik bulunamadı. 'bash deploy/kur.sh' ile (-y olmadan) tekrar çalıştırın."
  exit 1
fi

say "3/5 Eski bot durduruluyor (aynı token'ı yalnız bir program okuyabilir)"
systemctl stop "$NEW" >/dev/null 2>&1 || true
targets="$( { systemctl cat "$OLD" >/dev/null 2>&1 && echo "$OLD"; helper old-units "$ENV_FILE"; } | sort -u )"
if [ -z "$targets" ]; then
  echo "Aynı token ile çalışan eski bir program bulunamadı."
fi
for target in $targets; do
  case "$target" in
    pid:*)
      kill "${target#pid:}" 2>/dev/null && echo "Eski bot süreci durduruldu (PID ${target#pid:})." ;;
    *)
      if [ "$(systemctl show -p Type --value "$target" 2>/dev/null)" = "oneshot" ]; then
        echo "$target zamanlanmış görev; dokunulmadı."
        continue
      fi
      systemctl stop "$target" || true
      systemctl disable "$target" >/dev/null 2>&1 || true
      echo "$target durduruldu (geri almak için: sudo systemctl enable --now $target)." ;;
  esac
done

say "4/5 Sohbet kimliği"
chats="$(value TELEGRAM_ALLOWED_CHAT_IDS)"
if [ -n "$chats" ] && [ "$chats" != "CHANGE_ME" ]; then
  echo "Ayar dosyasında sohbet kimliği zaten var: $chats"
else
  CHAT_WAIT=$([ "$AUTO" -eq 1 ] && echo 900 || echo 180) helper chat-ask "$ENV_FILE"
fi

say "5/5 Yeni servis başlatılıyor"
systemctl enable --now "$NEW"
sleep 8
if journalctl -u "$NEW" -n 60 --no-pager 2>/dev/null | grep -q "TELEGRAM_ERROR_409"; then
  say "⚠️ Başka bir program hâlâ aynı botu okuyor. Bu listenin ekran görüntüsünü gönderin:"
  ps -eo pid,args | grep -i -E 'python|node|bot' | grep -v -E 'grep|okx_grid' \
    | sed -E 's/[0-9]{6,12}:[A-Za-z0-9_-]{30,}/<token>/g' | head -n 15
  exit 1
fi
if systemctl is-active --quiet "$NEW"; then
  say "✅ KURULUM TAMAM"
  echo "Telegram'a 'OKX TR bot başladı' mesajı gelmeli. /yardim yazın."
  echo "Kayıtları izlemek için: sudo journalctl -u $NEW -f"
  echo "Eski bota dönmek için: sudo systemctl disable --now $NEW && sudo systemctl enable --now $OLD"
else
  say "⚠️ Servis başlamadı. Aşağıdaki kayıtların ekran görüntüsünü gönderin:"
  journalctl -u "$NEW" -n 30 --no-pager | grep -v -i token || true
  exit 1
fi
