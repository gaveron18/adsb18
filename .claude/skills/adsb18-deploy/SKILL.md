---
name: adsb18-deploy
description: Применить изменения adsb18 — gzip, session лог, git push, рестарт сервисов
disable-model-invocation: true
---

Применить изменения в adsb18. Аргументы: 

## Шаги

1. **Проверь что изменилось** — git diff HEAD и git log --oneline -5

2. **Если изменён frontend JS** — обязательно сделай gzip:
   gzip -k -f /home/new/adsb18/frontend/script.js
   (nginx использует gzip_static on — без .gz браузер получит старый кеш)

3. **Обнови session лог** — добавь запись в docs/session_ДАТА.md что сделано и зачем.
   Session лог обновляется В ТОМ ЖЕ коммите что и код.

4. **Git commit + push** на GitHub

5. **Рестарт нужных сервисов:**
   - Если менялся server/api/     → sudo systemctl restart adsb18-api
   - Если менялся server/poller.py → sudo systemctl restart adsb18-poller

6. **Проверь результат:**
   sudo systemctl status adsb18-api adsb18-poller
   curl -s http://localhost:9001/data/aircraft.json | python3 -m json.tool | head -5

7. **Сообщи итог** — что изменено, статус сервисов, ошибки если были.
