"""Wait for Telegram limits without discarding login or dialog pagination."""
import asyncio
from telethon.errors import FloodWaitError
from telethon.tl import types


async def wait_for_telegram(seconds, label):
    remaining = max(1, int(seconds)) + 1
    print(f'Telegram попросил паузу {remaining} с: {label}. '
          'Окно оставьте открытым — продолжим автоматически.', flush=True)
    while remaining:
        if remaining <= 5 or remaining % 10 == 0:
            print(f'Осталось {remaining} с...', flush=True)
        await asyncio.sleep(min(1, remaining))
        remaining -= 1
    print('Продолжаем настройку.', flush=True)


async def setup_rpc(operation, label):
    total_wait = 0
    for _ in range(10):
        try:
            return await operation()
        except FloodWaitError as error:
            total_wait += max(1, error.seconds) + 1
            if total_wait > 600:
                raise RuntimeError(f'Telegram требует длительную паузу: {error.seconds} с. '
                                   'Настройку остановили; повторите после этой паузы.') from None
            await wait_for_telegram(error.seconds, label)
    raise RuntimeError('Telegram неоднократно ограничил запросы. Повторите настройку позже.')


async def setup_dialogs(client, *, rpc_waits_handled=False):
    print('Вход выполнен. Получаем список групп и каналов...', flush=True)
    iterator = client.iter_dialogs().__aiter__()
    result = []
    while True:
        try:
            dialog = await iterator.__anext__() if rpc_waits_handled else await setup_rpc(iterator.__anext__, 'получение списка чатов')
        except StopAsyncIteration:
            return result
        if dialog.is_group or dialog.is_channel or isinstance(dialog.entity,(types.ChannelForbidden,types.ChatForbidden)):
            result.append(dialog)
            if len(result)%100==0:
                print(f'Загружено групп и каналов: {len(result)}...',flush=True)
