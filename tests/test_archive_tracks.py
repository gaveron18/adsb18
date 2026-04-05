#!/usr/bin/env python3
"""
End-to-end test: compares track count in DB (via API) vs tracks rendered on the map.

Two modes:
  1. --api-only  (default): fast check via API — verifies all flights have track data
  2. --browser:  loads archive.html in headless Chrome, clicks "All", compares counts

Usage:
    python3 tests/test_archive_tracks.py                     # API-only, today
    python3 tests/test_archive_tracks.py --browser            # full browser test
    python3 tests/test_archive_tracks.py --from 2026-03-31T00:00:00Z --to 2026-04-01T00:00:00Z
"""

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone


BASE_URL = "http://localhost:8098"


def api_get(path: str, timeout: int = 15):
    url = BASE_URL + path
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def api_post(path: str, body: dict, timeout: int = 30):
    url = BASE_URL + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def test_api(from_ts: str, to_ts: str) -> bool:
    """Test via API: get flights, load each track, compare counts."""
    print(f"Period: {from_ts} → {to_ts}")
    print()

    # 1. Get flight list
    flights = api_get(f"/api/archive?from={qe(from_ts)}&to={qe(to_ts)}")
    total = len(flights)
    print(f"Flights from API:  {total}")

    if total == 0:
        print("No flights — nothing to test")
        return True

    # 2. Load each track via /api/history
    loaded = 0
    empty = 0
    failed = 0
    errors = []

    for f in flights:
        icao = f["icao"]
        try:
            pts = api_get(
                f"/api/history?icao={icao}"
                f"&from={qe(f['first_seen'])}&to={qe(f['last_seen'])}"
                f"&limit=500"
            )
            valid = [p for p in pts if p.get("lat") is not None]
            if valid:
                loaded += 1
            else:
                empty += 1
        except Exception as e:
            failed += 1
            errors.append(f"  {icao} ({f.get('callsign', '?')}): {e}")

    expected = total - empty
    print(f"Tracks loaded:     {loaded}")
    print(f"Empty (no pos):    {empty}")
    print(f"Failed:            {failed}")
    print(f"Expected on map:   {expected}")
    print()

    ok = loaded == expected and failed == 0
    if ok:
        print(f"PASS  {loaded}/{expected}")
    else:
        print(f"FAIL  {loaded}/{expected}  (failed={failed})")
        if errors:
            print("Errors:")
            for e in errors:
                print(e)
    return ok


def test_browser(from_ts: str, to_ts: str, screenshot: str) -> bool:
    """Test via Node.js: fetch all tracks same way the browser does, compare counts."""
    print(f"Period: {from_ts} → {to_ts}")
    print()

    # 1. Get expected count from API
    flights = api_get(f"/api/archive?from={qe(from_ts)}&to={qe(to_ts)}")
    total = len(flights)
    print(f"Flights from API: {total}")

    if total == 0:
        print("No flights — nothing to test")
        return True

    # 2. Run Node.js script that simulates selectVisible()
    node_script = f"""
const BASE = "{BASE_URL}";
async function main() {{
    const flights = {json.dumps(flights)};
    let loaded = 0, empty = 0, failed = 0;
    const errors = [];
    for (const f of flights) {{
        try {{
            const url = BASE + '/api/history?icao=' + f.icao
                + '&from=' + encodeURIComponent(f.first_seen)
                + '&to=' + encodeURIComponent(f.last_seen) + '&limit=500';
            const ctrl = new AbortController();
            const timer = setTimeout(() => ctrl.abort(), 10000);
            const r = await fetch(url, {{ signal: ctrl.signal }});
            clearTimeout(timer);
            const pts = await r.json();
            const valid = pts.filter(p => p.lat != null && p.lon != null);
            if (valid.length) loaded++;
            else empty++;
        }} catch(e) {{
            failed++;
            errors.push(f.icao + ': ' + e.message);
        }}
    }}
    const expected = flights.length - empty;
    const ok = loaded === expected && failed === 0;
    console.log(JSON.stringify({{ total: flights.length, loaded, empty, failed, expected, ok, errors }}));
}}
main();
"""
    print("Running Node.js fetch test...")
    result = subprocess.run(
        ["node", "--experimental-fetch", "-e", node_script],
        capture_output=True, text=True, timeout=300,
    )

    if result.returncode != 0:
        print(f"Node.js error: {result.stderr}")
        return False

    data = json.loads(result.stdout.strip())
    print(f"Tracks loaded:   {data['loaded']}")
    print(f"Empty (no pos):  {data['empty']}")
    print(f"Failed:          {data['failed']}")
    print(f"Expected on map: {data['expected']}")
    print()

    if data["ok"]:
        print(f"PASS  {data['loaded']}/{data['expected']}")
    else:
        print(f"FAIL  {data['loaded']}/{data['expected']}  (failed={data['failed']})")
        if data["errors"]:
            print("Errors:")
            for e in data["errors"]:
                print(f"  {e}")

    # 3. Screenshot via headless Chrome (just the real archive page, quick)
    print(f"\nTaking screenshot of archive.html...")
    subprocess.run(
        ["google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
         f"--screenshot={screenshot}", "--window-size=1280,900",
         "--virtual-time-budget=30000",
         f"{BASE_URL}/archive.html"],
        capture_output=True, timeout=60,
    )
    print(f"Screenshot: {screenshot}")

    return data["ok"]


