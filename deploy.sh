#!/bin/bash
# adsb18 — Deploy script
# Полная установка с нуля на свежий Ubuntu/Debian сервер.
# Запуск: bash deploy.sh
# После запуска: выполни вручную шаги из раздела "ПОСЛЕ ДЕПЛОЯ"

set -e  # остановиться при любой ошибке

# ─────────────────────────────────────────────────────────────────────────────
# Конфиг — поменяй под свой сервер
# ─────────────────────────────────────────────────────────────────────────────
DEPLOY_USER="${SUDO_USER:-$USER}"          # пользователь от которого запущен скрипт
APP_DIR="/home/$DEPLOY_USER/adsb18"        # папка с репозиторием
VENV_DIR="/opt/adsb18-venv"               # virtualenv
DB_NAME="adsb18"
DB_USER="adsb"
DB_PASS="adsb2024"
DB_URL="postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME"
WEB_PORT="8098"
API_PORT="9001"
INGEST_PORT="30001"

echo "=== adsb18 deploy ==="
echo "User:    $DEPLOY_USER"
echo "App dir: $APP_DIR"
echo ""

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
# 4. Права на домашнюю директорию (иначе nginx не сможет читать фронтенд)
# ─────────────────────────────────────────────────────────────────────────────
echo "[4/8] Fixing home directory permissions for nginx..."
chmod o+x "/home/$DEPLOY_USER"

# ─────────────────────────────────────────────────────────────────────────────
# 5. systemd сервисы
# ─────────────────────────────────────────────────────────────────────────────
echo "[5/8] Installing systemd services..."

cat > /etc/systemd/system/adsb18-ingest.service <<EOF
[Unit]
Description=adsb18 Ingest Server
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=$DEPLOY_USER
WorkingDirectory=$APP_DIR/server/ingest
ExecStart=$VENV_DIR/bin/python main.py
Restart=on-failure
RestartSec=5
Environment=DATABASE_URL=$DB_URL
Environment=INGEST_HOST=0.0.0.0
Environment=INGEST_PORT=$INGEST_PORT
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/adsb18-api.service <<EOF
[Unit]
Description=adsb18 API Server
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=$DEPLOY_USER
WorkingDirectory=$APP_DIR/server/api
ExecStart=$VENV_DIR/bin/uvicorn main:app --host 127.0.0.1 --port $API_PORT
Restart=on-failure
RestartSec=5
Environment=DATABASE_URL=$DB_URL
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable adsb18-ingest adsb18-api
systemctl restart adsb18-ingest adsb18-api
echo "    Services started."

# ─────────────────────────────────────────────────────────────────────────────
# 6. nginx
# ─────────────────────────────────────────────────────────────────────────────
echo "[6/8] Configuring nginx..."

