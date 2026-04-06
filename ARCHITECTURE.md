# Архитектура проекта adsb18

## Обзор

**adsb18** — система сбора, хранения и визуализации данных ADS-B. Самолёты транслируют своё местоположение по радио (1090 МГц) каждую секунду. Raspberry Pi принимает сигнал и отправляет на сервер, который хранит историю и показывает воздушную обстановку на карте.

---

## Схема компонентов

```
┌──────────────────────────────────────────────────────┐
│                  Raspberry Pi (ads-b)                │
│                                                      │
│  [Антенна 1090МГц]                                   │
│       ↓                                              │
│  [RTL-SDR USB]                                       │
│       ↓                                              │
│  [dump1090 / readsb]  →  SBS поток :30003            │
│                          JSON /run/readsb/aircraft.json │
│       ↓                                              │
│  [feeder.py]  ←── работает сейчас (SBS режим)        │
│  [feeder_json.py]  ←── альтернатива (JSON режим)     │
│       ↓                                              │
│  disk buffer (feeder_buffer.sbs, макс 200 МБ)        │
│  (при обрыве пишет на диск, при reconnect — replay)  │
│       ↓  TCP                                         │
│  [adsb-tunnel] ── SSH-туннель → VPS :30001           │
│  (systemd, Pi инициирует)                            │
└──────────────────────────────────────────────────────┘
                          ↓ TCP :30001
┌──────────────────────────────────────────────────────┐
│                  VPS 173.249.2.184                   │
│                                                      │
│  [adsb18-ingest :30001]                              │
│    • AUTH / AUTH-JSON протокол                       │
│    • SBS: парсит → merge по ICAO → _batch            │
│    • JSON: process_snapshot() → _batch               │
│    • ghost filter: отбрасывает позиции > 800 узлов   │
│    • writer_loop: flush каждые 2с или 200 строк      │
│         ↓ asyncpg                                    │
│  [PostgreSQL 16 :5432]                               │
│    • positions (PARTITION BY RANGE ts, по месяцам)   │
│    • aircraft (живое состояние + история)            │
│    • feeders (приёмники)                             │
│         ↓                                            │
│  [adsb18-api :9001]  (FastAPI + uvicorn)             │
│    • /data/aircraft.json  ← Primary: proxy Pi        │
│                              Fallback: DB query      │
│    • /data/receiver.json                             │
│    • /api/history?icao=&from=&to=&limit=             │
│    • /api/history/bulk  (POST)                       │
│    • /api/aircraft?hours=24                          │
│    • /api/archive?from=&to=                          │
│    • /api/feeders                                    │
│    • /api/monitor  ← Pi vs Server сравнение          │
│    • DELETE /api/flight
│    • GET/POST/PUT/DELETE /api/points  (точки измерений)                              │
│    • /data/traces/  (tar1090 getTrace)               │
│    • /globe_history/ (tar1090 date picker)           │
│    • WS /ws  ← пушит aircraft.json каждую секунду   │
│         ↓                                            │
│  [nginx :8098]                                       │
│    /          → frontend/ (статика)                  │
│    /data/     → :9001                                │
│    /api/      → :9001                                │
│    /ws        → :9001 (WebSocket upgrade)            │
│    gzip_static on  (нужен .gz рядом с каждым .js)   │
└──────────────────────────────────────────────────────┘
                          ↓ HTTP :8098
                  [Браузер — tar1090 карта]
                  (OpenLayers, jQuery)
                  • самолёты на карте
                  • треки полётов
                  • таблица бортов
                  • архив по датам
```

---

## Протокол фидера

```
Pi → сервер :30001

AUTH <name>\n          ← SBS режим (feeder.py)
AUTH-JSON <name>\n     ← JSON режим (feeder_json.py)

SBS режим:
  MSG,3,1,1,424141,1,2026/03/25,10:23:41,,,10000,,,-55.12,37.45,,,,,-1\n
  MSG,4,1,1,424141,...\n   ← скорость
  MSG,1,1,1,424141,...\n   ← позывной
  ...

JSON режим:
  {"now":1743350400.0,"aircraft":[{"hex":"424141","flight":"SU100",...}]}\n
  (один снапшот в секунду — все борта целиком)
```

