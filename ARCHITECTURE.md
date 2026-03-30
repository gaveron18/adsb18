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
│    • /api/history?icao=&from=&to=                    │
│    • /api/aircraft?hours=24                          │
│    • /api/archive?from=&to=                          │
│    • /api/feeders                                    │
│    • /api/monitor  ← Pi vs Server сравнение          │
│    • DELETE /api/flight                              │
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
`_last_pos_ts[icao]` хранит `seen_pos` timestamp последней записанной позиции.
Если dump1090 не слышал борт — `seen_pos` не меняется, дубль в DB не пишется.

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
