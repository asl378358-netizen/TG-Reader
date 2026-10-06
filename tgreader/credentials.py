"""Local, visible password entry. Values never go to logs or configuration."""
import ctypes
import os
import tkinter as tk
from tkinter import ttk


def keyboard_status():
    if os.name != 'nt':
        return 'Раскладку и Caps Lock проверьте на клавиатуре.'
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetKeyState.argtypes = [ctypes.c_int]
        user32.GetKeyState.restype = ctypes.c_short
        user32.GetKeyboardLayout.argtypes = [ctypes.c_uint32]
        user32.GetKeyboardLayout.restype = ctypes.c_void_p
        caps = bool(user32.GetKeyState(0x14) & 1)
        language = (user32.GetKeyboardLayout(0) or 0) & 0xFFFF
        layout = {0x09: 'EN', 0x19: 'RU', 0x22: 'UA', 0x23: 'BY',
                  0x07: 'DE'}.get(language & 0x3FF, f'0x{language:04X}')
        return f'Раскладка: {layout} · Caps Lock: {"ВКЛЮЧЁН" if caps else "выключен"}'
    except (AttributeError, OSError):
        return 'Раскладку и Caps Lock проверьте на клавиатуре.'


class PasswordDialog:
    def __init__(self, title, message, *, allow_empty=False, error=None):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.resizable(True, True)
        self.root.minsize(620, 400)
        self.root.geometry('720x430')
        self.answer = None
        self.allow_empty = allow_empty
        self.value = tk.StringVar(self.root)
        self.visible = tk.BooleanVar(self.root, value=True)
        self.count = tk.StringVar(self.root, value='Символов: 0')
        self.keyboard = tk.StringVar(self.root)
        self.error = tk.StringVar(self.root, value=error or '')
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=message, wraplength=650, justify='left').pack(anchor='w', pady=(0, 12))
        self.entry = ttk.Entry(frame, textvariable=self.value, show='', font=('Segoe UI', 13))
        self.entry.pack(fill='x', pady=(0, 8))
        ttk.Checkbutton(frame, text='Показать пароль', variable=self.visible,
                        command=self.change_visibility).pack(anchor='w')
        ttk.Label(frame, textvariable=self.count).pack(anchor='w', pady=(8, 0))
        ttk.Label(frame, textvariable=self.keyboard).pack(anchor='w')
        ttk.Label(frame, textvariable=self.error, foreground='#B00020',
                  wraplength=650).pack(anchor='w', pady=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(12, 0))
        ttk.Button(buttons, text='Вставить из буфера', command=self.paste).pack(side='left')
        ttk.Button(buttons, text='Отмена', command=self.cancel).pack(side='right')
        self.submit = ttk.Button(buttons, text='Продолжить', command=self.accept)
        self.submit.pack(side='right', padx=8)
        self.value.trace_add('write', self.changed)
        self.root.bind('<Return>', self.accept)
        self.root.bind('<Escape>', self.cancel)
        self.root.protocol('WM_DELETE_WINDOW', self.cancel)
        self.changed()
        self.refresh_keyboard()
        self.root.update_idletasks()
        width = max(720, self.root.winfo_reqwidth())
        height = max(430, self.root.winfo_reqheight())
        self.root.minsize(width, height)
        self.root.geometry(f'{width}x{height}')

    def changed(self, *_):
        value = self.value.get()
        edges = bool(value and (value[0].isspace() or value[-1].isspace()))
        self.count.set(f'Символов: {len(value)}' + (' · Есть пробел в начале или конце' if edges else ''))
        if any(char in value for char in '\r\n\t'):
            self.count.set(self.count.get() + ' · Есть перенос строки или табуляция')
        self.submit.state(['!disabled'] if value or self.allow_empty else ['disabled'])

    def change_visibility(self):
        self.entry.configure(show='' if self.visible.get() else '●')

    def refresh_keyboard(self):
        self.keyboard.set(keyboard_status())
        self.keyboard_timer = self.root.after(250, self.refresh_keyboard)

    def paste(self):
        try:
            value = self.root.clipboard_get()
        except tk.TclError:
            self.error.set('В буфере обмена нет текста.')
            return
        self.value.set(value)
        self.entry.icursor('end')
        self.entry.focus_set()

    def accept(self, event=None):
        value = self.value.get()
        if not value and not self.allow_empty:
            self.error.set('Введите пароль или нажмите «Отмена».')
            return
        # Do not strip or normalize a password; spaces/case can be intentional.
        self.answer = value
        self.value.set('')
        self.close()

    def cancel(self, event=None):
        self.value.set('')
        self.close()

    def close(self):
        self.root.after_cancel(self.keyboard_timer)
        self.root.destroy()

    def run(self):
        self.root.lift()
        self.entry.focus_force()
        self.root.mainloop()
        result, self.answer = self.answer, None
        if result is None:
            raise RuntimeError('Ввод пароля отменён. Настройка остановлена.')
        return result


def read_password(title, message, *, allow_empty=False, error=None):
    return PasswordDialog(title, message, allow_empty=allow_empty, error=error).run()
