"""Safe nft diagnostics: never propagate command output into lifecycle logs."""
import json
import subprocess


class NftError(OSError):
    pass


class NftCommandError(NftError):
    pass


class NftInstallError(NftError):
    pass


class NftJSONError(NftError):
    pass


class NftMismatchError(NftError):
    pass


def execute(argv, **kwargs):
    try:
        return subprocess.run(argv, text=True, check=True, timeout=5, **kwargs)
    except subprocess.CalledProcessError:
        error = NftInstallError if '-f' in argv else NftCommandError
        raise error() from None
    except (OSError, subprocess.TimeoutExpired):
        raise NftCommandError() from None


def install(rules):
    execute(['nft', '-f', '-'], input=rules,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def readback(table):
    result = execute(['nft', '-j', 'list', 'table', 'inet', table], capture_output=True)
    try:
        entries = json.loads(result.stdout)['nftables']
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError()
        return entries
    except (ValueError, TypeError, KeyError):
        raise NftJSONError() from None


def expressions(rule):
    # Ignore only standalone counters, never an expression containing a verdict.
    return [e for e in rule['expr'] if set(e) != {'counter'}]


def scoped(entries, table):
    for entry in entries:
        if len(entry) != 1:
            return False
        kind, value = next(iter(entry.items()))
        if kind == 'metainfo':
            continue
        if kind not in ('table', 'chain', 'rule') or not isinstance(value, dict):
            return False
        if value.get('family', 'inet') != 'inet' or value.get('flags'):
            return False
        key = 'name' if kind == 'table' else 'table'
        if value.get(key, table) != table:
            return False
    return True