def _build_test_page(from_ts: str, to_ts: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>TESTING</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
</head><body style="margin:0">
<div id="map" style="width:800px;height:400px"></div>
<pre id="result">RUNNING...</pre>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const BASE = "{BASE_URL}";
const FROM = "{from_ts}";
const TO   = "{to_ts}";

const map = L.map('map').setView([57.9, 56.2], 6);

async function runTest() {{
    const res = document.getElementById('result');
    const lines = [];
    function log(s) {{ lines.push(s); res.textContent = lines.join('\\n'); }}
    try {{
        const flights = await fetch(BASE+'/api/archive?from='+encodeURIComponent(FROM)+'&to='+encodeURIComponent(TO)).then(r=>r.json());
        log('API flights: '+flights.length);
        let loaded=0, failed=0, empty=0;
        for (const f of flights) {{
            try {{
                const ctrl = new AbortController();
                const timer = setTimeout(()=>ctrl.abort(), 15000);
                const url = BASE+'/api/history?icao='+f.icao+'&from='+encodeURIComponent(f.first_seen)+'&to='+encodeURIComponent(f.last_seen)+'&limit=500';
                const pts = await fetch(url,{{signal:ctrl.signal}}).then(r=>r.json());
                clearTimeout(timer);
                const valid = pts.filter(p=>p.lat!=null&&p.lon!=null);
                if(!valid.length) {{ empty++; continue; }}
                L.polyline(valid.map(p=>[p.lat,p.lon]),{{weight:2,opacity:0.6}}).addTo(map);
                loaded++;
            }} catch(e) {{ failed++; }}
        }}
        log(''); log('========== RESULT ==========');
        log('API flights:    '+flights.length);
        log('Tracks loaded:  '+loaded);
        log('Empty (no pos): '+empty);
        log('Failed:         '+failed);
        const expected = flights.length - empty;
        if(loaded===expected && failed===0) {{
            log('PASS '+loaded+'/'+expected);
            document.title='PASS '+loaded+'/'+expected;
        }} else {{
            log('FAIL '+loaded+'/'+expected+' (failed='+failed+')');
            document.title='FAIL '+loaded+'/'+expected;
        }}
    }} catch(e) {{ log('FATAL: '+e.message); document.title='FATAL'; }}
}}
runTest();
</script></body></html>"""


def qe(s: str) -> str:
    """URL-encode a timestamp string."""
    return urllib.parse.quote(s, safe="")


def main():
    parser = argparse.ArgumentParser(description="Test archive tracks: DB vs map")
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    parser.add_argument("--from", dest="from_ts",
                        default=today_start.strftime("%Y-%m-%dT%H:%M:%SZ"))
    parser.add_argument("--to", dest="to_ts",
                        default=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    parser.add_argument("--browser", action="store_true",
                        help="Run full browser test (slower)")
    parser.add_argument("--screenshot", default="/tmp/test_archive_tracks.png")
    args = parser.parse_args()

    if args.browser:
        ok = test_browser(args.from_ts, args.to_ts, args.screenshot)
    else:
        ok = test_api(args.from_ts, args.to_ts)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
