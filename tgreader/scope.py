"""The chosen time window and media permissions, independent of Telegram IDs."""
import datetime as dt
import json

from .common import UTC,now

MEDIA_LABELS={'photo':'Фото','voice':'Голосовые','round_video':'Кружочки'}
PERIODS=(1,3,7,14,30,90)


def history_days(cfg):
    days=int(cfg.get('history_days',cfg.get('bootstrap_days',3)))
    if not 1<=days<=90:raise ValueError('Период чтения должен быть от 1 до 90 суток.')
    return days


def cutoff(cfg,at=None):
    return (at or now())-dt.timedelta(days=history_days(cfg))


def media_options(chat):
    saved=chat.get('media',{})
    return {kind:bool(saved.get(kind,True)) for kind in MEDIA_LABELS}


def media_allowed(chat,message,cfg,at=None):
    kind=message.get('media_kind')
    if kind not in MEDIA_LABELS or not media_options(chat)[kind]:return False
    value=message.get('date_utc')
    if not value:return False
    stamp=dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=UTC)
    return stamp>=cutoff(cfg,at)


def apply_queue_scope(store,chats,cfg,at=None):
    updates=[]
    for chat in chats:
        for row in store.db.execute('SELECT j.id,j.enabled,m.payload FROM jobs j JOIN messages m '
                                   'ON m.chat_id=j.chat_id AND m.id=j.id WHERE j.chat_id=?',(chat['chat_id'],)):
            enabled=int(media_allowed(chat,json.loads(row['payload']),cfg,at))
            if row['enabled']!=enabled:updates.append((enabled,chat['chat_id'],row['id']))
    with store.db:
        store.db.executemany('UPDATE jobs SET enabled=? WHERE chat_id=? AND id=?',updates)