При обрыве: feeder.py пишет SBS строки на диск (`feeder_buffer.sbs`).
При reconnect: сначала replay буфера, потом live.

---

## База данных

### positions — каждое обновление позиции
```
ts              TIMESTAMPTZ   время сообщения
icao            CHAR(6)       hex-адрес борта (напр. '3C6444')
feeder_id       INTEGER       FK → feeders.id
callsign        VARCHAR(9)    позывной (SU100)
altitude        INTEGER       высота, футы (барометрическая)
ground_speed    SMALLINT      скорость, узлы
track           SMALLINT      курс, градусы 0-359
lat / lon       REAL          координаты
vertical_rate   SMALLINT      вертикальная скорость, фут/мин
squawk          VARCHAR(4)    код транспондера
is_on_ground    BOOLEAN
signal_type     VARCHAR(10)   adsb / mode_s / mlat / tisb / adsr
rssi            REAL          уровень сигнала, dBm
category        VARCHAR(4)    категория ВС (A0-C3)
emergency       VARCHAR(16)   статус аварии
```
Партиционирована по месяцам: `positions_2026_03`, `positions_2026_04`, ...
Партиции создаются автоматически (current + next 2 months).
**Внимание:** `drop_old_partitions()` в коде есть, но нигде не вызывается автоматически!

### aircraft — живое состояние
```
icao            CHAR(6)  PK
last_seen       TIMESTAMPTZ
first_seen      TIMESTAMPTZ
last_callsign   VARCHAR(9)
last_lat/lon    REAL
last_altitude   INTEGER
last_speed      SMALLINT
last_track      SMALLINT
last_vrate      SMALLINT
last_squawk     VARCHAR(4)
is_on_ground    BOOLEAN
msg_count       BIGINT
last_pos_seen   TIMESTAMPTZ   последнее время когда была позиция
```

### measurement_points
```
id, name, address, lat, lon, date_from, date_to, created_at
```
Точки для расчёта расстояний до треков на странице архива. altitude в футах.

### feeders
```
id              SERIAL  PK
name            VARCHAR(64) UNIQUE   (напр. 'ads-b-pi')
lat / lon       REAL         координаты приёмника
last_connected  TIMESTAMPTZ
msg_count       BIGINT
```

---

## Ключевые алгоритмы (db.py)

### Merge по ICAO
dump1090 шлёт разные поля в разных сообщениях:
- MSG,1 → callsign
- MSG,3 → позиция + высота
- MSG,4 → скорость + курс
- MSG,6 → squawk

`_state[icao]` объединяет все поля в одну запись. В batch пишется объединённая строка.

### Ghost position filter
Если скорость между двумя последовательными позициями > 800 узлов —
позиция отбрасывается (физически невозможно). `_last_valid_pos[icao]` сбрасывается,
следующая позиция принимается как новая стартовая точка.

### Дедупликация (JSON режим)
Двойная защита от дублей:
1. **Coordinate dedup** (`_last_pos[icao]`): если `(lat, lon)` не изменились — позиция не пишется.
   Закрывает "frozen positions": стоящий самолёт каждые 0.3с шлёт ADS-B, `seen_pos` меняется,
   но координаты одни и те же.
2. **Timestamp dedup** (`_last_pos_ts[icao]`): если `seen_pos` не изменился — дубль в DB не пишется.
   Закрывает старую проблему: dump1090 не слышал борт — `seen_pos` не меняется.

Приоритет: coordinate dedup срабатывает первым и отклоняет строку если `(lat, lon)` совпали.

### lastPosition fallback (JSON режим)
Если борт виден по Mode-S (без позиции), но в aircraft.json есть `lastPosition`
с `seen_pos < 120` сек — берём координаты оттуда.

---

## aircraft.json — источник данных для карты

**Primary:** curl проксирует Pi напрямую:
```
http://127.0.0.1:30092/tar1090/data/aircraft.json
```
(доступен через SSH-туннель)

