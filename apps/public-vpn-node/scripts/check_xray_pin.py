"""Offline public-file pin validation; only accepts the root-verified exact digest."""
import argparse
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TAG = 'ghcr.io/xtls/xray-core:26.3.27'
REPO = 'ghcr.io/xtls/xray-core'
DIGEST = REPO + '@sha256:592ec4d11f656db95598d01e76dbcc6e002d67360b96a5436500a938230f52c7'


def validate(lock, compose):
    errors = []
    if (lock.get('version'), lock.get('tag'), lock.get('reality_revision')) != (
            '26.3.27', TAG, '9234c772ba8f'):
        errors.append('必须固定正式版 26.3.27 / REALITY 9234c772ba8f')
    if lock.get('platform') != 'linux/amd64':
        errors.append('必须使用 root 核验的平台 linux/amd64')
    if lock.get('status') != 'verified-root-pull':
        errors.append('缺少 root 拉取核验状态')
    if lock.get('repo_digest') != DIGEST:
        errors.append('锁文件必须使用 root 核验的精确 RepoDigest')
    if compose.get('services', {}).get('ingress', {}).get('image') != DIGEST:
        errors.append('Compose 必须使用精确 RepoDigest；禁止 tag/latest/环境变量/其它 digest')
    return errors


def check():
    paths = [BASE / 'examples/xray.lock.json', BASE / 'compose.v1.1.yaml']
    if any(p.is_symlink() or not p.is_file() or p.parent.is_symlink() for p in paths):
        return ['公开固定文件缺失或为符号链接']
    try:
        return validate(*(json.loads(p.read_text()) for p in paths))
    except (ValueError, TypeError, AttributeError):
        return ['公开固定文件结构无效']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    problems = check()
    for problem in problems:
        print(problem)
    if not problems:
        print('Xray pin PASS')
    raise SystemExit(1 if problems else 0)
