import asyncio
import contextlib
import datetime as dt
import io
import os
import tkinter as tk
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telethon import TelegramClient, types
from telethon.errors import FloodWaitError, PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import MemorySession
from tgreader import credentials
from tgreader.desktop import new_reader_session
from tgreader.setup_flow import setup_dialogs, setup_rpc


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def password_login(self, outcomes, passwords):
        client = N(session=MemorySession(), authorized=False, connect=AsyncMock(),
                   disconnect=AsyncMock(), log_out=AsyncMock())
        client.is_user_authorized = AsyncMock(side_effect=lambda: client.authorized)
        client.qr_login = AsyncMock(return_value=N(token=b'offline-test',
            wait=AsyncMock(side_effect=SessionPasswordNeededError(request=None))))
        submitted = []
        async def sign_in(password):
            submitted.append(password)
            result = outcomes.pop(0)
            if isinstance(result, BaseException):
                raise result
            client.authorized = True
        client.sign_in = sign_in
        hints = []
        def enter(**options):
            hints.append(options.get('error'))
            value = passwords.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        class Bootstrap:
            session = N(dc_id=2, server_address='127.0.0.1', port=443)
            async def __call__(self, request):
                return None
        with patch('tgreader.desktop.identity_client', return_value=client):
            try:
                result = await new_reader_session(Bootstrap(), {}, enter)
            except BaseException:
                self.failed_client = client
                self.failed_submitted = submitted
                raise
        return result, submitted, hints

    async def test_invalid_password_can_be_corrected_on_same_new_session(self):
        password = '  ЁжPass-aA!  '
        with contextlib.redirect_stdout(io.StringIO()) as output:
            client, submitted, hints = await self.password_login(
                [PasswordHashInvalidError(request=None), True], ['wrong', password])
        self.assertEqual(submitted, ['wrong', password])
        self.assertIsNone(hints[0])
        self.assertIn('отклонил', hints[1])
        self.assertNotIn(password, output.getvalue())
        client.connect.assert_awaited_once()
        client.log_out.assert_not_awaited()
        client.disconnect.assert_not_awaited()

    async def test_password_flood_wait_retries_without_another_password_prompt(self):
        with patch('tgreader.setup_flow.wait_for_telegram', new_callable=AsyncMock) as wait:
            client, submitted, hints = await self.password_login(
                [FloodWaitError(request=None, capture=18), True], ['exact-password'])
        self.assertEqual(submitted, ['exact-password', 'exact-password'])
        self.assertEqual(len(hints), 1)
        wait.assert_awaited_once_with(18, 'проверка пароля')
        self.assertTrue(client.authorized)

    async def test_three_rejected_passwords_stop_without_unlimited_attempts(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'трижды'):
                await self.password_login([PasswordHashInvalidError(request=None) for _ in range(3)],
                                          ['one', 'two', 'three'])
        self.assertEqual(self.failed_submitted, ['one', 'two', 'three'])
        self.failed_client.disconnect.assert_awaited_once()
        self.failed_client.log_out.assert_not_awaited()

    async def test_cancelling_password_does_not_send_an_empty_password(self):
        with self.assertRaisesRegex(RuntimeError, 'отменён'):
            await self.password_login([True], [RuntimeError('Ввод отменён')])
        self.assertEqual(self.failed_submitted, [])
        self.failed_client.disconnect.assert_awaited_once()

    async def test_real_telethon_dialog_pagination_continues_after_flood_wait(self):
        def page(start, size):
            chats = [types.Channel(id=n, title=f'Test group {n}', access_hash=n + 100,
                                   photo=types.ChatPhotoEmpty(), date=dt.datetime.now(dt.timezone.utc),
                                   megagroup=True) for n in range(start, start + size)]
            dialogs = [types.Dialog(peer=types.PeerChannel(c.id), top_message=0,
                read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0,
                unread_mentions_count=0, unread_reactions_count=0, unread_poll_votes_count=0,
                notify_settings=types.PeerNotifySettings()) for c in chats]
            return types.messages.DialogsSlice(count=101, dialogs=dialogs, chats=chats,
                                              users=[], messages=[])
        client = TelegramClient(MemorySession(), 1, 'offline-only')
        original = client.iter_dialogs
        iterators = []
        def make_iterator():
            iterator = original()
            iterators.append(iterator)
            return iterator
        responses = AsyncMock(side_effect=[page(1, 100), FloodWaitError(request=None, capture=18), page(101, 1)])
        with patch.object(client, 'iter_dialogs', side_effect=make_iterator), \
             patch.object(TelegramClient, '__call__', responses), \
             patch('tgreader.setup_flow.wait_for_telegram', new_callable=AsyncMock) as wait, \
             contextlib.redirect_stdout(io.StringIO()):
            result = await setup_dialogs(client)
        self.assertEqual(len(iterators), 1)
        self.assertEqual(len(result), 101)
        self.assertEqual(len({item.id for item in result}), 101)
        self.assertEqual(responses.await_count, 3)
        wait.assert_awaited_once_with(18, 'получение списка чатов')

    async def test_long_flood_wait_stops_before_an_extra_request(self):
        operation = AsyncMock(side_effect=FloodWaitError(request=None, capture=1800))
        with patch('tgreader.setup_flow.wait_for_telegram', new_callable=AsyncMock) as wait:
            with self.assertRaisesRegex(RuntimeError, '1800'):
                await setup_rpc(operation, 'test')
        operation.assert_awaited_once()
        wait.assert_not_awaited()

    async def test_short_flood_wait_has_countdown_and_no_blocking_sleep(self):
        from tgreader.setup_flow import wait_for_telegram
        with patch('tgreader.setup_flow.asyncio.sleep', new_callable=AsyncMock) as sleep, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            await wait_for_telegram(18, 'получение списка чатов')
        self.assertEqual(sleep.await_count, 19)
        self.assertTrue(all(call.args[0] <= 1 for call in sleep.call_args_list))
        self.assertIn('Осталось', output.getvalue())
        self.assertIn('Продолжаем', output.getvalue())


