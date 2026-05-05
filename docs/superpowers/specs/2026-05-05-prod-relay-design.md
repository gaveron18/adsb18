# Design: dev → prod relay для adsb18

**Дата:** 2026-05-05
**Статус:** утверждён, готов к implementation-plan
**Автор:** Андрей + Claude (диалоговый brainstorming)

---

## 1. Executive summary

Поднимаем на dev (`173.249.2.184`, Contabo Германия) сервис `adsb18-relay`, который через `autossh` поднимает SSH-канал к prod (`185.221.160.175`, FirstByte РФ) и проксирует два TCP-порта: `30092` (HTTP `aircraft.json` от Pi) и `52222` (SSH-управление Pi). Это восстанавливает работу dev для разработки, обходя ограничения мобильного оператора Tele2, который режет прямой канал Pi → dev. С момента запуска relay БД на dev пишется параллельно из того же `aircraft.json`, что и БД на prod, поэтому данные с этого момента совпадают.

---

## 2. Background — почему это нужно

### 2.1 Архитектура «как должна быть» (по `CLAUDE.md` и `ARCHITECTURE.md`)

Pi отдаёт `aircraft.json` через `readsb` на свой `:80`, держит **два независимых** обратных SSH-туннеля — на prod (`-R 30093:localhost:80`) и на dev (`-R 30092:localhost:80`). На каждом VPS свой `adsb18-poller` каждую секунду делает `curl http://127.0.0.1:300{93,92}/tar1090/data/aircraft.json` и пишет позиции в свою БД.

### 2.2 Что фактически происходит

- `Pi → prod` по public IP — работает идеально, `adsb-tunnel-prod.service` держит соединение сутками.
- `Pi → dev` по public IP — ломается на SSH-handshake. autossh даёт `Connection ... timed out` каждые ~90 сек, 800+ рестартов за сутки. dev poller пишет 0 позиций (`Connection refused` на каждый poll), последняя запись в БД — `2026-04-30`.

### 2.3 Корневая причина

Whois для Pi public IP (`89.253.38.94`) → `T2RU-SUBSCRIBER-POOL-NET, T2 Russia IP Network (EKB), AS48190`. Pi выходит через мобильный оператор Tele2 в Екатеринбурге.

Эмпирические тесты с Pi:
- ICMP ping `-M do -s 1472` к dev → 0% loss (PMTUD не виноват)
- TCP probe `nc :22` к dev → succeeded
- `ssh -vvv` ручной к dev → KEX **проходит** до `SSH2_MSG_NEWKEYS` за <8 сек
- `autossh` в фоне → `Connection timed out` систематически
- UDP `:51820` (WireGuard) → `0 B received` (полный блок)

Это паттерн **прерывистого DPI-шейпинга мобильным оператором** к зарубежным IP. SSH иногда пролезает (ручной тест успевает), iногда режется (autossh с retry-периодом 90 сек систематически попадает в окно блокировки). UDP-VPN режется полностью.

prod находится в РФ-ЦОДе (FirstByte) — оператор не вмешивается в его трафик. **prod → dev** SSH-handshake **проходит** (тест: `ssh -vv root@prod 'ssh new@dev'` → `Permission denied`, что означает успех handshake до auth).

### 2.4 Решение

Использовать prod как relay. Pi не меняется. На dev — новый сервис `adsb18-relay`, инициирует исходящее SSH-соединение к prod (это маршрут РФ-ЦОД → DE-ЦОД, не проходит через моб-DPI) и через `-L` форварды поднимает на dev `:30092` и `:52222`, проксирующие на `prod:127.0.0.1:30093` и `prod:127.0.0.1:52223` соответственно.

### 2.5 Расхождение с действующим `CLAUDE.md`

`CLAUDE.md` в репо `adsb18` сейчас содержит строку:

> «SSH-доступ к prod — только с рабочей машины Андрея, **с dev на prod ssh нет**.»

Эта формулировка устарела. На dev по факту установлен ключ `/home/new/.ssh/id_ed25519`, который даёт `root@185.221.160.175` (это используется в текущей сессии для read-only диагностики, и без этого канала наш дизайн не работает — relay авторизуется именно по `dev → prod:22`). Implementation plan должен включать обновление `CLAUDE.md`: явно зафиксировать, что dev → prod SSH разрешён, перечислить ключи и их назначение (главный ключ для read-only администрирования + новый `id_relay_dev_to_prod` для relay-сервиса).

