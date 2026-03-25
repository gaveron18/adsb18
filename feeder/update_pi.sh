#!/bin/bash
# adsb18 — Обновление feeder.py на Pi через SSH-туннель
#
# Запуск с VPS (туннель уже должен работать):
#   bash feeder/update_pi.sh
#
# Что делает:
#   1. Копирует feeder.py на Pi
#   2. Перезапускает adsb18-feeder

set -e

PI_USER="ads-b"
PI_PORT="52222"
PI_HOST="127.0.0.1"
REMOTE_PATH="/opt/adsb18-feeder/feeder.py"
LOCAL_PATH="$(dirname "$0")/feeder.py"

echo "=== Updating feeder on Pi ==="
echo "  Local:  $LOCAL_PATH"
echo "  Remote: $PI_USER@$PI_HOST:$REMOTE_PATH (port $PI_PORT)"
echo ""

# Копируем файл
scp -P "$PI_PORT" "$LOCAL_PATH" "$PI_USER@$PI_HOST:$REMOTE_PATH"

# Перезапускаем сервис
ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" "sudo systemctl restart adsb18-feeder && sleep 2 && sudo journalctl -u adsb18-feeder -n 5 --no-pager"

echo ""
echo "=== Done ==="
