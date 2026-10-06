import asyncio
import contextlib
import io
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import Mock, patch

from telethon.tl import types

from tgreader.collection_flow import CollectionProgress
from tgreader.media import MediaProcessor, PublicModelNotice, model_wait_notice, process_jobs
from tgreader.store import Store
from test_reader import CHAT, CFG, message, raw


class MediaFlowTests(unittest.TestCase):
    def run_media(self, chats, records, *, missing=(), error=None, budget=200):
        calls=[]; downloads=[]
        class Client:
            async def get_messages(self,peer,ids):
                calls.append((peer.channel_id,list(ids)))
                if error:raise error
                rows=[]
                for mid in reversed(ids):
                    if mid in missing:continue
                    m=message(mid)
                    m.document=N(attributes=[types.DocumentAttributeAudio(3,voice=True)],mime_type='audio/ogg')
                    rows.append(m)
                return rows
            async def download_media(self,m,file):
                downloads.append(m.id);Path(file).write_bytes(b'offline voice');return file
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=Store(root/'messages.sqlite3')
            try:
                for cid,mid in records:store.put(dict(raw(mid,kind='voice'),chat_id=cid))
                with patch.object(MediaProcessor,'process_file',return_value={'transcription':{'text':'offline'}}), \
                     contextlib.redirect_stdout(io.StringIO()), patch('tgreader.media.LOG'):
                    asyncio.run(process_jobs(Client(),store,chats,root,
                        {**CFG,'output_dir':str(root/'output'),'max_media_jobs_per_run':budget}))
                statuses={(cid,mid):store.job(cid,mid)['status'] for cid,mid in records}
            finally:store.close()
        return calls,downloads,statuses

    def test_101_media_messages_use_two_batches_and_all_originals_saved(self):
        records=[(CHAT['chat_id'],i) for i in range(1,102)]
        calls,downloads,statuses=self.run_media([CHAT],records)
        self.assertEqual([len(ids) for _,ids in calls],[100,1])
        self.assertEqual(downloads,list(range(1,102)))
        self.assertEqual(set(statuses.values()),{'done'})

    def test_batches_keep_chats_separate_when_message_ids_overlap(self):
        other={**CHAT,'chat_id':-100888,'peer_id':888}
        records=[(cid,i) for cid in (CHAT['chat_id'],other['chat_id']) for i in (1,2)]
        calls,downloads,statuses=self.run_media([CHAT,other],records)
        self.assertEqual(sorted(calls),[(888,[1,2]),(999,[1,2])])
        self.assertEqual(len(downloads),4)
        self.assertEqual(set(statuses.values()),{'done'})

    def test_missing_message_is_not_assigned_another_messages_media(self):
        calls,downloads,statuses=self.run_media([CHAT],[(CHAT['chat_id'],i) for i in (1,2,3)],missing=(2,))
        self.assertEqual(len(calls),1)
        self.assertEqual(downloads,[1,3])
        self.assertEqual(statuses[(CHAT['chat_id'],2)],'error')
        self.assertEqual(statuses[(CHAT['chat_id'],3)],'done')

    def test_batch_failure_does_not_make_a_request_for_every_job(self):
        calls,downloads,statuses=self.run_media([CHAT],[(CHAT['chat_id'],i) for i in range(1,11)],
                                              error=ConnectionError('offline transport'))
        self.assertEqual(len(calls),1);self.assertFalse(downloads)
        self.assertEqual(set(statuses.values()),{'error'})

    def test_media_budget_leaves_remaining_jobs_pending(self):
        records=[(CHAT['chat_id'],i) for i in range(1,106)]
        calls,downloads,statuses=self.run_media([CHAT],records,budget=100)
        self.assertEqual(len(calls),1);self.assertEqual(len(downloads),100)
        self.assertEqual(statuses[(CHAT['chat_id'],105)],'pending')

    def test_last_30_seconds_include_wait_without_losing_saved_totals(self):
        clock=[0.0];progress=CollectionProgress(clock=lambda:clock[0])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            for _ in range(3):progress.saved()
            progress.saved(context=True)
            clock[0]=20;progress.report(force=True)
            clock[0]=31;progress.report(force=True)
        self.assertIn('за последние 30 с сохранено 3 новых',output.getvalue())
        self.assertIn('за последние 30 с сохранено 0 новых',output.getvalue())
        self.assertEqual((progress.new,progress.context),(3,1))

    def test_model_wait_shows_cache_bytes_instead_of_claiming_download_percent(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'partial.incomplete').write_bytes(b'a'*(1024*1024))
            stopped=Mock();stopped.wait.side_effect=[False,True]
            with patch('tgreader.media.time.monotonic',return_value=20),contextlib.redirect_stdout(io.StringIO()) as output:
                model_wait_notice(stopped,root,10)
            self.assertIn('10 с',output.getvalue());self.assertIn('1.0 МБ',output.getvalue())
            self.assertNotIn('%',output.getvalue())

    def test_model_failure_is_preserved_without_100_download_retries_and_next_run_can_retry(self):
        constructor=Mock(side_effect=[ConnectionError('offline weights'),N(model='offline')])
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict(sys.modules,{'faster_whisper':N(WhisperModel=constructor)}), \
             patch.dict('os.environ',{}),contextlib.redirect_stdout(io.StringIO()) as output:
            processor=MediaProcessor(Path(temp),CFG)
            with self.assertRaisesRegex(ConnectionError,'offline weights'):processor.model_instance()
            self.assertIsNone(processor.model)
            with self.assertRaisesRegex(ConnectionError,'offline weights'):processor.model_instance()
            self.assertEqual(constructor.call_count,1)
            processor=MediaProcessor(Path(temp),CFG)
            model=processor.model_instance()
            self.assertIs(processor.model_instance(),model)
        self.assertEqual(constructor.call_count,2)
        self.assertIn('Не удалось подготовить',output.getvalue())
        self.assertIn('модель речи готова',output.getvalue())

    def test_only_public_token_advisory_is_replaced_not_errors_or_other_warnings(self):
        notice=PublicModelNotice()
        advisory='You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN'
        record=lambda level,text:logging.LogRecord('huggingface_hub',level,'',1,text,(),None)
        self.assertFalse(notice.filter(record(logging.WARNING,advisory)))
        self.assertTrue(notice.filter(record(logging.ERROR,advisory)))
        self.assertTrue(notice.filter(record(logging.WARNING,'Download retry failed')))


if __name__=='__main__':unittest.main()
