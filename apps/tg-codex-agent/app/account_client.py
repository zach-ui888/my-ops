import asyncio
import json
import os
from pathlib import Path
import subprocess
import threading

from account_core import SafeError
from runner_config import runner_config


def request(action, on_device=None, **kwargs):
    config = runner_config()
    command = ['runuser', '-u', config.user, '--', 'env', '-i',
               'HOME=' + str(config.home), 'CODEX_HOME=' + str(config.codex_home),
               'CODEX_RUNNER_USER=' + config.user,
               'CODEX_RUNNER_HOME=' + str(config.home),
               'CODEX_RUNNER_BIN=' + str(config.binary),
               'PATH=/usr/local/bin:/usr/bin:/bin', 'LANG=C.UTF-8',
               'CODEX_WORK_ROOT=' + str(config.work_root),
               'CODEX_EXEC_TIMEOUT=' + str(config.timeout),
               '/usr/bin/python3', str(Path(__file__).with_name('runner_bridge.py'))]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    # Bridge/CLI keep the file lock if the controller dies. Timer closes stuck IPC.
    timeout = max(30, min(int(kwargs.get('timeout', 300)), 900)) if action == 'login' else (int(os.getenv('CODEX_EXEC_TIMEOUT', '3600')) if action == 'task' else 120)
    timer = threading.Timer(timeout + 30, proc.kill)
    timer.start()
    try:
        proc.stdin.write(json.dumps({'action': action, **kwargs}) + '\n')
        proc.stdin.close()
        result = None
        safe_error = None
        for line in proc.stdout:
            event = json.loads(line)
            if event.get('event') == 'device' and action == 'login' and on_device:
                on_device(event)
            elif event.get('event') == 'error':
                safe_error = event.get('message')
            elif event.get('event') == 'result':
                result = event['result']
        if proc.wait() or result is None:
            if safe_error in {'Codex 执行超时', 'Codex CLI 不存在', 'Codex Task 非零退出', '执行器忙，请稍后重试'}:
                raise SafeError(safe_error)
            raise SafeError('执行器忙、操作失败或超时')
        return result
    except SafeError:
        raise
    except Exception:
        proc.kill()
        proc.wait()
        raise SafeError('执行器忙、操作失败或超时') from None
    finally:
        timer.cancel()
        proc.stdout.close()


class Admission:
    """Event-loop-only admission; kernel lock additionally covers processes."""
    def __init__(self):
        self.tasks = 0
        self.login = False

    def task_enter(self):
        if self.login:
            raise SafeError('正在换号，暂不能启动 Task')
        self.tasks += 1

    def task_exit(self):
        self.tasks -= 1

    def login_enter(self):
        if self.login or self.tasks:
            raise SafeError('已有 Task 或账号切换正在进行')
        self.login = True

    def login_exit(self):
        self.login = False


admission = Admission()


def status_text(value):
    runner = runner_config().user
    if not value['logged_in']:
        return f'🔐 Codex 登录状态\n状态：未登录\n当前账号：无\n登录方式：未登录\n执行用户：{runner}'
    return ('🔐 Codex 登录状态\n状态：✅ 已登录'
            f"\n当前账号：{value['email']}\n登录方式：{value['method']}\n执行用户：{runner}")


async def login_flow(reply, on_success=None):
    loop = asyncio.get_running_loop()
    def deliver(event):
        future = asyncio.run_coroutine_threadsafe(reply(
            '请在官方页面完成登录；不要向 Bot 发送密码。\n' + event['url'] + '\n验证码：' + event['code']), loop)
        future.result(timeout=20)
    try:
        result = await asyncio.to_thread(request, 'login', on_device=deliver,
                                        timeout=int(os.getenv('CODEX_LOGIN_TIMEOUT', '300')))
        if on_success is not None:
            on_success()
        await reply('换号成功\n' + status_text(result))
    except Exception:
        # Telegram transport errors may retain request bodies. Never let a
        # failed delivery escape into the application's exception logger.
        try:
            await reply('换号未完成；原凭据保留。请稍后检查 /usage。')
        except Exception:
            pass
    finally:
        admission.login_exit()
