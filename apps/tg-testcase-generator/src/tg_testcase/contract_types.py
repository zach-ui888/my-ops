"""Strict internal V1 primitives; independent of public package/wire schemas."""
import hashlib
import json
import re

MAX_I63 = 2**63 - 1


def require(condition):
    if not condition:
        raise ValueError('Invalid contract value')


def integer(value, minimum=0, maximum=MAX_I63):
    require(type(value) is int and minimum <= value <= maximum)
    return value


def string(value, pattern):
    require(type(value) is str and re.fullmatch(pattern, value) is not None)
    return value


def name(value):
    return string(value, r'[a-z][a-z0-9_-]{0,63}')


def identifier(value):
    string(value, r'[A-Za-z0-9][A-Za-z0-9_.-]{0,119}')
    require('..' not in value)
    return value


def id32(value):
    return string(value, r'[0-9a-f]{32}')


def sha(value):
    return string(value, r'[0-9a-f]{64}')


def principal(value):
    string(value, r'telegram:[1-9][0-9]{0,15}')
    integer(int(value[9:]), 1, 4503599627370495)
    return value


def fields(value, keys):
    require(type(value) is dict and set(value) == set(keys.split()))
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_json(raw, *, limit=1048576, depth=16, ascii_only=False):
    require(type(raw) is bytes and len(raw) <= limit and not raw.startswith(b'\xef\xbb\xbf'))
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result)
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('Invalid JSON number')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid,
                           parse_float=invalid)
        def walk(item, level):
            require(level <= depth)
            if type(item) is str:
                item.encode('ascii' if ascii_only else 'utf-8')
            elif type(item) is dict:
                for k, v in item.items():
                    walk(k, level)
                    walk(v, level + 1)
            elif type(item) is list:
                for v in item:
                    walk(v, level + 1)
            else:
                require(item is None or type(item) in (bool, int))
        walk(value, 1)
        return value
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError('Invalid JSON encoding') from None
