#!/usr/bin/env python3
"""
Импорт справочника типов ВС.
Источник 1: tar1090-db (широкое покрытие западных бортов)
Источник 2: OpenSky Network (покрывает Россию, Китай и весь мир)
Запускать: sudo /opt/adsb18-venv/bin/python3 /home/new/adsb18/import_aircraft_db.py
"""
import gzip, json, csv, io, urllib.request, psycopg2

DB_URL = "postgresql://adsb:adsb2024@localhost:5432/adsb18"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "adsb18-importer"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def upsert(cur, rows):
    cur.executemany("""
        INSERT INTO aircraft (icao, registration, type_code, description)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (icao) DO UPDATE SET
            registration = EXCLUDED.registration,
            type_code    = EXCLUDED.type_code,
            description  = EXCLUDED.description
        WHERE EXCLUDED.type_code IS NOT NULL
    """, rows)

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()

# ── 1. tar1090-db ──────────────────────────────────────────────────────────────
print("=== tar1090-db ===")
INDEX_URL = "https://api.github.com/repos/wiedehopf/tar1090-db/contents/db"
files = [f["name"].replace(".js","") for f in json.loads(fetch(INDEX_URL))]
print(f"Файлов: {len(files)}")
for i, name in enumerate(files):
    try:
        raw = fetch(f"https://raw.githubusercontent.com/wiedehopf/tar1090-db/master/db/{name}.js")
        data = json.loads(gzip.decompress(raw))
        rows = []
        for icao, fields in data.items():
            if not isinstance(fields, list): continue
            icao = icao.upper().ljust(6)[:6]
            reg  = str(fields[0])[:16] if len(fields) > 0 and fields[0] else None
            tc   = str(fields[1])[:8]  if len(fields) > 1 and fields[1] else None
            desc = str(fields[3])[:64] if len(fields) > 3 and fields[3] else None
            if tc: rows.append((icao, reg, tc, desc))
        upsert(cur, rows)
        conn.commit()
        print(f"  [{i+1}/{len(files)}] {name}.js → {len(rows)}")
    except Exception as e:
        print(f"  SKIP {name}: {e}")

# ── 2. OpenSky Network ─────────────────────────────────────────────────────────
print("=== OpenSky Network ===")
raw = fetch("https://opensky-network.org/datasets/metadata/aircraftDatabase.csv")
reader = csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace")))
rows = []
for r in reader:
    icao = r.get("icao24","").upper().strip()
    if not icao: continue
    icao = icao.ljust(6)[:6]
    reg  = str(r.get("registration","") or "")[:16] or None
    tc   = str(r.get("typecode","") or "")[:8] or None
    desc = str(r.get("model","") or r.get("manufacturername","") or "")[:64] or None
    if tc: rows.append((icao, reg, tc, desc))
upsert(cur, rows)
conn.commit()
print(f"OpenSky: {len(rows)} бортов с типом")

# ── Итог ───────────────────────────────────────────────────────────────────────
cur.execute("SELECT COUNT(*) FROM aircraft WHERE type_code IS NOT NULL")
print(f"\nВсего с типом в БД: {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM aircraft WHERE last_callsign IS NOT NULL AND type_code IS NOT NULL")
print(f"Бортов Pi с типом: {cur.fetchone()[0]}")
cur.close(); conn.close()
print("Done.")
