"""Update only the installed reader code using its existing virtual environment."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from tgreader.common import AlreadyRunning, atomic_bytes, process_lock, state_dir

FILES = ('tgreader/network.py', 'tgreader/auth.py', 'tgreader/desktop.py',
         'tgreader/wizard.py', 'tgreader/credentials.py', 'tgreader/setup_flow.py',
         'app.py', 'requirements.txt', 'README.html', 'AGENT_README.txt')


def restore_program(app, originals):
    for name, raw in originals.items():
        target = app / name
        if raw is None:
            target.unlink(missing_ok=True)
        else:
            atomic_bytes(target, raw)


def apply_program_update(source, app, backup, verify):
    incoming = {name: (source / name).read_bytes() for name in FILES}
    originals = {name: (app / name).read_bytes() if (app / name).is_file() else None for name in FILES}
    backup.mkdir(parents=True)
    for name, raw in originals.items():
        if raw is not None:
            atomic_bytes(backup / name, raw)
    atomic_bytes(backup / 'files.json', json.dumps({name: raw is not None for name, raw in originals.items()}).encode())
    try:
        for name, raw in incoming.items():
            atomic_bytes(app / name, raw)
        verify()
    except BaseException:
        restore_program(app, originals)
        raise


def main():
    root = state_dir()
    login_fix = '--login-fix' in sys.argv[1:]
    log_path = root / ('update-login.log' if login_fix else 'update-network.log')
    with log_path.open('w', encoding='utf-8-sig') as log:
        def output(value):
            print(value, flush=True)
            log.write(value + '\n'); log.flush()
        def run(arguments, check=True):
            environment = os.environ.copy(); environment['OPENTELE_NO_FETCH'] = '1'
            environment['PYTHONIOENCODING'] = 'utf-8'
            process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace', env=environment)
            for line in process.stdout:
                output(line.rstrip('\r\n'))
            code = process.wait()
            if check and code:
                raise RuntimeError('Команда завершилась с кодом ' + str(code))
            return code
        try:
            if os.name != 'nt':
                raise RuntimeError('Обновление предназначено для установленного сборщика на Windows.')
            if os.path.normcase(str(Path(sys.prefix).resolve())) != os.path.normcase(str((root / 'venv').resolve())):
                raise RuntimeError('Запустите 0-update-login.cmd или 0-update-network.cmd: нужен существующий Python окружения сборщика.')
            app = root / 'app'
            if not (app / 'app.py').is_file():
                raise RuntimeError('Установленный сборщик не найден. Сначала выполните 0-install.cmd.')
            output('Используется существующий Python: ' + sys.executable)
            with process_lock():
                source = Path(__file__).resolve().parent
                wheel = source / 'vendor' / 'python_socks-2.8.2-py3-none-any.whl'
                if hashlib.sha256(wheel.read_bytes()).hexdigest() != '7cf785d0631e0659384a773b3c402bc22cccdc23894ba1d65f8524748ace1193':
                    raise RuntimeError('Проверка файла библиотеки прокси не прошла. Распакуйте исправленный архив целиком.')
                bundled = [str(wheel)]
                if sys.version_info < (3, 11):
                    timeout_wheel = source / 'vendor' / 'async_timeout-5.0.1-py3-none-any.whl'
                    if hashlib.sha256(timeout_wheel.read_bytes()).hexdigest() != '39e3809566ff85354557ec2398b55e096c8364bacac9405a7a1fa429e77fe76c':
                        raise RuntimeError('Проверка библиотеки таймаутов не прошла. Распакуйте архив целиком.')
                    bundled.append(str(timeout_wheel))
                output('Установка небольшой библиотеки прокси из архива; загрузка из интернета не требуется...')
                run([sys.executable, '-m', 'pip', 'install', '--no-index', '--disable-pip-version-check'] + bundled)
                backup = root / 'backups' / ('network_' + dt.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
                output('Обновление программы; предыдущий код сохранится: ' + str(backup))
                check = "import os; os.environ['OPENTELE_NO_FETCH']='1'; import tkinter,telethon,opentele2,python_socks; from python_socks.async_.asyncio import Proxy; import tgreader.network,tgreader.auth,tgreader.desktop,tgreader.credentials,tgreader.setup_flow; print('Login and network libraries: OK')"
                apply_program_update(source, app, backup,
                    lambda: run([sys.executable, '-c', 'import sys; sys.path.insert(0, ' + repr(str(app)) + '); ' + check]))
            output('Обновление установлено. Пароли вводятся в видимом поле; паузы Telegram обрабатываются автоматически.')
            output('Текущий сетевой маршрут:')
            run([sys.executable, str(app / 'app.py'), 'network-status'], check=False)
            output('Далее: полностью закройте Telegram Desktop и запустите 1-desktop-setup.cmd.')
            output('Журнал обновления: ' + str(log_path))
            return 0
        except AlreadyRunning:
            output('Сборщик сейчас работает. Дождитесь завершения и повторите обновление.')
            return 1
        except Exception as error:
            output('Ошибка обновления: ' + type(error).__name__ + ': ' + str(error))
            output('Журнал: ' + str(log_path))
            return 1


if __name__ == '__main__':
    sys.exit(main())
