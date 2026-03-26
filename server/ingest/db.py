"""
PostgreSQL writer for ADS-B positions.
Buffers messages and flushes in batches for performance.
Supports multiple feeders via feeder_id.
"""
import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Optional
import asyncpg

from sbs_parser import SBSMessage

log = logging.getLogger(__name__)

# Per-ICAO live state (merges multiple SBS message types)
_state: dict[str, dict] = {}

# Last validated position per ICAO — used for ghost position filtering
_last_valid_pos: dict[str, tuple] = {}  # icao → (lat, lon, ts)
MAX_SPEED_KTS = 800  # above this speed the position jump is physically impossible


def _valid_position(icao: str, lat: float, lon: float, ts: datetime) -> bool:
    """Return False if the position jump from the last known fix is impossible."""
    prev = _last_valid_pos.get(icao)
    if prev is None:
        _last_valid_pos[icao] = (lat, lon, ts)
        return True
    prev_lat, prev_lon, prev_ts = prev
    dt = (ts - prev_ts).total_seconds()
    if dt <= 0:
        return True
    dlat = math.radians(lat - prev_lat)
    dlon = math.radians(lon - prev_lon)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(prev_lat)) * math.cos(math.radians(lat))
         * math.sin(dlon / 2) ** 2)
    dist_km = 2 * 6371.0 * math.asin(math.sqrt(max(0.0, a)))
    speed_kts = (dist_km / 1.852) / (dt / 3600.0)
    if speed_kts > MAX_SPEED_KTS:
        log.warning(
            f'{icao}: ghost position rejected '
            f'({prev_lat:.3f},{prev_lon:.3f})→({lat:.3f},{lon:.3f}) '
            f'{dist_km:.0f}km in {dt:.1f}s = {speed_kts:.0f}kts'
        )
        return False
    _last_valid_pos[icao] = (lat, lon, ts)
    return True

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
    if msg.lat is not None and msg.lon is not None:
        if _valid_position(icao, msg.lat, msg.lon, msg.ts):
            s['lat'] = msg.lat
            s['lon'] = msg.lon
        else:
            # Ghost position — clear lat/lon so it doesn't pollute the track
            s.pop('lat', None)
            s.pop('lon', None)
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
    # Only store coordinates if the CURRENT message actually has position data.
    # _state retains the last-known lat/lon, so s.get('lat') would carry stale
    # coordinates into non-position messages (speed, altitude, etc.), creating
    # hundreds of duplicate points at the same location.
    has_pos = msg.lat is not None and msg.lon is not None
    _batch.append((
        s['ts'],
        s['icao'],
        s.get('feeder_id'),
        s.get('callsign'),
        s.get('altitude'),
        s.get('ground_speed'),
        s.get('track'),
        s.get('lat') if has_pos else None,  # validated by _merge/_valid_position
        s.get('lon') if has_pos else None,
        s.get('vertical_rate'),
        s.get('squawk'),
        s.get('is_on_ground', False),
    ))


