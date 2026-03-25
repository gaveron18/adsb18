# adsb18 — Журнал проблем и решений

Этот файл обновляется каждый раз когда находим и чиним баги.
**Claude: читай этот файл в начале каждой сессии по проекту adsb18.**

---

## Сессия 2025 (миграция с Docker на systemd)

### ПРОБЛЕМА 1: asyncio event loop starvation — данные не пишутся в БД

**Симптомы:**
- Фидер подключается, несколько сообщений приходит, затем `writer_loop tick: batch=0` — бесконечно
- В БД пишутся первые ~8 записей, потом всё останавливается
- `recv-Q=0` на VPS — данные в TCP-буфере ОС уже есть, но asyncio их не читает
- Pi имеет ~19KB в send-буфере (данные уходят с Pi, но ingest не обрабатывает)

**Причина:**
`asyncio.StreamReader.readline()` — если данные уже есть в буфере, возвращается **немедленно без yield в event loop**. Это блокирует все остальные корутины (`writer_loop`), потому что они никогда не получают управление.

**Решение:**
Добавить `await asyncio.sleep(0)` каждые N строк чтобы отдавать управление event loop:
```python
if line_count % 50 == 0:
    await asyncio.sleep(0)  # yield to event loop for writer_loop
```

**Файл:** `server/ingest/main.py`

---

### ПРОБЛЕМА 2: yield никогда не срабатывал (MSG,8 → None)

**Симптомы:**
После добавления yield через `msg_count % 50`, данные всё равно не шли.

**Причина:**
MSG,8 (surveillance status) парсится как `None` — не содержит полезных полей. Счётчик `msg_count` не инкрементируется на MSG,8, поэтому `msg_count % 50 == 0` никогда не выполнялось — поток на 90% состоит из MSG,8.

**Решение:**
Заменить `msg_count` на `line_count` для тригера yield:
```python
line_count += 1  # каждая строка, даже если parse() вернул None
if msg:
    store.enqueue(msg, feeder_id=feeder_id)
    msg_count += 1
if line_count % 50 == 0:
    await asyncio.sleep(0)
```

**Файл:** `server/ingest/main.py`

---

### ПРОБЛЕМА 3: Диагностика зависания readline()

**Симптомы:**
Непонятно — `readline()` блокирует или просто нет данных?

**Решение — диагностический код:**
```python
try:
    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
except asyncio.TimeoutError:
    log.warning(f'Feeder {name}: readline timeout! lines={line_count} msgs={msg_count} buf={len(reader._buffer)}')
    continue
```
Если timeout срабатывает при непустом `reader._buffer` — это starvation.
Если timeout при пустом буфере — данные просто не приходят от Pi.

**Файл:** `server/ingest/main.py`

---

### ПРОБЛЕМА 4: Диск переполнен (No space left on device)

**Симптомы:**
PostgreSQL перестал принимать запросы, в логах: `No space left on device`.

**Причина:**
`/home/hive` занимал 51GB. Основные виновники:
- `dronedoc2025/` — 13GB
- `dronedoc2026/` — 3.8GB
- Другие проекты в `/home/new/`

**Решение:**
Освободить место вручную. После освобождения — соединения asyncpg стали "сломанными" (pool держал старые соединения к упавшему PostgreSQL).

**Урок:** После восстановления PostgreSQL нужно перезапустить ingest-сервис чтобы pool пересоздал соединения.

---

### ПРОБЛЕМА 5: Порт 30001 уже занят

**Симптомы:**
`OSError: [Errno 98] Address already in use` при старте ingest.

**Причина:**
Docker-контейнер `adsb18_ingest_1` продолжал работать после `docker-compose down` (были запущены под другими именами).

**Диагностика:**
```bash
ss -tlnp | grep 30001
docker ps -a
```

**Решение:**
```bash
docker stop adsb18_ingest_1 adsb18_api_1
docker rm adsb18_ingest_1 adsb18_api_1
```

---

### ПРОБЛЕМА 6: Pi подключался к старому Docker-контейнеру

**Симптомы:**
После остановки Docker, Pi показывал `TCP ESTAB` на порт 30001, но новый ingest ничего не видел.

**Причина:**
Pi держал старое TCP-соединение к Docker-контейнеру (сессия ещё жила на уровне ядра).

**Решение:**
```bash
# На Pi (через reverse SSH tunnel):
ssh -p 52222 ads-b@127.0.0.1
sudo systemctl restart adsb18-feeder
```

---

### ПРОБЛЕМА 7: PostgreSQL — нет прав у пользователя adsb

**Симптомы:**
`ERROR: permission denied for table positions` в логах ingest.

**Причина:**
`GRANT` в `init.sql` срабатывает только один раз при первом создании Docker volume. При пересоздании БД на хосте привилегии не были выданы.

**Решение:**
```sql
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO adsb;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO adsb;
```

---

## Текущая архитектура (после миграции с Docker)

### Сервисы
| Сервис | Тип | Порт | Файл |
|--------|-----|------|------|
| PostgreSQL | systemd | 5432 | `/etc/postgresql/*/main/` |
| adsb18-ingest | systemd | 30001 | `/etc/systemd/system/adsb18-ingest.service` |
| adsb18-api | systemd | 9001 | `/etc/systemd/system/adsb18-api.service` |
| nginx | systemd | 8098 | `/etc/nginx/sites-available/adsb18` |

### Управление
```bash
sudo systemctl status adsb18-ingest adsb18-api
sudo systemctl restart adsb18-ingest
sudo journalctl -u adsb18-ingest -f   # логи ingest
sudo journalctl -u adsb18-api -f      # логи API
```

### Доступ к Pi
```bash
ssh -p 52222 ads-b@127.0.0.1   # через reverse SSH tunnel
sudo systemctl status adsb18-feeder
sudo journalctl -u adsb18-feeder -f
```

### Переменные окружения (ingest)
```
DATABASE_URL=postgresql://adsb:adsb2024@localhost:5432/adsb18
INGEST_HOST=0.0.0.0
INGEST_PORT=30001
```

### Virtualenv
```
/opt/adsb18-venv/
```

---

## Нераскрытые/незакрытые проблемы

- [ ] Нужно проверить что данные продолжают поступать после `asyncio.wait_for` + `asyncio.sleep(0)` фикса
- [ ] Pi иногда накапливает > 10 000 сообщений в очереди (Queue(maxsize=10000)) — возможна потеря при переполнении
