import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from tg_testcase.unix_transport import LocalSocketCapability, TransportRejected

PROJECT = Path(__file__).resolve().parents[1]


class PathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='uds-', dir=PROJECT)
        self.root = Path(self.temp.name)
        self.root.chmod(0o750)
        self.fd = os.open(self.root, os.O_DIRECTORY | os.O_NOFOLLOW)
        self.cap = LocalSocketCapability(self.root, self.fd, os.getuid(), os.getgid(), threading.Lock())

    def tearDown(self):
        self.cap.close()
        os.close(self.fd)
        self.temp.cleanup()

    def test_lifecycle(self):
        with self.cap.listen('receiver') as listener:
            self.assertEqual((self.root / 'receiver.sock').stat().st_mode & 0o777, 0o660)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.connect(str(self.root / 'receiver.sock'))
                accepted, _ = listener.accept()
                accepted.close()
        self.assertFalse((self.root / 'receiver.sock').exists())

    def test_unknown_nodes_are_never_repaired(self):
        path = self.root / 'receiver.sock'
        makers = [lambda: path.write_bytes(b'x'), lambda: path.mkdir(),
                  lambda: path.symlink_to('missing'), lambda: os.mkfifo(path)]
        for make in makers:
            make()
            before = path.lstat()
            with self.assertRaises(TransportRejected):
                with self.cap.listen('receiver'):
                    self.fail('admitted')
            self.assertEqual(path.lstat(), before)
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()

    def test_unknown_stale_socket_is_retained(self):
        path = self.root / 'receiver.sock'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.bind(str(path))
        with self.assertRaises(TransportRejected):
            with self.cap.listen('receiver'):
                self.fail('admitted')
        self.assertTrue(path.exists())

    def test_wrong_directory_mode(self):
        self.root.chmod(0o770)
        with self.assertRaises(TransportRejected):
            with self.cap.listen('receiver'):
                self.fail('admitted')
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o770)

    def test_replaced_node_is_not_deleted(self):
        path = self.root / 'receiver.sock'
        with self.cap.listen('receiver'):
            path.unlink()
            path.write_bytes(b'replacement')
        self.assertEqual(path.read_bytes(), b'replacement')

    def test_symlink_parent_rejected(self):
        from tg_testcase.runtime_paths import verify_local_directory
        alias = self.root / 'alias'
        target = self.root / 'target'
        target.mkdir()
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            verify_local_directory(alias)

    def test_wrong_gid_rejected(self):
        with self.assertRaises(TransportRejected):
            LocalSocketCapability(self.root, self.fd, os.getuid(), os.getgid()+1, threading.Lock())

    def test_socket_path_length_checked_before_bind(self):
        long_root = self.root / ('a'*80)
        long_root.mkdir(mode=0o750)
        fd = os.open(long_root, os.O_DIRECTORY | os.O_NOFOLLOW)
        cap = LocalSocketCapability(long_root, fd, os.getuid(), os.getgid(), threading.Lock())
        try:
            with self.assertRaises(TransportRejected):
                with cap.listen('receiver'):
                    self.fail('admitted')
        finally:
            cap.close(); os.close(fd)

    def test_lock_wait_budget_and_replaced_guard_inode(self):
        import fcntl
        import time
        from tg_testcase.authorization import AuthorizationGuard
        from tg_testcase.unix_transport import bounded_flock, local_budget
        lock = self.root / 'authorization.lock'
        lock.touch(mode=0o660)
        guard = AuthorizationGuard(lock)
        with lock.open('rb') as held, lock.open('rb') as waiting:
            fcntl.flock(held, fcntl.LOCK_EX)
            end = time.monotonic() + .03
            with local_budget(lambda: end), self.assertRaises(TransportRejected):
                bounded_flock(waiting)
            self.assertLess(time.monotonic() - end, .1)
        lock.rename(self.root / 'old-lock')
        lock.touch(mode=0o660)
        with self.assertRaises(ValueError):
            with guard.hold():
                self.fail('replaced guard acquired')
