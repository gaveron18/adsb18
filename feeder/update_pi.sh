#!/bin/bash
# adsb18 — Синхронизация Pi с репозиторием
#
# Запуск с VPS (туннель уже должен работать):
#   bash feeder/update_pi.sh           — показать diff + применить изменения
#   bash feeder/update_pi.sh --check   — только показать diff, не менять
#
# Что синхронизирует:
#   - feeder.py
#   - adsb-tunnel.service
#   - adsb18-feeder.service
#
# VPS IP и имя фидера читаются из текущих сервисов на Pi (не нужно передавать вручную).

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

# ─────────────────────────────────────────────────────────────────────────────
# Читаем VPS_USER@VPS_IP и FEEDER_NAME из текущих сервисов на Pi
# ─────────────────────────────────────────────────────────────────────────────
VPS_ADDR=$(ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" \
    "grep -oP '[a-z]+@[0-9.]+' /etc/systemd/system/adsb-tunnel.service | head -1")
FEEDER_NAME=$(ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" \
    "grep -oP '(?<=--name )\S+' /etc/systemd/system/adsb18-feeder.service")

echo "  Pi:           $PI_USER@$PI_HOST (port $PI_PORT)"
echo "  VPS:          $VPS_ADDR"
echo "  Feeder name:  $FEEDER_NAME"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Подготавливаем файлы из шаблонов (подставляем реальные значения)
# ─────────────────────────────────────────────────────────────────────────────
TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

cp "$REPO_DIR/feeder.py" "$TMP_DIR/feeder.py"

sed "s/VPS_USER@VPS_IP/$VPS_ADDR/g" \
    "$REPO_DIR/adsb-tunnel.service" > "$TMP_DIR/adsb-tunnel.service"

sed "s/FEEDER_NAME/$FEEDER_NAME/g" \
    "$REPO_DIR/adsb18-feeder.service" > "$TMP_DIR/adsb18-feeder.service"

# ─────────────────────────────────────────────────────────────────────────────
# Скачиваем текущие файлы с Pi для сравнения
# ─────────────────────────────────────────────────────────────────────────────
scp -q -P "$PI_PORT" \
    "$PI_USER@$PI_HOST:/opt/adsb18-feeder/feeder.py" \
    "$TMP_DIR/feeder.py.current"

scp -q -P "$PI_PORT" \
    "$PI_USER@$PI_HOST:/etc/systemd/system/adsb-tunnel.service" \
    "$TMP_DIR/adsb-tunnel.service.current"

scp -q -P "$PI_PORT" \
    "$PI_USER@$PI_HOST:/etc/systemd/system/adsb18-feeder.service" \
    "$TMP_DIR/adsb18-feeder.service.current"

# ─────────────────────────────────────────────────────────────────────────────
# Показываем diff
# ─────────────────────────────────────────────────────────────────────────────
CHANGED=()

for f in feeder.py adsb-tunnel.service adsb18-feeder.service; do
    if diff -q "$TMP_DIR/$f" "$TMP_DIR/$f.current" > /dev/null 2>&1; then
        echo "  ✓ $f — без изменений"
    else
        echo "  ✗ $f — ИЗМЕНЁН:"
        diff "$TMP_DIR/$f.current" "$TMP_DIR/$f" | sed 's/^/      /' || true
        CHANGED+=("$f")
    fi
done

echo ""

if [[ ${#CHANGED[@]} -eq 0 ]]; then
    echo "Pi уже синхронизирован с репозиторием."
    exit 0
fi

if [[ $CHECK_ONLY -eq 1 ]]; then
    echo "Режим --check: изменения не применены."
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────────────
# Применяем изменения
# ─────────────────────────────────────────────────────────────────────────────
echo "Применяем изменения на Pi..."
echo ""

RELOAD_SYSTEMD=0
RESTART_FEEDER=0
RESTART_TUNNEL=0

for f in "${CHANGED[@]}"; do
    case "$f" in
        feeder.py)
            scp -P "$PI_PORT" "$TMP_DIR/feeder.py" "$PI_USER@$PI_HOST:/opt/adsb18-feeder/feeder.py"
            echo "  ✓ feeder.py скопирован"
            RESTART_FEEDER=1
            ;;
        adsb-tunnel.service)
            scp -P "$PI_PORT" "$TMP_DIR/adsb-tunnel.service" "$PI_USER@$PI_HOST:/tmp/adsb-tunnel.service"
            ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" "sudo mv /tmp/adsb-tunnel.service /etc/systemd/system/adsb-tunnel.service"
            echo "  ✓ adsb-tunnel.service обновлён"
            RELOAD_SYSTEMD=1
            RESTART_TUNNEL=1
            ;;
        adsb18-feeder.service)
            scp -P "$PI_PORT" "$TMP_DIR/adsb18-feeder.service" "$PI_USER@$PI_HOST:/tmp/adsb18-feeder.service"
            ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" "sudo mv /tmp/adsb18-feeder.service /etc/systemd/system/adsb18-feeder.service"
            echo "  ✓ adsb18-feeder.service обновлён"
            RELOAD_SYSTEMD=1
            RESTART_FEEDER=1
            ;;
    esac
done

if [[ $RELOAD_SYSTEMD -eq 1 ]]; then
    ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" "sudo systemctl daemon-reload"
    echo "  ✓ systemctl daemon-reload"
fi

# Перезапускаем туннель первым (фидер зависит от него)
if [[ $RESTART_TUNNEL -eq 1 ]]; then
    echo ""
    echo "  Перезапуск adsb-tunnel (SSH отвалится на ~15 сек)..."
    ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" "sudo systemctl restart adsb-tunnel" || true
    sleep 18
    echo "  ✓ adsb-tunnel перезапущен"
fi

if [[ $RESTART_FEEDER -eq 1 ]]; then
    ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" "sudo systemctl restart adsb18-feeder"
    echo "  ✓ adsb18-feeder перезапущен"
fi

echo ""
echo "=== Готово ==="
echo ""
ssh -p "$PI_PORT" "$PI_USER@$PI_HOST" \
    "systemctl is-active adsb-tunnel adsb18-feeder readsb" 2>&1 | \
    paste - - - | awk '{print "  adsb-tunnel: "$1"  adsb18-feeder: "$2"  readsb: "$3}'
