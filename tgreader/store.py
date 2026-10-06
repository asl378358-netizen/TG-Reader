import json
import sqlite3
from pathlib import Path
from .common import iso, now

class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER, id INTEGER, day TEXT, context_only INTEGER,
            payload TEXT NOT NULL, PRIMARY KEY(chat_id,id));
        CREATE INDEX IF NOT EXISTS messages_day ON messages(chat_id,day);
        CREATE TABLE IF NOT EXISTS cursors(chat_id INTEGER PRIMARY KEY, id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(chat_id INTEGER, id INTEGER, fingerprint TEXT,
            status TEXT, result TEXT, error TEXT, attempts INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id,id));
        CREATE TABLE IF NOT EXISTS topics(chat_id INTEGER, id INTEGER, title TEXT,
            closed INTEGER, PRIMARY KEY(chat_id,id));
        CREATE TABLE IF NOT EXISTS health(chat_id INTEGER PRIMARY KEY, payload TEXT);
        CREATE TABLE IF NOT EXISTS missing_context(chat_id INTEGER, id INTEGER,
            status TEXT DEFAULT 'pending', PRIMARY KEY(chat_id,id));
        ''')
        if 'enabled' not in {row['name'] for row in self.db.execute('PRAGMA table_info(jobs)')}:
            with self.db:self.db.execute('ALTER TABLE jobs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')

    def cursor(self, chat_id):
        row = self.db.execute('SELECT id FROM cursors WHERE chat_id=?', (chat_id,)).fetchone()
        return row['id'] if row else 0

    def put(self, m, advance=False, context_only=False, media_enabled=True):
        cid, mid = m['chat_id'], m['id']
        with self.db:
            old = self.db.execute('SELECT context_only,payload FROM messages WHERE chat_id=? AND id=?', (cid,mid)).fetchone()
            context_only = bool(context_only and (old is None or old['context_only']))
            if old:
                previous=json.loads(old['payload'])
                comparable_old={k:v for k,v in previous.items() if k!='collected_utc'}
                comparable_new={k:v for k,v in m.items() if k!='collected_utc'}
                if comparable_old==comparable_new:m['collected_utc']=previous.get('collected_utc')
            self.db.execute('INSERT INTO messages VALUES(?,?,?,?,?) ON CONFLICT(chat_id,id) DO UPDATE SET day=excluded.day,context_only=excluded.context_only,payload=excluded.payload',
                            (cid,mid,m['day'],int(context_only),json.dumps(m,ensure_ascii=False)))
            fingerprint = m.get('media_fingerprint')
            if m.get('media_kind') in ('photo','voice','round_video'):
                self.db.execute('''INSERT INTO jobs(chat_id,id,fingerprint,status,enabled) VALUES(?,?,?,'pending',?)
                ON CONFLICT(chat_id,id) DO UPDATE SET
                  status=CASE WHEN fingerprint!=excluded.fingerprint THEN 'pending' ELSE status END,
                  result=CASE WHEN fingerprint!=excluded.fingerprint THEN NULL ELSE result END,
                  error=CASE WHEN fingerprint!=excluded.fingerprint THEN NULL ELSE error END,
                  fingerprint=excluded.fingerprint,enabled=excluded.enabled''', (cid,mid,fingerprint,int(media_enabled)))
            else:
                self.db.execute('DELETE FROM jobs WHERE chat_id=? AND id=?',(cid,mid))
            parent = m.get('reply_to_message_id')
            if parent and parent != mid and m.get('reply_to_chat_id',cid)==cid and self.get(cid,parent) is None:
                self.db.execute('INSERT OR IGNORE INTO missing_context(chat_id,id) VALUES(?,?)', (cid,parent))
            self.db.execute('DELETE FROM missing_context WHERE chat_id=? AND id=?', (cid,mid))
            if advance:
                self.db.execute('INSERT INTO cursors VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET id=MAX(id,excluded.id)', (cid,mid))
        return old is None or bool(old['context_only'] and not context_only)

    def get(self, cid, mid):
        row = self.db.execute('SELECT payload FROM messages WHERE chat_id=? AND id=?', (cid,mid)).fetchone()
        return json.loads(row['payload']) if row else None

    def day_messages(self, cid, day):
        return [json.loads(r['payload']) for r in self.db.execute('SELECT payload FROM messages WHERE chat_id=? AND day=? AND context_only=0 ORDER BY id', (cid,day))]

    def topic_rows(self, cid):
        return [dict(r) for r in self.db.execute('SELECT id,title,closed FROM topics WHERE chat_id=? ORDER BY id', (cid,))]

    def set_topics(self, cid, topics):
        with self.db:
            for topic in topics:
                self.db.execute('INSERT INTO topics VALUES(?,?,?,?) ON CONFLICT(chat_id,id) DO UPDATE SET title=excluded.title,closed=excluded.closed',
                                (cid,topic['id'],topic['title'],int(topic.get('closed',False))))

    def set_health(self, cid, payload):
        with self.db:
            self.db.execute('INSERT INTO health VALUES(?,?) ON CONFLICT(chat_id) DO UPDATE SET payload=excluded.payload', (cid,json.dumps(payload,ensure_ascii=False)))

    def health(self, cid):
        r=self.db.execute('SELECT payload FROM health WHERE chat_id=?',(cid,)).fetchone()
        return json.loads(r['payload']) if r else {}

    def pending_jobs(self, allowed_ids, limit=100):
        if not allowed_ids: return []
        slots=','.join('?' for _ in allowed_ids)
        # Failed jobs retry on the next run. A per-run budget never advances their status to done.
        return [dict(r) for r in self.db.execute(f"SELECT * FROM jobs WHERE chat_id IN ({slots}) AND status!='done' AND enabled=1 ORDER BY attempts,id LIMIT ?", [*allowed_ids,limit])]

    def finish_job(self, cid, mid, result=None, error=None):
        with self.db:
            self.db.execute('UPDATE jobs SET status=?,result=?,error=?,attempts=attempts+1 WHERE chat_id=? AND id=?',
                            ('error' if error else 'done', json.dumps(result,ensure_ascii=False) if result is not None else None, error,cid,mid))

    def job(self,cid,mid):
        r=self.db.execute('SELECT * FROM jobs WHERE chat_id=? AND id=?',(cid,mid)).fetchone()
        if not r:return None
        row=dict(r);row['result']=json.loads(row['result']) if row['result'] else None
        return row

    def context_chain(self, cid, mid, max_depth=20):
        result=[];seen={mid};current=self.get(cid,mid)
        while current and current.get('reply_to_message_id') and len(result)<max_depth:
            if current.get('reply_to_chat_id',cid)!=cid:
                return result,f'cross_chat:{current["reply_to_chat_id"]}:{current["reply_to_message_id"]}'
            pid=current['reply_to_message_id']
            if pid in seen:return result, 'cycle'
            seen.add(pid);current=self.get(cid,pid)
            if not current:return result, f'missing:{pid}'
            result.append(current)
        return result, ('depth_limit' if current and current.get('reply_to_message_id') else None)

    def missing(self,cid,limit=100):
        return [r['id'] for r in self.db.execute("SELECT id FROM missing_context WHERE chat_id=? AND status='pending' ORDER BY id LIMIT ?",(cid,limit))]

    def unavailable_context(self,cid,mid):
        with self.db:self.db.execute("UPDATE missing_context SET status='unavailable' WHERE chat_id=? AND id=?",(cid,mid))

    def close(self):
        self.db.close()
