"""
adsb18 — API server (FastAPI)
Serves aircraft.json for tar1090 frontend + REST history API.

Endpoints:
  GET  /data/aircraft.json     — live aircraft (tar1090 format)
  GET  /api/history?icao=&from=&to=   — track history for one aircraft
  GET  /api/aircraft           — all aircraft seen in last 24h
  GET  /api/feeders            — connected feeders
  GET  /api/monitor            — Pi vs Server comparison status
  WS   /ws                     — real-time aircraft updates
"""
import os
import time
import json
import asyncio
import logging
import asyncpg
from datetime import datetime, date, timezone, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

DB_DSN = os.getenv('DATABASE_URL', 'postgresql://adsb:adsb@postgres:5432/adsb18')

PI_SSH_CMD = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=5',
              '-p', '52222', 'ads-b@127.0.0.1']

MONITOR_INTERVAL = 30   # seconds between checks
MONITOR_SERVER_WINDOW = 120  # server window: aircraft seen in last N seconds
PI_AIRCRAFT_URL = os.getenv('PI_AIRCRAFT_URL', 'http://127.0.0.1:30092/tar1090/data/aircraft.json')

app = FastAPI(title='adsb18 API', docs_url='/api/docs')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

pool: asyncpg.Pool = None
ws_clients: list[WebSocket] = []

# Track Pi message counter to compute messageRate
_prev_pi_msgs: float = 0.0
_prev_pi_time: float = 0.0

_monitor_status: dict = {
    'ok': None,
    'checked_at': None,
    'pi': {'total': 0, 'by_type': {}},
    'server': {'total': 0},
    'missing': [],
    'error': None,
}


def _classify_type(raw_type: str) -> str:
    if not raw_type:
        return 'other'
    t = raw_type.lower()
    if t.startswith('adsb'):
        return 'ADS-B'
    if t == 'mlat':
        return 'MLAT'
    if t.startswith('tisb'):
        return 'TIS-B'
    if t == 'mode_s':
        return 'Mode-S'
    return 'other'


