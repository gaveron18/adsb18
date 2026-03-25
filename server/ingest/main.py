"""
adsb18 — Ingest server
Accepts TCP connections from multiple ADS-B feeders (Raspberry Pi),
parses SBS stream, writes to PostgreSQL.

Protocol:
  1. Feeder connects to TCP port 30001
  2. First line: AUTH <name>  (e.g. "AUTH perm-pi5")
  3. All subsequent lines: SBS messages

Port: 30001
"""
import asyncio
import logging
import os
import asyncpg

import sbs_parser
import db as store

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

HOST   = os.getenv('INGEST_HOST', '0.0.0.0')
PORT   = int(os.getenv('INGEST_PORT', '30001'))
DB_DSN = os.getenv('DATABASE_URL', 'postgresql://adsb:adsb@localhost:5432/adsb18')

# Connected feeders: name → feeder_id
_feeders: dict[str, int] = {}


async def register_feeder(pool: asyncpg.Pool, name: str, ip: str) -> int:
    """Upsert feeder in DB, return feeder_id."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO feeders (name, last_connected)
            VALUES ($1, NOW())
            ON CONFLICT (name) DO UPDATE
                SET last_connected = NOW()
            RETURNING id
        """, name)
    feeder_id = row['id']
    _feeders[name] = feeder_id
    log.info(f'Feeder registered: "{name}" (id={feeder_id}) from {ip}')
    return feeder_id


async def handle_feeder(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr    = writer.get_extra_info('peername')
    ip      = addr[0] if addr else 'unknown'
    name    = None
    feeder_id = None
    msg_count = 0

    try:
        # First line must be AUTH
        first = await asyncio.wait_for(reader.readline(), timeout=10.0)
        first = first.decode('ascii', errors='ignore').strip()

        if first.startswith('AUTH '):
            name = first[5:].strip()[:64] or ip
        else:
            # No AUTH — use IP as name, treat first line as SBS
            name = ip
            msg = sbs_parser.parse(first)
            if msg:
                store.enqueue(msg, feeder_id=None)
                msg_count += 1

        feeder_id = await register_feeder(pool_ref[0], name, ip)

        # Main SBS reading loop
        while True:
            line = await reader.readline()
            if not line:
                break
            msg = sbs_parser.parse(line.decode('ascii', errors='ignore'))
            if msg:
                store.enqueue(msg, feeder_id=feeder_id)
                msg_count += 1

    except asyncio.TimeoutError:
        log.warning(f'Feeder {ip}: no AUTH received in 10s, disconnecting')
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    except Exception as e:
        log.error(f'Feeder {name or ip} error: {e}')
    finally:
        log.info(f'Feeder disconnected: "{name or ip}" — {msg_count} messages received')
        if feeder_id is not None:
            await _update_feeder_stats(pool_ref[0], feeder_id, msg_count)
        writer.close()


async def _update_feeder_stats(pool: asyncpg.Pool, feeder_id: int, msg_count: int):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE feeders SET msg_count = msg_count + $1 WHERE id = $2
            """, msg_count, feeder_id)
    except Exception as e:
        log.error(f'Failed to update feeder stats: {e}')


# Global pool reference (set in main)
pool_ref: list = [None]


async def main():
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=10)
    pool_ref[0] = pool
    store.set_pool(pool)
    log.info('Connected to PostgreSQL')

    # Background DB writer
    asyncio.create_task(store.writer_loop())

    server = await asyncio.start_server(handle_feeder, HOST, PORT)
    addrs  = ', '.join(str(s.getsockname()) for s in server.sockets)
    log.info(f'Ingest server listening on {addrs}')
    log.info(f'Waiting for feeders... (AUTH protocol on port {PORT})')

    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
