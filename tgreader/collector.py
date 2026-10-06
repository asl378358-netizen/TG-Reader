import asyncio
import datetime as dt
import hashlib
from zoneinfo import ZoneInfo
from telethon import utils
from telethon.errors import FloodWaitError
from telethon.tl import functions, types
from .common import UTC, iso, now
from .collection_flow import LOG, request_name

def media_kind(m):
    if isinstance(getattr(m,'media',None),types.MessageMediaPhoto):return 'photo'
    doc=getattr(m,'document',None)
    if not doc:return 'none'
    for a in getattr(doc,'attributes',[]):
        if isinstance(a,types.DocumentAttributeAudio) and a.voice:return 'voice'
        if isinstance(a,types.DocumentAttributeVideo):return 'round_video' if a.round_message else 'ordinary_video'
    if (getattr(doc,'mime_type','') or '').startswith('image/'):return 'photo'
    return 'other_file'

def topic_id(m,forum):
    action=getattr(m,'action',None)
    if isinstance(action,types.MessageActionTopicCreate):return m.id
    r=getattr(m,'reply_to',None)
    if r and getattr(r,'forum_topic',False):
        return getattr(r,'reply_to_top_id',None) or getattr(r,'reply_to_msg_id',None)
    return 1 if forum else None

def normalize(m,chat,topics,timezone):
    date=getattr(m,'date',None) or now()
    if date.tzinfo is None:date=date.replace(tzinfo=UTC)
    local=date.astimezone(ZoneInfo(timezone))
    sender=getattr(m,'sender',None)
    name=(' '.join(str(x) for x in [getattr(sender,'first_name',None),getattr(sender,'last_name',None)] if x)
          or getattr(sender,'title',None) or getattr(m,'post_author',None) or '')
    kind=media_kind(m);doc=getattr(m,'document',None);photo=getattr(m,'photo',None)
    media_id=getattr(doc,'id',None) or getattr(photo,'id',None) or 0
    fingerprint=hashlib.sha256(f'{media_id}:{kind}'.encode()).hexdigest()[:12]
    tid=topic_id(m,chat.get('forum',False))
    url=None
    if chat['peer_type']=='channel':
        suffix=f'{tid}/{m.id}' if tid else str(m.id)
        url=f'https://t.me/c/{chat["peer_id"]}/{suffix}'
    r=getattr(m,'reply_to',None)
    item={'chat_id':chat['chat_id'],'chat_title':chat['title'],'id':m.id,
          'date_utc':iso(date),'date_local':local.isoformat(),'day':local.date().isoformat(),
          'topic_id':tid,'topic_title':topics.get(tid),
          'sender_id':getattr(m,'sender_id',None),'sender_name':name,
          'sender_username':getattr(sender,'username',None),
          'text':getattr(m,'message',None) or '',
          'reply_to_message_id':getattr(r,'reply_to_msg_id',None),
          'reply_to_chat_id':utils.get_peer_id(r.reply_to_peer_id) if r and getattr(r,'reply_to_peer_id',None) else chat['chat_id'],
          'reply_quote_text':getattr(r,'quote_text',None),
          'reply_to_top_id':getattr(r,'reply_to_top_id',None),
          'grouped_id':getattr(m,'grouped_id',None),'source_url':url,
          'edited_utc':iso(getattr(m,'edit_date',None)),
          'media_kind':kind,'media_fingerprint':fingerprint,
          'media_size_bytes':getattr(doc,'size',None),
          'media_mime_type':getattr(doc,'mime_type',None),
          'collected_utc':iso(now())}
    if kind=='ordinary_video':item['media_note']='Обычное видео: сохранены подпись и ссылка; файл не скачивается.'
    if kind=='other_file':item['media_note']='Файл другого типа: сохранены текст и ссылка; файл не скачивается.'
    action=getattr(m,'action',None)
    if action:
        item['service_action']=type(action).__name__
        if isinstance(action,(types.MessageActionTopicCreate,types.MessageActionTopicEdit)):
            item['service_topic_title']=getattr(action,'title',None)
    urls=[]
    for e in getattr(m,'entities',None) or []:
        if isinstance(e,types.MessageEntityTextUrl):urls.append(e.url)
    item['embedded_urls']=urls
    forward=getattr(m,'fwd_from',None)
    if forward:item['forward']={'sender_name':getattr(forward,'from_name',None),'channel_post':getattr(forward,'channel_post',None),'date_utc':iso(getattr(forward,'date',None))}
    if isinstance(getattr(m,'media',None),types.MessageMediaPoll):
        poll=m.media.poll
        def text(v):return getattr(v,'text',v) if v else ''
        item['poll']={'question':text(poll.question),'answers':[text(a.text) for a in poll.answers],
                      'total_voters':getattr(m.media.results,'total_voters',None)}
    return item

def input_peer(chat):
    if chat['peer_type']=='channel':return types.InputPeerChannel(chat['peer_id'],chat['access_hash'])
    return types.InputPeerChat(chat['peer_id'])