class KeyboardTests(unittest.TestCase):
    def test_windows_caps_lock_and_layout_read_from_active_thread(self):
        api = MagicMock()
        api.GetKeyState.return_value = 1
        api.GetKeyboardLayout.return_value = 0x0419
        with patch('tgreader.credentials.os', N(name='nt')), \
             patch('tgreader.credentials.ctypes.WinDLL', return_value=api, create=True):
            self.assertIn('RU', credentials.keyboard_status())
            self.assertIn('ВКЛЮЧЁН', credentials.keyboard_status())
            api.GetKeyboardLayout.assert_called_with(0)
            api.GetKeyState.return_value = 0
            api.GetKeyboardLayout.return_value = 0x0809
            self.assertIn('EN', credentials.keyboard_status())
            self.assertIn('выключен', credentials.keyboard_status())


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', 'Tk display unavailable')
class PasswordWindowTests(unittest.TestCase):
    def dialog(self, **options):
        dialog = credentials.PasswordDialog('Offline test', 'Пароль выбранного аккаунта', **options)
        def close_if_open():
            try:
                if dialog.root.winfo_exists():
                    dialog.cancel()
            except tk.TclError:
                pass
        self.addCleanup(close_if_open)
        return dialog

    def test_password_visible_by_default_and_case_spaces_unicode_preserved(self):
        dialog = self.dialog()
        value = '  ЁжPass-aA!  '
        dialog.value.set(value)
        self.assertEqual(str(dialog.entry.cget('show')), '')
        self.assertTrue(dialog.visible.get())
        self.assertIn(str(len(value)), dialog.count.get())
        self.assertIn('пробел', dialog.count.get())
        dialog.root.after(20, dialog.accept)
        self.assertEqual(dialog.run(), value)

    def test_hide_and_show_switch_and_actual_clipboard_paste(self):
        dialog = self.dialog()
        dialog.visible.set(False)
        dialog.change_visibility()
        self.assertNotEqual(str(dialog.entry.cget('show')), '')
        dialog.visible.set(True)
        dialog.change_visibility()
        self.assertEqual(str(dialog.entry.cget('show')), '')
        dialog.root.clipboard_clear()
        dialog.root.clipboard_append('Copy-Ёж-aA!')
        dialog.paste()
        self.assertEqual(dialog.value.get(), 'Copy-Ёж-aA!')
        self.assertEqual(dialog.entry.get(), 'Copy-Ёж-aA!')

    def test_empty_2fa_is_blocked_but_local_passcode_can_be_empty(self):
        required = self.dialog()
        self.assertIn('disabled', required.submit.state())
        required.accept()
        self.assertTrue(required.root.winfo_exists())
        required.cancel()
        optional = self.dialog(allow_empty=True)
        self.assertNotIn('disabled', optional.submit.state())
        optional.root.after(20, optional.accept)
        self.assertEqual(optional.run(), '')

    def test_close_cancels_and_buttons_fit_in_window(self):
        dialog = self.dialog(error='Telegram отклонил предыдущий пароль. Проверьте видимый текст и выбранный аккаунт.')
        dialog.root.update()
        bottom = dialog.submit.winfo_rooty() + dialog.submit.winfo_height()
        self.assertLessEqual(bottom, dialog.root.winfo_rooty() + dialog.root.winfo_height())
        dialog.root.after(20, dialog.cancel)
        with self.assertRaisesRegex(RuntimeError, 'отменён'):
            dialog.run()


if __name__ == '__main__':
    unittest.main()
