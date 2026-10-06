"""Final B crash/barrier/pressure evidence, synthetic and project-local only.

Forked workers are bounded and reaped even when an assertion fails. No socket
failure is skipped. Hooks pause real code; they do not replace commit or locks.
"""
from contextlib import ExitStack
import fcntl
import multiprocessing
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_step22a_commit as commit
from tg_testcase.authorization import AuthorizationGuard
from tg_testcase.protected_acquisition import _CommitBoundary
from tg_testcase.receiver_service import ReceiverService
from tg_testcase.store import Store
from tg_testcase.unix_transport import Endpoint, PeerPolicy, kernel_peer, configure_socket

CTX = multiprocessing.get_context('fork')
TABLES = ('source_packages', 'source_package_artifacts',
          'batch_input_manifests', 'acquisition_commit_outbox')


class AcceptanceFixture(unittest.TestCase):
    explicit_capability = commit.CommitTests.explicit_capability
    setUp = commit.CommitTests.setUp
    def tearDown(self):
        self.doCleanups()
        commit.CommitTests.tearDown(self)
    send = commit.CommitTests.send
    wire = commit.CommitTests.wire
    sql = commit.CommitTests.sql
    seal = commit.CommitTests.seal
    revoke = commit.CommitTests.revoke

    def child(self, target):
        p = CTX.Process(target=target)
        p.start()
        def cleanup():
            if p.is_alive():
                p.kill()
            p.join(3)
            p.close()
        self.addCleanup(cleanup)
        return p

    def message(self, pipe):
        self.assertTrue(pipe.poll(5), 'child did not reach barrier')
        return pipe.recv()

    def facts(self, committed):
        reopened = Store(self.store.root, capability=self.store_cap)
        with reopened._locked() as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            for table in TABLES:
                self.assertEqual(conn.execute('SELECT count(*) FROM ' + table).fetchone(), (int(committed),))
            self.assertEqual(conn.execute('SELECT status FROM source_fetch_runs').fetchone(),
                             ('sealed' if committed else 'fetching',))
            if committed:
                self.assertEqual(conn.execute('SELECT body FROM source_package_artifacts').fetchone(), (b'abc',))
        # A new process must acquire a fresh guard after the crashed writer.
        with AuthorizationGuard(self.lock).hold():
            pass


class CrashTests(AcceptanceFixture):
    def crash(self, phase):
        def run():
            with ExitStack() as stack:
                if phase == 'before_transaction':
                    stack.enter_context(patch.object(self.importer, '_commit_staged', side_effect=lambda *a: os._exit(71)))
                elif phase == 'blob':
                    original = commit.write_blob
                    def blob(*a):
                        original(*a)
                        os._exit(71)
                    stack.enter_context(patch('tg_testcase.streaming.write_blob', blob))
                elif phase == 'before_commit':
                    original = _CommitBoundary.finish
                    def finish(*a):
                        original(*a)
                        os._exit(71)
                    stack.enter_context(patch.object(_CommitBoundary, 'finish', finish))
                self.seal()
                os._exit(71)
        p = self.child(run)
        p.join(5)
        self.assertEqual(p.exitcode, 71)
        self.facts(phase == 'after_commit')

    def test_before_transaction(self):
        self.crash('before_transaction')

    def test_during_blob(self):
        self.crash('blob')

    def test_after_outbox_before_commit(self):
        self.crash('before_commit')

    def test_after_commit(self):
        self.crash('after_commit')
        before = [self.sql('SELECT * FROM ' + t) for t in TABLES]
        self.seal()
        self.assertEqual([self.sql('SELECT * FROM ' + t) for t in TABLES], before)


MUTATIONS = {
    'revoke': lambda s: s.revoke(),
    'rotate': lambda s: s.registry.mutate_credential('notion-read-a', 'rotate', 'rotate', 1000),
    'cancel': lambda s: s.registry.stop(s.job['job_id'], 'JOB_CANCELLED', 1000),
    'fence': lambda s: s.sql('UPDATE source_fetch_runs SET fencing_token=2'),
}


