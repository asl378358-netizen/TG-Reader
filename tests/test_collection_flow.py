import asyncio
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import time
import queue
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telethon.errors import FloodWaitError
from telethon.sessions import MemorySession
from telethon.tl import functions, types

import app
from tgreader.auth import verify_account
from tgreader.collection_flow import CollectionClient, LOG, request_name
from tgreader.common import iso, now
from tgreader.store import Store
from tgreader.media import MediaProcessor,process_jobs
from test_reader import raw

CHAT = {'chat_id': -100999, 'peer_type': 'channel', 'peer_id': 999,
        'access_hash': 123456789, 'forum': False, 'title': 'Offline group'}


class Sender:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def send(self, request, ordered=False):
        while getattr(request, 'query', None) is not None:
            request = request.query
        self.calls.append((type(request).__name__, getattr(request, 'offset_id', None),
                           getattr(request, 'offset', None)))
        future = asyncio.get_running_loop().create_future()
        try:
            future.set_result(self.handler(request))
        except Exception as error:
            future.set_exception(error)
        return future


@contextlib.contextmanager
def virtual_wait():
    clock = [time.time()]
    async def sleep(seconds):
        clock[0] += max(0, seconds)
    with patch('telethon.client.users.time.time', side_effect=lambda: clock[0]), \
         patch('tgreader.collection_flow.asyncio.sleep', side_effect=sleep) as sleeper:
        yield sleeper


def client_for(handler):
    client = CollectionClient(MemorySession(), 2040, 'offline-placeholder', receive_updates=False,
                              request_retries=1)
    client._sender = Sender(handler)
    return client


