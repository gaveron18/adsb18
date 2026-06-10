# Известные баги archive.html — разбор и решения

Этот файл — база знаний по багам страницы /archive.html.
При повторном появлении симптома — сначала сюда.

---

## Баг 1 — Треки не появляются на карте (галочки ставятся, карта пустая)

**Симптом:** выбираешь рейсы, ставишь галочки, на карте ничего.  
**Причина:** в nginx был конфликт маршрутов:
```
location = /api/history { proxy_pass http://localhost:8094; }  ← WRONG
```
Exact-match перехватывал все запросы `/api/history` и отправлял в сторонний API (Proba1/METAR на порту 8094), который возвращал JSON без полей `lat/lon`. Frontend получал пустые точки и ничего не рисовал.

**Решение:** удалить конфликтующие `location =` блоки из `/etc/nginx/sites-enabled/adsb18`. Треки должны идти на порт 9001 (основной backend).

**Как проверить:** `curl 'http://localhost:9001/api/history?icao=780AE5&from=...&to=...'` — должен вернуть массив точек с `lat`, `lon`.

---

## Баг 2 — Список рейсов пропадает сразу после появления (race condition)

**Симптом:** нажимаешь «Загрузить», список на секунду появляется, потом исчезает или показывает «Загрузка...» навсегда.  
**Причина:** `loadArchive(true)` (автозапуск при загрузке страницы) и `loadArchive(false)` (по клику «Загрузить») работали параллельно. Если стартовый запрос завершался позже пользовательского — он затирал DOM списком из старых данных.

**Решение:** глобальный токен `archiveLoadToken`. Каждый вызов получает свой номер, после каждого `await` проверяет что он актуальный:
```javascript
const token = ++archiveLoadToken;
// ... await fetch ...
if (token !== archiveLoadToken) return;  // ← устаревший вызов молча выходит
```

**Коммит:** 45080cf

---

## Баг 3 — Chrome падал / вкладка перезагружалась при открытии страницы

**Симптом:** открываешь archive.html → список появляется → вкладка крашится → перезагружается → показывает «Выберите период и нажмите Загрузить».  
**Причина:** блок авто-восстановления сессии вызывал `addTrack()` в цикле **без await** (параллельно). Если в прошлый раз было выбрано 50–100+ треков — запускались 50–100 параллельных fetch к `/api/history` и одновременно строились тысячи объектов Leaflet → out of memory → Chrome убивал вкладку.

**Решение:**
1. Цикл стал последовательным (`await addTrack()` — каждый трек ждёт предыдущий).
2. Лимит: не более **10 треков** восстанавливается автоматически при загрузке.
3. Токен-чек внутри цикла: если пользователь нажал «Загрузить» пока шло восстановление — цикл прерывается.

```javascript
for (const el of items) {
    if (token !== archiveLoadToken) break;  // ← прерываем если начался новый loadArchive
    ...
    await addTrack(f, el, color, true);
}
```

**Коммиты:** 3d981a9, b442d9d

---

## Баг 4 — Список рейсов исчезает через секунду после загрузки (autofill)

**Симптом:** загружаешь апрель 14-15, список появляется (105 рейсов), через ~1 секунду всё исчезает. Кнопки и заголовок на месте, список пуст.  
**Причина:** браузерное расширение (или Chrome autofill) **асинхронно** вписывало текст `PICK A DATE` в поле поиска уже после того, как список был построен. Это триггерило `oninput → filterFlights(PICK A DATE)`, которая скрывала все элементы через `el.style.display = 'none'` (ни один рейс не матчил строку PICK A DATE).

DOM при этом оставался нетронутым — стандартный MutationObserver (childList) ничего не видел. Обнаружено через второй observer на атрибут `style`.

**Решение:** первая строка `filterFlights()`:
```javascript
function filterFlights() {
    const si = document.getElementById('searchInput');
    if (si.value.includes(' ')) { si.value = ''; }  // reject autofill
    ...
}
```
ICAO (780AE5) и callsign (SVR624) никогда не содержат пробелов. Любое значение с пробелом — мусор от autofill, сразу очищается.

**Если вернётся:** открой DevTools → Console. Должно быть `[filterFlights] query=...`. Если нет — добавь в начало filterFlights временный `console.warn` и смотри что за значение и откуда.

**Коммит:** d523ac6

---

## Диагностический инструментарий (для будущих багов)

### Поймать кто очищает список рейсов (innerHTML)
Добавить перед `loadArchive(true);`:
```javascript
(function() {
    const list = document.getElementById('flightList');
    const obs = new MutationObserver(function(muts) {
        for (const m of muts) {
            if (m.type === 'childList' && m.removedNodes.length > 0) {
                console.warn('[flightList CLEARED]', list.innerHTML.slice(0, 80));
                console.trace();
            }
        }
    });
    obs.observe(list, { childList: true, subtree: false });
})();
```

### Поймать кто скрывает элементы через CSS
```javascript
obs.observe(list, { childList: true, subtree: true, attributes: true, attributeFilter: ['style'] });
// В callback добавить:
if (m.type === 'attributes' && m.target.style.display === 'none') {
    console.warn('[HIDDEN]', m.target.dataset.icao); console.trace();
}
```

### Логировать все вызовы filterFlights с непустым запросом
В начало `filterFlights()`:
```javascript
if (si.value) { console.warn('[filterFlights]', si.value); console.trace(); }
```
