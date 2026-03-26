"""
adsb18 JSON feeder — runs on Raspberry Pi
Reads /run/readsb/aircraft.json every second and forwards full snapshots
to the adsb18 ingest server as compact JSON lines.

This replaces the SBS-stream feeder (feeder.py) with a simpler, richer
approach: instead of tailing individual SBS messages, we send complete
aircraft.json snapshots that include all currently visible aircraft.

Usage:
  python feeder_json.py --server 127.0.0.1 --port 30001 --name ads-b-pi
"""
import argparse
import asyncio
import json
import logging
import socket
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

AIRCRAFT_JSON   = '/run/readsb/aircraft.json'
READ_INTERVAL   = 1.0   # seconds between reads
RECONNECT_SECS  = 5     # seconds to wait before reconnecting after disconnect
LOG_EVERY       = 30    # log summary every N snapshots


async def read_snapshot() -> dict | None:
    """Read and parse /run/readsb/aircraft.json. Returns None on error."""
    try:
        path = Path(AIRCRAFT_JSON)
        raw = await asyncio.get_event_loop().run_in_executor(None, path.read_bytes)
        return json.loads(raw)
    except FileNotFoundError:
        log.warning(f'aircraft.json not found: {AIRCRAFT_JSON}')
        return None
    except json.JSONDecodeError as e:
        log.warning(f'aircraft.json parse error: {e}')
        return None
    except OSError as e:
        log.warning(f'aircraft.json read error: {e}')
        return None


async def send_loop(server: str, port: int, name: str):
    """
    Main loop: connect to server, authenticate, then send one JSON
    snapshot per second. Reconnects automatically on any error.
    """
    while True:
        writer = None
        try:
            log.info(f'Connecting to adsb18 server at {server}:{port}...')
            reader, writer = await asyncio.open_connection(server, port)
            log.info(f'Connected. Authenticating as "{name}"...')

            # Auth handshake
            writer.write(f'AUTH-JSON {name}\n'.encode('utf-8'))
            await writer.drain()
            log.info(f'Authenticated as "{name}" (JSON mode)')

            snapshot_count = 0
            next_read = asyncio.get_event_loop().time()

            while True:
                # Pace the loop to READ_INTERVAL regardless of how long I/O takes
                now = asyncio.get_event_loop().time()
                sleep_for = next_read - now
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                next_read = asyncio.get_event_loop().time() + READ_INTERVAL

                data = await read_snapshot()
                if data is None:
                    continue

                line = json.dumps(data, separators=(',', ':')) + '\n'
                writer.write(line.encode('utf-8'))
                await writer.drain()

                snapshot_count += 1

                if snapshot_count % LOG_EVERY == 0:
                    aircraft = data.get('aircraft', [])
                    with_pos = sum(
                        1 for a in aircraft
                        if 'lat' in a and 'lon' in a
                    )
                    log.info(
                        f'Snapshots sent: {snapshot_count} | '
                        f'aircraft: {len(aircraft)} total, {with_pos} with position'
                    )

        except (ConnectionRefusedError, OSError) as e:
            log.warning(f'Server connection lost: {e}')
        except asyncio.IncompleteReadError:
            log.warning('Server closed connection')
        except Exception as e:
            log.error(f'Unexpected error: {e}')
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass

        log.info(f'Reconnecting in {RECONNECT_SECS}s...')
        await asyncio.sleep(RECONNECT_SECS)


async def main(args):
    name = args.name or socket.gethostname()
    log.info(f'adsb18 JSON feeder starting')
    log.info(f'Feeder name:   {name}')
    log.info(f'Source:        {AIRCRAFT_JSON}')
    log.info(f'Server:        {args.server}:{args.port}')
    log.info(f'Read interval: {READ_INTERVAL}s')

    await send_loop(args.server, args.port, name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='adsb18 JSON feeder — sends aircraft.json snapshots to ingest server'
    )
    parser.add_argument('--server', default='127.0.0.1',
                        help='adsb18 ingest server host (default: 127.0.0.1)')
    parser.add_argument('--port',   type=int, default=30001,
                        help='adsb18 ingest port (default: 30001)')
    parser.add_argument('--name',   default='',
                        help='feeder name sent to server (default: hostname)')
    args = parser.parse_args()
    asyncio.run(main(args))
