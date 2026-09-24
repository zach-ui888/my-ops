"""Manual acceptance helper. Administrator supplies credentials interactively.
No secret file reads, argv secrets, environment secrets, or raw error output.
"""
import argparse
import getpass
import ipaddress
import subprocess
import time
import urllib.parse


def curl_auth(username, password):
    if any(ord(c) < 33 or ord(c) > 126 for c in username + password) or ':' in username:
        raise ValueError('凭据格式错误')
    value = (username + ':' + password).replace('\\', '\\\\').replace('"', '\\"')
    return 'proxy-user = "' + value + '"\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--protocol', choices=['http', 'socks5h'], required=True)
    parser.add_argument('--expect', choices=['success', 'blocked'], default='success')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--url', default='https://api.ipify.org')
    parser.add_argument('--stream', action='store_true', help='持续 HTTPS 下载，仅报告状态和字节数')
    parser.add_argument('--timeout', type=int, default=30)
    args = parser.parse_args()
    target = urllib.parse.urlsplit(args.url)
    if target.scheme != 'https' or not target.hostname or target.username or target.password:
        parser.error('仅允许无认证信息的 HTTPS 验收 URL')
    if not 1 <= args.repeat <= 100 or not 1 <= args.timeout <= 300:
        parser.error('次数须为 1–100，超时须为 1–300 秒')
    username = getpass.getpass('代理账号（隐藏输入）: ')
    password = getpass.getpass('代理密码（隐藏输入）: ')
    config = curl_auth(username, password)
    failed = False
    for i in range(args.repeat):
        cmd = ['curl', '-q', '--silent', '--fail', '--noproxy', '', '--proto', '=https',
               '--proxy', args.protocol + '://127.0.0.1:7928', '--config', '-',
               '--max-time', str(args.timeout)]
        if args.stream:
            cmd += ['--limit-rate', '1024', '--output', '/dev/null', '--write-out', '%{http_code} %{size_download}']
        else:
            cmd += ['--max-filesize', '1048576']
        cmd += [args.url]
        result = subprocess.run(cmd, input=config, text=True, capture_output=True, timeout=args.timeout + 5)
        ok = result.returncode == 0
        expected = ok if args.expect == 'success' else not ok
        failed |= not expected
        detail = ''
        if ok and args.url == 'https://api.ipify.org':
            try:
                detail = ' 出口 IP=' + str(ipaddress.ip_address(result.stdout.strip()))
            except ValueError:
                expected = False
                failed = True
                detail = ' 出口响应格式无效'
        elif args.stream:
            parts = result.stdout.split()
            if len(parts) == 2 and all(x.replace('.', '', 1).isdigit() for x in parts):
                detail = ' HTTP=' + parts[0] + ' 接收字节=' + parts[1]
        print(f'{i + 1}: curl退出码={result.returncode} 预期符合={expected}{detail}', flush=True)
        if i + 1 < args.repeat:
            time.sleep(1)
    return int(failed)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, EOFError, subprocess.TimeoutExpired):
        print('验收工具失败；未输出凭据、响应正文或原始异常。')
        raise SystemExit(2)
