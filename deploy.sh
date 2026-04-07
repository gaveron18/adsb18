#!/bin/bash
# adsb18 — Deploy script
# Полная установка с нуля на свежий Ubuntu/Debian сервер.
# Запуск: sudo bash deploy.sh
# После запуска: выполни вручную шаги из раздела "ПОСЛЕ ДЕПЛОЯ"

set -e  # остановиться при любой ошибке

# ─────────────────────────────────────────────────────────────────────────────
# Конфиг — поменяй под свой сервер
# ─────────────────────────────────────────────────────────────────────────────
REPO_URL="https://github.com/gaveron18/adsb18.git"
APP_DIR="/opt/adsb18"                     # папка с репозиторием
VENV_DIR="/opt/adsb18-venv"              # virtualenv
DB_NAME="adsb18"
DB_USER="adsb"
DB_PASS="adsb2024"
DB_URL="postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME"
WEB_PORT="8098"
API_PORT="9001"
PI_TUNNEL_PORT="30093"                   # порт SSH-туннеля Pi→VPS (aircraft.json)

echo "=== adsb18 deploy ==="
echo "App dir: $APP_DIR"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 0. Клонировать репозиторий (если ещё не скачан)
# ─────────────────────────────────────────────────────────────────────────────
echo "[0/8] Cloning repository..."
if [[ ! -d "$APP_DIR/.git" ]]; then
    git clone "$REPO_URL" "$APP_DIR"
    echo "    Cloned into $APP_DIR"
else
    echo "    Already exists, pulling latest..."
    git -C "$APP_DIR" pull
fi

# ─────────────────────────────────────────────────────────────────────────────
# 1. Системные пакеты
# ─────────────────────────────────────────────────────────────────────────────
echo "[1/8] Installing system packages..."
apt-get update -q
apt-get install -y -q \
    python3 python3-venv python3-pip \
    postgresql postgresql-contrib \
    nginx \
    ufw

# ─────────────────────────────────────────────────────────────────────────────
# 2. Python virtualenv
# ─────────────────────────────────────────────────────────────────────────────
echo "[2/8] Creating virtualenv at $VENV_DIR..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q \
    fastapi uvicorn asyncpg websockets

# ─────────────────────────────────────────────────────────────────────────────
# 3. PostgreSQL — создать БД и пользователя
# ─────────────────────────────────────────────────────────────────────────────
echo "[3/8] Setting up PostgreSQL..."
systemctl enable --now postgresql

sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';"

sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"

# Применить схему
sudo -u postgres psql -d "$DB_NAME" < "$APP_DIR/server/db/init.sql"

# Выдать права (включая будущие таблицы)
sudo -u postgres psql -d "$DB_NAME" <<SQL
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $DB_USER;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $DB_USER;
SQL

echo "    PostgreSQL ready."

# ─────────────────────────────────────────────────────────────────────────────
# 5. systemd сервисы
# ─────────────────────────────────────────────────────────────────────────────
echo "[5/8] Installing systemd services..."

cat > /etc/systemd/system/adsb18-api.service <<EOF
[Unit]
Description=adsb18 API Server
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=root
WorkingDirectory=$APP_DIR/server/api
ExecStart=$VENV_DIR/bin/uvicorn main:app --host 127.0.0.1 --port $API_PORT
Restart=on-failure
RestartSec=5
Environment=DATABASE_URL=$DB_URL
Environment=PI_AIRCRAFT_URL=http://127.0.0.1:$PI_TUNNEL_PORT/tar1090/data/aircraft.json
Environment=PI_SSH_PORT=52223
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/adsb18-poller.service <<EOF
[Unit]
Description=adsb18 Poller (aircraft.json -> PostgreSQL)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=root
WorkingDirectory=$APP_DIR/server/ingest
ExecStart=$VENV_DIR/bin/python poller.py
Restart=on-failure
RestartSec=5
Environment=DATABASE_URL=$DB_URL
Environment=PI_AIRCRAFT_URL=http://127.0.0.1:$PI_TUNNEL_PORT/tar1090/data/aircraft.json
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable adsb18-api adsb18-poller
systemctl restart adsb18-api adsb18-poller
echo "    Services started."

