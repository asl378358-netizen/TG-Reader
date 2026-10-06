import asyncio
import datetime as dt
import json
import logging
from logging.handlers import RotatingFileHandler
import sys
import webbrowser
from pathlib import Path
from tgreader.common import AlreadyRunning,atomic_json,load_config,process_lock,state_dir,iso,now
from tgreader.collection_flow import CollectionProgress,LOG,MAX_SHORT_WAIT,local_resume,request_name,wait_for_collection

def logging_setup():
    handler=RotatingFileHandler(state_dir()/'collector.log',maxBytes=2*1024*1024,backupCount=3,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.basicConfig(level=logging.WARNING,handlers=[handler])
    LOG.setLevel(logging.INFO)

async def collect():
    from telethon.errors import FloodWaitError
    from tgreader.auth import create_client,persist_client,verify_account
    from tgreader.collector import collect_chat
    from tgreader.media import process_jobs
    from tgreader.packets import export_all
    from tgreader.store import Store
    cfg=load_config();state=state_dir();store=Store(state/'messages.sqlite3')
    client=None;verified=False;cooldown=state/'telegram_cooldown.json'
    phase='сохранённая пауза';result=0
    LOG.info('Collection started groups=%s',len(cfg['chats']))
    try:
        if cooldown.exists():
            saved=json.loads(cooldown.read_text(encoding='utf-8'))
            resume=dt.datetime.fromisoformat(saved['resume_utc'])
            remaining=(resume-now()).total_seconds()
            if remaining>MAX_SHORT_WAIT:
                print('Сбор приостановлен Telegram. Следующая попытка после:',local_resume(resume,cfg['timezone']),flush=True)
                print('Запрос:',saved.get('request','неизвестный запрос (пауза сохранена старой версией)'),flush=True)
                LOG.info('Saved pause still active resume_utc=%s request=%s',iso(resume),saved.get('request','unknown'))
                return 2
            if remaining>0:
                await wait_for_collection(remaining,saved.get('request','ранее сохранённая пауза'))
            cooldown.unlink()
        phase='подключение'
        print('Подключаемся к Telegram...',flush=True)
        client=create_client(cfg)
        client.collection_progress=CollectionProgress()
        await client.connect()
        phase='проверка аккаунта'
        await verify_account(client,cfg)
        verified=True
        print('Аккаунт подтверждён. Начинаем чтение групп.',flush=True)
        phase='чтение групп'
        for chat in cfg['chats']:
            health=await collect_chat(client,store,chat,cfg)
            if health.get('collection_error'):result=3
        phase='скачивание и обработка медиа'
        print('Чтение групп завершено. Обрабатываем фотографии, голосовые и кружочки...',flush=True)
        client.collection_progress.set_phase(phase)
        await process_jobs(client,store,cfg['chats'],state,cfg)
        if store.pending_jobs([chat['chat_id'] for chat in cfg['chats']],1):result=3
    except FloodWaitError as e:
        resume=now()+dt.timedelta(seconds=e.seconds+5)
        name=request_name(e.request)
        LOG.warning('Collection paused phase=%s request=%s seconds=%s resume_utc=%s',phase,name,e.seconds,iso(resume))
        atomic_json(cooldown,{'resume_utc':iso(resume),'request':name,'seconds':e.seconds,'phase':phase})
        for chat in cfg['chats']:
            health=store.health(chat['chat_id'])
            health.update(collection_error=f'Telegram попросил паузу {e.seconds} секунд: {name}; продолжение после {local_resume(resume,cfg["timezone"])}.',telegram_resume_utc=iso(resume),telegram_request=name)
            store.set_health(chat['chat_id'],health)
        print(f'Сбор приостановлен Telegram: {name}, пауза {e.seconds} с. Продолжение после {local_resume(resume,cfg["timezone"])}.',flush=True)
        print('Уже полученные сообщения сохранены. Повторный вход не нужен.',flush=True)
        return 2
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
            if client is not None and verified:
                persist_client(client,cfg)
            phase='выгрузка файлов'
            print('Выгружаем уже сохранённые материалы в папку Google Диска...',flush=True)
            export_all(store,cfg['chats'],state,cfg)
        finally:
            store.close()
            if client is not None:await client.disconnect()
    LOG.info('Collection finished result=%s',result)
    print('Сбор частично завершён: есть ошибки чтения или необработанные медиа. Откройте состояние.' if result else
          'Сбор завершён. Проверьте синхронизацию папки в Google Диске.',flush=True)
    return result

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
            elif command=='select-groups':
                from tgreader.wizard import select_groups
                asyncio.run(select_groups())
            elif command=='collect':return asyncio.run(collect())
            else:raise RuntimeError('Неизвестная команда.')
    except AlreadyRunning:
        print('Предыдущий сбор еще идет. Дождитесь его завершения.');return
    except Exception as e:
        logging.exception('Application error')
        print(f'Ошибка: {type(e).__name__}: {e}\nЖурнал: {state_dir()/"collector.log"}',flush=True)
        sys.exit(1)

if __name__=='__main__':raise SystemExit(main() or 0)
