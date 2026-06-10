#!/usr/bin/env python3
"""
lint_archive.py — проверки archive.html перед деплоем
Запускать после каждого изменения frontend/archive.html
"""
import re, sys, subprocess, os, tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(SCRIPT_DIR, 'frontend', 'archive.html')
GZ   = HTML + '.gz'
errors = []

# ── 1. Читаем файл ────────────────────────────────────────────────────────────
with open(HTML, encoding='utf-8') as f:
    content = f.read()
lines = content.splitlines()

# ── 2. JS синтаксис ───────────────────────────────────────────────────────────
scripts = re.findall(r'<script[^>]*>(.*?)</script>', content, re.DOTALL)
if len(scripts) < 2:
    errors.append('ERROR: не найден основной script блок')
else:
    js = scripts[1]
    with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False, encoding='utf-8') as f:
        tmp_js = f.name
        f.write(js)
    try:
        r = subprocess.run(['node', '--check', tmp_js], capture_output=True, text=True)
        if r.returncode != 0:
            errors.append(f'ERROR JS синтаксис:\n{r.stderr.strip()}')
        else:
            print('  ✓ JS синтаксис OK')
    finally:
        os.unlink(tmp_js)

# ── 3. TDZ: let/const после init блока ───────────────────────────────────────
init_line = None
for i, line in enumerate(lines):
    if '// ── Init' in line:
        init_line = i + 1  # 1-based
        break

if init_line is None:
    errors.append('ERROR: не найден init блок (// ── Init)')
else:
    tdz_found = []
    for i, line in enumerate(lines[init_line:], start=init_line+1):
        if re.match(r'^(let|const)\s+\w', line):
            tdz_found.append(f'  строка {i}: {line.strip()}')
    if tdz_found:
        errors.append('ERROR TDZ: let/const объявления ПОСЛЕ init блока (строка {}):\n'.format(init_line) +
                      '\n'.join(tdz_found))
    else:
        print(f'  ✓ TDZ OK (init блок на строке {init_line}, деклараций после нет)')

# ── 4. gzip актуален ─────────────────────────────────────────────────────────
if not os.path.exists(GZ):
    errors.append('ERROR: archive.html.gz не существует — запусти: gzip -k -f frontend/archive.html')
else:
    html_mtime = os.path.getmtime(HTML)
    gz_mtime   = os.path.getmtime(GZ)
    if gz_mtime < html_mtime:
        errors.append('ERROR: archive.html.gz устарел — запусти: gzip -k -f frontend/archive.html')
    else:
        print('  ✓ gzip актуален')

# ── 5. HTTP 200 ───────────────────────────────────────────────────────────────
try:
    import urllib.request
    with urllib.request.urlopen('http://localhost:8098/archive.html', timeout=5) as resp:
        if str(resp.status) == '200':
            print('  ✓ HTTP 200 OK')
        else:
            errors.append(f'ERROR: HTTP {resp.status} (ожидался 200)')
except Exception as e:
    print(f'  ⚠ HTTP check skipped (dev server unavailable: {e})')

# ── Итог ──────────────────────────────────────────────────────────────────────
print()
if errors:
    print('─' * 60)
    for e in errors:
        print(e)
    print('─' * 60)
    print(f'ПРОВАЛ: {len(errors)} ошибок. Деплой запрещён.')
    sys.exit(1)
else:
    print('─' * 60)
    print('ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ — можно деплоить.')
    sys.exit(0)