class BarrierTests(AcceptanceFixture):
    def barrier(self, mutation, commit_first):
        parent, child = CTX.Pipe()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        if not commit_first:
            def change():
                with self.guard.hold():
                    mutation(self)
                child.send('changed')
            p = self.child(change)
            self.assertEqual(self.message(parent), 'changed')
            p.join(5)
            self.assertEqual(p.exitcode, 0)
            with self.assertRaises(ValueError):
                self.seal()
            self.facts(False)
            return

        def change():
            child.recv()
            # Prove contention with an independent open file description, not
            # a sleep or an inherited thread-local guard token.
            with self.lock.open('rb') as fd:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    child.send('blocked')
                else:
                    child.send('incorrectly_unlocked')
            with self.guard.hold():
                mutation(self)
            child.send('changed')
        p = self.child(change)  # fork BEFORE the parent holds any lock
        original = _CommitBoundary.finish
        def finish(boundary, conn):
            original(boundary, conn)
            self.assertTrue(conn.in_transaction)
            parent.send('attempt')
            self.assertEqual(self.message(parent), 'blocked')
            self.assertFalse(parent.poll(.05), 'mutation completed before commit')
        with patch.object(_CommitBoundary, 'finish', finish):
            self.seal()
        self.assertEqual(self.message(parent), 'changed')
        p.join(5)
        self.assertEqual(p.exitcode, 0)
        self.facts(True)


for name, mutation in MUTATIONS.items():
    for first in (False, True):
        def test(self, mutation=mutation, first=first):
            self.barrier(mutation, first)
        setattr(BarrierTests, 'test_' + name + ('_after_commit' if first else '_before_commit'), test)


