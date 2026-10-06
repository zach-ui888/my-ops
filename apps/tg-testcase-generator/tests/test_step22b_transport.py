"""Project-local Linux sockets and real protected SQLite; no external service."""
import json
import os
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

import test_step22a_commit as commit
from tg_testcase.receiver import read_frame, ACK, ERROR
from tg_testcase.receiver_service import ReceiverService
from tg_testcase.unix_transport import (kernel_peer, KernelPeer, PeerPolicy, SocketReader,
                                       TransportRejected, local_budget, bounded_flock)


class PeerTests(unittest.TestCase):
    def test_real_kernel_peer(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.assertEqual(kernel_peer(a), KernelPeer(os.getpid(), os.getuid(), os.getgid()))
        finally:
            a.close(); b.close()

    def test_default_deny_and_exact_primary_pair(self):
        for policy, peer in [(PeerPolicy(), KernelPeer(1, 12, 13)),
                             (PeerPolicy(12, 13), KernelPeer(1, 0, 13)),
                             (PeerPolicy(12, 13), KernelPeer(1, 11, 13)),
                             (PeerPolicy(12, 13), KernelPeer(1, 12, 14)),
                             (PeerPolicy(12, 13, (12,)), KernelPeer(1, 12, 13))]:
            with self.assertRaises(TransportRejected):
                policy.authorize(peer)
        PeerPolicy(12, 13).authorize(KernelPeer(1, 12, 13))

    def test_missing_short_or_failed_kernel_credentials(self):
        from unittest.mock import Mock
        for result in (b'', b'123', OSError()):
            s = Mock(family=socket.AF_UNIX, type=socket.SOCK_STREAM)
            if isinstance(result, Exception):
                s.getsockopt.side_effect = result
            else:
                s.getsockopt.return_value = result
            with self.assertRaises(TransportRejected):
                kernel_peer(s)

    def test_fixed_header_and_total_deadlines(self):
        a, b = socket.socketpair()
        try:
            reader = SocketReader(a, time.monotonic(), 100)
            reader.header()
            reader.phase = time.monotonic() - .001
            with self.assertRaises(TransportRejected):
                reader.read(1)
            reader.payload()
            reader.deadline = time.monotonic() - .001
            with self.assertRaises(TransportRejected):
                reader.read(1)
        finally:
            a.close(); b.close()


class ReceiverTests(unittest.TestCase):
    explicit_capability = commit.CommitTests.explicit_capability
    setUp = commit.CommitTests.setUp
    tearDown = commit.CommitTests.tearDown
    send = commit.CommitTests.send
    wire = commit.CommitTests.wire
    sql = commit.CommitTests.sql
    empty = commit.CommitTests.empty

    def exchange(self, wire, *, shutdown=True, lose_ack=False):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        service = ReceiverService(self.store, self.registry.db_path, self.guard, self.staging_cap,
                                  clock=lambda: self.now)
        errors = []
        def run():
            try:
                service(server, kernel_peer(server), time.monotonic())
            except OSError:
                pass
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                server.close()
        thread = threading.Thread(target=run)
        thread.start()
        try:
            client.sendall(wire)
            if shutdown:
                client.shutdown(socket.SHUT_WR)
            if lose_ack:
                client.close()
                result = None
            else:
                client.settimeout(8)
                with client.makefile('rb') as reader:
                    kind, data = read_frame(reader, {ACK, ERROR})
                result = json.loads(data)
        finally:
            client.close()
            thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        return result

    def test_half_close_commit_and_full_replay(self):
        first = self.exchange(self.wire())
        self.assertEqual(first['type'], 'ACK', first)
        self.assertEqual(self.exchange(self.wire()), first)
        for table in ('source_packages', 'acquisition_commit_outbox'):
            self.assertEqual(self.sql('SELECT count(*) FROM ' + table), [(1,)])

    def test_trailing_byte_never_commits(self):
        self.assertEqual(self.exchange(self.wire(tail=b'x'))['type'], 'ERROR')
        self.empty()

    def test_commit_without_half_close_times_out(self):
        self.assertEqual(self.exchange(self.wire(), shutdown=False)['type'], 'ERROR')
        self.empty()

    def test_truncated_input_never_commits(self):
        self.assertEqual(self.exchange(self.wire()[:-6])['type'], 'ERROR')
        self.empty()

    def test_ack_loss_preserves_outbox(self):
        self.exchange(self.wire(), lose_ack=True)
        self.assertEqual(self.sql('SELECT count(*) FROM source_packages'), [(1,)])
        self.assertEqual(self.sql('SELECT count(*) FROM acquisition_commit_outbox'), [(1,)])
        self.assertEqual(self.exchange(self.wire())['type'], 'ACK')

    def test_unknown_residue_blocks_and_is_retained(self):
        residue = self.staging / 'unknown'
        residue.write_bytes(b'synthetic')
        self.assertEqual(self.exchange(self.wire())['type'], 'ERROR')
        self.assertEqual(residue.read_bytes(), b'synthetic')
        self.assertEqual(self.sql('SELECT count(*) FROM source_packages'), [(0,)])

    def test_no_db_locks_during_eof_or_ack(self):
        from tg_testcase.unix_transport import SocketReader, write_response
        original = SocketReader.eof
        def check_free():
            self.assertFalse(self.guard.held)
            with self.store._locked() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.rollback()
        def eof(reader):
            check_free()
            return original(reader)
        def write(conn, data):
            check_free()
            return write_response(conn, data)
        with patch.object(SocketReader, 'eof', eof), patch('tg_testcase.receiver_service.write_response', write):
            self.assertEqual(self.exchange(self.wire())['type'], 'ACK')


class MemorySocket:
    """Injected I/O only; explicitly not evidence of Linux socket permissions."""
    def __init__(self, data):
        from io import BytesIO
        self.input = BytesIO(data)
        self.output = bytearray()
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, size):
        return self.input.read(min(1, size))

    def send(self, data):
        self.output.extend(data)
        return len(data)