**Fallback:** SELECT из таблицы aircraft WHERE last_seen > NOW()-120s

Это значит: карта показывает то, что видит Pi в реальном времени.
DB используется только при обрыве туннеля.

---

## Сервисы systemd

| Сервис | Где | Файл | Команда |
|--------|-----|------|---------|
| adsb18-ingest | VPS | /etc/systemd/system/ | `python main.py` в /opt/adsb18-venv |
| adsb18-api | VPS | /etc/systemd/system/ | `uvicorn main:app --host 127.0.0.1 --port 9001` |
| adsb-tunnel | VPS | /etc/systemd/system/ | SSH reverse tunnel |
| adsb18-feeder | Pi | /etc/systemd/system/ | `python3 feeder.py --server 127.0.0.1 --port 30091 --name ads-b-pi` |

---

## Открытые задачи

- [ ] **drop_old_partitions** — нет автоматического удаления партиций старше 6 месяцев. Нужен cron.
- [ ] **feeder_json.py** — не используется. Pi работает в SBS режиме. Оценить переход на JSON (богаче данные: rssi, signal_type, category).
- [ ] **Шаг 3** — подтвердить движение самолётов на живом борте с координатами.

---

## Деплой

```bash
# После изменений на VPS:
sudo systemctl restart adsb18-ingest
sudo systemctl restart adsb18-api

# После изменений frontend JS:
gzip -k -f /home/new/adsb18/frontend/script.js
# (nginx использует gzip_static on — без .gz браузеры получат старый кеш)

# Изменения на Pi — через репо:
bash /home/new/adsb18/feeder/update_pi.sh
```

---

## Зависимости и риски

### db.py — центральный модуль, самый опасный для правок

```
enqueue() / process_snapshot()
    ↓ обновляет
_state[icao]
    ↓ читается
get_live_aircraft()  ←── используется в api/main.py (fallback aircraft.json)

_batch[]  ←── только позиции с lat/lon
    ↓
writer_loop() → _flush()
    ↓ пишет
positions (INSERT)          ←── /api/history, /data/traces/, /globe_history/
aircraft (UPSERT с lat/lon) ←── fallback aircraft.json, /api/aircraft

_ac_batch[]  ←── ВСЕ борты включая Mode-S без позиции
    ↓
writer_loop() → _flush_aircraft()
    ↓ пишет
aircraft (UPSERT БЕЗ lat/lon)  ←── может затереть last_lat/last_lon!
```

**Риск 1 — _flush() vs _flush_aircraft() конфликт:**
Два пути обновляют одну таблицу `aircraft`. Если `_flush_aircraft()` выполнится
после `_flush()` — он перезапишет `last_lat/last_lon = NULL` (там нет координат).
**Это уже случалось (2026-03-30) — все самолёты стояли на месте.**
Правило: при любом изменении `_flush_aircraft()` проверять что lat/lon не затирается.

**Риск 2 — Ghost filter сброс:**
При отклонении позиции `_last_valid_pos[icao]` удаляется. Следующая позиция
принимается безусловно. Если dump1090 выдаёт две подряд плохих позиции —
вторая пройдёт фильтр. Не трогать MAX_SPEED_KTS без тестирования.

**Риск 3 — retry при ошибке DB:**
При ошибке flush: `_batch.extend(batch)` — батч возвращается в очередь.
Если ошибка постоянная — память растёт бесконечно. Нет ограничения на размер.

**Риск 4 — _last_pos_ts дедупликация (только JSON режим):**
Если `pos_ts` не меняется между снапшотами — позиция не пишется в positions.
Это правильно, но если время на Pi сбилось — можно потерять реальные позиции.

---

### api/main.py — зависимости

```
GET /data/aircraft.json
    Primary: curl → Pi :30092 (SSH-туннель)
    Fallback: SELECT aircraft WHERE last_seen > NOW()-120s
                                              ↑
                                     НЕЛЬЗЯ менять без согласования
                                     (было 3600 → меняли на 120, борты пропали)

WS /ws → ws_broadcaster() → вызывает aircraft_json()
    ↓ та же логика primary/fallback

GET /api/monitor → fetch Pi :30092
    если туннель недоступен → monitor падает с ошибкой

GET /data/traces/ и /globe_history/
    → SELECT positions WHERE lat IS NOT NULL
    → зависит от того что пишет _flush() в db.py
```

