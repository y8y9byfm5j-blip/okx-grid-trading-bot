#!/usr/bin/env bash
# One short command for a phone console: starts the guided setup in the
# background (survives a closed console) and logs to /root/kur.log.
set -u
cd "$(dirname "$0")" || exit 1
git pull -q 2>/dev/null || echo "(git pull atlandı)"
echo "$(date '+%H:%M:%S') kurulum başlatıldı" > /root/kur.log
setsid nohup bash deploy/kur.sh -y >> /root/kur.log 2>&1 < /dev/null &
echo "Kurulum arka planda başladı."
echo "Şimdi Telegram'dan bota /start yazın (15 dakika beklenir)."
echo "Durumu görmek için: tail -n 15 /root/kur.log"
