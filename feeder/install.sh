#!/bin/bash
# adsb18 — Установка туннеля на Raspberry Pi (poller-архитектура)
#
# Что нужно перед запуском:
#   - readsb установлен и работает (systemctl is-active readsb)
#   - tar1090 отдаёт aircraft.json на localhost:80/tar1090/data/aircraft.json
#
# Запуск: sudo bash install.sh --vps-ip 173.249.2.184 --vps-user new --http-port 30092 --ssh-port 52222
#
# Для второго VPS (prod):
#   sudo bash install.sh --vps-ip 185.221.160.175 --vps-user root --http-port 30093 --ssh-port 52223 --service-name adsb-tunnel-prod
#
# После установки:
#   Добавь публичный ключ на VPS:
#   cat /home/ads-b/.ssh/id_adsb_vps.pub
#   → на VPS: echo 'КЛЮЧ' >> ~/.ssh/authorized_keys

set -e

VPS_IP=""
VPS_USER="new"
HTTP_PORT="30092"
SSH_PORT="52222"
SERVICE_NAME="adsb-tunnel"
FEEDER_USER="ads-b"
KEY="/home/$FEEDER_USER/.ssh/id_adsb_vps"

while [[ $# -gt 0 ]]; do
    case $1 in
        --vps-ip)       VPS_IP="$2";       shift 2 ;;
        --vps-user)     VPS_USER="$2";     shift 2 ;;
        --http-port)    HTTP_PORT="$2";    shift 2 ;;
        --ssh-port)     SSH_PORT="$2";     shift 2 ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

if [[ -z "$VPS_IP" ]]; then
    echo "Usage: sudo bash install.sh --vps-ip <IP> [--vps-user new] [--http-port 30092] [--ssh-port 52222] [--service-name adsb-tunnel]"
    exit 1
fi

echo "=== adsb18 Pi tunnel install ==="
echo "  VPS:        $VPS_USER@$VPS_IP"
echo "  HTTP port:  $HTTP_PORT  (aircraft.json)"
echo "  SSH port:   $SSH_PORT   (remote SSH to Pi)"
echo "  Service:    $SERVICE_NAME"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Проверка readsb / tar1090
# ─────────────────────────────────────────────────────────────────────────────
echo "[1/4] Checking readsb / tar1090..."
if ! systemctl is-active --quiet readsb; then
    echo "  [WARN] readsb не запущен! Туннель установим, но данных не будет."
    echo "         Установи readsb: https://github.com/wiedehopf/readsb"
else
    echo "  readsb: OK"
fi

if curl -s --max-time 3 http://localhost:80/tar1090/data/aircraft.json | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
    echo "  tar1090 aircraft.json: OK"
else
    echo "  [WARN] tar1090 не отвечает на localhost:80/tar1090/data/aircraft.json"
    echo "         Установи tar1090: https://github.com/wiedehopf/tar1090"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 2. Зависимости
# ─────────────────────────────────────────────────────────────────────────────
echo "[2/4] Installing autossh..."
apt-get update -q
apt-get install -y -q autossh

# ─────────────────────────────────────────────────────────────────────────────
# 3. Пользователь ads-b + SSH ключ
# ─────────────────────────────────────────────────────────────────────────────
echo "[3/4] Setting up user $FEEDER_USER and SSH key..."
id "$FEEDER_USER" &>/dev/null || useradd -m -s /bin/bash "$FEEDER_USER"

if [[ ! -f "$KEY" ]]; then
    sudo -u "$FEEDER_USER" mkdir -p "/home/$FEEDER_USER/.ssh"
    sudo -u "$FEEDER_USER" ssh-keygen -t ed25519 -f "$KEY" -N "" -C "adsb18-pi"
    echo ""
    echo "  *** Публичный ключ — добавь на VPS в ~/.ssh/authorized_keys: ***"
    cat "${KEY}.pub"
    echo ""
else
    echo "  SSH ключ уже существует: $KEY"
    echo "  Публичный ключ:"
    cat "${KEY}.pub"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4. systemd сервис туннеля
# ─────────────────────────────────────────────────────────────────────────────
echo "[4/4] Installing $SERVICE_NAME.service..."

sed \
    -e "s/VPS_USER/$VPS_USER/g" \
    -e "s/VPS_IP/$VPS_IP/g" \
    -e "s/HTTP_PORT/$HTTP_PORT/g" \
    -e "s/SSH_PORT/$SSH_PORT/g" \
    "$(dirname "$0")/adsb-tunnel.service" \
    > /etc/systemd/system/${SERVICE_NAME}.service

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 3

echo ""
echo "=== Done ==="
systemctl is-active "$SERVICE_NAME" && echo "  $SERVICE_NAME: OK" || echo "  $SERVICE_NAME: FAILED"

echo ""
echo "=== ВАЖНО: добавь ключ на VPS ==="
echo "  На VPS ($VPS_USER@$VPS_IP) выполни:"
echo "  echo '$(cat ${KEY}.pub)' >> ~/.ssh/authorized_keys"
echo ""
echo "  После этого проверь туннель:"
echo "  curl -s http://127.0.0.1:$HTTP_PORT/tar1090/data/aircraft.json | head -c 100"
echo ""
echo "Логи: sudo journalctl -u $SERVICE_NAME -f"
