"""Use the owner's Windows system proxy for every Telegram connection.

Passwords are used in memory only. Diagnostics never print raw proxy URLs.
PAC scripts are reported explicitly rather than silently bypassed.
"""
from dataclasses import dataclass
import logging
import os
import socket
import sys
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies_environment


@dataclass(frozen=True)
class NetworkRoute:
    proxy: dict | None
    description: str


def read_windows_proxy_settings():
    result = {'available': False, 'enabled': False, 'server': '', 'pac': False}
    if sys.platform != 'win32':
        return result
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            def read(name, default):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except OSError:
                    return default
            result.update(available=True, enabled=bool(read('ProxyEnable', 0)),
                          server=str(read('ProxyServer', '') or ''),
                          pac=bool(read('AutoConfigURL', '') or ''))
    except OSError:
        pass
    return result


def proxy_from_url(value, default_scheme='http'):
    value = value.strip()
    try:
        parsed = urlsplit(value if '://' in value else default_scheme + '://' + value)
        kind = parsed.scheme.lower()
        if kind == 'socks':
            kind = 'socks5'
        if kind not in ('http', 'socks4', 'socks5'):
            raise ValueError('unsupported protocol')
        port = parsed.port
        if not parsed.hostname or not port or not 1 <= port <= 65535:
            raise ValueError('missing host or port')
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('unexpected URL components')
        result = {'proxy_type': kind, 'addr': parsed.hostname, 'port': port, 'rdns': True}
        if parsed.username is not None:
            result['username'] = unquote(parsed.username)
        if parsed.password is not None:
            result['password'] = unquote(parsed.password)
        return result
    except (TypeError, ValueError):
        raise RuntimeError('Не удалось разобрать адрес системного прокси. Нужны сервер и порт; '
                           'поддерживаются HTTP CONNECT, SOCKS4 и SOCKS5. '
                           'Прокси с TLS (https://) и PAC требуют отдельной настройки.') from None


def windows_proxy_url(server):
    if '=' not in server:
        return server, 'http'
    values = {}
    for part in server.split(';'):
        if '=' in part:
            name, address = part.split('=', 1)
            values[name.strip().lower()] = address.strip()
    # HTTP(S) destinations use an HTTP CONNECT proxy; the https= key does not
    # imply a TLS connection to the proxy. Windows' bare socks= means SOCKS4.
    for key, default in (('https', 'http'), ('http', 'http'), ('socks5', 'socks5'),
                         ('socks', 'socks4'), ('socks4', 'socks4')):
        if values.get(key):
            return values[key], default
    raise RuntimeError('В системных настройках не найден подходящий HTTP/SOCKS-прокси.')


def route_from_sources(system, environment):
    if system.get('enabled'):
        if not system.get('server'):
            raise RuntimeError('Прокси Windows включён, но его сервер не указан.')
        value, default = windows_proxy_url(system['server'])
        proxy = proxy_from_url(value, default)
        origin = 'системный прокси Windows'
    elif system.get('pac'):
        raise RuntimeError('Windows использует автоматический сценарий прокси (PAC). '
                           'Этот сценарий пока не поддерживается сборщиком. '
                           'Нужен VPN для Windows или явный адрес HTTP/SOCKS-прокси.')
    else:
        found = next((environment[name] for name in ('https', 'all', 'http', 'socks5', 'socks')
                      if environment.get(name)), None)
        if not found:
            source = 'Прокси Windows отключён. ' if system.get('available') else ''
            return NetworkRoute(None, source + 'Прямое подключение (маршрут VPN, если он охватывает Python).')
        proxy = proxy_from_url(found)
        origin = 'прокси из переменных среды'
    host = proxy['addr']
    if ':' in host:
        host = '[' + host + ']'
    auth = '; с авторизацией' if 'username' in proxy or 'password' in proxy else ''
    return NetworkRoute(proxy, f"{origin}: {proxy['proxy_type']}://{host}:{proxy['port']}{auth}")


def detect_route():
    return route_from_sources(read_windows_proxy_settings(), getproxies_environment())


def client_network_options(route=None):
    route = route if route is not None else detect_route()
    if route.proxy:
        try:
            import python_socks  # noqa: F401
        except ImportError:
            raise RuntimeError('Для подключения через прокси запустите 0-update-network.cmd '
                               'из исправленного архива.') from None
    return {'proxy': route.proxy, 'timeout': 15, 'connection_retries': 2, 'retry_delay': 1}


def connection_failed(client, route, error):
    logging.warning('Telegram connection failed: route=%s; server=%s:%s; error_type=%s',
                    route.description, client.session.server_address, client.session.port,
                    type(error).__name__)
    return RuntimeError('Не удалось подключиться к Telegram. Маршрут: ' + route.description +
                        ' Подробности: collector.log. Для проверки настроек запустите 6-network-check.cmd.')


def network_status():
    from .common import state_dir
    lines = []
    code = 0
    def output(line):
        lines.append(line)
        print(line, flush=True)
    try:
        route = detect_route()
        output('Маршрут Telegram: ' + route.description)
        if route.proxy:
            try:
                with socket.create_connection((route.proxy['addr'], route.proxy['port']), timeout=4):
                    output('TCP-подключение к самому прокси: успешно. Авторизация Telegram здесь не проверяется.')
            except OSError as error:
                code = 1
                output('Не удалось открыть TCP-подключение к прокси: ' + type(error).__name__)
                output('Проверьте, запущено ли приложение, которому принадлежит системный прокси.')
        else:
            output('Для Telegram будет использоваться прямой маршрут Windows. '
                   'VPN-расширение браузера на него не влияет.')
    except RuntimeError as error:
        code = 1
        output(str(error))
    log = state_dir() / 'network-check.log'
    log.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    print('Проверка записана: ' + str(log), flush=True)
    return code