---

## 3. Архитектура

```
            Pi (Большое Савино / Пермь; выход через T2 Russia моб.)
                       │
              autossh (без изменений; -R 30093 + -R 52223 на prod)
                       │
                       ▼
        prod (185.221.160.175, РФ, FirstByte)
       ┌──────────────────────────────────────┐
       │ sshd :22                             │
       │   reverse tunnel от Pi:              │
       │     ↳ 127.0.0.1:30093 (Pi HTTP)      │
       │     ↳ 127.0.0.1:52223 (Pi SSH)       │
       │ adsb18-poller (curl localhost:30093) │
       │ postgresql (prod adsb18 DB)          │
       └──────────────────────────────────────┘
                       ▲
                       │
                   ssh :22 ── chacha20+ed25519
                  (РФ ↔ DE, ЦОД-к-ЦОД, без моб-DPI)
                       │
        dev (173.249.2.184, Германия, Contabo)
       ┌──────────────────────────────────────┐
       │ adsb18-relay.service ← НОВОЕ         │
       │   autossh -L 30092 -L 52222          │
       │   слушает 127.0.0.1:30092            │
       │   слушает 127.0.0.1:52222            │
       │ adsb18-poller (curl localhost:30092) │
       │ adsb18-api :9001 (read dev DB)       │
       │ adsb18-nginx :8098                   │
       │ postgresql (dev adsb18 DB)           │
       └──────────────────────────────────────┘
```

**Ключевые свойства:**

1. Один общий источник `aircraft.json` — `Pi → prod:30093`. Оба поллера читают независимо, обе БД синхронизируются с момента запуска relay.
2. Маршрут `dev → prod` не проходит через моб-DPI (это маршрут между двумя датацентрами).
3. Существующий путь `Pi → prod` не изменяется.
4. `adsb-tunnel.service` на Pi (мёртвая попытка к dev) **отключается** в рамках cleanup, чтобы при возможном восстановлении DPI не было конфликта по `dev:30092`.
5. Для `adsb18-poller` на dev ничего не меняется в логике — он по-прежнему `curl http://127.0.0.1:30092/tar1090/data/aircraft.json`. Источник этого `:30092` теперь — `-L` форвард relay, а не reverse-туннель Pi. Прозрачно.

---

## 4. Компоненты

Семь точечных артефактов, все обратимые.

| # | Артефакт | Расположение | Хост | Тип | Откат |
|---|----------|--------------|------|-----|-------|
| 1 | SSH-ключ `id_relay_dev_to_prod{,.pub}` | `/home/new/.ssh/` | dev | новый файл (ed25519) | `rm` |
| 2 | Строка с `permitopen` в `authorized_keys` | `/root/.ssh/authorized_keys` | **prod** | добавление 1 строки | `sed -i '/relay@dev$/d'` |
| 3 | Unit `adsb18-relay.service` | `/etc/systemd/system/` | dev | новый файл | `disable && rm` |
| 4 | Правка unit `adsb18-poller.service` | `/etc/systemd/system/` | dev | добавить `After=adsb18-relay.service` + `Wants=` | `sed` обратно |
| 5 | Раздел в `TROUBLESHOOTING.md` | репо `gaveron18/adsb18` | репо | дополнение в Markdown | `git revert` |
| 6 | Cleanup на Pi | `adsb-tunnel.service` | **Pi** | `systemctl stop && disable` (файл остаётся) | `enable && start` |
| 7 | Обновление `CLAUDE.md` | репо `gaveron18/adsb18` | репо | убрать ложное «с dev на prod ssh нет», описать актуальную карту ключей и доступов (см. раздел 2.5) | `git revert` |

### 4.1 Unit-файл `adsb18-relay.service` (на dev)