class CollectionFlowTests(unittest.TestCase):
    def test_real_telethon_fetches_100_media_descriptions_in_one_rpc(self):
        messages=[types.Message(i,types.PeerChannel(999),now(),'offline',
                    media=types.MessageMediaPhoto(types.Photo(i,1,b'offline',now(),[],2))) for i in range(1,101)]
        requested=[]
        def respond(request):
            self.assertIsInstance(request,functions.channels.GetMessagesRequest)
            requested.append([item.id for item in request.id])
            return types.messages.Messages(messages,[],[],[])
        client=client_for(respond)
        async def download(message,file):Path(file).write_bytes(b'offline image');return file
        client.download_media=AsyncMock(side_effect=download)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=Store(root/'messages.sqlite3')
            try:
                for i in range(1,101):store.put(raw(i,kind='photo'))
                with patch.object(MediaProcessor,'process_file',return_value={'assets':[]}), \
                     contextlib.redirect_stdout(io.StringIO()),self.assertLogs(LOG,level='INFO') as logs:
                    asyncio.run(process_jobs(client,store,[CHAT],root,{'output_dir':str(root/'output')}))
                self.assertEqual(requested,[list(range(1,101))])
                self.assertEqual(client.download_media.await_count,100)
                self.assertFalse(store.pending_jobs([CHAT['chat_id']]))
                self.assertIn('requested_messages=100 returned_messages=100','\n'.join(logs.output))
                self.assertNotIn('123456789','\n'.join(logs.output))
            finally:store.close()

    def test_short_limit_retries_exact_rpc_without_reconnect_or_secret_in_log(self):
        requests = []
        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                raise FloodWaitError(request, capture=18)
            return N(ok=True)
        client = client_for(respond)
        request = functions.messages.GetHistoryRequest(types.InputPeerChannel(999, 123456789),
            offset_id=0, offset_date=None, add_offset=0, limit=1, max_id=0, min_id=0, hash=0)
        with virtual_wait() as sleeper, contextlib.redirect_stdout(io.StringIO()) as output, \
             self.assertLogs(LOG, level='WARNING') as logged:
            result = asyncio.run(client(request))
        self.assertTrue(result.ok)
        self.assertEqual(len(requests), 2)
        self.assertIs(requests[0], requests[1])
        self.assertGreaterEqual(sum(call.args[0] for call in sleeper.call_args_list), 19)
        self.assertIn('автоматически', output.getvalue())
        self.assertIn('GetHistoryRequest', '\n'.join(logged.output))
        self.assertNotIn('123456789', '\n'.join(logged.output))

    def test_multiple_short_waits_continue_same_request(self):
        responses = iter([20, 10, None])
        def respond(request):
            delay = next(responses)
            if delay is not None:
                raise FloodWaitError(request, capture=delay)
            return N(ok=True)
        client = client_for(respond)
        with virtual_wait(), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(asyncio.run(client(functions.help.GetConfigRequest())).ok)
        self.assertEqual(len(client._sender.calls), 3)
        self.assertEqual(client.collection_waited, 32)

    def test_long_limit_is_not_retried(self):
        original = FloodWaitError(functions.help.GetConfigRequest(), capture=7200)
        def respond(request): raise original
        client = client_for(respond)
        with virtual_wait() as sleeper, self.assertRaises(FloodWaitError) as caught:
            asyncio.run(client(functions.help.GetConfigRequest()))
        self.assertIs(caught.exception, original)
        self.assertEqual(len(client._sender.calls), 1)
        sleeper.assert_not_called()

    def test_repeated_limits_have_a_total_wait_budget(self):
        def respond(request): raise FloodWaitError(request, capture=300)
        client = client_for(respond)
        with virtual_wait(), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(FloodWaitError):
            asyncio.run(client(functions.help.GetConfigRequest()))
        self.assertEqual(len(client._sender.calls), 2)
        self.assertEqual(client.collection_waited, 301)

    def test_real_download_iterator_retains_file_offset_during_wait(self):
        attempted = False
        data = b'A' * 4096 + b'last chunk'
        def respond(request):
            nonlocal attempted
            self.assertIsInstance(request, functions.upload.GetFileRequest)
            if request.offset == 4096 and not attempted:
                attempted = True
                raise FloodWaitError(request, capture=18)
            return types.upload.File(types.storage.FileUnknown(), 0, data[request.offset:request.offset + request.limit])
        client = client_for(respond)
        location = types.InputDocumentFileLocation(123, 123456789, b'offline-reference', '')
        async def download():
            return b''.join([chunk async for chunk in client.iter_download(location, request_size=4096)])
        with virtual_wait(), contextlib.redirect_stdout(io.StringIO()):
            result = asyncio.run(download())
        self.assertEqual(result, data)
        self.assertEqual([call[2] for call in client._sender.calls], [0, 4096, 4096])

    def test_real_reader_pipeline_exports_all_pages_after_mid_history_flood(self):
        user = types.User(123, is_self=True, first_name='Offline', access_hash=1)
        channel = types.Channel(999, 'Offline group', types.ChatPhotoEmpty(), now(), megagroup=True,
                                access_hash=CHAT['access_hash'])
        messages = [types.Message(i, types.PeerChannel(999), now() - dt.timedelta(hours=1),
                                  f'Offline message {i}', from_id=types.PeerUser(123)) for i in range(1, 202)]
        waited = False
        def respond(request):
            nonlocal waited
            if isinstance(request, functions.users.GetUsersRequest): return [user]
            if isinstance(request, functions.channels.GetChannelsRequest): return types.messages.Chats([channel])
            if isinstance(request, functions.messages.GetHistoryRequest):
                if request.add_offset < 0:
                    if request.offset_id == 101 and not waited:
                        waited = True
                        raise FloodWaitError(request, capture=18)
                    batch = [m for m in messages if m.id >= request.offset_id][:request.limit]
                else:
                    batch = messages[-request.limit:]
                return types.messages.MessagesSlice(len(messages), list(reversed(batch)), [], [channel], [user])
            if isinstance(request, functions.channels.GetMessagesRequest):
                ids = {item.id for item in request.id}
                return types.messages.Messages([m for m in messages if m.id in ids], [], [channel], [user])
            raise AssertionError('Unexpected offline RPC: ' + type(request).__name__)
        client = client_for(respond)
        client.connect = AsyncMock(); client.disconnect = AsyncMock()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = {'timezone': 'Europe/Berlin', 'chats': [dict(CHAT)], 'account_user_id': 123,
                   'output_dir': str(root / 'output'), 'bootstrap_days': 3, 'edit_refresh_hours': 48}
            with patch('app.load_config', return_value=cfg), patch('app.state_dir', return_value=root), \
                 patch('tgreader.auth.create_client', return_value=client), patch('tgreader.auth.persist_client'), \
                 virtual_wait(), contextlib.redirect_stdout(io.StringIO()) as output:
                result = asyncio.run(app.collect())
            self.assertEqual(result, 0)
            self.assertTrue(waited)
            self.assertEqual(client.connect.await_count, 1)
            self.assertEqual(client.disconnect.await_count, 1)
            self.assertNotIn('GetMessagesRequest', [call[0] for call in client._sender.calls])
            self.assertEqual([call[1] for call in client._sender.calls if call[0] == 'GetHistoryRequest'].count(101), 2)
            store = Store(root / 'messages.sqlite3')
            try:
                self.assertEqual(store.cursor(CHAT['chat_id']), 201)
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 201)
            finally: store.close()
            status = json.loads((root / 'output' / 'latest_status.json').read_text(encoding='utf-8'))
            self.assertIsNone(status['chats'][0]['collection_error'])
            self.assertEqual(status['chats'][0]['new_messages_last_run'], 201)
            self.assertTrue(list((root / 'output').glob('*.zip')))
            self.assertIn('Сбор завершён.', output.getvalue())
            self.assertIn('новых сообщений 201, контекст 0',output.getvalue())

    def test_previous_short_pause_is_waited_before_connection(self):
        self.run_saved_pause(18, expect_result=0, expect_connection=True)

    def test_previous_long_pause_does_not_connect_or_report_success(self):
        self.run_saved_pause(7200, expect_result=2, expect_connection=False)

    def run_saved_pause(self, seconds, *, expect_result, expect_connection):
        client = N(connect=AsyncMock(), disconnect=AsyncMock(), get_me=AsyncMock(return_value=N(id=123)))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = {'timezone': 'Europe/Berlin', 'chats': [dict(CHAT)], 'account_user_id': 123,
                   'output_dir': str(root / 'output')}
            (root / 'telegram_cooldown.json').write_text(json.dumps({'resume_utc': iso(now() + dt.timedelta(seconds=seconds))}), encoding='utf-8')
            with patch('app.load_config', return_value=cfg), patch('app.state_dir', return_value=root), \
                 patch('tgreader.auth.create_client', return_value=client) as factory, \
                 patch('tgreader.auth.persist_client'), patch('tgreader.collector.collect_chat', new=AsyncMock(return_value={})), \
                 patch('tgreader.media.process_jobs', new=AsyncMock()), virtual_wait() as sleeper, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(asyncio.run(app.collect()), expect_result)
            if expect_connection:
                factory.assert_called_once(); self.assertTrue(sleeper.called)
                self.assertFalse((root / 'telegram_cooldown.json').exists())
            else:
                factory.assert_not_called(); sleeper.assert_not_called()
                self.assertIn('(CEST)', output.getvalue())
                self.assertNotIn('Сбор завершён.', output.getvalue())

    def test_pause_persists_request_name_and_partial_messages_without_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = {'timezone': 'Europe/Berlin', 'chats': [dict(CHAT)], 'account_user_id': 123,
                   'output_dir': str(root / 'output')}
            client = N(connect=AsyncMock(), disconnect=AsyncMock(), get_me=AsyncMock(return_value=N(id=123)))
            async def partial(client, store, chat, cfg):
                store.put({'chat_id': CHAT['chat_id'], 'id': 1, 'day': now().date().isoformat(),
                    'date_utc': iso(now()), 'date_local': iso(now()), 'text': 'offline', 'media_kind': 'none'}, advance=True)
                raise FloodWaitError(functions.messages.GetHistoryRequest(types.InputPeerChannel(999, 123456789),
                    0, None, 0, 100, 0, 0, 0), capture=7200)
            with patch('app.load_config', return_value=cfg), patch('app.state_dir', return_value=root), \
                 patch('tgreader.auth.create_client', return_value=client), patch('tgreader.auth.persist_client') as persist, \
                 patch('tgreader.collector.collect_chat', side_effect=partial), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(asyncio.run(app.collect()), 2)
            saved = json.loads((root / 'telegram_cooldown.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['request'], 'GetHistoryRequest')
            self.assertEqual(saved['seconds'], 7200)
            persist.assert_called_once()
            self.assertNotIn('Сбор завершён.', output.getvalue())
            store = Store(root / 'messages.sqlite3')
            try: self.assertEqual(store.cursor(CHAT['chat_id']), 1)
            finally: store.close()

    def test_account_check_uses_one_request_and_preserves_real_wait(self):
        client = N(get_me=AsyncMock(side_effect=FloodWaitError(functions.users.GetUsersRequest([]), capture=7200)),
                   is_user_authorized=AsyncMock(return_value=False))
        with self.assertRaises(FloodWaitError): asyncio.run(verify_account(client, {'account_user_id': 123}))
        client.is_user_authorized.assert_not_called()

    def test_window_reports_pause_and_keeps_manual_collection_log(self):
        from winlaunch import ReaderWindow
        class InlineThread:
            def __init__(self, target, **options): self.target=target
            def start(self): self.target()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'config.json').write_text('{}',encoding='utf-8')
            window=ReaderWindow.__new__(ReaderWindow)
            window.root=root;window.source=root;window.record={'version':'0.3.2','python':'offline-python'}
            window.updates=N(check_update=Mock(return_value=(window.record,root,False)),child_environment=Mock(return_value={}))
            window.busy=False;window.buttons=[Mock()];window.queue=queue.Queue()
            window.status=Mock();window.version=Mock();window.show_output=Mock();window.window=Mock()
            output='Сбор приостановлен Telegram: GetHistoryRequest, пауза 7200 с.\n'
            process=N(stdout=io.StringIO(output),wait=Mock(return_value=2))
            with patch('winlaunch.threading.Thread',InlineThread),patch('winlaunch.subprocess.Popen',return_value=process), \
                 patch('winlaunch.messagebox.showerror') as popup:
                window.job('collect');window.poll()
            self.assertIn('Сбор приостановлен',window.status.set.call_args.args[0])
            self.assertNotIn('Готово',window.status.set.call_args.args[0])
            self.assertEqual((root/'last-collection.log').read_text(encoding='utf-8'),output)
            self.assertFalse(window.busy)
            popup.assert_not_called()


if __name__ == '__main__': unittest.main()
