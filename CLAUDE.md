# CLAUDE.md — adsb18

Этот файл читается автоматически при запуске Claude из папки проекта.
Общайся по-русски. Имя пользователя: Андрей.

---

## Проект

ADS-B сервер сбора и визуализации данных о воздушных судах.

- **VPS:** `new@173.249.2.184` · `/home/new/adsb18/`
- **GitHub:** https://github.com/gaveron18/adsb18
- **Веб:** http://173.249.2.184:8098
- **Стек:** Python 3.12, FastAPI, asyncpg, PostgreSQL 16, nginx, systemd

---

## Обязательно при старте сессии

1. `ssh new@173.249.2.184 "cat /home/new/adsb18/ARCHITECTURE.md"`
2. `ssh new@173.249.2.184 "cat /home/new/adsb18/TROUBLESHOOTING.md"`
3. `ssh new@173.249.2.184 "ls /home/new/adsb18/docs/session_*.md | tail -1 | xargs cat"`

---

## Архитектура (актуальная — poller режим)

```
[RTL-SDR на Pi] → [readsb] → aircraft.json (:80)
                                    ↓ SSH reverse tunnel (-R 30092:localhost:80)
                             VPS :30092 (HTTP туннель)
                                    ↓ curl каждую секунду
                             [poller.py] → process_snapshot()
                                    ↓
                             [PostgreSQL 16]
                                    ↓
                             [FastAPI :9001]
                                    ↓
                             [nginx :8098]
```

---

## Сервисы (systemd, НЕ Docker)

| Сервис | Порт | Примечание |
|--------|------|------------|
| PostgreSQL 16 | 5432 | running, enabled |
| adsb18-poller | — | **ОСНОВНОЙ** — опрашивает Pi aircraft.json каждую секунду |
| adsb18-api | 9001 | FastAPI |
| nginx | 8098 | отдаёт frontend + проксирует /api/ и /data/ |
| adsb18-ingest | 30001 | **MASKED → /dev/null** — не запускать! |
| adsb18-feeder (Pi) | — | disabled — не нужен при poller-архитектуре |

Перезапуск после изменений:
```bash
sudo systemctl restart adsb18-poller
sudo systemctl restart adsb18-api
```

Логи:
```bash
sudo journalctl -u adsb18-api -f
sudo journalctl -u adsb18-poller -f
```

---

## Raspberry Pi (фидер)

- Подключиться с VPS: `ssh -p 52222 ads-b@127.0.0.1`
- Туннель Pi→VPS: порт 30092 (HTTP, aircraft.json), порт 52222 (SSH)
- readsb отдаёт aircraft.json на Pi:80/tar1090/data/aircraft.json
- Туннель на Pi: systemd-сервис `adsb-tunnel`

### tar1090 настройки Pi (`/etc/default/tar1090`)
- INTERVAL=8 (сек между снапшотами)
- HISTORY_SIZE=450 (1 час в основном интерфейсе)
- PTRACKS=8 (часов для /?pTracks) — **отложено: увеличить до 24**
- CHUNK_SIZE=20, GZIP_LVL=1

---

## Структура файлов

```
server/
  ingest/
    main.py       — TCP-сервер AUTH/AUTH-JSON (ЗАМЕНЁН поллером, не используется)
    sbs_parser.py — парсит SBS строки → SBSMessage dataclass
    db.py         — буфер + PostgreSQL writer, process_snapshot()
    poller.py     — curl aircraft.json каждую секунду, пишет в БД
  api/
    main.py       — FastAPI: /data/aircraft.json, /api/history, /api/feeders,
                    /api/archive, DELETE /api/flight, /ws, /api/monitor
  db/
    init.sql      — схема БД: positions (партиц.), aircraft, feeders
feeder/
  feeder.py       — SBS-режим (НЕ используется при poller-архитектуре)
  update_pi.sh    — деплой изменений на Pi
frontend/
  archive.html    — страница архива рейсов (Leaflet)
  index.html      — живая карта (OpenLayers / tar1090)
nginx.conf
healthcheck.py    — healthcheck с Telegram алертами
TESTING.md        — чек-лист ручного тестирования archive.html
TROUBLESHOOTING.md
ARCHITECTURE.md
docs/session_*.md — логи сессий
```

---

## Ключевая логика кода

### ingest/db.py (poller.py использует те же функции)
- `_state[icao]` — in-memory объединение полей по ICAO (merge MSG,1 + MSG,3 + MSG,4)
- `_valid_position()` — фильтр призрачных позиций (скорость > 800 уз → отброс)
- `_batch[]` — позиции с lat/lon для таблицы positions
- `_last_pos_ts[icao]` — дедупликация: не писать повторную позицию с тем же ts
- `writer_loop()` — каждые 2с флашит батчи в PostgreSQL
- `process_snapshot()` — парсит aircraft.json, `lastPosition` fallback если seen_pos < 120с
- `MAX_SEEN_POS_SECS=60` — порог для lastPosition fallback

### server/api/main.py
- `/data/aircraft.json` — Primary: проксирует Pi (:30092), Fallback: SELECT из DB
- `/api/archive` — список рейсов за период (GROUP BY icao, MAX callsign)
- `/api/monitor` — сравнивает Pi live vs DB за последние 120с
- `monitor_task()` — фоновая задача каждые 30с

