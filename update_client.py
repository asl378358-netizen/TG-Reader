"""Updates from the owner's GitHub, with immutable versions and rollback.

No tokens are saved here. Existing gh/Git credentials are read noninteractively.
"""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import zipfile

REPOSITORY = 'asl378358-netizen/TG-Reader'
CHANNEL = 'stable'
MAX_ARCHIVE = 32 * 1024 * 1024


class UpdateError(RuntimeError):
    pass


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def save_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def read_current(root):
    record = json.loads((Path(root) / 'current.json').read_text(encoding='utf-8-sig'))
    if not re.fullmatch(r'(?:[0-9a-f]{40}|bundled-[0-9a-f]{16})', record.get('sha', '')):
        raise UpdateError('Некорректная запись установленной версии.')
    path = Path(root) / 'versions' / record['sha']
    if not (path / 'app.py').is_file():
        raise UpdateError('Файлы установленной версии не найдены. Запустите установку ещё раз.')
    return record, path


def child_environment():
    env = os.environ.copy()
    env.update(OPENTELE_NO_FETCH='1', PYTHONIOENCODING='utf-8',
               GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never', GH_PROMPT_DISABLED='1')
    # gh and pip do not consistently read the Windows system HTTP proxy.
    proxies = urllib.request.getproxies()
    for kind in ('https', 'http'):
        value = proxies.get(kind)
        if value and not env.get(kind.upper() + '_PROXY'):
            env[kind.upper() + '_PROXY'] = value
    return env


def run_capture(arguments, *, timeout=20, input=None):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    return subprocess.run(arguments, input=input, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=timeout, creationflags=flags,
        env=child_environment())


def existing_token():
    for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
        if os.environ.get(name):
            return os.environ[name].strip()
    gh = shutil.which('gh')
    if gh:
        try:
            result = run_capture([gh, 'auth', 'token', '--hostname', 'github.com'])
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    git = shutil.which('git')
    if git:
        try:
            result = run_capture([git, '-c', 'credential.interactive=never', 'credential', 'fill'],
                input='protocol=https\nhost=github.com\npath=' + REPOSITORY + '\n\n')
            values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
            if result.returncode == 0 and values.get('password'):
                return values['password']
        except (OSError, subprocess.SubprocessError):
            pass
    return None


class GitHubClient:
    def __init__(self):
        self.token = None
        self.auth_tried = False

    def request(self, resource, *, binary=False):
        url = 'https://api.github.com/repos/' + REPOSITORY + '/' + resource
        headers = {'User-Agent': 'TG-Reader/0.3', 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        request = urllib.request.Request(url, headers=headers)
        if self.token:
            request.add_unredirected_header('Authorization', 'Bearer ' + self.token)
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read(MAX_ARCHIVE + 1)
            if len(raw) > MAX_ARCHIVE:
                raise UpdateError('Обновление превышает допустимый размер.')
            return raw if binary else json.loads(raw)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (401, 403, 404) and not self.auth_tried:
                self.auth_tried = True
                self.token = existing_token()
                if self.token:
                    return self.request(resource, binary=binary)
            if error.code in (401, 403, 404):
                raise UpdateError('GitHub не дал доступ к обновлению. Репозиторий приватный; '
                    'сохранённый доступ GitHub на этом компьютере не найден или не подходит. '
                    'Установленная версия продолжает работать.') from None
            raise UpdateError(f'GitHub временно недоступен (HTTP {error.code}).') from None
        except (OSError, ValueError) as error:
            raise UpdateError('Не удалось получить обновление. Проверьте подключение и системный прокси. '
                              'Установленная версия продолжает работать.') from None


def safe_name(name):
    path = PurePosixPath(name)
    if not name or '\\' in name or ':' in name or path.is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
        raise UpdateError('Недопустимый путь в пакете обновления.')
    return path


def unpack_archive(data, destination):
    from io import BytesIO
    destination = Path(destination)
    names = set()
    with zipfile.ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 1500 or sum(entry.file_size for entry in entries) > MAX_ARCHIVE:
            raise UpdateError('Пакет обновления слишком большой.')
        roots = set()
        for entry in entries:
            name = entry.filename.rstrip('/')
            path = safe_name(name)
            roots.add(path.parts[0])
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise UpdateError('Ссылки в пакете обновления не допускаются.')
            key = name.casefold()
            if key in names:
                raise UpdateError('Повторяющиеся пути в пакете обновления.')
            names.add(key)
        if len(roots) != 1:
            raise UpdateError('Некорректная структура пакета обновления.')
        for entry in entries:
            if entry.is_dir():
                continue
            relative = PurePosixPath(entry.filename).parts[1:]
            if not relative:
                raise UpdateError('Некорректный файл в корне архива.')
            target = destination.joinpath(*relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))


def verify_package(path):
    path = Path(path)
    manifest = json.loads((path / 'update-manifest.json').read_text(encoding='utf-8-sig'))
    if manifest.get('schema') != 1 or not isinstance(manifest.get('files'), dict):
        raise UpdateError('Неизвестный формат обновления.')
    required = {'app.py', 'launcher.py', 'winlaunch.py', 'update_client.py', 'requirements.txt'}
    if not required.issubset(manifest['files']):
        raise UpdateError('Обновление неполное.')
    keys = set()
    for name, value in manifest['files'].items():
        safe_name(name)
        if name.casefold() in keys:
            raise UpdateError('Пути манифеста различаются только регистром.')
        keys.add(name.casefold())
        raw = (path / name).read_bytes()
        if len(raw) != value['bytes'] or hashlib.sha256(raw).hexdigest() != value['sha256']:
            raise UpdateError('Проверка целостности обновления не прошла.')
    manifest['requirements_sha256'] = manifest['files']['requirements.txt']['sha256']
    return manifest


@contextlib.contextmanager
def update_lock(root):
    path = Path(root) / 'update.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as file:
        file.seek(0)
        if not file.read(1):
            file.write(b'0'); file.flush()
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UpdateError('Обновление уже выполняется в другом запуске.') from None
        try:
            yield
        finally:
            file.seek(0)
            if os.name == 'nt':
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def verify_runtime(source, python):
    code = """import os,sys,importlib.metadata
os.environ['OPENTELE_NO_FETCH']='1'
sys.path.insert(0,sys.argv[1])
from pip._vendor.packaging.requirements import Requirement
from pathlib import Path
for line in (Path(sys.argv[1])/'requirements.txt').read_text().splitlines():
    if not line.strip() or line.startswith('#'): continue
    r=Requirement(line)
    if r.marker and not r.marker.evaluate(): continue
    if importlib.metadata.version(r.name) not in r.specifier: raise RuntimeError('Dependency version mismatch: '+r.name)
import tkinter,telethon,opentele2,av,PIL,faster_whisper,python_socks
import tgreader.desktop,tgreader.credentials,tgreader.setup_flow,update_client,winlaunch
compile((Path(sys.argv[1])/'launcher.py').read_text(encoding='utf-8'), 'launcher.py', 'exec')
"""
    result = run_capture([str(python), '-c', code, str(source)], timeout=120)
    if result.returncode:
        raise UpdateError('Проверка новой версии не прошла. Сохранена предыдущая рабочая версия.')


def prepare_python(root, source, manifest, current, report):
    old_python = Path(current['python'])
    if manifest['requirements_sha256'] == current.get('requirements_sha256'):
        return old_python
    env = Path(root) / 'envs' / manifest['requirements_sha256'][:20]
    python = env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    report('Обновление зависимостей программы. Используется уже установленный Python…')
    if not python.is_file():
        base = run_capture([str(old_python), '-c', 'import sys; print(sys._base_executable)'])
        if base.returncode:
            raise UpdateError('Не удалось найти существующий Python для обновления.')
        result = run_capture([base.stdout.strip(), '-m', 'venv', str(env)], timeout=120)
        if result.returncode:
            raise UpdateError('Не удалось подготовить окружение обновления; старая версия сохранена.')
    result = run_capture([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                          '-r', str(Path(source) / 'requirements.txt')], timeout=900)
    if result.returncode:
        raise UpdateError('Зависимости обновления не установились; старая версия сохранена.')
    return python


def activate(root, record, source):
    root = Path(root)
    pointer = root / 'current.json'
    launcher = root / 'start.py'
    original_pointer = pointer.read_bytes()
    original_launcher = launcher.read_bytes() if launcher.exists() else None
    try:
        atomic_write(launcher, (Path(source) / 'launcher.py').read_bytes())
        save_json(pointer, record)
    except BaseException:
        atomic_write(pointer, original_pointer)
        if original_launcher is None:
            launcher.unlink(missing_ok=True)
        else:
            atomic_write(launcher, original_launcher)
        raise


def check_update(root, report=lambda message: None, *, client=None, validator=verify_runtime):
    root = Path(root)
    with update_lock(root):
        current, old_source = read_current(root)
        report('Проверяю обновления…')
        client = client or GitHubClient()
        head = client.request('commits/' + CHANNEL)
        sha = head.get('sha', '')
        if not re.fullmatch('[0-9a-f]{40}', sha):
            raise UpdateError('GitHub вернул некорректный номер версии.')
        if sha == current['sha']:
            report('Установлена актуальная версия ' + current.get('version', '') + '.')
            return current, old_source, False
        report('Скачиваю обновление…')
        target = root / 'versions' / sha
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='update_', dir=root) as temp:
            stage = Path(temp) / 'package'
            unpack_archive(client.request('zipball/' + sha, binary=True), stage)
            manifest = verify_package(stage)
            python = prepare_python(root, stage, manifest, current, report)
            report('Проверяю новую версию…')
            validator(stage, python)
            if target.exists():
                # Reuse only an independently verified immutable package.
                verify_package(target)
            else:
                os.replace(stage, target)
            record = {'sha': sha, 'version': manifest['version'], 'python': str(python),
                      'requirements_sha256': manifest['requirements_sha256'],
                      'previous': current['sha']}
            activate(root, record, target)
        report('Обновление установлено: ' + record['version'] + '.')
        return record, target, True
