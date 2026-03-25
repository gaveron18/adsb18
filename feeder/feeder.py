"""
adsb18 feeder — runs on Raspberry Pi
Reads SBS stream from dump1090 (localhost:30003)
and forwards to adsb18 ingest server.

Disk buffering: when connection to server is lost, incoming messages are
spooled to a local file. On reconnect, the buffer is replayed first,
then live data continues. No data loss for outages up to ~MAX_BUFFER_MB.

Usage:
  python feeder.py --server 173.249.2.184 --port 30001 --name perm-pi5
"""
import asyncio
import argparse
import logging
import socket
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

DUMP1090_HOST  = '127.0.0.1'
DUMP1090_PORT  = 30003   # SBS output port of dump1090/readsb
RECONNECT_SECS = 5       # wait before reconnecting
MAX_BUFFER_MB  = 200     # max disk buffer size before dropping messages

# Single-threaded asyncio — no locks needed
_connected = False


async def read_dump1090(queue: asyncio.Queue, dump1090_host: str):
    """Reads SBS lines from dump1090 and puts them in queue."""
    while True:
        try:
            log.info(f'Connecting to dump1090 at {dump1090_host}:{DUMP1090_PORT}...')
            reader, _ = await asyncio.open_connection(dump1090_host, DUMP1090_PORT)
            log.info('Connected to dump1090')
            while True:
                line = await reader.readline()
                if not line:
                    break
                await queue.put(line)
        except (ConnectionRefusedError, OSError) as e:
            log.warning(f'dump1090 connection lost: {e}')
        except Exception as e:
            log.error(f'dump1090 reader error: {e}')
        log.info(f'Reconnecting to dump1090 in {RECONNECT_SECS}s...')
        await asyncio.sleep(RECONNECT_SECS)


async def disk_spooler(queue: asyncio.Queue, buffer_path: Path):
    """When offline: drain queue to disk file every 0.5s."""
    while True:
        await asyncio.sleep(0.5)
        if _connected or queue.empty():
            continue

        items = []
        while True:
            try:
                items.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not items:
            continue

        buf_size = buffer_path.stat().st_size if buffer_path.exists() else 0
        if buf_size >= MAX_BUFFER_MB * 1024 * 1024:
            log.warning(f'Disk buffer full ({MAX_BUFFER_MB} MB), dropping {len(items)} messages')
            continue

        with buffer_path.open('ab') as f:
            for item in items:
                f.write(item)
        log.debug(f'Spooled {len(items)} messages to disk (total {(buf_size + sum(len(i) for i in items)) / 1024:.1f} KB)')


async def replay_buffer(writer: asyncio.StreamWriter, buffer_path: Path):
    """Send buffered file to server line by line, then delete it."""
    if not buffer_path.exists():
        return
    size = buffer_path.stat().st_size
    if size == 0:
        buffer_path.unlink()
        return

    log.info(f'Replaying disk buffer: {size / 1024:.1f} KB...')
    count = 0
    with buffer_path.open('rb') as f:
        while True:
            line = f.readline()
            if not line:
                break
            writer.write(line)
            count += 1
            if count % 500 == 0:
                await writer.drain()
    await writer.drain()
    buffer_path.unlink()
    log.info(f'Replayed {count} buffered messages, buffer cleared')


async def send_to_server(queue: asyncio.Queue, server: str, port: int, name: str, buffer_path: Path):
    """Reads from queue and forwards to adsb18 ingest server."""
    global _connected
    while True:
        try:
            log.info(f'Connecting to adsb18 server at {server}:{port}...')
            reader, writer = await asyncio.open_connection(server, port)

            # AUTH before signalling online (disk_spooler keeps writing until AUTH is done)
            writer.write(f'AUTH {name}\n'.encode())
            await writer.drain()
            log.info(f'Authenticated as "{name}"')

            # Signal online — disk_spooler stops writing to disk
            _connected = True

            # Replay disk buffer (data accumulated during outage)
            await replay_buffer(writer, buffer_path)

            # Live queue
            msg_count = 0
            while True:
                line = await queue.get()
                writer.write(line)
                msg_count += 1
                if msg_count % 1000 == 0:
                    await writer.drain()
                    log.info(f'Forwarded {msg_count} messages')

        except (ConnectionRefusedError, OSError) as e:
            log.warning(f'Server connection lost: {e}')
        except Exception as e:
            log.error(f'Server sender error: {e}')
        finally:
            _connected = False

        log.info(f'Reconnecting to server in {RECONNECT_SECS}s...')
        await asyncio.sleep(RECONNECT_SECS)


async def main(args):
    name = args.name or socket.gethostname()
    buffer_path = Path(args.buffer)

    log.info(f'Starting feeder "{name}"')
    log.info(f'dump1090:    {args.dump1090}:{DUMP1090_PORT}')
    log.info(f'Server:      {args.server}:{args.port}')
    log.info(f'Disk buffer: {buffer_path} (max {MAX_BUFFER_MB} MB)')

    queue: asyncio.Queue = asyncio.Queue(maxsize=10000)

    await asyncio.gather(
        read_dump1090(queue, args.dump1090),
        send_to_server(queue, args.server, args.port, name, buffer_path),
        disk_spooler(queue, buffer_path),
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='adsb18 feeder for Raspberry Pi')
    parser.add_argument('--server',   required=True,                              help='adsb18 server IP')
    parser.add_argument('--port',     type=int, default=30001,                    help='adsb18 ingest port')
    parser.add_argument('--name',     default='',                                 help='feeder name (default: hostname)')
    parser.add_argument('--dump1090', default='127.0.0.1',                        help='dump1090 host (default: 127.0.0.1)')
    parser.add_argument('--buffer',   default='/opt/adsb18-feeder/feeder_buffer.sbs',
                                                                                  help='disk buffer file path')
    args = parser.parse_args()
    asyncio.run(main(args))
