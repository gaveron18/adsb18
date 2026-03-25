"""
PostgreSQL writer for ADS-B positions.
Buffers messages and flushes in batches for performance.
Supports multiple feeders via feeder_id.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
import asyncpg

from sbs_parser import SBSMessage

log = logging.getLogger(__name__)

# Per-ICAO live state (merges multiple SBS message types)
_state: dict[str, dict] = {}

# Write buffer
_batch: list[tuple] = []
BATCH_SIZE = 200
BATCH_SECS = 2.0

_pool: Optional[asyncpg.Pool] = None


def set_pool(pool: asyncpg.Pool):
    global _pool
    _pool = pool


def _merge(icao: str, msg: SBSMessage, feeder_id: Optional[int]) -> dict:
    """Merge incoming message fields into per-aircraft live state."""
    s = _state.setdefault(icao, {'icao': icao})
    if msg.callsign      is not None: s['callsign']      = msg.callsign
    if msg.altitude      is not None: s['altitude']      = msg.altitude
    if msg.ground_speed  is not None: s['ground_speed']  = msg.ground_speed
    if msg.track         is not None: s['track']         = msg.track
    if msg.lat           is not None: s['lat']           = msg.lat
    if msg.lon           is not None: s['lon']           = msg.lon
    if msg.vertical_rate is not None: s['vertical_rate'] = msg.vertical_rate
    if msg.squawk        is not None: s['squawk']        = msg.squawk
    s['is_on_ground'] = msg.is_on_ground
    s['ts']           = msg.ts
    s['feeder_id']    = feeder_id
    return s


def get_live_aircraft() -> list[dict]:
    """Current state of all aircraft seen in last 60 sec — used for aircraft.json."""
    cutoff = datetime.now(timezone.utc).timestamp() - 60
    return [
        s for s in _state.values()
        if s.get('ts') and s['ts'].timestamp() > cutoff
    ]


def enqueue(msg: SBSMessage, feeder_id: Optional[int] = None):
    """Called for every parsed SBS message from any feeder."""
    global _batch
    s = _merge(msg.icao, msg, feeder_id)
    if not s.get('ts'):
        return
    _batch.append((
        s['ts'],
        s['icao'],
        s.get('feeder_id'),
        s.get('callsign'),
        s.get('altitude'),
        s.get('ground_speed'),
        s.get('track'),
        s.get('lat'),
        s.get('lon'),
        s.get('vertical_rate'),
        s.get('squawk'),
        s.get('is_on_ground', False),
    ))


async def writer_loop():
    """Background task: flush batch to PostgreSQL every BATCH_SECS."""
    global _batch
    while True:
        await asyncio.sleep(BATCH_SECS)
        if _batch and _pool:
            batch, _batch = _batch, []
            await _flush(batch)


async def _flush(batch: list[tuple]):
    pos_sql = """
        INSERT INTO positions
            (ts, icao, feeder_id, callsign, altitude, ground_speed, track,
             lat, lon, vertical_rate, squawk, is_on_ground)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
    """
    ac_sql = """
        INSERT INTO aircraft
            (icao, last_seen, first_seen, last_callsign,
             last_lat, last_lon, last_altitude, last_speed,
             last_track, last_vrate, last_squawk, is_on_ground, msg_count)
        VALUES ($1,$2,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,1)
        ON CONFLICT (icao) DO UPDATE SET
            last_seen     = EXCLUDED.last_seen,
            last_callsign = COALESCE(EXCLUDED.last_callsign, aircraft.last_callsign),
            last_lat      = COALESCE(EXCLUDED.last_lat,      aircraft.last_lat),
            last_lon      = COALESCE(EXCLUDED.last_lon,      aircraft.last_lon),
            last_altitude = COALESCE(EXCLUDED.last_altitude, aircraft.last_altitude),
            last_speed    = COALESCE(EXCLUDED.last_speed,    aircraft.last_speed),
            last_track    = COALESCE(EXCLUDED.last_track,    aircraft.last_track),
            last_vrate    = COALESCE(EXCLUDED.last_vrate,    aircraft.last_vrate),
            last_squawk   = COALESCE(EXCLUDED.last_squawk,   aircraft.last_squawk),
            is_on_ground  = EXCLUDED.is_on_ground,
            msg_count     = aircraft.msg_count + 1
    """
    try:
        async with _pool.acquire() as conn:
            await conn.executemany(pos_sql, batch)
            # Upsert aircraft state (one per unique ICAO in batch)
            seen = {}
            for row in batch:
                seen[row[1]] = row  # keep last row per ICAO
            for row in seen.values():
                await conn.execute(ac_sql,
                    row[1],  # icao
                    row[0],  # ts
                    row[3],  # callsign
                    row[7],  # lat
                    row[8],  # lon
                    row[4],  # altitude
                    row[5],  # speed
                    row[6],  # track
                    row[9],  # vrate
                    row[10], # squawk
                    row[11], # is_on_ground
                )
        log.debug(f'Flushed {len(batch)} positions, {len(seen)} aircraft updated')
    except Exception as e:
        log.error(f'DB flush error: {e}')
        global _batch
        _batch.extend(batch)  # retry on next flush