def process_snapshot(data: dict, feeder_id: Optional[int] = None) -> int:
    """
    Process a full aircraft.json snapshot from readsb.

    data: dict with keys:
      'now'      — float UNIX timestamp (seconds)
      'aircraft' — list of aircraft dicts

    Each aircraft dict may contain:
      hex, flight, alt_baro, gs, track, lat, lon,
      baro_rate, squawk, seen, seen_pos, ground

    Returns the count of aircraft added to _batch (those with lat+lon).
    """
    global _batch

    now_ts = float(data.get('now', 0))
    if now_ts == 0:
        now_ts = datetime.now(timezone.utc).timestamp()
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    aircraft_list = data.get('aircraft', [])
    added = 0

    for ac in aircraft_list:
        # --- ICAO ---
        hex_raw = ac.get('hex', '')
        icao = hex_raw.upper().strip()
        if len(icao) != 6:
            continue

        # --- Callsign ---
        flight = ac.get('flight')
        callsign = flight.strip() if flight else None
        if callsign == '':
            callsign = None

        # --- Altitude ---
        alt_baro = ac.get('alt_baro')
        is_on_ground = bool(ac.get('ground', False))
        altitude = None
        if alt_baro == 'ground':
            altitude = 0
            is_on_ground = True
        elif alt_baro is not None:
            try:
                altitude = int(round(float(alt_baro)))
            except (TypeError, ValueError):
                altitude = None

        # --- Ground speed ---
        gs = ac.get('gs')
        ground_speed = None
        if gs is not None:
            try:
                ground_speed = int(round(float(gs)))
            except (TypeError, ValueError):
                ground_speed = None

        # --- Track ---
        track_raw = ac.get('track')
        track = None
        if track_raw is not None:
            try:
                track = int(round(float(track_raw)))
            except (TypeError, ValueError):
                track = None

        # --- Position ---
        lat = ac.get('lat')
        lon = ac.get('lon')
        if lat is not None:
            try:
                lat = float(lat)
            except (TypeError, ValueError):
                lat = None
        if lon is not None:
            try:
                lon = float(lon)
            except (TypeError, ValueError):
                lon = None

        # --- Vertical rate ---
        baro_rate = ac.get('baro_rate')
        vertical_rate = None
        if baro_rate is not None:
            try:
                vertical_rate = int(round(float(baro_rate)))
            except (TypeError, ValueError):
                vertical_rate = None

        # --- Squawk ---
        squawk = ac.get('squawk')
        if squawk is not None:
            squawk = str(squawk).strip() or None

        # --- Timestamps ---
        seen = ac.get('seen', 0)
        seen_pos = ac.get('seen_pos')
        try:
            seen = float(seen)
        except (TypeError, ValueError):
            seen = 0.0

        # ts for the position record: prefer seen_pos if lat/lon present
        if lat is not None and lon is not None and seen_pos is not None:
            try:
                pos_ts = datetime.fromtimestamp(now_ts - float(seen_pos), tz=timezone.utc)
            except (TypeError, ValueError):
                pos_ts = datetime.fromtimestamp(now_ts - seen, tz=timezone.utc)
        else:
            pos_ts = datetime.fromtimestamp(now_ts - seen, tz=timezone.utc)

        # --- Update _state for all aircraft ---
        s = _state.setdefault(icao, {'icao': icao})
        if callsign   is not None: s['callsign']      = callsign
        if altitude   is not None: s['altitude']      = altitude
        if ground_speed is not None: s['ground_speed'] = ground_speed
        if track      is not None: s['track']         = track
        if lat is not None and lon is not None:
            if _valid_position(icao, lat, lon, pos_ts):
                s['lat'] = lat
                s['lon'] = lon
            else:
                s.pop('lat', None)
                s.pop('lon', None)
                lat, lon = None, None  # don't store in batch either
        if vertical_rate is not None: s['vertical_rate'] = vertical_rate
        if squawk     is not None: s['squawk']         = squawk
        s['is_on_ground'] = is_on_ground
        s['ts']        = pos_ts
        s['feeder_id'] = feeder_id

        # --- Add to _batch (positions + aircraft table) for all aircraft ---
        # For aircraft without lat/lon, lat/lon will be None — _flush uses COALESCE,
        # so the aircraft table keeps its last known position.
        _batch.append((
            pos_ts,
            icao,
            feeder_id,
            callsign,
            altitude,
            ground_speed,
            track,
            lat,
            lon,
            vertical_rate,
            squawk,
            is_on_ground,
        ))

        if lat is not None and lon is not None:
            added += 1

    return added


async def ensure_partitions():
    """Create partitions for current month + next 2 months if they don't exist."""
    if not _pool:
        return
    now = datetime.now(timezone.utc)
    async with _pool.acquire() as conn:
        for i in range(3):
            month = now.month + i
            year  = now.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            await conn.execute(
                "SELECT create_monthly_partition('positions', $1, $2)",
                year, month,
            )
    log.info('Partitions checked/created for current + next 2 months')


async def partition_watchdog():
    """Background task: ensure partitions exist, check daily."""
    while True:
        await ensure_partitions()
        await asyncio.sleep(86400)  # 24 hours


async def writer_loop():
    """Background task: flush batch to PostgreSQL every BATCH_SECS."""
    global _batch
    while True:
        await asyncio.sleep(BATCH_SECS)
        log.info(f'writer_loop tick: batch={len(_batch)} pool={_pool is not None}')
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