# ─────────────────────────────────────────────────────────────────────────────
# 6. nginx — используем nginx.conf из репо, заменяем пути и порт туннеля
# ─────────────────────────────────────────────────────────────────────────────
echo "[6/8] Configuring nginx..."

# Заменяем пути /home/new/adsb18 -> /opt/adsb18 и порт туннеля 30092 -> 30093
sed \
    -e "s|/home/new/adsb18|$APP_DIR|g" \
    -e "s|:30092|:$PI_TUNNEL_PORT|g" \
    "$APP_DIR/nginx.conf" > /etc/nginx/sites-available/adsb18

ln -sf /etc/nginx/sites-available/adsb18 /etc/nginx/sites-enabled/adsb18
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx
echo "    nginx ready."

# ─────────────────────────────────────────────────────────────────────────────
# 7. Файрвол (UFW)
# ─────────────────────────────────────────────────────────────────────────────
echo "[7/8] Configuring UFW firewall..."
ufw --force enable
ufw allow 22/tcp  comment 'SSH'
ufw allow "$WEB_PORT/tcp" comment 'adsb18 web interface'
ufw reload
echo "    UFW ready. Open ports: 22, $WEB_PORT"

# ─────────────────────────────────────────────────────────────────────────────
# 8. Проверка
# ─────────────────────────────────────────────────────────────────────────────
echo "[8/8] Checking services..."
sleep 3

STATUS_POLLER=$(systemctl is-active adsb18-poller)
STATUS_API=$(systemctl is-active adsb18-api)
STATUS_NGINX=$(systemctl is-active nginx)
STATUS_PG=$(systemctl is-active postgresql)

echo ""
echo "=== Status ==="
echo "  postgresql:      $STATUS_PG"
echo "  adsb18-poller:   $STATUS_POLLER"
echo "  adsb18-api:      $STATUS_API"
echo "  nginx:           $STATUS_NGINX"

SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=== Done ==="
echo "  Web: http://$SERVER_IP:$WEB_PORT"
echo "  API: http://$SERVER_IP:$WEB_PORT/api/docs"
echo ""
echo "=== ПОСЛЕ ДЕПЛОЯ — подключи Raspberry Pi ==="
echo ""
echo "  Pi должен иметь SSH-туннель на этот сервер."
echo "  Туннель пробрасывает aircraft.json Pi -> порт $PI_TUNNEL_PORT на VPS."
echo ""
echo "  1. Добавь публичный ключ Pi на этот сервер:"
echo "     ssh -p 52222 ads-b@127.0.0.1 'cat /home/ads-b/.ssh/id_adsb_vps.pub'"
echo "     echo 'ПУБЛИЧНЫЙ_КЛЮЧ' >> /root/.ssh/authorized_keys"
echo ""
echo "  2. На Pi создай сервис туннеля на этот VPS:"
echo "     Файл: /etc/systemd/system/adsb-tunnel-prod.service"
echo "     ExecStart: autossh ... -R 0.0.0.0:$PI_TUNNEL_PORT:localhost:80 root@$SERVER_IP"
echo "     sudo systemctl enable --now adsb-tunnel-prod"
echo ""
echo "  3. Проверь туннель:"
echo "     curl -s http://127.0.0.1:$PI_TUNNEL_PORT/tar1090/data/aircraft.json | head -c 200"
echo ""
echo "=== ПЕРЕПОДКЛЮЧЕНИЕ Pi НА ДРУГОЙ СЕРВЕР ==="
echo ""
echo "  1. Добавь публичный ключ Pi на новый сервер (см. п.1 выше)"
echo ""
echo "  2. На Pi поменяй IP в сервисе туннеля:"
echo "     ssh -p 52222 ads-b@127.0.0.1"
echo "     sudo nano /etc/systemd/system/adsb-tunnel-prod.service"
echo "     sudo systemctl daemon-reload && sudo systemctl restart adsb-tunnel-prod"
