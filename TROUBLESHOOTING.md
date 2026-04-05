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

---

## Сессия 2026-03-25 (починка потока данных)

### ПРОБЛЕМА 8: Домашний роутер Pi сбрасывает TCP-пакеты к VPS:30001

**Симптомы:**
- Pi подключается, отправляет ~24 строки, потом данные прекращаются
- `ss -tnoi` на Pi: `backoff:7-10, retrans:1/8-11, lost:1, cwnd:1, rto:35000-120000ms`
- VPS принимает только 1976 байт за всё соединение
- `writer_loop tick: batch=0` бесконечно

**Причина:**
Домашний роутер Pi делает stateful inspection и после ~1-2KB данных начинает дропать пакеты к VPS:30001. TCP retransmit уходит в exponential backoff (120 секунд между попытками). Прямое соединение Pi → VPS через публичную сеть нестабильно.

**Решение:**
Пустить трафик фидера через SSH-туннель. Для роутера это обычный SSH-трафик (порт 22), который он не трогает.

1. Добавить local forward в autossh на Pi (`-L 30091:localhost:30001`):
   ```ini
   # /etc/systemd/system/adsb-tunnel.service
   ExecStart=/usr/bin/autossh -M 0 -N \
     -o ServerAliveInterval=30 \
     -o ServerAliveCountMax=3 \
     -o StrictHostKeyChecking=no \
     -o ExitOnForwardFailure=yes \
     -i /home/ads-b/.ssh/id_adsb_vps \
     -R 52222:localhost:22 \
     -L 30091:localhost:30001 \
     new@173.249.2.184
   ```

2. Поменять адрес в feeder:
   ```ini
   # /etc/systemd/system/adsb18-feeder.service
   ExecStart=/usr/bin/python3 /home/ads-b/feeder.py --server 127.0.0.1 --port 30091 --name ads-b-pi
   ```

**Результат:** данные пошли непрерывно (lines=50, 100, 150... каждые 10 сек)

---

### ПРОБЛЕМА 9: asyncio starvation на Pi (feeder.py)

**Симптомы:**
Pi вызывал `writer.drain()` каждые 1000 сообщений. asyncio write buffer наполнялся, event loop не получал управление, transport не флашил данные в OS-сокет.

**Решение:**
```python
if msg_count % 50 == 0:
    await asyncio.sleep(0)  # yield to flush transport
    await writer.drain()
```

---

### ПРОБЛЕМА 10: Порт 30001 не открыт в UFW

**Симптомы:**
Прямые TCP-соединения к VPS:30001 иногда работали (established), но данные не шли.

**Решение:**
```bash
sudo ufw allow 30001/tcp comment 'adsb18 ingest'
```
(Хотя после перехода на SSH-туннель это уже не критично — трафик идёт через порт 22)

---

### ПРОБЛЕМА 11: nginx 500 — Permission denied на /home/new

**Симптомы:**
Браузер возвращает `500 Internal Server Error` при открытии http://173.249.2.184:8098.
В логах nginx: `stat() "/home/new/adsb18/frontend/index.html" failed (13: Permission denied)`

**Причина:**
Домашняя директория `/home/new` имела права `drwxr-x---` — пользователь `www-data` (nginx) не мог зайти в неё (нет бита `x` для `others`).

**Решение:**
```bash
sudo chmod o+x /home/new
```

**Диагностика:**
```bash
sudo tail -20 /var/log/nginx/error.log
```

---

## Текущее состояние (2026-03-26)

**Работает:**
- Pi → SSH-туннель (порт 30092) → VPS poller → PostgreSQL
- Poller опрашивает aircraft.json каждую секунду
- Веб-интерфейс открывается: http://173.249.2.184:8098
- Самолёты отображаются на карте, количество совпадает с Pi tar1090

