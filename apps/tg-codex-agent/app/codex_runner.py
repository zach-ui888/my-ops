import os
import subprocess
from pathlib import Path


# ============================================================
# 配置
# ============================================================

WORK_ROOT = Path(
    os.getenv(
        "CODEX_WORK_ROOT",
        "/ops/apps",
    )
).resolve()

RUNNER_USER = os.getenv(
    "CODEX_RUNNER_USER",
    "codex-runner",
)

RUNNER_HOME = Path(
    os.getenv(
        "CODEX_RUNNER_HOME",
        "/home/codex-runner",
    )
).resolve()

CODEX_BIN = Path(
    os.getenv(
        "CODEX_RUNNER_BIN",
        "/home/codex-runner/.local/bin/codex",
    )
).resolve()

CODEX_TIMEOUT = int(
    os.getenv(
        "CODEX_EXEC_TIMEOUT",
        "600",
    )
)


# Agent 自己的目录不能作为开发项目
PROTECTED_PROJECTS = {
    "tg-codex-agent",
}


# ============================================================
# 工作目录验证
# ============================================================

def validate_workdir(workdir):
    path = Path(workdir).resolve()

    if not path.exists():
        raise RuntimeError(
            f"工作目录不存在：{path}"
        )

    if not path.is_dir():
        raise RuntimeError(
            f"工作路径不是目录：{path}"
        )

    if path == WORK_ROOT:
        raise RuntimeError(
            "禁止直接操作 /ops/apps 根目录"
        )

    try:
        relative = path.relative_to(
            WORK_ROOT
        )
    except ValueError:
        raise RuntimeError(
            f"工作目录超出允许范围：{path}"
        )

    if not relative.parts:
        raise RuntimeError(
            "无效工作目录"
        )

    project_name = relative.parts[0]

    if project_name in PROTECTED_PROJECTS:
        raise RuntimeError(
            f"禁止 Codex 修改受保护项目：{project_name}"
        )

    return path


# ============================================================
# Prompt 安全规则
# ============================================================

def build_prompt(user_prompt):
    return f"""
You are running as a restricted Linux development user.

SECURITY RULES:

1. Work only inside the current project directory.
2. Do not attempt to access files outside the current project.
3. Never access /ops/conf.
4. Never access /root.
5. Never access another user's home directory.
6. Never read real .env files, credentials, passwords, tokens,
   API keys, SSH keys, private keys, or authentication sessions.
7. Never modify /etc or system configuration.
8. Never use sudo, su, runuser, setuid, or privilege escalation.
9. Never run git push.
10. Never upload project files or secrets to external services.
11. Do not disable or bypass sandbox or Linux security controls.
12. If the project needs secrets, use environment-variable
    references and create .env.example with placeholder values only.
13. You may create, modify, delete, build, and test files only
    inside the current project directory.
14. Do not modify the TG Codex Agent itself.
15. At completion, provide a concise report containing:
    - result
    - files created/modified
    - tests executed
    - errors or remaining work

USER TASK:

{user_prompt}
""".strip()


# ============================================================
# Codex 执行
# ============================================================

def run_codex(
    workdir,
    user_prompt,
):
    workdir = validate_workdir(
        workdir
    )

    if not CODEX_BIN.exists():
        raise RuntimeError(
            f"Codex CLI 不存在：{CODEX_BIN}"
        )

    prompt = build_prompt(
        user_prompt
    )

    command = [
        "runuser",
        "-u",
        RUNNER_USER,
        "--",
        "env",
        f"HOME={RUNNER_HOME}",
        f"CODEX_HOME={RUNNER_HOME / '.codex'}",
        str(CODEX_BIN),
        "exec",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        prompt,
    ]

    try:
        result = subprocess.run(
            command,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=CODEX_TIMEOUT,
        )

    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Codex 执行超时：{CODEX_TIMEOUT} 秒"
        )

    if result.returncode != 0:
        error_text = (
            result.stderr.strip()
            or result.stdout.strip()
            or "unknown error"
        )

        raise RuntimeError(
            "Codex 执行失败：\n"
            + error_text[-4000:]
        )

    output = result.stdout.strip()

    if not output:
        output = "Codex 执行完成，但没有返回文本。"

    return output


# ============================================================
# CLI 测试
# ============================================================

if __name__ == "__main__":
    test_workdir = (
        "/ops/apps/codex-test"
    )

    test_prompt = """
Create a file named controller-runner-test.txt
in the current project directory.

Its content must be exactly:

CONTROLLER_RUNNER_OK

Then verify the file content.
""".strip()

    print(
        "=== Restricted Codex Executor ==="
    )

    print(
        f"Runner: {RUNNER_USER}"
    )

    print(
        f"Workdir: {test_workdir}"
    )

    print()

    output = run_codex(
        test_workdir,
        test_prompt,
    )

    print(
        "=== Codex Output ==="
    )

    print(
        output
    )
