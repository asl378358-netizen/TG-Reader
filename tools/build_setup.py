"""Build deterministic integrity manifest and one self-contained Windows setup."""
import base64
import hashlib
import io
import json
import subprocess
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.3.0'
EXCLUDE = {'TG-Reader-Setup.cmd', 'update-manifest.json', 'SOURCES_SHA256.txt'}


def build():
    paths = []
    tracked = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT).decode('utf-8').split('\0')
    for name in sorted(set(tracked)):
        if not name: continue
        path = ROOT / name
        relative = path.relative_to(ROOT)
        if not path.is_file() or any(part in ('.git', '__pycache__', '.test_state') for part in relative.parts): continue
        if str(relative).replace('\\', '/') in EXCLUDE or path.suffix in ('.pyc', '.log', '.enc'): continue
        if path.suffix in ('.ps1', '.cmd'):
            text = path.read_text(encoding='utf-8-sig')
            raw = text.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8-sig' if path.suffix == '.ps1' else 'utf-8')
            path.write_bytes(raw)
        paths.append(path)
    paths.sort(key=lambda path: path.relative_to(ROOT).as_posix())
    files = {path.relative_to(ROOT).as_posix(): {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
             'bytes': path.stat().st_size} for path in paths}
    manifest = {'schema': 1, 'version': VERSION, 'files': files}
    manifest_path = ROOT / 'update-manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in paths + [manifest_path]:
            info = zipfile.ZipInfo('TG-Reader/' + path.relative_to(ROOT).as_posix(), date_time=(2026, 10, 6, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    data = blob.getvalue()
    payload = base64.b64encode(data).decode('ascii')
    bootstrap = (ROOT / 'tools/bootstrap.ps1').read_text(encoding='utf-8-sig')
    bootstrap = bootstrap.replace('__PACKAGE_SHA256__', hashlib.sha256(data).hexdigest())
    header = '''@echo off
setlocal
set "TGR_SETUP_FILE=%~f0"
title TG Reader - setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:TGR_SETUP_FILE,[Text.Encoding]::UTF8);$a=$t.LastIndexOf('# BEGIN POWERSHELL');$b=$t.LastIndexOf('# END POWERSHELL / PACKAGE');Invoke-Expression $t.Substring($a,$b-$a)"
set "TGR_RESULT=%errorlevel%"
if not "%TGR_RESULT%"=="0" pause
exit /b %TGR_RESULT%
# BEGIN POWERSHELL
'''
    contents = header + bootstrap + '\n# END POWERSHELL / PACKAGE\n' + '\n'.join(payload[n:n+120] for n in range(0, len(payload), 120)) + '\n'
    setup = ROOT / 'TG-Reader-Setup.cmd'
    setup.write_bytes(contents.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8'))
    print(f'Built {VERSION}: {len(files)} verified files; one-file setup {setup.stat().st_size:,} bytes')


if __name__ == '__main__': build()
