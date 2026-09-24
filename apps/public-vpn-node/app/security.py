"""public-vpn-node security policy, 2026-09-24. GPL-3.0-or-later.
No network or filesystem activity at import time.
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex

SECRET_FIELDS = ('username', 'password', 'secret_path', 'proxy_username', 'proxy_password')


def validate_credentials(cfg):
    if not isinstance(cfg, dict):
        raise ValueError('凭据必须为 JSON 对象')
    for field in SECRET_FIELDS:
        value = cfg.get(field)
        if not isinstance(value, str) or not value.isascii() or not value or len(value) > 128:
            raise ValueError('凭据字段缺失或格式不正确')
        if any(ord(c) < 33 or ord(c) > 126 for c in value) or 'REPLACE' in value:
            raise ValueError('凭据不能包含空白、控制字符或占位符')
    for field in ('username', 'proxy_username'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{4,64}', cfg[field]):
            raise ValueError('账号须为 4–64 位字母、数字、下划线或连字符')
    for field in ('password', 'proxy_password'):
        if len(cfg[field]) < 20:
            raise ValueError('密码至少 20 位')
    if not re.fullmatch(r'[A-Za-z0-9]{24,128}', cfg['secret_path']):
        raise ValueError('路径须为 24–128 位字母数字')
    return {key: cfg[key] for key in SECRET_FIELDS}


def load_credentials():
    # Called only inside the container, never by host preflight.
    path = Path(os.environ.get('CREDENTIALS_FILE', '/run/secrets/credentials'))
    return validate_credentials(json.loads(path.read_text(encoding='utf-8')))


def public_ipv4(value):
    addr = ipaddress.ip_address(value)
    if addr.version != 4 or not addr.is_global or addr.is_multicast:
        raise ValueError('仅允许公共 IPv4 地址')
    return str(addr)


def sanitize_config(text):
    """Rebuild a profile; never execute untrusted directives or reference files.

    Unknown directives fail closed (including config, plugin, scripts, management,
    routes, log/status paths, proxy options and inline connection sections).
    """
    if not isinstance(text, str) or len(text) > 131072 or '\x00' in text:
        raise ValueError('VPN 配置大小或格式不正确')
    remote = None
    proto = 'udp'
    blocks = {}
    block = None
    lines = []
    safe_ignored = {
        'client', 'dev', 'dev-type', 'resolv-retry', 'nobind', 'persist-key',
        'persist-tun', 'cipher', 'data-ciphers', 'data-ciphers-fallback',
        'auth', 'verb', 'mute', 'auth-user-pass', 'remote-cert-tls',
        'comp-lzo', 'compress', 'sndbuf', 'rcvbuf', 'tun-mtu', 'mssfix',
        'reneg-sec', 'connect-retry', 'connect-timeout', 'float',
        'allow-compression', 'script-security', 'route-nopull', 'auth-nocache',
    }
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if block:
            if line == f'</{block}>':
                content = '\n'.join(lines)
                label = 'CERTIFICATE' if block in ('ca', 'cert') else '(?:RSA |EC )?PRIVATE KEY'
                if not re.fullmatch(r'-----BEGIN ' + label + r'-----\n[A-Za-z0-9+/=\n]+\n-----END ' + label + r'-----', content):
                    raise ValueError('无效的内嵌证书块')
                blocks[block] = content
                block = None
                lines = []
            else:
                lines.append(line)
            continue
        if line in ('<ca>', '<cert>', '<key>'):
            block = line[1:-1]
            if block in blocks:
                raise ValueError('重复证书块')
            continue
        parts = shlex.split(line, comments=True)
        if not parts:
            continue
        option = parts[0]
        if option == 'remote':
            if remote is not None or len(parts) not in (3, 4):
                raise ValueError('要求唯一 remote IP 和端口')
            ip = public_ipv4(parts[1])
            port = int(parts[2])
            if not 1 <= port <= 65535:
                raise ValueError('无效 VPN 端口')
            remote = (ip, port)
            if len(parts) == 4:
                proto = parts[3]
        elif option == 'proto':
            if len(parts) != 2:
                raise ValueError('无效 VPN 协议')
            proto = parts[1]
        elif option not in safe_ignored:
            raise ValueError('VPN 配置含禁止或未知指令')
    if block or remote is None or 'ca' not in blocks or proto not in ('udp', 'udp4', 'tcp', 'tcp-client', 'tcp4-client'):
        raise ValueError('VPN 配置缺少必要字段或协议不支持')
    proto = 'tcp-client' if proto.startswith('tcp') else 'udp'
    result = [
        'client', 'dev tun', f'proto {proto}', f'remote {remote[0]} {remote[1]}',
        'nobind', 'resolv-retry 0', 'remote-cert-tls server', 'auth SHA1',
        'cipher AES-128-CBC', 'allow-compression no', 'script-security 0',
        'route-nopull', 'auth-nocache', 'verb 3',
    ]
    for name, content in blocks.items():
        result.extend((f'<{name}>', content, f'</{name}>'))
    return '\n'.join(result) + '\n'


def credential_match(actual, expected):
    return secrets.compare_digest(str(actual).encode(), str(expected).encode())


# Global bounded login rate prevents brute force without storing client identifiers.
import collections
import threading
import time
_login_times = collections.deque()
_login_lock = threading.Lock()


def allow_login():
    with _login_lock:
        now = time.monotonic()
        while _login_times and _login_times[0] <= now - 60:
            _login_times.popleft()
        if len(_login_times) >= 10:
            return False
        _login_times.append(now)
        return True
