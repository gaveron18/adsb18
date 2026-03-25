"""
adsb18 — Aircraft simulator
Generates fake ADS-B (SBS) data and sends to ingest server.
Simulates aircraft flying around Perm (USPP area).

Usage:
  python simulator.py --server 127.0.0.1 --port 30001
"""
import asyncio
import argparse
import math
import random
import time
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

# Perm area center
CENTER_LAT = 57.914
CENTER_LON = 56.218

AIRCRAFT = [
    dict(icao='424141', callsign='AFL1234', altitude=10000, speed=450, track=45,
         lat=57.5, lon=55.5, vrate=0, squawk='2100'),
    dict(icao='424142', callsign='SBI301',  altitude=8000,  speed=380, track=120,
         lat=58.3, lon=56.8, vrate=-512, squawk='3100'),
    dict(icao='424143', callsign='UTR215',  altitude=12000, speed=500, track=270,
         lat=57.9, lon=57.5, vrate=0, squawk='4200'),
    dict(icao='424144', callsign='UTA102',  altitude=5000,  speed=300, track=200,
         lat=58.1, lon=55.9, vrate=256, squawk='1200'),
    dict(icao='424145', callsign='GZP443',  altitude=3000,  speed=250, track=350,
         lat=57.7, lon=56.4, vrate=-256, squawk='5300'),
    dict(icao='424146', callsign='AFL777',  altitude=11000, speed=480, track=90,
         lat=57.2, lon=55.0, vrate=0, squawk='2200'),
    dict(icao='424147', callsign='SBI022',  altitude=9000,  speed=420, track=315,
         lat=58.5, lon=57.0, vrate=0, squawk='3300'),
    dict(icao='424148', callsign='UTA501',  altitude=1500,  speed=180, track=210,
         lat=57.95, lon=56.22, vrate=-1024, squawk='7700'),  # на посадке
]


def move(ac: dict, dt: float):
    """Move aircraft by dt seconds."""
    spd_ms  = ac['speed'] * 0.514444       # knots → m/s
    dist_m  = spd_ms * dt
    bearing = math.radians(ac['track'])

    lat_r = math.radians(ac['lat'])
    lon_r = math.radians(ac['lon'])
    R     = 6371000.0

    new_lat = math.asin(
        math.sin(lat_r) * math.cos(dist_m / R) +
        math.cos(lat_r) * math.sin(dist_m / R) * math.cos(bearing)
    )
    new_lon = lon_r + math.atan2(
        math.sin(bearing) * math.sin(dist_m / R) * math.cos(lat_r),
        math.cos(dist_m / R) - math.sin(lat_r) * math.sin(new_lat)
    )

    ac['lat'] = math.degrees(new_lat)
    ac['lon'] = math.degrees(new_lon)

    # Update altitude by vrate
    ac['altitude'] = max(0, ac['altitude'] + int(ac['vrate'] * dt / 60))

    # Slightly drift track for realism
    ac['track'] = (ac['track'] + random.uniform(-1, 1)) % 360

    # Turn back toward center if too far (>200 km)
    dist_from_center = math.sqrt(
        (ac['lat'] - CENTER_LAT) ** 2 + (ac['lon'] - CENTER_LON) ** 2
    ) * 111
    if dist_from_center > 200:
        dy = CENTER_LAT - ac['lat']
        dx = CENTER_LON - ac['lon']
        ac['track'] = (math.degrees(math.atan2(dx, dy)) + 360) % 360


def sbs_line(ac: dict, msg_type: int) -> str:
    """Generate one SBS message string."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime('%Y/%m/%d')
    time_str = now.strftime('%H:%M:%S.%f')[:-3]

    callsign = ac.get('callsign', '')
    alt      = int(ac['altitude']) if msg_type in (3, 5) else ''
    spd      = int(ac['speed'])    if msg_type == 4 else ''
    track    = int(ac['track'])    if msg_type == 4 else ''
    lat      = f"{ac['lat']:.5f}"  if msg_type == 3 else ''
    lon      = f"{ac['lon']:.5f}"  if msg_type == 3 else ''
    vrate    = int(ac['vrate'])    if msg_type == 4 else ''
    squawk   = ac.get('squawk', '') if msg_type == 6 else ''
    cs       = callsign             if msg_type == 1 else ''

    on_ground = -1 if ac.get('altitude', 9999) < 100 else 0

    return (
        f"MSG,{msg_type},1,1,{ac['icao']},1,"
        f"{date_str},{time_str},{date_str},{time_str},"
        f"{cs},{alt},{spd},{track},{lat},{lon},{vrate},{squawk},0,0,0,{on_ground}\n"
    )


async def simulate(writer: asyncio.StreamWriter):
    """Generate and send SBS messages continuously."""
    last_t = time.time()
    tick   = 0

    while True:
        await asyncio.sleep(1)
        now_t = time.time()
        dt    = now_t - last_t
        last_t = now_t
        tick  += 1

        for ac in AIRCRAFT:
            move(ac, dt)
            lines = []

            # MSG,3 — position (every tick)
            lines.append(sbs_line(ac, 3))
            # MSG,4 — speed/track (every tick)
            lines.append(sbs_line(ac, 4))
            # MSG,1 — callsign (every 10 ticks)
            if tick % 10 == 0:
                lines.append(sbs_line(ac, 1))
            # MSG,6 — squawk (every 30 ticks)
            if tick % 30 == 0:
                lines.append(sbs_line(ac, 6))

            for line in lines:
                writer.write(line.encode())

        await writer.drain()

        if tick % 30 == 0:
            log.info(f'Tick {tick}: {len(AIRCRAFT)} aircraft simulated')


async def main(args):
    log.info(f'Connecting to {args.server}:{args.port}...')
    while True:
        try:
            reader, writer = await asyncio.open_connection(args.server, args.port)
            writer.write(f'AUTH simulator\n'.encode())
            await writer.drain()
            log.info('Connected. Simulating aircraft...')
            await simulate(writer)
        except (ConnectionRefusedError, OSError) as e:
            log.warning(f'Connection failed: {e}. Retrying in 5s...')
            await asyncio.sleep(5)
        except Exception as e:
            log.error(f'Error: {e}. Retrying in 5s...')
            await asyncio.sleep(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='adsb18 aircraft simulator')
    parser.add_argument('--server', default='127.0.0.1', help='ingest server host')
    parser.add_argument('--port',   type=int, default=30001, help='ingest server port')
    args = parser.parse_args()
    asyncio.run(main(args))