async def _fetch_pi_live() -> dict[str, str]:
    """Fetch Pi's live aircraft.json via HTTP tunnel, return {hex: raw_type}."""
    proc = await asyncio.create_subprocess_exec(
        'curl', '-s', '--max-time', '5', PI_AIRCRAFT_URL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    data = json.loads(stdout.decode())
    hexes = {}
    for ac in data.get('aircraft', []):
        h = ac.get('hex', '').lower().strip()
        if h and len(h) == 6:
            hexes[h] = ac.get('type', '') or ''
    return hexes


async def _check_pi_alive() -> bool:
    """Return True if Pi tunnel is reachable and aircraft.json timestamp is fresh (<30s)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            'curl', '-s', '--max-time', '5', PI_AIRCRAFT_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        data = json.loads(stdout.decode())
        pi_now = data.get('now')
        if pi_now is None:
            return False
        age = time.time() - float(pi_now)
        return age < 30
    except Exception:
        return False


async def monitor_task():
    """Background task: compare Pi chunks vs server every MONITOR_INTERVAL seconds."""
    global _monitor_status
    await asyncio.sleep(5)  # wait for pool to be ready
    while True:
        try:
            # 1. Fetch Pi's LIVE aircraft.json right now
            pi_hexes = await _fetch_pi_live()  # {hex: raw_type}

            # Count by signal type
            by_type: dict[str, int] = {}
            for raw_t in pi_hexes.values():
                t = _classify_type(raw_t)
                by_type[t] = by_type.get(t, 0) + 1

            # 2. Server: aircraft seen in last MONITOR_SERVER_WINDOW seconds
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=MONITOR_SERVER_WINDOW)
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT icao FROM aircraft WHERE last_seen >= $1", cutoff
                )
            server_hexes = {r['icao'].lower().strip() for r in rows}

            # 3. Missing = Pi live aircraft not on server
            missing = sorted(set(pi_hexes.keys()) - server_hexes)

            _monitor_status = {
                'ok': len(missing) == 0,
                'checked_at': datetime.now(timezone.utc).isoformat(),
                'pi':     {'total': len(pi_hexes), 'by_type': by_type},
                'server': {'total': len(server_hexes)},
                'missing': missing,
                'error': None,
            }

            if missing:
                log.warning(f'Monitor: {len(missing)} missing from server: {missing}')

        except Exception as e:
            log.error(f'Monitor error: {e}')
            _monitor_status['error'] = str(e)
            _monitor_status['ok'] = False

        await asyncio.sleep(MONITOR_INTERVAL)


@app.on_event('startup')
async def startup():
    global pool
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    asyncio.create_task(ws_broadcaster())
    asyncio.create_task(monitor_task())
    log.info('API started, connected to PostgreSQL')


@app.on_event('shutdown')
async def shutdown():
    await pool.close()


# ── Receiver info (tar1090 reads this first on startup) ──────────────────────

@app.get('/data/receiver.json')
async def receiver_json():
    return JSONResponse({
        "version":    "adsb18",
        "refresh":    1000,
        "history":    0,
        "lat":        56.8373,
        "lon":        53.2492,
        "haveTraces": True,
        "zstd":       False,
        "binCraft":   False,
    })


# ── Live aircraft (tar1090 format) ────────────────────────────────────────────

@app.get('/data/aircraft.json')
async def aircraft_json():
    """
    Live aircraft data. Primary: proxy Pi tar1090 directly.
    Fallback: DB query (last 2 min) if Pi/tunnel is unavailable.
    """
    # --- Primary: Pi is source of truth ---
    try:
        proc = await asyncio.create_subprocess_exec(
            'curl', '-s', '--max-time', '3', PI_AIRCRAFT_URL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        data = json.loads(stdout.decode())
        if isinstance(data.get('aircraft'), list):
            global _prev_pi_msgs, _prev_pi_time
            cur_msgs = data.get('messages', 0)
            cur_time = data.get('now', time.time())
            dt = cur_time - _prev_pi_time
            if dt > 0.1 and _prev_pi_msgs > 0 and cur_msgs >= _prev_pi_msgs:
                data['messageRate'] = round((cur_msgs - _prev_pi_msgs) / dt, 1)
            _prev_pi_msgs = cur_msgs
            _prev_pi_time = cur_time
            return JSONResponse(data)
    except Exception as e:
        log.warning(f'Pi proxy failed, using DB fallback: {e}')

    # --- Fallback: reconstruct from DB (last 2 minutes) ---
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=120)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT icao, last_callsign, last_altitude, last_speed,
                   last_track, last_lat, last_lon, last_vrate,
                   last_squawk, is_on_ground, msg_count,
                   EXTRACT(EPOCH FROM (NOW() - last_seen)) AS seen_ago,
                   EXTRACT(EPOCH FROM (NOW() - last_pos_seen)) AS pos_age
            FROM aircraft
            WHERE last_seen >= $1
        """, cutoff)

    aircraft = []
    for r in rows:
        a = {'hex': r['icao'].lower().strip(), 'seen': round(float(r['seen_ago']), 1)}
        if r['last_callsign']: a['flight']    = r['last_callsign'].strip()
        if r['last_altitude'] is not None: a['alt_baro']  = r['last_altitude']
        if r['last_speed']    is not None: a['gs']        = r['last_speed']
        if r['last_track']    is not None: a['track']     = r['last_track']
        pos_age = r['pos_age']
        if r['last_lat'] is not None and pos_age is not None and float(pos_age) < 120:
            a['lat'] = round(r['last_lat'], 5)
            a['lon'] = round(r['last_lon'], 5)
        if r['last_vrate']    is not None: a['baro_rate'] = r['last_vrate']
        if r['last_squawk']:  a['squawk']    = r['last_squawk']
        if r['is_on_ground']: a['ground']    = 1
        a['messages'] = r['msg_count']
        aircraft.append(a)

    return JSONResponse({
        'now':      time.time(),
        'messages': sum(a['messages'] for a in aircraft),
        'aircraft': aircraft,
    })


# ── Monitor ───────────────────────────────────────────────────────────────────

@app.get('/api/monitor')
async def monitor():
    """Pi vs Server comparison status."""
    return JSONResponse(_monitor_status)


# ── History API ───────────────────────────────────────────────────────────────