cat > /etc/nginx/sites-available/adsb18 <<EOF
server {
    listen $WEB_PORT;

    location / {
        root $APP_DIR/frontend;
        index index.html;
        try_files \$uri \$uri/ /index.html;
    }

    location /data/ {
        proxy_pass http://127.0.0.1:$API_PORT/data/;
        proxy_cache_bypass 1;
        add_header Cache-Control "no-cache";
    }

    location /api/ {
        proxy_pass http://127.0.0.1:$API_PORT/api/;
    }

    location /ws {
        proxy_pass http://127.0.0.1:$API_PORT/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF

ln -sf /etc/nginx/sites-available/adsb18 /etc/nginx/sites-enabled/adsb18
nginx -t
systemctl enable --now nginx
systemctl reload nginx
echo "    nginx ready."

# ─────────────────────────────────────────────────────────────────────────────
# 7. Файрвол (UFW)
# ─────────────────────────────────────────────────────────────────────────────
echo "[7/8] Configuring UFW firewall..."
ufw --force enable
ufw allow 22/tcp    comment 'SSH'
ufw allow "$WEB_PORT/tcp"    comment 'adsb18 web interface'
ufw allow "$INGEST_PORT/tcp" comment 'adsb18 ingest'
ufw reload
echo "    UFW ready. Open ports: 22, $WEB_PORT, $INGEST_PORT"

# ─────────────────────────────────────────────────────────────────────────────
# 8. Проверка
# ─────────────────────────────────────────────────────────────────────────────
echo "[8/8] Checking services..."
sleep 3

STATUS_INGEST=$(systemctl is-active adsb18-ingest)
STATUS_API=$(systemctl is-active adsb18-api)
STATUS_NGINX=$(systemctl is-active nginx)
STATUS_PG=$(systemctl is-active postgresql)

echo ""
echo "=== Status ==="
echo "  postgresql:     $STATUS_PG"
echo "  adsb18-ingest:  $STATUS_INGEST"
echo "  adsb18-api:     $STATUS_API"
echo "  nginx:          $STATUS_NGINX"

SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=== Done ==="
echo "  Web: http://$SERVER_IP:$WEB_PORT"
echo "  API: http://$SERVER_IP:$WEB_PORT/api/docs"
echo ""
echo "=== ПОСЛЕ ДЕПЛОЯ — настрой Raspberry Pi ==="
echo ""
echo "  1. Скопируй на Pi файл feeder/feeder.py из репозитория"
echo ""
echo "  2. Создай на Pi /etc/systemd/system/adsb-tunnel.service:"
echo "     (замени YOUR_VPS_IP на IP этого сервера, YOUR_KEY на путь к ключу)"
cat <<'PIEOF'

     [Unit]
     Description=Reverse SSH tunnel to VPS
     After=network-online.target
     Wants=network-online.target

     [Service]
     User=ads-b
     ExecStart=/usr/bin/autossh -M 0 -N \
       -o ServerAliveInterval=30 \
       -o ServerAliveCountMax=3 \
       -o StrictHostKeyChecking=no \
       -o ExitOnForwardFailure=yes \
       -i /home/ads-b/.ssh/id_adsb_vps \
       -R 52222:localhost:22 \
       -L 30091:localhost:30001 \
       new@YOUR_VPS_IP
     Restart=always
     RestartSec=10

     [Install]
     WantedBy=multi-user.target

PIEOF
echo "  3. Создай на Pi /etc/systemd/system/adsb18-feeder.service:"
cat <<'PIEOF'

     [Unit]
     Description=adsb18 ADS-B Feeder
     After=network-online.target adsb-tunnel.service
     Wants=network-online.target

     [Service]
     ExecStart=/usr/bin/python3 /home/ads-b/feeder.py --server 127.0.0.1 --port 30091 --name ads-b-pi
     Restart=always
     RestartSec=10
     User=ads-b

     [Install]
     WantedBy=multi-user.target

PIEOF
echo "  4. На Pi:"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable --now adsb-tunnel adsb18-feeder"
echo ""
echo "=== ПЕРЕПОДКЛЮЧЕНИЕ Pi НА ДРУГОЙ СЕРВЕР ==="
echo ""
echo "  Если меняешь IP сервера (переезд на новый VPS):"
echo ""
echo "  1. Добавь публичный ключ Pi на новый сервер:"
echo "     # Посмотри ключ Pi:"
echo "     ssh -p 52222 ads-b@127.0.0.1 'cat /home/ads-b/.ssh/id_adsb_vps.pub'"
echo "     # Добавь его на новом сервере:"
echo "     echo 'ПУБЛИЧНЫЙ_КЛЮЧ' >> ~/.ssh/authorized_keys"
echo ""
echo "  2. Поменяй IP в tunnel-сервисе на Pi:"
echo "     ssh -p 52222 ads-b@127.0.0.1"
echo "     sudo nano /etc/systemd/system/adsb-tunnel.service"
echo "     # Поменять: new@СТАРЫЙ_IP → new@НОВЫЙ_IP"
echo "     sudo systemctl daemon-reload && sudo systemctl restart adsb-tunnel"
echo ""
echo "  3. Фидер переподключится автоматически (After=adsb-tunnel.service)"