**Архитектура данных:**
- `adsb18-poller.service` на VPS (НЕ feeder.py на Pi)
- `adsb18-ingest.service` — disabled (не нужен)
- `adsb18-feeder.service` на Pi — disabled (не нужен)
- SSH-туннель: `-R 52222:localhost:22` + `-R 30092:localhost:80`

**Открытые вопросы:**
- [ ] Рассмотреть переход на WireGuard + readsb --net-connector для более чистой архитектуры

---

---

## Сессия 2026-03-26 (исправление расхождения количества самолётов)

### ПРОБЛЕМА 12: Сервер показывал меньше самолётов чем Pi tar1090

**Симптомы:**
Pi tar1090 показывает N самолётов в таблице, сервер показывает меньше.

**Причина:**
feeder.py читал SBS-поток (порт 30003) — `sbs_parser.py` фильтрует MSG,8 (surveillance status).
Самолёты, которые посылают ТОЛЬКО MSG,8, видны dump1090/tar1090 но не доходили до сервера.
Pi's tar1090 читает `aircraft.json` — полный снимок состояния со ВСЕМИ бортами.

**Решение:**
Переключиться с feeder.py (SBS) на poller.py (aircraft.json):
1. SSH-туннель на Pi: `-R 30092:localhost:80` (уже был в конфиге)
2. URL в poller.py: `http://127.0.0.1:30092/tar1090/data/aircraft.json`
3. Создать `adsb18-poller.service` на VPS
4. Отключить `adsb18-feeder.service` на Pi и `adsb18-ingest.service` на VPS

**Сервис на VPS:** `/etc/systemd/system/adsb18-poller.service`
```ini
[Service]
WorkingDirectory=/home/new/adsb18/server/ingest
ExecStart=/opt/adsb18-venv/bin/python poller.py
Environment=PI_AIRCRAFT_URL=http://127.0.0.1:30092/tar1090/data/aircraft.json
```

**Результат:** количество самолётов на сервере = количество на Pi tar1090

---

## Сессия 2026-03-26 (автозапуск сервисов)

### ПРОБЛЕМА 13: adsb18-ingest и adsb18-feeder не в автозапуске

**Симптомы:**
- После перезагрузки VPS данные не собираются (ingest не стартует)
- После перезагрузки Pi данные не отправляются (feeder не стартует)

**Причина:**
Сервисы были созданы и запущены вручную, но не добавлены в автозапуск (`systemctl enable`).

**Фикс:**
```bash
# На VPS:
sudo systemctl enable adsb18-ingest

# На Pi (через туннель):
ssh -p 52222 ads-b@127.0.0.1
sudo systemctl enable adsb18-feeder
```

**Текущий статус автозапуска (все enabled):**
- VPS: postgresql, nginx, adsb18-api, adsb18-ingest
- Pi: readsb, adsb-tunnel, adsb18-feeder

---

## Переподключение Pi на другой сервер

Когда меняется IP VPS (переезд, новый сервер):

**Шаг 1 — добавить ключ Pi на новый сервер**
```bash
# Посмотреть публичный ключ Pi (с VPS через туннель):
ssh -p 52222 ads-b@127.0.0.1 'cat /home/ads-b/.ssh/id_adsb_vps.pub'

# Добавить на новом сервере:
echo 'ПУБЛИЧНЫЙ_КЛЮЧ' >> ~/.ssh/authorized_keys
```

**Шаг 2 — поменять IP в tunnel-сервисе на Pi**
```bash
ssh -p 52222 ads-b@127.0.0.1
sudo nano /etc/systemd/system/adsb-tunnel.service
# Поменять: new@СТАРЫЙ_IP  →  new@НОВЫЙ_IP
sudo systemctl daemon-reload
sudo systemctl restart adsb-tunnel
```

**Шаг 3 — готово**
Фидер переподключится автоматически (он `After=adsb-tunnel.service`).

---

## Сессия 2026-03-26 (архив полётов)

### ПРОБЛЕМА 14: Неполный трек в архиве — первые секунды рейса обрезаются

