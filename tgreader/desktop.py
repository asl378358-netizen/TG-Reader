"""Bootstrap from the owner's local Telegram Desktop, then create a separate session.

opentele2 is used for parsing tdata, not random device generation. The original
Desktop session is only held in memory, is never saved, and is never logged out.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog

from telethon import functions
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import MemorySession, StringSession

from .auth import identity_client, save_desktop_bundle
from .common import atomic_json, state_dir
from .wizard import choose_dialogs
from .network import client_network_options, connection_failed, detect_route
from .credentials import read_password
from .setup_flow import setup_dialogs, setup_rpc


def choose_account(previews):
    if len(previews) == 1:
        return 0
    root = tk.Tk(); root.title('TG Reader — аккаунт Telegram'); root.geometry('680x330')
    from tkinter import ttk
    ttk.Label(root, text='Выберите аккаунт, в котором доступны нужные группы.',
              wraplength=630).pack(anchor='w', padx=20, pady=15)
    value = tk.IntVar(root, value=0)
    for index, me in enumerate(previews):
        name = ' '.join(x for x in (me.first_name, me.last_name) if x) or str(me.id)
        tail = str(getattr(me, 'phone', '') or '')[-4:]
        ttk.Radiobutton(root, text=f'{name}; номер заканчивается на {tail}',
                        variable=value, value=index).pack(anchor='w', padx=20, pady=8)
    answer = []
    def finish():
        answer.append(value.get()); root.destroy()
    ttk.Button(root, text='Продолжить', command=finish).pack(pady=20)
    root.mainloop()
    if not answer:
        raise RuntimeError('Выбор аккаунта отменён.')
    return answer[0]


def desktop_dependencies():
    # Disable unrelated remote version lookups on opentele2 import.
    os.environ['OPENTELE_NO_FETCH'] = '1'
    from opentele2.api import API, UseCurrentSession
    from opentele2.td import TDesktop
    return API, UseCurrentSession, TDesktop


def choose_tdata():
    default = Path(os.environ.get('APPDATA', '')) / 'Telegram Desktop' / 'tdata'
    root = tk.Tk(); root.withdraw()
    try:
        folder = filedialog.askdirectory(
            title='Выберите локальную папку tdata своего Telegram Desktop',
            initialdir=str(default if default.is_dir() else Path.home()))
    finally:
        root.destroy()
    if not folder:
        raise RuntimeError('Настройка отменена; локальная сессия не прочитана.')
    result = Path(folder).resolve()
    if not (result / 'key_datas').is_file() and not (result / 'key_data').is_file():
        raise RuntimeError('Выберите именно папку tdata. В ней должен быть файл key_datas или key_data.')
    return result


async def connect_account(account, use_current, network=None):
    network = network if network is not None else detect_route()
    from opentele2.exception import OpenTeleException
    try:
        client = await account.ToTelethon(session=MemorySession(), flag=use_current,
            receive_updates=False, auto_post_login=False, flood_sleep_threshold=0,
            request_retries=1, **client_network_options(network))
    except OpenTeleException as exc:
        raise RuntimeError('Не удалось преобразовать Desktop-сеанс. Тип ошибки: '+type(exc).__name__) from None
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError('Desktop-сеанс завершён. Войдите в официальный Telegram Desktop заново.')
        me = await client.get_me()
        return client, me
    except (ConnectionError, OSError) as error:
        await client.disconnect()
        raise connection_failed(client, network, error) from error
    except BaseException:
        await client.disconnect()
        raise


def client_identity(api):
    return {name: getattr(api, name) for name in
        ('api_id', 'api_hash', 'device_model', 'system_version', 'app_version',
         'lang_code', 'system_lang_code', 'lang_pack')}


async def new_reader_session(bootstrap, identity, password_callback, network=None):
    client = identity_client(StringSession(), identity, receive_updates=True, network=network)
    client.session.set_dc(bootstrap.session.dc_id, bootstrap.session.server_address,
                          bootstrap.session.port)
    waiter = None
    try:
        await client.connect()
        qr = await client.qr_login()
        # Register the update listener BEFORE accepting the token. Otherwise a
        # fast acceptance may be missed by QRLogin.wait(). Do not print the token.
        waiter = asyncio.create_task(qr.wait(timeout=45))
        await asyncio.sleep(0)
        try:
            await bootstrap(functions.auth.AcceptLoginTokenRequest(qr.token))
            await waiter
        except SessionPasswordNeededError:
            error_message = None
            for attempt in range(3):
                password = password_callback(error=error_message)
                try:
                    await setup_rpc(lambda: client.sign_in(password=password), 'проверка пароля')
                    break
                except PasswordHashInvalidError:
                    if attempt == 2:
                        raise RuntimeError('Telegram отклонил пароль трижды. '
                                           'Проверьте пароль двухэтапной проверки выбранного аккаунта.') from None
                    error_message = ('Telegram отклонил предыдущий пароль. '
                                     'Проверьте видимый текст, раскладку и выбранный аккаунт.')
                    print(error_message, flush=True)
                finally:
                    password = None
        if not await client.is_user_authorized():
            raise RuntimeError('Telegram не подтвердил отдельный сеанс сборщика.')
        return client
    except BaseException:
        # This client is the NEW session only; never log out the Desktop session.
        try:
            with contextlib.suppress(Exception):
                if await client.is_user_authorized():
                    await client.log_out()
        finally:
            await client.disconnect()
        raise
    finally:
        if waiter and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


async def setup_desktop():
    state = state_dir(); config_path = state / 'config.json'
    old = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    print('\nВход через установленный официальный Telegram Desktop. Собственные API-ключи не нужны.')
    print('Сначала полностью закройте Telegram Desktop: значок у часов → Выход.')
    print('Программа прочитает локальный tdata и создаст отдельный сеанс для сборщика.')
    print('Пароли и сеансы остаются на этом компьютере. tdata на Google Диск не копируется.\n')
    network = detect_route()
    print('Маршрут Telegram:', network.description, flush=True)
    path = choose_tdata()
    API, use_current, TDesktop = desktop_dependencies()
    from opentele2.exception import OpenTeleException
    local_passcode = read_password('Локальный код-пароль Telegram Desktop',
        'Введите код-пароль, которым разблокируется Telegram Desktop на этом компьютере. '
        'Если код-пароль не установлен, оставьте поле пустым и нажмите «Продолжить».',
        allow_empty=True)
    try:
        desktop = TDesktop(str(path), api=API.TelegramDesktop, passcode=local_passcode or None)
    except (Exception, OpenTeleException) as exc:
        raise RuntimeError('Не удалось прочитать tdata. Проверьте локальный код-пароль и папку. '
                           'Возможна несовместимость версии Desktop. Тип ошибки: '+type(exc).__name__) from None
    finally:
        local_passcode = None
    if not desktop.isLoaded() or not desktop.accounts:
        raise RuntimeError('В tdata не найден авторизованный аккаунт.')

    accounts = desktop.accounts
    bootstrap = None; client = None; committed = False
    try:
        previews = []
        for index, account in enumerate(accounts):
            preview, me = await connect_account(account, use_current, network)
            try:
                name = ' '.join(x for x in (me.first_name, me.last_name) if x) or str(me.id)
                phone_tail = str(getattr(me, 'phone', '') or '')[-4:]
                print(f'{index + 1}. {name}; номер заканчивается на {phone_tail}; ID {me.id}')
                previews.append(me)
            finally:
                await preview.disconnect()
        index = choose_account(previews)
        if index < 0 or index >= len(accounts):
            raise RuntimeError('Такого аккаунта нет в списке; настройка отменена.')
        bootstrap, me = await connect_account(accounts[index], use_current, network)
        identity = client_identity(API.TelegramDesktop)
        identity['device_model'] = 'Telegram Daily Reader (Windows)'
        print('\nСоздаём отдельный сеанс. Telegram может прислать уведомление о новом входе.')
        name = ' '.join(x for x in (me.first_name, me.last_name) if x) or str(me.id)
        tail = str(getattr(me, 'phone', '') or '')[-4:]
        client = await new_reader_session(bootstrap, identity,
            lambda **options: read_password('Пароль двухэтапной проверки Telegram',
                f'Аккаунт: {name}; номер заканчивается на {tail}.\n'
                'Введите облачный пароль из настроек Telegram → Конфиденциальность → '
                'Двухэтапная проверка. Поле показывает введённый текст.', **options), network)
        connected_me = await setup_rpc(client.get_me, 'проверка выбранного аккаунта')
        if connected_me.id != me.id:
            raise RuntimeError('Telegram вернул другой аккаунт; настройка остановлена.')
        await bootstrap.disconnect(); bootstrap = None

        dialogs = await setup_dialogs(client)
        previous = old.get('chats', []) if old.get('account_user_id') == me.id else []
        choices = choose_dialogs(dialogs, previous)
        root = tk.Tk(); root.withdraw()
        try:
            folder = filedialog.askdirectory(title='Выберите синхронизируемую папку Google Диска')
        finally:
            root.destroy()
        if not folder:
            raise RuntimeError('Папка выгрузок не выбрана; настройка отменена.')
        output = Path(folder).resolve() / 'TelegramDailyReader'
        if output.is_relative_to(state.resolve()):
            raise RuntimeError('Выберите папку Google Диска отдельно от данных программы.')
        output.mkdir(parents=True, exist_ok=True)
        chats = []
        for dialog in choices:
            entity = dialog.entity
            chats.append({'chat_id': dialog.id, 'title': dialog.name,
                'peer_type': 'channel' if hasattr(entity, 'access_hash') else 'chat',
                'peer_id': entity.id, 'access_hash': getattr(entity, 'access_hash', None),
                'forum': bool(getattr(entity, 'forum', False))})
        auth_file = save_desktop_bundle(client, identity, me.id)
        cfg = {**old, 'auth_mode': 'desktop', 'auth_file': auth_file, 'account_user_id': me.id,
            'chats': chats, 'output_dir': str(output), 'timezone': old.get('timezone', 'Europe/Berlin'),
            'bootstrap_days': old.get('bootstrap_days', 3), 'publish_days': 7,
            'edit_refresh_hours': 48, 'max_media_jobs_per_run': 100,
            'whisper_model': old.get('whisper_model', 'small'), 'speech_language': 'ru'}
        cfg.pop('api_id', None); cfg.pop('api_hash', None)
        atomic_json(config_path, cfg); committed = True
        print('\nНастройка сохранена. Выбрано групп:', len(chats))
        print('Теперь нажмите «Собрать сейчас». Автосбор включайте после проверки выгрузки.')
    finally:
        if bootstrap is not None:
            await bootstrap.disconnect()
        if client is not None:
            try:
                if not committed and await client.is_user_authorized():
                    await client.log_out()
            finally:
                await client.disconnect()
