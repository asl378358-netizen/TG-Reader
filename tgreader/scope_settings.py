"""Local settings: no new Telegram login or dialog-list requests."""
import copy
from pathlib import Path
import tkinter as tk
from tkinter import filedialog,messagebox,ttk
from zoneinfo import ZoneInfo

from .common import atomic_json,load_config,state_dir
from .scope import MEDIA_LABELS,PERIODS,cutoff,history_days,media_options


class ScopeSettings:
    def __init__(self,root,cfg):
        self.root=root;self.cfg=copy.deepcopy(cfg);self.answer=None
        self.options={chat['chat_id']:media_options(chat) for chat in cfg['chats']}
        root.title('TG Reader — период и медиа');root.geometry('920x760');root.minsize(780,700)
        frame=ttk.Frame(root,padding=20);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='Период чтения и вложения',font=('Segoe UI',18,'bold')).pack(anchor='w')
        period=ttk.Frame(frame);period.pack(fill='x',pady=(16,6))
        ttk.Label(period,text='Собирать сообщения за последние').pack(side='left')
        self.days=tk.StringVar(value=str(history_days(cfg)))
        box=ttk.Combobox(period,textvariable=self.days,values=[str(x) for x in PERIODS],width=5,state='readonly')
        box.pack(side='left',padx=8);ttk.Label(period,text='суток.').pack(side='left')
        self.start=tk.StringVar();ttk.Label(frame,textvariable=self.start).pack(anchor='w')
        self.days.trace_add('write',lambda *_:self.update_start());self.update_start()
        self.output=tk.StringVar(value=cfg.get('output_dir',''));self.output_changed=False
        destination=ttk.Frame(frame);destination.pack(fill='x',pady=(12,0))
        ttk.Label(destination,text='Папка выгрузок:').pack(anchor='w')
        ttk.Label(destination,textvariable=self.output,wraplength=850).pack(anchor='w')
        ttk.Button(destination,text='Выбрать папку Google Диска…',command=self.choose_output).pack(anchor='w',pady=5)
        self.output_note=tk.StringVar();self.update_output_note()
        ttk.Label(destination,textvariable=self.output_note,wraplength=860).pack(anchor='w')
        ttk.Label(frame,text='Текст и ссылки сохраняются всегда. Галочки управляют скачиванием и обработкой вложений.',
                  wraplength=860).pack(anchor='w',pady=(12,10))
        search=ttk.Frame(frame);search.pack(fill='x',pady=(0,8))
        ttk.Label(search,text='Поиск среди выбранных групп:').pack(side='left')
        self.query=tk.StringVar();ttk.Entry(search,textvariable=self.query).pack(side='left',fill='x',expand=True,padx=(10,0))
        self.query.trace_add('write',lambda *_:self.redraw())
        listing=ttk.Frame(frame);listing.pack(fill='both',expand=True)
        self.tree=ttk.Treeview(listing,columns=('name','text',*MEDIA_LABELS),show='headings',selectmode='browse')
        for key,label,width in [('name','Группа / канал',410),('text','Текст',65),
                                ('photo','Фото',80),('voice','Голосовые',105),('round_video','Кружочки',105)]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width,minwidth=60,stretch=key=='name')
        scrollbar=ttk.Scrollbar(listing,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side='left',fill='both',expand=True);scrollbar.pack(side='right',fill='y')
        self.tree.bind('<ButtonRelease-1>',self.click)
        self.tree.bind('<<TreeviewSelect>>',lambda *_:self.sync_controls())
        controls=ttk.Frame(frame);controls.pack(fill='x',pady=10)
        self.flags={kind:tk.BooleanVar() for kind in MEDIA_LABELS}
        self.buttons=[]
        for kind,label in MEDIA_LABELS.items():
            button=ttk.Checkbutton(controls,text=label,variable=self.flags[kind],command=lambda k=kind:self.set_selected(k))
            button.pack(side='left',padx=(0,18));self.buttons.append(button)
        ttk.Label(frame,text='Нажмите галочку в таблице или выделите группу и используйте переключатели ниже. '
                  'Для новостей можно оставить только текст. Обычные видео не скачиваются.',wraplength=860).pack(anchor='w')
        ttk.Label(frame,text='Изменения действуют со следующего сбора. Старые вложения вне периода не скачиваются. '
                  'Уже сохранённые файлы на компьютере не удаляются.',wraplength=860).pack(anchor='w',pady=(8,0))
        bottom=ttk.Frame(frame);bottom.pack(fill='x',pady=(15,0))
        ttk.Button(bottom,text='Отмена',command=root.destroy).pack(side='left')
        ttk.Button(bottom,text='Сохранить',command=self.save).pack(side='right')
        root.protocol('WM_DELETE_WINDOW',root.destroy);self.redraw()

    def update_start(self):
        value={**self.cfg,'history_days':int(self.days.get())}
        stamp=cutoff(value).astimezone(ZoneInfo(self.cfg.get('timezone','Europe/Berlin')))
        self.start.set('Сейчас это сообщения начиная с '+stamp.strftime('%d.%m.%Y %H:%M (%Z)')+'.')

    def update_output_note(self):
        parts=self.output.get().replace('\\','/').casefold().split('/')
        if 'tdata' in parts:
            self.output_note.set('Сейчас выгрузки находятся внутри tdata. Для автоматической передачи выберите '
                                 'синхронизируемую папку Google Диска; повторный вход в Telegram не нужен.')
        else:
            self.output_note.set('Выберите синхронизируемую папку Google Диска в Проводнике. '
                                 'Настройки Telegram и tdata остаются отдельно.')

    def redraw(self):
        focus=self.tree.focus();items=self.tree.get_children()
        if items:self.tree.delete(*items)
        words=self.query.get().casefold().split()
        for chat in self.cfg['chats']:
            if not all(word in chat['title'].casefold() for word in words):continue
            options=self.options[chat['chat_id']]
            self.tree.insert('','end',iid=str(chat['chat_id']),values=(chat['title'],'Всегда',
                *['✓' if options[kind] else '—' for kind in MEDIA_LABELS]))
        if focus and self.tree.exists(focus):self.tree.focus(focus);self.tree.selection_set(focus)
        self.sync_controls()

    def sync_controls(self):
        selected=self.tree.selection()
        for button in self.buttons:button.state(['!disabled'] if selected else ['disabled'])
        if selected:
            options=self.options[int(selected[0])]
            for kind,variable in self.flags.items():variable.set(options[kind])

    def toggle(self,cid,kind):
        self.options[cid][kind]=not self.options[cid][kind]
        self.redraw()

    def click(self,event):
        iid=self.tree.identify_row(event.y);column=self.tree.identify_column(event.x)
        if iid and column in ('#3','#4','#5'):
            self.toggle(int(iid),list(MEDIA_LABELS)[int(column[1:])-3])

    def set_selected(self,kind):
        selected=self.tree.selection()
        if selected:
            self.options[int(selected[0])][kind]=self.flags[kind].get();self.redraw()

    def save(self):
        if self.output_changed:
            try:Path(self.output.get()).mkdir(parents=True,exist_ok=True)
            except OSError as error:
                messagebox.showerror('Папка недоступна',str(error),parent=self.root);return
        self.cfg['output_dir']=self.output.get()
        self.cfg['history_days']=int(self.days.get());self.cfg['scope_confirmed']=True
        for chat in self.cfg['chats']:chat['media']=dict(self.options[chat['chat_id']])
        self.answer=self.cfg;self.root.destroy()

    def choose_output(self):
        selected=filedialog.askdirectory(title='Выберите синхронизируемую папку Google Диска',parent=self.root)
        if not selected:return
        try:target=output_directory(selected)
        except ValueError as error:
            messagebox.showerror('Выберите другую папку',str(error),parent=self.root);return
        self.output.set(str(target));self.output_changed=True;self.update_output_note()


