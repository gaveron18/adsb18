# adsb18 — CLAUDE.md

## Проект
ADS-B сервер сбора и визуализации данных о воздушных судах.

- **Репозиторий:** https://github.com/gaveron18/adsb18
- **Веб-интерфейс:** http://173.249.2.184:8098
- **Папка на VPS:** `/home/new/adsb18/`

---

## Инфраструктура (PRODUCTION)

### Сервер (VPS, 173.249.2.184)
Деплой через **systemd** (НЕ Docker):

| Сервис | Порт | Команда |
|--------|------|---------|
| PostgreSQL 16 | 5432 | `sudo systemctl status postgresql` |
| adsb18-ingest | 30001 | `sudo systemctl status adsb18-ingest` |
| adsb18-api | 9001 | `sudo systemctl status adsb18-api` |
| nginx | 8098 | `sudo systemctl status nginx` |

- Virtualenv: `/opt/adsb18-venv/`
- Логи: `sudo journalctl -u adsb18-ingest -f` / `sudo journalctl -u adsb18-api -f`

### Raspberry Pi (фидер)
- IP в локальной сети: 192.168.1.170, пользователь: ads-b
- Прямого доступа с VPS нет — используется обратный SSH-туннель
- **Зайти на Pi с VPS:** `ssh -p 52222 ads-b@127.0.0.1`
- Туннель: `-R 52222:localhost:22` (SSH) + `-L 30091:localhost:30001` (данные)
- Перезапуск туннеля: `sudo systemctl restart adsb-tunnel` (SSH оборвётся на ~15 сек)
- Pi подключается на `127.0.0.1:30091`, а НЕ напрямую на :30001 (роутер дропает пакеты)

### Переменные окружения ingest
```
DATABASE_URL=postgresql://adsb:adsb2024@localhost:5432/adsb18
INGEST_HOST=0.0.0.0
INGEST_PORT=30001
```

---

## Архитектура

```
[RTL-SDR на Pi] → [dump1090] → [feeder.py] → TCP :30091 → SSH-туннель → :30001
                                                                              ↓
                                                                    [ingest/main.py]
                                                                              ↓
                                                                    [PostgreSQL 16]
                                                                              ↓
                                                                    [FastAPI :9001]
                                                                              ↓
                                                                    [nginx :8098]
```

Стек: Python 3.12, FastAPI, asyncpg, PostgreSQL 16, nginx, systemd.

---

## База данных

- `positions` — партиционирована по месяцам (positions_2026_03, etc.)
- `aircraft` — живое состояние каждого борта
- `feeders` — подключённые приёмники
- Партиции старше 6 месяцев удаляются автоматически
- `partition_watchdog()` создаёт следующую партицию каждые 24ч

---

## Известные проблемы (все решены)

1. **asyncio starvation в ingest** — `await asyncio.sleep(0)` каждые 50 строк по `line_count` (НЕ `msg_count` — MSG,8 парсится как None)
2. **Партиции БД** — при смене месяца нет партиции → INSERT падают. Фикс: `partition_watchdog()`
3. **Роутер Pi дропает пакеты к VPS:30001** — трафик идёт через SSH-туннель
4. **asyncio starvation в feeder.py** — `await asyncio.sleep(0)` + `drain()` каждые 50 сообщений
5. **nginx Permission denied** — `sudo chmod o+x /home/new`
6. **UFW**: порт 30001 должен быть открыт (`sudo ufw allow 30001/tcp`)

Подробности: **TROUBLESHOOTING.md** в этом репозитории.

---

## Правила работы

- Деплой: **systemd, не Docker** — `docker-compose.yml` оставлен для справки, НЕ использовать на проде
- При любых изменениях ingest или api: `sudo systemctl restart adsb18-ingest` / `sudo systemctl restart adsb18-api`
- После фикса бага — добавить запись в **TROUBLESHOOTING.md**
- В начале сессии читать TROUBLESHOOTING.md чтобы не повторять старые ошибки

---

## Pi — правила изменений и деплоя

**Репо = единственный источник правды для Pi.**
Все изменения кода и конфига Pi делаются в репо, затем применяются скриптом.

### Изменить что-то на Pi:
1. Внести изменение в репо (`feeder/feeder.py`, `feeder/adsb-tunnel.service`, `feeder/adsb18-feeder.service`)
2. Закоммитить и запушить
3. Применить на Pi:
```bash
git pull
bash feeder/update_pi.sh
```

### Проверить расхождения без применения:
```bash
bash feeder/update_pi.sh --check
```

### Деплой на новый Pi (с нуля):
```bash
# На новом Pi:
git clone https://github.com/gaveron18/adsb18.git
sudo bash adsb18/feeder/install.sh --vps-ip 173.249.2.184 --vps-user new --name ИМЯ-PI

# На VPS — добавить публичный ключ нового Pi:
echo 'ПУБЛИЧНЫЙ_КЛЮЧ' >> ~/.ssh/authorized_keys
```

**Никогда не редактировать файлы напрямую на Pi** — при следующем `update_pi.sh` они будут перезаписаны из репо.
