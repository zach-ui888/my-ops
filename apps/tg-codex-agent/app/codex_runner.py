import os
import subprocess
from pathlib import Path


# ============================================================
# 配置
# ============================================================

from runner_config import runner_config

# Compatibility exports; runtime validation also reads current configuration.
_config = runner_config()
WORK_ROOT = _config.work_root
RUNNER_USER = _config.user
RUNNER_HOME = _config.home
CODEX_BIN = _config.binary
CODEX_TIMEOUT = _config.timeout


# Agent 自己的目录不能作为开发项目
PROTECTED_PROJECTS = {
    "tg-codex-agent",
}


# ============================================================
# 工作目录验证
# ============================================================

def validate_workdir(workdir):
    WORK_ROOT = runner_config().work_root
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
    from account_client import request
    return request('task', workdir=str(validate_workdir(workdir)), prompt=user_prompt)['output']