**Риск 5 — туннель Pi недоступен:**
Если SSH-туннель упал → весь `aircraft.json` идёт из DB (устаревшие данные).
Карта будет показывать борты но они не будут двигаться.
Monitor покажет ошибку. Проверять: `sudo systemctl status adsb-tunnel`.

**Риск 6 — окно fallback aircraft.json:**
Сейчас 120 секунд. Если уменьшить — борты пропадают с карты при fallback.
Если увеличить — карта показывает давно улетевшие борты.

---

### ingest/main.py — зависимости

```
handle_feeder()
    SBS режим  → sbs_parser.parse() → store.enqueue()
    JSON режим → json.loads()       → store.process_snapshot()

register_feeder() → INSERT/UPDATE feeders таблица

pool_ref[0]  ←── глобальная ссылка, устанавливается в main()
    если store.set_pool() не вызван → writer_loop не пишет в DB (тихая ошибка)
```

**Риск 7 — AUTH таймаут:**
Если Pi не прислал AUTH за 10 секунд — соединение закрывается.
При высокой нагрузке на Pi это может случиться. Симптом: фидер постоянно
переподключается, в логах "no AUTH received in 10s".

---

### feeder.py (Pi) — зависимости

```
read_dump1090() → queue → send_to_server()
                        ↓ при обрыве
                  disk_spooler() → feeder_buffer.sbs

при reconnect: replay_buffer() → send_to_server() → live queue
```

**Риск 8 — буфер переполнен:**
Если Pi офлайн > времени для накопления 200 МБ — старые сообщения теряются.
200 МБ ≈ несколько часов при нормальном трафике.

**Риск 9 — replay блокирует live данные:**
Во время replay буфера live данные идут в queue но не отправляются на сервер
(send_to_server занят replay). Queue ограничен 10000 сообщений — при переполнении
новые SBS строки теряются.

---

### PostgreSQL — зависимости

```
positions (партиции по месяцам)
    create_monthly_partition() — вызывается автоматически (partition_watchdog, раз в сутки)
    drop_old_partitions()      — НЕ вызывается автоматически! Диск растёт бесконечно.
```

**Риск 10 — диск заполнится:**
`drop_old_partitions()` есть в коде но нигде не вызывается.
Нужен cron: `SELECT drop_old_partitions('positions', 6);` раз в месяц.


---

## Зависимости внутри кода

### db.py — вызовы функций

```
enqueue(msg)
    → _merge(icao, msg)          — обновляет _state[icao]
    → _batch.append(...)         — добавляет позицию

process_snapshot(data)
    → _state[icao]               — обновляет напрямую
    → _valid_position()          — ghost filter
    → _batch.append(...)         — позиции с lat/lon
    → _ac_batch.append(...)      — все борты включая Mode-S

writer_loop()                    — каждые 2с
    → _flush(_batch)             — INSERT positions + UPSERT aircraft С lat/lon
    → _flush_aircraft(_ac_batch) — UPSERT aircraft БЕЗ lat/lon

get_live_aircraft()
    → читает _state[icao]        — используется в api как fallback
```

### db.py — общие переменные (разделяются между функциями)

```
_state          — enqueue, process_snapshot, get_live_aircraft
_batch          — enqueue, process_snapshot, writer_loop, _flush
_ac_batch       — process_snapshot, writer_loop, _flush_aircraft
_last_valid_pos — _valid_position (ghost filter)
_last_pos_ts    — process_snapshot (timestamp dedup, JSON режим)
_last_pos       — process_snapshot (coordinate dedup, JSON режим — frozen position filter)
_pool           — set_pool, _flush, _flush_aircraft, ensure_partitions
```

### ingest/main.py — вызовы

