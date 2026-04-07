# adsb18 — ADS-B сервер сбора и визуализации данных о воздушных судах

Система принимает сигналы ADS-B от Raspberry Pi с RTL-SDR приёмником,
хранит данные в PostgreSQL и отображает воздушную обстановку на интерактивной карте.

---

## Архитектура

```
[RTL-SDR] → [readsb на Pi] → aircraft.json (:80)
                                    │
                         SSH reverse tunnel (-R 30092:localhost:80)
                                    │
                             VPS :30092 (HTTP туннель)
                                    │ curl каждую секунду
                             [adsb18-poller]
                                    │
                             [PostgreSQL 16]
                                    │
                             [FastAPI :9001]
                                    │
                             [nginx :8098]
```

Pi инициирует SSH-туннель на VPS. VPS сам забирает `aircraft.json` через этот туннель — поллер-архитектура без сложного протокола.

---

## Деплой VPS

### Новый VPS (Ubuntu/Debian, от root)

```bash
git clone https://github.com/gaveron18/adsb18.git /opt/adsb18
cd /opt/adsb18
sudo bash deploy.sh
```

Скрипт устанавливает:
- PostgreSQL 16, создаёт БД и схему
- Python venv + зависимости
- systemd сервисы: `adsb18-poller`, `adsb18-api`
- nginx на порту 8098
- UFW: открывает порт 8098

После запуска скрипт напечатает инструкцию — нужно вручную добавить SSH-ключ Pi в `~/.ssh/authorized_keys`.

Карта: **http://&lt;IP&gt;:8098**

### Обновление VPS

```bash
cd /opt/adsb18
git pull
sudo systemctl restart adsb18-poller
sudo systemctl restart adsb18-api
```

---

## Деплой Pi

### Требования
- Raspberry Pi с RTL-SDR антенной
- `readsb` установлен и работает
- `tar1090` отдаёт `aircraft.json` на `localhost:80/tar1090/data/aircraft.json`

Установка readsb: https://github.com/wiedehopf/readsb  
Установка tar1090: https://github.com/wiedehopf/tar1090

### Установка туннеля (один раз)

**Шаг 1** — скопировать скрипты с VPS на Pi:

```bash
# С VPS (туннель ещё не работает — копируем через обычный SSH):
scp feeder/install.sh feeder/adsb-tunnel.service pi@<IP_PI>:/tmp/
```

**Шаг 2** — запустить на Pi:

```bash
sudo bash /tmp/install.sh --vps-ip <IP_VPS> --vps-user <USER>
```

Скрипт:
- установит `autossh`
- создаст пользователя `ads-b`
- сгенерирует SSH-ключ
- установит и запустит `adsb-tunnel.service`

**Шаг 3** — добавить ключ Pi на VPS:

```bash
# Скрипт напечатает публичный ключ. На VPS выполнить:
echo 'ПУБЛИЧНЫЙ_КЛЮЧ' >> ~/.ssh/authorized_keys
```

**Шаг 4** — проверить что туннель работает:

```bash
# На VPS:
curl -s http://127.0.0.1:30092/tar1090/data/aircraft.json | head -c 100
```

### Для второго VPS (prod)

```bash
sudo bash /tmp/install.sh \
  --vps-ip <IP_PROD> \
  --vps-user root \
  --http-port 30093 \
  --ssh-port 52223 \
  --service-name adsb-tunnel-prod
```

### Обновление туннеля на Pi

Если изменился `feeder/adsb-tunnel.service` в репо:

```bash
# С VPS:
bash feeder/update_pi.sh
```

Скрипт покажет diff и применит изменения.

---

## Сервисы

| Сервис | Где | Порт | Описание |
|--------|-----|------|----------|
| adsb18-poller | VPS | — | Опрашивает Pi каждую секунду, пишет в БД |
| adsb18-api | VPS | 9001 | FastAPI — REST API + WebSocket |
| nginx | VPS | 8098 | Отдаёт фронтенд + проксирует /api/ и /data/ |
| PostgreSQL 16 | VPS | 5432 | База данных |
| adsb-tunnel | Pi | — | SSH reverse tunnel → VPS :30092 (HTTP) + :52222 (SSH) |

---

## Логи и статус

```bash
# VPS:
sudo systemctl status adsb18-poller adsb18-api
sudo journalctl -u adsb18-poller -f
sudo journalctl -u adsb18-api -f

# Pi:
sudo systemctl status adsb-tunnel readsb
sudo journalctl -u adsb-tunnel -f
```

---

## API

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | `/data/aircraft.json` | Текущие борты (прокси Pi или fallback из БД) |
| GET | `/api/history?icao=&from=&to=` | Трек борта за период |
| GET | `/api/archive?from=&to=` | Список рейсов за период |
| GET | `/api/feeders` | Список приёмников |
| GET | `/api/monitor` | Сравнение Pi live vs БД |
| DELETE | `/api/flight?icao=&from=&to=` | Удалить рейс из архива |
| WS | `/ws` | aircraft.json каждую секунду |

---

## Структура проекта

```
adsb18/
├── deploy.sh                # деплой VPS с нуля
├── nginx.conf               # конфиг nginx
├── feeder/
│   ├── install.sh           # установка туннеля на Pi (один раз)
│   ├── update_pi.sh         # обновление туннеля на Pi
│   └── adsb-tunnel.service  # шаблон systemd сервиса туннеля
├── frontend/
│   ├── index.html           # живая карта (OpenLayers / tar1090)
│   └── archive.html         # архив рейсов (Leaflet)
└── server/
    ├── db/init.sql          # схема БД (positions, aircraft, feeders)
    ├── ingest/
    │   ├── poller.py        # опрашивает Pi aircraft.json, пишет в БД
    │   └── db.py            # буфер + PostgreSQL writer
    └── api/main.py          # FastAPI
```

---

## Лицензия

MIT
