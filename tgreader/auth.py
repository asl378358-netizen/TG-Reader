"""Local authentication storage and client construction.

Desktop mode keeps session + API identity in one DPAPI encrypted bundle.
No auth file is ever placed in the output/sync directory.
"""
import json
import re
import uuid

from telethon import TelegramClient
from telethon.sessions import StringSession

from .common import atomic_bytes, protect_session, read_session, save_session, state_dir
from .network import client_network_options


def bundle_path(name):
    if not isinstance(name, str) or not re.fullmatch(r'desktop_[0-9a-f]{32}\.auth\.enc', name):
        raise RuntimeError('Некорректное имя локального файла авторизации.')
    return state_dir() / name


def save_desktop_bundle(client, identity, user_id):
    name = f'desktop_{uuid.uuid4().hex}.auth.enc'
    value = {'schema': 1, 'session': StringSession.save(client.session),
             'identity': identity, 'user_id': user_id}
    raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
    atomic_bytes(bundle_path(name), protect_session(raw))
    return name


def read_bundle(cfg):
    path = bundle_path(cfg['auth_file'])
    value = json.loads(protect_session(path.read_bytes(), decrypt=True))
    if value.get('schema') != 1 or value.get('user_id') != cfg.get('account_user_id'):
        raise RuntimeError('Настройка и сеанс относятся к разным аккаунтам. Повторите настройку.')
    return value


def identity_client(session, identity, *, receive_updates=False, network=None, client_type=TelegramClient):
    client = client_type(session, identity['api_id'], identity['api_hash'],
        device_model=identity['device_model'], system_version=identity['system_version'],
        app_version=identity['app_version'], lang_code=identity['lang_code'],
        system_lang_code=identity['system_lang_code'], receive_updates=receive_updates,
        flood_sleep_threshold=0, request_retries=1, **client_network_options(network))
    # Telethon exposes no constructor argument for the existing client's lang pack.
    client._init_request.lang_pack = identity.get('lang_pack', '')
    return client


def create_client(cfg):
    from .collection_flow import CollectionClient
    if cfg.get('auth_mode') == 'desktop':
        value = read_bundle(cfg)
        return identity_client(StringSession(value['session']), value['identity'], client_type=CollectionClient)
    return CollectionClient(StringSession(read_session()), cfg['api_id'], cfg['api_hash'],
        device_model='Telegram Daily Reader', app_version='0.2.0', receive_updates=False,
        **client_network_options())


def persist_client(client, cfg):
    if cfg.get('auth_mode') != 'desktop':
        save_session(client.session.save())
        return
    value = read_bundle(cfg)
    value['session'] = StringSession.save(client.session)
    raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
    atomic_bytes(bundle_path(cfg['auth_file']), protect_session(raw))


async def verify_account(client, cfg):
    # get_me alone verifies the session and account; is_user_authorized catches
    # RPCError (including FloodWait) and can misreport a wait as a lost session.
    me = await client.get_me()
    if not me:
        raise RuntimeError('Telegram завершил сеанс. Нажмите «Подключить Telegram / выбрать группы».')
    if cfg.get('account_user_id'):
        if me.id != cfg['account_user_id']:
            raise RuntimeError('Выбран другой аккаунт. Сбор остановлен; повторите настройку.')
