# Prod-Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Поднять на dev сервис `adsb18-relay` (autossh dev → prod) с двумя `-L` форвардами, чтобы dev-поллер снова получал `aircraft.json` и был доступен SSH к Pi через `:52222`. Источник истины — `docs/superpowers/specs/2026-05-05-prod-relay-design.md`.

**Architecture:** Один `autossh` на dev в systemd-юните. Подключается к `root@185.221.160.175:22` ключом `id_relay_dev_to_prod`, который на prod ограничен `restrict + permitopen` ровно на нужные два TCP-форварда. Pi не меняется в основной части; в cleanup-задаче выключаем мёртвый `adsb-tunnel.service` (Pi → dev).

**Tech Stack:** OpenSSH, autossh, systemd, bash, sed.

**Hosts (где исполняются шаги):**
- **dev** = `new@173.249.2.184` — основные изменения
- **prod** = `root@185.221.160.175` — одна строка в `authorized_keys`
- **Pi** = `ads-b@127.0.0.1` через `ssh -J root@185.221.160.175 -p 52223` — cleanup `adsb-tunnel.service`

---

## File Structure

| Файл | Хост | Назначение |
|------|------|------------|
| `/home/new/.ssh/id_relay_dev_to_prod{,.pub}` | dev | новый ed25519 ключ только для relay-сервиса |
| `/etc/systemd/system/adsb18-relay.service` | dev | новый unit autossh |
| `/etc/systemd/system/adsb18-poller.service` | dev | правка: `After=` и `Wants=` для adsb18-relay |
| `/root/.ssh/authorized_keys` | prod | +1 строка с `restrict,permitopen=...` для relay@dev |
| `TROUBLESHOOTING.md` (репо) | репо adsb18 | новый раздел про relay |
| `CLAUDE.md` (репо) | репо adsb18 | убрать ложное «с dev на prod ssh нет», описать карту ключей |
| `adsb-tunnel.service` (на Pi) | Pi | `systemctl stop && disable`, файл остаётся |

---

## Task 1: Baseline и подготовка

**Files:** только чтение для записи baseline.

- [ ] **Step 1.1: Записать baseline активных сервисов на dev**

Run on dev:
```bash
mkdir -p /tmp/relay_baseline
sudo systemctl status adsb18-api adsb18-poller adsb18-nginx postgresql --no-pager > /tmp/relay_baseline/dev_services_before.txt 2>&1
sudo ss -ltnp 2>/dev/null > /tmp/relay_baseline/dev_listeners_before.txt
```
Expected: оба файла созданы, размер > 0. Без `sudo` `ss -p` не покажет owner-процессы для чужих пользователей.

- [ ] **Step 1.2: Записать baseline на prod**

Run on dev:
```bash
ssh -i /home/new/.ssh/id_ed25519 root@185.221.160.175 "
  systemctl status adsb18-api adsb18-poller adsb18-bot nginx postgresql --no-pager
  echo --- listeners ---
  ss -ltnp 2>/dev/null
  echo --- authorized_keys count ---
  wc -l /root/.ssh/authorized_keys
" > /tmp/relay_baseline/prod_state_before.txt 2>&1
```
Expected: файл создан, видны все adsb18-сервисы prod как `active`.

- [ ] **Step 1.3: Записать baseline на Pi**

Run on dev:
```bash
ssh -i /home/new/.ssh/id_ed25519 -J root@185.221.160.175 -p 52223 ads-b@127.0.0.1 "
  systemctl is-active adsb-tunnel adsb-tunnel-prod readsb
" > /tmp/relay_baseline/pi_state_before.txt 2>&1
```
Expected: видны все три сервиса как `active`.

- [ ] **Step 1.4: Подтвердить baseline**

```bash
ls -la /tmp/relay_baseline/
```
Expected: 4 файла (`dev_services_before.txt`, `dev_listeners_before.txt`, `prod_state_before.txt`, `pi_state_before.txt`), каждый ненулевого размера.

(Этот task не делает изменений, только фиксирует before-состояние для последующего регрессионного diff. Коммит не нужен.)

---

## Task 2: Сгенерировать SSH-ключ для relay (на dev)

**Files:**
- Create: `/home/new/.ssh/id_relay_dev_to_prod`
- Create: `/home/new/.ssh/id_relay_dev_to_prod.pub`

- [ ] **Step 2.1: Verify ключа ещё нет**

Run on dev:
```bash
ls -la /home/new/.ssh/id_relay_dev_to_prod* 2>&1
```
Expected: `No such file or directory` (т.е. начинаем с чистого состояния).

- [ ] **Step 2.2: Сгенерировать ключ**

Run on dev:
```bash
ssh-keygen -t ed25519 -N '' -C 'relay@dev' -f /home/new/.ssh/id_relay_dev_to_prod
```
Expected: создаются два файла, выводится fingerprint.

- [ ] **Step 2.3: Проверить файлы и права**