class PressureTests(unittest.TestCase):
    def setUp(self):
        # SO_PEERCRED reports effective credentials at connect/socketpair time.
        # A root test process cannot be an authorized producer: fail explicitly,
        # never relax root rejection or manufacture a different kernel identity.
        self.assertNotEqual(os.geteuid(), 0,
                            'pressure fixture requires a non-root test process; '
                            'PeerPolicy rejects UID 0 before handler invocation')
        self.policy = PeerPolicy(os.geteuid(), os.getegid())
        self.preflight()

    def preflight(self):
        # Exercise the same real credential/auth/buffer path as admission, so a
        # rejection is reported here rather than as an opaque handler timeout.
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with server, client:
            peer = kernel_peer(server)
            self.assertEqual((peer.pid, peer.uid, peer.gid),
                             (os.getpid(), os.geteuid(), os.getegid()))
            self.policy.authorize(peer)
            configure_socket(server)

    def start_endpoint(self, listener, handler):
        admission = []
        class ObservedPolicy(PeerPolicy):
            def authorize(policy, peer):
                try:
                    super().authorize(peer)
                except Exception as exc:
                    admission.append(('rejected', peer, type(exc).__name__))
                    raise
                admission.append(('authorized', peer))
        # Observe the actual kernel peer and delegate to the unchanged policy.
        endpoint = Endpoint(listener, ObservedPolicy(self.policy.uid, self.policy.gid), handler)
        endpoint.pressure_admission = admission
        errors = []
        def serve():
            try:
                endpoint.serve()
            except BaseException as exc:
                errors.append(type(exc).__name__)
        thread = threading.Thread(target=serve)
        thread.start()
        return endpoint, thread, errors

    def wait_for(self, predicate, endpoint, thread, errors):
        end = time.monotonic() + 3
        while not predicate() and not errors and time.monotonic() < end:
            time.sleep(.005)
        self.assertTrue(predicate(),
                        f'endpoint progress missing: thread_alive={thread.is_alive()}, '
                        f'errors={errors}, waiting={endpoint.waiting.qsize()}, '
                        f'unfinished={endpoint.waiting.unfinished_tasks}, '
                        f'euid={os.geteuid()}, egid={os.getegid()}, '
                        f'admission={endpoint.pressure_admission}')

    def test_active_four_waiting_overflow_and_recovery(self):
        self.pressure(False)

    def test_waiting_expiry_and_recovery(self):
        self.pressure(True)

    def test_slow_receiver_does_not_block_gateway(self):
        project = Path(__file__).resolve().parents[1]
        release, entered = threading.Event(), threading.Event()
        endpoints, threads, clients, server_errors = [], [], [], []
        completed = threading.Event()
        with tempfile.TemporaryDirectory(prefix='parallel-', dir=project) as directory, ExitStack() as stack:
            def slow(conn, peer, accepted):
                entered.set()
                try:
                    if not release.wait(5):
                        raise AssertionError("receiver release timeout")
                finally:
                    completed.set()
            def fast(conn, peer, accepted):
                conn.sendall(b'OK')
            try:
                for name, handler in (('receiver', slow), ('gateway', fast)):
                    listener = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                    path = str(Path(directory) / (name + '.sock'))
                    listener.bind(path)
                    listener.listen(4)
                    endpoint, thread, errors = self.start_endpoint(listener, handler)
                    endpoints.append(endpoint)
                    threads.append(thread)
                    server_errors.append(errors)
                    client = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
                    clients.append(client)
                    client.settimeout(3)
                    client.connect(path)
                    if name == 'receiver':
                        self.wait_for(entered.is_set, endpoint, thread, errors)
                self.assertEqual(clients[1].recv(2), b'OK')
                self.assertFalse(release.is_set())
                self.assertFalse(completed.is_set(), "Receiver must still be active")
                self.assertIsNot(endpoints[0].waiting, endpoints[1].waiting)
                self.assertIsNot(endpoints[0].stop, endpoints[1].stop)
                release.set()
                self.assertEqual(clients[0].recv(1), b'')
                for endpoint, thread, errors in zip(endpoints, threads, server_errors):
                    self.wait_for(lambda: endpoint.waiting.unfinished_tasks == 0,
                                  endpoint, thread, errors)
            finally:
                release.set()
                for endpoint in endpoints:
                    endpoint.stop.set()
                for thread in threads:
                    thread.join(6)
                    self.assertFalse(thread.is_alive())
                self.assertTrue(all(not errors for errors in server_errors), server_errors)

    def pressure(self, expire):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='pressure-', dir=project) as directory:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                path = str(Path(directory) / 'receiver.sock')
                listener.bind(path)
                listener.listen(4)
                active, release = threading.Event(), threading.Event()
                handled, accepted_sockets = [], []
                def handler(conn, peer, accepted):
                    accepted_sockets.append(conn)
                    handled.append((threading.get_ident(), accepted))
                    active.set()
                    if len(handled) == 1:
                        if not release.wait(15):
                            raise AssertionError('release timeout')
                        raise ValueError('synthetic handler failure')
                    conn.sendall(b'OK')
                endpoint, server, errors = self.start_endpoint(listener, handler)
                clients = []
                def connect():
                    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    clients.append(c)
                    c.settimeout(3)
                    c.connect(path)
                    return c
                def wait_for(predicate):
                    self.wait_for(predicate, endpoint, server, errors)
                try:
                    connect()
                    wait_for(active.is_set)
                    for n in range(4):
                        connect()
                        wait_for(lambda: endpoint.waiting.qsize() == n + 1)
                    overflow = connect()
                    self.assertEqual(overflow.recv(1), b'')
                    self.assertEqual(len(handled), 1)
                    with endpoint.waiting.mutex:
                        queued = [item[0] for item in endpoint.waiting.queue]
                    for c in queued:
                        for option in (socket.SO_SNDBUF, socket.SO_RCVBUF):
                            self.assertLessEqual(c.getsockopt(socket.SOL_SOCKET, option), 131072)
                    if expire:
                        for c in clients[1:5]:
                            c.settimeout(12)
                            self.assertEqual(c.recv(1), b'')
                        self.assertEqual(len(handled), 1)
                        self.assertTrue(endpoint.waiting.empty())
                        self.assertEqual(endpoint.waiting.unfinished_tasks, 1)
                        # Refill BEFORE releasing active: expiry itself must
                        # restore all four waiting slots, not worker completion.
                        replacement = []
                        for n in range(4):
                            replacement.append(connect())
                            wait_for(lambda: endpoint.waiting.qsize() == n + 1)
                        self.assertEqual(connect().recv(1), b'')
                        self.assertEqual(len(handled), 1)
                    release.set()
                    self.assertEqual(clients[0].recv(1), b'')
                    for c in (replacement if expire else clients[1:5]):
                        self.assertEqual(c.recv(2), b'OK')
                    self.assertEqual(connect().recv(2), b'OK')
                    wait_for(lambda: endpoint.waiting.unfinished_tasks == 0)
                    self.assertEqual(len(handled), 6)
                    self.assertEqual(len({item[0] for item in handled}), 1)
                    wait_for(lambda: all(c.fileno() == -1 for c in accepted_sockets))
                finally:
                    release.set()
                    endpoint.stop.set()
                    server.join(6)
                    for c in clients:
                        c.close()
                self.assertFalse(server.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(endpoint.waiting.empty())


class SocketBoundaryTests(AcceptanceFixture):
    def exercise(self, phase, crash=False):
        from tg_testcase.receiver_service import ReceiverReader
        from tg_testcase.unix_transport import write_response
        parent, child = CTX.Pipe()
        self.addCleanup(parent.close)
        self.addCleanup(child.close)
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        kernel_peer(server)  # Fail visibly before forking if sandbox denies SO_PEERCRED.
        def run():
            client.close()
            def pause():
                child.send('paused')
                child.recv()
            with ExitStack() as stack:
                if phase == 'read':
                    original = ReceiverReader.package_budget
                    def budget(*a):
                        original(*a)
                        pause()
                    stack.enter_context(patch.object(ReceiverReader, 'package_budget', budget))
                elif phase == 'eof':
                    original = ReceiverReader.eof
                    def eof(reader):
                        pause()
                        original(reader)
                    stack.enter_context(patch.object(ReceiverReader, 'eof', eof))
                elif phase == 'after_eof':
                    original = ReceiverReader.eof
                    def eof(reader):
                        original(reader)
                        pause()
                    stack.enter_context(patch.object(ReceiverReader, 'eof', eof))
                else:
                    def response(conn, data):
                        if phase == 'partial_ack':
                            conn.sendall(data[:3])
                        pause()
                        write_response(conn, data[3:] if phase == 'partial_ack' else data)
                    stack.enter_context(patch('tg_testcase.receiver_service.write_response', response))
                ReceiverService(self.store, self.registry.db_path, self.guard,
                                self.staging_cap, clock=lambda: self.now)(
                                    server, kernel_peer(server), time.monotonic())
            server.close()
        p = self.child(run)
        server.close()
        client.settimeout(5)
        client.sendall(self.wire())
        if phase != 'eof':
            client.shutdown(socket.SHUT_WR)
        self.assertEqual(self.message(parent), 'paused')
        # Independent process (parent) gets guard, registry write, Store write
        # while receiver is held at the actual network path boundary.
        with AuthorizationGuard(self.lock).hold():
            db = sqlite3.connect(self.registry.db_path, timeout=.1)
            try:
                db.execute('BEGIN IMMEDIATE')
                db.rollback()
            finally:
                db.close()
            with self.store._locked() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.rollback()
        if crash:
            p.kill()
            p.join(5)
            self.assertEqual(p.exitcode, -9)
            self.facts(phase in ('ack', 'partial_ack'))
            if phase in ('ack', 'partial_ack'):
                before = [self.sql('SELECT * FROM ' + t) for t in TABLES]
                from test_step22b_transport import ReceiverTests
                self.assertEqual(ReceiverTests.exchange(self, self.wire())['type'], 'ACK')
                self.assertEqual([self.sql('SELECT * FROM ' + t) for t in TABLES], before)
        else:
            if phase == 'eof':
                client.shutdown(socket.SHUT_WR)
            parent.send('resume')
            from tg_testcase.receiver import read_frame, ACK
            with client.makefile('rb') as stream:
                self.assertEqual(read_frame(stream, {ACK})[0], ACK)
            p.join(5)
            self.assertEqual(p.exitcode, 0)
            self.facts(True)


for phase in ('read', 'eof', 'after_eof', 'ack', 'partial_ack'):
    for crash in (False, True):
        def test(self, phase=phase, crash=crash):
            self.exercise(phase, crash)
        setattr(SocketBoundaryTests, 'test_' + phase + ('_kill' if crash else '_locks_free'), test)
