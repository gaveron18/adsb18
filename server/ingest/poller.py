"""
adsb18 poller — runs on VPS.
Polls Pi's readsb aircraft.json via SSH reverse tunnel (no feeder needed on Pi).

Pi's lighttpd (port 80) is forwarded to VPS:30092 via SSH reverse tunnel:
  -R 30092:localhost:80  in adsb-tunnel.service on Pi.

URL: http://127.0.0.1:30092/data/aircraft.json
"""
import asyncio
import json
import logging
import os
import urllib.request

import asyncpg

import db as store

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)

PI_URL   = os.getenv('PI_AIRCRAFT_URL', 'http://127.0.0.1:30092/tar1090/data/aircraft.json')
DB_DSN   = os.getenv('DATABASE_URL', 'postgresql://adsb:adsb2024@localhost:5432/adsb18')
INTERVAL = float(os.getenv('POLL_INTERVAL', '1.0'))


def _fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as resp:
        return json.loads(resp.read())


async def poll_loop(feeder_id: int):
    last_now  = 0.0
    poll_count = 0
    errors     = 0

    while True:
        try:
            data = await asyncio.to_thread(_fetch, PI_URL)
            now  = float(data.get('now', 0))

            if now != last_now:
                store.process_snapshot(data, feeder_id=feeder_id)
                last_now = now
                poll_count += 1
                errors = 0

                if poll_count % 30 == 0:
                    ac       = data.get('aircraft', [])
                    with_pos = sum(1 for a in ac if 'lat' in a)
                    log.info(f'poll #{poll_count}: aircraft={len(ac)} with_pos={with_pos} batch={len(store._batch)}')

        except Exception as e:
            errors += 1
            if errors <= 3 or errors % 30 == 0:
                log.warning(f'poll error ({errors}): {e}')

        await asyncio.sleep(INTERVAL)


async def main():
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    store.set_pool(pool)
    log.info(f'PostgreSQL connected. Polling {PI_URL} every {INTERVAL}s')

    asyncio.create_task(store.partition_watchdog())
    asyncio.create_task(store.writer_loop())

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO feeders (name, last_connected)
            VALUES ('ads-b-pi', NOW())
            ON CONFLICT (name) DO UPDATE SET last_connected = NOW()
            RETURNING id
        """)
    feeder_id = row['id']
    log.info(f'Feeder id={feeder_id}')

    await poll_loop(feeder_id)


if __name__ == '__main__':
    asyncio.run(main())