**Симптомы:**
- В архиве трек рейса начинается на ~20 секунд позже чем на живой карте
- Первые точки маршрута отсутствуют

**Причина:**
`/api/archive` группировал по `(icao, callsign)`. Первые секунды полёта самолёт
слышен, но позывной ещё не передан → записи с `callsign=NULL` попадали в
отдельную строку. При клике на рейс трек загружался только с момента появления позывного.

**Фикс:** `server/api/main.py` — группировка только по `icao`, позывной через `MAX(callsign)`:
```sql
GROUP BY icao  -- было: GROUP BY icao, callsign
MAX(callsign) AS callsign  -- игнорирует NULL, берёт первый непустой
```

### ПРОБЛЕМА 15: Призрачные точки на треке — CPR ghost positions

**Симптомы:**
- На треке самолёта есть точка(и) в 100-200+ км от реального маршрута
- Скорость в этих точках ~20 узлов (нереально для крейсерского самолёта)
- Происходит когда самолёт на краю зоны приёма антенны

**Причина:**
CPR (Compact Position Reporting) — алгоритм декодирования позиции в ADS-B — требует
два последовательных кадра (odd+even) для точного вычисления координат. На краю зоны
приёма сигнал теряется, и readsb использует устаревшие опорные данные → выдаёт
"зеркальную" позицию в стороне от реального маршрута.

**Фикс:** `server/ingest/db.py` — функция `_valid_position()`:
- Вычисляет дистанцию Хаверсина от последней известной точки
- Если расстояние требует скорость > 800 узлов — позиция отбрасывается
- Логирует предупреждение: `ghost position rejected`
- Очищает lat/lon из состояния борта чтобы следующие сообщения не наследовали призрак

**Как проверить наличие призраков в логах:**
```bash
sudo journalctl -u adsb18-ingest | grep "ghost position"
```

---

## Сессия 2026-03-30 (аномальные точки в треках)

### ПРОБЛЕМА 16: Frozen positions — самолёт "стоит" на одном месте минутами

**Симптомы:**
- В треке первые 1-3 минуты самолёт не движется, затем резкий прыжок на 100-400 км
- В БД сотни записей с одинаковыми lat/lon при ground_speed > 400 кт

**Причина:**
readsb держит последний известный CPR-фикс и продолжает отдавать его в aircraft.json
даже когда самолёт уже давно не передавал позицию. Поле `seen_pos` показывает
сколько секунд назад была последняя реальная позиция — мы его игнорировали.

**Решение (`server/ingest/db.py`):**
Добавить константу `MAX_SEEN_POS_SECS = 60` и отбрасывать lat/lon если
`seen_pos > MAX_SEEN_POS_SECS` — readsb сам считает позицию устаревшей.

---

### ПРОБЛЕМА 17: Ghost positions не фильтруются после перезапуска poller

**Симптомы:**
- После рестарта poller (или первого появления ICAO) ghost-скачки на 500-750 км
  попадают в БД несмотря на `_valid_position()`
- В логах нет ни одного `ghost position rejected`

**Причина:**
`_last_valid_pos` — in-memory dict, сбрасывается при каждом рестарте.
При первом появлении ICAO `prev is None` → первая точка принимается без проверки.
Если эта первая точка — ghost (невалидный CPR), следующие реальные точки будут
отклоняться (слишком далеко от ghost). Потом ghost так и остаётся в `_last_valid_pos`.

**Решение:**
После отклонения ghost: `del _last_valid_pos[icao]` вместо просто `return False`.
Следующая точка будет принята как свежий старт.


---

## Сессия 2026-03-30 (лишние записи в БД)

### ПРОБЛЕМА 18: positions пишется каждую секунду — дубликаты и null строки

