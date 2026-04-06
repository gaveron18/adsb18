---
name: adsb18-deploy
description: Деплой изменений adsb18 на VPS с проверкой всех шагов
disable-model-invocation: true
---

Задеплой изменения adsb18 на VPS 173.249.2.184. Аргументы: $ARGUMENTS

## Шаги

1. **Проверь что изменилось** — git diff HEAD и git log --oneline -5

2. **Если изменён frontend JS** — обязательно:
   ssh new@173.249.2.184 "gzip -k -f /home/new/adsb18/frontend/script.js"
   (nginx использует gzip_static on — без .gz браузер получит старый кеш)

3. **Обнови session лог** — добавь запись в docs/session_ДАТА.md что сделано и зачем.
   Session лог обновляется В ТОМ ЖЕ коммите что и код.

4. **Git push** — git add, git commit, git push

5. **Деплой на VPS:**
   ssh new@173.249.2.184 "cd /home/new/adsb18 && git pull"

6. **Рестарт нужных сервисов:**
   - Если менялся server/api/    → sudo systemctl restart adsb18-api
   - Если менялся server/ingest/ → adsb18-ingest замаскирован, не трогать
   - Если менялся server/poller.py → sudo systemctl restart adsb18-poller

7. **Проверь результат:**
   ssh new@173.249.2.184 "sudo systemctl status adsb18-api adsb18-poller"
   ssh new@173.249.2.184 "curl -s http://localhost:9001/data/aircraft.json | python3 -m json.tool | head -20"

8. **Сообщи итог** — что задеплоено, статус сервисов, ошибки если были.
