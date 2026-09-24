import tempfile
from pathlib import Path

import sys

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import task_manager


def scan_text(content: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.py"
        path.write_text(content, encoding="utf-8")
        return task_manager.scan_file_for_secrets(path)


def expect_safe(name: str, content: str):
    rules = scan_text(content)

    if rules:
        raise AssertionError(
            f"{name}: expected SAFE, got {rules}"
        )

    print(f"PASS SAFE  : {name}")


def expect_blocked(name: str, content: str, expected_rule: str):
    rules = scan_text(content)

    if expected_rule not in rules:
        raise AssertionError(
            f"{name}: expected {expected_rule}, got {rules}"
        )

    print(f"PASS BLOCK : {name} -> {expected_rule}")


def test_path_policy():
    allowed = [
        ".env.example",
        "README.md",
        "app/main.py",
        "scripts/check.py",
    ]

    blocked = [
        ".env",
        ".env.production",
        "secrets/credentials.json",
        "reference/source/main.py",
        ".test-work/result.log",
        "data/state.json",
        "logs/app.log",
        "venv/bin/python",
    ]

    for path in allowed:
        task_manager.validate_approval_relative_path(path)
        print(f"PASS PATH  : allow {path}")

    for path in blocked:
        try:
            task_manager.validate_approval_relative_path(path)
        except ValueError:
            print(f"PASS PATH  : block {path}")
        else:
            raise AssertionError(
                f"Expected blocked path: {path}"
            )


def test_secret_scanner():
    # --------------------------------------------------------
    # 正常动态 credential 使用：必须允许。
    # --------------------------------------------------------
    expect_safe(
        "getpass",
        "password = getpass.getpass('Password: ')\n",
    )

    expect_safe(
        "environment",
        "password = os.environ.get('APP_PASSWORD')\n",
    )

    expect_safe(
        "config lookup",
        "password = config['password']\n",
    )

    expect_safe(
        "request value",
        "password = request.json.get('password')\n",
    )

    expect_safe(
        "function parameter",
        "def login(username: str, password: str):\n"
        "    return username, password\n",
    )

    expect_safe(
        "json field",
        "payload = {'username': username, 'password': password}\n",
    )

    expect_safe(
        "javascript variable",
        "const password = document.getElementById('password').value;\n",
    )

    # 测试 fixture，不是真实部署凭据。
    expect_safe(
        "synthetic fixture",
        "# Synthetic values deliberately unrelated to deployment credentials.\n"
        "FAKE = dict("
        "username='fixture-user', "
        "password='fixture-password-not-a-secret', "
        "secret_path='fixture-secret-path-only'"
        ")\n",
    )

    # --------------------------------------------------------
    # 真正的硬编码 credential：必须阻止。
    # 这里使用明确测试值，不是任何真实 Secret。
    # --------------------------------------------------------
    expect_blocked(
        "hardcoded password",
        "password = '"
        "RealLikePassword_9X7q2Lm4P8v6"
        "'\n",
        "credential_assignment",
    )

    expect_blocked(
        "hardcoded client secret",
        "client_secret = '"
        "ClientSecret_7Qp9Lm2Xv8K4"
        "'\n",
        "credential_assignment",
    )

    # --------------------------------------------------------
    # 高置信 token/key 规则必须继续阻止。
    # 以下全部是 synthetic 测试字符串。
    # --------------------------------------------------------
    expect_blocked(
        "github token",
        "TOKEN = 'ghp_"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        "'\n",
        "github_token",
    )

    expect_blocked(
        "openai key",
        "KEY = 'sk-"
        "abcdefghijklmnopqrstuvwxyz1234567890"
        "'\n",
        "openai_api_key",
    )

    expect_blocked(
        "aws key",
        "AWS_ACCESS_KEY_ID = 'AKIA"
        "ABCDEFGHIJKLMNOP"
        "'\n",
        "aws_access_key",
    )

    expect_blocked(
        "private key",
        "KEY = '''-----BEGIN OPENSSH PRIVATE KEY-----\\n"
        "synthetic-test-only\\n"
        "-----END OPENSSH PRIVATE KEY-----'''\n",
        "private_key",
    )


def main():
    print("=== PATH POLICY ===")
    test_path_policy()

    print()
    print("=== SECRET SCANNER ===")
    test_secret_scanner()

    print()
    print("ALL_APPROVAL_SECURITY_TESTS=PASS")


if __name__ == "__main__":
    main()
