"""Shared runner settings, read after the controller loads dotenv."""
import os
import re
from pathlib import Path
from dataclasses import dataclass


@dataclass(frozen=True)
class RunnerConfig:
    user: str
    home: Path
    binary: Path
    timeout: int
    work_root: Path

    @property
    def codex_home(self):
        return self.home / '.codex'


def runner_config():
    user = os.getenv('CODEX_RUNNER_USER', 'codex-runner')
    if user == 'root' or not re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', user):
        raise ValueError('runner 必须是非 root 用户')
    home = Path(os.getenv('CODEX_RUNNER_HOME', '/home/codex-runner'))
    binary = Path(os.getenv('CODEX_RUNNER_BIN', '/home/codex-runner/.local/bin/codex'))
    if not home.is_absolute() or not binary.is_absolute() or home == Path('/root') or Path('/root') in home.parents:
        raise ValueError('runner 路径配置无效')
    return RunnerConfig(user, home, binary, int(os.getenv('CODEX_EXEC_TIMEOUT', '3600')),
                        Path(os.getenv('CODEX_WORK_ROOT', '/ops/apps')).resolve())
