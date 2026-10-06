import asyncio
import hashlib
import json
import logging
import math
import mimetypes
import os
import shutil
import threading
import time
from pathlib import Path
from PIL import Image, ImageOps
from .common import atomic_json, iso, now
from .collection_flow import LOG


class PublicModelNotice(logging.Filter):
    def filter(self, record):
        # Replace this advisory with a short Russian explanation. Keep errors
        # and other warnings: authentication is optional for this public model.
        return not (record.levelno == logging.WARNING and
                    record.getMessage().startswith('You are sending unauthenticated requests to the HF Hub'))


def model_cache_bytes(directory):
    total=0
    for path in Path(directory).rglob('*'):
        try:
            if path.is_file() and not path.is_symlink():total+=path.stat().st_size
        except OSError:
            pass  # A download may atomically rename a cache file during scanning.
    return total


def model_wait_notice(stopped,directory,started):
    while not stopped.wait(10):
        megabytes=model_cache_bytes(directory)/(1024*1024)
        print(f'Модель речи: подготовка идёт {int(time.monotonic()-started)} с; '
              f'в папке модели {megabytes:.1f} МБ. Ожидаем завершения загрузки и открытия модели.',flush=True)

def small_image(source,target):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    with Image.open(source) as im:
        im=ImageOps.exif_transpose(im)
        im.thumbnail((2560,2560))
        if im.mode in ('RGBA','LA'):
            rgba=im.convert('RGBA');base=Image.new('RGB',rgba.size,'white');base.paste(rgba,mask=rgba.getchannel('A'));im=base
        else:im=im.convert('RGB')
        im.save(target,format='JPEG',quality=92)

def extract_frames(source,directory):
    import av
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    frames=[]
    with av.open(str(source)) as container:
        if not container.streams.video:raise RuntimeError('В кружочке нет видеодорожки.')
        stream=container.streams.video[0]
        duration=float(stream.duration*stream.time_base) if stream.duration else (float(container.duration/av.time_base) if container.duration else None)
        step=max(10.0,(duration or 60)/11)
        next_time=0.0;last_time=0.0;last_frame=None
        for n,frame in enumerate(container.decode(video=0)):
            timestamp=float(frame.time or 0)
            last_frame=frame
            last_time=timestamp
            if timestamp>=next_time and len(frames)<11:
                p=directory/f'frame_{len(frames):02d}.jpg'
                image=frame.to_image();image.thumbnail((1280,1280));image.convert('RGB').save(p,quality=90)
                frames.append({'path':str(p),'seconds':round(timestamp,2)})
                next_time+=step
        # The final frame must represent the end, not the previous sample.
        if last_frame is not None:
            image=last_frame.to_image();image.thumbnail((1280,1280))
            if not frames or last_time-frames[-1]['seconds']>2:
                p=directory/f'frame_{len(frames):02d}.jpg';image.convert('RGB').save(p,quality=90)
                frames.append({'path':str(p),'seconds':round(last_time,2)})
    if not frames:raise RuntimeError('Не удалось декодировать кадры кружочка.')
    return frames

