"""Independent IPv4/IPv6 output boundary for capability-free Xray UID 65532.
Never applied by offline tools. No broad established-flow exception.
"""
import subprocess
import nft_support

TABLE = 'public_vpn_ingress'
UID = 65532


def ruleset():
    return f'''add table inet {TABLE}
flush table inet {TABLE}
add chain inet {TABLE} output {{ type filter hook output priority -10; policy accept; }}
add rule inet {TABLE} output meta skuid {UID} ip daddr 127.0.0.1 tcp dport {{ 7928, 9443 }} accept
add rule inet {TABLE} output meta skuid {UID} tcp sport 8443 ct direction reply ct state established accept
add rule inet {TABLE} output meta skuid {UID} counter drop
'''


def install():
    nft_support.install(ruleset())


def verified():
    entries = nft_support.readback(TABLE)
    try:
        return matches(entries)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def matches(entries):
    if not nft_support.scoped(entries, TABLE):
        return False
    chains = [e['chain'] for e in entries if 'chain' in e]
    if len(chains) != 1 or any(chains[0].get(k) != v for k, v in
                             {'name': 'output', 'type': 'filter', 'hook': 'output',
                              'prio': -10, 'policy': 'accept'}.items()):
        return False
    def match(left, right):
        return {'match': {'op': '==', 'left': left, 'right': right}}
    uid = match({'meta': {'key': 'skuid'}}, UID)
    expected = [
        [uid, match({'payload': {'protocol': 'ip', 'field': 'daddr'}}, '127.0.0.1'),
         match({'payload': {'protocol': 'tcp', 'field': 'dport'}}, {'set': [7928, 9443]}), {'accept': None}],
        [uid, match({'payload': {'protocol': 'tcp', 'field': 'sport'}}, 8443),
         match({'ct': {'key': 'direction'}}, 'reply'),
         match({'ct': {'key': 'state'}}, 'established'), {'accept': None}],
        [uid, {'drop': None}],
    ]
    rules = [e['rule'] for e in entries if 'rule' in e]
    # nft emits bitmask state membership as "in". Normalize only this exact
    # single state, and the unordered, exact two-port anonymous set.
    actual = []
    for rule in rules:
        expr = nft_support.expressions(rule)
        normalized = []
        for e in expr:
            # Some versions retain the implicit TCP dependency in inet JSON.
            if e == match({'meta': {'key': 'l4proto'}}, 'tcp') and len(actual) < 2:
                continue
            m = e.get('match', {})
            if (set(e) == {'match'} and set(m) == {'op', 'left', 'right'}
                    and m['left'] == {'ct': {'key': 'state'}}
                    and m['op'] in ('in', '==')
                    and m['right'] in ('established', {'set': ['established']})):
                e = match({'ct': {'key': 'state'}}, 'established')
            elif (set(e) == {'match'} and set(m) == {'op', 'left', 'right'}
                  and m['left'] == {'payload': {'protocol': 'tcp', 'field': 'dport'}}
                  and m['op'] == '==' and m['right'] == {'set': [9443, 7928]}):
                e = match(m['left'], {'set': [7928, 9443]})
            normalized.append(e)
        actual.append(normalized)
    return len(rules) == 3 and all(r.get('chain') == 'output' for r in rules) and actual == expected