```
main()
    → store.set_pool(pool)          — БЕЗ этого writer_loop не пишет в DB
    → store.writer_loop()           — фоновая задача
    → store.partition_watchdog()    — фоновая задача
    → asyncio.start_server(handle_feeder)

handle_feeder()
    → register_feeder()             — upsert в feeders
    → store.enqueue()               — SBS режим
    → store.process_snapshot()      — JSON режим
    → _update_feeder_stats()        — счётчик сообщений
```

### api/main.py — вызовы

```
ws_broadcaster()
    → aircraft_json()       — та же функция что GET /data/aircraft.json
                              любая ошибка в aircraft_json ломает WebSocket

monitor_task()
    → _fetch_pi_live()      — curl Pi :30092
    → SELECT aircraft       — сравнивает с DB

aircraft_json()
    → curl Pi :30092        — primary
    → SELECT aircraft       — fallback (окно 120с, не менять!)
```

### Самые опасные связи

1. `_flush()` и `_flush_aircraft()` — обе пишут в таблицу `aircraft` но с разными полями.
   `_flush_aircraft()` не обновляет lat/lon — если выполнится после `_flush()` может затереть координаты.

2. `ws_broadcaster()` → `aircraft_json()` — любая ошибка в `aircraft_json` ломает WebSocket для всех клиентов.

3. `_state[icao]` — общая память для всех фидеров без блокировок.
   asyncio однопоточный поэтому окей, но нельзя добавлять threading.

4. `store.set_pool()` должен быть вызван до старта `writer_loop()` — иначе тихая ошибка, данные не пишутся.

---

## Фронтенд — логика и поведение

### Два режима (две страницы)

| Страница | Карта | Данные | Назначение |
|----------|-------|--------|------------|
| `index.html` | OpenLayers 8.2.0 | HTTP polling каждые ~1с | Живая карта самолётов |
| `archive.html` | Leaflet | REST API по периоду | Архив рейсов за дату |

---

### Главный цикл (index.html)

```
setInterval(fetchData, refreshMs)   ← каждые ~1000ms
    ↓ GET /data/aircraft.json
    ↓ fetchDone(data)
    ↓ processReceiverUpdate(data)
        ↓ для каждого борта:
        ↓ processAircraft(ac, init)
            ↓ plane.updateData(now, last, data)
            ↓ обновить иконку / трек на карте
    ↓ обновить таблицу бортов
    ↓ обновить счётчики
```

**При ошибке fetch:**
- `fetchFail()` → показать сообщение об ошибке в UI
- `StaleReceiverCount++` → если > 5 подряд → предупреждение "данные устарели"
- При 5 подряд "время идёт назад" → полный reset данных + cache busting (`?timestamp` к URL)

---

### Объект PlaneObject (planeObject.js)

Каждый борт — это объект со следующими группами полей:

```javascript
// Идентификация
icao, flight, registration, country

// Позиция и движение
position [lon, lat], altitude, alt_baro, alt_geom
track, gs, ias, tas, vertical_rate

// Сигнал
rssi, seen, seen_pos, messages, signal_type

// Визуализация
marker          // ol.Feature — иконка на карте
markerStyle     // ol.Style — цвет + поворот по track
track_linesegs  // линия трека (массив сегментов)
elastic_feature // эластичная линия (интерполяция)
visible         // отображать ли борт
```

**Цвета иконок по высоте:**
```
На земле          → серый/коричневый
0–5 000 ft        → красный
5 000–10 000 ft   → оранжевый
10 000–15 000 ft  → жёлтый
15 000–20 000 ft  → зелёный
20 000–25 000 ft  → голубой
25 000–35 000 ft  → синий
> 35 000 ft       → фиолетовый
```

Иконки кешируются в `iconCache[altitude_range + rotation]`.

---

### Карта — слои (layers.js)

```
Base Layers (один активен):
  OpenStreetMap, CARTO, ESRI, OpenFreeMap, NASA GIBS, Bing Maps

Overlay Layers (поверх):
  iconLayer         — иконки самолётов
  trailLayers       — треки (ol.layer.Group)
  siteCircleLayer   — кружок вокруг приёмника
  Heatmap, Range outline, openAIP, TFR, NEXRAD, RainViewer, ...
```

