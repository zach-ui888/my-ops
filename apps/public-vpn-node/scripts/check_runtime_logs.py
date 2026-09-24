"""Administrator-only log check. Invoke manually after deployment, never in offline CI."""
import getpass
import subprocess
import sys


def main():
    if not sys.stdin.isatty():
        raise ValueError('需要私密交互终端')
    values = [getpass.getpass(field + '（隐藏输入，仅用于内存比对）: ')
              for field in ('username', 'password', 'secret_path', 'proxy_username', 'proxy_password')]
    if not all(values):
        raise ValueError('字段不能为空')
    result = subprocess.run(['docker', 'compose', '-p', 'public-vpn-node', 'logs',
                             '--no-color', '--no-log-prefix', '--tail=1000', 'vpn'],
                            capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('日志获取失败')
    leaked = any(value.encode() in result.stdout for value in values)
    allowed = {
        'public-vpn-node: 凭据校验或容器防泄漏规则安装失败；拒绝启动。',
        'public-vpn-node: 启动；仅输出生命周期事件，诊断请查看本机管理页面。',
        'public-vpn-node: 服务异常退出；未输出异常内容。',
    }
    unexpected = any(line.strip() not in allowed for line in result.stdout.decode('utf-8', 'replace').splitlines() if line.strip())
    print('日志秘密命中=' + str(leaked) + ' 非预期日志行=' + str(unexpected))
    print('仅检查最近 1000 行；会话 Cookie 应另在私密浏览器中核验，不导出 HAR。')
    return int(leaked or unexpected)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, EOFError, subprocess.TimeoutExpired):
        print('日志检查未完成；未输出日志或秘密。')
        raise SystemExit(2)
