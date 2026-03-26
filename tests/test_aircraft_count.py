"""
Тест сравнения количества самолётов на Pi и на сервере.

Запуск (на VPS):
    cd /home/new/adsb18
    pip install pytest requests
    pytest tests/test_aircraft_count.py -v

Требования:
    - Запускать на VPS (доступ к порту 30092 и 9001)
    - SSH-туннель от Pi должен быть активен (30092 = Pi lighttpd)
    - adsb18-api должен быть запущен (порт 9001)
"""

import json
import urllib.request
import urllib.error
import time
import pytest

pytestmark = pytest.mark.live

PI_URL     = 'http://127.0.0.1:30092/tar1090/data/aircraft.json'
SERVER_URL = 'http://127.0.0.1:9001/data/aircraft.json'

# Допустимое расхождение в процентах.
# Pi и сервер никогда не будут совпадать идеально:
#   - сервер отфильтровывает призраков (_valid_position)
#   - есть задержка в 1-2 секунды при поллинге
#   - Pi показывает борта без позиции тоже
TOLERANCE_PCT = 30


def _fetch(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        pytest.fail(f'Не удалось подключиться к {url}: {e}\n'
                    f'Проверь: systemctl status adsb18-api / активен ли SSH-туннель')


# ── Тесты ─────────────────────────────────────────────────────────────────────

def test_pi_is_reachable():
    """SSH-туннель активен и Pi отдаёт aircraft.json."""
    data = _fetch(PI_URL)
    assert 'aircraft' in data, f'Нет ключа aircraft в ответе Pi: {list(data.keys())}'
    assert 'now' in data, f'Нет ключа now в ответе Pi'


def test_server_api_is_reachable():
    """adsb18-api запущен и отвечает."""
    data = _fetch(SERVER_URL)
    assert 'aircraft' in data, f'Нет ключа aircraft в ответе сервера: {list(data.keys())}'
    assert 'now' in data


def test_aircraft_count_matches():
    """
    Количество бортов на Pi и на сервере совпадает с допуском TOLERANCE_PCT%.

    Pi считает ВСЕ борта из aircraft.json (включая без позиции).
    Сервер возвращает борта, виденные за последние 60 секунд.

    Если тест падает — возможные причины:
      1. Туннель лаганул и поллер давно не получал данные
      2. Сервер фильтрует слишком много призраков (_valid_position)
      3. Борта истекают из aircraft-таблицы (last_seen > 60 сек)
    """
    pi_data     = _fetch(PI_URL)
    server_data = _fetch(SERVER_URL)

    pi_total    = len(pi_data['aircraft'])
    server_total = len(server_data['aircraft'])

    # Детальный отчёт для понимания расхождения
    pi_with_pos = sum(1 for a in pi_data['aircraft'] if 'lat' in a and 'lon' in a)
    srv_with_pos = sum(1 for a in server_data['aircraft'] if 'lat' in a and 'lon' in a)

    print(f'\n--- Сравнение бортов ---')
    print(f'Pi:     всего={pi_total}, с позицией={pi_with_pos}')
    print(f'Сервер: всего={server_total}, с позицией={srv_with_pos}')

    if pi_total == 0:
        pytest.skip('Pi не видит ни одного борта — нет смысла сравнивать')

    diff_pct = abs(pi_total - server_total) / pi_total * 100
    print(f'Расхождение: {pi_total - server_total:+d} бортов ({diff_pct:.1f}%)')
    print(f'Допуск: {TOLERANCE_PCT}%')

    assert diff_pct <= TOLERANCE_PCT, (
        f'Расхождение {diff_pct:.1f}% превышает допуск {TOLERANCE_PCT}%.\n'
        f'Pi={pi_total}, Сервер={server_total}.\n'
        f'Проверь: journalctl -u adsb18-ingest -n 50'
    )


def test_positions_count_matches():
    """
    Количество бортов С ПОЗИЦИЕЙ на Pi и на сервере совпадает с допуском.
    Это точнее чем test_aircraft_count_matches, т.к. оба источника
    в этом случае сравнивают одно и то же.
    """
    pi_data     = _fetch(PI_URL)
    server_data = _fetch(SERVER_URL)

    pi_pos  = sum(1 for a in pi_data['aircraft']     if 'lat' in a and 'lon' in a)
    srv_pos = sum(1 for a in server_data['aircraft'] if 'lat' in a and 'lon' in a)

    print(f'\n--- Борта с позицией ---')
    print(f'Pi:     {pi_pos}')
    print(f'Сервер: {srv_pos}')

    if pi_pos == 0:
        pytest.skip('Pi не видит бортов с позицией')

    diff_pct = abs(pi_pos - srv_pos) / pi_pos * 100
    print(f'Расхождение: {pi_pos - srv_pos:+d} ({diff_pct:.1f}%)')

    assert diff_pct <= TOLERANCE_PCT, (
        f'Борта с позицией: Pi={pi_pos}, Сервер={srv_pos}, '
        f'расхождение {diff_pct:.1f}% > {TOLERANCE_PCT}%'
    )


def test_no_stale_data():
    """
    Данные на сервере свежие — не старше 10 секунд.
    Если устарели — поллер завис или туннель упал.
    """
    server_data = _fetch(SERVER_URL)
    server_now  = float(server_data.get('now', 0))
    age = time.time() - server_now

    print(f'\nВозраст данных на сервере: {age:.1f} сек')
    assert age < 10, (
        f'Данные сервера устарели на {age:.1f} сек (должно быть < 10 сек).\n'
        f'Проверь: journalctl -u adsb18-ingest -n 20'
    )


def test_pi_data_is_fresh():
    """
    Pi отдаёт свежие данные — не старше 5 секунд.
    Если устарели — readsb завис или aircraft.json не обновляется.
    """
    pi_data = _fetch(PI_URL)
    pi_now  = float(pi_data.get('now', 0))
    age = time.time() - pi_now

    print(f'\nВозраст данных на Pi: {age:.1f} сек')
    assert age < 5, (
        f'Данные Pi устарели на {age:.1f} сек (должно быть < 5 сек).\n'
        f'Зайди на Pi: ssh -p 52222 ads-b@127.0.0.1\n'
        f'Проверь: sudo systemctl status readsb'
    )