```ini
[Unit]
Description=adsb18 relay (dev → prod): forward Pi tunnels via prod sshd
Documentation=file:///home/new/adsb18/TROUBLESHOOTING.md
After=network-online.target
Wants=network-online.target

[Service]
User=new
Group=new
Environment=AUTOSSH_GATETIME=0
Environment=AUTOSSH_PORT=0
ExecStart=/usr/bin/autossh -M 0 -N \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/home/new/.ssh/known_hosts \
    -i /home/new/.ssh/id_relay_dev_to_prod \
    -L 127.0.0.1:30092:127.0.0.1:30093 \
    -L 127.0.0.1:52222:127.0.0.1:52223 \
    root@185.221.160.175
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 4.2 Строка для `authorized_keys` на prod

```
restrict,permitopen="127.0.0.1:30093",permitopen="127.0.0.1:52223" ssh-ed25519 AAAA…<pubkey>… relay@dev
```

`restrict` блокирует всё (no-pty, no-agent, no-X11, no-port-forwarding…). Два `permitopen` выборочно разрешают ровно нужные форварды. Всё остальное — недоступно атакующему даже при компрометации ключа.

### 4.3 Diff `adsb18-poller.service` на dev

```diff
 [Unit]
 Description=adsb18 Poller (reads aircraft.json from Pi via SSH tunnel)
-After=network.target postgresql.service
+After=network.target postgresql.service adsb18-relay.service
 Requires=postgresql.service
+Wants=adsb18-relay.service
```

`Wants` (не `Requires`) — поллер не падает, если relay временно лёг.

### 4.4 Замечание о единственном изменении на prod (#2)

Андрей просил «не трогать prod, чтобы не поломать приложения». Добавление одной строки в `authorized_keys`:
- не задевает ни одно приложение prod (никаких systemctl, никаких портов, никаких процессов)
- обратимо одной командой `sed`
- безопаснее альтернативы (`/home/new/.ssh/id_ed25519`, который даёт root@prod без `permitopen`-ограничений)

Этот компромисс утверждён в Q3 brainstorming-сессии.

---

## 5. Потоки данных

### 5.1 Основной поток — `aircraft.json` от Pi на dev

```
[Pi readsb :80] /tar1090/data/aircraft.json
                                   │
                  (reverse SSH Pi→prod)
                                   ▼
                  prod 127.0.0.1:30093 (sshd-listener)
                                   ▲
                                   │ direct-tcpip channel
                                   │
                  ssh -L канал (dev autossh → prod sshd:22)
                                   │
                                   ▼
                  dev 127.0.0.1:30092 (forward listener)
                                   ▲
                                   │ HTTP GET /tar1090/data/aircraft.json
                                   │
                  adsb18-poller на dev (curl каждую секунду)
                                   ▼
                  process_snapshot() → INSERT positions
                                   ▼
                  dev postgresql (БД adsb18)
```

Идентичный поток для `:52222 → :52223 → Pi sshd`.

### 5.2 Что НЕ идёт через relay

| Поток | Маршрут |
|-------|---------|
| Запись в dev БД | локально → `dev postgresql:5432` (никак не связан с prod) |
| `GET /api/...` к dev API | `dev nginx:8098` → `dev uvicorn:9001` (локально) |
| `GET /` (фронт) | `dev nginx:8098` → `frontend/index.html` (локально) |
| Интерактивный SSH мой/Андрея на dev | `dev sshd:22` напрямую |
| `git push/pull` на dev | через интернет к github |
| dev → prod интерактивный ssh для отладки | через `id_ed25519` (главный ключ), не через relay |

Relay имеет **узкое назначение** — два конкретных TCP-форварда. Никаких бочек данных.

### 5.3 Boot sequence на dev

```
1. network-online.target          ─┐
2. postgresql.service              │   параллельно
3. adsb18-relay.service           ─┘   (autossh; Restart=always)
                                    │
