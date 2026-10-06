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

def choose_dialogs(dialogs,previous):
    root=tk.Tk();root.title('Telegram Daily Reader — выбор групп');root.geometry('850x620')
    ttk.Label(root,text='Выберите группы и каналы. Для первой проверки оставьте одну группу.',wraplength=790).pack(pady=12)
    selected=set(c['chat_id'] for c in previous) or {-1003933265463}
    search=tk.StringVar();entry=ttk.Entry(root,textvariable=search);entry.pack(fill='x',padx=15)
    tree=ttk.Treeview(root,columns=('selected','name'),show='headings',selectmode='browse')
    tree.heading('selected',text='Выбрано');tree.heading('name',text='Группа / канал');tree.column('selected',width=80,stretch=False);tree.column('name',width=710)
    tree.pack(fill='both',expand=True,padx=15,pady=10)
    rows={d.id:d for d in dialogs};mapping={};answer=[]
    def redraw(*_):
        tree.delete(*tree.get_children());mapping.clear()
        for d in dialogs:
            if search.get().casefold() in d.name.casefold():
                iid=tree.insert('','end',values=('✓' if d.id in selected else '',d.name));mapping[iid]=d.id
    def toggle(event=None):
        iid=tree.identify_row(event.y) if event and event.type==tk.EventType.ButtonRelease else tree.focus()
        if iid not in mapping:return
        cid=mapping[iid]
        if cid in selected:selected.remove(cid)
        else:selected.add(cid)
        tree.item(iid,values=('✓' if cid in selected else '',rows[cid].name))
    def finish():
        actual=[rows[cid] for cid in selected if cid in rows]
        if not actual:messagebox.showerror('Нет выбора','Выберите хотя бы одну группу.');return
        answer.extend(actual);root.destroy()
    tree.bind('<ButtonRelease-1>',toggle);tree.bind('<space>',toggle)
    search.trace_add('write',redraw)
    ttk.Button(root,text='Продолжить',command=finish).pack(pady=12)
    redraw();root.mainloop()
    if not answer:raise RuntimeError('Настройка отменена; выбор групп не сохранен.')
    return answer

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
        choices=choose_dialogs(dialogs,old.get('chats',[]))
        root=tk.Tk();root.withdraw()
        folder=filedialog.askdirectory(title='Выберите синхронизируемую папку Google Диска')
        root.destroy()
        if not folder:raise RuntimeError('Папка выгрузок не выбрана; настройка не завершена.')
        output=Path(folder).resolve()/'TelegramDailyReader'
        if output.is_relative_to(state.resolve()):raise RuntimeError('Выберите папку Google Диска, отдельно от данных программы.')
        output.mkdir(parents=True,exist_ok=True)
        chats=[]
        for d in choices:
            e=d.entity
            channel=hasattr(e,'access_hash')
            chats.append({'chat_id':d.id,'title':d.name,'peer_type':'channel' if channel else 'chat',
                          'peer_id':e.id,'access_hash':getattr(e,'access_hash',None),'forum':bool(getattr(e,'forum',False))})
        me=await client.get_me()
        cfg={**old,'auth_mode':'api','account_user_id':me.id,'api_id':api_id,'api_hash':api_hash,'chats':chats,'output_dir':str(output),
             'timezone':old.get('timezone','Europe/Berlin'),'bootstrap_days':old.get('bootstrap_days',3),
             'publish_days':7,'edit_refresh_hours':48,'max_media_jobs_per_run':100,
             'whisper_model':old.get('whisper_model','small'),'speech_language':old.get('speech_language','ru')}
        cfg.pop('auth_file',None)
        atomic_json(p,cfg)
        print('\nНастройка сохранена. Выбрано групп:',len(chats))
        print('Папка материалов:',output)
        print('Теперь запустите 2-collect-now.cmd. После успешной проверки — 3-enable-autostart.cmd.')
    finally:
        await client.disconnect()