### frontend/archive.html (Leaflet)
Разбит на 4 слоя (рефакторинг 2026-04-06):
- **Layer 1 Network:** `fetchTrackPoints`, `fetchWithTimeout` — только HTTP
- **Layer 2 State:** `registerTrack`, `unregisterTrack` — только activeTracks Map
- **Layer 3 UI:** `setItemActive`, `isItemChecked` — только checkbox/CSS
- **Layer 4 Orchestrators:** `addTrack`, `removeTrack`, `selectVisible` — вызывают слои 1-3

---

## База данных

```
postgresql://adsb:adsb2024@localhost:5432/adsb18
```

- `positions` — PARTITION BY RANGE (ts), партиции по месяцам (positions_2026_04)
- `aircraft` — живое состояние + история (last_seen, first_seen, msg_count)
- `feeders` — имя, координаты, last_connected, msg_count
- `partition_watchdog()` — создаёт current + next 2 партиции, раз в сутки
- `drop_old_partitions(keep_months=6)` — **не вызывается автоматически**, нужен cron

Полезные запросы:
```sql
SELECT max(ts) FROM positions;                          -- последняя позиция
SELECT count(*) FROM positions_2026_04;                -- кол-во записей за апрель
SELECT name, last_connected, msg_count FROM feeders ORDER BY last_connected DESC LIMIT 5;
```

---

## Healthcheck (настроен 2026-04-06)

- **Скрипт:** `/home/new/adsb18/healthcheck.py`
- **Cron пользователя `new`:** `*/5 * * * *` — каждые 5 минут
- **Лог:** `/var/log/adsb18-healthcheck.log`
- **Telegram:** бот @adsb18_monitor_bot, chat_id=357650937
- **Проверяет:** API :9001, свежесть позиций (порог 10 мин), фидер (порог 15 мин), туннель Pi :30092
- **venv:** `/opt/adsb18-venv` (psycopg2-binary установлен через `sudo pip`)

Включить cron (когда Pi включён):
```bash
crontab -l | sed 's|^#\*/5|\*/5|' | crontab -
```

Отключить cron (когда Pi выключен):
```bash
crontab -l | sed 's|^\*/5|#*/5|' | crontab -
```

---

## Правила минимизации багов при кодинге

1. **Читать весь файл перед правкой** — найти все места где вызывается изменяемая функция
2. **Один слой — одна ответственность** — Network/State/UI/Orchestrator не смешивать; ошибка в одном слое не должна ломать другой
3. **Async-код: думать о гонках** — перед каждым `await` спрашивать: что может измениться пока ждём? После `await` — guard проверка что состояние ещё актуально
4. **Порядок операций важен** — сначала все изменения состояния/DOM, потом функции которые его читают (пример: `setItemActive` до `updateTrackCount`)
5. **Внешние функции в try-catch** — если вызываешь функцию которую не контролируешь, оборачивай; падение не должно откатывать чужой успешный результат
6. **Пройти TESTING.md перед каждым push** — 10 сценариев, 10 минут; особенно T4 и T6-T7
7. **Одно изменение за раз** — не менять два места одновременно
8. **Если сломалось после моих действий — сначала смотреть на себя** — не искать внешние причины пока не исключены мои изменения

---

## Правила работы

- Деплой: **systemd, НЕ Docker** (docker-compose.yml — только для справки)
- **После изменения frontend JS/HTML:** `gzip -k -f frontend/archive.html` — nginx использует `gzip_static on`
- Изменения Pi — только через репо + `bash feeder/update_pi.sh`
- **Никогда не редактировать файлы напрямую на Pi**
- Читать TROUBLESHOOTING.md перед работой
- Перед `git push` в archive.html — пройти чек-лист из TESTING.md

---

## Git workflow

```bash
# Обычный деплой
git add ...
git commit -m "..."
git push

# После изменений frontend
gzip -k -f frontend/archive.html
git add frontend/archive.html frontend/archive.html.gz
git commit -m "..."
git push
```

**Session лог обновляется в том же коммите что и код** — не пушить код без лога.

При завершении сессии ("завершаем"):
1. Финальное обновление `docs/session_ДАТА.md`
2. Обновить `ARCHITECTURE.md` если были изменения в коде
3. Git push

---

## Открытые задачи

- [ ] `drop_old_partitions` — не вызывается автоматически, нужен cron
- [ ] PTRACKS 24h — увеличить с 8 до 24 в `/etc/default/tar1090` на Pi (одна строка)
- [ ] ESLint для frontend JS — разовая настройка
- [ ] Toast-уведомления в archive.html при ошибках (сейчас только в console.error)

---

## Закрытые баги

- [x] enqueue() NULL lat/lon — проверка `lat/lon is None` в db.py (2026-03-31)
- [x] adsb18-ingest параллельно с poller — замаскирован в /dev/null (2026-03-31)
- [x] Frozen positions — MAX_SEEN_POS_SECS=60 в process_snapshot (2026-03-31)
- [x] Ghost positions — _valid_position() + del _last_valid_pos (2026-03-31)
- [x] Archive GROUP BY — GROUP BY icao только, MAX(callsign) (2026-03-31)
- [x] CBJ666 checkbox самоснимается — recalcDistances в отдельном try-catch в addTrack (2026-04-06)
- [x] clearDistanceLayers null labelMarker — добавлена проверка if(labelMarker) (2026-04-06)
- [x] selectVisible race condition — guard !cb2.checked после await (2026-04-06)
- [x] onDotIntervalChange — build new layer before removeLayer (2026-04-06)
- [x] removeTrack updateTrackCount порядок — setItemActive до unregisterTrack (2026-04-06)
