"""
adsb18 — Ingest server
Accepts TCP connections from ADS-B feeders (Raspberry Pi),
parses SBS stream, writes to PostgreSQL.

Port: 30001 (feeders connect to this)
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

HOST = os.getenv('INGEST_HOST', '0.0.0.0')
PORT = int(os.getenv('INGEST_PORT', '30001'))
DB_DSN = os.getenv('DATABASE_URL', 'postgresql://adsb:adsb@localhost:5432/adsb18')


async def handle_feeder(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info('peername')
    log.info(f'Feeder connected: {addr}')
    msg_count = 0
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            msg = sbs_parser.parse(line.decode('ascii', errors='ignore'))
            if msg:
                store.enqueue(msg)
                msg_count += 1
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        log.info(f'Feeder disconnected: {addr} — received {msg_count} messages')
        writer.close()


async def main():
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=5)
    log.info('Connected to PostgreSQL')

    # Background DB writer
    asyncio.create_task(store.writer_loop(pool))

    server = await asyncio.start_server(handle_feeder, HOST, PORT)
    log.info(f'Ingest server listening on {HOST}:{PORT}')

    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