**Симптомы:**
- AFL1461 (без позиции) — 2626 строк с lat=NULL за 17 минут, все с одинаковым ts
- SVR266 (с позицией) — ~5 записей/сек при poll каждую секунду
- positions таблица растёт намного быстрее чем нужно

**Причина 1 — adsb18-ingest работал параллельно с poller:**
Сервис был `enabled` несмотря на то что был заменён poller'ом.
Писал все борта через `enqueue()` без проверки lat/lon.

**Решение 1:**
```bash
sudo systemctl stop adsb18-ingest
sudo systemctl disable adsb18-ingest
```

**Причина 2 — process_snapshot писал в positions даже без позиции:**
Все борта из aircraft.json добавлялись в _batch включая тех у кого нет lat/lon.
Также при фиксированном seen_pos (борт не слышен) pos_ts не меняется →
каждую секунду пишется идентичная запись.

**Решение 2 (db.py):**
- Добавить `_last_pos_ts: dict[str, datetime]`
- В `process_snapshot`: добавлять в `_batch` только если `lat IS NOT NULL`
  и `pos_ts != _last_pos_ts[icao]`
- Результат: в positions только реальные новые точки, без дубликатов


---

## Сессия 2026-03-31 (страница не открывается после перезапуска сервисов)

### ПРОБЛЕМА 19: Страница зависает — серый экран, браузер крутит загрузку бесконечно

**Симптомы:**
- Открываешь http://173.249.2.184:8098/ — серый экран, страница не загружается
- Браузер бесконечно "грузит"
- На сервере всё запущено: nginx, API, поллер — всё active

**Что происходит (простыми словами):**
Когда перезапускаешь `adsb18-api`, есть ~3 секунды пока он поднимается.
В это время nginx возвращает ошибку 502. Браузер получил ошибку, но
TCP-соединение с сервером осталось "открытым" (keep-alive).
Дальше nginx пытается отправить данные по этому мёртвому соединению —
данные уходят в буфер операционной системы и там застревают навсегда.
Браузер ждёт данные по старому соединению, сервер их отправил но они не дошли.
Оба висят и ждут друг друга.

**Как проверить что это именно эта проблема:**
```bash
sudo ss -tnp | grep ':8098'
```
Если видишь строки с ненулевым вторым числом (send-буфер) для внешнего IP — это оно:
```
ESTAB  0  106203  173.249.2.184:8098  81.4.255.145:56172   ← данные застряли
```

**Решение — одна команда на сервере:**
```bash
sudo nginx -s reload
```
После этого браузер автоматически установит новое соединение и страница откроется.

**Правило:** Всегда после `sudo systemctl restart adsb18-api` выполнять:
```bash
sudo nginx -s reload
```

---

### ПРОБЛЕМА 20: tar1090 показывает "Problem fetching data from the server"

**Симптомы:**
- Страница открылась, но карта пустая и красное сообщение об ошибке

**Что происходит:**
tar1090 получил несколько ошибок подряд (во время перезапуска API) и
"решил" что сервер сломан. Сам из этого состояния не выходит.

**Решение — одна команда на сервере:**
```bash
sudo nginx -s reload
```
После этого обновить страницу в браузере (F5). nginx -s reload сбрасывает
зависшие соединения, браузер подключается заново и получает нормальные данные.

---

## Сессия 2026-04-01 (archive.html не загружается)

### ПРОБЛЕМА 21: Страница архива пустая — тёмный экран, ничего не отображается

**Симптомы:**
- Открываешь http://173.249.2.184:8098/archive.html — тёмный фон, нет ни карты, ни панелей, ни дат
- В консоли браузера (F12) пусто — ни ошибок, ни логов
- На сервере всё работает: nginx 200, API отдаёт данные, headless Chrome рендерит страницу нормально
- Ctrl+Shift+R иногда помогало, но ненадолго

**Диагностика:**
1. Логи nginx показали: браузер загружает `archive.html`, `leaflet.css`, `leaflet.js` (все 200),
   но запрос к `/api/archive` **никогда не происходит** — значит inline JS не выполняется
