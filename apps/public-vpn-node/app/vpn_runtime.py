"""Single owner of VPN readiness, kernel policy, and outbound socket generations.
Only the serving OpenVPN child's log stream may nominate an interface. Probe
children never publish state. Runtime status contains no authentication data.
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import weakref

import firewall

STATE = Path(os.environ.get('VPNGATE_DATA_DIR', '/data')) / 'vpn_runtime.json'
LOCK = threading.RLock()
interface = None
owner = None
candidate = None
sockets = weakref.WeakSet()


def run(*args):
    return subprocess.run(list(args), capture_output=True, text=True, check=True, timeout=5)


def ip_json(*args):
    return json.loads(run('ip', '-j', '-4', *args).stdout)


def tun_identity(dev):
    firewall.valid_interface(dev)
    links = json.loads(run('ip', '-j', '-d', 'link', 'show', 'dev', dev).stdout)
    if len(links) != 1:
        raise OSError('VPN link missing')
    link = links[0]
    if ('UP' not in link.get('flags', []) or
            link.get('linkinfo', {}).get('info_kind') != 'tun' or
            link.get('linkinfo', {}).get('info_data', {}).get('type') != 'tun'):
        raise OSError('VPN link invalid')
    addresses = ip_json('addr', 'show', 'dev', dev)
    if not any(a.get('family') == 'inet' and not ipaddress.ip_address(a['local']).is_unspecified
               for item in addresses for a in item.get('addr_info', [])):
        raise OSError('VPN address missing')
    return link['ifindex']


def routing_valid(dev):
    routes = ip_json('route', 'show', 'table', '100')
    rules = ip_json('rule', 'show')
    wanted = [r for r in rules if r.get('priority') == 100]
    return (len(routes) == 1 and routes[0].get('dst') == 'default'
            and routes[0].get('dev') == dev and routes[0].get('type', 'unicast') == 'unicast'
            and len(wanted) == 1 and str(wanted[0].get('table')) == '100'
            and str(wanted[0].get('fwmark')) in (str(firewall.MARK), hex(firewall.MARK))
            and str(wanted[0].get('fwmask', '0xffffffff')) in ('0xffffffff', str(0xffffffff))
            and not any(r.get('priority', 32766) < 100 and r.get('priority') != 0 for r in rules))


def setup_policy_routing(dev):
    firewall.valid_interface(dev)
    # Called only behind the all-marked DROP gate. Never modify main routing.
    for rule in ip_json('rule', 'show'):
        if rule.get('priority') == 100 or str(rule.get('table')) == '100':
            run('ip', '-4', 'rule', 'del', 'priority', str(rule['priority']))
    # Query all tables: querying/flushing an absent table can fail on first boot.
    if any(str(r.get('table')) == '100' for r in ip_json('route', 'show', 'table', 'all')):
        run('ip', '-4', 'route', 'flush', 'table', '100')
    run('ip', '-4', 'route', 'replace', 'default', 'dev', dev, 'table', '100')
    run('ip', '-4', 'rule', 'add', 'priority', '100', 'fwmark', hex(firewall.MARK) + '/0xffffffff', 'lookup', '100')
    if not routing_valid(dev):
        raise OSError('VPN policy verification failed')


def revoke():
    global interface
    with LOCK:
        interface = None
        for sock in list(sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        sockets.clear()
        try:
            STATE.unlink(missing_ok=True)
        finally:
            firewall.install()  # Failure propagates; never continue activation.


def begin(process):
    global owner, candidate
    with LOCK:
        revoke()
        owner, candidate = process, None


def event(process, line):
    global candidate, interface
    with LOCK:
        if process is not owner:
            return
        match = re.search(r'TUN/TAP device (tun[0-9]+) opened', line)
        if match:
            revoke()
            candidate = match[1]
        lower = line.lower()
        if any(s in lower for s in ('restarting', 'closing tun', 'sigterm', 'exiting', 'auth_failed', 'fatal')):
            revoke()
            candidate = None
        if 'initialization sequence completed' in lower:
            revoke()
            if candidate is None or process.poll() is not None:
                raise OSError('No owned VPN interface')
            index = tun_identity(candidate)
            setup_policy_routing(candidate)
            firewall.install(candidate)
            if not firewall.verified(candidate):
                raise OSError('VPN firewall verification failed')
            interface = (candidate, index)
            try:
                publish()
            except Exception:
                revoke()
                raise


def require_interface():
    with LOCK:
        if (interface is None or owner is None or owner.poll() is not None or
                tun_identity(interface[0]) != interface[1] or
                not routing_valid(interface[0]) or not firewall.verified(interface[0])):
            raise OSError('VPN unavailable')
        return interface[0]


def bind_socket(sock):
    with LOCK:
        dev = require_interface()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, firewall.MARK)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, dev.encode() + b'\0')
        sockets.add(sock)


def publish():
    dev = require_interface()
    record = {'interface': dev, 'ifindex': interface[1], 'pid': owner.pid, 'updated': time.time()}
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(record), encoding='utf-8')
    temporary.replace(STATE)


def healthy():
    try:
        state = json.loads(STATE.read_text(encoding='utf-8'))
        if not 0 <= time.time() - state['updated'] < 10:
            return False
        os.kill(state['pid'], 0)
        dev = state['interface']
        return tun_identity(dev) == state['ifindex'] and routing_valid(dev) and firewall.verified(dev)
    except Exception:
        return False


def monitor():
    while True:
        time.sleep(1)
        with LOCK:
            if interface is not None:
                try:
                    publish()
                except Exception:
                    try:
                        revoke()
                    except Exception:
                        pass  # Sockets/readiness already revoked; old nft transaction remains.


def start_monitor():
    threading.Thread(target=monitor, daemon=True).start()