@app.get('/api/history')
async def history(
    icao: str = Query(..., description='ICAO hex, e.g. 3C6444'),
    from_ts: str = Query(None, alias='from', description='ISO datetime'),
    to_ts:   str = Query(None, alias='to',   description='ISO datetime'),
    limit:   int = Query(2000, le=10000),
):
    """Track history for one aircraft."""
    icao = icao.upper().strip()
    now  = datetime.now(timezone.utc)

    try:
        t_from = datetime.fromisoformat(from_ts) if from_ts else now - timedelta(hours=24)
        t_to   = datetime.fromisoformat(to_ts)   if to_ts   else now
    except ValueError:
        return JSONResponse({'error': 'Invalid date format'}, status_code=400)

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT ts, lat, lon, altitude, ground_speed, track,
                   vertical_rate, squawk, callsign, is_on_ground
            FROM positions
            WHERE icao = $1 AND ts BETWEEN $2 AND $3
              AND lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY ts
            LIMIT $4
        """, icao, t_from, t_to, limit)

    return [dict(r) for r in rows]



# Bulk History API

class _FlightReq(BaseModel):
    icao: str
    from_ts: str
    to_ts: str

class BulkHistoryRequest(BaseModel):
    flights: list[_FlightReq]
    limit_per_flight: int = 300


@app.post('/api/history/bulk')
async def history_bulk(payload: BulkHistoryRequest):
    """Bulk track history for multiple flights.
    Parallel queries (semaphore = pool size).
    Returns: {icao: [points], ...}
    """
    limit = min(max(1, payload.limit_per_flight), 2000)
    sem = asyncio.Semaphore(10)

    async def fetch_one(f):
        icao = f.icao.upper().strip()
        try:
            t_from = datetime.fromisoformat(f.from_ts)
            t_to   = datetime.fromisoformat(f.to_ts)
        except ValueError:
            return icao, []
        async with sem:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT ts, lat, lon, altitude, ground_speed, track,
                              vertical_rate, squawk, callsign, is_on_ground
                       FROM positions
                       WHERE icao = $1 AND ts BETWEEN $2 AND $3
                         AND lat IS NOT NULL AND lon IS NOT NULL
                       ORDER BY ts
                       LIMIT 10000""",
                    icao, t_from, t_to
                )
        if not rows:
            return icao, []
        # Decimate: evenly sample `limit` points across the full track
        if len(rows) <= limit:
            return icao, rows
        step = len(rows) / limit
        decimated = [rows[int(i * step)] for i in range(limit - 1)]
        decimated.append(rows[-1])  # always include last point
        return icao, decimated

    pairs = await asyncio.gather(*[fetch_one(f) for f in payload.flights])
    result = {}
    for icao, rows in pairs:
        if rows:
            result[icao] = [
                {k: (v.isoformat() if hasattr(v, 'isoformat') else v)
                 for k, v in dict(r).items()}
                for r in rows
            ]

    return JSONResponse(result)

@app.get('/api/aircraft')
async def aircraft_list(hours: int = Query(24, le=168)):
    """All aircraft seen in last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT icao, last_callsign, last_altitude, last_speed,
                   last_lat, last_lon, last_seen, msg_count
            FROM aircraft
            WHERE last_seen >= $1
            ORDER BY last_seen DESC
        """, cutoff)
    return [dict(r) for r in rows]


