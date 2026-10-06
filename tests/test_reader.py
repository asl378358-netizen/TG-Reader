import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch
import zipfile
from telethon.tl import types
from telethon.errors import FloodWaitError
from PIL import Image
from tgreader.common import UTC,iso,now
from tgreader.collector import collect_chat,fetch_topics,media_kind,normalize,topic_id
from tgreader.store import Store
from tgreader.packets import export_day,write_zip,MAX_ARCHIVE_BYTES
from tgreader.media import extract_frames,MediaProcessor,process_jobs

CHAT={'chat_id':-100999,'peer_type':'channel','peer_id':999,'access_hash':1,'forum':False,'title':'Test group'}
CFG={'timezone':'Europe/Berlin','bootstrap_days':3,'max_media_jobs_per_run':100}

def message(mid,parent=None,date=None):
    return N(id=mid,date=date or now()-dt.timedelta(hours=1),message=f'Сообщение {mid}',media=None,document=None,
             photo=None,sender_id=10,sender=N(first_name='Тест',last_name='Участник',username=None),
             reply_to=types.MessageReplyHeader(reply_to_msg_id=parent) if parent else None,
             action=None,edit_date=None,entities=None,grouped_id=None,fwd_from=None,post_author=None)

def raw(mid,parent=None,date=None,kind='none'):
    item=normalize(message(mid,parent,date),CHAT,{},CFG['timezone'])
    item['media_kind']=kind;item['media_fingerprint']=f'fingerprint_{mid}'
    return item

class FakeClient:
    def __init__(self,count=600,fail_after=None):
        self.items=[message(i) for i in range(1,count+1)];self.fail_after=fail_after;self.iter_calls=[]
    async def get_entity(self,peer):return N(title='Test group',forum=False)
    async def get_messages(self,peer,limit=None,ids=None):
        if limit is not None:return self.items[-limit:]
        if isinstance(ids,int):return next((m for m in self.items if m.id==ids),None)
        mapped={m.id:m for m in self.items};return [mapped.get(i) for i in ids]
    async def iter_messages(self,peer,**kw):
        self.iter_calls.append(kw)
        for n,m in enumerate([m for m in self.items if kw['min_id']<m.id<kw['max_id']]):
            if self.fail_after is not None and n==self.fail_after:raise ConnectionError('simulated interruption')
            yield m