class InjectedReceiverTests(unittest.TestCase):
    explicit_capability = commit.CommitTests.explicit_capability
    setUp = commit.CommitTests.setUp
    tearDown = commit.CommitTests.tearDown
    send = commit.CommitTests.send
    wire = commit.CommitTests.wire
    sql = commit.CommitTests.sql
    empty = commit.CommitTests.empty

    def exchange(self, data):
        from io import BytesIO
        conn = MemorySocket(data)
        service = ReceiverService(self.store, self.registry.db_path, self.guard, self.staging_cap,
                                  clock=lambda: self.now)
        service(conn, KernelPeer(1, 101, 102), time.monotonic())
        _, body = read_frame(BytesIO(conn.output), {ACK, ERROR})
        return json.loads(body)

    def test_single_byte_fragmented_submission_and_replay(self):
        result = self.exchange(self.wire())
        self.assertEqual(result['type'], 'ACK', result)
        self.assertEqual(result, self.exchange(self.wire()))
        self.assertEqual(self.sql('SELECT count(*) FROM acquisition_commit_outbox'), [(1,)])

    def test_tail_duplicate_commit_and_truncation(self):
        for raw in (self.wire(tail=b'x'), self.wire(tail=b'\x06\0\0\0\0'), self.wire()[:-1]):
            self.assertEqual(self.exchange(raw)['type'], 'ERROR')
            self.empty()

    def test_oversized_header_and_spoofed_identity(self):
        from tg_testcase.receiver import frame, HELLO
        for data in (b'\x01\xff\xff\xff\xff', frame(HELLO, b'{"uid":101}')):
            self.assertEqual(self.exchange(data)['type'], 'ERROR')
            self.empty()

    def test_final_authorization_still_rejects_revoke(self):
        import test_step21_contract as contracts
        original = SocketReader.eof
        def revoke(reader):
            original(reader)
            self.registry.publish(contracts.canonical(contracts.policy(epoch=2)), 1, 'revoke', 1001)
        with patch.object(SocketReader, 'eof', revoke):
            self.assertEqual(self.exchange(self.wire())['type'], 'ERROR')
        self.empty()

    def test_ack_write_failure_keeps_commit(self):
        with patch('tg_testcase.receiver_service.write_response', side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):
                self.exchange(self.wire())
        self.assertEqual(self.sql('SELECT count(*) FROM source_packages'), [(1,)])
        self.assertEqual(self.sql('SELECT count(*) FROM acquisition_commit_outbox'), [(1,)])
        self.assertEqual(self.exchange(self.wire())['type'], 'ACK')


