import asyncio
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import webbrowser
from pathlib import Path
from tgreader.common import AlreadyRunning,atomic_json,load_config,process_lock,state_dir,iso,now

def logging_setup():
    handler=RotatingFileHandler(state_dir()/'collector.log',maxBytes=2*1024*1024,backupCount=3,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.basicConfig(level=logging.WARNING,handlers=[handler])

async def collect():
    from telethon.errors import FloodWaitError
    from tgreader.auth import create_client,persist_client,verify_account
    from tgreader.collector import collect_chat
    from tgreader.media import process_jobs
    from tgreader.packets import export_all
    from tgreader.store import Store
    cfg=load_config();state=state_dir();store=Store(state/'messages.sqlite3')
    client=None;connected=False;cooldown=state/'telegram_cooldown.json'
    try:
        if cooldown.exists():
            resume=dt.datetime.fromisoformat(json.loads(cooldown.read_text(encoding='utf-8'))['resume_utc'])
            if resume>now():
                print('Telegram попросил паузу. Следующая попытка после:',resume.isoformat(),flush=True)
                return
            cooldown.unlink()
        client=create_client(cfg)
        await client.connect();connected=True
        await verify_account(client,cfg)
        for chat in cfg['chats']:await collect_chat(client,store,chat,cfg)
        await process_jobs(client,store,cfg['chats'],state,cfg)
        persist_client(client,cfg)
    except FloodWaitError as e:
        resume=now()+dt.timedelta(seconds=e.seconds+5)
        atomic_json(cooldown,{'resume_utc':iso(resume)})
        for chat in cfg['chats']:
            health=store.health(chat['chat_id'])
            health.update(collection_error=f'Telegram попросил паузу {e.seconds} секунд; следующий запрос после {resume.isoformat()}.',telegram_resume_utc=iso(resume))
            store.set_health(chat['chat_id'],health)
        print('Telegram попросил паузу; продолжим после',resume.isoformat(),flush=True)
        return
    except Exception as e:
        logging.exception('Collector failed')
        for chat in cfg['chats']:
            health=store.health(chat['chat_id'])
            health['collection_error']=f'{type(e).__name__}: {e}'
            store.set_health(chat['chat_id'],health)
        print('Ошибка:',type(e).__name__,str(e),flush=True)
        raise
    finally:
        try:
            export_all(store,cfg['chats'],state,cfg)
        finally:
            store.close()
            if client is not None:await client.disconnect()
    print('Готово. Проверьте синхронизацию папки в Google Диске.',flush=True)

def main():
    logging_setup();command=sys.argv[1] if len(sys.argv)>1 else 'status'
    if command=='network-status':
        from tgreader.network import network_status
        sys.exit(network_status())
    if command=='status':
        p=state_dir()/'status.html'
        if p.exists():webbrowser.open(p.as_uri())
        else:print('Первый сбор еще не выполнен. Подключите Telegram и нажмите «Собрать сейчас».')
        return
    if command=='warm-model':
        from tgreader.media import MediaProcessor
        MediaProcessor(state_dir(),{'whisper_model':'small'}).model_instance()
        print('Модель речи готова.');return
    try:
        with process_lock():
            if command=='setup':
                from tgreader.wizard import setup
                asyncio.run(setup())
            elif command=='setup-desktop':
                from tgreader.desktop import setup_desktop
                asyncio.run(setup_desktop())
            elif command=='collect':asyncio.run(collect())
            else:raise RuntimeError('Неизвестная команда.')
    except AlreadyRunning:
        print('Предыдущий сбор еще идет. Дождитесь его завершения.');return
    except Exception as e:
        logging.exception('Application error')
        print(f'Ошибка: {type(e).__name__}: {e}\nЖурнал: {state_dir()/"collector.log"}',flush=True)
        sys.exit(1)

if __name__=='__main__':main()
