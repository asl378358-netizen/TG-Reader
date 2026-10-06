"""Keep read-only RPCs and their pagination alive during short Telegram limits."""
import asyncio
import logging
import math
import time
from collections import deque
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.errors import FloodWaitError

LOG = logging.getLogger('tgreader.collection')
MAX_SHORT_WAIT = 300
MAX_TOTAL_WAIT = 600


class CollectionProgress:
    """Count persisted messages, including time spent waiting for Telegram."""
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.last_report = self.started
        self.new = 0
        self.context = 0
        self.media_done = 0
        self.phase = 'подключение'
        self.recent = deque()

    def set_phase(self, value):
        self.phase = value
        self.report(force=True)

    def saved(self, *, context=False):
        if context:
            self.context += 1
        else:
            self.new += 1
            self.recent.append(self.clock())
        self.report(force=not context and self.new % 50 == 0)

    def report(self, *, force=False):
        stamp = self.clock()
        while self.recent and self.recent[0] <= stamp - 30:
            self.recent.popleft()
        if not force and stamp - self.last_report < 10:
            return
        self.last_report = stamp
        print(f'Прогресс: новых сообщений {self.new}, контекст {self.context}; '
              f'за последние 30 с сохранено {len(self.recent)} новых; '
              f'медиа готово {self.media_done}. Этап: {self.phase}.', flush=True)
        LOG.info('Progress new=%s context=%s new_last_30s=%s media_done=%s elapsed_seconds=%.1f phase=%s',
                 self.new, self.context, len(self.recent), self.media_done,
                 stamp - self.started, self.phase)


def request_size(request):
    if isinstance(request, (list, tuple)):
        return None
    for _ in range(8):
        query = getattr(request, 'query', None)
        if query is None:
            break
        request = query
    if type(request).__name__ == 'GetMessagesRequest':
        return len(request.id)
    if type(request).__name__ == 'GetHistoryRequest':
        return request.limit
    return None


def request_name(request):
    # Never render a request: it can contain IDs, access hashes or message text.
    if isinstance(request, (list, tuple)):
        return ', '.join(sorted({request_name(item) for item in request}))
    for _ in range(8):
        query = getattr(request, 'query', None)
        if query is None:
            break
        request = query
    return type(request).__name__ if request is not None else 'неизвестный запрос'


def local_resume(value, timezone):
    return value.astimezone(ZoneInfo(timezone)).strftime('%d.%m.%Y %H:%M:%S (%Z)')


async def wait_for_collection(seconds, label, progress=None):
    remaining = max(1, math.ceil(seconds)) + 1
    print(f'Telegram попросил паузу {remaining} с: {label}. '
          'Сбор продолжится автоматически; оставьте окно открытым.', flush=True)
    if progress:
        progress.report(force=True)
    elapsed = 0
    while remaining:
        if elapsed % 10 == 0 or remaining <= 5:
            print(f'До продолжения сбора: {remaining} с.', flush=True)
            if progress:
                progress.report(force=True)
        step = min(5, remaining)
        await asyncio.sleep(step)
        remaining -= step
        elapsed += step
    print('Продолжаем сбор на том же запросе.', flush=True)


class CollectionClient(TelegramClient):
    """Cover history and exported-DC downloads in pinned Telethon 1.45.0.

    Its download iterators call _call directly; overriding __call__ alone
    would leave photographs, voice and circles without the same retry policy.
    """
    def __init__(self, *args, **kwargs):
        # Telegram waits are handled here so the UI gets a visible countdown.
        kwargs.update(flood_sleep_threshold=0, raise_last_call_error=True)
        super().__init__(*args, **kwargs)
        self.collection_waited = 0

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        label = request_name(request)
        size = request_size(request)
        started = time.monotonic()
        for attempt in range(10):
            try:
                response = await super()._call(sender, request, ordered=ordered, flood_sleep_threshold=0)
                if size is not None:
                    LOG.info('RPC completed request=%s requested_messages=%s returned_messages=%s elapsed_seconds=%.1f',
                             label, size, len(getattr(response, 'messages', ())), time.monotonic() - started)
                return response
            except FloodWaitError as error:
                delay = max(1, error.seconds) + 1
                retry = (error.seconds <= MAX_SHORT_WAIT and attempt < 9
                         and self.collection_waited + delay <= MAX_TOTAL_WAIT)
                LOG.warning('FloodWait request=%s requested_messages=%s seconds=%s auto_retry=%s waited_seconds=%s',
                            request_name(error.request), size, error.seconds, retry, self.collection_waited)
                if not retry:
                    raise
                self.collection_waited += delay
                description = f'{label}, запрошено сообщений: {size}' if size is not None else label
                await wait_for_collection(error.seconds, description, getattr(self, 'collection_progress', None))
        raise AssertionError('Unreachable')
