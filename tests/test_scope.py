import asyncio
import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import tkinter as tk
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock,patch
import zipfile

from telethon.tl import types
from tgreader.collector import collect_chat
from tgreader.common import iso,now
from tgreader.media import process_jobs
from tgreader.packets import export_day,export_all
from tgreader.scope import apply_queue_scope,history_days,media_options
from tgreader.scope_settings import ScopeSettings,edit_scope,output_directory
from tgreader.store import Store
from tgreader.wizard import chat_records
from test_reader import CHAT,CFG,FakeClient,message,raw
from test_collection_flow import client_for,virtual_wait


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'messages.db')
    def tearDown(self):self.store.close();self.tmp.cleanup()

    def test_legacy_job_schema_migrates_without_losing_ready_results(self):
        path=self.root/'legacy.db';db=sqlite3.connect(path)
        db.execute('CREATE TABLE jobs(chat_id INTEGER,id INTEGER,fingerprint TEXT,status TEXT,result TEXT,error TEXT,attempts INTEGER DEFAULT 0,PRIMARY KEY(chat_id,id))')
        db.execute('INSERT INTO jobs VALUES(?,?,?, ?,?,NULL,2)',(CHAT['chat_id'],1,'offline','done','{"assets":["ready.jpg"]}'))
        db.commit();db.close();legacy=Store(path)
        try:
            job=legacy.job(CHAT['chat_id'],1)
            self.assertEqual(job['enabled'],1);self.assertEqual(job['status'],'done')
            self.assertEqual(job['result']['assets'],['ready.jpg'])
        finally:legacy.close()

    def test_options_are_backward_compatible_and_period_is_bounded(self):
        self.assertEqual(history_days({}),3);self.assertTrue(all(media_options(CHAT).values()))
        for value in (0,-1,91):
            with self.assertRaises(ValueError):history_days({'history_days':value})

    def test_disabled_and_old_jobs_do_not_make_any_telegram_request(self):
        self.store.put(raw(1,kind='photo'))
        self.store.put(raw(2,kind='voice',date=now()-dt.timedelta(days=20)))
        client=N(get_messages=AsyncMock(side_effect=AssertionError('No Telegram request expected')))
        chat={**CHAT,'media':{'photo':False,'voice':True,'round_video':False}}
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(process_jobs(client,self.store,[chat],self.root,{**CFG,'output_dir':str(self.root/'output')}))
        client.get_messages.assert_not_called();self.assertFalse(self.store.pending_jobs([CHAT['chat_id']]))
        self.assertEqual(self.store.job(CHAT['chat_id'],1)['status'],'pending')

    def test_reenable_uses_existing_finished_result(self):
        self.store.put(raw(1,kind='photo'));self.store.finish_job(CHAT['chat_id'],1,result={'assets':['ready.jpg']})
        disabled={**CHAT,'media':{'photo':False}}
        apply_queue_scope(self.store,[disabled],CFG);self.assertEqual(self.store.job(CHAT['chat_id'],1)['enabled'],0)
        apply_queue_scope(self.store,[CHAT],CFG);job=self.store.job(CHAT['chat_id'],1)
        self.assertEqual(job['status'],'done');self.assertEqual(job['result']['assets'],['ready.jpg'])
        self.assertFalse(self.store.pending_jobs([CHAT['chat_id']]))

    def test_voice_and_circle_switches_are_independent(self):
        for mid,kind in enumerate(('photo','voice','round_video'),1):self.store.put(raw(mid,kind=kind))
        apply_queue_scope(self.store,[{**CHAT,'media':{'photo':False,'voice':True,'round_video':False}}],CFG)
        self.assertEqual([job['id'] for job in self.store.pending_jobs([CHAT['chat_id']])],[2])

    def test_old_reply_text_is_kept_without_queuing_its_voice(self):
        client=FakeClient(count=2);client.items=[message(1,date=now()-dt.timedelta(days=20)),message(2,parent=1)]
        client.items[0].document=N(id=123,attributes=[types.DocumentAttributeAudio(3,voice=True)],mime_type='audio/ogg')
        with contextlib.redirect_stdout(io.StringIO()):asyncio.run(collect_chat(client,self.store,dict(CHAT),CFG))
        self.assertEqual(len(self.store.day_messages(CHAT['chat_id'],raw(2)['day'])),1)
        self.assertEqual(self.store.get(CHAT['chat_id'],1)['text'],'Сообщение 1')
        self.assertEqual(self.store.job(CHAT['chat_id'],1)['enabled'],0)

    def test_expanding_window_backfills_only_missing_records_without_resetting_durable_cursor(self):
        client=FakeClient(count=3);client.items=[message(1,date=now()-dt.timedelta(days=10)),
            message(2,date=now()-dt.timedelta(days=5)),message(3)]
        with contextlib.redirect_stdout(io.StringIO()):
            initial=asyncio.run(collect_chat(client,self.store,dict(CHAT),CFG))
            expanded=asyncio.run(collect_chat(client,self.store,dict(CHAT),{**CFG,'history_days':7}))
        self.assertEqual(initial['new_messages_last_run'],1);self.assertEqual(expanded['new_messages_last_run'],1)
        self.assertEqual(self.store.cursor(CHAT['chat_id']),3);self.assertIsNone(self.store.get(CHAT['chat_id'],1))
        self.assertIsNotNone(self.store.get(CHAT['chat_id'],2));self.assertEqual(client.iter_calls[-1]['min_id'],0)
        self.assertIsNotNone(client.iter_calls[-1]['offset_date'])

    def test_restart_after_long_gap_starts_at_window_instead_of_old_cursor(self):
        self.store.put(raw(1,date=now()-dt.timedelta(days=40)),advance=True)
        client=FakeClient(count=3);client.items=[message(1,date=now()-dt.timedelta(days=40)),
            message(2,date=now()-dt.timedelta(days=20)),message(3)]
        with contextlib.redirect_stdout(io.StringIO()):asyncio.run(collect_chat(client,self.store,dict(CHAT),CFG))
        self.assertEqual(client.iter_calls[0]['min_id'],0);self.assertIsNotNone(client.iter_calls[0]['offset_date'])
        self.assertIsNone(self.store.get(CHAT['chat_id'],2));self.assertEqual(self.store.cursor(CHAT['chat_id']),3)

    def test_real_telethon_iterator_uses_date_boundary_and_reads_all_newer_pages(self):
        instant=now();channel=types.Channel(CHAT['peer_id'],CHAT['title'],types.ChatPhotoEmpty(),instant,
            megagroup=True,access_hash=CHAT['access_hash'])
        from telethon.tl import functions
        messages=[types.Message(i,types.PeerChannel(CHAT['peer_id']),
            instant-dt.timedelta(days=10 if i<=100 else 1),f'Offline {i}') for i in range(1,302)]
        history=[]
        def respond(request):
            if isinstance(request,functions.channels.GetChannelsRequest):return types.messages.Chats([channel])
            if isinstance(request,functions.messages.GetHistoryRequest):
                history.append(copy.copy(request))
                if request.add_offset<0:
                    candidates=[m for m in messages if m.id>=request.offset_id and
                                (not request.offset_date or m.date>=request.offset_date)]
                    batch=candidates[:request.limit]
                else:batch=messages[-request.limit:]
                return types.messages.MessagesSlice(len(messages),list(reversed(batch)),[],[channel],[])
            raise AssertionError(type(request).__name__)
        client=client_for(respond)
        with virtual_wait(),contextlib.redirect_stdout(io.StringIO()):
            result=asyncio.run(collect_chat(client,self.store,dict(CHAT),CFG))
        pages=[request for request in history if request.add_offset<0]
        self.assertIsNotNone(pages[0].offset_date);self.assertEqual(pages[0].offset_id,0)
        self.assertEqual(result['new_messages_last_run'],201);self.assertIsNone(result['collection_error'])
        self.assertIsNone(self.store.get(CHAT['chat_id'],100));self.assertEqual(self.store.cursor(CHAT['chat_id']),301)
        self.assertGreaterEqual(len(pages),3)

    def test_text_only_export_excludes_previously_processed_photo_but_keeps_caption(self):
        item=raw(1,kind='photo');self.store.put(item)
        image=self.root/'ready.jpg';image.write_bytes(b'offline photo')
        self.store.finish_job(CHAT['chat_id'],1,result={'assets':['ready.jpg']})
        chat={**CHAT,'media':{'photo':False}}
        out=self.root/'out';out.mkdir();summary=export_day(self.store,chat,item['day'],self.root,out,CFG)
        self.assertEqual(summary['media_unfinished_count'],0)
        with zipfile.ZipFile(out/summary['parts'][0]['file_name']) as archive:
            self.assertEqual(archive.namelist(),['packet.json']);packet=json.loads(archive.read('packet.json'))
        self.assertEqual(packet['messages'][0]['text'],item['text'])
        self.assertEqual(packet['messages'][0]['media']['status'],'skipped');self.assertTrue(image.exists())

    def test_disabled_backlog_does_not_mark_status_as_unfinished(self):
        self.store.put(raw(1,kind='photo'))
        chat={**CHAT,'media':{'photo':False}}
        status=export_all(self.store,[chat],self.root,{**CFG,'output_dir':str(self.root/'out')})
        self.assertEqual(status['chats'][0]['unfinished_media_count'],0)

    def test_unread_empty_day_does_not_create_a_misleading_archive(self):
        out=self.root/'out';out.mkdir()
        day=export_day(self.store,CHAT,now().date().isoformat(),self.root,out,CFG)
        self.assertEqual(day['messages_count'],0);self.assertFalse(day['full_day_collected'])
        self.assertEqual(day['parts'],[]);self.assertFalse(list(out.iterdir()))

    def test_group_reselection_preserves_media_preferences(self):
        old={**CHAT,'media':{'photo':False,'voice':True,'round_video':False}}
        dialog=N(id=CHAT['chat_id'],name='Renamed',entity=N(id=CHAT['peer_id'],access_hash=2,forum=False))
        result=chat_records([dialog],[old])[0]
        self.assertEqual(result['media'],old['media']);self.assertEqual(result['title'],'Renamed')

    def test_cancel_settings_keeps_config_byte_for_byte(self):
        path=self.root/'config.json';original=b'{"output_dir":"offline","chats":[]}'
        path.write_bytes(original)
        with patch('tgreader.scope_settings.load_config',return_value={}), \
             patch('tgreader.scope_settings.configure_scope',return_value=None), \
             patch('tgreader.scope_settings.state_dir',return_value=self.root),contextlib.redirect_stdout(io.StringIO()):edit_scope()
        self.assertEqual(path.read_bytes(),original)

    def test_destination_rejects_tdata_and_does_not_add_duplicate_folder(self):
        with patch('tgreader.scope_settings.state_dir',return_value=self.root/'state'):
            with self.assertRaises(ValueError):output_directory(self.root/'tdata')
            with self.assertRaises(ValueError):output_directory(self.root/'state'/'out')
            result=output_directory(self.root/'drive'/'TelegramDailyReader')
            self.assertEqual(result,self.root/'drive'/'TelegramDailyReader')
            self.assertFalse(result.exists())