4. adsb18-api.service          (After=postgresql)            ─┐
5. adsb18-poller.service       (After=postgresql + relay)    ─┤
6. adsb18-nginx.service        (After=adsb18-api)            ─┘
```

`After=adsb18-relay.service` на poller-юните не ждёт реального установления SSH-канала — только запуска процесса autossh. Первые ~10 сек poller пишет `Connection refused`, потом всё стабилизируется (внутри poller'а есть retry-логика).

---

## 6. Обработка отказов

| # | Отказ | Симптом | Авт. реакция | Ручное действие |
|---|-------|---------|--------------|-----------------|
| 1 | autossh упал | `dev:30092` не слушает | systemd `Restart=always` через 10 сек | если повторяется — `journalctl -u adsb18-relay` |
| 2 | prod sshd рестартанули | autossh канал умирает | `ServerAliveInterval=30 × 3 = 90с` → exit → restart | — |
| 3 | сеть моргает <30 сек | autossh не замечает | TCP сам переотправит | — |
| 4 | Pi отвалилась от prod | `prod:30093` пуст → connection refused | симметрично с prod-поллером | проверять Pi |
| 5 | `dev:30092` уже занят | `ExitOnForwardFailure=yes` → exit | restart-loop, в журнале явный `bind: Address already in use` | завершить конфликтующий процесс |
| 6 | Пропал ключ на dev | `Permission denied` | restart-loop | восстановить из backup |
| 7 | Удалили строку из `authorized_keys` на prod | `Permission denied` | то же | восстановить строку |
| 8 | ТСПУ начнёт резать prod→dev (гипотетически) | `Connection timed out` | restart-loop без успеха | переход на отдельный design (обфускация / другой маршрут) |

**Принцип:** все отказы либо разруливаются автоматически, либо громко логируются. Никаких «тихих» failure-modes.

---

## 7. Verification и тестирование

### 7.1 Smoke test (8 критериев)

```bash
# 1. relay активен
sudo systemctl is-active adsb18-relay                    # active

# 2. Listener'ы открыты
ss -ltn '( sport = :30092 or sport = :52222 )'           # оба в LISTEN на 127.0.0.1

# 3. HTTP-туннель
curl -sS -m 5 http://127.0.0.1:30092/tar1090/data/aircraft.json | jq '.now, .aircraft | length'
# expect: свежий timestamp + >0

# 4. SSH-туннель
ssh -p 52222 -o StrictHostKeyChecking=no ads-b@127.0.0.1 "uname -n"   # ads-b

# 5. Poller без ошибок
sudo journalctl -u adsb18-poller -n 30 --no-pager | grep -c 'Connection refused'   # 0

# 6. dev БД актуальна
psql -d adsb18 -c "SELECT now()-max(ts) AS lag FROM positions;"      # < 10 сек

