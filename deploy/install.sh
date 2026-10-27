#!/usr/bin/env bash
# Install or update the OKX TR Telegram bot on the VPS. Run as root from the
# repository checkout: sudo bash deploy/install.sh
# It never starts the service and never stops the old one; deploy/kur.sh does
# that so the single Telegram consumer rule is respected.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/okx-tr-bot
ENV_FILE=/etc/okx-tr-bot.env

if [ "$(id -u)" -ne 0 ]; then
  echo "root olarak çalıştırın: sudo bash deploy/install.sh" >&2
  exit 1
fi
PY=""
for candidate in python3.13 python3.12 python3.11 python3; do
  path="$(command -v "$candidate" || true)"
  if [ -n "$path" ] && "$path" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
    PY="$path"
    break
  fi
done
[ -n "$PY" ] || { echo "Python 3.11 veya üstü bulunamadı (sudo apt install python3.12)" >&2; exit 1; }

id okxtrbot >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin okxtrbot

mkdir -p "$APP"
# Only the application package is installed; tests, docs and git data stay out.
rm -rf "$APP/okx_grid.new"
cp -a "$SRC/okx_grid" "$APP/okx_grid.new"
find "$APP/okx_grid.new" -name '__pycache__' -prune -exec rm -rf {} +
[ -d "$APP/okx_grid" ] && rm -rf "$APP/okx_grid.old" && mv "$APP/okx_grid" "$APP/okx_grid.old"
mv "$APP/okx_grid.new" "$APP/okx_grid"
echo "$PY" > "$APP/PYTHON"
chown -R root:root "$APP"
chmod -R a+rX "$APP"

if [ ! -f "$ENV_FILE" ]; then
  install -m 600 -o root -g root "$SRC/deploy/okx-tr-bot.env.example" "$ENV_FILE"
  echo "Ayar dosyası oluşturuldu: $ENV_FILE  (nano $ENV_FILE ile doldurun)"
fi
chmod 600 "$ENV_FILE"

sed "s#^ExecStart=/usr/bin/python3 #ExecStart=$PY #" "$SRC/deploy/okx-tr-bot.service" > /etc/systemd/system/okx-tr-bot.service
chmod 644 /etc/systemd/system/okx-tr-bot.service
systemctl daemon-reload

(cd "$APP" && "$PY" -c 'import okx_grid.live_bot') && echo "Kod kuruldu: $APP ($PY)"
