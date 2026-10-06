"""Search and filter the loaded dialog snapshot without Telegram requests."""
import datetime as dt
import math
from dataclasses import dataclass
import tkinter as tk
from tkinter import messagebox, ttk
from zoneinfo import ZoneInfo

from telethon.tl import types
from .common import UTC, now

PAGE_SIZE = 300
KINDS = ('Все', 'Группы', 'Группы с темами', 'Каналы')
ARCHIVES = ('Все, включая архив', 'Без архива', 'Только архив')
ACTIVITY = ('Любое время', 'За 7 дней', 'За 30 дней', 'За 90 дней', 'Старше 90 дней', 'Дата неизвестна')
SORTS = ('Последние сообщения', 'Название', 'Непрочитанные')


def folded(value):
    return str(value or '').casefold().replace('ё', 'е')


@dataclass
class DialogRow:
    dialog: object
    name: str
    search: str
    kind: str
    archived: bool
    date: object
    unread: int
    unavailable: bool


class DialogIndex:
    def __init__(self, dialogs, previous):
        self.rows = {}
        for dialog in dialogs:
            entity = dialog.entity
            unavailable = isinstance(entity, (types.ChatForbidden, types.ChannelForbidden, types.ChatEmpty)) or any(
                bool(getattr(entity, field, False)) for field in ('left', 'kicked', 'deactivated', 'migrated_to'))
            group = bool(getattr(dialog, 'is_group', False) or getattr(entity, 'megagroup', False)
                         or isinstance(entity, (types.Chat, types.ChatForbidden)))
            kind = 'Группа с темами' if group and getattr(entity, 'forum', False) else ('Группа' if group else 'Канал')
            date = getattr(dialog, 'date', None)
            if date is not None and date.tzinfo is None: date = date.replace(tzinfo=UTC)
            usernames = [getattr(entity, 'username', '')]
            usernames.extend(getattr(item, 'username', '') for item in (getattr(entity, 'usernames', None) or []))
            name = str(getattr(dialog, 'name', '') or getattr(entity, 'title', '') or dialog.id)
            search = folded(' '.join([name, str(dialog.id), *[str(x) for x in usernames if x]]))
            self.rows[dialog.id] = DialogRow(dialog, name, search, kind,
                bool(getattr(dialog, 'archived', False) or getattr(dialog, 'folder_id', None) == 1),
                date, int(getattr(dialog, 'unread_count', 0) or 0), unavailable)
        self.selected = {item['chat_id'] for item in previous if item['chat_id'] in self.rows
                         and not self.rows[item['chat_id']].unavailable}

    def filtered(self, *, query='', kind='Все', archive='Все, включая архив', activity='Любое время',
                 unread_only=False, selected_only=False, show_unavailable=False, sort='Последние сообщения', at=None):
        query = folded(query).strip()
        for prefix in ('https://t.me/', 'http://t.me/', 't.me/'):
            if query.startswith(prefix): query = query[len(prefix):]
        words = query.lstrip('@').split()
        at = at or now()
        rows = []
        for cid, row in self.rows.items():
            if row.unavailable and not show_unavailable: continue
            if selected_only and cid not in self.selected: continue
            if words and not all(word in row.search for word in words): continue
            if kind == 'Группы' and row.kind == 'Канал': continue
            if kind == 'Группы с темами' and row.kind != 'Группа с темами': continue
            if kind == 'Каналы' and row.kind != 'Канал': continue
            if archive == 'Без архива' and row.archived: continue
            if archive == 'Только архив' and not row.archived: continue
            if unread_only and row.unread == 0: continue
            if activity.startswith('За '):
                days = int(activity.split()[1])
                if row.date is None or row.date < at - dt.timedelta(days=days): continue
            if activity == 'Старше 90 дней' and (row.date is None or row.date >= at - dt.timedelta(days=90)): continue
            if activity == 'Дата неизвестна' and row.date is not None: continue
            rows.append(row)
        if sort == 'Название': rows.sort(key=lambda row: (folded(row.name), row.dialog.id))
        elif sort == 'Непрочитанные': rows.sort(key=lambda row: (-row.unread, folded(row.name)))
        else: rows.sort(key=lambda row: (row.date or dt.datetime.min.replace(tzinfo=UTC), row.dialog.id), reverse=True)
        return rows

    def toggle(self, cid):
        if self.rows[cid].unavailable: return False
        if cid in self.selected: self.selected.remove(cid)
        else: self.selected.add(cid)
        return True

    def choices(self):
        return [row.dialog for cid, row in self.rows.items() if cid in self.selected and not row.unavailable]


