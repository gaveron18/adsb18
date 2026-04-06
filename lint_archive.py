#!/usr/bin/env python3
"""
lint_archive.py — проверки archive.html перед деплоем
Запускать после каждого изменения frontend/archive.html
"""
import re, sys, subprocess, os

HTML = '/home/new/adsb18/frontend/archive.html'
GZ   = HTML + '.gz'
errors = []

# ── 1. Читаем файл ────────────────────────────────────────────────────────────
with open(HTML) as f:
    content = f.read()
lines = content.splitlines()

# ── 2. JS синтаксис ───────────────────────────────────────────────────────────
scripts = re.findall(r'<script[^>]*>(.*?)</script>', content, re.DOTALL)
if len(scripts) < 2:
    errors.append('ERROR: не найден основной script блок')
else:
    js = scripts[1]
    with open('/tmp/_archive_lint.js', 'w') as f:
        f.write(js)
    r = subprocess.run(['node', '--check', '/tmp/_archive_lint.js'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        errors.append(f'ERROR JS синтаксис:\n{r.stderr.strip()}')
    else:
        print('  ✓ JS синтаксис OK')

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
    # Ищем top-level let/const — строки начинающиеся с let/const (не внутри функции)
    # Простая эвристика: строка начинается с "let " или "const " без отступа
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
    errors.append('ERROR: archive.html.gz не существует — запусти: gzip -k -f ' + HTML)
else:
    html_mtime = os.path.getmtime(HTML)
    gz_mtime   = os.path.getmtime(GZ)
    if gz_mtime < html_mtime:
        errors.append('ERROR: archive.html.gz устарел — запусти: gzip -k -f ' + HTML)
    else:
        print('  ✓ gzip актуален')

# ── 5. HTTP 200 ───────────────────────────────────────────────────────────────
r = subprocess.run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                    'http://localhost:8098/archive.html'],
                   capture_output=True, text=True)
code = r.stdout.strip()
if code == '200':
    print('  ✓ HTTP 200 OK')
else:
    errors.append(f'ERROR: HTTP {code} (ожидался 200)')

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
