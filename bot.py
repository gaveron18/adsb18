#!/usr/bin/env python3
"""
adsb18 Telegram bot — управление уведомлениями мониторинга.

Команды:
  /enable  — включить уведомления о проблемах
  /disable — выключить уведомления
  /status  — показать состояние системы прямо сейчас

Переменные окружения (из /etc/adsb18-bot.env):
  TG_TOKEN   — токен Telegram бота
  TG_CHAT    — chat_id (принимать команды только от него)
  DB_DSN     — строка подключения к PostgreSQL
  TUNNEL_URL — URL aircraft.json через туннель Pi
  API_URL    — URL API сервера
"""

import os
import sys
import json
import time
import threading
import urllib.request
import urllib.parse
import urllib.error
import psycopg2
from datetime import datetime, timezone, timedelta

# ── Настройки ──────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ['TG_TOKEN']
TG_CHAT    = os.environ['TG_CHAT']
DB_DSN     = os.environ.get('DB_DSN',     'postgresql://adsb:adsb2024@localhost:5432/adsb18')
API_URL    = os.environ.get('API_URL',    'http://127.0.0.1:9001/api/feeders')
TUNNEL_URL = os.environ.get('TUNNEL_URL', 'http://127.0.0.1:30093/tar1090/data/aircraft.json')
STATE_FILE = os.environ.get('STATE_FILE', '/opt/adsb18/.bot_enabled')
CHECK_INTERVAL   = 300  # секунд между проверками
MAX_DATA_AGE_MIN = 10
MAX_FEEDER_AGE_MIN = 15

# ── Состояние (файл-флаг) ──────────────────────────────────────────────────────
def is_enabled():
    return os.path.exists(STATE_FILE)

def set_enabled(value: bool):
    if value:
        open(STATE_FILE, 'w').close()
    elif os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

# ── Telegram API ───────────────────────────────────────────────────────────────
def tg_request(method, **params):
    url = f'https://api.telegram.org/bot{TG_TOKEN}/{method}'
    data = urllib.parse.urlencode(params).encode()
    try:
        req = urllib.request.Request(url, data=data, method='POST')
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        print(f'Telegram error ({method}): {e}', file=sys.stderr)
        return None

def send_message(text):
    tg_request('sendMessage', chat_id=TG_CHAT, text=text, parse_mode='HTML')

def register_commands():
    tg_request('setMyCommands', commands=json.dumps([
        {'command': 'enable',  'description': 'Включить уведомления о проблемах'},
        {'command': 'disable', 'description': 'Выключить уведомления'},
        {'command': 'status',  'description': 'Показать состояние системы прямо сейчас'},
    ]))

# ── Проверки ───────────────────────────────────────────────────────────────────
def run_checks():
    errors = []
    info   = []

    # 1. API
    try:
        urllib.request.urlopen(API_URL, timeout=5)
        info.append('✅ API — работает')
    except Exception as e:
        errors.append(f'❌ API недоступен: {e}')

    # 2. Свежесть данных в БД
    try:
        conn = psycopg2.connect(DB_DSN)
        cur  = conn.cursor()
        cur.execute("SELECT max(ts) FROM positions")
        last_pos = cur.fetchone()[0]
        if last_pos is None:
            errors.append('❌ В БД нет позиций')
        else:
            age  = datetime.now(timezone.utc) - last_pos.astimezone(timezone.utc)
            mins = int(age.total_seconds() // 60)
            if age > timedelta(minutes=MAX_DATA_AGE_MIN):
                errors.append(f'❌ Последняя позиция {mins} мин назад')
            else:
                info.append(f'✅ Позиции — {mins} мин назад')
        cur.close()
        conn.close()
    except Exception as e:
        errors.append(f'❌ БД недоступна: {e}')

    # 3. Туннель Pi
    try:
        urllib.request.urlopen(TUNNEL_URL, timeout=5)
        info.append('✅ Туннель Pi — работает')
    except Exception:
        errors.append('⚠️ Туннель Pi недоступен (карта работает через БД)')

    return errors, info

def format_status():
    errors, info = run_checks()
    state = '🟢 Уведомления включены' if is_enabled() else '⚪️ Уведомления выключены'
    now   = datetime.now().strftime('%H:%M:%S')
    lines = info + (errors if errors else ['✅ Всё в порядке'])
    return f'<b>adsb18 [{now}]</b>\n{state}\n\n' + '\n'.join(lines)

# ── Фоновый мониторинг ─────────────────────────────────────────────────────────
def monitor_loop():
    while True:
        time.sleep(CHECK_INTERVAL)
        if not is_enabled():
            continue
        errors, _ = run_checks()
        if errors:
            send_message('🔴 <b>adsb18 ALERT</b>\n' + '\n'.join(errors))

# ── Обработка команд ───────────────────────────────────────────────────────────
def handle_update(update):
    msg = update.get('message') or update.get('edited_message')
    if not msg:
        return
    if str(msg.get('chat', {}).get('id', '')) != TG_CHAT:
        return
    text = msg.get('text', '').strip().split('@')[0]

    if text == '/enable':
        set_enabled(True)
        send_message('🟢 Уведомления <b>включены</b>. Буду сообщать о проблемах каждые 5 минут.')
    elif text == '/disable':
        set_enabled(False)
        send_message('⚪️ Уведомления <b>выключены</b>.')
    elif text == '/status':
        send_message(format_status())

# ── Polling ────────────────────────────────────────────────────────────────────
def polling_loop():
    offset = 0
    while True:
        result = tg_request('getUpdates', offset=offset, timeout=30)
        if not result or not result.get('ok'):
            time.sleep(5)
            continue
        for update in result.get('result', []):
            offset = update['update_id'] + 1
            try:
                handle_update(update)
            except Exception as e:
                print(f'Handle error: {e}', file=sys.stderr)

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f'adsb18 bot starting... notifications={"on" if is_enabled() else "off"}')
    register_commands()
    threading.Thread(target=monitor_loop, daemon=True).start()
    polling_loop()