---

### Клик на самолёт

```
click на карте
    → OLMap.forEachFeatureAtPixel()    найти feature под курсором
    → feature.getId()                  получить ICAO
    → selectPlaneByHex(icao)
        → SelectedPlane = g.planes[icao]
        → updateSelectedInfo()         обновить info-панель справа
        → подсветить иконку на карте

Двойной клик:
    → follow mode — карта следует за самолётом
    → zoom: 8

Info-панель показывает:
    ICAO, позывной, регистрацию, тип ВС, высоту,
    скорость, курс, squawk, маршрут (если доступен),
    ветер (вычисляется из TAS/GS/heading),
    флаги: Military, PIA, LADD
```

---

### Таблица бортов

- По умолчанию **80 строк** (настраивается до "все")
- Сортируется по любой колонке
- Обновляется в `everySecond()` каждые ~850ms
- Колонки: ICAO, Callsign, Type, Altitude, Speed, Track, Msgs, Seen, RSSI
- Клик на строку → `selectPlaneByHex()` → тот же что и клик на карте

---

### Архив (archive.html)

**Интерфейс:**
- Выбор периода: datetime-local inputs с поддержкой timezone
- Timezone пресеты: UTC, Ижевск (UTC+4), Пермь (UTC+5)
- Сохраняется в `localStorage['adsb18_tz']`

**API запросы:**
```
1. GET /api/archive?from=ISO&to=ISO
   → список рейсов: [{icao, callsign, first_seen, last_seen, max_altitude, max_speed, points, type_code, description}]

2. GET /api/history?icao=ABC&from=ISO&to=ISO&limit=10000
   → позиции одного рейса: [{lat, lon, altitude, ground_speed, ts, track}]
   (LIMIT 10000 — все точки)

3. POST /api/history/bulk
   body: [{icao, from, to, limit}]   ← массив рейсов, limit по умолч. 500
   → [{icao, points: [...]}]
   Decimation: берёт до 10000 точек из БД, равномерно сэмплирует до limit.
   Используется кнопкой "Все" в архиве.

4. DELETE /api/flight?icao=ABC&from=ISO&to=ISO
   → удалить рейс из архива (с подтверждением)
```

**Карта архива:**
- Leaflet (не OpenLayers!)
- Трек: polyline + circle markers каждые N км
- Старт: зелёный кружок, финиш: красный кружок со стрелкой
- Несколько треков одновременно разными цветами

---

### WebSocket (/ws)

**НЕ используется фронтендом.** Все данные через HTTP GET polling.
WebSocket эндпоинт существует на сервере но браузер его не подключает.
Можно использовать в будущем для снижения задержки.

---

### Зависимости фронтенд → API

| Что | Endpoint | Когда |
|-----|----------|-------|
| Живые борты | `GET /data/aircraft.json` | каждые ~1с |
| Конфиг приёмника | `GET /data/receiver.json` | при старте |
| Трек живого борта | `GET /data/traces/{last2}/trace_full_{icao}.json` | при клике |
| Список рейсов архива | `GET /api/archive?from=&to=` | по кнопке |
| История рейса (один) | `GET /api/history?icao=&from=&to=` | при клике на рейс |
| История рейсов (все) | `POST /api/history/bulk` | по кнопке "Все" |
| Удалить рейс | `DELETE /api/flight` | по кнопке |

---

### Риски фронтенда

**Риск 11 — gzip кеш nginx:**
После изменения любого JS файла обязательно:
`gzip -k -f /home/new/adsb18/frontend/script.js`
Иначе nginx отдаёт старый `.gz` и браузер не видит изменений.

**Риск 12 — Leaflet vs OpenLayers:**
Live карта использует OpenLayers, архив — Leaflet.
Это разные библиотеки с разными API. Не путать при правках.

**Риск 13 — cache busting:**
При "времени идущем назад" фронтенд добавляет `?timestamp` к URL.
Если это происходит постоянно — значит сервер отдаёт устаревшие данные.
