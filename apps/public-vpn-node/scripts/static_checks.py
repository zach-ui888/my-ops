"""Offline review of explicitly public project files; never open runtime secrets."""
import ast
import fnmatch
import json
from pathlib import Path
import re
import subprocess

BASE = Path(__file__).resolve().parent.parent
PUBLIC = [
    'README.md', 'Dockerfile', 'compose.yaml', '.env.example', '.gitignore', '.dockerignore',
    'app/vpn_runtime.py', 'scripts/test_vpn_runtime.py', 'app/entrypoint.py', 'app/firewall.py', 'app/security.py', 'app/vpn_utils.py',
    'app/proxy_server.py', 'app/vpngate_manager.py',
    'scripts/configure_credentials.py', 'scripts/healthcheck.py', 'scripts/preflight.py',
    'scripts/test_preflight.py', 'scripts/static_checks.py', 'scripts/verify.sh',
    'scripts/probe_proxy.py', 'scripts/check_runtime_logs.py',
    'docs/源码审查.md', 'docs/源码审查清单.md', 'docs/测试报告.md',
    'docs/人工Docker验收.md', 'docs/续作记录.md', 'docs/上游文件指纹.json',
    'licenses/UPSTREAM-LICENSE.txt', 'licenses/NOTICE.txt', 'licenses/GPL-3.0.txt',
    'scripts/release_checks.py',
]


def docker_included(path, rules):
    # Limited regression model for this project's simple allowlist, not Docker's parser.
    included = True
    for line in rules.splitlines():
        if not line or line.startswith('#'):
            continue
        negate = line.startswith('!')
        pattern = line.lstrip('!').rstrip('/')
        if fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern + '/'):
            included = negate
    return included


def check():
    errors = []
    bodies = {}
    for rel in PUBLIC:
        path = BASE / rel
        if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents if p != BASE and BASE in p.parents):
            errors.append(rel + ': 缺失或符号链接')
            continue
        if path.stat().st_mode & 0o022:
            errors.append(rel + ': 组/其他用户可写')
        bodies[rel] = path.read_text(encoding='utf-8')
    if errors:
        return errors
    # Report only file/category, never a matching value or source line.
    patterns = {
        '私钥正文': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{32}',
        '已知令牌格式': r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}|sk-proj-[A-Za-z0-9_-]{30,})',
        'URL 中的认证值': r'https?://[^\s/<>"\']+:[^\s/<>"\']+@',
    }
    for rel, body in bodies.items():
        for category, pattern in patterns.items():
            if re.search(pattern, body):
                errors.append(rel + ': ' + category)
        if rel.endswith('.py'):
            tree = ast.parse(body)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    names = [n.id.lower() for n in node.targets if isinstance(n, ast.Name)]
                    if any(n in ('password', 'proxy_password', 'api_key', 'access_token') for n in names) and node.value.value and not node.value.value.startswith('TEST_ONLY'):
                        errors.append(rel + ': 硬编码敏感字段')
    dockerfile = bodies['Dockerfile']
    copies = [line for line in dockerfile.splitlines() if line.startswith('COPY ')]
    if copies != ['COPY app/ /app/', 'COPY scripts/healthcheck.py /app/healthcheck.py', 'COPY licenses/ /app/licenses/']:
        errors.append('Dockerfile: 构建输入偏离白名单')
    for forbidden in ('ADD ', 'curl |', 'wget ', 'install.sh', 'docker.sock', '--privileged'):
        if forbidden in dockerfile:
            errors.append('Dockerfile: 禁止的构建指令')
    if 'FROM debian:12-slim' not in dockerfile or 'nftables' not in dockerfile or 'ENTRYPOINT ["python3", "/app/entrypoint.py"]' not in dockerfile:
        errors.append('Dockerfile: 基础镜像/防火墙/入口不一致')
    for path in ('reference/source/vpngate_manager.py', 'reference/source/.git/config', '.git/config',
                 'runtime/state.json', '.test-work/cache.pyc', 'service.log', '.env', '.env.production', 'secrets/credentials.json', 'data/nodes.json',
                 'app/__pycache__/security.pyc', 'scripts/configure_credentials.py'):
        if docker_included(path, bodies['.dockerignore']):
            errors.append('构建上下文不应包含: ' + path)
    for path in ('app/vpn_runtime.py', 'app/security.py', 'app/firewall.py', 'scripts/healthcheck.py', 'licenses/NOTICE.txt', 'licenses/UPSTREAM-LICENSE.txt', 'licenses/GPL-3.0.txt'):
        if not docker_included(path, bodies['.dockerignore']):
            errors.append('构建上下文缺少: ' + path)
    for rel, body in bodies.items():
        chunks = [body] if rel.endswith('.sh') else re.findall(r'```bash\n(.*?)```', body, re.S)
        for chunk in chunks:
            result = subprocess.run(['bash', '-n'], input=chunk, text=True, capture_output=True)
            if result.returncode:
                errors.append(rel + ': shell 语法失败')
    return errors


if __name__ == '__main__':
    problems = check()
    print(json.dumps({'公开文件扫描': len(PUBLIC), '错误': problems,
                      '边界': '未读取真实凭据；模式扫描不能证明不存在未知格式秘密；Docker 语义待人工验收'}, ensure_ascii=False))
    raise SystemExit(1 if problems else 0)
