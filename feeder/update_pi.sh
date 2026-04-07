#!/bin/bash
# adsb18 — Синхронизация Pi с репозиторием
#
# Запуск с VPS (туннель уже должен работать):
#   bash feeder/update_pi.sh           — показать diff + применить изменения
#   bash feeder/update_pi.sh --check   — только показать diff, не менять
#
# Что синхронизирует:
#   - adsb-tunnel.service
#
# VPS адрес читается из текущего сервиса на Pi (не нужно передавать вручную).

set -e

PI_USER="ads-b"
PI_PORT="52222"
PI_HOST="127.0.0.1"
REPO_DIR="$(dirname "$0")"
CHECK_ONLY=0

if [[ "${1:-}" == "--check" ]]; then
    CHECK_ONLY=1
fi

echo "=== adsb18 Pi sync ==="
echo ""

VPS_ADDR=$(ssh -p "$PI_PORT" "$PI_USER@$PI_HOST"     "grep -oP '[a-z]+@[0-9.]+' /etc/systemd/system/adsb-tunnel.service | head -1")

echo "  Pi:   $PI_USER@$PI_HOST (port $PI_PORT)"
echo "  VPS:  $VPS_ADDR"
echo ""

TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

sed "s/VPS_USER@VPS_IP/$VPS_ADDR/g"     "$REPO_DIR/adsb-tunnel.service" > "$TMP_DIR/adsb-tunnel.service"

scp -q -P "$PI_PORT"     "$PI_USER@$PI_HOST:/etc/systemd/system/adsb-tunnel.service"     "$TMP_DIR/adsb-tunnel.service.current"

if diff -q "$TMP_DIR/adsb-tunnel.service" "$TMP_DIR/adsb-tunnel.service.current" > /dev/null 2>&1; then
    echo "  ✓ adsb-tunnel.service — без изменений"
    echo ""
    echo "Pi уже синхронизирован с репозиторием."
    exit 0
else
    echo "  ✗ adsb-tunnel.service — ИЗМЕНЁН:"
    diff "$TMP_DIR/adsb-tunnel.service.current" "$TMP_DIR/adsb-tunnel.service" | sed 's/^/      /' || true
    echo ""
fi

if [[ $CHECK_ONLY -eq 1 ]]; then
    echo "Режим --check: изменения не применены."
    exit 0
fi

echo "Применяем изменения на Pi..."
echo ""

scp -P "$PI_PORT" "$TMP_DIR/adsb-tunnel.service" "$PI_USER@$PI_HOST:/tmp/adsb-tunnel.service"
ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" "sudo mv /tmp/adsb-tunnel.service /etc/systemd/system/adsb-tunnel.service && sudo systemctl daemon-reload"
echo "  ✓ adsb-tunnel.service обновлён"

echo ""
echo "  Перезапуск adsb-tunnel (SSH отвалится на ~15 сек)..."
ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" "sudo systemctl restart adsb-tunnel" || true
sleep 18
echo "  ✓ adsb-tunnel перезапущен"

echo ""
echo "=== Готово ==="
echo ""
ssh -p "$PI_PORT" "$PI_USER@$PI_HOST"     "systemctl is-active adsb-tunnel readsb" 2>&1 |     paste - - | awk '{print "  adsb-tunnel: "$1"  readsb: "$2}'
