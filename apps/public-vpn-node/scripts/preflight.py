"""Offline, project-only static checks. Never reads .env, secrets or runtime data."""
import ast
import hashlib
import json
from pathlib import Path

COMMIT = '14b803685e67b7d50deff8ee768d042d96dcaab1'
BASE = Path(__file__).resolve().parent.parent


def validate_compose(cfg):
    errors = []
    try:
        s = cfg['services']['vpn']
        if set(cfg['services']) != {'vpn'}:
            errors.append('服务范围变化')
        if s.get('privileged') or s.get('network_mode') or s.get('pid') or s.get('ipc'):
            errors.append('禁止宿主命名空间或 privileged')
        if s.get('cap_drop') != ['ALL'] or set(s.get('cap_add', [])) != {'NET_ADMIN', 'NET_RAW'}:
            errors.append('capability 不符合审查基线')
        if s.get('ports') != ['127.0.0.1:8787:8787', '127.0.0.1:7928:7928']:
            errors.append('端口必须仅绑定回环')
        if s.get('devices') != ['/dev/net/tun:/dev/net/tun:rw']:
            errors.append('只允许 TUN 设备')
        if s.get('volumes') != ['vpn_data:/data'] or s.get('secrets') != ['credentials']:
            errors.append('非预期挂载')
        if cfg['secrets'] != {'credentials': {'file': './secrets/credentials.json'}}:
            errors.append('凭据必须使用项目内只读 secret')
        if not s.get('read_only') or s.get('security_opt') != ['no-new-privileges:true']:
            errors.append('只读根文件系统/禁止新增权限未启用')
        if s.get('restart') != 'unless-stopped' or not s.get('init'):
            errors.append('重启/子进程回收配置不一致')
        if s.get('healthcheck', {}).get('test') != ['CMD', 'python3', '/app/healthcheck.py']:
            errors.append('健康检查不一致')
        if s.get('networks') != ['vpn_private'] or s.get('environment') != {'CREDENTIALS_FILE': '/run/secrets/credentials', 'MAX_SCAN_ROWS': '60'}:
            errors.append('网络/环境配置不一致')
        if s.get('sysctls') != {'net.ipv4.conf.all.rp_filter': '2', 'net.ipv4.conf.default.rp_filter': '2'}:
            errors.append('sysctl 超出容器审查基线')
        if cfg.get('networks') != {'vpn_private': {'driver': 'bridge'}}:
            errors.append('必须使用独立 bridge 网络')
    except (KeyError, TypeError):
        errors.append('Compose 结构缺失')
    return errors


def check(base=BASE):
    if Path.cwd() != base:
        return {'结果': '失败', '错误': ['请从项目根目录执行']}, 2
    errors = []
    # Explicit public-file allowlist; no recursive secret scan.
    public = ['compose.yaml', 'Dockerfile', '.gitignore', '.dockerignore', '.env.example',
              'app/entrypoint.py', 'app/firewall.py', 'app/security.py', 'app/proxy_server.py',
              'app/vpngate_manager.py', 'app/vpn_utils.py', 'scripts/healthcheck.py']
    for rel in public:
        path = base / rel
        if path.is_symlink() or not path.is_file():
            errors.append('公开文件缺失或为符号链接: ' + rel)
            continue
        if path.stat().st_mode & 0o022:
            errors.append('公开文件不应允许组/其他用户写入: ' + rel)
        if path.suffix == '.py':
            try:
                ast.parse(path.read_text(encoding='utf-8'))
            except SyntaxError:
                errors.append('Python 语法错误: ' + rel)
    if errors:
        return {'结果': '失败', '错误': errors}, 2
    errors += validate_compose(json.loads((base / 'compose.yaml').read_text()))
    for filename in ('.gitignore', '.dockerignore'):
        rules = (base / filename).read_text().splitlines()
        for entry in ('reference/', 'secrets/', '.env', '.env.*'):
            if entry not in rules:
                errors.append(filename + ' 缺少排除规则: ' + entry)
    dockerfile = (base / 'Dockerfile').read_text()
    if 'COPY . ' in dockerfile or 'install.sh' in dockerfile or 'docker.sock' in dockerfile:
        errors.append('Dockerfile 存在非预期构建输入')
    manifest = json.loads((base / 'docs/上游文件指纹.json').read_text())
    source = base / 'reference/source'
    # Review source is optional after handoff. If present, verify all public files.
    if source.is_dir():
        if source.is_symlink() or (base / 'reference').is_symlink():
            errors.append('审查源码目录不能为符号链接')
        else:
            if (base / 'reference/UPSTREAM_COMMIT').read_text().strip() != COMMIT:
                errors.append('交付 commit 记录不符')
            if (source / 'LICENSE').read_bytes() != (base / 'licenses/UPSTREAM-LICENSE.txt').read_bytes():
                errors.append('上游许可证副本不一致')
            allowed_source = {'.dockerignore', '.gitignore', 'Dockerfile', 'LICENSE', 'README.md', 'docker/ml', 'docker-compose.yml', 'install.sh', 'proxy_server.py', 'vpn_utils.py', 'vpngate_manager.py'}
            if set(manifest) != allowed_source:
                errors.append('上游指纹清单偏离公开文件白名单')
                return {'结果': '失败', '错误': errors}, 2
            for rel, digest in manifest.items():
                path = source / rel
                if '..' in Path(rel).parts or Path(rel).is_absolute() or path.is_symlink():
                    errors.append('上游清单路径不安全')
                    continue
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    errors.append('上游文件指纹不符: ' + rel)
    return {'结果': '静态检查通过' if not errors else '失败', '错误': errors,
            'commit': COMMIT, '运行验收': '未执行；需管理员验证',
            '许可证': 'GPL-3.0-or-later；完整正文及上游声明见 licenses/',
            'kill_switch': '带标记代理 TCP/DNS 的 nftables 防泄漏规则已实现；运行效果待验证'}, 2 if errors else 0


if __name__ == '__main__':
    result, code = check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(code)
