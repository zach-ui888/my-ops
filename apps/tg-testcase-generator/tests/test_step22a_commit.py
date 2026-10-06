"""Real SQLite commit boundary tests; synthetic data, project-local storage."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import os
import sqlite3
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_step21_contract as contracts
import test_notion_package as packages
from tg_testcase.acquisition import OfflineAcquisition
from tg_testcase.acquisition_adapter import ProtectedAcquisitionAdapter
from tg_testcase.application import Application
from tg_testcase.contract_types import canonical
from tg_testcase.protected_acquisition import ProtectedReceiverAcquisition, _CommitBoundary
from tg_testcase.registry_reader import RegistryReader
from tg_testcase.receiver import receive, frame, BINDING, HELLO, PACKAGE, COMMIT, ARTIFACT, CHUNK, END
from tg_testcase.capability_root import CapabilityRoot
from tg_testcase.store import Store
from tg_testcase.streaming import write_blob


class CommitTests(contracts.Base):
    def setUp(self):
        contracts.Base.setUp(self)
        self.now = 1.0
        self.mono = 1.0
        acquisition_root = self.root / 'acquisition'
        acquisition_root.mkdir(mode=0o700)
        self.store_cap = self.explicit_capability(acquisition_root)
        self.store = Store(acquisition_root, protected_acquisition=True, capability=self.store_cap)
        self.staging = self.root / 'staging'
        self.staging.mkdir(mode=0o700)
        self.staging_cap = self.explicit_capability(self.staging)
        self.app = Application(self.store, {'42'})
        self.task = None
        self.send('create_or_get_active')
        result = self.send('append_notion_reference', {'reference': contracts.ROOT})
        self.bid, self.sid = result['data']['batch_id'], result['data']['source_id']
        self.send('finish_collection')
        self.port = ProtectedAcquisitionAdapter(self.store, self.guard, lambda: self.now)
        action = dict(protocol_version=1, operation='finish_collection', task_id=self.task.id,
                      expected_version=self.task.version - 1, payload={})
        reserved = self.identity.reserve(canonical(contracts.envelope(action=action)), self.peer, 1000)
        self.identity.complete(reserved['request_id'], {'ok': True})
        self.job = self.registry.register(reserved['request_id'], self.task.id, self.bid, self.sid, self.port, 1000)
        self.registry.start_claim(self.job['job_id'], 'worker', self.port, 1000)
        with self.guard.hold():
            self.claim = self.port.lookup_claim(self.bid, self.sid)
        self.p = packages.package(task_id=self.task.id, batch_id=self.bid, source_id=self.sid,
                                  root=dict(type='page', id=contracts.ROOT),
                                  nodes=[dict(id=contracts.ROOT, type='page', parent_id=None, text='Synthetic')])
        packages.artifact(self.p, b'abc', 'pdf')
        self.p['nodes'][-1]['parent_id'] = contracts.ROOT
        if getattr(self, 'failed_package', False):
            self.p.update(outcome='failed', artifacts=[], nodes=[], gaps=[])
            self.p['scope'] = {k: False for k in self.p['scope']}
        self.raw = packages.raw(self.p)
        packages.n.load_package(self.raw)
        self.registry.pin(self.job['job_id'], self.p['package_id'], hashlib.sha256(self.raw).hexdigest(), len(self.raw), 'spool', 1000)
        self.job = self.registry.begin_submit(self.job['job_id'], self.port, 1000)
        self.reader = RegistryReader(self.registry.db_path, self.guard)
        self.importer = ProtectedReceiverAcquisition(self.store, self.reader, self.job['job_id'],
                                                     staging_root=self.staging_cap, clock=lambda: self.now,
                                                     monotonic=lambda: self.mono)

    def explicit_capability(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            cap = CapabilityRoot(path, fd)
        finally:
            os.close(fd)
        self.addCleanup(cap.close)
        return cap

    def test_protected_staging_never_uses_local_factory(self):
        from tg_testcase.internal_errors import ContractError
        with patch.object(CapabilityRoot, 'local', side_effect=AssertionError('fallback')):
            for root in (self.staging, str(self.staging), None):
                with self.subTest(kind=type(root)), self.assertRaisesRegex(ContractError, '^AUTH_INVALID$'):
                    ProtectedReceiverAcquisition(self.store, self.reader, self.job['job_id'],
                                                 staging_root=root)
            self.seal()
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_protected_seal_rejects_staging_path_replacement(self):
        from tg_testcase.internal_errors import ContractError
        self.importer.staging_root = self.staging
        with patch.object(CapabilityRoot, 'local', side_effect=AssertionError('fallback')):
            with self.assertRaisesRegex(ContractError, '^AUTH_INVALID$'):
                self.seal()
        self.empty()

    def send(self, operation, payload=None):
        req = dict(protocol_version=1, request_id=uuid4().hex,
                   actor=dict(user_id='42', chat_id='42', chat_type='private'), operation=operation, payload=payload or {})
        if self.task:
            self.task = self.store.get(self.task.id)
            req.update(task_id=self.task.id, expected_version=self.task.version)
        result = self.app.handle(req)
        self.assertTrue(result['ok'], result)
        self.task = self.store.get(result['task_id'])
        return result

    def seal(self):
        return self.importer.seal(self.claim, self.raw, {'a': b'abc'})

    def wire(self, *, tail=b''):
        hello = dict(protocol_version=1, **{k: self.job[k] for k in BINDING},
                     package_length=len(self.raw), package_sha256=hashlib.sha256(self.raw).hexdigest())
        a = self.p['artifacts'][0]
        return b''.join([frame(HELLO, canonical(hello)), frame(PACKAGE, self.raw),
                         frame(ARTIFACT, canonical(dict(artifact_id='a', size=3, sha256=a['sha256']))),
                         frame(CHUNK, b'abc'), frame(END), frame(COMMIT), tail])

    def sql(self, statement, args=()):
        with self.store._locked() as conn, conn:
            return conn.execute(statement, args).fetchall()

    def empty(self):
        for table in ('source_packages', 'source_package_artifacts', 'batch_input_manifests', 'acquisition_commit_outbox'):
            self.assertEqual(self.sql('SELECT count(*) FROM ' + table), [(0,)])
        self.assertEqual(list(self.staging.iterdir()), [])

    def mutate_task(self, mutation):
        with self.guard.hold():
            self.store.change(self.task.id, '42', self.store.get(self.task.id).version, mutation)

    def revoke(self):
        p = contracts.policy(2)
        p['principals'] = {}
        self.registry.publish(canonical(p), 1, 'revoke', 1000)

    def test_commit_replay_outbox_and_independent_registry_consumption(self):
        response = receive(self.importer, self.claim, self.job, BytesIO(self.wire()))
        self.assertEqual(response['type'], 'ACK', response)
        with self.store._locked() as conn:
            cursor = conn.execute('SELECT * FROM acquisition_commit_outbox')
            event = dict(zip([d[0] for d in cursor.description], cursor.fetchone()))
        self.assertEqual(event['package_digest'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(event['attempt'], self.claim['attempt'])
        self.assertEqual(receive(self.importer, self.claim, self.job, BytesIO(self.wire())), response)
        self.assertEqual(self.sql('SELECT count(*) FROM acquisition_commit_outbox'), [(1,)])
        with patch.object(self.registry, 'transaction', side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(sqlite3.Error):
                self.registry.consume_outbox(event, 1000)
        self.registry.consume_outbox(event, 1000)
        self.registry.consume_outbox(event, 1000)
        with self.guard.hold():
            self.assertEqual(self.registry.get_job(self.job['job_id'])['state'], 'committed')
        self.revoke()
        self.assertEqual(receive(self.importer, self.claim, self.job, BytesIO(self.wire()))['type'], 'ERROR')
        self.assertEqual(self.sql('SELECT count(*) FROM source_packages'), [(1,)])
        for op in ('UPDATE acquisition_commit_outbox SET committed_at=2', 'DELETE FROM acquisition_commit_outbox'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.sql(op)

    def test_eof_and_input_outside_guard(self):
        self.assertEqual(receive(self.importer, self.claim, self.job, BytesIO(self.wire(tail=b'x')))['type'], 'ERROR')
        self.empty()
        outer = self
        class Input(BytesIO):
            def read(self, count=-1):
                outer.assertFalse(outer.guard.held)
                return super().read(count)
        self.assertEqual(receive(self.importer, self.claim, self.job, Input(self.wire()))['type'], 'ACK')

    def test_ordinary_task_version_is_not_seal_cas(self):
        self.mutate_task(lambda task: None)
        self.seal()
        self.assertEqual(self.sql('SELECT count(*) FROM acquisition_commit_outbox'), [(1,)])

    def test_production_primitive_bypass_rejected(self):
        primitive = OfflineAcquisition(self.store, lambda: self.now)
        for call in (lambda: primitive.seal(self.claim, self.raw, {'a': b'abc'}),
                     lambda: primitive.seal_stream(self.claim, BytesIO(self.raw), len(self.raw), []),
                     lambda: primitive.seal_failure(self.claim),
                     lambda: self.importer.seal_failure(self.claim)):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(receive(primitive, self.claim, self.job, BytesIO(self.wire()))['type'], 'ERROR')
        self.empty()

    def test_deadline_expires_during_blob_rolls_back_all(self):
        def expired(*args):
            write_blob(*args)
            self.now = self.job['deadline'] / 1000
        with patch('tg_testcase.streaming.write_blob', expired), self.assertRaises(ValueError):
            self.seal()
        self.empty()
        self.assertEqual(self.sql('SELECT status FROM source_fetch_runs'), [('fetching',)])

    def test_monotonic_expiry_with_wall_clock_rollback(self):
        def expired(*args):
            write_blob(*args)
            self.now = 0.5
            self.mono = 902
        with patch('tg_testcase.streaming.write_blob', expired), self.assertRaises(ValueError):
            self.seal()
        self.empty()

    def test_lease_expires_during_blob_rolls_back_all(self):
        def expired(*args):
            write_blob(*args)
            self.now = self.claim['lease_until'] / 1000
        with patch('tg_testcase.streaming.write_blob', expired), self.assertRaises(ValueError):
            self.seal()
        self.empty()

    def test_failure_after_outbox_rolls_back(self):
        finish = _CommitBoundary.finish
        def broken(boundary, conn):
            finish(boundary, conn)
            raise sqlite3.OperationalError('synthetic')
        with patch.object(_CommitBoundary, 'finish', broken), self.assertRaises(sqlite3.Error):
            self.seal()
        self.empty()

    def test_guard_and_registry_snapshot_order(self):
        before = _CommitBoundary.before
        def checked(boundary, conn):
            self.assertTrue(self.guard.held)
            self.assertIsNone(self.reader._connection)
            self.assertTrue(conn.in_transaction)
            with patch.object(self.reader, 'transaction', side_effect=AssertionError('reverse registry read')):
                return before(boundary, conn)
        with patch.object(_CommitBoundary, 'before', checked):
            self.seal()

    def test_commit_then_revoke_waits_for_guard(self):
        entered, started, done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def revoke():
            entered.wait(5)
            started.set()
            try:
                self.revoke()
            except BaseException as exc:
                errors.append(exc)
            done.set()
        thread = threading.Thread(target=revoke)
        thread.start()
        def blob(*args):
            write_blob(*args)
            entered.set()
            self.assertTrue(started.wait(5))
            self.assertFalse(done.is_set())
        try:
            with patch('tg_testcase.streaming.write_blob', blob):
                self.seal()
        finally:
            entered.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertTrue(done.is_set())
        self.assertEqual(self.sql('SELECT count(*) FROM source_packages'), [(1,)])

    def test_adapter_requires_guard_and_absolute_lease(self):
        with self.assertRaises(ValueError):
            self.port.lookup_claim(self.bid, self.sid)
        with self.guard.hold():
            self.now = 5
            updated = self.port.heartbeat(self.claim, 60001)
            self.assertLessEqual(updated['lease_until'], 60001)
            self.now = 61
            with self.assertRaises(ValueError):
                self.port.heartbeat(updated, 60001)

    def test_readonly_reader_cannot_write(self):
        with self.guard.hold(), self.reader.transaction() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('DELETE FROM jobs')


    def test_reopening_store_cannot_downgrade_protection(self):
        with self.assertRaisesRegex(ValueError, '^Protected Store requires explicit CapabilityRoot$'):
            Store(self.store.root)
        reopened = Store(self.store.root, capability=self.store_cap)
        self.assertTrue(reopened.protected_acquisition)
        with self.assertRaises(ValueError):
            OfflineAcquisition(reopened).seal(self.claim, self.raw, {'a': b'abc'})
        self.empty()

    def test_claim_revision_mismatch_never_repaired_by_upsert(self):
        self.sql("UPDATE source_fetch_runs SET revision=2,lease_until=0")
        with self.guard.hold():
            source = self.port.inspect_source(self.task.id, self.bid, self.sid)
            with self.assertRaises(ValueError):
                self.port.claim(source, 'new-worker', 61000)
        self.assertEqual(self.sql('SELECT revision,attempt FROM source_fetch_runs'), [(2, 1)])

    def test_absolute_lease_expired_while_waiting_for_store(self):
        locked = self.store._locked
        @contextmanager
        def delayed():
            self.now = 62
            with locked() as conn:
                yield conn
        with self.guard.hold():
            source = self.port.inspect_source(self.task.id, self.bid, self.sid)
            with patch.object(self.store, '_locked', delayed), self.assertRaises(ValueError):
                self.port.claim(source, 'worker2', 61000)
        self.assertEqual(self.sql('SELECT attempt FROM source_fetch_runs'), [(1,)])

    def test_failure_package_requires_pin_and_normal_authorization(self):
        # A distinct authorized source/job can pin a failed package normally.
        self.p.update(outcome='failed', artifacts=[], nodes=[], gaps=[])
        self.p['scope'] = {k: False for k in self.p['scope']}
        failed = packages.raw(self.p)
        # Existing pin is immutable; changing to failed content must be refused.
        with self.assertRaises(ValueError):
            self.importer.seal(self.claim, failed, {})
        self.empty()

    def test_sqlite_process_exit_during_transaction_rolls_back(self):
        import os
        pid = os.fork()
        if pid == 0:
            def exit_after_blob(*args):
                write_blob(*args)
                os._exit(71)
            with patch('tg_testcase.streaming.write_blob', exit_after_blob):
                self.seal()
            os._exit(72)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 71)
        reopened = Store(self.store.root, capability=self.store_cap)
        with reopened._locked() as conn:
            for table in ('source_packages', 'source_package_artifacts', 'batch_input_manifests', 'acquisition_commit_outbox'):
                self.assertEqual(conn.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT status FROM source_fetch_runs').fetchone()[0], 'fetching')

    def test_sqlite_process_exit_after_commit_preserves_event(self):
        import os
        pid = os.fork()
        if pid == 0:
            self.seal()
            os._exit(73)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 73)
        reopened = Store(self.store.root, capability=self.store_cap)
        with reopened._locked() as conn:
            for table in ('source_packages', 'source_package_artifacts', 'batch_input_manifests', 'acquisition_commit_outbox'):
                self.assertEqual(conn.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT status FROM source_fetch_runs').fetchone()[0], 'sealed')


    def test_manifest_corruption_after_write_rejects_commit(self):
        from tg_testcase.acquisition import seal_inputs
        def corrupt(conn, batch_id):
            seal_inputs(conn, batch_id)
            conn.execute("UPDATE collection_batches SET input_manifest_digest=? WHERE id=?", ('f'*64, batch_id))
        with patch('tg_testcase.acquisition.seal_inputs', corrupt), self.assertRaises(ValueError):
            self.seal()
        self.empty()

    def test_authoritative_claim_rechecked_after_blob(self):
        def changed(*args):
            write_blob(*args)
            args[0].execute('UPDATE source_fetch_runs SET fencing_token=2')
        with patch('tg_testcase.streaming.write_blob', changed), self.assertRaises(ValueError):
            self.seal()
        self.empty()
        self.assertEqual(self.sql('SELECT fencing_token FROM source_fetch_runs'), [(1,)])

    def test_outbox_insert_failure_rolls_back_package_and_manifest(self):
        self.sql("CREATE TRIGGER reject_event BEFORE INSERT ON acquisition_commit_outbox BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.seal()
        self.empty()

    def test_package_pin_changed_bytes_rejected_at_boundary(self):
        self.p['nodes'][0]['text'] = 'changed synthetic data'
        self.raw = packages.raw(self.p)
        with self.assertRaises(ValueError):
            self.seal()
        self.empty()

    def test_sealed_claim_cannot_be_reclaimed(self):
        self.seal()
        self.now = 100
        with self.guard.hold():
            source = self.port.inspect_source(self.task.id, self.bid, self.sid)
            with self.assertRaises(ValueError):
                self.port.claim(source, 'new-worker', 160000)
        self.assertEqual(self.sql('SELECT attempt,status FROM source_fetch_runs'), [(1, 'sealed')])


    def test_fake_trusted_job_cannot_replace_registry_binding(self):
        other = dict(self.job, job_id='fake-job')
        hello = dict(protocol_version=1, **{k: other[k] for k in BINDING},
                     package_length=len(self.raw), package_sha256=hashlib.sha256(self.raw).hexdigest())
        valid_wire = self.wire()
        hello_end = 5 + int.from_bytes(valid_wire[1:5], 'big')
        wire = frame(HELLO, canonical(hello)) + valid_wire[hello_end:]
        self.assertEqual(receive(self.importer, self.claim, other, BytesIO(wire))['type'], 'ERROR')
        self.empty()


    def test_missing_or_forged_commit_context_cannot_bypass(self):
        for value in (None, lambda *args: nullcontext(None), lambda *args: nullcontext({'decision': True})):
            with patch.object(self.importer, '_commit_protection', value), self.assertRaises(ValueError):
                self.seal()
            self.empty()


def denied_case(change):
    def test(self):
        def artifacts():
            yield 'a', BytesIO(b'abc')
            with self.guard.hold():
                change(self)
        entered = []
        before = _CommitBoundary.before
        def checked(boundary, conn):
            entered.append(True)
            return before(boundary, conn)
        with patch.object(_CommitBoundary, 'before', checked), self.assertRaises(ValueError):
            self.importer.seal_stream(self.claim, BytesIO(self.raw), len(self.raw), artifacts())
        self.assertTrue(entered)
        self.empty()
    return test


for name, change in {
    'policy_revoke': lambda s: s.revoke(),
    'rotate': lambda s: s.registry.mutate_credential('notion-read-a', 'rotate', 'rotate', 1000),
    'disable': lambda s: s.registry.mutate_credential('notion-read-a', 'disable', 'disable', 1000),
    'delete': lambda s: s.registry.mutate_credential('notion-read-a', 'delete', 'delete', 1000),
    'verification': lambda s: s.registry.publish(canonical(dict(contracts.policy(2), principals={}, grants={}, workspaces={}, credentials={})), 1, 'verification', 1000, verification_changes=[contracts.verified(2, 'revoked')]),
    'job_cancel': lambda s: s.registry.stop(s.job['job_id'], 'JOB_CANCELLED', 1000),
    'task_cancel': lambda s: s.send('cancel'),
    'owner': lambda s: s.sql("UPDATE tasks SET user_id='43'"),
    'source_revision': lambda s: s.sql('UPDATE source_inputs SET revision=2'),
    'source_root': lambda s: s.sql("UPDATE source_inputs SET locator=?", ('b'*32,)),
    'task_source_root': lambda s: s.mutate_task(lambda t: setattr(t.sources[0], 'locator', 'b'*32)),
    'attempt': lambda s: s.sql('UPDATE source_fetch_runs SET attempt=2'),
    'fence': lambda s: s.sql('UPDATE source_fetch_runs SET fencing_token=2'),
    'worker': lambda s: s.sql("UPDATE source_fetch_runs SET worker_id='other'"),
    'lease': lambda s: s.sql('UPDATE source_fetch_runs SET lease_until=1'),
    'batch_state': lambda s: s.sql("UPDATE collection_batches SET status='abandoned'"),
    'membership': lambda s: s.sql("UPDATE collection_batches SET source_ids='[]'"),
    'blocked_internal': lambda s: s.registry.stop(s.job['job_id'], 'INTERNAL_FAILURE', 1000),
}.items():
    setattr(CommitTests, 'test_before_guard_rejects_' + name, denied_case(change))


class MigrationTests(unittest.TestCase):
    def test_v3_additive_preserves_rows_and_rolls_back_failure(self):
        import tempfile
        from pathlib import Path
        from tg_testcase.migrations import migrate
        base = contracts.PROJECT / '.test-runtime'
        with tempfile.TemporaryDirectory(dir=base) as directory:
            store = Store(Path(directory) / 'store')
            task = store.create('42')
            with store._locked() as conn, conn:
                conn.execute('DROP TABLE acquisition_commit_outbox')
                conn.execute('PRAGMA user_version=3')
                original = conn.execute('SELECT * FROM tasks').fetchall()
                conn.execute('CREATE TABLE acquisition_commit_outbox(dummy TEXT)')
            with store._locked() as conn:
                before = conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
                with self.assertRaises(sqlite3.OperationalError):
                    migrate(conn)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
                self.assertEqual(conn.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall(), before)
                conn.execute('DROP TABLE acquisition_commit_outbox'); conn.commit()
                migrate(conn); migrate(conn)
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 4)
                self.assertEqual(conn.execute('SELECT * FROM tasks').fetchall(), original)
                self.assertEqual(conn.execute('SELECT count(*) FROM acquisition_commit_outbox').fetchone()[0], 0)
            self.assertEqual(Store(store.root).get(task.id).dumps(), task.dumps())


class FailedPackageTests(contracts.Base):
    failed_package = True
    explicit_capability = CommitTests.explicit_capability
    setUp = CommitTests.setUp
    send = CommitTests.send
    sql = CommitTests.sql
    empty = CommitTests.empty
    revoke = CommitTests.revoke

    def test_authorized_pinned_failure_commits_outbox(self):
        self.importer.seal(self.claim, self.raw, {})
        self.assertEqual(self.sql('SELECT outcome FROM source_packages'), [('failed',)])
        self.assertEqual(self.sql('SELECT count(*) FROM acquisition_commit_outbox'), [(1,)])

    def test_revoked_pinned_failure_cannot_commit(self):
        self.revoke()
        with self.assertRaises(ValueError):
            self.importer.seal(self.claim, self.raw, {})
        self.empty()


def committed_history_case(change):
    def test(self):
        self.seal()
        package = self.sql('SELECT * FROM source_packages')
        event = self.sql('SELECT * FROM acquisition_commit_outbox')
        with self.guard.hold():
            change(self)
        self.assertEqual(self.sql('SELECT * FROM source_packages'), package)
        self.assertEqual(self.sql('SELECT * FROM acquisition_commit_outbox'), event)
        self.assertEqual(self.sql('SELECT status FROM source_fetch_runs'), [('sealed',)])
    return test


for name, change in {
    'rotate': lambda s: s.registry.mutate_credential('notion-read-a', 'rotate', 'rotate', 1000),
    'job_cancel': lambda s: s.registry.stop(s.job['job_id'], 'JOB_CANCELLED', 1000),
    'task_cancel': lambda s: s.send('cancel'),
}.items():
    setattr(CommitTests, 'test_commit_before_' + name + '_preserves_history', committed_history_case(change))
