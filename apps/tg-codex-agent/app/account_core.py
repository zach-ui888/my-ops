from quota_display import window_minutes, safe_plan
"""Runner-only credential boundary. No raw CLI output leaves this module."""
import base64
import hashlib
from runner_config import runner_config
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime

class SafeError(RuntimeError):
    pass

class Busy(SafeError):
    pass


def identity(path):
    """Unverified JWT metadata is display-only, never authorization."""
    try:
        if path.is_symlink():
            return '未知'
        data = json.loads(path.read_text())
        if data.get('auth_mode') != 'chatgpt':
            return '未知'
        tokens = data.get('tokens', {})
        for name in ('id_token', 'access_token'):
            token = tokens.get(name, '')
            try:
                payload = token.split('.')[1]
                claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
                if not isinstance(claims, dict):
                    continue
            except Exception:
                continue
            profile = claims.get('https://api.openai.com/profile', {})
            candidates = [claims.get('email'), claims.get('https://api.openai.com/profile.email')]
            if isinstance(profile, dict):
                candidates.append(profile.get('email'))
            for email in candidates:
                if isinstance(email, str) and len(email) <= 254 and re.fullmatch(r'[A-Za-z0-9_+.%\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+', email):
                    return email
    except Exception:
        pass
    return '未知'


