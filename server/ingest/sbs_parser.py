"""
SBS (BaseStation) message parser.

SBS format — comma-separated line:
MSG,<type>,<session>,<aircraft>,<icao>,<flight_id>,
    <date_gen>,<time_gen>,<date_log>,<time_log>,
    <callsign>,<altitude>,<speed>,<track>,<lat>,<lon>,
    <vertical_rate>,<squawk>,<alert>,<emergency>,<spi>,<is_on_ground>
"""
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SBSMessage:
    msg_type:      int
    icao:          str
    ts:            datetime
    callsign:      Optional[str]      = None
    altitude:      Optional[int]      = None
    ground_speed:  Optional[int]      = None
    track:         Optional[int]      = None
    lat:           Optional[float]    = None
    lon:           Optional[float]    = None
    vertical_rate: Optional[int]      = None
    squawk:        Optional[str]      = None
    is_on_ground:  bool               = False


def _int(s: str) -> Optional[int]:
    s = s.strip()
    return int(float(s)) if s else None


def _float(s: str) -> Optional[float]:
    s = s.strip()
    return float(s) if s else None


def _str(s: str) -> Optional[str]:
    s = s.strip()
    return s if s else None


def parse(line: str) -> Optional[SBSMessage]:
    """Parse one SBS line. Returns None if line is invalid or not useful."""
    line = line.strip()
    if not line or not line.startswith('MSG,'):
        return None

    parts = line.split(',')
    if len(parts) < 22:
        return None

    try:
        msg_type = int(parts[1])
    except ValueError:
        return None

    # MSG type 8 carries no useful data
    if msg_type == 8:
        return None

    icao = parts[4].strip().upper()
    if not icao or len(icao) != 6:
        return None

    # Parse timestamp from date_gen + time_gen
    try:
        ts_str = f"{parts[6].strip()} {parts[7].strip()}"
        ts = datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        ts = datetime.now(timezone.utc)

    msg = SBSMessage(
        msg_type     = msg_type,
        icao         = icao,
        ts           = ts,
        callsign     = _str(parts[10]),
        altitude     = _int(parts[11]),
        ground_speed = _int(parts[12]),
        track        = _int(parts[13]),
        lat          = _float(parts[14]),
        lon          = _float(parts[15]),
        vertical_rate= _int(parts[16]),
        squawk       = _str(parts[17]),
        is_on_ground = parts[21].strip() == '-1',
    )

    return msg