class MediaProcessor:
    def __init__(self,state,cfg):
        self.state=Path(state);self.cfg=cfg;self.model=None;self.model_error=None
    def model_instance(self):
        if self.model_error is not None:raise self.model_error
        if self.model is None:
            os.environ['HF_HOME']=str(self.state/'models')
            os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING']='1'
            from faster_whisper import WhisperModel
            for name,logger in list(logging.Logger.manager.loggerDict.items()):
                if name.startswith('huggingface_hub') and isinstance(logger,logging.Logger):
                    if not any(isinstance(f,PublicModelNotice) for f in logger.filters):logger.addFilter(PublicModelNotice())
            print('Подготовка локальной модели речи. При первом запуске скачиваются веса; '
                  'публичная модель не требует аккаунта или токена. Сохранённые сообщения уже в базе.',flush=True)
            stopped=threading.Event();started=time.monotonic()
            worker=threading.Thread(target=model_wait_notice,args=(stopped,self.state/'models',started),daemon=True);worker.start()
            try:
                self.model=WhisperModel(self.cfg.get('whisper_model','small'),device='cpu',compute_type='int8',
                                       cpu_threads=min(4,os.cpu_count() or 2),download_root=str(self.state/'models'))
            except Exception as error:
                self.model_error=error
                print('Не удалось подготовить модель речи. Оригиналы сохранены; ошибка будет записана в журнал.',flush=True)
                raise
            finally:
                stopped.set();worker.join(timeout=1)
            print('Локальная модель речи готова. Начинаем расшифровку.',flush=True)
        return self.model
    def transcribe(self,path):
        from faster_whisper.audio import decode_audio
        # PyAV is pinned to 16.1.0 because PyAV 19 removed an argument used by faster-whisper 1.2.1.
        decoded=decode_audio(str(path),sampling_rate=16000)
        segments,info=self.model_instance().transcribe(decoded,language=self.cfg.get('speech_language','ru'),beam_size=5,vad_filter=True)
        rows=[{'start':round(s.start,2),'end':round(s.end,2),'text':s.text.strip(),
               'avg_logprob':round(s.avg_logprob,3),'no_speech_probability':round(s.no_speech_prob,3)} for s in segments]
        return {'method':'local_faster_whisper','model':self.cfg.get('whisper_model','small'),'language':info.language,
                'duration_seconds':round(info.duration,2),'text':' '.join(s['text'] for s in rows),'segments':rows,
                'note':'Автоматическая расшифровка; имена и отдельные слова могут быть распознаны неверно.'}
    def process_file(self,path,kind,cid,mid,fingerprint):
        path=Path(path);base=self.state/'cache'/f'chat_{abs(cid)}'/f'{mid}_{fingerprint}'
        base.mkdir(parents=True,exist_ok=True);assets=[];result={'kind':kind,'processed_utc':iso(now())}
        if kind=='photo':
            p=base/'photo.jpg';small_image(path,p);assets.append(p)
        elif kind in ('voice','round_video'):
            if kind=='round_video':
                frames=extract_frames(path,base/'frames')
                assets.extend(Path(f['path']) for f in frames)
                result['frames']=[{'path':str(Path(f['path']).relative_to(self.state)).replace('\\','/'),'seconds':f['seconds']} for f in frames]
                import av
                with av.open(str(path)) as c:has_audio=bool(c.streams.audio)
            else:has_audio=True
            if has_audio:result['transcription']=self.transcribe(path)
            else:result['transcription']={'text':'','note':'В кружочке нет аудиодорожки.'}
            atomic_json(base/'transcript.json',result['transcription'])
        result['assets']=[str(p.relative_to(self.state)).replace('\\','/') for p in assets]
        return result

def original_path(state,job,payload,kind):
    mime=payload.get('media_mime_type') or ''
    ext='.jpg' if kind=='photo' else ('.ogg' if kind=='voice' else '.mp4')
    if kind=='photo' and mime:ext={'image/png':'.png','image/webp':'.webp','image/jpeg':'.jpg','image/gif':'.gif'}.get(mime,'.image')
    return Path(state)/'originals'/f'chat_{abs(job["chat_id"])}'/f'{job["id"]}_{job["fingerprint"]}{ext}'


