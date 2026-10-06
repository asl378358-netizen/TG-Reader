import contextlib
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc

def now():
    return dt.datetime.now(UTC)

def iso(value):
    return value.astimezone(UTC).isoformat() if value else None

def state_dir():
    if os.name != 'nt':
        # The Linux path exists for offline tests, not for Windows credentials.
        base = Path(os.environ.get('TG_READER_TEST_STATE', Path.cwd() / '.test_state'))
    else:
        base = Path(os.environ['LOCALAPPDATA']) / 'TelegramDailyReader'
    base.mkdir(parents=True, exist_ok=True)
    return base

def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def atomic_json(path, obj):
    atomic_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8'))

def load_config():
    p = state_dir() / 'config.json'
    if not p.exists():
        raise RuntimeError('Сначала запустите 1-desktop-setup.cmd.')
    cfg = json.loads(p.read_text(encoding='utf-8'))
    ZoneInfo(cfg['timezone'])
    if not cfg.get('chats'):
        raise RuntimeError('Не выбраны группы для чтения.')
    return cfg

def protect_session(raw, decrypt=False):
    """Windows DPAPI, bound to the current Windows user; no plaintext session file."""
    if os.name != 'nt':
        raise RuntimeError('Авторизация аккаунта поддерживается только в Windows.')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    func = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    func.restype = wintypes.BOOL
    func.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)

def save_session(value):
    atomic_bytes(state_dir() / 'account.session.enc', protect_session(value.encode()))

def read_session():
    return protect_session((state_dir() / 'account.session.enc').read_bytes(), decrypt=True).decode()

class AlreadyRunning(Exception):
    pass

@contextlib.contextmanager
def process_lock():
    with (state_dir() / 'collector.lock').open('a+b') as f:
        f.seek(0)
        if not f.read(1):
            f.write(b'0'); f.flush()
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as e:
            raise AlreadyRunning() from e
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
