"""
Unit-тесты для sbs_parser.py — парсинг SBS-строк от dump1090.
Не требуют никакой инфраструктуры, запускаются везде (в т.ч. GitHub Actions).
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server', 'ingest'))

from sbs_parser import parse


def _line(msg_type=3, icao='3C6444', lat='55.750', lon='37.620',
          alt='10000', speed='450', track='270',
          date='2026/01/01', time_='12:00:00.000'):
    return (
        f'MSG,{msg_type},1,1,{icao},1,'
        f'{date},{time_},{date},{time_},'
        f',{alt},{speed},{track},{lat},{lon},,,,,,'
    )


# ── Базовый парсинг ───────────────────────────────────────────────────────────

def test_valid_msg3_parses_position():
    msg = parse(_line())
    assert msg is not None
    assert msg.icao == '3C6444'
    assert msg.altitude == 10000
    assert msg.ground_speed == 450
    assert abs(msg.lat - 55.750) < 0.001
    assert abs(msg.lon - 37.620) < 0.001


def test_icao_uppercased():
    msg = parse(_line(icao='3c6444'))
    assert msg.icao == '3C6444'


def test_timestamp_parsed():
    msg = parse(_line(date='2026/03/26', time_='15:30:00.500'))
    assert msg.ts.year == 2026
    assert msg.ts.month == 3
    assert msg.ts.day == 26
    assert msg.ts.hour == 15


# ── MSG type 8 — бесполезный тип, должен возвращать None ─────────────────────
# Это было причиной asyncio starvation: счётчик msg_count не рос,
# sleep(0) не вызывался → event loop не освобождался.

def test_msg8_returns_none():
    msg = parse(_line(msg_type=8))
    assert msg is None


# ── Некорректные строки ───────────────────────────────────────────────────────

def test_empty_line_returns_none():
    assert parse('') is None


def test_non_msg_line_returns_none():
    assert parse('AIR,something,else') is None
    assert parse('STA,something') is None


def test_short_line_returns_none():
    assert parse('MSG,3,1,1,3C6444') is None


def test_invalid_icao_too_short_returns_none():
    msg = parse(_line(icao='ABC'))
    assert msg is None


def test_invalid_icao_too_long_returns_none():
    msg = parse(_line(icao='ABCDEFG'))
    assert msg is None


# ── Пустые поля — не должны падать ───────────────────────────────────────────

def test_empty_lat_lon_gives_none_position():
    msg = parse(_line(lat='', lon=''))
    assert msg is not None
    assert msg.lat is None
    assert msg.lon is None


def test_empty_altitude_gives_none():
    msg = parse(_line(alt=''))
    assert msg is not None
    assert msg.altitude is None


def test_empty_speed_gives_none():
    msg = parse(_line(speed=''))
    assert msg is not None
    assert msg.ground_speed is None
