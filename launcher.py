"""Permanent shortcut target. Standard library only, before app dependencies load."""
import importlib.util
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


def main():
    root = Path(os.environ['LOCALAPPDATA']) / 'TelegramDailyReader' if os.name == 'nt' else Path(os.environ['TG_READER_TEST_STATE'])
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=root / 'launcher.log', level=logging.INFO, encoding='utf-8',
                        format='%(asctime)s %(levelname)s %(message)s')
    record = json.loads((root / 'current.json').read_text(encoding='utf-8-sig'))
    name = record.get('sha', '')
    import re
    if not re.fullmatch(r'(?:[0-9a-f]{40}|bundled-[0-9a-f]{16})', name):
        raise RuntimeError('Некорректная установленная версия.')
    source = root / 'versions' / name
    spec = importlib.util.spec_from_file_location('reader_updates', source / 'update_client.py')
    updates = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updates)
    if '--collect' in sys.argv:
        try:
            record, source, _ = updates.check_update(root, logging.info)
        except Exception as error:
            logging.warning('Update: %s', str(error))
        with (root / 'last-collection.log').open('w', encoding='utf-8') as output:
            process = subprocess.run([record['python'], str(source / 'app.py'), 'collect'],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                env=updates.child_environment(), creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return process.returncode

    import tkinter as tk
    from tkinter import ttk
    window = tk.Tk()
    window.title('TG Reader')
    window.geometry('760x650')
    frame = ttk.Frame(window, padding=28); frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='TG Reader', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
    status = tk.StringVar(value='Запускаю программу…')
    ttk.Label(frame, textvariable=status, wraplength=690).pack(anchor='w', pady=25)
    progress = ttk.Progressbar(frame, mode='indeterminate'); progress.pack(fill='x'); progress.start()
    messages = queue.Queue()
    def worker():
        try:
            result = updates.check_update(root, lambda value: messages.put(('status', value)))
            messages.put(('done', (result[0], result[1], None)))
        except Exception as error:
            logging.warning('Update: %s', str(error))
            messages.put(('done', (record, source, str(error))))
    threading.Thread(target=worker, daemon=True).start()
    def poll():
        try:
            while True:
                kind, value = messages.get_nowait()
                if kind == 'status':
                    status.set(value)
                else:
                    current, app_source, error = value
                    progress.stop(); frame.destroy()
                    sys.path.insert(0, str(app_source))
                    from winlaunch import ReaderWindow
                    ReaderWindow(window, root, app_source, current, updates, update_error=error)
                    return
        except queue.Empty:
            window.after(100, poll)
    window.after(100, poll)
    window.mainloop()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        logging.exception('Launcher failed')
        import tkinter as tk
        from tkinter import messagebox
        window = tk.Tk(); window.withdraw()
        messagebox.showerror('TG Reader', 'Не удалось открыть программу. Запустите TG-Reader-Setup.cmd ещё раз.\n'
            'Журнал: %LOCALAPPDATA%\\TelegramDailyReader\\launcher.log')
        window.destroy()
        raise SystemExit(1)
