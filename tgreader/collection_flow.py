"""Keep read-only RPCs and their pagination alive during short Telegram limits."""
import asyncio
import logging
import math
from zoneinfo import ZoneInfo

from telethon import TelegramClient
from telethon.errors import FloodWaitError

LOG = logging.getLogger('tgreader.collection')
MAX_SHORT_WAIT = 300
MAX_TOTAL_WAIT = 600


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


async def wait_for_collection(seconds, label):
    remaining = max(1, math.ceil(seconds)) + 1
    print(f'Telegram попросил паузу {remaining} с: {label}. '
          'Сбор продолжится автоматически; оставьте окно открытым.', flush=True)
    while remaining:
        if remaining % 10 == 0 or remaining <= 5:
            print(f'До продолжения сбора: {remaining} с.', flush=True)
        step = min(5, remaining)
        await asyncio.sleep(step)
        remaining -= step
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
        for attempt in range(10):
            try:
                return await super()._call(sender, request, ordered=ordered, flood_sleep_threshold=0)
            except FloodWaitError as error:
                delay = max(1, error.seconds) + 1
                retry = (error.seconds <= MAX_SHORT_WAIT and attempt < 9
                         and self.collection_waited + delay <= MAX_TOTAL_WAIT)
                LOG.warning('FloodWait request=%s seconds=%s auto_retry=%s waited_seconds=%s',
                            request_name(error.request), error.seconds, retry, self.collection_waited)
                if not retry:
                    raise
                self.collection_waited += delay
                await wait_for_collection(error.seconds, label)
        raise AssertionError('Unreachable')