class DialogPicker:
    def __init__(self, root, dialogs, previous, timezone='Europe/Berlin'):
        self.root = root; self.index = DialogIndex(dialogs, previous); self.zone = ZoneInfo(timezone)
        self.answer = []; self.page = 0; self.redraw_job = None; self.visible = []
        root.title('TG Reader — поиск и выбор групп'); root.geometry('1150x720'); root.minsize(950, 650)
        frame = ttk.Frame(root, padding=16); frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Поиск и выбор групп', font=('Segoe UI', 17, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Выбор сохраняется при смене поиска и фильтров. Для первой проверки достаточно одной группы.').pack(anchor='w', pady=(4, 12))
        search_row = ttk.Frame(frame); search_row.pack(fill='x')
        ttk.Label(search_row, text='Поиск:').pack(side='left', padx=(0, 8))
        self.search = tk.StringVar(); self.entry = ttk.Entry(search_row, textvariable=self.search)
        self.entry.pack(side='left', fill='x', expand=True)
        ttk.Button(search_row, text='Очистить поиск', command=lambda: self.search.set('')).pack(side='left', padx=(8, 0))
        ttk.Label(frame, text='Введите часть названия, @username, ссылку t.me или ID чата. Ctrl+F — перейти к поиску.').pack(anchor='w', pady=(3, 10))
        filters = ttk.Frame(frame); filters.pack(fill='x')
        self.kind = tk.StringVar(value=KINDS[0]); self.archive = tk.StringVar(value=ARCHIVES[0])
        self.activity = tk.StringVar(value=ACTIVITY[0]); self.sort = tk.StringVar(value=SORTS[0])
        for column, (label, variable, values, width) in enumerate([
            ('Тип', self.kind, KINDS, 20), ('Архив', self.archive, ARCHIVES, 23),
            ('Последнее сообщение', self.activity, ACTIVITY, 20), ('Сортировка', self.sort, SORTS, 23)]):
            group = ttk.Frame(filters); group.grid(row=0, column=column, padx=(0, 10), sticky='ew')
            ttk.Label(group, text=label).pack(anchor='w')
            box = ttk.Combobox(group, textvariable=variable, values=values, state='readonly', width=width)
            box.pack(fill='x'); filters.columnconfigure(column, weight=1)
            box.bind('<<ComboboxSelected>>', self.schedule_redraw)
        flags = ttk.Frame(frame); flags.pack(fill='x', pady=10)
        self.unread = tk.BooleanVar(); self.selected_only = tk.BooleanVar(); self.unavailable = tk.BooleanVar()
        for label, variable in [('Только непрочитанные', self.unread), ('Только выбранные', self.selected_only),
                                ('Показать недоступные / покинутые', self.unavailable)]:
            ttk.Checkbutton(flags, text=label, variable=variable, command=self.schedule_redraw).pack(side='left', padx=(0, 15))
        listing = ttk.Frame(frame); listing.pack(fill='both', expand=True)
        columns = ('selected', 'name', 'kind', 'archive', 'date', 'unread', 'access')
        self.tree = ttk.Treeview(listing, columns=columns, show='headings', selectmode='browse')
        for key, label, width in [('selected','Выбрано',65),('name','Название',330),('kind','Тип',130),
                                  ('archive','Архив',60),('date','Последнее сообщение',160),('unread','Не прочитано',90),('access','Доступ',100)]:
            self.tree.heading(key,text=label); self.tree.column(key,width=width,minwidth=45,stretch=(key=='name'))
        self.tree.tag_configure('unavailable', foreground='#777777')
        vertical = ttk.Scrollbar(listing, orient='vertical', command=self.tree.yview)
        horizontal = ttk.Scrollbar(listing, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0,column=0,sticky='nsew'); vertical.grid(row=0,column=1,sticky='ns')
        horizontal.grid(row=1,column=0,sticky='ew'); listing.rowconfigure(0,weight=1); listing.columnconfigure(0,weight=1)
        self.tree.bind('<ButtonRelease-1>', self.click); self.tree.bind('<space>', self.keyboard_toggle)
        navigation = ttk.Frame(frame); navigation.pack(fill='x', pady=(10, 4))
        self.previous_button = ttk.Button(navigation,text='← Назад',command=lambda: self.change_page(-1));self.previous_button.pack(side='left')
        self.page_label = tk.StringVar();ttk.Label(navigation,textvariable=self.page_label).pack(side='left',padx=12)
        self.next_button = ttk.Button(navigation,text='Далее →',command=lambda: self.change_page(1));self.next_button.pack(side='left')
        self.counter = tk.StringVar();ttk.Label(frame,textvariable=self.counter).pack(anchor='w',pady=4)
        ttk.Label(frame,text='Галочка в первом столбце или пробел — выбрать. Давность означает дату сообщения, а не вашего посещения.').pack(anchor='w')
        bottom = ttk.Frame(frame);bottom.pack(fill='x',pady=(10,0))
        ttk.Button(bottom,text='Снять весь выбор',command=self.clear_selection).pack(side='left')
        ttk.Button(bottom,text='Продолжить с выбранными',command=self.finish).pack(side='right')
        root.bind('<Control-f>',lambda event: self.entry.focus_set())
        self.search.trace_add('write',self.schedule_redraw)
        root.protocol('WM_DELETE_WINDOW', self.cancel)
        self.redraw(); self.entry.focus_set()

    def schedule_redraw(self, *_):
        self.page = 0
        if self.redraw_job is not None: self.root.after_cancel(self.redraw_job)
        self.redraw_job = self.root.after(180, self.redraw)

    def redraw(self):
        if self.redraw_job is not None: self.root.after_cancel(self.redraw_job)
        self.redraw_job = None
        rows = self.index.filtered(query=self.search.get(),kind=self.kind.get(),archive=self.archive.get(),
            activity=self.activity.get(),unread_only=self.unread.get(),selected_only=self.selected_only.get(),
            show_unavailable=self.unavailable.get(),sort=self.sort.get())
        pages = max(1, math.ceil(len(rows)/PAGE_SIZE)); self.page = min(self.page,pages-1)
        self.visible = rows[self.page*PAGE_SIZE:(self.page+1)*PAGE_SIZE]
        items = self.tree.get_children()
        if items: self.tree.delete(*items)
        for row in self.visible:
            date = row.date.astimezone(self.zone).strftime('%d.%m.%Y %H:%M') if row.date else 'Неизвестно'
            self.tree.insert('','end',iid=str(row.dialog.id),values=(
                '✓' if row.dialog.id in self.index.selected else '',row.name,row.kind,'Да' if row.archived else 'Нет',
                date,row.unread,'Недоступен' if row.unavailable else 'Доступен'),tags=('unavailable',) if row.unavailable else ())
        matched = {row.dialog.id for row in rows}
        hidden = len(self.index.selected - matched)
        self.counter.set(f'Найдено: {len(rows)} из {len(self.index.rows)}. Выбрано: {len(self.index.selected)} '
                         f'(скрыто текущими фильтрами: {hidden}).')
        self.page_label.set(f'Страница {self.page+1} из {pages}')
        self.previous_button.state(['!disabled'] if self.page else ['disabled'])
        self.next_button.state(['!disabled'] if self.page+1<pages else ['disabled'])

    def change_page(self, step):
        if self.redraw_job is not None:
            self.root.after_cancel(self.redraw_job); self.redraw_job=None
        self.page = max(0,self.page+step); self.redraw()

    def click(self,event):
        if self.tree.identify_column(event.x)=='#1': self.toggle(self.tree.identify_row(event.y))

    def keyboard_toggle(self,event=None):
        self.toggle(self.tree.focus()); return 'break'

    def toggle(self,iid):
        if not iid: return
        if not self.index.toggle(int(iid)):
            messagebox.showinfo('Чат недоступен','Telegram сообщает, что доступ закрыт, вы вышли из чата или он перенесён. Выбрать его для чтения нельзя.',parent=self.root)
            return
        self.redraw()
        if self.tree.exists(iid):
            self.tree.focus(iid);self.tree.selection_set(iid)

    def clear_selection(self):
        self.index.selected.clear();self.redraw()

    def finish(self):
        selected = self.index.choices()
        if not selected:
            messagebox.showerror('Нет выбора','Выберите хотя бы одну доступную группу или канал.',parent=self.root);return
        self.answer = selected;self.cancel()

    def cancel(self):
        if self.redraw_job is not None: self.root.after_cancel(self.redraw_job)
        self.root.destroy()


def choose_dialogs(dialogs, previous, timezone='Europe/Berlin'):
    root=tk.Tk();picker=DialogPicker(root,dialogs,previous,timezone);root.mainloop()
    if not picker.answer: raise RuntimeError('Настройка отменена; выбор групп не сохранён.')
    return picker.answer