class GatewayInjectedTests(unittest.TestCase):
    def test_eof_precedes_dispatch_and_bounds(self):
        from tg_testcase.gateway_transport import GatewayTransport
        from tg_testcase.controller_bridge import ControllerBridge
        from tg_testcase.gateway_transport import BoundedGatewayRegistry
        from unittest.mock import Mock
        # Construct only the wire harness; complete real ledger tested separately.
        transport = object.__new__(GatewayTransport)
        transport.bridge = Mock()
        transport.bridge.handle.return_value = {'ok': True}
        transport.clock = lambda: 1
        for data in (struct.pack('!I', 2)+b'{}x', b'\xff'*4, struct.pack('!I', 2)+b'\xff\xff'):
            conn = MemorySocket(data)
            transport(conn, KernelPeer(1, 101, 102), time.monotonic())
            transport.bridge.handle.assert_not_called()
        conn = MemorySocket(struct.pack('!I', 2)+b'{}')
        transport(conn, KernelPeer(1, 101, 102), time.monotonic())
        transport.bridge.handle.assert_called_once()
        self.assertEqual(transport.bridge.handle.call_args.args[1].uid, 101)

    def test_real_identity_bridge_and_durable_replay(self):
        import test_step21_contract as contracts
        from tg_testcase.gateway_transport import GatewayTransport, BoundedGatewayRegistry
        from tg_testcase.controller_bridge import ControllerBridge
        from tg_testcase.telegram_identity import IdentityLedger
        from tg_testcase.application import Application
        from tg_testcase.store import Store
        fixture = contracts.Base()
        fixture.setUp()
        try:
            registry = BoundedGatewayRegistry(fixture.registry.db_path, fixture.guard)
            identity = IdentityLedger(registry, {('gateway', 'bot'): (101, 'notion-controller-v1')})
            app = Application(Store(fixture.root / 'app'), {'42'})
            bridge = ControllerBridge(app, registry, identity, authorize_other=lambda *a: True)
            transport = GatewayTransport(bridge, clock=lambda: 1)
            action = dict(protocol_version=1, operation='create_or_get_active', payload={})
            raw = contracts.canonical(contracts.envelope(action=action))
            results = []
            for _ in range(2):
                conn = MemorySocket(struct.pack('!I', len(raw)) + raw)
                transport(conn, KernelPeer(1, 101, 102), time.monotonic())
                result = json.loads(conn.output[4:])
                self.assertTrue(result['ok'], result)
                results.append(result)
            self.assertEqual(results[0], results[1])
        finally:
            fixture.tearDown()


class ProcessBoundaryTests(unittest.TestCase):
    explicit_capability = commit.CommitTests.explicit_capability
    setUp = InjectedReceiverTests.setUp
    tearDown = InjectedReceiverTests.tearDown
    send = InjectedReceiverTests.send
    wire = InjectedReceiverTests.wire
    exchange = InjectedReceiverTests.exchange
    # Real-process lock proof; not evidence of successful AF_UNIX operations.
    def test_other_process_can_lock_at_eof_and_ack(self):
        import select
        import signal
        import sqlite3
        from tg_testcase.unix_transport import write_response
        from tg_testcase.authorization import AuthorizationGuard
        from tg_testcase.store import Store
        def probe():
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                status = b'0'
                try:
                    with AuthorizationGuard(self.lock).hold():
                        conn = sqlite3.connect(self.registry.db_path, timeout=.1)
                        try:
                            conn.execute('BEGIN IMMEDIATE')
                            conn.rollback()
                        finally:
                            conn.close()
                        store = Store(self.store.root, protected_acquisition=True, capability=self.store_cap)
                        with store._locked() as conn:
                            conn.execute('BEGIN IMMEDIATE')
                            conn.rollback()
                    status = b'1'
                except Exception:
                    pass
                os.write(write_fd, status)
                os.close(write_fd)
                os._exit(0)
            os.close(write_fd)
            try:
                ready, _, _ = select.select([read_fd], [], [], 2)
                if not ready:
                    os.kill(pid, signal.SIGKILL)
                self.assertTrue(ready, 'child lock probe blocked')
                self.assertEqual(os.read(read_fd, 1), b'1')
            finally:
                os.close(read_fd)
                os.waitpid(pid, 0)
        original = SocketReader.eof
        def eof(reader):
            probe()
            return original(reader)
        def write(conn, data):
            probe()
            return write_response(conn, data)
        with patch.object(SocketReader, 'eof', eof), patch('tg_testcase.receiver_service.write_response', write):
            self.assertEqual(self.exchange(self.wire())['type'], 'ACK')
