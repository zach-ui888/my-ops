"""Linux transport primitives. No deployment discovery or identity fallback."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import fcntl
import os
import queue
import socket
import stat
import struct
import threading
import time


class TransportRejected(ValueError):
    def __init__(self):
        super().__init__('transport_rejected')


def check(condition):
    if not condition:
        raise TransportRejected()


_deadline = ContextVar('transport_deadline', default=None)


@contextmanager
def local_budget(deadline):
    token = _deadline.set(deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining(cap=5):
    deadline = _deadline.get()
    value = min(cap, deadline() - time.monotonic()) if deadline else cap
    check(value > 0)
    return value


def bounded_flock(fd):
    end = time.monotonic() + remaining()
    while True:
        check(time.monotonic() < end)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(min(.01, max(0, end - time.monotonic())))


@dataclass(frozen=True)
class KernelPeer:
    pid: int
    uid: int
    gid: int


def kernel_peer(conn):
    check(conn.family == socket.AF_UNIX and conn.type == socket.SOCK_STREAM)
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('@iII'))
        check(len(raw) == struct.calcsize('@iII'))
        pid, uid, gid = struct.unpack('@iII', raw)
        check(pid > 0 and uid < 4294967295 and gid < 4294967295)
        return KernelPeer(pid, uid, gid)
    except (AttributeError, OSError, struct.error):
        raise TransportRejected() from None


@dataclass(frozen=True)
class PeerPolicy:
    uid: int = None
    gid: int = None
    denied_uids: tuple = ()

    def authorize(self, peer):
        check(type(self.uid) is int and type(self.gid) is int)
        check(peer.uid != 0 and peer.uid not in self.denied_uids)
        check((peer.uid, peer.gid) == (self.uid, self.gid))

    @classmethod
    def endpoint(cls, owners, endpoint, runner_uid):
        check(endpoint in ('receiver', 'gateway'))
        uid = getattr(owners, 'producer_uid' if endpoint == 'receiver' else 'gateway_uid')
        check(uid != runner_uid)
        return cls(uid, getattr(owners, endpoint + '_gid'), (0, runner_uid))


class SocketReader:
    """One unbuffered reader, fixed phase deadlines and cumulative byte cap."""
    def __init__(self, conn, accepted_at, cap):
        self.conn = conn
        self.deadline = accepted_at + 180
        self.phase = self.deadline
        self.cap, self.count = cap, 0

    def header(self):
        self.phase = min(self.deadline, time.monotonic() + 10)

    def payload(self):
        self.phase = self.deadline

    def eof(self):
        self.phase = min(self.deadline, time.monotonic() + 5)
        check(self.read(1) == b'')

    def read(self, size):
        check(type(size) is int and 0 <= size <= 65536)
        check(self.count + size <= self.cap)
        budget = min(10, self.deadline - time.monotonic(), self.phase - time.monotonic())
        check(budget > 0)
        self.conn.settimeout(budget)
        chunk = self.conn.recv(size)
        self.count += len(chunk)
        return chunk


def write_response(conn, data):
    end = time.monotonic() + 5
    view = memoryview(data)
    while view:
        left = end - time.monotonic()
        check(left > 0)
        conn.settimeout(left)
        written = conn.send(view[:65536])
        check(written > 0)
        view = view[written:]


def configure_socket(conn):
    for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        conn.setsockopt(socket.SOL_SOCKET, option, 65536)
        check(0 < conn.getsockopt(socket.SOL_SOCKET, option) <= 131072)


class Endpoint:
    """One worker and four authenticated waiters; no per-client threads."""
    def __init__(self, listener, policy, handler):
        self.listener, self.policy, self.handler = listener, policy, handler
        self.waiting = queue.Queue(4)
        self.stop = threading.Event()

    def _worker(self):
        while not self.stop.is_set():
            try:
                conn, peer, accepted = self.waiting.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if time.monotonic() - accepted < 10:
                    self.handler(conn, peer, accepted)
            except Exception:
                pass  # Fixed close; never log attacker input or exception text.
            finally:
                conn.close()
                self.waiting.task_done()

    def serve(self):
        worker = threading.Thread(target=self._worker, daemon=False)
        worker.start()
        self.listener.settimeout(.1)
        try:
            while not self.stop.is_set():
                with self.waiting.mutex:
                    while self.waiting.queue and time.monotonic() - self.waiting.queue[0][2] >= 10:
                        self.waiting.queue.popleft()[0].close()
                        self.waiting.unfinished_tasks -= 1
                        self.waiting.not_full.notify()
                try:
                    conn, _ = self.listener.accept()
                except socket.timeout:
                    continue
                accepted = time.monotonic()
                try:
                    peer = kernel_peer(conn)  # Before queueing or application read.
                    self.policy.authorize(peer)
                    configure_socket(conn)
                    self.waiting.put_nowait((conn, peer, accepted))
                except Exception:
                    conn.close()
        finally:
            self.stop.set()
            worker.join()
            while not self.waiting.empty():
                self.waiting.get_nowait()[0].close()


class LocalSocketCapability:
    """Explicit offline directory-management capability, never production proof.

    The composition layer supplies an already-open stable directory and lifecycle
    mutex. Unknown old sockets are retained, even if connect would be refused.
    Ancestor/ACL attestation must be supplied by the protected provisioner for a
    production capability; this local class deliberately cannot grant that role.
    """
    def __init__(self, root, directory_fd, uid, gid, lifecycle):
        from .runtime_paths import _path
        self.root = str(_path(str(root)))
        self.fd = os.dup(directory_fd)
        self.uid, self.gid, self.lifecycle = uid, gid, lifecycle
        self.identity = os.fstat(self.fd)
        try:
            self._verify()
        except BaseException:
            os.close(self.fd)
            raise

    def _verify(self):
        from .storage import inside_project
        from .runtime_paths import verify_local_directory
        inside_project(self.root)
        verify_local_directory(self.root)
        s = os.stat(self.root, follow_symlinks=False)
        check(stat.S_ISDIR(s.st_mode) and stat.S_IMODE(s.st_mode) == 0o750)
        check((s.st_uid, s.st_gid) == (self.uid, self.gid))
        check((s.st_dev, s.st_ino) == (self.identity.st_dev, self.identity.st_ino))

    @contextmanager
    def listen(self, endpoint):
        check(endpoint in ('receiver', 'gateway'))
        name = endpoint + '.sock'
        path = self.root + '/' + name
        check(len(path.encode('ascii')) <= 107)
        with self.lifecycle:
            self._verify()
            try:
                os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise TransportRejected()
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            identity = None
            try:
                old = os.umask(0o077)
                try:
                    conn.bind(path)
                finally:
                    os.umask(old)
                s = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                check(stat.S_ISSOCK(s.st_mode) and s.st_uid == self.uid and s.st_gid == self.gid)
                identity = (s.st_dev, s.st_ino)
                os.chmod(name, 0o660, dir_fd=self.fd, follow_symlinks=False)
                self._verify()
                s = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                check((s.st_dev, s.st_ino) == identity and stat.S_IMODE(s.st_mode) == 0o660)
                configure_socket(conn)
                conn.listen(4)
            except BaseException:
                conn.close()
                self._remove(name, identity)
                raise
        try:
            yield conn
        finally:
            conn.close()
            with self.lifecycle:
                self._remove(name, identity)

    def _remove(self, name, identity):
        self._verify()
        try:
            s = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if identity == (s.st_dev, s.st_ino) and stat.S_ISSOCK(s.st_mode):
            os.unlink(name, dir_fd=self.fd)

    def close(self):
        os.close(self.fd)