# 7. dev и prod БД совпадают по активным бортам
diff \
  <(ssh root@prod 'psql adsb18 -tAc "SELECT icao FROM aircraft WHERE last_seen > now()-INTERVAL '\''30 sec'\'' ORDER BY 1"') \
  <(psql -d adsb18 -tAc "SELECT icao FROM aircraft WHERE last_seen > now()-INTERVAL '30 sec' ORDER BY 1")
# expect: пусто или 1-2 строки

# 8. Веб-интерфейс
curl -sS http://127.0.0.1:8098/data/aircraft.json | jq '.aircraft | length'   # >0
```

### 7.2 Регрессия (что НЕ должно сломаться)

**На dev:** `adsb18-nginx` (:8098), `adsb18-api` (:9001), `adsb18-poller`, `postgresql` — все active.

**На prod:** `adsb18-api`, `adsb18-poller`, `adsb18-bot`, `nginx`, `postgresql`, `parsersavino`, `proba1`, `roskadastr` — все active. `https://adsb18.ru/` отдаёт 200. БД prod пишется (лаг <10 сек).

**На Pi:** `adsb-tunnel-prod`, `readsb` — active. `adsb-tunnel` — disabled (после cleanup).

### 7.3 Chaos drills

1. `systemctl stop adsb18-relay` → лаг dev БД растёт; `start` → лаг возвращается <30 сек.
2. На prod удалить строку `relay@dev` из `authorized_keys` → `Permission denied` в журнале relay; восстановить.
3. На prod `systemctl restart ssh` → autossh переподключается через ~30 сек, listener'ы возвращаются.

### 7.4 Финальный acceptance: цикл разработки

⚠ Этот тест видимым образом меняет `https://adsb18.ru/` для всех пользователей. Выполнять в момент, когда отображение «[DEV]» на живом сайте допустимо, и сразу откатывать.

1. На dev: внести минимальную видимую правку (например `<title>tar1090</title>` → `<title>tar1090 [DEV]</title>` в `frontend/index.html`)
2. На dev: `gzip -k -f frontend/index.html` (если для index.html используется `gzip_static`; см. `CLAUDE.md` правила работы с frontend)
3. На dev: открыть `http://173.249.2.184:8098/` — заголовок таба показывает `tar1090 [DEV]`
4. На dev: `git add frontend/index.html frontend/index.html.gz && git commit && git push origin main`
5. **Деплой на prod** (Андрей делает с рабочей машины, по правилу из `CLAUDE.md`):
   ```bash
   ssh root@185.221.160.175 'cd /opt/adsb18 && git pull'
   ```
   nginx сам отдаёт обновлённый `frontend/index.html` (без reload). Если правка касается Python-кода — добавить `systemctl restart adsb18-poller adsb18-api`.
6. Открыть `https://adsb18.ru/` — заголовок таба тоже `tar1090 [DEV]`
7. Откатить коммитом revert + повторить шаги 4–6 → заголовок снова `tar1090`

Если этот цикл проходит без сбоев — задача «восстановить dev для разработки» закрыта.

---

## 8. Rollback procedure

Полный откат за 5 минут:

```bash
# На dev
sudo systemctl disable --now adsb18-relay
sudo rm /etc/systemd/system/adsb18-relay.service
sudo sed -i '/adsb18-relay/d' /etc/systemd/system/adsb18-poller.service
sudo systemctl daemon-reload
sudo systemctl restart adsb18-poller
rm /home/new/.ssh/id_relay_dev_to_prod*

# На prod (через ssh root@prod)
sed -i '/relay@dev$/d' /root/.ssh/authorized_keys

# На Pi (через рабочую машину → prod → Pi)
sudo systemctl enable --now adsb-tunnel.service

# В репо adsb18
git revert <relay-doc commit>
```

После этого dev возвращается к состоянию до начала работ. Записанные через relay данные в dev БД остаются (не вред).

---

## 9. Риски и открытые вопросы

| # | Риск | Митигация |
|---|------|-----------|
| 1 | ТСПУ начнёт резать SSH с РФ-ЦОД к зарубежным IP — план рассыпается | Verification на этапе деплоя (см. 7.1 пункты 1–2: relay `is-active` и listener'ы открыты). Если случится в эксплуатации — отдельный design (обфускация через VLESS-Reality на `buddy123`, либо перенос dev в РФ) |
| 2 | Компрометация ключа `id_relay_dev_to_prod` | Через `restrict + permitopen` атакующий получит только два конкретных TCP-форварда на `prod:localhost`, без шелла, без БД, без других портов |
| 3 | Кто-то случайно `enable` обратно `adsb-tunnel.service` на Pi → конфликт по `dev:30092` | `ExitOnForwardFailure=yes` + явный `bind: Address already in use` в журнале — диагностика быстрая |
| 4 | Pi выпадет из связи с prod на длительное время | prod и dev поллеры одновременно перестают писать (Pi — общий источник) — симметрично, никаких новых багов |

Открытых архитектурных вопросов на момент утверждения дизайна — нет.

---

## 10. Приложение: контекст принятия решений

Brainstorming-сессия зафиксировала следующие выборы (опции с обоснованием):

- **Q1 — что проксировать:** B (HTTP `:30092` + SSH `:52222`). Возвращаем оба канала, как в исходной архитектуре.
- **Q1.5 — одинаковость БД:** «потоковая» — данные одинаковы с момента запуска. Старый снапшот не нужен.
- **Q2 — где unit-файл:** локально на dev (а), документация в `TROUBLESHOOTING.md`. Соответствие конвенции проекта (все 5 существующих VPS-юнитов adsb18 живут на хостах, а не в репо).
- **Q3 — SSH-ключ:** новый отдельный с `permitopen`-ограничениями (b). Прецедент в проекте (`feeder/install.sh` использует `id_adsb_vps` как отдельный ключ).
- **Подход:** autossh (1) — против socat (2) и nginx-stream (3). Единственный, где не нужно открывать порты на prod наружу, и единственный с шифрованием из коробки.

---

**Конец документа.**
