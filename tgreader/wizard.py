import json
import re
import tkinter as tk
from pathlib import Path
from tkinter import filedialog,messagebox,ttk
from telethon import TelegramClient,utils
from telethon.sessions import StringSession
from .common import atomic_json,state_dir,save_session,read_session
from .network import client_network_options
from .credentials import read_password
from .setup_flow import setup_dialogs
from .dialog_picker import choose_dialogs
from .scope import media_options


def chat_records(dialogs,previous=()):
    saved={chat['chat_id']:chat for chat in previous}
    return [{**saved.get(dialog.id,{}),'chat_id':dialog.id,'title':dialog.name,
             'peer_type':'channel' if hasattr(dialog.entity,'access_hash') else 'chat',
             'peer_id':dialog.entity.id,'access_hash':getattr(dialog.entity,'access_hash',None),
             'forum':bool(getattr(dialog.entity,'forum',False)),
             'media':media_options(saved.get(dialog.id,{}))} for dialog in dialogs]


async def select_groups():
    from .auth import create_client,persist_client,verify_account
    from .common import load_config
    cfg=load_config();client=create_client(cfg);verified=False
    print('Используем сохранённый вход Telegram. Получаем список групп для поиска...',flush=True)
    try:
        await client.connect();await verify_account(client,cfg);verified=True
        dialogs=await setup_dialogs(client,rpc_waits_handled=True)
        selected=choose_dialogs(dialogs,cfg['chats'],cfg['timezone'])
        cfg['chats']=chat_records(selected,cfg['chats'])
        atomic_json(state_dir()/'config.json',cfg)
        print(f'Список групп сохранён: {len(selected)}. Вход и папка выгрузок сохранены.',flush=True)
    finally:
        try:
            if verified:persist_client(client,cfg)
        finally:await client.disconnect()

async def setup():
    state=state_dir();p=state/'config.json';old=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
    print('\n1. Получите API ID и API hash своего приложения на https://my.telegram.org/apps')
    print('Это ключ Telegram, не ключ платной нейросети. Вводите код входа только в этом окне.\n')
    api_id=old.get('api_id') or int(input('API ID: ').strip())
    api_hash=old.get('api_hash') or read_password('API hash Telegram','Введите API hash своего приложения Telegram.').strip()
    if not re.fullmatch(r'[0-9a-fA-F]{32}',api_hash):raise RuntimeError('API hash должен содержать 32 шестнадцатеричных символа.')
    saved=read_session() if (state/'account.session.enc').exists() else ''
    client=TelegramClient(StringSession(saved),api_id,api_hash,device_model='Telegram Daily Reader',app_version='0.1.0',**client_network_options())
    try:
        await client.start(phone=lambda:input('Номер телефона с кодом страны: ').strip(),
               code_callback=lambda:read_password('Код входа Telegram','Введите код входа, полученный в Telegram.').strip(),
               password=lambda:read_password('Пароль двухэтапной проверки Telegram','Введите облачный пароль двухэтапной проверки этого аккаунта.'))
        save_session(client.session.save())
        dialogs=await setup_dialogs(client)
        choices=choose_dialogs(dialogs,old.get('chats',[]),old.get('timezone','Europe/Berlin'))
        root=tk.Tk();root.withdraw()
        folder=filedialog.askdirectory(title='Выберите синхронизируемую папку Google Диска')
        root.destroy()
        if not folder:raise RuntimeError('Папка выгрузок не выбрана; настройка не завершена.')
        from .scope_settings import configure_scope,output_directory
        output=output_directory(folder)
        output.mkdir(parents=True,exist_ok=True)
        chats=chat_records(choices,old.get('chats',[]))
        me=await client.get_me()
        cfg={**old,'auth_mode':'api','account_user_id':me.id,'api_id':api_id,'api_hash':api_hash,'chats':chats,'output_dir':str(output),
             'timezone':old.get('timezone','Europe/Berlin'),'bootstrap_days':old.get('bootstrap_days',3),
             'publish_days':7,'edit_refresh_hours':48,'max_media_jobs_per_run':100,
             'whisper_model':old.get('whisper_model','small'),'speech_language':old.get('speech_language','ru')}
        cfg.pop('auth_file',None)
        cfg=configure_scope(cfg)
        atomic_json(p,cfg)
        print('\nНастройка сохранена. Выбрано групп:',len(chats))
        print('Папка материалов:',output)
        print('Теперь запустите 2-collect-now.cmd. После успешной проверки — 3-enable-autostart.cmd.')
    finally:
        await client.disconnect()