class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'messages.db')
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def test_all_600_messages_without_truncation(self):
        c=FakeClient();h=asyncio.run(collect_chat(c,self.store,dict(CHAT),CFG))
        self.assertEqual(self.store.cursor(CHAT['chat_id']),600)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages WHERE context_only=0').fetchone()[0],600)
        self.assertEqual(h['new_messages_last_run'],600)
        self.assertIsNone(c.iter_calls[0]['limit'])
    def test_resume_after_interruption_without_losing_messages(self):
        h=asyncio.run(collect_chat(FakeClient(fail_after=250),self.store,dict(CHAT),CFG))
        self.assertTrue(h['collection_error']);self.assertEqual(self.store.cursor(CHAT['chat_id']),250)
        c=FakeClient();h=asyncio.run(collect_chat(c,self.store,dict(CHAT),CFG))
        self.assertEqual(c.iter_calls[0]['min_id'],250)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],600)
        self.assertIsNone(h['collection_error'])
    def test_repeat_preserves_ready_media_and_stable_timestamp(self):
        first=raw(1,kind='voice');self.store.put(first,advance=True)
        self.store.finish_job(CHAT['chat_id'],1,result={'transcription':{'text':'тест'}})
        second=dict(first,collected_utc=iso(now()))
        self.store.put(second,advance=True,context_only=True)
        self.assertEqual(self.store.job(CHAT['chat_id'],1)['status'],'done')
        self.assertEqual(self.store.get(CHAT['chat_id'],1)['collected_utc'],first['collected_utc'])
        self.assertEqual(len(self.store.day_messages(CHAT['chat_id'],first['day'])),1)
    def test_edit_refresh_keeps_old_edits_without_rereading_new_history(self):
        client=FakeClient(count=2)
        with patch.object(client,'get_messages',wraps=client.get_messages) as requests:
            asyncio.run(collect_chat(client,self.store,dict(CHAT),CFG))
            self.assertFalse(any(call.kwargs.get('ids') for call in requests.call_args_list))
        client.items[0].message='Updated old message'
        client.items.append(message(3))
        with patch.object(client,'get_messages',wraps=client.get_messages) as requests:
            asyncio.run(collect_chat(client,self.store,dict(CHAT),CFG))
            refresh=[call.kwargs['ids'] for call in requests.call_args_list if call.kwargs.get('ids')]
        self.assertEqual(refresh,[[1,2]])
        self.assertEqual(self.store.get(CHAT['chat_id'],1)['text'],'Updated old message')
        self.assertEqual(self.store.cursor(CHAT['chat_id']),3)
    def test_changed_media_requeues_without_dropping_text(self):
        m=raw(1,kind='photo');self.store.put(m,advance=True);self.store.finish_job(CHAT['chat_id'],1,result={'assets':[]})
        m['media_fingerprint']='changed';m['text']='изменено';self.store.put(m)
        self.assertEqual(self.store.job(CHAT['chat_id'],1)['status'],'pending')
        self.assertEqual(self.store.cursor(CHAT['chat_id']),1)
        m['media_kind']='ordinary_video';self.store.put(m)
        self.assertIsNone(self.store.job(CHAT['chat_id'],1))
    def test_topic_top_id_distinct_from_reply_parent(self):
        m=message(400);m.reply_to=types.MessageReplyHeader(forum_topic=True,reply_to_top_id=7,reply_to_msg_id=399)
        item=normalize(m,{**CHAT,'forum':True},{7:'Вопросы к команде'},CFG['timezone'])
        self.assertEqual(item['topic_id'],7);self.assertEqual(item['reply_to_message_id'],399)
        self.assertEqual(item['topic_title'],'Вопросы к команде')
        self.assertTrue(item['source_url'].endswith('/7/400'))
        m.reply_to=types.MessageReplyHeader(forum_topic=True,reply_to_msg_id=7)
        self.assertEqual(topic_id(m,True),7)
    def test_round_video_distinct_from_ordinary_video(self):
        m=message(1);m.document=N(attributes=[types.DocumentAttributeVideo(60,480,480,round_message=True)],mime_type='video/mp4')
        self.assertEqual(media_kind(m),'round_video')
        m.document.attributes[0].round_message=False
        self.assertEqual(media_kind(m),'ordinary_video')
    def test_forum_catalog_paginates_past_100_topics(self):
        def topic(i):return types.ForumTopic(i,now(),types.PeerChannel(999),f'Тема {i}',0,i,0,0,0,0,0,0,types.PeerUser(10),types.PeerNotifySettings())
        topics=[topic(i) for i in range(1,202)];calls=[]
        async def invoke(req):
            calls.append(req);batch=topics[req.offset_topic:req.offset_topic+80]
            return N(topics=batch,count=201,messages=[message(t.top_message) for t in batch],order_by_create_date=False)
        output=asyncio.run(fetch_topics(invoke,types.InputPeerChannel(999,1)))
        self.assertEqual(len(output),201);self.assertEqual(len(calls),3)
        self.assertEqual(calls[1].offset_topic,80)
    def test_context_from_previous_day_and_missing_parent_marked(self):
        question=raw(1,date=now()-dt.timedelta(days=1));answer=raw(2,parent=1)
        self.store.put(question);self.store.put(answer,advance=True)
        chain,issue=self.store.context_chain(CHAT['chat_id'],2)
        self.assertEqual(chain[0]['id'],1);self.assertIsNone(issue)
        self.store.put(raw(3,parent=99999))
        self.assertEqual(self.store.context_chain(CHAT['chat_id'],3)[1],'missing:99999')
    def test_cross_chat_reply_never_attaches_unrelated_local_message(self):
        self.store.put(raw(10))
        m=message(20);m.reply_to=types.MessageReplyHeader(reply_to_msg_id=10,reply_to_peer_id=types.PeerChannel(777),quote_text='Цитата из другой группы')
        item=normalize(m,CHAT,{},CFG['timezone']);self.store.put(item)
        chain,issue=self.store.context_chain(CHAT['chat_id'],20)
        self.assertEqual(chain,[]);self.assertTrue(issue.startswith('cross_chat:'))
        self.assertEqual(self.store.missing(CHAT['chat_id']),[])
        self.assertEqual(item['reply_quote_text'],'Цитата из другой группы')
    def test_parent_media_queue_visible_in_manifest(self):
        parent=raw(1,date=now()-dt.timedelta(days=1),kind='voice')
        answer=raw(2,parent=1);self.store.put(parent,context_only=True);self.store.put(answer)
        out=self.root/'output';out.mkdir()
        result=export_day(self.store,CHAT,answer['day'],self.root,out,CFG)
        self.assertEqual(result['media_unfinished_count'],0)
        self.assertEqual(result['context_media_unfinished_count'],1)
    def test_600_records_split_into_three_complete_parts(self):
        for i in range(1,601):self.store.put(raw(i),advance=True)
        output=self.root/'output';output.mkdir()
        result=export_day(self.store,CHAT,raw(1)['day'],self.root,output,CFG)
        ids=[]
        for part in result['parts']:
            with zipfile.ZipFile(output/part['file_name']) as z:ids.extend(m['id'] for m in json.loads(z.read('packet.json'))['messages'])
        self.assertEqual(len(result['parts']),3);self.assertEqual(ids,list(range(1,601)))
        self.assertFalse(result['full_day_collected'])
    def test_large_assets_split_with_all_references_preserved(self):
        output=self.root/'output';output.mkdir()
        for i in (1,2):
            p=self.root/f'asset{i}.jpg';p.write_bytes(os.urandom(13*1024*1024))
            self.store.put(raw(i,kind='photo'));self.store.finish_job(CHAT['chat_id'],i,result={'assets':[p.name]})
        result=export_day(self.store,CHAT,raw(1)['day'],self.root,output,CFG)
        self.assertEqual(len(result['parts']),2)
        names=[];ids=[]
        for part in result['parts']:
            self.assertLessEqual(part['size_bytes'],MAX_ARCHIVE_BYTES)
            with zipfile.ZipFile(output/part['file_name']) as z:
                names+=z.namelist();ids.extend(m['id'] for m in json.loads(z.read('packet.json'))['messages'])
        self.assertEqual(ids,[1,2]);self.assertIn('asset1.jpg',names);self.assertIn('asset2.jpg',names)
    def test_zip_path_escape_rejected(self):
        with self.assertRaises(ValueError):write_zip(self.root/'out.zip',{},['../secret'],self.root)
    def test_berlin_dst_day_is_25_hours(self):
        self.store.set_health(CHAT['chat_id'],{'tracking_started_utc':'2026-10-01T00:00:00+00:00','collection_ok_utc':'2026-10-26T12:00:00+00:00'})
        output=self.root/'output';output.mkdir()
        day=export_day(self.store,CHAT,'2026-10-25',self.root,output,CFG)
        duration=dt.datetime.fromisoformat(day['coverage_end_utc'])-dt.datetime.fromisoformat(day['coverage_start_utc'])
        self.assertEqual(duration.total_seconds(),25*3600);self.assertTrue(day['full_day_collected'])
    def test_synthetic_circle_frames_include_start_and_end(self):
        import av
        path=self.root/'round.mp4'
        with av.open(str(path),'w') as c:
            stream=c.add_stream('mpeg4',rate=1);stream.width=64;stream.height=64;stream.pix_fmt='yuv420p'
            for i in range(20):
                frame=av.VideoFrame.from_image(Image.new('RGB',(64,64),(i*10,0,0)))
                for p in stream.encode(frame):c.mux(p)
            for p in stream.encode():c.mux(p)
        frames=extract_frames(path,self.root/'frames')
        self.assertEqual(len(frames),3);self.assertEqual(frames[0]['seconds'],0);self.assertGreaterEqual(frames[-1]['seconds'],18)
        self.assertTrue(all(Path(f['path']).exists() for f in frames))
        result=MediaProcessor(self.root,CFG).process_file(path,'round_video',CHAT['chat_id'],1,'synthetic')
        self.assertEqual(len(result['assets']),3)
        self.assertEqual(result['transcription']['text'],'')
        self.assertIn('нет аудиодорожки',result['transcription']['note'])
    def test_failed_transcription_retains_original_and_retries(self):
        src=self.root/'source.ogg';src.write_bytes(b'original-voice')
        m=message(1);m.document=N(attributes=[types.DocumentAttributeAudio(3,voice=True)],mime_type='audio/ogg')
        self.store.put(raw(1,kind='voice'),advance=True)
        class Client:
            async def get_messages(self,peer,ids):return m
            async def download_media(self,m,file):Path(file).write_bytes(src.read_bytes());return file
        cfg={**CFG,'output_dir':str(self.root/'output')}
        with patch.object(MediaProcessor,'transcribe',side_effect=RuntimeError('offline model')):
            asyncio.run(process_jobs(Client(),self.store,[CHAT],self.root,cfg))
        job=self.store.job(CHAT['chat_id'],1)
        self.assertEqual(job['status'],'error')
        self.assertTrue((Path(cfg['output_dir'])/job['result']['original_relative_path']).exists())
        self.assertEqual(self.store.cursor(CHAT['chat_id']),1)
        with patch.object(MediaProcessor,'transcribe',return_value={'text':'распознано'}):
            asyncio.run(process_jobs(Client(),self.store,[CHAT],self.root,cfg))
        self.assertEqual(self.store.job(CHAT['chat_id'],1)['status'],'done')
    def test_media_flood_wait_stops_other_requests_and_keeps_queue(self):
        for mid in (1,2):self.store.put(raw(mid,kind='voice'))
        class Client:
            calls=0
            async def get_messages(s,peer,ids):
                s.calls+=1;raise FloodWaitError(request=None,capture=7200)
        client=Client()
        with self.assertRaises(FloodWaitError):asyncio.run(process_jobs(client,self.store,[CHAT],self.root,{**CFG,'output_dir':str(self.root/'output')}))
        self.assertEqual(client.calls,1)
        self.assertEqual(self.store.job(CHAT['chat_id'],1)['status'],'error')
        self.assertEqual(self.store.job(CHAT['chat_id'],2)['status'],'pending')
    def test_account_cooldown_survives_restart_and_skips_connection(self):
        import app
        cfg={**CFG,'chats':[CHAT],'api_id':1,'api_hash':'0'*32,'output_dir':str(self.root/'output')}
        (self.root/'config.json').write_text(json.dumps(cfg),encoding='utf-8')
        class Client:
            async def connect(s):raise FloodWaitError(request=None,capture=7200)
            async def disconnect(s):pass
        with patch('tgreader.common.state_dir',return_value=self.root),patch('app.state_dir',return_value=self.root),patch('tgreader.auth.create_client',return_value=Client()) as factory:
            asyncio.run(app.collect())
            self.assertTrue((self.root/'telegram_cooldown.json').exists())
            factory.reset_mock();asyncio.run(app.collect());factory.assert_not_called()
        self.assertIn('паузу',json.loads((self.root/'output'/'latest_status.json').read_text(encoding='utf-8'))['chats'][0]['collection_error'])

if __name__=='__main__':unittest.main()