```bash
ls -la /home/new/.ssh/id_relay_dev_to_prod*
```
Expected:
- `id_relay_dev_to_prod` имеет права `-rw-------` (600)
- `id_relay_dev_to_prod.pub` имеет права `-rw-r--r--` (644)
- Оба принадлежат `new:new`.

- [ ] **Step 2.4: Записать pubkey в переменную для следующего task**

```bash
PUBKEY=$(cat /home/new/.ssh/id_relay_dev_to_prod.pub)
echo "PUBKEY=$PUBKEY"
```
Expected: строка вида `ssh-ed25519 AAAAC3... relay@dev`.

(Коммит не нужен — приватный ключ не должен попадать в репо.)

---

## Task 3: Установить публичный ключ в `authorized_keys` на prod

**Files:**
- Modify: `/root/.ssh/authorized_keys` на prod (добавление 1 строки)

- [ ] **Step 3.1: Сделать backup authorized_keys на prod**

Run on dev:
```bash
ssh -i /home/new/.ssh/id_ed25519 root@185.221.160.175 "
  cp /root/.ssh/authorized_keys /root/.ssh/authorized_keys.bak.$(date +%Y%m%d-%H%M)
  ls -la /root/.ssh/authorized_keys*
"
```
Expected: видны исходный файл + backup с timestamp.

- [ ] **Step 3.2: Verify пока строки relay@dev нет**

Run on dev:
```bash
ssh -i /home/new/.ssh/id_ed25519 root@185.221.160.175 "grep -c 'relay@dev$' /root/.ssh/authorized_keys || true"
```
Expected: `0`.

- [ ] **Step 3.3: Добавить ограниченную строку**

