"""
adsb18 — API server (FastAPI)
Serves aircraft.json for tar1090 frontend + REST history API.

Endpoints:
  GET  /data/aircraft.json     — live aircraft (tar1090 format)
  GET  /api/history?icao=&from=&to=   — track history for one aircraft
  GET  /api/aircraft           — all aircraft seen in last 24h
  GET  /api/feeders            — connected feeders
  WS   /ws                     — real-time aircraft updates
"""
import os
import time
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

app = FastAPI(title='adsb18 API', docs_url='/api/docs')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

pool: asyncpg.Pool = None
ws_clients: list[WebSocket] = []


@app.on_event('startup')
async def startup():
    global pool
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    asyncio.create_task(ws_broadcaster())
    log.info('API started, connected to PostgreSQL')


@app.on_event('shutdown')
async def shutdown():
    await pool.close()


# ── Live aircraft (tar1090 format) ────────────────────────────────────────────

@app.get('/data/aircraft.json')
async def aircraft_json():
    """tar1090 frontend reads this every second."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=60)
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
