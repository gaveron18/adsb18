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
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

DB_DSN = os.getenv('DATABASE_URL', 'postgresql://adsb:adsb@postgres:5432/adsb18')

PI_SSH_CMD = ['ssh', '-o', 'StrictHostKeyChecking=no', '-o', 'ConnectTimeout=5',
              '-p', '52222', 'ads-b@127.0.0.1']

MONITOR_INTERVAL = 30    # seconds between checks
MONITOR_WINDOW   = 3600  # 1 hour — match tar1090 chunk history

# Script sent to Pi via stdin: reads all tar1090 gz chunks, returns {hex: type} JSON
_PI_CHUNKS_SCRIPT = b"""
import glob, gzip, json
chunks = glob.glob('/run/tar1090/chunk_*.gz')
for extra in ['/run/tar1090/current_large.gz', '/run/tar1090/current_small.gz']:
    if extra not in chunks:
        chunks.append(extra)
hexes = {}
for f in chunks:
    try:
        raw = gzip.open(f).read().decode()
        for line in raw.strip().split('\\n'):
            line = line.strip().rstrip(',')
            if not line:
                continue
            try:
                d = json.loads(line)
                for a in d.get('aircraft', []):
                    if isinstance(a, list) and len(a) > 0:
                        h = a[0].lower().strip()
                        t = a[7] if len(a) > 7 else ''
                        if h and h not in hexes:
                            hexes[h] = t or ''
            except Exception:
                pass
    except Exception:
        pass
print(json.dumps(hexes))
"""

app = FastAPI(title='adsb18 API', docs_url='/api/docs')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

pool: asyncpg.Pool = None
ws_clients: list[WebSocket] = []

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


async def _fetch_pi_chunks() -> dict[str, str]:
    """SSH to Pi, run chunk-reader script, return {hex: raw_type}."""
    proc = await asyncio.create_subprocess_exec(
        *PI_SSH_CMD, 'python3',
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(input=_PI_CHUNKS_SCRIPT), timeout=15)
    return json.loads(stdout.decode())


async def monitor_task():
    """Background task: compare Pi chunks vs server every MONITOR_INTERVAL seconds."""
    global _monitor_status
    await asyncio.sleep(5)  # wait for pool to be ready
    while True:
        try:
            # 1. Fetch all unique aircraft from Pi tar1090 chunks (last ~1 hour)
            pi_hexes = await _fetch_pi_chunks()  # {hex: raw_type}

            # Count by signal type
            by_type: dict[str, int] = {}
            for raw_t in pi_hexes.values():
                t = _classify_type(raw_t)
                by_type[t] = by_type.get(t, 0) + 1

            # 2. Fetch server aircraft seen in last MONITOR_WINDOW seconds
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=MONITOR_WINDOW)
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT icao FROM aircraft WHERE last_seen >= $1", cutoff
                )
            server_hexes = {r['icao'].lower().strip() for r in rows}

            # 3. Missing = on Pi chunks but not on server
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
                log.warning(f'Monitor: {len(missing)} aircraft on Pi missing from server: {missing}')

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
        "version":  "adsb18",
        "refresh":  1000,
        "history":  0,
        "lat":      56.8373,
        "lon":      53.2492,
    })


# ── Live aircraft (tar1090 format) ────────────────────────────────────────────

@app.get('/data/aircraft.json')
async def aircraft_json():
    """tar1090 frontend reads this every second."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=3600)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT icao, last_callsign, last_altitude, last_speed,
                   last_track, last_lat, last_lon, last_vrate,
                   last_squawk, is_on_ground, msg_count,
                   EXTRACT(EPOCH FROM (NOW() - last_seen)) AS seen_ago
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
        if r['last_lat']      is not None: a['lat']       = round(r['last_lat'], 5)
        if r['last_lon']      is not None: a['lon']       = round(r['last_lon'], 5)
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
                icao,
                MAX(callsign) AS callsign,
                MIN(ts)       AS first_seen,
                MAX(ts)       AS last_seen,
                COUNT(*)      AS points,
                MAX(altitude) AS max_altitude,
                MAX(ground_speed) AS max_speed
            FROM positions
            WHERE ts BETWEEN $1 AND $2
              AND lat IS NOT NULL
            GROUP BY icao
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


@app.get('/api/feeders')
async def feeders():
    """List of registered feeders."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, lat, lon, last_connected, msg_count
            FROM feeders ORDER BY last_connected DESC NULLS LAST
        """)
    return [dict(r) for r in rows]


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
