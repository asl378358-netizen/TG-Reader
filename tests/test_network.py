import asyncio
import base64
import importlib.util
import logging
import os
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ['OPENTELE_NO_FETCH'] = '1'
from telethon.network.connection import ConnectionTcpFull
from telethon.sessions import MemorySession
from tgreader import network
from tgreader.auth import identity_client
from tgreader.desktop import connect_account, new_reader_session


class RoutingTests(unittest.TestCase):
    def test_reads_windows_registry_without_publishing_pac(self):
        fake = MagicMock()
        values = {'ProxyEnable': 1, 'ProxyServer': '127.0.0.1:2080', 'AutoConfigURL': 'https://private/pac'}
        fake.QueryValueEx.side_effect = lambda key, name: (values[name], 1)
        with patch('tgreader.network.sys.platform', 'win32'), patch.dict('sys.modules', {'winreg': fake}):
            result = network.read_windows_proxy_settings()
        self.assertTrue(result['enabled'])
        self.assertTrue(result['pac'])
        self.assertNotIn('https://private/pac', str(result))

    def test_windows_proxy_has_priority_over_environment(self):
        route = network.route_from_sources({'enabled': True, 'server': '127.0.0.1:2080'}, {'https': 'http://wrong:99'})
        self.assertEqual((route.proxy['addr'], route.proxy['port'], route.proxy['proxy_type']), ('127.0.0.1', 2080, 'http'))

    def test_https_key_means_http_connect(self):
        route = network.route_from_sources({'enabled': True, 'server': 'http=one:80;https=two:443;socks=three:1080'}, {})
        self.assertEqual((route.proxy['addr'], route.proxy['port'], route.proxy['proxy_type']), ('two', 443, 'http'))

    def test_windows_socks4_and_explicit_socks5_are_distinct(self):
        old = network.route_from_sources({'enabled': True, 'server': 'socks=localhost:1080'}, {})
        new = network.route_from_sources({'enabled': True, 'server': 'socks5=localhost:1081'}, {})
        self.assertEqual(old.proxy['proxy_type'], 'socks4')
        self.assertEqual(new.proxy['proxy_type'], 'socks5')

    def test_disabled_proxy_does_not_use_stale_server(self):
        route = network.route_from_sources({'available': True, 'enabled': False, 'server': 'stale:80'}, {})
        self.assertIsNone(route.proxy)
        self.assertIn('отключён', route.description)

    def test_decodes_credentials_without_displaying_them(self):
        route = network.route_from_sources({}, {'https': 'socks5://user:p%40ss@127.0.0.1:1080'})
        self.assertEqual(route.proxy['password'], 'p@ss')
        for secret in ('user', 'p@ss', 'p%40ss'):
            self.assertNotIn(secret, route.description)

    def test_ipv6_proxy(self):
        route = network.route_from_sources({'enabled': True, 'server': '[::1]:2080'}, {})
        self.assertEqual(route.proxy['addr'], '::1')
        self.assertIn('[::1]:2080', route.description)

    def test_pac_is_reported_instead_of_silent_bypass(self):
        with self.assertRaisesRegex(RuntimeError, 'PAC'):
            network.route_from_sources({'enabled': False, 'pac': True}, {})

    def test_rejects_invalid_or_tls_proxy_without_leaking_password(self):
        for value in ('http://user:private@host:bad', 'https://user:private@host:443', 'socks5://user:private@host'):
            with self.assertRaises(RuntimeError) as error:
                network.proxy_from_url(value)
            self.assertNotIn('private', str(error.exception))

    def test_actual_reader_constructor_receives_proxy(self):
        route = network.route_from_sources({'enabled': True, 'server': '127.0.0.1:2080'}, {})
        identity = {'api_id': 2040, 'api_hash': 'offline-test', 'device_model': 'test', 'system_version': 'test', 'app_version': 'test', 'lang_code': 'ru', 'system_lang_code': 'ru'}
        client = identity_client(MemorySession(), identity, network=route)
        self.assertEqual(client._proxy, route.proxy)

    def test_desktop_conversion_receives_selected_route(self):
        route = network.route_from_sources({'enabled': True, 'server': '127.0.0.1:2080'}, {})
        client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True), get_me=AsyncMock(return_value='me'), disconnect=AsyncMock())
        account = SimpleNamespace(ToTelethon=AsyncMock(return_value=client))
        self.assertEqual(asyncio.run(connect_account(account, None, route)), (client, 'me'))
        self.assertEqual(account.ToTelethon.call_args.kwargs['proxy'], route.proxy)

    def test_new_reader_session_receives_bootstrap_route(self):
        route = network.NetworkRoute(None, 'offline-direct')
        client = SimpleNamespace(session=MemorySession(), connect=AsyncMock(side_effect=TimeoutError()), is_user_authorized=AsyncMock(return_value=False), disconnect=AsyncMock())
        bootstrap = SimpleNamespace(session=SimpleNamespace(dc_id=2, server_address='127.0.0.1', port=443))
        with patch('tgreader.desktop.identity_client', return_value=client) as factory:
            with self.assertRaises(TimeoutError):
                asyncio.run(new_reader_session(bootstrap, {}, lambda: 'unused', route))
        self.assertIs(factory.call_args.kwargs['network'], route)

    def test_diagnostic_log_does_not_expose_proxy_credentials(self):
        route = network.route_from_sources({}, {'https': 'http://name:password@localhost:8080'})
        with tempfile.TemporaryDirectory() as folder, patch('tgreader.network.detect_route', return_value=route), patch('tgreader.common.state_dir', return_value=Path(folder)), patch('tgreader.network.socket.create_connection', side_effect=ConnectionRefusedError):
            self.assertEqual(network.network_status(), 1)
            text = (Path(folder) / 'network-check.log').read_text(encoding='utf-8-sig')
            self.assertNotIn('password', text)
            self.assertNotIn('name', text)

    def test_update_rolls_back_code_and_preserves_user_data(self):
        file = Path(__file__).resolve().parents[1] / 'update-network.py'
        spec = importlib.util.spec_from_file_location('update_network', file)
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, app = root / 'source', root / 'app'
            for name in updater.FILES:
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                (source / name).write_bytes(b'new')
                if name != 'tgreader/network.py':
                    (app / name).parent.mkdir(parents=True, exist_ok=True)
                    (app / name).write_bytes(b'old')
            (root / 'config.json').write_bytes(b'keep-config')
            (root / 'session.auth.enc').write_bytes(b'keep-session')
            def fail_check():
                raise RuntimeError('simulated import failure')
            with self.assertRaises(RuntimeError):
                updater.apply_program_update(source, app, root / 'backup', fail_check)
            self.assertFalse((app / 'tgreader/network.py').exists())
            self.assertEqual((app / 'app.py').read_bytes(), b'old')
            updater.apply_program_update(source, app, root / 'backup2', lambda: None)
            self.assertEqual((app / 'app.py').read_bytes(), b'new')
            self.assertEqual((root / 'config.json').read_bytes(), b'keep-config')
            self.assertEqual((root / 'session.auth.enc').read_bytes(), b'keep-session')


class ProxyConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def roundtrip(self, kind):
        writers, tasks = [], []
        async def echo(reader, writer):
            writers.append(writer)
            try:
                while data := await reader.read(8192):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
        destination = await asyncio.start_server(echo, '127.0.0.1', 0)
        destination_port = destination.sockets[0].getsockname()[1]
        async def pump(reader, writer):
            try:
                while data := await reader.read(8192):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
        async def handle(reader, writer):
            writers.append(writer)
            try:
                if kind == 'http':
                    head = await reader.readuntil(b'\r\n\r\n')
                    self.assertTrue(head.startswith(f'CONNECT 127.0.0.1:{destination_port} '.encode()))
                    self.assertIn(b'Proxy-Authorization: Basic ' + base64.b64encode(b'user:pass'), head)
                    upstream_reader, upstream_writer = await asyncio.open_connection('127.0.0.1', destination_port)
                    writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
                    await writer.drain()
                elif kind == 'socks4':
                    request = await reader.readexactly(8)
                    self.assertEqual(request[:2], b'\x04\x01')
                    port = struct.unpack('!H', request[2:4])[0]
                    address = socket.inet_ntoa(request[4:8])
                    await reader.readuntil(b'\x00')
                    self.assertEqual((address, port), ('127.0.0.1', destination_port))
                    upstream_reader, upstream_writer = await asyncio.open_connection(address, port)
                    writer.write(b'\x00\x5a' + request[2:8])
                    await writer.drain()
                else:
                    version, count = await reader.readexactly(2)
                    self.assertEqual(version, 5)
                    self.assertIn(0, await reader.readexactly(count))
                    writer.write(b'\x05\x00')
                    await writer.drain()
                    request = await reader.readexactly(4)
                    self.assertEqual(request, b'\x05\x01\x00\x01')
                    address = socket.inet_ntoa(await reader.readexactly(4))
                    port = struct.unpack('!H', await reader.readexactly(2))[0]
                    self.assertEqual((address, port), ('127.0.0.1', destination_port))
                    upstream_reader, upstream_writer = await asyncio.open_connection(address, port)
                    writer.write(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
                    await writer.drain()
                writers.append(upstream_writer)
                pipes = [asyncio.create_task(pump(reader, upstream_writer)), asyncio.create_task(pump(upstream_reader, writer))]
                tasks.extend(pipes)
                await asyncio.gather(*pipes)
            finally:
                writer.close()
        proxy = await asyncio.start_server(handle, '127.0.0.1', 0)
        proxy_port = proxy.sockets[0].getsockname()[1]
        settings = {'proxy_type': kind, 'addr': '127.0.0.1', 'port': proxy_port, 'rdns': True}
        if kind == 'http':
            settings.update(username='user', password='pass')
        client = ConnectionTcpFull('127.0.0.1', destination_port, 2, loggers=defaultdict(lambda: logging.getLogger('proxy-test')), proxy=settings)
        try:
            await asyncio.wait_for(client.connect(timeout=3), 5)
            await client.send(b'local-network-proof')
            self.assertEqual(await asyncio.wait_for(client.recv(), 3), b'local-network-proof')
        finally:
            await client.disconnect()
            proxy.close()
            destination.close()
            await proxy.wait_closed()
            await destination.wait_closed()
            for writer in writers:
                writer.close()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_telethon_through_authenticated_http_connect(self):
        await self.roundtrip('http')

    async def test_telethon_through_socks5(self):
        await self.roundtrip('socks5')

    async def test_telethon_through_socks4(self):
        await self.roundtrip('socks4')