def output_directory(selected):
    target=Path(selected).resolve()
    if any(part.casefold()=='tdata' for part in target.parts):
        raise ValueError('Папка выгрузок должна быть отдельно от tdata. Выберите папку Google Диска.')
    if target.is_relative_to(state_dir().resolve()):
        raise ValueError('Выберите папку Google Диска, отдельно от данных программы.')
    if target.name.casefold()!='telegramdailyreader':target=target/'TelegramDailyReader'
    return target


def configure_scope(cfg,*,cancel_ok=False):
    root=tk.Tk();dialog=ScopeSettings(root,cfg);root.mainloop()
    if dialog.answer is None and not cancel_ok:raise RuntimeError('Настройка периода и медиа отменена; изменения не сохранены.')
    return dialog.answer


def edit_scope():
    cfg=configure_scope(load_config(),cancel_ok=True)
    if cfg is None:
        print('Изменения периода и медиа отменены; прежние настройки сохранены.',flush=True);return
    atomic_json(state_dir()/'config.json',cfg)
    print(f'Период сохранён: последние {history_days(cfg)} суток. Текст сохраняется во всех группах.',flush=True)
    for chat in cfg['chats']:
        names=[MEDIA_LABELS[kind] for kind,enabled in media_options(chat).items() if enabled]
        print(chat['title']+': '+(', '.join(names) if names else 'только текст и ссылки')+'.',flush=True)