@app.get('/api/archive')
async def archive(
    from_ts: str = Query(..., alias='from'),
    to_ts:   str = Query(..., alias='to'),
):
    """List of unique flights in date range for archive page."""
    try:
        t_from = datetime.fromisoformat(from_ts)
        t_to   = datetime.fromisoformat(to_ts)
    except ValueError:
        return JSONResponse({'error': 'Invalid date format'}, status_code=400)

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                p.icao,
                MAX(p.callsign)     AS callsign,
                MIN(p.ts)           AS first_seen,
                MAX(p.ts)           AS last_seen,
                COUNT(*)            AS points,
                MAX(p.altitude)     AS max_altitude,
                MAX(p.ground_speed) AS max_speed,
                MAX(a.type_code)    AS type_code,
                MAX(a.description)  AS description
            FROM positions p
            LEFT JOIN aircraft a ON a.icao = p.icao
            WHERE p.ts BETWEEN $1 AND $2
              AND p.lat IS NOT NULL
            GROUP BY p.icao
            ORDER BY first_seen DESC
        """, t_from, t_to)

    return [dict(r) for r in rows]


@app.delete('/api/flight')
async def delete_flight(
    icao:    str = Query(..., description='ICAO hex'),
    from_ts: str = Query(..., alias='from'),
    to_ts:   str = Query(..., alias='to'),
):
    """Delete all position records for one flight (icao + time range)."""
    icao = icao.upper().strip()
    try:
        t_from = datetime.fromisoformat(from_ts)
        t_to   = datetime.fromisoformat(to_ts)
    except ValueError:
        return JSONResponse({'error': 'Invalid date format'}, status_code=400)

    async with pool.acquire() as conn:
        result = await conn.execute(
            'DELETE FROM positions WHERE icao = $1 AND ts BETWEEN $2 AND $3',
            icao, t_from, t_to,
        )
        deleted = int(result.split()[-1])
        remaining = await conn.fetchval(
            'SELECT COUNT(*) FROM positions WHERE icao = $1', icao
        )
        if remaining == 0:
            await conn.execute('DELETE FROM aircraft WHERE icao = $1', icao)

    log.info(f'Deleted flight {icao} [{t_from} – {t_to}]: {deleted} rows')
    return {'deleted': deleted, 'icao': icao}


# ── Measurement points ────────────────────────────────────────────────────────

class PointIn(BaseModel):
    name:      str
    address:   str | None = None
    lat:       float
    lon:       float
    date_from: str | None = None   # ISO date YYYY-MM-DD
    date_to:   str | None = None

@app.get('/api/points')
async def points_list():
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            'SELECT id, name, address, lat, lon, date_from, date_to FROM measurement_points ORDER BY id'
        )
    return [dict(r) for r in rows]

def _parse_date(s: str | None):
    return date.fromisoformat(s) if s else None

@app.post('/api/points', status_code=201)
async def points_create(p: PointIn):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''INSERT INTO measurement_points (name, address, lat, lon, date_from, date_to)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING id, name, address, lat, lon, date_from, date_to''',
            p.name, p.address, p.lat, p.lon, _parse_date(p.date_from), _parse_date(p.date_to)
        )
    return dict(row)

@app.put('/api/points/{point_id}')
async def points_update(point_id: int, p: PointIn):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''UPDATE measurement_points
               SET name=$1, address=$2, lat=$3, lon=$4, date_from=$5, date_to=$6
               WHERE id=$7
               RETURNING id, name, address, lat, lon, date_from, date_to''',
            p.name, p.address, p.lat, p.lon, _parse_date(p.date_from), _parse_date(p.date_to), point_id
        )
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail='Point not found')
    return dict(row)

@app.delete('/api/points/{point_id}')
async def points_delete(point_id: int):
    async with pool.acquire() as conn:
        result = await conn.execute('DELETE FROM measurement_points WHERE id=$1', point_id)
    deleted = int(result.split()[-1])
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail='Point not found')
    return {'deleted': point_id}


@app.get('/api/feeders')
async def feeders():
    """List of registered feeders."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, lat, lon, last_connected, msg_count
            FROM feeders ORDER BY last_connected DESC NULLS LAST
        """)
    return [dict(r) for r in rows]


@app.get('/api/receiver-log')
async def receiver_log(
    from_date: str = Query(None, alias='from'),
    to_date:   str = Query(None, alias='to'),
):
    """Receiver uptime log: sessions with flight and route counts."""
    now = datetime.now(timezone.utc)
    t_to   = now
    t_from = now - timedelta(days=30)

    if from_date:
        try:
            d = date.fromisoformat(from_date)
            t_from = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
        except ValueError:
            pass
    if to_date:
        try:
            d = date.fromisoformat(to_date)
            t_to = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
        except ValueError:
            pass

    async with pool.acquire() as conn:
        minutes = await conn.fetch("""
            SELECT DISTINCT date_trunc('minute', ts) AS m
            FROM positions
            WHERE ts >= $1 AND ts <= $2
            ORDER BY m
        """, t_from, t_to)

        if not minutes:
            return []

        GAP = timedelta(minutes=15)
        sessions = []
        sess_start = minutes[0]['m']
        sess_end   = minutes[0]['m']

        for row in minutes[1:]:
            m = row['m']
            if m - sess_end > GAP:
                sessions.append((sess_start, sess_end + timedelta(minutes=1)))
                sess_start = m
            sess_end = m
        sessions.append((sess_start, sess_end + timedelta(minutes=1)))

        feeder_name = await conn.fetchval(
            "SELECT name FROM feeders ORDER BY last_connected DESC NULLS LAST LIMIT 1"
        )


        pi_active = await _check_pi_alive()

        result = []
        for i, (start, end) in enumerate(sessions):
            is_active = pi_active and (i == len(sessions) - 1)
            actual_end = now if is_active else end
            routes = await conn.fetchval("""
                SELECT COUNT(DISTINCT icao)
                FROM positions
                WHERE ts >= $1 AND ts < $2
            """, start, actual_end)
            result.append({
                'feeder':    feeder_name or 'ads-b-pi',
                'date':      start.astimezone(timezone.utc).strftime('%Y-%m-%d'),
                'start_utc': start.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'end_utc':   actual_end.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'routes':    routes,
                'is_active': is_active,
            })

    return result


# ── Trace files (tar1090 format for getTrace) ─────────────────────────────────

@app.get('/data/traces/{last2}/{filename}')
async def trace_json(last2: str, filename: str):
    """
    Serve historical tracks in tar1090 trace_full / trace_recent format.
    URL: /data/traces/{hex[-2:]}/trace_full_{hex}.json
         /data/traces/{hex[-2:]}/trace_recent_{hex}.json
    """
    # Parse filename: trace_full_ABC123.json or trace_recent_ABC123.json
    name = filename
    if name.endswith('.json'):
        name = name[:-5]
    is_recent = name.startswith('trace_recent_')
    hex_part = name[len('trace_recent_'):] if is_recent else name[len('trace_full_'):]
    icao = hex_part.upper().strip()

    if len(icao) != 6:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail='Invalid ICAO')

    hours = 2 if is_recent else 24
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT EXTRACT(EPOCH FROM ts)::double precision AS ts_epoch,
                   lat, lon, altitude, ground_speed, track, vertical_rate
            FROM positions
            WHERE icao = $1
              AND ts >= $2
              AND lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY ts ASC
            LIMIT 20000
        """, icao, cutoff)

    trace = []
    for r in rows:
        trace.append([
            round(float(r['ts_epoch']), 1),   # absolute Unix ts (timestamp=0 so no offset)
            round(float(r['lat']), 5),
            round(float(r['lon']), 5),
            r['altitude'],                     # int or None
            r['ground_speed'],                 # int or None
            r['track'],                        # int or None
            0,                                 # flags (0 = normal)
            r['vertical_rate'],                # int or None
        ])

    return JSONResponse({
        'icao':      icao,
        'timestamp': 0,       # normalizeTraceStamps adds this to each point[0]
        'trace':     trace,
    })


