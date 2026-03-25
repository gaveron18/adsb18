#!/bin/bash
# adsb18 — Установка фидера на Raspberry Pi
#
# Запуск: sudo bash install.sh --vps-ip 173.249.2.184 --vps-user new --name ads-b-pi
#
# Что делает:
#   1. Устанавливает зависимости (autossh, python3)
#   2. Генерирует SSH-ключ для туннеля (если нет)
#   3. Копирует feeder.py в /opt/adsb18-feeder/
#   4. Устанавливает systemd-сервисы: adsb-tunnel + adsb18-feeder
#   5. Запускает всё
#
# После установки:
#   Скопируй публичный ключ на VPS:
#   cat /home/ads-b/.ssh/id_adsb_vps.pub
#   → добавь в ~/.ssh/authorized_keys на VPS

set -e

VPS_IP=""
VPS_USER="new"
FEEDER_NAME=""
FEEDER_USER="ads-b"
INSTALL_DIR="/opt/adsb18-feeder"

while [[ $# -gt 0 ]]; do
    case $1 in
        --vps-ip)   VPS_IP="$2";     shift 2 ;;
        --vps-user) VPS_USER="$2";   shift 2 ;;
        --name)     FEEDER_NAME="$2"; shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

if [[ -z "$VPS_IP" ]]; then
    echo "Usage: sudo bash install.sh --vps-ip <IP> [--vps-user new] [--name ads-b-pi]"
    exit 1
fi

FEEDER_NAME="${FEEDER_NAME:-$(hostname)}"

echo "=== adsb18 feeder install ==="
echo "  VPS:  $VPS_USER@$VPS_IP"
echo "  Name: $FEEDER_NAME"
echo "  Dir:  $INSTALL_DIR"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Зависимости
# ─────────────────────────────────────────────────────────────────────────────
echo "[1/5] Installing dependencies..."
apt-get update -q
apt-get install -y -q autossh python3

# ─────────────────────────────────────────────────────────────────────────────
# 2. Пользователь ads-b (если нет)
# ─────────────────────────────────────────────────────────────────────────────
echo "[2/5] Setting up user $FEEDER_USER..."
id "$FEEDER_USER" &>/dev/null || useradd -m -s /bin/bash "$FEEDER_USER"

# SSH-ключ для туннеля
KEY="/home/$FEEDER_USER/.ssh/id_adsb_vps"
if [[ ! -f "$KEY" ]]; then
    sudo -u "$FEEDER_USER" mkdir -p "/home/$FEEDER_USER/.ssh"
    sudo -u "$FEEDER_USER" ssh-keygen -t ed25519 -f "$KEY" -N "" -C "adsb18-feeder"
    echo ""
    echo "  *** Публичный ключ (добавь на VPS в ~/.ssh/authorized_keys): ***"
    cat "${KEY}.pub"
    echo ""
fi

# ─────────────────────────────────────────────────────────────────────────────
# 3. Копируем feeder.py
# ─────────────────────────────────────────────────────────────────────────────
echo "[3/5] Installing feeder.py..."
mkdir -p "$INSTALL_DIR"
cp "$(dirname "$0")/feeder.py" "$INSTALL_DIR/feeder.py"
chown -R "$FEEDER_USER:$FEEDER_USER" "$INSTALL_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# 4. systemd сервисы
# ─────────────────────────────────────────────────────────────────────────────
echo "[4/5] Installing systemd services..."

sed "s/VPS_USER@VPS_IP/$VPS_USER@$VPS_IP/g" \
    "$(dirname "$0")/adsb-tunnel.service" \
    > /etc/systemd/system/adsb-tunnel.service

sed "s/FEEDER_NAME/$FEEDER_NAME/g" \
    "$(dirname "$0")/adsb18-feeder.service" \
    > /etc/systemd/system/adsb18-feeder.service

systemctl daemon-reload
systemctl enable adsb-tunnel adsb18-feeder

# ─────────────────────────────────────────────────────────────────────────────
# 5. Запуск
# ─────────────────────────────────────────────────────────────────────────────
echo "[5/5] Starting services..."
systemctl restart adsb-tunnel
sleep 3
systemctl restart adsb18-feeder

echo ""
echo "=== Done ==="
systemctl is-active adsb-tunnel    && echo "  adsb-tunnel:     OK" || echo "  adsb-tunnel:     FAILED"
systemctl is-active adsb18-feeder  && echo "  adsb18-feeder:   OK" || echo "  adsb18-feeder:   FAILED"

echo ""
echo "=== ВАЖНО: добавь ключ на VPS ==="
echo "  cat ${KEY}.pub"
echo "  → скопируй на VPS: echo 'КЛЮЧ' >> ~/.ssh/authorized_keys"
echo ""
echo "Логи туннеля:  sudo journalctl -u adsb-tunnel -f"
echo "Логи фидера:   sudo journalctl -u adsb18-feeder -f"
