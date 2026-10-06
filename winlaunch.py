"""Desktop controls, all child jobs run against the active immutable version."""
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser


class ReaderWindow:
    def __init__(self, window, root, source, record, updates, *, update_error=None):
        self.window, self.root, self.source = window, Path(root), Path(source)
        self.record, self.updates = record, updates
        self.busy = False
        self.queue = queue.Queue()
        self.buttons = []
        window.title('TG Reader'); window.minsize(700, 560)
        frame = ttk.Frame(window, padding=22); frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='TG Reader', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
        self.version = tk.StringVar(value='Версия ' + record['version'])
        ttk.Label(frame, textvariable=self.version).pack(anchor='w', pady=(2, 12))
        self.status = tk.StringVar(value=update_error or self.ready_text())
        ttk.Label(frame, textvariable=self.status, wraplength=680).pack(anchor='w', pady=(0, 15))
        actions = ttk.Frame(frame); actions.pack(fill='x')
        for text, callback in [
            ('Подключить Telegram / выбрать группы', lambda: self.job('setup-desktop')),
            ('Собрать сейчас', lambda: self.job('collect')),
            ('Открыть состояние', self.open_status),
            ('Открыть папку выгрузок', self.open_exports),
            ('Включить автосбор', lambda: self.job('schedule-enable')),
            ('Отключить автосбор', lambda: self.job('schedule-disable')),
            ('Проверить обновления', lambda: self.job('update')),
            ('Открыть журналы', self.open_logs),
        ]:
            button = ttk.Button(actions, text=text, command=callback)
            button.pack(fill='x', pady=3); self.buttons.append(button)
        self.output = tk.Text(frame, height=7, wrap='word', state='disabled')
        self.output.pack(fill='both', expand=True, pady=(15, 0))
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.after(100, self.poll)

    def ready_text(self):
        return ('Настройка сохранена. Можно собирать сообщения.' if (self.root / 'config.json').exists()
                else 'Сначала нажмите «Подключить Telegram / выбрать группы».')

    def config(self):
        path = self.root / 'config.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}

    def show_output(self, value):
        self.output.configure(state='normal'); self.output.insert('end', value + '\n')
        self.output.see('end'); self.output.configure(state='disabled')

    def job(self, command):
        if self.busy: return
        if command in ('collect', 'schedule-enable') and not (self.root / 'config.json').exists():
            messagebox.showinfo('TG Reader', 'Сначала подключите Telegram и выберите группы.'); return
        self.busy = True
        for button in self.buttons: button.state(['disabled'])
        self.status.set('Выполняю…')
        def worker():
            try:
                try:
                    self.record, self.source, _ = self.updates.check_update(self.root,
                        lambda value: self.queue.put(('status', value)))
                except Exception as error:
                    self.queue.put(('line', str(error)))
                    if command == 'update':
                        self.queue.put(('done', (1, str(error)))); return
                if command == 'update':
                    self.queue.put(('done', (0, 'Проверка обновлений завершена.'))); return
                if command.startswith('schedule-'):
                    mode = command.split('-', 1)[1]
                    arguments = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(self.source / 'schedule.ps1'), '-Mode', mode]
                else:
                    arguments = [self.record['python'], str(self.source / 'app.py'), command]
                self.queue.put(('status', 'Настройка Telegram…' if command == 'setup-desktop' else 'Выполняю сбор…'))
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace', env=self.updates.child_environment(), creationflags=flags)
                for line in process.stdout:
                    self.queue.put(('line', line.rstrip()))
                code = process.wait()
                result = 'Готово.' if code == 0 else 'Операция завершилась с ошибкой. Подробности ниже и в журнале.'
                self.queue.put(('done', (code, result)))
            except Exception as error:
                logging.exception('Reader action failed')
                self.queue.put(('done', (1, str(error))))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.queue.get_nowait()
                if kind == 'line': self.show_output(value)
                elif kind == 'status': self.status.set(value)
                else:
                    code, text = value
                    self.status.set(text); self.show_output(text)
                    self.version.set('Версия ' + self.record['version'])
                    self.busy = False
                    for button in self.buttons: button.state(['!disabled'])
                    if code: messagebox.showerror('TG Reader', text)
        except queue.Empty:
            pass
        self.window.after(100, self.poll)

    def open_status(self):
        path = self.root / 'status.html'
        if path.exists(): webbrowser.open(path.as_uri())
        else: messagebox.showinfo('TG Reader', 'Сначала выполните первый сбор сообщений.')

    def open_exports(self):
        folder = self.config().get('output_dir')
        if folder and Path(folder).is_dir(): os.startfile(folder)
        else: messagebox.showinfo('TG Reader', 'Папка выгрузок выбирается при подключении Telegram.')

    def open_logs(self):
        os.startfile(str(self.root))

    def close(self):
        if self.busy:
            messagebox.showinfo('TG Reader', 'Дождитесь завершения текущей операции.'); return
        self.window.destroy()