# ── Globe history (tar1090 date picker) ──────────────────────────────────────

@app.get('/globe_history/{year}/{month}/{day}/traces/{last2}/{filename}')
async def globe_history_trace(year: int, month: int, day: int, last2: str, filename: str):
    """
    Historical tracks for a specific date — used by tar1090 date picker.
    URL: /globe_history/2026/03/29/traces/{hex[-2:]}/trace_full_{hex}.json
    """
    from fastapi import HTTPException
    name = filename[:-5] if filename.endswith('.json') else filename
    hex_part = name[len('trace_recent_'):] if name.startswith('trace_recent_') else name[len('trace_full_'):]
    icao = hex_part.upper().strip()

    if len(icao) != 6:
        raise HTTPException(status_code=404, detail='Invalid ICAO')

    try:
        date_from = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc)
        date_to   = datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail='Invalid date')

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT EXTRACT(EPOCH FROM ts)::double precision AS ts_epoch,
                   lat, lon, altitude, ground_speed, track, vertical_rate
            FROM positions
            WHERE icao = $1
              AND ts BETWEEN $2 AND $3
              AND lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY ts ASC
            LIMIT 20000
        """, icao, date_from, date_to)

    if not rows:
        raise HTTPException(status_code=404, detail='No data')

    trace = []
    for r in rows:
        trace.append([
            round(float(r['ts_epoch']), 1),
            round(float(r['lat']), 5),
            round(float(r['lon']), 5),
            r['altitude'],
            r['ground_speed'],
            r['track'],
            0,
            r['vertical_rate'],
        ])

    return JSONResponse({'icao': icao, 'timestamp': 0, 'trace': trace})


# ── WebSocket broadcast ───────────────────────────────────────────────────────

@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.remove(ws)


async def ws_broadcaster():
    """Push aircraft.json to all WS clients every second."""
    while True:
        await asyncio.sleep(1)
        if not ws_clients:
            continue
        try:
            data = await aircraft_json()
            body = data.body.decode()
            dead = []
            for ws in ws_clients:
                try:
                    await ws.send_text(body)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                ws_clients.remove(ws)
        except Exception as e:
            log.error(f'WS broadcast error: {e}')
