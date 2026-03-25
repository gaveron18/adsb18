"""
PostgreSQL writer for ADS-B positions.
Uses asyncpg with batch inserts for performance.
"""
import asyncio
import logging
import asyncpg
from datetime import datetime, timezone
from typing import Optional

from sbs_parser import SBSMessage

log = logging.getLogger(__name__)

# In-memory state per ICAO — merges multiple MSG types into one position
_state: dict[str, dict] = {}

# Batch buffer for positions insert
_batch: list[tuple] = []
BATCH_SIZE  = 200    # insert after N rows
BATCH_SECS  = 2.0   # or after N seconds


def _merge(icao: str, msg: SBSMessage) -> dict:
    """Merge incoming message into per-aircraft state."""
    s = _state.setdefault(icao, {'icao': icao})
    if msg.callsign     is not None: s['callsign']      = msg.callsign
    if msg.altitude     is not None: s['altitude']      = msg.altitude
    if msg.ground_speed is not None: s['ground_speed']  = msg.ground_speed
    if msg.track        is not None: s['track']         = msg.track
    if msg.lat          is not None: s['lat']           = msg.lat
    if msg.lon          is not None: s['lon']           = msg.lon
    if msg.vertical_rate is not None: s['vertical_rate'] = msg.vertical_rate
    if msg.squawk       is not None: s['squawk']        = msg.squawk
    s['is_on_ground'] = msg.is_on_ground
    s['ts'] = msg.ts
    return s


def get_live_aircraft() -> list[dict]:
    """Return current state of all recently seen aircraft (for aircraft.json)."""
    cutoff = datetime.now(timezone.utc).timestamp() - 60  # seen in last 60 sec
    return [
        s for s in _state.values()
        if s.get('ts') and s['ts'].timestamp() > cutoff
    ]


async def writer_loop(pool: asyncpg.Pool):
    """Background task: flush batch to PostgreSQL every BATCH_SECS."""
    global _batch
    while True:
        await asyncio.sleep(BATCH_SECS)
        if _batch:
            batch, _batch = _batch, []
            await _flush(pool, batch)


async def _flush(pool: asyncpg.Pool, batch: list[tuple]):
    sql = """
        INSERT INTO positions
            (ts, icao, callsign, altitude, ground_speed, track,
             lat, lon, vertical_rate, squawk, is_on_ground)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
    """
    try:
        async with pool.acquire() as conn:
            await conn.executemany(sql, batch)
            # Upsert aircraft live state
            for row in batch:
                await conn.execute("""
                    INSERT INTO aircraft
                        (icao, last_seen, first_seen,
                         last_callsign, last_lat, last_lon,
                         last_altitude, last_speed, last_track,
                         last_vrate, last_squawk, is_on_ground, msg_count)
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
                """,
                    row[1],  # icao
                    row[0],  # ts
                    row[2],  # callsign
                    row[6],  # lat
                    row[7],  # lon
                    row[3],  # altitude
                    row[4],  # speed
                    row[5],  # track
                    row[8],  # vrate
                    row[9],  # squawk
                    row[10], # is_on_ground
                )
        log.debug(f'Flushed {len(batch)} positions to DB')
    except Exception as e:
        log.error(f'DB flush error: {e}')
        _batch.extend(batch)  # put back on failure


def enqueue(msg: SBSMessage):
    """Called for every parsed SBS message."""
    global _batch
    s = _merge(msg.icao, msg)

    # Only store if we have at least ts + icao
    if not s.get('ts'):
        return

    _batch.append((
        s['ts'],
        s['icao'],
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

    if len(_batch) >= BATCH_SIZE:
        # Signal flush (non-blocking — writer_loop will pick up)
        pass
