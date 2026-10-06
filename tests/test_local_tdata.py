import asyncio
import contextlib
import hashlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import Mock, patch

os.environ['OPENTELE_NO_FETCH'] = '1'
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession
from tgreader.desktop import desktop_dependencies, load_desktop


def create_local_data(folder, passcode=None):
    API, current, TDesktop = desktop_dependencies()
    from opentele2.tl import TelegramClient
    async def create():
        memory = MemorySession()
        memory.set_dc(2, '149.154.167.50', 443)
        key = os.urandom(256)
        memory.auth_key = AuthKey(key)
        client = TelegramClient(memory, api=API.TelegramDesktop, auto_post_login=False)
        client.UserId = 987654321
        with patch.object(client, 'connect', side_effect=AssertionError('Unexpected network')):
            desktop = await TDesktop.FromTelethon(client, flag=current)
            desktop.SaveTData(str(folder), passcode=passcode)
        return key
    return asyncio.run(create())


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob('*') if p.is_file()}


class LocalDataTests(unittest.TestCase):
    def test_real_unlocked_tdata_needs_no_password_prompt_and_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'tdata'; create_local_data(path)
            before = snapshot(path)
            callback = Mock(side_effect=AssertionError('Unnecessary local password prompt'))
            with contextlib.redirect_stdout(io.StringIO()):
                desktop = load_desktop(path, callback)
            self.assertTrue(desktop.isLoaded())
            self.assertEqual(desktop.mainAccount.UserId, 987654321)
            callback.assert_not_called()
            self.assertEqual(snapshot(path), before)

    def test_real_locked_tdata_wrong_password_can_be_corrected_without_restart(self):
        API, _, TDesktop = desktop_dependencies()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'tdata'; create_local_data(path, 'local-passcode')
            before = snapshot(path); submitted = []
            def parse(*args, **options):
                submitted.append(options['passcode']); return TDesktop(*args, **options)
            callback = Mock(side_effect=['mistaken-cloud-password', 'local-passcode'])
            with patch('tgreader.desktop.desktop_dependencies', return_value=(API, None, parse)), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                desktop = load_desktop(path, callback)
            self.assertTrue(desktop.isLoaded())
            self.assertEqual(submitted, [None, 'mistaken-cloud-password', 'local-passcode'])
            self.assertEqual(callback.call_count, 2)
            self.assertFalse(callback.call_args.kwargs['allow_empty'])
            self.assertIn('Можно повторить', callback.call_args.kwargs['error'])
            self.assertIn('Облачный пароль', callback.call_args.args[1])
            self.assertNotIn('mistaken-cloud-password', output.getvalue())
            self.assertNotIn('local-passcode', output.getvalue())
            self.assertEqual(snapshot(path), before)

    def test_three_bad_local_codes_stop_without_changing_source_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'tdata'; create_local_data(path, 'local-passcode')
            before = snapshot(path); callback = Mock(return_value='wrong-local-passcode')
            with self.assertRaisesRegex(RuntimeError, 'трёх попыток') as caught:
                load_desktop(path, callback)
            self.assertEqual(callback.call_count, 3)
            self.assertNotIn('wrong-local-passcode', str(caught.exception))
            self.assertEqual(snapshot(path), before)

    def test_cancel_local_code_prompt_stops_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'tdata'; create_local_data(path, 'local-passcode')
            callback = Mock(side_effect=RuntimeError('Ввод пароля отменён'))
            with self.assertRaisesRegex(RuntimeError, 'отменён'): load_desktop(path, callback)
            callback.assert_called_once()

    def test_damaged_files_are_not_reported_as_wrong_account_password(self):
        from opentele2.exception import TDataInvalidCheckSum
        callback = Mock()
        API, current, _ = desktop_dependencies()
        parser = Mock(side_effect=TDataInvalidCheckSum('offline damaged file'))
        with patch('tgreader.desktop.desktop_dependencies', return_value=(API, current, parser)):
            with self.assertRaisesRegex(RuntimeError, 'TDataInvalidCheckSum'): load_desktop(Path('offline'), callback)
        callback.assert_not_called()

    def test_unicode_local_code_error_allows_retry_and_does_not_echo_code(self):
        from opentele2.exception import TDataBadDecryptKey
        API, current, _ = desktop_dependencies()
        good = N(isLoaded=lambda: True, accounts=[N()])
        parser = Mock(side_effect=[TDataBadDecryptKey('offline'), UnicodeEncodeError('ascii', 'ёж', 0, 1, 'offline'), good])
        callback = Mock(side_effect=['ёж', 'ascii-local'])
        with patch('tgreader.desktop.desktop_dependencies', return_value=(API, current, parser)), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertIs(load_desktop(Path('offline'), callback), good)
        self.assertEqual(callback.call_count, 2)
        self.assertNotIn('ёж', output.getvalue())
        self.assertIn('символы', callback.call_args.kwargs['error'])


if __name__ == '__main__': unittest.main()