2. Создали пошаговую тест-страницу (`test2.html`): загрузка Leaflet с локального сервера зависала —
   ни `onload`, ни `onerror` не срабатывали
3. Тест с CDN (`test3.html`): Leaflet с `unpkg.com` по HTTPS загрузился и работал без проблем

**Корневая причина — DPI провайдера обрезает chunked HTTP-ответы:**

Сайт работает по **HTTP** (не HTTPS). Российские провайдеры используют ТСПУ/DPI
(Deep Packet Inspection) — оборудование, которое анализирует незашифрованный трафик.

Когда nginx сжимает файл **на лету** (on-the-fly gzip для файлов без `.gz`),
он отдаёт ответ **без `Content-Length`** с `Transfer-Encoding: chunked`.
DPI не может определить размер chunked-ответа и **обрезает или блокирует** его.
Браузер получает неполный файл — скрипт невалиден, но ни `onload`, ни `onerror`
не срабатывают (браузер всё ещё ждёт данные). Страница остаётся пустой.

Разница между файлами:
```
leaflet.js    → НЕТ .gz → on-the-fly gzip → БЕЗ Content-Length → DPI режет
ol-custom.js  → ЕСТЬ .gz → gzip_static     → ЕСТЬ Content-Length → DPI пропускает
script.js     → ЕСТЬ .gz → gzip_static     → ЕСТЬ Content-Length → DPI пропускает
```

Поэтому `index.html` работал (все крупные JS имели `.gz`), а `archive.html` — нет
(`leaflet.js` не имел `.gz`).

**Решение 1 — CDN для Leaflet (`frontend/archive.html`):**
```html
<!-- Было: -->
<link rel="stylesheet" href="libs/leaflet/leaflet.css"/>
<script src="libs/leaflet/leaflet.js"></script>

<!-- Стало: -->
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
```
CDN отдаёт по HTTPS — DPI не может вмешаться.

**Решение 2 — `.gz` для всех файлов без них:**
Созданы предварительно сжатые `.gz` для всех JS/CSS > 5KB.
Теперь `gzip_static` отдаёт их с `Content-Length` — DPI пропускает.
```bash
gzip -k frontend/libs/leaflet/leaflet.js
gzip -k frontend/libs/egm96-universal-1.1.0.min.js
gzip -k frontend/early.js
gzip -k frontend/formatter.js
gzip -k frontend/defaults.js
gzip -k frontend/registrations.js
gzip -k frontend/style.css
gzip -k frontend/libs/jquery-ui-1.13.2.min.css
gzip -k frontend/libs/ol-8.2.0.css
gzip -k frontend/libs/ol-layerswitcher-4.1.1.css
gzip -k frontend/libs/leaflet/leaflet.css
```

**Решение 3 — `no-cache` для archive.html в nginx:**
```nginx
location = /archive.html {
    root /home/new/adsb18/frontend;
    add_header Cache-Control "no-store, no-cache, must-revalidate";
}
```
Обновления страницы доходят до пользователя без Ctrl+Shift+R.

**Правило на будущее:**
При добавлении любого нового JS/CSS файла > 5KB — обязательно создавать `.gz`:
```bash
gzip -k frontend/новый_файл.js
```

**Кардинальное решение:**
Поставить **HTTPS** (Let's Encrypt + домен). Это полностью закроет проблему с DPI
для всех файлов. По голому IP сертификат не выдают — нужен домен.

**Как диагностировать похожие проблемы в будущем:**
1. Страница пустая, консоль чистая → скрипт не загрузился
2. Проверить: `curl -sI -H "Accept-Encoding: gzip" http://...файл.js | grep Content-Length`
3. Если `Content-Length` нет → файл отдаётся chunked → создать `.gz` файл
4. Пошаговая диагностика: создать тест-страницу с `onload`/`onerror` для каждого ресурса