class ScopeWindowTests(unittest.TestCase):
    def setUp(self):
        try:self.root=tk.Tk()
        except tk.TclError:self.skipTest('No display for real Tk window')
    def tearDown(self):
        if hasattr(self,'root') and self.root.winfo_exists():self.root.destroy()

    def test_real_table_keeps_switches_when_search_hides_group_and_save_preserves_account(self):
        cfg={**CFG,'chats':[dict(CHAT),{**CHAT,'chat_id':-100888,'title':'News'}],
             'auth_file':'keep-session.enc','output_dir':'offline-output'}
        original=copy.deepcopy(cfg);picker=ScopeSettings(self.root,cfg);self.root.update()
        picker.toggle(CHAT['chat_id'],'photo');picker.toggle(CHAT['chat_id'],'round_video')
        picker.query.set('News');self.root.update()
        self.assertEqual(len(picker.tree.get_children()),1)
        picker.days.set('1');picker.query.set('');self.root.update()
        self.assertIn('—',picker.tree.item(str(CHAT['chat_id']),'values'))
        self.assertFalse(picker.options[CHAT['chat_id']]['photo'])
        with patch.object(self.root,'destroy'):picker.save()
        self.assertEqual(picker.answer['history_days'],1);self.assertEqual(picker.answer['auth_file'],'keep-session.enc')
        self.assertTrue(picker.answer['chats'][0]['media']['voice']);self.assertEqual(cfg,original)

    def test_real_controls_change_voice_without_changing_photo_or_text(self):
        picker=ScopeSettings(self.root,{**CFG,'chats':[dict(CHAT)],'output_dir':'offline'})
        iid=str(CHAT['chat_id']);picker.tree.selection_set(iid);picker.tree.focus(iid);self.root.update()
        picker.flags['voice'].set(False);picker.set_selected('voice');self.root.update()
        self.assertFalse(picker.options[CHAT['chat_id']]['voice']);self.assertTrue(picker.options[CHAT['chat_id']]['photo'])
        self.assertEqual(picker.tree.item(iid,'values')[1],'Всегда')


if __name__=='__main__':unittest.main()
