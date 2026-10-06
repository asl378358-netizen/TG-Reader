import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch

os.environ['OPENTELE_NO_FETCH'] = '1'
from telethon.crypto import AuthKey
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import MemorySession, StringSession
from tgreader import auth
from tgreader.desktop import desktop_dependencies, new_reader_session


class DesktopTests(unittest.TestCase):
    def test_real_parser_roundtrip_with_local_passcode_and_no_network(self):
        API, current, TDesktop = desktop_dependencies()
        from opentele2.tl import TelegramClient
        from opentele2.exception import TDataBadDecryptKey
        async def roundtrip(folder):
            memory = MemorySession()
            memory.set_dc(2, '149.154.167.50', 443)
            fake_key = os.urandom(256)
            memory.auth_key = AuthKey(fake_key)
            client = TelegramClient(memory, api=API.TelegramDesktop, auto_post_login=False)
            client.UserId = 123456789
            with patch.object(client, 'connect', side_effect=AssertionError('Unexpected network')):
                desktop = await TDesktop.FromTelethon(client, flag=current)
                desktop.SaveTData(str(folder), passcode='local-test-passcode')
            loaded = TDesktop(str(folder), passcode='local-test-passcode')
            self.assertEqual(loaded.accountsCount, 1)
            self.assertEqual(loaded.mainAccount.UserId, 123456789)
            imported = await loaded.ToTelethon(session=MemorySession(), flag=current,
                                               auto_post_login=False, receive_updates=False)
            self.assertEqual(imported.session.auth_key.key, fake_key)
            self.assertEqual(imported.api_id, 2040)
            self.assertEqual(imported.session.dc_id, 2)
            self.assertFalse(imported.is_connected())
            with self.assertRaises(TDataBadDecryptKey):
                TDesktop(str(folder), passcode='wrong-passcode')
        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(roundtrip(Path(folder) / 'tdata'))

    def test_bundle_does_not_leave_plaintext_and_preserves_identity(self):
        memory = StringSession(); memory.set_dc(2, '149.154.167.50', 443)
        memory.auth_key = AuthKey(os.urandom(256))
        identity = {'api_id': 2040, 'api_hash': 'test-placeholder', 'device_model': 'Reader',
                    'system_version': 'Windows', 'app_version': 'test', 'lang_code': 'ru',
                    'system_lang_code': 'ru-RU', 'lang_pack': 'tdesktop'}
        # DPAPI cannot run on Linux. Assert all storage flows through its boundary.
        def protect(raw, decrypt=False):
            return raw[4:] if decrypt else b'ENC:' + raw
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(auth, 'state_dir', return_value=Path(folder)), \
             patch.object(auth, 'protect_session', side_effect=protect):
            name = auth.save_desktop_bundle(N(session=memory), identity, 123)
            cfg = {'auth_mode': 'desktop', 'auth_file': name, 'account_user_id': 123}
            self.assertEqual(len(list(Path(folder).iterdir())), 1)
            self.assertTrue((Path(folder)/name).read_bytes().startswith(b'ENC:'))
            reloaded = auth.create_client(cfg)
            self.assertEqual(reloaded.session.auth_key.key, memory.auth_key.key)
            self.assertEqual(reloaded.api_id, 2040)
            self.assertEqual(reloaded._init_request.lang_pack, 'tdesktop')
            auth.persist_client(reloaded, cfg)
            self.assertEqual(auth.read_bundle(cfg)['identity'], identity)
            with self.assertRaises(RuntimeError):
                auth.create_client({**cfg, 'account_user_id': 999})

    def test_bundle_path_cannot_escape_local_state(self):
        for name in ('../account.auth.enc', '/tmp/account.auth.enc', 'desktop_bad.auth.enc'):
            with self.assertRaises(RuntimeError):
                auth.bundle_path(name)

    def test_expected_account_mismatch_stops_collection(self):
        class Client:
            async def is_user_authorized(self): return True
            async def get_me(self): return N(id=22)
        with self.assertRaises(RuntimeError):
            asyncio.run(auth.verify_account(Client(), {'account_user_id': 11}))

    def run_qr(self, mode):
        seen = []; ready = asyncio.Event(); accepted = asyncio.Event()
        class QR:
            token = b'fake-token-for-offline-test'
            async def wait(self, timeout):
                ready.set(); await accepted.wait()
                if mode == 'password':
                    raise SessionPasswordNeededError(request=None)
                if mode == 'fail':
                    raise TimeoutError('offline failure')
                client.authorized = True
        class Client:
            session = MemorySession()
            authorized = False
            async def connect(self): seen.append('new-connect')
            async def qr_login(self): return QR()
            async def is_user_authorized(self): return self.authorized
            async def sign_in(self, password):
                self.assert_password = password; self.authorized = True
            async def log_out(self): seen.append('new-logout')
            async def disconnect(self): seen.append('new-disconnect')
        class Bootstrap:
            session = N(dc_id=2, server_address='149.154.167.50', port=443)
            async def __call__(self, request):
                self_test.assertTrue(ready.is_set(), 'Token accepted before waiter was ready')
                seen.append('accept'); accepted.set()
        self_test = self; client = Client()
        with patch('tgreader.desktop.identity_client', return_value=client):
            if mode == 'fail':
                with self.assertRaises(TimeoutError):
                    asyncio.run(new_reader_session(Bootstrap(), {}, lambda **options: 'local-only'))
                self.assertIn('new-disconnect', seen)
            else:
                result = asyncio.run(new_reader_session(Bootstrap(), {}, lambda **options: 'local-only'))
                self.assertIs(result, client); self.assertTrue(client.authorized)
                if mode == 'password': self.assertEqual(client.assert_password, 'local-only')
        self.assertNotIn('bootstrap-logout', seen)

    def test_qr_waiter_is_registered_before_acceptance(self): self.run_qr('ok')
    def test_qr_2fa_is_requested_locally_after_telegram_requests_it(self): self.run_qr('password')
    def test_failed_qr_disconnects_new_client(self): self.run_qr('fail')


if __name__ == '__main__': unittest.main()
