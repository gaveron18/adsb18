# adsb18

Система сбора, хранения и визуализации данных ADS-B.

**Живой адрес:** http://173.249.2.184:8099 *(скоро)*

## Архитектура

```
[RTL-SDR] → [dump1090 на Raspberry Pi] → [feeder]
                                               ↓ TCP
                                    [сервер — ingest]
                                               ↓
                                         [PostgreSQL]
                                               ↓
                                    [FastAPI + WebSocket]
                                               ↓
                                      [Карта Leaflet]
```

## Компоненты

| Папка | Назначение |
|-------|-----------|
| `feeder/` | Скрипт для Raspberry Pi — читает SBS из dump1090, шлёт на сервер |
| `server/ingest/` | TCP-сервер — принимает данные от feeder, парсит SBS, пишет в БД |
| `server/api/` | FastAPI — REST + WebSocket для фронтенда |
| `server/db/` | Схема БД, миграции |
| `frontend/` | Веб-карта (Leaflet + OpenStreetMap) |

## Быстрый старт

```bash
# На сервере
docker-compose up -d

# На Raspberry Pi
cd feeder
pip install -r requirements.txt
python feeder.py --server 173.249.2.184 --port 30001
```

## Требования

- Raspberry Pi 3/5 + RTL-SDR + dump1090 или readsb
- Сервер: Docker, Docker Compose
- Python 3.11+
