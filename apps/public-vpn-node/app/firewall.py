"""Container-only mark backstop; unmarked OpenVPN transport remains permitted."""
import re
import subprocess
import nft_support

MARK = 0x56504E
TABLE = 'public_vpn_node'


def valid_interface(interface):
    if not isinstance(interface, str) or not re.fullmatch(r'tun[0-9]+', interface):
        raise ValueError('Invalid VPN interface')
    return interface


def ruleset(interface=None):
    condition = '' if interface is None else f' oifname != "{valid_interface(interface)}"'
    result = f'add table inet {TABLE}\nflush table inet {TABLE}\n'
    for chain in ('output', 'postrouting'):
        result += f'add chain inet {TABLE} {chain} {{ type filter hook {chain} priority 0; policy accept; }}\n'
        result += f'add rule inet {TABLE} {chain} meta mark 0x56504e{condition} counter drop\n'
    return result


def install(interface=None):
    # Atomic replacement: None blocks ALL marked traffic, including existing flows.
    nft_support.install(ruleset(interface))


def verified(interface=None):
    if interface is not None:
        valid_interface(interface)
    entries = nft_support.readback(TABLE)
    try:
        return matches(entries, interface)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def matches(entries, interface):
    if not nft_support.scoped(entries, TABLE):
        return False
    expected = [
        {'match': {'op': '==', 'left': {'meta': {'key': 'mark'}}, 'right': MARK}},
        {'match': {'op': '!=', 'left': {'meta': {'key': 'oifname'}}, 'right': interface}},
        {'drop': None},
    ]
    if interface is None:
        del expected[1]
    if sum('chain' in e for e in entries) != 2 or sum('rule' in e for e in entries) != 2:
        return False
    for name in ('output', 'postrouting'):
        chains = [e['chain'] for e in entries if 'chain' in e and e['chain']['name'] == name]
        if len(chains) != 1 or any(chains[0].get(k) != v for k, v in
                                  {'type': 'filter', 'hook': name, 'prio': 0, 'policy': 'accept'}.items()):
            return False
        rules = [e['rule'] for e in entries if 'rule' in e and e['rule']['chain'] == name]
        if len(rules) != 1 or nft_support.expressions(rules[0]) != expected:
            return False
    return True
