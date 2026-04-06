#!/usr/bin/env python3
"""
adsb18 healthcheck — запускается каждые 5 минут через cron.
Проверяет: API, свежесть данных в БД, фидер, туннель Pi.
Алерт в Telegram при любой проблеме.
"""
import sys
import urllib.request
import urllib.error
import urllib.parse
import psycopg2
from datetime import datetime, timezone, timedelta

# ── Настройки ──────────────────────────────────────────────────────────────────
TG_TOKEN  = '8490641093:AAH39HUJQTZkGS9O1BkEkNE_3GFvXN6IoK0'
TG_CHAT   = '357650937'
DB_DSN    = 'postgresql://adsb:adsb2024@localhost:5432/adsb18'
API_URL   = 'http://127.0.0.1:9001/api/feeders'
TUNNEL_URL = 'http://127.0.0.1:30092/tar1090/data/aircraft.json'
MAX_DATA_AGE_MIN  = 10   # алерт если последняя позиция старше N минут
MAX_FEEDER_AGE_MIN = 15  # алерт если фидер не подключался N минут

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_alert(text):
    url = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
    data = f'chat_id={TG_CHAT}&text={urllib.parse.quote(text)}&parse_mode=HTML'
    try:
        req = urllib.request.Request(url, data=data.encode(), method='POST')
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f'Telegram error: {e}', file=sys.stderr)

# ── Проверки ───────────────────────────────────────────────────────────────────
errors = []

# 1. API живой?
try:
    urllib.request.urlopen(API_URL, timeout=5)
except Exception as e:
    errors.append(f'❌ API недоступен: {e}')

# 2. Свежесть данных в БД + фидер
try:
    conn = psycopg2.connect(DB_DSN)
    cur  = conn.cursor()

    # последняя позиция
    cur.execute("SELECT max(ts) FROM positions")
    last_pos = cur.fetchone()[0]
    if last_pos is None:
        errors.append('❌ В БД нет позиций')
    else:
        age = datetime.now(timezone.utc) - last_pos.astimezone(timezone.utc)
        if age > timedelta(minutes=MAX_DATA_AGE_MIN):
            errors.append(f'❌ Последняя позиция {int(age.total_seconds()//60)} мин назад')

    # фидер
    cur.execute("SELECT name, last_connected FROM feeders ORDER BY last_connected DESC LIMIT 1")
    row = cur.fetchone()
    if row is None:
        errors.append('❌ Нет фидеров в БД')
    else:
        name, last_conn = row
        if last_conn:
            age = datetime.now(timezone.utc) - last_conn.astimezone(timezone.utc)
            if age > timedelta(minutes=MAX_FEEDER_AGE_MIN):
                errors.append(f'❌ Фидер {name} не подключался {int(age.total_seconds()//60)} мин')

    cur.close()
    conn.close()
except Exception as e:
    errors.append(f'❌ БД недоступна: {e}')

# 3. Туннель Pi живой?
try:
    urllib.request.urlopen(TUNNEL_URL, timeout=5)
except Exception:
    errors.append('⚠️ Туннель Pi недоступен (карта работает через БД)')

# ── Результат ──────────────────────────────────────────────────────────────────
if errors:
    msg = '🔴 <b>adsb18 ALERT</b>\n' + '\n'.join(errors)
    send_alert(msg)
    print(msg)
    sys.exit(1)
else:
    print(f'OK [{datetime.now().strftime("%H:%M:%S")}]')
