"""Invoked only by the installed controller as codex-runner."""
import json
import os
import pwd
import sys
from account_core import Engine, SafeError
from runner_config import runner_config


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main():
    try:
        if os.getuid() == 0 or pwd.getpwuid(os.getuid()).pw_name != runner_config().user:
            raise SafeError('执行身份错误')
        request = json.loads(sys.stdin.readline())
        engine = Engine(timeout=request.get('timeout', 300))
        with engine.lock(wait=5 if request.get('action') == 'task' else 0) as fd:
            action = request['action']
            if action == 'login':
                result = engine.login(fd, emit)
            elif action == 'status':
                result = engine.status(fd=fd)
            elif action == 'quota':
                result = engine.quota(fd, request.get('refresh', True), request.get('max_age', 900))
            elif action == 'task':
                from codex_runner import validate_workdir, build_prompt, CODEX_TIMEOUT
                code, output = engine.capture(['exec', '--sandbox', 'workspace-write',
                    '--skip-git-repo-check', build_prompt(request['prompt'])], fd=fd,
                    cwd=validate_workdir(request['workdir']), timeout=CODEX_TIMEOUT, include_stderr=False)
                if code:
                    raise SafeError('Codex Task 非零退出')
                result = {'output': output.decode('utf-8', 'replace').strip() or 'Codex 执行成功，但未返回文本输出。'}
            else:
                raise SafeError('无效操作')
        emit({'event': 'result', 'result': result})
    except SafeError as exc:
        emit({'event': 'error', 'message': str(exc)})
        return 1
    except Exception:
        emit({'event': 'error', 'message': '执行器忙、操作失败或超时；请稍后重试'})
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
