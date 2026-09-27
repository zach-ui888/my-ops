"""Operator tool: private local state, independent users, atomic Xray config.
Do not run against deployment secrets during development. No network/Docker calls.
"""
import argparse
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import uuid
from urllib.parse import urlencode, quote

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / 'app'))
from ingress import target_name
from security import load_credentials


def keypair():
    # OpenSSL supplies audited X25519; private bytes never appear in argv/stdout.
    private = subprocess.run(['openssl', 'genpkey', '-algorithm', 'X25519', '-outform', 'DER'],
                             capture_output=True, check=True).stdout
    public = subprocess.run(['openssl', 'pkey', '-inform', 'DER', '-pubout', '-outform', 'DER'],
                            input=private, capture_output=True, check=True).stdout
    if (private[:16].hex() != '302e020100300506032b656e04220420' or len(private) != 48 or
            public[:12].hex() != '302a300506032b656e032100' or len(public) != 44):
        raise ValueError('Unexpected X25519 encoding')
    encode = lambda b: base64.urlsafe_b64encode(b[-32:]).decode().rstrip('=')
    return encode(private), encode(public)


def validate(db):
    if db['version'] != 1 or not 1 <= db['port'] <= 65535:
        raise ValueError('Invalid version/port')
    ip = ipaddress.ip_address(db['host'])
    if ip.version != 4 or not ip.is_global:
        raise ValueError('Use a public server IPv4; no client bootstrap DNS required')
    target_name(db['sni'])
    for field in ('private_key', 'public_key'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', db[field]):
            raise ValueError('Invalid key')
    ids, shorts = set(), set()
    for name, user in db['users'].items():
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', name):
            raise ValueError('Invalid user name')
        identity = uuid.UUID(user['id'])
        if identity.version != 4 or str(identity) != user['id'] or identity in ids:
            raise ValueError('Duplicate or invalid identity')
        if not re.fullmatch(r'[0-9a-f]{16}', user['short_id']) or user['short_id'] in shorts:
            raise ValueError('Duplicate or invalid short ID')
        if type(user['enabled']) is not bool:
            raise ValueError('Invalid status')
        ids.add(identity)
        shorts.add(user['short_id'])
    return db


def change(db, action, name):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', name):
        raise ValueError('Invalid user name')
    users = db['users']
    if action == 'add':
        if name in users:
            raise ValueError('User exists')
        users[name] = {'id': str(uuid.uuid4()), 'short_id': secrets.token_hex(8), 'enabled': True}
    elif action == 'delete':
        del users[name]
    else:
        users[name]['enabled'] = action == 'enable'
    validate(db)


def server_config(db, credentials):
    validate(db)
    active = [u for u in db['users'].values() if u['enabled']]
    # Empty client list accepts no VLESS user; nonempty random shortIds avoid parser
    # ambiguity when all users have been revoked. No fallback/direct outbound exists.
    return {
        'log': {'loglevel': 'none', 'access': 'none', 'error': 'none'},
        'inbounds': [{'tag': 'public', 'listen': '0.0.0.0', 'port': 8443, 'protocol': 'vless',
                      'settings': {'clients': [{'id': u['id'], 'flow': 'xtls-rprx-vision'} for u in active],
                                   'decryption': 'none'},
                      'streamSettings': {'network': 'tcp', 'security': 'reality',
                                         'realitySettings': {'show': False, 'target': '127.0.0.1:9443',
                                                             'xver': 0, 'serverNames': [db['sni']],
                                                             'privateKey': db['private_key'],
                                                             'shortIds': [u['short_id'] for u in active] or [secrets.token_hex(8)]}}}],
        'outbounds': [{'tag': 'vpn-socks', 'protocol': 'socks',
                       'settings': {'servers': [{'address': '127.0.0.1', 'port': 7928,
                                                 'users': [{'user': credentials['proxy_username'],
                                                            'pass': credentials['proxy_password']}]}]}},
                      {'tag': 'deny', 'protocol': 'blackhole'}],
        'routing': {'domainStrategy': 'AsIs', 'rules': [
            {'type': 'field', 'network': 'udp', 'outboundTag': 'deny'},
            {'type': 'field', 'network': 'tcp', 'outboundTag': 'vpn-socks'}]},
    }


def client_exports(db, name):
    validate(db)
    u = db['users'][name]
    if not u['enabled']:
        raise ValueError('Disabled user')
    params = {'encryption': 'none', 'security': 'reality', 'type': 'tcp',
              'flow': 'xtls-rprx-vision', 'sni': db['sni'], 'fp': 'chrome',
              'pbk': db['public_key'], 'sid': u['short_id']}
    uri = f"vless://{u['id']}@{db['host']}:{db['port']}?{urlencode(params)}#{quote(name)}\n"
    node = {'name': name, 'type': 'vless', 'server': db['host'], 'port': db['port'],
            'uuid': u['id'], 'flow': 'xtls-rprx-vision', 'network': 'tcp', 'tls': True,
            'udp': False, 'servername': db['sni'], 'client-fingerprint': 'chrome',
            'reality-opts': {'public-key': db['public_key'], 'short-id': u['short_id']}}
    # JSON is YAML-compatible; no public subscription server, DIRECT or fallback.
    clash = {'mixed-port': 7890, 'allow-lan': False, 'mode': 'rule', 'ipv6': False,
             'dns': {'enable': True, 'ipv6': False, 'enhanced-mode': 'fake-ip',
                     'nameserver': [f'https://1.1.1.1/dns-query#{name}']},
             'proxies': [node], 'proxy-groups': [{'name': 'VPN', 'type': 'select', 'proxies': [name]}],
             'rules': ['NETWORK,UDP,REJECT', 'MATCH,VPN']}
    return uri, json.dumps(clash, indent=2) + '\n'


def private_dir(path):
    if path.is_symlink():
        raise ValueError('Symlink directory')
    path.mkdir(mode=0o700, exist_ok=True)
    if path.stat().st_mode & 0o077:
        raise ValueError('Private directory must be 0700')


def write_file(path, data, mode=0o600, group=None):
    temporary = path.with_name(path.name + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if group is not None:
                os.fchown(stream.fileno(), -1, group)
                os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'add', 'disable', 'enable', 'delete', 'render', 'export', 'list'])
    parser.add_argument('name', nargs='?')
    parser.add_argument('--host')
    parser.add_argument('--sni')
    args = parser.parse_args()
    if Path.cwd() != BASE:
        raise ValueError('Run in project root')
    os.umask(0o077)
    root = BASE / 'secrets'
    private_dir(root)
    lockfd = os.open(root / 'ingress.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lockfd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = root / 'ingress.json'
        if args.action == 'init':
            if state.exists() or state.is_symlink():
                raise ValueError('State exists')
            private, public = keypair()
            db = {'version': 1, 'host': args.host, 'port': 443, 'sni': args.sni,
                  'private_key': private, 'public_key': public, 'users': {}}
            validate(db)
            write_file(state, json.dumps(db))
        else:
            if state.is_symlink() or state.stat().st_mode & 0o077:
                raise ValueError('Unsafe state permissions')
            db = validate(json.loads(state.read_text()))
            if args.action in ('add', 'disable', 'enable', 'delete'):
                change(db, args.action, args.name or '')
                write_file(state, json.dumps(db))
                # Invalidate any previously exported local copies for this user.
                exports = root / 'exports'
                if exports.is_symlink():
                    raise ValueError('Symlink exports')
                for suffix in ('.vless.txt', '.clash.yaml'):
                    (exports / (args.name + suffix)).unlink(missing_ok=True)
            elif args.action == 'list':
                for name, u in db['users'].items():
                    print(name, 'enabled' if u['enabled'] else 'disabled')
            elif args.action == 'export':
                uri, clash = client_exports(db, args.name or '')
                exports = root / 'exports'
                private_dir(exports)
                write_file(exports / (args.name + '.vless.txt'), uri)
                write_file(exports / (args.name + '.clash.yaml'), clash)
            elif args.action == 'render':
                if os.environ.get('REALITY_TARGET') != db['sni']:
                    raise ValueError('REALITY_TARGET must match stored SNI')
                # Runtime host permissions need administrator ownership for Docker UID.
                if os.geteuid() != 0:
                    raise ValueError('Render requires deployment administrator file ownership')
                os.environ['CREDENTIALS_FILE'] = str(root / 'credentials.json')
                if (root / 'credentials.json').is_symlink():
                    raise ValueError('Symlink credentials')
                cfg = server_config(db, load_credentials())
                runtime = BASE / 'runtime'
                private_dir(runtime)
                directory = runtime / 'ingress'
                if directory.is_symlink():
                    raise ValueError('Symlink runtime')
                directory.mkdir(mode=0o750, exist_ok=True)
                os.chown(directory, -1, 65532)
                os.chmod(directory, 0o750)
                write_file(directory / 'server.json', json.dumps(cfg), 0o640, 65532)
    print('完成；配置变更须在停止 ingress 后 render、校验并重建 ingress 才生效。未输出凭据。')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('操作失败；检查参数、私密目录权限、配置和依赖。保持 ingress 停止，未输出异常内容。', file=sys.stderr)
        raise SystemExit(1)
