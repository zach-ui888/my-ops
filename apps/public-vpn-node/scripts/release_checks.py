"""Offline release checks over the public allowlist only."""
import hashlib
from pathlib import Path
import subprocess
from static_checks import BASE, PUBLIC, check


def main():
    errors = check()
    license_path = BASE / 'licenses/GPL-3.0.txt'
    expected = '3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986'
    if hashlib.sha256(license_path.read_bytes()).hexdigest() != expected:
        errors.append('完整 GPL 正文指纹不符')
    work = BASE / '.test-work'
    work.mkdir(exist_ok=True)
    empty = work / 'release-empty'
    empty.write_bytes(b'')
    for rel in PUBLIC:
        # Treat every line as added, including files that have never been tracked.
        result = subprocess.run(['git', '-c', 'core.whitespace=blank-at-eol,blank-at-eof,space-before-tab',
                                 'diff', '--no-index', '--check', str(empty), str(BASE / rel)],
                                capture_output=True)
        # --no-index returns 1 for different files even when --check is clean.
        if result.returncode not in (0, 1) or result.stdout or result.stderr:
            errors.append(rel + ': git diff --no-index --check 失败')
    rules = (BASE / '.gitignore').read_text().splitlines()
    for rule in ('secrets/', 'reference/', '.test-work/', 'data/', 'runtime/',
                 '*.log', '.env', '.env.*', '*.key', '*.pem', '.auth/', '.agents/', '.codex/'):
        if rule not in rules:
            errors.append('.gitignore 缺少规则: ' + rule)
    if errors:
        for error in errors:
            print(error)
        return 1
    print(f'Release checks PASS: {len(PUBLIC)} public files; full GPL digest; whitespace; exclusions.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