async def fetch_topics(client,peer):
    result=[];seen=set();offset_id=0;offset_topic=0;offset_date=None
    while True:
        response=await client(functions.messages.GetForumTopicsRequest(peer=peer,offset_date=offset_date,
                         offset_id=offset_id,offset_topic=offset_topic,limit=100))
        batch=[t for t in response.topics if isinstance(t,types.ForumTopic)]
        if not response.topics:return result
        new=[t for t in batch if t.id not in seen]
        if not new:raise RuntimeError('Не удалось завершить пагинацию тем; каталог неполный.')
        for t in new:
            seen.add(t.id);result.append({'id':t.id,'title':t.title,'closed':bool(t.closed)})
        if len(seen)>=response.count:return result
        last=batch[-1]
        mapped={m.id:m for m in response.messages}
        offset_date=last.date if getattr(response,'order_by_create_date',False) else getattr(mapped.get(last.top_message),'date',last.date)
        offset_id=last.top_message;offset_topic=last.id

async def collect_chat(client,store,chat,cfg):
    cid=chat['chat_id'];peer=input_peer(chat);health=store.health(cid)
    health.update({'chat_id':cid,'title':chat['title'],'attempt_utc':iso(now()),'collection_error':None,'telegram_resume_utc':None})
    try:
        print(f'Читаем группу: {chat["title"]}.',flush=True)
        LOG.info('Group collection started cursor=%s forum=%s',store.cursor(cid),chat.get('forum',False))
        entity=await client.get_entity(peer)
        chat.update(title=entity.title,forum=bool(getattr(entity,'forum',False)))
        if chat['forum']:
            try:
                store.set_topics(cid,await fetch_topics(client,peer))
                health['topics_error']=None;health['topics_updated_utc']=iso(now())
            except FloodWaitError:raise
            except Exception as e:health['topics_error']=f'{type(e).__name__}: {e}'
        catalog={t['id']:t['title'] for t in store.topic_rows(cid)}
        if chat['forum'] and 1 not in catalog:catalog[1]='Общий чат'
        started=now()
        tip=await client.get_messages(peer,limit=1)
        high=tip[0].id if tip else 0
        cursor=store.cursor(cid)
        since=started-dt.timedelta(days=cfg.get('bootstrap_days',3))
        if 'tracking_started_utc' not in health:health['tracking_started_utc']=iso(since)
        count=0
        if high>cursor:
            async for m in client.iter_messages(peer,limit=None,min_id=cursor,max_id=high+1,
                    reverse=True,offset_date=since if not cursor else None,wait_time=1):
                if not getattr(m,'date',None):continue
                if not cursor and m.date<since:continue
                store.put(normalize(m,chat,catalog,cfg['timezone']),advance=True)
                count+=1
                if count % 50 == 0:
                    print(f'{chat["title"]}: сохранено {count} новых сообщений.',flush=True)
        # Empty windows must not repeatedly scan ancient history.
        if high and store.cursor(cid)<high:
            # Only after successful traversal: higher IDs might be service/deleted messages.
            with store.db:
                store.db.execute('INSERT INTO cursors VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET id=MAX(id,excluded.id)',(cid,high))
        # Capture parent chains in batches. Unfinished work stays durable for the next run.
        context_count=0
        for _ in range(5):
            missing=store.missing(cid,100)
            if not missing:break
            parents=await client.get_messages(peer,ids=missing)
            found=set()
            for m in parents:
                if m and getattr(m,'date',None):
                    found.add(m.id);store.put(normalize(m,chat,catalog,cfg['timezone']),context_only=True);context_count+=1
            for mid in set(missing)-found:store.unavailable_context(cid,mid)
        # Refresh older records for edits. Messages just read from history
        # already have their current text: rereading them doubles first-run RPCs.
        cutoff=now()-dt.timedelta(hours=cfg.get('edit_refresh_hours',48))
        recent=[]
        for row in store.db.execute('SELECT id,payload FROM messages WHERE chat_id=? AND day>=?',(cid,cutoff.astimezone(ZoneInfo(cfg['timezone'])).date().isoformat())):
            payload=__import__('json').loads(row['payload'])
            if row['id']<=cursor and payload['date_utc']>=iso(cutoff):recent.append(row['id'])
        for i in range(0,len(recent),100):
            updates=await client.get_messages(peer,ids=recent[i:i+100])
            for m in updates:
                if m and getattr(m,'date',None):store.put(normalize(m,chat,catalog,cfg['timezone']),context_only=True)
        health.update({'collection_ok_utc':iso(started),'captured_tip_id':high,'cursor_id':store.cursor(cid),
                       'new_messages_last_run':count,'context_added_last_run':context_count,
                       'tracking_started_utc':health['tracking_started_utc']})
        print(f'{chat["title"]}: новых сообщений {count}, контекст {context_count}',flush=True)
        LOG.info('Group collection finished new_messages=%s context=%s cursor=%s',count,context_count,store.cursor(cid))
    except FloodWaitError as e:
        health['collection_error']=f'{type(e).__name__}: {request_name(e.request)}; Telegram попросил подождать {e.seconds} секунд.'
        store.set_health(cid,health)
        raise
    except Exception as e:
        health['collection_error']=f'{type(e).__name__}: {e}'
        LOG.error('Group collection failed type=%s cursor=%s',type(e).__name__,store.cursor(cid))
        print(f'{chat["title"]}: ошибка чтения {type(e).__name__}',flush=True)
    store.set_health(cid,health)
    return health