class Engine:
    def __init__(self, home=None, binary=None, timeout=300):
        self.config = runner_config()
        self.home = Path(home) if home is not None else self.config.codex_home
        self.binary = binary or str(self.config.binary)
        self.timeout = max(30, min(int(timeout), 900))
        self.state = self.home / 'tg-account-manager'

    def env(self, home=None):
        # Do not inherit API keys, alternate auth providers or controller HOME.
        return {'HOME': str(self.config.home), 'CODEX_HOME': str(home or self.home),
                'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}

    def command(self, args):
        return [self.binary, '-c', 'cli_auth_credentials_store="file"', *args]

    @contextlib.contextmanager
    def lock(self, wait=0):
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.state / 'operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + min(max(wait, 0), 5)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Busy('执行器忙，请稍后重试') from None
                    time.sleep(0.05)
            self.recover()
            # Children inherit lock: controller death cannot unlock a live CLI.
            yield fd
        finally:
            os.close(fd)

    def sync(self):
        fd = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def recover(self):
        journal = self.home / '.tg-login-pending'
        backup = self.home / '.tg-auth-backup'
        if journal.exists():
            if journal.read_text() == 'existing':
                if not backup.exists():
                    raise SafeError('凭据恢复需要人工检查')
                restore = self.home / '.tg-auth-restore'
                restore.unlink(missing_ok=True)
                os.link(backup, restore)
                os.replace(restore, self.home / 'auth.json')
            elif journal.read_text() == 'absent':
                (self.home / 'auth.json').unlink(missing_ok=True)
            else:
                raise SafeError('事务状态异常，需要人工检查')
            self.sync()
            journal.unlink()
            self.sync()
        backup.unlink(missing_ok=True)
        for stage in self.state.glob('login-*'):
            if stage.is_dir() and not stage.is_symlink():
                shutil.rmtree(stage)

    def capture(self, args, home=None, fd=None, cwd=None, timeout=30, include_stderr=True):
        proc = None
        try:
            proc = subprocess.Popen(self.command(args), env=self.env(home), cwd=cwd or self.home,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True, pass_fds=(() if fd is None else (fd,)))
            output, error = proc.communicate(timeout=timeout)
            return proc.returncode, output + error if include_stderr else output
        except subprocess.TimeoutExpired:
            raise SafeError('Codex 执行超时') from None
        except FileNotFoundError:
            raise SafeError('Codex CLI 不存在') from None
        except Exception:
            raise SafeError('Codex 操作失败或超时') from None
        finally:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                proc.stdout.close()
                proc.stderr.close()

    def status(self, home=None, fd=None):
        code, output = self.capture(['login', 'status'], home, fd)
        if code == 0 and b'Logged in using ChatGPT' in output:
            return {'logged_in': True, 'email': identity(Path(home or self.home) / 'auth.json'),
                    'method': 'ChatGPT', 'runner': self.config.user}
        if b'Not logged in' in output:
            return {'logged_in': False, 'email': '未知', 'method': '未知', 'runner': self.config.user}
        raise SafeError('无法确认 ChatGPT 登录状态')

    def device(self, stage, fd, emit):
        proc = subprocess.Popen(self.command(['login', '--device-auth']), env=self.env(stage),
                                cwd=stage, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True, pass_fds=(fd,))
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + self.timeout
        buffer = ''
        sent = False
        try:
            while time.monotonic() < deadline:
                for key, _ in sel.select(0.2):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    buffer = (buffer + chunk.decode('utf-8', 'replace'))[-16384:]
                    clean = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', buffer)
                    url = re.search(r'https://auth\.openai\.com/codex/device(?=[\s\x1b]|$)', clean)
                    code = re.search(r'(?<![A-Z0-9])([A-Z0-9]{4}-[A-Z0-9]{5})(?![A-Z0-9])', clean)
                    if url and code and not sent:
                        emit({'event': 'device', 'url': url.group(), 'code': code.group(1)})
                        sent = True
                if proc.poll() is not None and not sel.get_map():
                    if proc.returncode != 0 or not sent:
                        raise SafeError('设备登录失败；原账号保留')
                    return
            raise SafeError('设备登录超时；原账号保留')
        finally:
            # Kill descendants as well, before releasing the inherited lock.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            sel.close()
            proc.stdout.close()

    def commit(self, stage, fd):
        auth = self.home / 'auth.json'
        backup = self.home / '.tg-auth-backup'
        journal = self.home / '.tg-login-pending'
        candidate = stage / 'auth.json'
        if candidate.is_symlink() or not candidate.is_file() or auth.is_symlink():
            raise SafeError('凭据文件不安全')
        os.chmod(candidate, 0o600)
        with candidate.open('rb') as stream:
            os.fsync(stream.fileno())
        if auth.exists():
            os.link(auth, backup)  # No credential serialization or token copies in logs.
        pending = self.home / '.tg-login-journal-new'
        pending.unlink(missing_ok=True)
        with pending.open('x') as stream:
            os.chmod(pending, 0o600)
            stream.write('existing' if auth.exists() else 'absent')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, journal)
        self.sync()
        try:
            os.replace(candidate, auth)
            self.sync()
            result = self.status(fd=fd)
            if not result['logged_in']:
                raise SafeError('登录确认失败')
            # All older quota records are invalid after switching accounts.
            cutoff = self.home / '.tg-quota-cutoff'
            cutoff.write_text(str(time.time()))
            with cutoff.open('rb') as stream:
                os.fsync(stream.fileno())
            journal.unlink()
            self.sync()
            # Commit is durable once the journal disappears; cleanup is retryable.
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
            return result
        except Exception:
            self.recover()
            raise SafeError('登录确认失败；已恢复原账号') from None

    def login(self, fd, emit):
        stage = Path(tempfile.mkdtemp(prefix='login-', dir=self.state))
        try:
            self.device(stage, fd, emit)
            if not self.status(stage, fd)['logged_in']:
                raise SafeError('登录验证失败')
            return self.commit(stage, fd)
        except Exception:
            raise SafeError('换号失败或超时；原账号保留') from None
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def quota(self, fd, refresh=True, max_age=900):
        try:
            result = self.snapshot()
        except SafeError:
            result = None
        if refresh and (result is None or time.time() - result['timestamp'] > max_age):
            code, _ = self.capture(['exec', '--sandbox', 'read-only', '--skip-git-repo-check',
                                    'Reply with exactly: OK'], fd=fd, timeout=60)
            if code:
                raise SafeError('额度刷新失败')
            result = self.snapshot()
        if result is None:
            raise SafeError('没有可用额度快照')
        result['stale'] = time.time() - result['timestamp'] > max_age
        result['generation'] = self.generation()
        result['identity'] = self.status(fd=fd)
        if not result['identity']['logged_in']:
            raise SafeError('runner 未登录，不能确认当前账号额度')
        return result

    def generation(self):
        # No credential bytes, JWT, email or internal account ID are hashed.
        auth = self.home / 'auth.json'
        stat = auth.stat() if auth.exists() else None
        metadata = (str(self.home), (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns) if stat else None)
        cutoff = self.home / '.tg-quota-cutoff'
        return hashlib.sha256(repr((metadata, cutoff.read_text() if cutoff.exists() else '')).encode()).hexdigest()

    def snapshot(self):
        root = self.home / 'sessions'
        if root.is_symlink() or not root.resolve().is_relative_to(self.home.resolve()):
            raise SafeError('拒绝非 runner session 目录')
        files = [p for p in root.rglob('*.jsonl') if not p.is_symlink() and p.resolve().is_relative_to(root.resolve())]
        if not files:
            raise SafeError('没有 runner 额度快照')
        latest = max(files, key=lambda p: p.stat().st_mtime_ns)
        cutoff = self.home / '.tg-quota-cutoff'
        after = float(cutoff.read_text()) if cutoff.exists() else 0
        auth = self.home / 'auth.json'
        after = max(after, auth.stat().st_mtime if auth.exists() else 0)
        result = None
        with latest.open() as stream:
            for line in stream:
                try:
                    obj = json.loads(line)
                    limits = obj['payload']['rate_limits']
                    stamp = datetime.fromisoformat(obj['timestamp'].replace('Z', '+00:00')).timestamp()
                    if stamp < after or stamp > time.time() + 60:
                        continue
                    safe = {}
                    for name in ('primary', 'secondary'):
                        value = limits.get(name)
                        if value is None:
                            safe[name] = None
                            continue
                        used = float(value['used_percent'])
                        reset = float(value['resets_at'])
                        if not math.isfinite(used) or not 0 <= used <= 100 or not math.isfinite(reset) or not 0 <= reset <= 253402214400:
                            raise ValueError()
                        safe[name] = {'used_percent': used, 'resets_at': reset}
                        minutes = window_minutes(value)
                        if minutes is not None:
                            safe[name]['window_minutes'] = minutes
                    plan = limits.get('plan_type')
                    safe['plan_type'] = safe_plan(plan)
                    result = {'rate_limits': safe, 'timestamp': stamp, 'session': latest.name}
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
        if result is None:
            raise SafeError('最新 runner session 无有效额度')
        return result
