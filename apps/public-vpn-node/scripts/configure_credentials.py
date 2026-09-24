"""Administrator-only interactive setup; never prints or reads existing secrets."""
import getpass
import json
import os
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / 'app'))
from security import SECRET_FIELDS, validate_credentials


def main():
    if Path.cwd() != BASE or not sys.stdin.isatty():
        raise ValueError('请在项目根目录的私密交互终端运行')
    target = BASE / 'secrets'
    if target.is_symlink():
        raise ValueError('拒绝符号链接目录')
    os.umask(0o077)
    target.mkdir(mode=0o700, exist_ok=True)
    if target.stat().st_mode & 0o077:
        raise ValueError('secrets 目录必须为 0700')
    values = {key: getpass.getpass(key + '（隐藏输入）: ') for key in SECRET_FIELDS}
    validate_credentials(values)
    if getpass.getpass('再次输入管理密码: ') != values['password']:
        raise ValueError('密码确认不一致')
    if getpass.getpass('再次输入代理密码: ') != values['proxy_password']:
        raise ValueError('密码确认不一致')
    # Exclusive creation avoids overwriting or following an existing secret.
    fd = os.open(target / 'credentials.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(values, stream)
    print('已保存凭据（0600）；请使用密码管理器保管，不通过日志获取。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, EOFError):
        print('配置失败：检查输入、目录权限及文件是否已存在；未输出秘密。', file=sys.stderr)
        sys.exit(1)