Run on dev (ОДНА команда; PUBKEY читается из dev'овского pub-файла и через ssh stdin кладётся на prod):
```bash
PUBKEY=$(cat /home/new/.ssh/id_relay_dev_to_prod.pub)
RESTRICTED='restrict,command="false",permitopen="127.0.0.1:30093",permitopen="127.0.0.1:52223" '"$PUBKEY"
echo "$RESTRICTED" | ssh -i /home/new/.ssh/id_ed25519 root@185.221.160.175 "cat >> /root/.ssh/authorized_keys"
```
Expected: команда отрабатывает без ошибок, ничего в stdout.

⚠ Обрати внимание на кавычки: внешние одиночные `'…'` сохраняют двойные `"` буквально, а `'"$PUBKEY"'` подставляет переменную. Это критично — двойные кавычки вокруг `false` и портов должны попасть в `authorized_keys` как есть.

- [ ] **Step 3.4: Verify строка появилась и в правильном формате**

Run on dev:
```bash
ssh -i /home/new/.ssh/id_ed25519 root@185.221.160.175 "grep 'relay@dev$' /root/.ssh/authorized_keys"
```
Expected: одна строка вида `restrict,command="false",permitopen="127.0.0.1:30093",permitopen="127.0.0.1:52223" ssh-ed25519 AAAA… relay@dev`. Двойные кавычки сохранены.

- [ ] **Step 3.5: Verify ключ авторизуется и port-forward на разрешённый порт работает**

Реальный smoke-test (запускаем `-N -L` фоном, curl через форвард, гасим). exec-тест бесполезен потому что `command="false"` всё переопределяет — поэтому проверяем именно ту функциональность, ради которой ключ существует:
```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new \
    -o ExitOnForwardFailure=yes \
    -i /home/new/.ssh/id_relay_dev_to_prod \
    -L 22299:127.0.0.1:30093 -N -f \
    root@185.221.160.175
sleep 2
curl -sS -m 3 http://127.0.0.1:22299/tar1090/data/aircraft.json -o /dev/null -w "HTTP %{http_code}\n"
pkill -f 'ssh.*-L 22299:127.0.0.1:30093.*id_relay_dev_to_prod'
```
Expected: `HTTP 200`.

Если получаем `Permission denied` — проблема с самим ключом (Step 3.3). Если `HTTP 000` или ssh не запустился — `permitopen` не сработал (опечатка в строке authorized_keys).

(Коммит не нужен — это правка на удалённом хосте, не в репо.)

---

## Task 4: Создать unit-файл `adsb18-relay.service` на dev (но не запускать)

**Files:**
- Create: `/etc/systemd/system/adsb18-relay.service`

- [ ] **Step 4.1: Verify юнита ещё нет**

Run on dev:
```bash
sudo systemctl status adsb18-relay 2>&1 | head -3
```
Expected: `Unit adsb18-relay.service could not be found.`

- [ ] **Step 4.2: Verify autossh установлен**

```bash
which autossh && autossh -V 2>&1 | head -1
```
Expected: путь типа `/usr/bin/autossh` и версия. Если `not found` — `sudo apt install -y autossh`.

- [ ] **Step 4.3: Записать unit-файл**

Run on dev:
```bash
sudo tee /etc/systemd/system/adsb18-relay.service > /dev/null <<'UNIT'
[Unit]
Description=adsb18 relay (dev → prod): forward Pi tunnels via prod sshd
Documentation=https://github.com/gaveron18/adsb18/blob/main/TROUBLESHOOTING.md
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
UNIT
sudo systemctl daemon-reload
```
Expected: команды отрабатывают без вывода.

- [ ] **Step 4.4: Verify unit загрузился, но не запущен**

```bash
sudo systemctl status adsb18-relay --no-pager 2>&1 | head -5
```
Expected: `Loaded: loaded (/etc/systemd/system/adsb18-relay.service; disabled; ...)`, `Active: inactive (dead)`.

(Коммит не нужен — unit живёт на хосте, не в репо. Это конвенция проекта, см. spec раздел 4.)

---

## Task 5: Запустить relay и проверить туннель

**Files:** только проверки.

- [ ] **Step 5.1: Verify dev-порты 30092 и 52222 свободны**

```bash
ss -ltn '( sport = :30092 or sport = :52222 )'
```
Expected: пусто (только заголовок таблицы). Если что-то слушает — diagnose, что мешает (см. spec раздел 6.5).

- [ ] **Step 5.2: Запустить relay**

```bash
sudo systemctl start adsb18-relay
sleep 5
sudo systemctl status adsb18-relay --no-pager 2>&1 | head -15
```
Expected: `Active: active (running)`, основной процесс `autossh`, дочерний `ssh`.

- [ ] **Step 5.3: Verify listener'ы открыты**

```bash
ss -ltn '( sport = :30092 or sport = :52222 )'
```
Expected: оба порта в `LISTEN` на `127.0.0.1`.

- [ ] **Step 5.4: Verify HTTP-туннель работает**

```bash
curl -sS -m 5 http://127.0.0.1:30092/tar1090/data/aircraft.json | head -c 300
echo
```
Expected: JSON начинается с `{ "now" : ...`, видно поле `aircraft`. Если получаем `Connection refused` — relay не успел подняться или Pi не на связи с prod.

- [ ] **Step 5.5: Verify SSH-туннель управления Pi работает**

```bash
ssh -p 52222 -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
    ads-b@127.0.0.1 "uname -n && uptime"
```
Expected: первая строка `ads-b`, вторая — `uptime` Pi.

- [ ] **Step 5.6: Enable relay для автозагрузки**

```bash
sudo systemctl enable adsb18-relay
```
Expected: `Created symlink ... adsb18-relay.service ... multi-user.target.wants ...`.

- [ ] **Step 5.7: Verify журнал чистый (без подозрительных ошибок)**

```bash
sudo journalctl -u adsb18-relay -n 30 --no-pager
```
Expected: видны строки `Successfully forked into background` и `ssh child pid is ...`. **Не должно быть** `Connection timed out`, `Permission denied`, `bind: Address already in use`.

(Коммит не нужен.)

---

## Task 6: Подключить poller к relay (After= и Wants=)

**Files:**
- Modify: `/etc/systemd/system/adsb18-poller.service` — добавить две правки

- [ ] **Step 6.1: Сделать backup poller.service**

```bash
sudo cp /etc/systemd/system/adsb18-poller.service /etc/systemd/system/adsb18-poller.service.bak.$(date +%Y%m%d-%H%M)
ls -la /etc/systemd/system/adsb18-poller.service*
```
Expected: видны исходный + backup.

- [ ] **Step 6.2: Verify исходное содержимое (для diff)**

```bash
sudo grep -E '^(After|Requires|Wants)=' /etc/systemd/system/adsb18-poller.service
```
Expected:
```
After=network.target postgresql.service
Requires=postgresql.service
```

- [ ] **Step 6.3: Применить правки**

```bash
sudo sed -i \
  -e 's|^After=network.target postgresql.service$|After=network.target postgresql.service adsb18-relay.service|' \
  -e '/^Requires=postgresql.service$/a Wants=adsb18-relay.service' \
  /etc/systemd/system/adsb18-poller.service
```

- [ ] **Step 6.4: Verify правки попали**

```bash
sudo grep -E '^(After|Requires|Wants)=' /etc/systemd/system/adsb18-poller.service
```
Expected:
```
After=network.target postgresql.service adsb18-relay.service
Requires=postgresql.service
Wants=adsb18-relay.service
```

- [ ] **Step 6.5: Daemon-reload и restart poller**

```bash
sudo systemctl daemon-reload
sudo systemctl restart adsb18-poller
sleep 5
sudo systemctl is-active adsb18-poller
```
Expected: `active`.

- [ ] **Step 6.6: Verify poller перестал писать `Connection refused`**

```bash
sudo journalctl -u adsb18-poller -n 30 --no-pager | tail -15
```
Expected: видны строки `writer_loop tick: batch=N` где `N > 0` через 5–10 секунд после старта. **Не должно быть** свежих `poll error: Connection refused`.

- [ ] **Step 6.7: Verify в БД появляются свежие позиции**

```bash
sudo -u postgres psql -d adsb18 -c "SELECT max(ts) AS last_pos, now()-max(ts) AS lag FROM positions;"
```
Expected: `lag` ≤ 10 сек после 1 минуты работы poller'а.

(Коммит не нужен.)

---

## Task 7: Cleanup на Pi — отключить мёртвый `adsb-tunnel.service`

**Files:**
- Modify (на Pi): `adsb-tunnel.service` — `systemctl stop && disable` (файл остаётся)

⚠ Это единственная задача, требующая read-write действия на Pi (`feedback_adsb18_pi_readonly` обычно запрещает). В рамках этой задачи действие явно разрешено Андреем как часть spec'а.

- [ ] **Step 7.1: Verify статус adsb-tunnel.service на Pi (до)**

```bash
ssh -i /home/new/.ssh/id_ed25519 -J root@185.221.160.175 -p 52223 ads-b@127.0.0.1 "
  systemctl is-active adsb-tunnel
  systemctl is-enabled adsb-tunnel
"
```
Expected: `active` + `enabled`.

- [ ] **Step 7.2: Stop и disable**

```bash
ssh -i /home/new/.ssh/id_ed25519 -J root@185.221.160.175 -p 52223 ads-b@127.0.0.1 "
  sudo systemctl stop adsb-tunnel
  sudo systemctl disable adsb-tunnel
"
```
Expected: видна строка типа `Removed "/etc/systemd/system/multi-user.target.wants/adsb-tunnel.service".` Файл `/etc/systemd/system/adsb-tunnel.service` НЕ удаляется.

- [ ] **Step 7.3: Verify статус (после)**

```bash
ssh -i /home/new/.ssh/id_ed25519 -J root@185.221.160.175 -p 52223 ads-b@127.0.0.1 "
  systemctl is-active adsb-tunnel
  systemctl is-enabled adsb-tunnel
  systemctl is-active adsb-tunnel-prod readsb
"
```
Expected:
- `adsb-tunnel`: `inactive` + `disabled`
- `adsb-tunnel-prod`: `active` (НЕ задели)
- `readsb`: `active` (НЕ задели)

(Коммит не нужен — изменения только на Pi.)

---

## Task 8: Документация в репо (TROUBLESHOOTING.md + CLAUDE.md)

**Files:**
- Modify: `/home/new/adsb18/TROUBLESHOOTING.md` — добавить раздел про relay
- Modify: `/home/new/adsb18/CLAUDE.md` — убрать ложное «с dev на prod ssh нет», описать карту ключей и доступов

- [ ] **Step 8.1: Verify рабочего каталога и текущего HEAD**

Run on dev:
```bash
cd /home/new/adsb18
git status --short
git log --oneline -3
```
Expected: чистый каталог (или только посторонние untracked, не наши); HEAD = коммит `fbe0b69` (правки spec).

- [ ] **Step 8.2: Добавить раздел в TROUBLESHOOTING.md**

Открыть `/home/new/adsb18/TROUBLESHOOTING.md`, в конец добавить:

```markdown

## Сессия 2026-05-05

### Связанность Pi → dev: SSH-handshake режется мобильным DPI Tele2

**Симптомы:**
- `adsb-tunnel.service` на Pi (autossh к `new@173.249.2.184`) пишет `Connection to 173.249.2.184 port 22 timed out` каждые ~90 сек, 800+ рестартов в сутки.
- На dev `:30092` не слушает; `adsb18-poller` пишет `Connection refused` на каждый poll; БД dev замерзает.
- Параллельно `adsb-tunnel-prod.service` (то же autossh, тот же ключ, то же ПО, но к prod) — работает идеально, держится сутками.

**Корневая причина:**
Pi выходит через мобильный оператор T2 Russia (AS48190, EKB-сегмент). DPI оператора:
- ICMP большие пакеты (DF, 1472) пропускает
- TCP `:22` SYN/ACK тоже проходят
- SSH KEX в ручном `ssh -vvv` иногда успевает за <8 сек
- но autossh с retry 90 сек систематически попадает в окно блокировки → таймаут
- UDP `:51820` (WireGuard) — 0 байт обратно (полная блокировка UDP-VPN)

prod расположен в РФ-ЦОДе (FirstByte) — оператор не вмешивается; `adsb-tunnel-prod` работает.

**Решение:** `adsb18-relay.service` на dev — forward-tunnel `dev → prod` (РФ-ЦОД ↔ DE-ЦОД, не проходит через моб-DPI), который на dev открывает:
- `127.0.0.1:30092` → `prod:127.0.0.1:30093` (Pi HTTP `aircraft.json`)
- `127.0.0.1:52222` → `prod:127.0.0.1:52223` (Pi SSH управление)

**Артефакты:**
- Unit: `/etc/systemd/system/adsb18-relay.service` (на dev, не в репо — конвенция проекта)
- Ключ: `/home/new/.ssh/id_relay_dev_to_prod` (на dev)
- `authorized_keys` на prod содержит строку с `restrict,permitopen="127.0.0.1:30093",permitopen="127.0.0.1:52223" ... relay@dev` (узкие полномочия — только эти два TCP-форварда, никакого шелла)
- На Pi `adsb-tunnel.service` отключён (`stop && disable`), файл остаётся
- Полный design: `docs/superpowers/specs/2026-05-05-prod-relay-design.md`
- Implementation plan: `docs/superpowers/plans/2026-05-05-prod-relay.md`

**Управление:**
```bash
sudo systemctl status adsb18-relay
sudo journalctl -u adsb18-relay -f
ss -ltn '( sport = :30092 or sport = :52222 )'
curl -sS http://127.0.0.1:30092/tar1090/data/aircraft.json | head -c 200
ssh -p 52222 ads-b@127.0.0.1 'uname -n'
```

**Связь с CLAUDE.md:** строка про «с dev на prod ssh нет» удалена, актуальная карта доступов вписана в раздел «Окружения / Доступы».
```

- [ ] **Step 8.3: Verify TROUBLESHOOTING.md синтаксически OK**

```bash
tail -50 TROUBLESHOOTING.md
```
Expected: видно добавленный раздел, заголовки в порядке (`## Сессия 2026-05-05`, `### …`, и т.д.).

- [ ] **Step 8.4: Обновить CLAUDE.md — убрать ложное утверждение и добавить карту доступов**

В `/home/new/adsb18/CLAUDE.md`:
- найти строку:
  ```
  - **Деплой на prod:** `git pull` вручную на prod (после `git push` с dev). SSH-доступ к prod — только с рабочей машины Андрея, с dev на prod ssh нет.
  ```
- заменить на:
  ```
  - **Деплой на prod:** `git pull` вручную на prod, обычно с рабочей машины Андрея. SSH dev → prod **есть** (ключ `/home/new/.ssh/id_ed25519` → `root@185.221.160.175`) — используется для read-only администрирования и для `adsb18-relay.service`.

  ### Карта SSH-ключей и доступов

  | С / На | dev | prod | Pi |
  |--------|-----|------|----|
  | dev | — | `id_ed25519` (root, общий) + `id_relay_dev_to_prod` (relay-only, `restrict+permitopen`) | через ProxyJump prod → `:52223` ads-b |
  | prod | (только если ключ установлен на dev) | — | `id_adsb_vps` (root → ads-b@127.0.0.1:52223) |
  | Pi | autossh-tunnel `new@dev` (отключён, см. TROUBLESHOOTING) + autossh-tunnel `root@prod` (active) | autossh-tunnel `root@prod` (active) | — |
  ```

- [ ] **Step 8.5: Verify CLAUDE.md изменения**

```bash
grep -n 'с dev на prod ssh нет' CLAUDE.md
grep -n 'Карта SSH-ключей' CLAUDE.md
```
Expected: первая команда — пусто. Вторая — одна строка с заголовком.

- [ ] **Step 8.6: Закоммитить изменения**

```bash
cd /home/new/adsb18
git add TROUBLESHOOTING.md CLAUDE.md
git diff --cached --stat
git commit -m "$(cat <<'EOF'
docs: relay через prod — TROUBLESHOOTING + актуальная карта SSH-доступов

После реализации adsb18-relay.service (см. spec и plan в docs/superpowers/):
- TROUBLESHOOTING: добавлен разбор инцидента + решение через relay
- CLAUDE.md: исправлено устаревшее «с dev на prod ssh нет», добавлена
  таблица SSH-ключей и доступов между dev/prod/Pi
EOF
)"
```
Expected: коммит создан, выводится `1 file changed` или `2 files changed`.

- [ ] **Step 8.7: Push в github**

```bash
git push origin main
```
Expected: `To https://github.com/gaveron18/adsb18.git`, успешный push.

⚠ После push **обязательно** на prod выполнить `cd /opt/adsb18 && git pull` (с рабочей машины Андрея), чтобы обновлённые TROUBLESHOOTING/CLAUDE были и там.

---

## Task 9: Acceptance — полный smoke test (8 критериев)

**Files:** только проверки.

- [ ] **Step 9.1: Сервис активен**

```bash
sudo systemctl is-active adsb18-relay
```
Expected: `active`.

- [ ] **Step 9.2: Listener'ы открыты**

```bash
ss -ltn '( sport = :30092 or sport = :52222 )'
```
Expected: оба порта в `LISTEN` на `127.0.0.1`.

- [ ] **Step 9.3: HTTP-туннель отдаёт свежий aircraft.json**

```bash
curl -sS -m 5 http://127.0.0.1:30092/tar1090/data/aircraft.json \
  | python3 -c "import json,sys,time; d=json.load(sys.stdin); print(f'now={d[\"now\"]:.0f} age={time.time()-d[\"now\"]:.1f}s aircraft={len(d[\"aircraft\"])}')"
```
Expected: `age` < 5 сек, `aircraft` > 0 (если самолёты есть в зоне Pi).

- [ ] **Step 9.4: SSH-туннель к Pi работает**

```bash
ssh -p 52222 -o StrictHostKeyChecking=no ads-b@127.0.0.1 "uname -n"
```
Expected: `ads-b`.

- [ ] **Step 9.5: Poller без `Connection refused`**

```bash
COUNT=$(sudo journalctl -u adsb18-poller --since '2 min ago' --no-pager | grep -c 'Connection refused')
echo "Connection refused за последние 2 минуты: $COUNT"
```
Expected: `0`. Окно «2 min ago» отсекает старые ошибки до запуска relay.

- [ ] **Step 9.6: Лаг dev БД**

```bash
sudo -u postgres psql -d adsb18 -c "SELECT now()-max(ts) AS lag FROM positions;"
```
Expected: `lag` < 10 сек.

- [ ] **Step 9.7: dev и prod БД совпадают по активным бортам**

```bash
diff \
  <(ssh -i /home/new/.ssh/id_ed25519 root@185.221.160.175 "sudo -u postgres psql -d adsb18 -tAc \"SELECT icao FROM aircraft WHERE last_seen > now()-INTERVAL '30 sec' ORDER BY 1\"") \
  <(sudo -u postgres psql -d adsb18 -tAc "SELECT icao FROM aircraft WHERE last_seen > now()-INTERVAL '30 sec' ORDER BY 1")
```
Expected: пусто (или ≤2 строки расхождения из-за timing).

- [ ] **Step 9.8: Веб-интерфейс**

```bash
curl -sS http://127.0.0.1:8098/data/aircraft.json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'aircraft={len(d[\"aircraft\"])}')"
```
Expected: `aircraft` > 0.

(Если все 8 критериев проходят — переходим к Task 10. Если что-то не проходит — diagnose по spec'у раздел 6.)

---

## Task 10: Reliability & Security drills

Цель — убедиться, что заявленные в spec'е failure-modes (раздел 6) и security-границы (раздел 4.2) действительно работают. Это **активное** тестирование с искусственными сбоями.

⚠ Этот Task роняет relay на короткие промежутки. Если кто-то прямо сейчас активно использует dev для отладки — согласовать момент.

### Drill 1 — stop/start relay (auto-recovery)

- [ ] **Step 10.1.1: Засечь baseline лага БД**

```bash
sudo -u postgres psql -d adsb18 -c "SELECT now()-max(ts) AS lag FROM positions;"
```
Expected: `lag` < 10 сек (нормальное состояние).

- [ ] **Step 10.1.2: Остановить relay**

```bash
sudo systemctl stop adsb18-relay
date +%H:%M:%S
sleep 30
```

- [ ] **Step 10.1.3: Verify лаг растёт**

```bash
sudo -u postgres psql -d adsb18 -c "SELECT now()-max(ts) AS lag FROM positions;"
sudo journalctl -u adsb18-poller --since '40 sec ago' --no-pager | grep -c 'Connection refused'
```
Expected: `lag` ≥ 30 сек (растёт), `Connection refused` count ≥ 1 (поллер видит что туннель упал).

- [ ] **Step 10.1.4: Запустить relay обратно**

```bash
sudo systemctl start adsb18-relay
date +%H:%M:%S
sleep 15
```

- [ ] **Step 10.1.5: Verify autossh поднял туннель и лаг восстановился**

```bash
ss -ltn '( sport = :30092 or sport = :52222 )'
sudo -u postgres psql -d adsb18 -c "SELECT now()-max(ts) AS lag FROM positions;"
```
Expected: оба порта в LISTEN, `lag` снова < 10 сек.

### Drill 2 — restart prod sshd (auto-reconnect)

- [ ] **Step 10.2.1: Перезагрузить sshd на prod**

```bash
ssh -i /home/new/.ssh/id_ed25519 root@185.221.160.175 "systemctl restart ssh"
date +%H:%M:%S
```
Expected: команда отрабатывает (наша же ssh-сессия может разорваться — это норма).

- [ ] **Step 10.2.2: Verify autossh заметил разрыв**

```bash
sleep 10
sudo journalctl -u adsb18-relay -n 20 --no-pager | tail -10
```
Expected: видим строки типа `client_loop: send disconnect: Connection reset` или `Connection to 185.221.160.175 closed by remote host`, затем `restarting ssh` (это autossh работает).

- [ ] **Step 10.2.3: Verify туннель восстановился за ≤ 60 секунд**

```bash
sleep 50
ss -ltn '( sport = :30092 or sport = :52222 )'
curl -sS -m 5 http://127.0.0.1:30092/tar1090/data/aircraft.json | head -c 100
```
Expected: оба порта снова в LISTEN, `aircraft.json` отдаётся.

### Drill 3 — security: попытка форварда на НЕ-разрешённый порт

- [ ] **Step 10.3.1: Попытаться открыть `-L` на `:5432` (postgres)**

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 -o ExitOnForwardFailure=yes \
    -i /home/new/.ssh/id_relay_dev_to_prod \
    -L 12345:127.0.0.1:5432 -N -f \
    root@185.221.160.175 2>&1 | head
echo "exit code: $?"
```
Expected: ssh падает с `administratively prohibited: open failed` или `forwarding request failed`, **exit code != 0**. Если процесс таки запустился — pkill его и считать drill failed: `permitopen` не работает.

- [ ] **Step 10.3.2: Verify никакого процесса не осталось**

```bash
pgrep -f 'ssh.*-L 12345' || echo "no leftover ssh process"
```
Expected: `no leftover ssh process`.

### Drill 4 — security: попытка exec и pty (должны быть заблокированы)

- [ ] **Step 10.4.1: Попытаться выполнить exec-команду через ключ**

```bash
OUTPUT=$(ssh -o BatchMode=yes -o ConnectTimeout=10 \
    -i /home/new/.ssh/id_relay_dev_to_prod \
    root@185.221.160.175 'cat /etc/passwd' 2>&1)
EXIT=$?
echo "exit code: $EXIT"
echo "stdout/stderr: '$OUTPUT'"
```
Expected: `exit code: 1` (это `false`, не `cat`), `stdout/stderr` пустой или содержит только banner-сообщения, **никаких строк из /etc/passwd**. Если в выводе видишь строки `root:x:0:0:...` — `command="false"` не сработал, security drill failed.

- [ ] **Step 10.4.2: Попытаться открыть интерактивный pty**

```bash
ssh -tt -o BatchMode=yes -o ConnectTimeout=10 \
    -i /home/new/.ssh/id_relay_dev_to_prod \
    root@185.221.160.175 2>&1 | head -3
echo "exit code: $?"
```
Expected: вывод содержит `PTY allocation request failed on channel 0` (от `restrict` → `no-pty`), exit code != 0.

- [ ] **Step 10.4.3: Попытаться использовать ssh agent forwarding**

```bash
ssh -A -o BatchMode=yes -o ConnectTimeout=10 \
    -i /home/new/.ssh/id_relay_dev_to_prod \
    root@185.221.160.175 -N 2>&1 &
SSH_PID=$!
sleep 3
sudo journalctl -u ssh --since '5 sec ago' --no-pager 2>/dev/null | head
kill $SSH_PID 2>/dev/null
```
Expected: либо в журнале появляется упоминание `agent forwarding disabled`, либо ssh клиент молча игнорирует `-A` (так как `restrict` блокирует). Главное — **никакого риска** что атакующий получит наш SSH-агент.

### Drill 5 — compromise: удалить строку из authorized_keys и восстановить

- [ ] **Step 10.5.1: Удалить строку relay@dev на prod**

```bash
ssh -i /home/new/.ssh/id_ed25519 root@185.221.160.175 "
  cp /root/.ssh/authorized_keys /root/.ssh/authorized_keys.drill5.bak
  sed -i '/relay@dev$/d' /root/.ssh/authorized_keys
  grep -c 'relay@dev$' /root/.ssh/authorized_keys || true
"
```
Expected: вторая команда — `0` (строки больше нет).

- [ ] **Step 10.5.2: Дождаться, пока autossh потеряет соединение**

```bash
sleep 60   # дольше чем ServerAliveInterval=30 × Count=3
sudo journalctl -u adsb18-relay --since '70 sec ago' --no-pager | grep -iE 'permission denied|publickey'
```
Expected: видим `Permission denied (publickey)` от ssh-клиента — значит autossh пытается, не пускают, restart-loop.

- [ ] **Step 10.5.3: Восстановить строку из backup**

```bash
ssh -i /home/new/.ssh/id_ed25519 root@185.221.160.175 "
  mv /root/.ssh/authorized_keys.drill5.bak /root/.ssh/authorized_keys
  grep -c 'relay@dev$' /root/.ssh/authorized_keys
"
```
Expected: вторая команда — `1` (строка вернулась).

- [ ] **Step 10.5.4: Verify relay сам восстановился**

```bash
sleep 20
sudo systemctl is-active adsb18-relay
ss -ltn '( sport = :30092 or sport = :52222 )'
sudo -u postgres psql -d adsb18 -c "SELECT now()-max(ts) AS lag FROM positions;"
```
Expected: `active`, оба порта в LISTEN, `lag` снова < 30 сек.

(Drills passed → security и reliability подтверждены. Коммит не нужен, всё восстановлено.)

---

## Task 11: Регрессия — что НЕ сломалось

**Files:** только проверки.

- [ ] **Step 11.1: dev сервисы**

```bash
for s in adsb18-api adsb18-poller adsb18-nginx postgresql; do
  printf "%-25s %s\n" "$s" "$(sudo systemctl is-active $s)"
done
```
Expected: все `active`.

- [ ] **Step 11.2: prod сервисы**

```bash
ssh -i /home/new/.ssh/id_ed25519 root@185.221.160.175 "
  for s in adsb18-api adsb18-poller adsb18-bot nginx postgresql parsersavino proba1 roskadastr; do
    printf '%-20s %s\n' \"\$s\" \"\$(systemctl is-active \$s)\"
  done
  echo --- adsb18.ru ---
  curl -sS -o /dev/null -w 'HTTP %{http_code}\n' https://adsb18.ru/
  echo --- prod БД лаг ---
  sudo -u postgres psql -d adsb18 -c \"SELECT now()-max(ts) FROM positions;\"
"
```
Expected: все сервисы `active`, `HTTP 200`, лаг < 10 сек.

- [ ] **Step 11.3: Pi сервисы**

```bash
ssh -i /home/new/.ssh/id_ed25519 -J root@185.221.160.175 -p 52223 ads-b@127.0.0.1 "
  systemctl is-active adsb-tunnel-prod readsb
  systemctl is-active adsb-tunnel  # должен быть inactive после Task 7
"
```
Expected: `adsb-tunnel-prod` и `readsb` — `active`. `adsb-tunnel` — `inactive` (это норма по результатам Task 7).

(Коммит не нужен.)

---

## Task 12: Финальный acceptance — цикл разработки (опционально, по согласованию с Андреем)

⚠ Этот тест **видимо** меняет `https://adsb18.ru/` для всех пользователей. Выполнять только когда отображение `[DEV]` на живом сайте допустимо.

**Files:**
- Modify: `frontend/index.html` — `<title>tar1090</title>` → `<title>tar1090 [DEV]</title>` (и обратно)

- [ ] **Step 12.1: Внести правку**

```bash
cd /home/new/adsb18
sed -i 's|<title>tar1090</title>|<title>tar1090 [DEV]</title>|' frontend/index.html
grep '<title>' frontend/index.html
```
Expected: `<title>tar1090 [DEV]</title>`.

- [ ] **Step 12.2: Сжать, если используется gzip_static**

```bash
gzip -k -f frontend/index.html
ls -la frontend/index.html*
```

- [ ] **Step 12.3: Verify видно на dev**

```bash
curl -sS http://127.0.0.1:8098/ | grep '<title>'
```
Expected: `<title>tar1090 [DEV]</title>`.

- [ ] **Step 11.4: Push**

```bash
git add frontend/index.html frontend/index.html.gz
git commit -m "test: [DEV] заголовок для проверки цикла deploy (откат следующим коммитом)"
git push origin main
```

- [ ] **Step 11.5: Деплой на prod (с рабочей машины Андрея)**

```bash
ssh root@185.221.160.175 'cd /opt/adsb18 && git pull'
```

- [ ] **Step 11.6: Verify видно на prod**

```bash
curl -sS https://adsb18.ru/ | grep '<title>'
```
Expected: `<title>tar1090 [DEV]</title>`.

- [ ] **Step 11.7: Откатить правку**

⚠ По CLAUDE.md правило 11 — рефакторинг и фикс отдельными коммитами. Не используем `--amend`. Делаем revert отдельным коммитом, потом отдельный коммит для регенерации gzip.

```bash
cd /home/new/adsb18
git revert --no-edit HEAD
gzip -k -f frontend/index.html
git add frontend/index.html.gz
git commit -m "chore: regenerate index.html.gz after revert"
git push origin main
ssh root@185.221.160.175 'cd /opt/adsb18 && git pull'
```

- [ ] **Step 11.8: Verify откат сработал**

```bash
curl -sS https://adsb18.ru/ | grep '<title>'
curl -sS http://127.0.0.1:8098/ | grep '<title>'
```
Expected: оба показывают `<title>tar1090</title>` (без `[DEV]`).

(Если этот цикл прошёл — задача «восстановить dev для разработки» полностью закрыта.)

---

## Acceptance Criteria

Реализация считается успешной когда:

- ✅ Task 5 шаги 5.4 и 5.5 проходят (HTTP и SSH туннели через relay работают)
- ✅ Task 6 шаг 6.7 проходит (dev БД пишется, лаг < 10 сек)
- ✅ Task 8 — TROUBLESHOOTING + CLAUDE.md обновлены и запушены
- ✅ Task 9 — все 8 smoke-критериев зелёные
- ✅ Task 10 — все 5 drills проходят: stop/start, sshd restart, отказ форварда на запрещённый порт, отказ exec и pty, compromise/restore ключа
- ✅ Task 11 — все регрессионные проверки зелёные (на prod и Pi ничего не сломано)
- ✅ (опционально) Task 12 — цикл разработки прогнан end-to-end

## Rollback (если что-то пошло не так)

См. spec, раздел 8: «Rollback procedure». Полный откат всех 7 артефактов за ≈ 5 минут.

---

**Конец плана.**
