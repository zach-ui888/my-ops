"""Synthetic, project-local Step 2.1 contract tests; no network or credentials."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from tg_testcase.authorization import AuthorizationGuard, authorize_commit
from tg_testcase.contract_types import canonical, digest, strict_json
from tg_testcase.controller_ports import SourceSnapshot, outbox_event
from tg_testcase.internal_errors import ContractError, ErrorCode, ERROR_INFO, select_error, AuditEvent
from tg_testcase.job_registry import JobRegistry, EDGES, TERMINAL
from tg_testcase.scope_policy import Policy
from tg_testcase.notion_reference import parse_notion_reference
from tg_testcase.telegram_identity import IdentityLedger, GatewayLedger, Peer, request_id
from tg_testcase.runtime_paths import RuntimePaths, RuntimeOwners, PathMetadata

PROJECT = Path(__file__).resolve().parents[1]
ROOT = '0123456789abcdef0123456789abcdef'


def policy(epoch=1, generation=1, enabled=True):
    return dict(schema_version=1, policy_version='scope-1', policy_epoch=epoch, default_effect='deny',
                principals={'telegram:42': dict(operations=['refresh', 'cancel', 'acquire', 'append_notion_reference'], grants=['grant-a'])},
                grants={'grant-a': dict(workspace_ref='workspace-a', credential_ref='notion-read-a',
                                       roots=[dict(type='page', id=ROOT, descendants='structural')],
                                       relations='deny_follow', synced_references='deny_follow', links='deny_follow',
                                       external_attachments='deny', limits_profile='notion-standard')},
                workspaces={'workspace-a': dict(verification_ref='verify-a')},
                credentials={'notion-read-a': dict(workspace_ref='workspace-a', secret_handle='notion-read-a', generation=generation, enabled=enabled)})


def credential(generation=1, enabled=True, deleted=False):
    return dict(credential_ref='notion-read-a', generation=generation, workspace_ref='workspace-a',
                secret_handle='notion-read-a', enabled=enabled, deleted=deleted, changed_at=1000)


def verified(version=1, status='verified'):
    return dict(verification_ref='verify-a', workspace_ref='workspace-a', status=status, record_version=version)


def envelope(update=7, issued=1000, nonce=None, action=None, uid=42):
    if action is None:
        action = dict(protocol_version=1, operation='append_notion_reference', task_id='task',
                      expected_version=1, payload={'reference': ROOT})
    return dict(audience='notion-controller-v1', auth_context=dict(issuer='gateway', bot_instance='bot',
                telegram_user_id=uid, chat_id=uid, chat_type='private', update_id=update,
                issued_at=issued, expires_at=issued + 60000, nonce=nonce or ('%032x' % update)),
                action=action, action_digest=digest(action))


class FakeAcquisition:
    def __init__(self):
        self.source = SourceSnapshot('task', 'batch', 'source', '42', 1, ROOT, True, False, False)
        self.current = None
        self.observed = {'status': 'not_committed'}
        self.crash_after_claim = False
        self.claim_count = 0

    def inspect_source(self, task_id, batch_id, source_id):
        return self.source

    def claim(self, source, worker_id, lease_until):
        self.claim_count += 1
        old = self.current
        self.current = dict(batch_id=source.batch_id, source_id=source.source_id, revision=source.revision,
                            worker_id=worker_id, attempt=old['attempt'] + 1 if old else 1,
                            fencing_token=old['fencing_token'] + 1 if old else 1,
                            lease_until=lease_until, state='claimed')
        if self.crash_after_claim:
            raise RuntimeError('synthetic crash')
        return dict(self.current)

    def lookup_claim(self, batch_id, source_id):
        return deepcopy(self.current)

    def transition(self, claim, target, retry_at=None):
        self.current['state'] = target
        if retry_at is not None:
            self.current['lease_until'] = retry_at

    def heartbeat(self, claim, lease_until):
        self.current['lease_until'] = lease_until
        return dict(self.current)

    def receipt(self, job):
        return deepcopy(self.observed)


class Base(unittest.TestCase):
    def setUp(self):
        base = PROJECT / '.test-runtime'
        base.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.lock = self.root / 'authorization.lock'
        self.lock.touch(mode=0o660)
        self.guard = AuthorizationGuard(self.lock)
        self.registry = JobRegistry(self.root / 'registry.sqlite3', self.guard)
        self.registry.publish(canonical(policy()), 0, 'initial', 1000,
                              credential_changes=[credential()], verification_changes=[verified()])
        self.identity = IdentityLedger(self.registry, {('gateway', 'bot'): (101, 'notion-controller-v1')})
        self.peer = Peer(101)
        self.port = FakeAcquisition()

    def tearDown(self):
        self.tmp.cleanup()

    def assertCode(self, code, call, *args, **kwargs):
        with self.assertRaises(ContractError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code.value, code)

    def job(self, update=7):
        reservation = self.identity.reserve(canonical(envelope(update)), self.peer, 1000)
        self.identity.complete(reservation['request_id'], {'ok': True})
        return self.registry.register(reservation['request_id'], 'task', 'batch', 'source', self.port, 1000)

    def submitting(self):
        job = self.job()
        self.registry.start_claim(job['job_id'], 'worker', self.port, 1000)
        self.registry.pin(job['job_id'], 'package', 'a' * 64, 100, 'spool', 1001)
        return self.registry.begin_submit(job['job_id'], self.port, 1002)

    def current(self, job):
        with self.guard.hold():
            return self.registry.get_job(job['job_id'])


class PrimitiveContract(unittest.TestCase):
    def test_json_rejections(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":{"x":1,"x":2}}', b'NaN', b'Infinity',
                    b'1.0', b'\xef\xbb\xbf{}', b'"\\ud800"', b'"\xff"', b'[' * 30 + b'0' + b']' * 30):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                strict_json(raw)
        self.assertEqual(canonical({'z': '中', 'a': 1}), '{"a":1,"z":"中"}'.encode())

    def test_reference_positive_and_limits(self):
        uuid = '01234567-89ab-cdef-0123-456789abcdef'
        for text in (ROOT, ROOT.upper(), uuid, 'https://www.notion.so/My-Page-' + ROOT,
                     'https://notion.so/' + uuid, 'https://notion.so/' + 'a' * 80 + '-' + uuid):
            self.assertEqual(parse_notion_reference(text), ROOT)
        invalid = ['0123-' + ROOT[4:], ' ' + ROOT, ROOT + '\n',
                   'https://notion.so/x' + ROOT, 'https://notion.so/' + ROOT + '?',
                   'https://notion.so/' + ROOT + '#', 'https://u@notion.so:443/' + ROOT,
                   'https://notion.so/a%252Fb-' + ROOT, 'https://notion.site/' + ROOT,
                   'https://notion.so/' + 'a' * 81 + '-' + uuid, 'a' * 512, 'a' * 513,
                   'https://notion.so/a/' + ROOT, 'https://notion.so/' + ROOT + '/',
                   'HTTPS://notion.so/' + ROOT, 'https://NOTION.SO/' + ROOT,
                   'https://notion.so./' + ROOT, 'https://notion.so:443/' + ROOT]
        invalid += ['https://notion.so/' + ROOT + suffix for suffix in ('\r', '\n', '\t', '\x00', '\\', '%2f', '%5c', '%25', '?v=a', '\x7f', '中')]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ContractError):
                parse_notion_reference(text)

    def test_errors_are_closed_prioritized_and_sanitized(self):
        self.assertEqual(len(ErrorCode), 35)
        self.assertEqual(select_error(['JOB_CANCELLED', 'POLICY_REVOKED', 'CREDENTIAL_STALE']), ErrorCode.CREDENTIAL_STALE)
        for code in ErrorCode:
            err = ContractError(code)
            self.assertEqual(str(err), code.value)
            self.assertEqual(err.application_code, ERROR_INFO[code][3])
        with self.assertRaises(ValueError):
            ContractError('arbitrary-body')
        audit = dict(error='AUTH_INVALID', principal_alias='a' * 64, job_id='job', request_id='request',
                     policy_epoch=1, policy_digest='b' * 64, at=0, count=1, endpoint='none')
        AuditEvent.parse(audit)
        with self.assertRaises(ValueError):
            AuditEvent.parse(dict(audit, headers='secret'))


class PolicyContract(Base):
    def test_canonical_sets_and_strict_schema(self):
        p = policy()
        first = Policy.parse(canonical(p), {'verify-a': verified()}, {'notion-read-a': credential()})
        p['principals']['telegram:42']['operations'].reverse()
        second = Policy.parse(canonical(p), {'verify-a': verified()}, {'notion-read-a': credential()})
        self.assertEqual(first.canonical_bytes, second.canonical_bytes)
        for edit in (lambda p: p.update(deny_rules=[]), lambda p: p.update(policy_epoch=True),
                     lambda p: p['grants']['grant-a'].update(credential_ref='missing'),
                     lambda p: p['grants']['grant-a']['roots'][0].update(id=ROOT.upper()),
                     lambda p: p['principals']['telegram:42']['operations'].append('acquire')):
            p = policy(); edit(p)
            self.assertCode('POLICY_INVALID', Policy.parse, canonical(p), {'verify-a': verified()}, {'notion-read-a': credential()})

    def test_database_bidirectional_and_ambiguity(self):
        p = policy(); roots = p['grants']['grant-a']['roots']
        roots.extend([dict(type='database', id='b'*32, data_sources={'mode': 'explicit', 'ids': ['c'*32]}),
                      dict(type='data_source', id='c'*32, expected_database_id='b'*32)])
        Policy.parse(canonical(p), {'verify-a': verified()}, {'notion-read-a': credential()})
        roots.pop()
        self.assertCode('POLICY_INVALID', Policy.parse, canonical(p), {'verify-a': verified()}, {'notion-read-a': credential()})
        p = policy(); p['grants']['grant-b'] = deepcopy(p['grants']['grant-a'])
        p['principals']['telegram:42']['grants'].append('grant-b')
        self.assertCode('ROOT_AMBIGUOUS', Policy.parse, canonical(p), {'verify-a': verified()}, {'notion-read-a': credential()})

    def test_cas_rotation_history_and_atomic_failure(self):
        self.assertCode('POLICY_INVALID', self.registry.publish, canonical(policy(2)), 0, 'bad-cas', 1001)
        rotated = policy(2, 2)
        args = (canonical(rotated), 1, 'rotate', 1002)
        result = self.registry.publish(*args, credential_changes=[credential(2)])
        self.assertEqual(self.registry.publish(*args, credential_changes=[credential(2)]), result)
        with self.guard.hold(), self.registry.transaction() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM credential_history').fetchone()[0], 2)
        self.assertCode('POLICY_INVALID', self.registry.publish, canonical(policy(3, 4)), 2, 'skip', 1003,
                        credential_changes=[credential(4)])
        with self.guard.hold():
            self.assertEqual(self.registry.authority()[0].epoch, 2)
        self.assertCode('REQUEST_CONFLICT', self.registry.publish, canonical(policy(3, 3)), 2, 'rotate', 1003,
                        credential_changes=[credential(3)])

    def test_delete_tombstone_and_full_revoke(self):
        p = policy(2)
        for key in ('principals', 'grants', 'workspaces', 'credentials'):
            p[key] = {}
        self.registry.publish(canonical(p), 1, 'delete', 1001, credential_changes=[credential(2, False, True)])
        self.assertCode('POLICY_INVALID', self.registry.publish, canonical(policy(3)), 2, 'recreate', 1002,
                        credential_changes=[credential()])
        with self.guard.hold():
            current, heads = self.registry.authority()
            self.assertTrue(heads['notion-read-a']['deleted'])
            self.assertCode('SCOPE_DENIED', current.resolve, 'telegram:42', 'acquire', ROOT)

    def test_disabled_and_corrupt_authority(self):
        self.registry.publish(canonical(policy(2, 2, False)), 1, 'disable', 1002, credential_changes=[credential(2, False)])
        with self.guard.hold():
            self.assertCode('CREDENTIAL_STALE', self.registry.authority()[0].resolve, 'telegram:42', 'acquire', ROOT)
            with self.registry.transaction() as conn:
                conn.execute("UPDATE current_policy SET digest='bad'")
            self.assertCode('POLICY_INVALID', self.registry.authority)


class IdentityContract(Base):
    def test_durable_retry_expired_and_reordered(self):
        e = envelope()
        first = self.identity.reserve(canonical(e), self.peer, 1000)
        restarted = IdentityLedger(JobRegistry(self.registry.db_path, AuthorizationGuard(self.lock)), self.identity.peers)
        self.assertEqual(restarted.reserve(canonical(e), self.peer, 90000), first)
        self.assertEqual(first['request_id'], 'tg-' + digest(['gateway', 'bot', 7]))
        self.identity.reserve(canonical(envelope(4, issued=90000)), self.peer, 90000)
        self.assertCode('AUTH_INVALID', self.identity.reserve, canonical(envelope(8)), self.peer, 66000)
        for change in ('nonce', 'update_id', 'issued_at'):
            bad = deepcopy(e)
            if change == 'nonce': bad['auth_context'][change] = 'f' * 32
            elif change == 'update_id': bad['auth_context'][change] = 9
            else:
                bad['auth_context']['issued_at'] += 1; bad['auth_context']['expires_at'] += 1
            self.assertCode('AUTH_REPLAY', self.identity.reserve, canonical(bad), self.peer, 1001)

    def test_peer_digest_and_actor_forgery(self):
        e = envelope()
        for peer in (Peer(102), {'uid': 101}):
            self.assertCode('AUTH_INVALID', self.identity.reserve, canonical(e), peer, 1000)
        for field, value in [('actor', {'user_id': '42'}), ('request_id', 'chosen')]:
            bad = deepcopy(e); bad['action'][field] = value; bad['action_digest'] = digest(bad['action'])
            self.assertCode('AUTH_INVALID', self.identity.reserve, canonical(bad), self.peer, 1000)
        bad = deepcopy(e); bad['action_digest'] = 'f'*64
        self.assertCode('AUTH_INVALID', self.identity.reserve, canonical(bad), self.peer, 1000)
        for field, value in [('telegram_user_id', True), ('chat_id', 43), ('chat_type', 'group'), ('update_id', 2**31), ('expires_at', 61001)]:
            bad = deepcopy(e); bad['auth_context'][field] = value
            self.assertCode('AUTH_INVALID', self.identity.reserve, canonical(bad), self.peer, 1000)

    def test_time_boundaries_rollback_and_gc(self):
        self.identity.reserve(canonical(envelope(1, issued=6000)), self.peer, 1000)
        self.assertCode('AUTH_INVALID', self.identity.reserve, canonical(envelope(2, issued=6001)), self.peer, 1000)
        e = envelope(3, issued=100000)
        self.identity.reserve(canonical(e), self.peer, 164999)
        self.assertCode('AUTH_INVALID', self.identity.reserve, canonical(envelope(4, issued=100000)), self.peer, 165000)
        self.assertCode('AUTH_INVALID', self.identity.reserve, canonical(envelope(5, issued=1000)), self.peer, 1000)
        self.assertEqual(self.identity.reserve(canonical(e), self.peer, 1000)['request_id'], request_id(e['auth_context']))
        self.identity.gc(100000000)
        self.assertEqual(self.identity.reserve(canonical(e), self.peer, 100000000)['state'], 'reserved')
        rid = request_id(e['auth_context']); self.identity.complete(rid, {'ok': True})
        self.identity.gc(100000000)
        self.assertCode('AUTH_INVALID', self.identity.reserve, canonical(e), self.peer, 100000000)

    def test_concurrent_reservation_and_gateway_ledger(self):
        e = envelope()
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda _: self.identity.reserve(canonical(e), self.peer, 1000), range(8)))
        self.assertTrue(all(r == records[0] for r in records))
        gateway = GatewayLedger(self.registry, 'gateway', 'bot')
        first = gateway.envelope(20, 42, e['action'], 1000, 1000)
        second = GatewayLedger(self.registry, 'gateway', 'bot').envelope(20, 42, e['action'], 2000, 2000)
        self.assertEqual(first, second)
        self.assertCode('AUTH_INVALID', gateway.envelope, 20, 42, e['action'], 90000, 90000)


class JobContract(Base):
    def test_semantic_dedupe_and_no_parallel_claim(self):
        job = self.job()
        self.assertEqual(self.job(8)['job_id'], job['job_id'])
        current = self.registry.start_claim(job['job_id'], 'worker', self.port, 1000)
        self.assertEqual(current['state'], 'fetching')
        self.assertEqual(self.port.claim_count, 1)
        self.assertCode('FENCED', self.registry.start_claim, job['job_id'], 'other', self.port, 1001)
        self.registry.publish(canonical(policy(2)), 1, 'epoch', 1001)
        self.assertCode('FENCED', self.job, 9)

    def test_claim_crash_recovery_and_worker_mismatch(self):
        job = self.job(); self.port.crash_after_claim = True
        with self.assertRaises(RuntimeError):
            self.registry.start_claim(job['job_id'], 'worker', self.port, 1000)
        self.assertEqual(self.current(job)['state'], 'claim_pending')
        restarted = JobRegistry(self.registry.db_path, AuthorizationGuard(self.lock))
        recovered = restarted.recover_claim(job['job_id'], self.port, 1001)
        self.assertEqual(recovered['state'], 'fetching')
        self.assertEqual(self.port.claim_count, 1)

    def test_pin_fencing_deadline_and_ephemeral_decision(self):
        job = self.submitting()
        with self.guard.hold():
            decision = authorize_commit(self.registry, job['job_id'], self.port.current, 'package', 'a'*64, 1003)
            self.guard.validate_decision(decision)
            bad = dict(self.port.current, fencing_token=2)
            self.assertCode('FENCED', authorize_commit, self.registry, job['job_id'], bad, 'package', 'a'*64, 1003)
            self.assertCode('DIGEST_CONFLICT', authorize_commit, self.registry, job['job_id'], self.port.current, 'package', 'b'*64, 1003)
            self.assertCode('LEASE_LOST', authorize_commit, self.registry, job['job_id'], self.port.current, 'package', 'a'*64, 61000)
        with self.guard.hold():
            self.assertCode('POLICY_REVOKED', self.guard.validate_decision, decision)
            self.assertCode('POLICY_REVOKED', self.guard.validate_decision, object())
        self.assertCode('DIGEST_CONFLICT', self.registry.pin, job['job_id'], 'package', 'b'*64, 100, 'spool', 1003)

    def test_revoke_before_and_after_fake_commit(self):
        job = self.submitting(); entered = threading.Event(); attempted = threading.Event()
        def seal():
            with self.guard.hold():
                decision = authorize_commit(self.registry, job['job_id'], self.port.current, 'package', 'a'*64, 1003)
                entered.set()
                self.assertTrue(attempted.wait(5))
                self.guard.validate_decision(decision)
                self.port.observed = {'status': 'committed', 'receipt': dict(package_id='package', package_digest='a'*64, content_fingerprint='c'*64)}
        def revoke():
            self.assertTrue(entered.wait(5)); attempted.set()
            # A different guard instance must serialize with the seal guard.
            other = JobRegistry(self.registry.db_path, AuthorizationGuard(self.lock))
            return other.publish(canonical(policy(2)), 1, 'revoke', 1004)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(seal), pool.submit(revoke)
            a.result(timeout=10); b.result(timeout=10)
        self.assertEqual(self.registry.recovery_plan(job['job_id'], self.port, 1005), 'committed')
        with self.guard.hold():
            self.assertCode('POLICY_REVOKED', authorize_commit, self.registry, job['job_id'], self.port.current, 'package', 'a'*64, 1005)

    def test_cancel_and_pending_must_reconcile(self):
        job = self.submitting()
        stopped = self.registry.stop(job['job_id'], 'JOB_CANCELLED', 1003)
        self.assertEqual(stopped['state'], 'reconciling')
        self.port.observed = {'status': 'pending'}
        self.assertEqual(self.registry.recovery_plan(job['job_id'], self.port, 1004), 'pending')
        self.assertEqual(self.current(job)['state'], 'reconciling')
        self.port.observed = {'status': 'not_committed'}
        self.assertEqual(self.registry.recovery_plan(job['job_id'], self.port, 1005), 'cancelled')

    def test_outbox_idempotency_and_conflict(self):
        job = self.submitting()
        event = {k: job[k] for k in ('job_id', 'task_id', 'batch_id', 'source_id', 'revision', 'attempt', 'fencing_token',
                                   'policy_epoch', 'policy_digest', 'credential_ref', 'credential_generation', 'package_id', 'package_digest')}
        event.update(event_id='event', content_fingerprint='c'*64, committed_at=1003)
        first = self.registry.consume_outbox(event, 1004)
        self.assertEqual(first['state'], 'committed')
        self.assertEqual(self.registry.consume_outbox(event, 1005), first)
        self.assertCode('DIGEST_CONFLICT', self.registry.consume_outbox, dict(event, content_fingerprint='d'*64), 1006)
        self.assertEqual(self.registry.stop(job['job_id'], 'JOB_CANCELLED', 1007)['state'], 'committed')

    def test_lost_spool_and_old_generation(self):
        job = self.submitting()
        self.assertEqual(self.registry.recovery_plan(job['job_id'], self.port, 1003, spool_present=False), 'failed')
        # Terminal may not be rewritten even if a fabricated later event arrives.
        with self.guard.hold(), self.registry.transaction() as conn:
            self.assertCode('FENCED', self.registry._transition, conn, self.current(job), 'fetching', 1004)

    def test_heartbeat_caps_deadline_and_rejects_rotation(self):
        job = self.job(); self.registry.start_claim(job['job_id'], 'worker', self.port, 1000)
        self.assertEqual(self.registry.heartbeat(job['job_id'], self.port, 2000)['lease_until'], 62000)
        self.registry.publish(canonical(policy(2, 2)), 1, 'rotate', 2001, credential_changes=[credential(2)])
        self.assertCode('CREDENTIAL_STALE', self.registry.heartbeat, job['job_id'], self.port, 2002)


class PathContract(unittest.TestCase):
    def setUp(self):
        base = str(PROJECT / '.test-runtime' / 'paths')
        self.paths = RuntimePaths(**{f: base + '/' + f for f in RuntimePaths.__dataclass_fields__})
        self.owners = RuntimeOwners(0, 101, 102, 103, 104, 105, 201, 202, 203)
        self.meta = {}
        for f in RuntimePaths.__dataclass_fields__:
            path = Path(getattr(self.paths, f))
            for p in (path, *path.parents):
                self.meta[str(p)] = PathMetadata(0, 0, 0o755, 'directory', False, False, False)
        owner_keys = {'release_root': 0, 'config_root': 0, 'registry_state_root': 102, 'acquisition_state_root': 103,
                      'receiver_staging_root': 103, 'producer_spool_root': 104, 'downloader_staging_root': 105,
                      'socket_root': 102, 'authorization_lock_path': 102}
        for f, uid in owner_keys.items():
            mode = 0o660 if f == 'authorization_lock_path' else 0o750 if f == 'socket_root' else 0o700
            self.meta[getattr(self.paths, f)] = PathMetadata(uid, 201, mode, 'file' if f == 'authorization_lock_path' else 'directory', False, False, False)
        class FakeFS:
            def __init__(inner): inner.meta = self.meta
            def inspect(inner, path): return inner.meta[path]
            def login_disabled(inner, uid): return True
            def runner_in_group(inner, gid): return False
        self.fs = FakeFS()

    def test_fake_ownership_and_fixed_names(self):
        self.paths.validate(self.owners, self.fs)
        self.assertTrue(self.paths.registry_db.endswith('/registry.sqlite3'))
        self.assertTrue(self.paths.acquisition_db.endswith('/tasks.sqlite3'))
        self.assertTrue(self.paths.receiver_socket.endswith('/receiver.sock'))
        for field in ('runner_writable', 'producer_writable'):
            key = str(Path(self.paths.authorization_lock_path).parent)
            old = self.meta[key]
            self.meta[key] = replace(old, **{field: True})
            with self.assertRaises(ValueError): self.paths.validate(self.owners, self.fs)
            self.meta[key] = old
        key = self.paths.receiver_staging_root
        self.meta[key] = replace(self.meta[key], kind='symlink')
        with self.assertRaises(ValueError): self.paths.validate(self.owners, self.fs)

    def test_path_and_owner_rejections(self):
        for value in ('relative', '/a/../b', '/a/./b', '/a\x00b', '/a//b'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(self.paths, registry_state_root=value)
        with self.assertRaises(ValueError): replace(self.paths, producer_spool_root=self.paths.registry_state_root)
        with self.assertRaises(ValueError): replace(self.paths, socket_root='/' + 'x'*100)
        with self.assertRaises(ValueError): replace(self.owners, gateway_uid=True)
        with self.assertRaises(ValueError): replace(self.owners, gateway_uid=0)
        with self.assertRaises(ValueError): replace(self.owners, gateway_uid=self.owners.receiver_uid)
        with self.assertRaises(ValueError): RuntimePaths.parse(canonical(dict(asdict(self.paths), staging='/arbitrary')))


class BridgeContract(Base):
    def setUp(self):
        super().setUp()
        from tg_testcase.application import Application
        from tg_testcase.store import Store
        from tg_testcase.controller_bridge import ControllerBridge
        self.store = Store(self.root / 'application')
        self.app = Application(self.store, {'42', '43'})
        self.allowed = True
        self.bridge = ControllerBridge(self.app, self.registry, self.identity,
                                       authorize_other=lambda principal, action, task: self.allowed)
        action = dict(protocol_version=1, operation='create_or_get_active', payload={})
        result = self.bridge.handle(canonical(envelope(1, action=action)), self.peer, 1000)
        self.assertTrue(result['ok'], result)
        self.task = self.store.get(result['task_id'])

    def action(self, operation='append_notion_reference', **overrides):
        a = dict(protocol_version=1, operation=operation, task_id=self.task.id, expected_version=self.task.version,
                 payload={'reference': 'https://notion.so/My-Page-' + ROOT})
        a.update(overrides)
        return a

    def test_real_application_normalization_retry_and_fixed_version(self):
        e = envelope(2, action=self.action())
        result = self.bridge.handle(canonical(e), self.peer, 1000)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.store.get(self.task.id).sources[0].locator, ROOT)
        stale = self.bridge.handle(canonical(envelope(3, action=self.action())), self.peer, 1001)
        self.assertEqual(stale['code'], 'stale_version')
        self.assertEqual(self.bridge.handle(canonical(e), self.peer, 90000), result)
        self.assertEqual(len(self.store.get(self.task.id).sources), 1)

    def test_crash_after_application_before_completion(self):
        e = envelope(2, action=self.action())
        with patch.object(self.identity, 'complete', side_effect=OSError('synthetic failure')):
            self.assertEqual(self.bridge.handle(canonical(e), self.peer, 1000)['code'], 'storage_error')
        result = self.bridge.handle(canonical(e), self.peer, 1001)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(self.store.get(self.task.id).sources), 1)

    def test_owner_revoke_cache_and_owner_safe_cancel(self):
        e = envelope(2, action=self.action())
        self.assertTrue(self.bridge.handle(canonical(e), self.peer, 1000)['ok'])
        bad = envelope(3, uid=43, action=self.action())
        self.assertEqual(self.bridge.handle(canonical(bad), self.peer, 1000)['code'], 'forbidden')
        p = policy(2)
        for k in ('principals', 'grants', 'workspaces', 'credentials'): p[k] = {}
        self.registry.publish(canonical(p), 1, 'revoke', 1001)
        self.assertEqual(self.bridge.handle(canonical(e), self.peer, 1002)['code'], 'forbidden')
        self.task = self.store.get(self.task.id)
        result = self.bridge.handle(canonical(envelope(4, action=self.action('cancel', payload={}))), self.peer, 1003)
        self.assertTrue(result['ok'], result)

    def test_content_reads_need_current_explicit_authorization(self):
        e = envelope(2, action=self.action('get_summary', payload={}))
        self.assertTrue(self.bridge.handle(canonical(e), self.peer, 1000)['ok'])
        self.allowed = False
        self.assertEqual(self.bridge.handle(canonical(e), self.peer, 1001)['code'], 'forbidden')


class ExtendedPolicyContract(Base):
    def test_admin_generation_mutations_delete_pruning_and_idempotency(self):
        result = self.registry.mutate_credential('notion-read-a', 'disable', 'disable-admin', 1001)
        self.assertEqual(result['epoch'], 2)
        self.assertEqual(self.registry.mutate_credential('notion-read-a', 'disable', 'disable-admin', 5000), result)
        self.assertCode('REQUEST_CONFLICT', self.registry.mutate_credential, 'notion-read-a', 'enable', 'disable-admin', 1002)
        self.registry.mutate_credential('notion-read-a', 'enable', 'enable-admin', 1002)
        self.registry.mutate_credential('notion-read-a', 'rotate', 'rotate-admin', 1003, secret_handle='new-label')
        with self.guard.hold():
            current, heads = self.registry.authority()
            self.assertEqual(heads['notion-read-a']['generation'], 4)
            self.assertEqual(current.resolve('telegram:42', 'acquire', ROOT).credential_generation, 4)
        self.registry.mutate_credential('notion-read-a', 'delete', 'delete-admin', 1004)
        with self.guard.hold(), self.registry.transaction() as conn:
            current, heads = self.registry.authority()
            self.assertEqual(current.data['principals'], {})
            self.assertEqual(current.data['grants'], {})
            self.assertEqual(current.data['credentials'], {})
            self.assertEqual(conn.execute('SELECT count(*) FROM credential_history').fetchone()[0], 5)
        self.assertCode('CREDENTIAL_STALE', self.registry.mutate_credential, 'notion-read-a', 'enable', 'reuse', 1005)

    def test_verification_revocation_is_atomic_and_blocks_old_jobs(self):
        job = self.submitting()
        # Dangling revoked verification is rejected without replacing any state.
        self.assertCode('POLICY_INVALID', self.registry.publish, canonical(policy(2)), 1, 'bad-revoke', 1003,
                        verification_changes=[verified(2, 'revoked')])
        with self.guard.hold():
            authorize_commit(self.registry, job['job_id'], self.port.current, 'package', 'a'*64, 1004)
        p = policy(2)
        for k in ('principals', 'grants', 'credentials', 'workspaces'): p[k] = {}
        self.registry.publish(canonical(p), 1, 'revoke-verification', 1005, verification_changes=[verified(2, 'revoked')])
        with self.guard.hold():
            self.assertCode('POLICY_REVOKED', authorize_commit, self.registry, job['job_id'], self.port.current, 'package', 'a'*64, 1006)

    def test_oversized_json_maps_and_unknown_values(self):
        bad = canonical(policy()).replace(b'"policy_epoch":1', b'"policy_epoch":1,"policy_epoch":1')
        self.assertCode('POLICY_INVALID', Policy.parse, bad, {'verify-a': verified()}, {'notion-read-a': credential()})
        for change in (lambda p: p.update(principals={'telegram:042': p['principals']['telegram:42']}),
                       lambda p: p['grants']['grant-a'].update(limits_profile='large'),
                       lambda p: p['credentials']['notion-read-a'].update(generation=True),
                       lambda p: p['principals']['telegram:42'].update(operations=[]),
                       lambda p: p.update(policy_epoch=2**63)):
            p = policy(); change(p)
            self.assertCode('POLICY_INVALID', Policy.parse, canonical(p), {'verify-a': verified()}, {'notion-read-a': credential()})
        self.assertCode('POLICY_INVALID', Policy.parse, b' ' * 1048577, {}, {})


class ExtendedJobContract(Base):
    def test_reservation_is_not_an_accepted_action(self):
        r = self.identity.reserve(canonical(envelope()), self.peer, 1000)
        self.assertCode('AUTH_INVALID', self.registry.register, r['request_id'], 'task', 'batch', 'source', self.port, 1000)

    def test_retry_wait_new_authoritative_attempt_and_fencing(self):
        job = self.job()
        first = self.registry.start_claim(job['job_id'], 'worker', self.port, 1000)
        self.assertCode('RATE_LIMIT_EXHAUSTED', self.registry.schedule_retry, job['job_id'], 'RATE_LIMIT_EXHAUSTED', 3000, self.port, 1001)
        waiting = self.registry.schedule_retry(job['job_id'], 'UPSTREAM_UNAVAILABLE', 3000, self.port, 1001)
        self.assertEqual(waiting['state'], 'retry_wait')
        self.assertEqual(self.registry.recovery_plan(job['job_id'], self.port, 2999), 'wait')
        self.assertCode('FENCED', self.registry.start_claim, job['job_id'], 'worker2', self.port, 2999)
        second = self.registry.start_claim(job['job_id'], 'worker2', self.port, 3000)
        self.assertEqual(second['attempt'], first['attempt'] + 1)
        self.assertEqual(second['fencing_token'], first['fencing_token'] + 1)
        old = dict(self.port.current, worker_id='worker', attempt=1, fencing_token=1)
        self.registry.pin(job['job_id'], 'package', 'a'*64, 1, 'spool', 3001)
        self.registry.begin_submit(job['job_id'], self.port, 3002)
        with self.guard.hold():
            self.assertCode('FENCED', authorize_commit, self.registry, job['job_id'], old, 'package', 'a'*64, 3003)

    def test_pinned_retry_preserves_package_and_can_resubmit(self):
        job = self.submitting()
        self.assertEqual(self.registry.recovery_plan(job['job_id'], self.port, 61000), 'retry_wait')
        new = self.registry.start_claim(job['job_id'], 'worker2', self.port, 61001)
        self.assertEqual(new['package_digest'], 'a'*64)
        pinned = self.registry.pin(job['job_id'], 'package', 'a'*64, 100, 'spool', 61002)
        self.assertEqual(pinned['state'], 'prepared')
        self.assertEqual(self.registry.begin_submit(job['job_id'], self.port, 61003)['state'], 'submitting')
        with self.guard.hold():
            authorize_commit(self.registry, job['job_id'], self.port.current, 'package', 'a'*64, 61004)

    def test_ack_after_revoke_and_no_backward_terminal_edges(self):
        job = self.submitting()
        self.registry.publish(canonical(policy(2)), 1, 'revoke', 1003)
        receipt = dict(package_id='package', package_digest='a'*64, content_fingerprint='c'*64)
        self.assertEqual(self.registry.accept_ack(job['job_id'], receipt, 1004)['state'], 'committed')
        self.assertCode('DIGEST_CONFLICT', self.registry.accept_ack, job['job_id'], dict(receipt, package_digest='b'*64), 1005)
        with self.guard.hold(), self.registry.transaction() as conn:
            for target in EDGES:
                if target != 'committed':
                    self.assertCode('FENCED', self.registry._transition, conn, self.current(job), target, 1005)

    def test_unknown_receipt_and_integrity_conflict_never_resend(self):
        job = self.submitting()
        self.port.observed = {'status': 'conflict'}
        self.assertEqual(self.registry.recovery_plan(job['job_id'], self.port, 1003), 'conflict')
        self.assertCode('DIGEST_CONFLICT', self.registry.begin_submit, job['job_id'], self.port, 1004)
        with self.guard.hold():
            self.assertCode('DIGEST_CONFLICT', authorize_commit, self.registry, job['job_id'], self.port.current, 'package', 'a'*64, 1004)

    def test_claim_none_mismatch_and_cancelled_source(self):
        job = self.job()
        with patch.object(self.port, 'claim', return_value=None):
            self.assertIsNone(self.registry.start_claim(job['job_id'], 'worker', self.port, 1000))
        self.assertEqual(self.current(job)['state'], 'reconciling')
        self.assertEqual(self.registry.recovery_plan(job['job_id'], self.port, 1001), 'retry_wait')
        self.port.source = replace(self.port.source, cancelled=True)
        self.assertCode('JOB_CANCELLED', self.registry.start_claim, job['job_id'], 'worker', self.port, 1002)

    def test_strict_persisted_job_corruption_stops_execution(self):
        job = self.job()
        with self.guard.hold(), self.registry.transaction() as conn:
            bad = dict(job, attempt=True)
            conn.execute('UPDATE jobs SET payload=? WHERE job_id=?', (canonical(bad), job['job_id']))
        with self.guard.hold():
            self.assertCode('POLICY_INVALID', self.registry.get_job, job['job_id'])

    def test_all_unlisted_state_edges_rejected(self):
        job = self.job()
        # Private state transition test uses synthetic DTO copies, no real worker.
        with self.guard.hold(), self.registry.transaction() as conn:
            for source in EDGES:
                for target in EDGES:
                    if source != target and target not in EDGES[source]:
                        fake = dict(job, state=source)
                        self.assertCode('FENCED', self.registry._transition, conn, fake, target, 1001)

    def test_time_conversion_and_wall_clock_cannot_extend_budget(self):
        from tg_testcase.controller_ports import lease_milliseconds, DeadlineBudget
        self.assertEqual(lease_milliseconds(1.2349), 1234)
        with self.assertRaises(ValueError): lease_milliseconds(True)
        with self.assertRaises(ValueError): lease_milliseconds(float('inf'))
        budget = DeadlineBudget(2000, 1000, 5.0)
        self.assertEqual(budget.remaining_ms(0, 6.0), 0)
        self.assertEqual(budget.remaining_ms(2000, 5.0), 0)


class BoundaryRegressionContract(Base):
    def test_guard_is_held_through_fake_acquisition_sqlite_commit(self):
        job = self.submitting()
        fake_db = self.root / 'fake-acquisition.sqlite3'
        with sqlite3.connect(fake_db) as conn:
            conn.execute('CREATE TABLE sealed (package_id TEXT PRIMARY KEY)')
        entered, attempted, revoked = threading.Event(), threading.Event(), threading.Event()
        def seal():
            with self.guard.hold():
                decision = authorize_commit(self.registry, job['job_id'], self.port.current, 'package', 'a'*64, 1003)
                with sqlite3.connect(fake_db) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    conn.execute('INSERT INTO sealed VALUES(?)', ('package',))
                    entered.set()
                    self.assertTrue(attempted.wait(5))
                    self.assertFalse(revoked.is_set())
                    self.guard.validate_decision(decision)
                    conn.commit()
        def revoke():
            self.assertTrue(entered.wait(5))
            attempted.set()
            self.registry.publish(canonical(policy(2)), 1, 'revoke-sqlite', 1004)
            revoked.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(seal), pool.submit(revoke)
            a.result(timeout=10); b.result(timeout=10)
        with sqlite3.connect(fake_db) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sealed').fetchone()[0], 1)
        with self.guard.hold():
            self.assertCode('POLICY_REVOKED', authorize_commit, self.registry, job['job_id'], self.port.current, 'package', 'a'*64, 1005)

    def test_expired_delivery_observation_persists_clock_high_water(self):
        self.identity.reserve(canonical(envelope(1)), self.peer, 1000)
        self.assertCode('AUTH_INVALID', self.identity.reserve, canonical(envelope(2)), self.peer, 100000)
        restarted = IdentityLedger(JobRegistry(self.registry.db_path, AuthorizationGuard(self.lock)), self.identity.peers)
        self.assertCode('AUTH_INVALID', restarted.reserve, canonical(envelope(3, issued=90000)), self.peer, 90000)
        # At exactly 5 seconds of rollback, a new fresh update remains permitted.
        restarted.reserve(canonical(envelope(4, issued=95000)), self.peer, 95000)
        # Delivery retries remain available without a new execution.
        self.assertEqual(restarted.reserve(canonical(envelope(1)), self.peer, 1000)['state'], 'reserved')

    def test_retry_with_legal_large_action_does_not_overflow_ledger_decoder(self):
        # Eight MiB text is legal; original+normalized copies exceed 16 MiB.
        action = dict(protocol_version=1, operation='append_text', task_id='task',
                      expected_version=1, payload={'text': 'x' * (8 * 1024 * 1024)})
        raw = canonical(envelope(action=action))
        first = self.identity.reserve(raw, self.peer, 1000)
        self.identity.complete(first['request_id'], {'ok': True})
        retry = self.identity.reserve(raw, self.peer, 1001)
        self.assertEqual(retry['state'], 'completed')
        self.assertEqual(retry['request']['payload'], action['payload'])

    def test_frozen_dto_and_unknown_outbox_fields(self):
        from tg_testcase.controller_ports import claim_record, SafeProperty
        with self.assertRaises(ValueError):
            SafeProperty('f:Ab', 'text')
        SafeProperty('np1-' + 'a'*64, 'text')
        job = self.submitting()
        with self.assertRaises(ValueError):
            claim_record(dict(self.port.current, attempt=True))
        event = {k: job[k] for k in ('job_id', 'task_id', 'batch_id', 'source_id', 'revision', 'attempt', 'fencing_token',
                                   'policy_epoch', 'policy_digest', 'credential_ref', 'credential_generation', 'package_id', 'package_digest')}
        event.update(event_id='e', committed_at=1003, content_fingerprint='f'*64)
        with self.assertRaises(ValueError):
            outbox_event(dict(event, token='synthetic-forbidden-field'))

    def test_failed_policy_publication_keeps_all_metadata_and_audit_atomic(self):
        with self.guard.hold(), self.registry.transaction() as conn:
            before = {table: conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                      for table in ('credential_history', 'policy_history', 'mutations', 'audit')}
        invalid = policy(2, 2); invalid['grants']['grant-a']['workspace_ref'] = 'wrong'
        self.assertCode('POLICY_INVALID', self.registry.publish, canonical(invalid), 1, 'bad', 1001,
                        credential_changes=[credential(2)])
        with self.guard.hold(), self.registry.transaction() as conn:
            after = {table: conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in before}
            self.assertEqual(before, after)
            self.assertEqual(self.registry.authority()[1]['notion-read-a']['generation'], 1)


class SocketPathContract(PathContract):
    def test_socket_metadata_exact_permissions_and_groups(self):
        self.fs.meta[self.paths.gateway_socket] = PathMetadata(102, 202, 0o660, 'socket', False, False, True)
        self.fs.meta[self.paths.receiver_socket] = PathMetadata(103, 203, 0o660, 'socket', False, False, True)
        self.paths.validate_sockets(self.owners, self.fs)
        for key, value in [('mode', 0o666), ('kind', 'symlink'), ('gid', 999), ('runner_writable', True)]:
            original = self.fs.meta[self.paths.receiver_socket]
            self.fs.meta[self.paths.receiver_socket] = replace(original, **{key: value})
            with self.assertRaises(ValueError):
                self.paths.validate_sockets(self.owners, self.fs)
            self.fs.meta[self.paths.receiver_socket] = original


if __name__ == '__main__':
    unittest.main()