async def process_jobs(client,store,chats,state,cfg):
    from telethon.errors import FloodWaitError
    from .collector import input_peer,media_kind
    processor=MediaProcessor(state,cfg);allowed={c['chat_id']:c for c in chats}
    jobs=store.pending_jobs(list(allowed),cfg.get('max_media_jobs_per_run',100))
    progress=getattr(client,'collection_progress',None)
    fetched={};cached={}
    for job in jobs:
        key=(job['chat_id'],job['id']);payload=store.get(*key) or {}
        kind=payload.get('media_kind')
        if kind in ('photo','voice','round_video'):
            path=original_path(state,job,payload,kind)
            if path.is_file() and path.stat().st_size:cached[key]=(path,kind)
    print(f'Очередь медиа на этот запуск: {len(jobs)}; оригиналов уже на диске: {len(cached)}.',flush=True)
    for index,job in enumerate(jobs):
        cid,mid=job['chat_id'],job['id']
        key=(cid,mid)
        partial=None
        try:
            if key in cached and not cached[key][0].is_file():cached.pop(key)
            if key in cached:
                original,kind=cached[key]
                print(f'Медиа {index+1}/{len(jobs)}: используем сохранённый {kind}.',flush=True)
            else:
                if key not in fetched:
                    ids=[j['id'] for j in jobs[index:] if j['chat_id']==cid
                         and (cid,j['id']) not in fetched and (cid,j['id']) not in cached][:100]
                    if progress:progress.set_phase(f'медиа: сведения о {len(ids)} вложениях одной порцией')
                    print(f'Медиа: запрашиваем {len(ids)} сообщений одной порцией.',flush=True)
                    try:
                        batch=await client.get_messages(input_peer(allowed[cid]),ids=ids)
                    except FloodWaitError:raise
                    except Exception as error:
                        # One failed batch must not become 100 identical requests.
                        fetched.update({(cid,i):error for i in ids})
                    else:
                        mapped={m.id:m for m in batch if m is not None}
                        fetched.update({(cid,i):mapped.get(i) for i in ids})
                m=fetched[key]
                if isinstance(m,Exception):raise m
                if not m:raise RuntimeError('Сообщение удалено или недоступно; медиа не получено.')
                kind=media_kind(m)
                if kind not in ('photo','voice','round_video'):raise RuntimeError('Тип медиа изменился; требуется повторное чтение сообщения.')
                mime=getattr(getattr(m,'document',None),'mime_type','') or ''
                original=original_path(state,job,{'media_mime_type':mime},kind)
            original.parent.mkdir(parents=True,exist_ok=True)
            if not original.exists() or not original.stat().st_size:
                if progress:progress.set_phase(f'медиа {index+1}/{len(jobs)}: скачивание {kind}')
                print(f'Медиа {index+1}/{len(jobs)}: скачиваем {kind}.',flush=True)
                temp=original.with_name(original.name+'.download')
                downloaded=await client.download_media(m,file=str(temp))
                if not downloaded or not Path(downloaded).exists():raise RuntimeError('Telegram не вернул файл.')
                os.replace(downloaded,original)
            rel=str(original.relative_to(Path(state)/'originals')).replace('\\','/')
            mirror=Path(cfg['output_dir'])/'originals'/rel;mirror.parent.mkdir(parents=True,exist_ok=True)
            if not mirror.exists() or mirror.stat().st_size!=original.stat().st_size:
                tmp=mirror.with_name(mirror.name+'.tmp');shutil.copyfile(original,tmp);os.replace(tmp,mirror)
            partial={'kind':kind,'original_relative_path':'originals/'+rel}
            if progress:progress.set_phase(f'медиа {index+1}/{len(jobs)}: обработка {kind} на компьютере')
            print(f'Медиа {index+1}/{len(jobs)}: оригинал сохранён, '
                  + ('готовим фото.' if kind=='photo' else 'расшифровываем речь на компьютере.'),flush=True)
            result=await asyncio.to_thread(processor.process_file,original,kind,cid,mid,job['fingerprint'])
            result['original_relative_path']='originals/'+rel
            store.finish_job(cid,mid,result=result)
            if progress:
                progress.media_done+=1;progress.report(force=True)
            print(f'Медиа {mid}: {kind}, готово.',flush=True)
        except FloodWaitError as e:
            store.finish_job(cid,mid,result=partial,error=f'{type(e).__name__}: {e}')
            raise
        except Exception as e:
            store.finish_job(cid,mid,result=partial,error=f'{type(e).__name__}: {e}')
            LOG.exception('Media job failed type=%s kind=%s',type(e).__name__,(store.get(cid,mid) or {}).get('media_kind'))
            print(f'Медиа {mid}: ошибка {type(e).__name__}: {e}; будет повторено. Подробности: collector.log.',flush=True)
