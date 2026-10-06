import asyncio
import hashlib
import json
import math
import mimetypes
import os
import shutil
from pathlib import Path
from PIL import Image, ImageOps
from .common import atomic_json, iso, now

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
        self.state=Path(state);self.cfg=cfg;self.model=None
    def model_instance(self):
        if self.model is None:
            os.environ['HF_HOME']=str(self.state/'models')
            from faster_whisper import WhisperModel
            print('Загрузка локальной модели речи; при первом запуске скачиваются веса.',flush=True)
            self.model=WhisperModel(self.cfg.get('whisper_model','small'),device='cpu',compute_type='int8',
                                   cpu_threads=min(4,os.cpu_count() or 2),download_root=str(self.state/'models'))
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

async def process_jobs(client,store,chats,state,cfg):
    from telethon.errors import FloodWaitError
    from .collector import input_peer,media_kind
    processor=MediaProcessor(state,cfg);allowed={c['chat_id']:c for c in chats}
    jobs=store.pending_jobs(list(allowed),cfg.get('max_media_jobs_per_run',100))
    for job in jobs:
        cid,mid=job['chat_id'],job['id']
        partial=None
        try:
            m=await client.get_messages(input_peer(allowed[cid]),ids=mid)
            if not m:raise RuntimeError('Сообщение удалено или недоступно; медиа не получено.')
            kind=media_kind(m)
            if kind not in ('photo','voice','round_video'):raise RuntimeError('Тип медиа изменился; требуется повторное чтение сообщения.')
            mime=getattr(getattr(m,'document',None),'mime_type','') or ''
            ext='.jpg' if kind=='photo' and not getattr(m,'document',None) else ('.ogg' if kind=='voice' else '.mp4')
            if kind=='photo' and mime:ext={ 'image/png':'.png','image/webp':'.webp','image/jpeg':'.jpg','image/gif':'.gif'}.get(mime,'.image')
            original=Path(state)/'originals'/f'chat_{abs(cid)}'/f'{mid}_{job["fingerprint"]}{ext}'
            original.parent.mkdir(parents=True,exist_ok=True)
            if not original.exists():
                temp=original.with_name(original.name+'.download')
                downloaded=await client.download_media(m,file=str(temp))
                if not downloaded or not Path(downloaded).exists():raise RuntimeError('Telegram не вернул файл.')
                os.replace(downloaded,original)
            rel=str(original.relative_to(Path(state)/'originals')).replace('\\','/')
            mirror=Path(cfg['output_dir'])/'originals'/rel;mirror.parent.mkdir(parents=True,exist_ok=True)
            if not mirror.exists() or mirror.stat().st_size!=original.stat().st_size:
                tmp=mirror.with_name(mirror.name+'.tmp');shutil.copyfile(original,tmp);os.replace(tmp,mirror)
            partial={'kind':kind,'original_relative_path':'originals/'+rel}
            result=await asyncio.to_thread(processor.process_file,original,kind,cid,mid,job['fingerprint'])
            result['original_relative_path']='originals/'+rel
            store.finish_job(cid,mid,result=result)
            print(f'Медиа {mid}: {kind}, готово.',flush=True)
        except FloodWaitError as e:
            store.finish_job(cid,mid,result=partial,error=f'{type(e).__name__}: {e}')
            raise
        except Exception as e:
            store.finish_job(cid,mid,result=partial,error=f'{type(e).__name__}: {e}')
            print(f'Медиа {mid}: ошибка {type(e).__name__}; будет повторено.',flush=True)
