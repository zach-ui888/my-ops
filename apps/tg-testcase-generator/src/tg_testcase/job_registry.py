"""Offline protected-registry model, separate SQLite authority and claim port.

All public mutators take authorization.guard before the registry transaction.
No production acquisition adapter or network/socket lifecycle is installed here.
"""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4
from .contract_types import canonical, digest, identifier, integer, sha, strict_json, require
from .credential_registry import CredentialRegistryMixin
from .controller_ports import SourceSnapshot, outbox_event, claim_record, DEFAULT_CLAIM_LEASE_MS
from .job_contract import validate_job
from .internal_errors import fail
from .registry_migrations import SQL, VERSION
from .telegram_identity import MAX_REPLAY_BYTES

TERMINAL = {'committed', 'cancelled', 'revoked', 'expired', 'failed'}
_STOP = {'cancelled', 'revoked', 'expired', 'failed'}
EDGES = {
    'registered': {'claim_pending'} | _STOP,
    'claim_pending': {'claimed', 'reconciling'} | _STOP,
    'claimed': {'fetching', 'retry_wait', 'reconciling'} | _STOP,
    'fetching': {'prepared', 'retry_wait', 'reconciling'} | _STOP,
    'retry_wait': {'claim_pending', 'reconciling'} | _STOP,
    'prepared': {'submitting', 'reconciling'} | _STOP,
    'submitting': {'committed', 'reconciling'},
    'reconciling': {'committed', 'submitting', 'retry_wait'} | _STOP,
    **{s: set() for s in TERMINAL},
}
SEMANTIC = ('principal task_id batch_id source_id revision workspace_ref grant_ref root_type '
            'canonical_root_id policy_epoch policy_digest credential_ref credential_generation').split()


class JobRegistry(CredentialRegistryMixin):
    def __init__(self, db_path, guard):
        db_path = Path(db_path)
        require(db_path.is_absolute() and db_path.name == 'registry.sqlite3')
        self.db_path, self.guard = db_path, guard
        self._local = threading.local()
        with guard.hold(), self.transaction() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, VERSION):
                fail('POLICY_INVALID')
            # execute statements individually: executescript would commit implicitly.
            for statement in SQL.split(';'):
                if statement.strip():
                    conn.execute(statement)
            conn.execute('PRAGMA user_version=1')

    @contextmanager
    def transaction(self):
        self.guard.require_held()
        current = getattr(self._local, 'connection', None)
        if current is not None:
            yield current
            return
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=FULL')
        self._local.connection = conn
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                yield conn
        finally:
            self._local.connection = None
            conn.close()

    def get_job(self, job_id):
        identifier(job_id)
        self.guard.require_held()
        with self.transaction() as conn:
            row = conn.execute('SELECT payload,semantic_key,batch_id,source_id,state,package_id FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not row:
                fail('FENCED')
            try:
                job = validate_job(strict_json(row[0]))
                require(job['job_id'] == job_id)
                require(tuple(job[k] for k in ('semantic_key', 'batch_id', 'source_id', 'state', 'package_id')) == row[1:])
                require(job['semantic_key'] == digest(['job-v1'] + [job[k] for k in SEMANTIC] + ['acquire']))
                return job
            except (ValueError, TypeError, KeyError):
                fail('POLICY_INVALID')

    @staticmethod
    def _save(conn, job):
        validate_job(job)
        conn.execute('UPDATE jobs SET state=?,package_id=?,payload=? WHERE job_id=?',
                     (job['state'], job['package_id'], canonical(job), job['job_id']))

    def register(self, request_id, task_id, batch_id, source_id, port, now_ms):
        for v in (request_id, task_id, batch_id, source_id):
            identifier(v)
        integer(now_ms); integer(now_ms + 900000)
        with self.guard.hold():
            # No registry write transaction is held across acquisition port calls.
            source = port.inspect_source(task_id, batch_id, source_id)
            require(type(source) is SourceSnapshot)
            require((source.task_id, source.batch_id, source.source_id) == (task_id, batch_id, source_id))
            with self.transaction() as conn:
                row = conn.execute('SELECT payload FROM replay WHERE request_id=?', (request_id,)).fetchone()
                if not row:
                    fail('AUTH_INVALID')
                reservation = strict_json(row[0], limit=MAX_REPLAY_BYTES, depth=32)
                # Jobs originate from an accepted controller action. An identity
                # reservation alone does not prove Application owner/version CAS.
                if reservation['state'] != 'completed':
                    fail('AUTH_INVALID')
                req = reservation['request']
                if req.get('task_id') != task_id or req['actor']['user_id'] != source.owner:
                    fail('OWNER_MISMATCH')
                version = integer(req['expected_version'], 1)
                actor = 'telegram:' + source.owner
                policy, _ = self.authority()
                binding = policy.resolve(actor, 'acquire', source.root_id)
                j = asdict(binding)
                j.update(task_id=task_id, batch_id=batch_id, source_id=source_id,
                         revision=source.revision, canonical_root_id=source.root_id)
                semantic_key = digest(['job-v1'] + [j[k] for k in SEMANTIC] + ['acquire'])
                mapped = conn.execute('SELECT job_id,intent_digest FROM request_jobs WHERE request_id=?', (request_id,)).fetchone()
                existing = conn.execute('SELECT job_id FROM jobs WHERE semantic_key=?', (semantic_key,)).fetchone()
                if mapped and (not existing or mapped != (existing[0], reservation['action_digest'])):
                    fail('REQUEST_CONFLICT')
                if existing:
                    conn.execute('INSERT OR IGNORE INTO request_jobs VALUES(?,?,?)', (request_id, existing[0], reservation['action_digest']))
                    return self.get_job(existing[0])
                if source.cancelled:
                    fail('JOB_CANCELLED')
                if not source.finalized or source.sealed:
                    fail('FENCED')
                if conn.execute("SELECT 1 FROM jobs WHERE batch_id=? AND source_id=? AND state NOT IN ('committed','cancelled','revoked','expired','failed')", (batch_id, source_id)).fetchone():
                    fail('FENCED')
                j.update(job_id=uuid4().hex, request_id=request_id, intent_digest=reservation['action_digest'],
                         semantic_key=semantic_key, task_expected_version=version, state='registered',
                         attempt=0, fencing_token=0, worker_id=None, created_at=now_ms, updated_at=now_ms,
                         deadline=now_ms + 900000, lease_until=None, retry_at=None, cancel_state='none', cancel_at=None,
                         package_id=None, package_digest=None, package_length=None, spool_state='none', spool_ref=None,
                         receipt_state='unknown', receipt=None, last_error=None, failure_count=0)
                conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', (j['job_id'], semantic_key, batch_id, source_id, 'registered', None, canonical(j)))
                conn.execute('INSERT INTO request_jobs VALUES(?,?,?)', (request_id, j['job_id'], reservation['action_digest']))
                return j

    def _transition(self, conn, job, target, now_ms, *, no_commit=False, receipt=None):
        integer(now_ms)
        if target == job['state']:
            return
        if target not in EDGES[job['state']]:
            fail('FENCED')
        if job['state'] == 'reconciling' and target in _STOP | {'retry_wait'} and not no_commit:
            fail('RECEIPT_PENDING')
        if target == 'committed':
            if receipt is None:
                fail('RECEIPT_PENDING')
            self._validate_receipt(job, receipt)
            job.update(receipt_state='committed', receipt=receipt)
        job.update(state=target, updated_at=now_ms)
        self._save(conn, job)

    @staticmethod
    def _validate_receipt(job, receipt):
        from .contract_types import fields
        fields(receipt, 'package_id package_digest content_fingerprint')
        identifier(receipt['package_id']); sha(receipt['package_digest']); sha(receipt['content_fingerprint'])
        if any(receipt[k] != job[k] for k in ('package_id', 'package_digest')):
            fail('DIGEST_CONFLICT')

    def start_claim(self, job_id, worker_id, port, now_ms):
        from .authorization import check_job_authorization
        identifier(worker_id); integer(now_ms)
        with self.guard.hold():
            job = self.get_job(job_id)
            check_job_authorization(self, job, now_ms)
            if job['state'] not in {'registered', 'retry_wait'}:
                fail('FENCED')
            if job['retry_at'] is not None and now_ms < job['retry_at']:
                fail('FENCED')
            source = port.inspect_source(job['task_id'], job['batch_id'], job['source_id'])
            self._check_source(job, source)
            if job['state'] == 'retry_wait':
                old = port.lookup_claim(job['batch_id'], job['source_id'])
                if old and old['lease_until'] > now_ms:
                    fail('LEASE_LOST')
            with self.transaction() as conn:
                if job['attempt']:
                    self._attempt_state(conn, job, 'lost', now_ms)
                job['worker_id'] = worker_id
                self._transition(conn, job, 'claim_pending', now_ms)
            # Intent committed before acquisition.claim. Crash here is recoverable.
            claim = port.claim(source, worker_id, min(now_ms + DEFAULT_CLAIM_LEASE_MS, job['deadline']))
            if claim is None:
                with self.transaction() as conn:
                    self._transition(conn, job, 'reconciling', now_ms)
                return None
            self._bind_claim(job, claim, now_ms)
            port.transition(claim, 'fetching')
            with self.transaction() as conn:
                self._transition(conn, job, 'fetching', now_ms)
                self._attempt_state(conn, job, 'fetching', now_ms)
            return self.get_job(job_id)

    @staticmethod
    def _check_source(job, source):
        require(type(source) is SourceSnapshot)
        if 'telegram:' + source.owner != job['principal']:
            fail('OWNER_MISMATCH')
        if source.cancelled:
            fail('JOB_CANCELLED')
        if (not source.finalized or source.sealed or source.revision != job['revision'] or
                source.root_id != job['root_id'] or any(getattr(source, k) != job[k] for k in ('task_id', 'batch_id', 'source_id'))):
            fail('FENCED')

    def _bind_claim(self, job, claim, now_ms):
        claim = claim_record(claim)
        for k in ('batch_id', 'source_id', 'revision', 'worker_id'):
            if claim.get(k) != job[k]:
                fail('FENCED')
        for k in ('attempt', 'fencing_token'):
            integer(claim[k], 1)
            if claim[k] <= job[k]:
                fail('FENCED')
        integer(claim['lease_until'])
        if not now_ms < claim['lease_until'] <= job['deadline'] or claim['state'] not in {'claimed', 'fetching'}:
            fail('LEASE_LOST')
        with self.transaction() as conn:
            for k in ('attempt', 'fencing_token', 'lease_until'):
                job[k] = claim[k]
            self._transition(conn, job, 'claimed', now_ms)
            attempt = {k: job[k] for k in ('fencing_token', 'worker_id', 'lease_until')}
            attempt.update(state='claimed', started_at=now_ms, ended_at=None)
            conn.execute('INSERT INTO job_attempts VALUES(?,?,?)', (job['job_id'], job['attempt'], canonical(attempt)))

    def _attempt_state(self, conn, job, state, now_ms):
        row = conn.execute('SELECT payload FROM job_attempts WHERE job_id=? AND attempt=?', (job['job_id'], job['attempt'])).fetchone()
        if row:
            attempt = strict_json(row[0])
            attempt.update(state=state, lease_until=job['lease_until'])
            if state in {'sealed', 'abandoned', 'lost'}:
                attempt['ended_at'] = now_ms
            conn.execute('UPDATE job_attempts SET payload=? WHERE job_id=? AND attempt=?', (canonical(attempt), job['job_id'], job['attempt']))

    def recover_claim(self, job_id, port, now_ms):
        from .authorization import check_job_authorization
        with self.guard.hold():
            job = self.get_job(job_id)
            check_job_authorization(self, job, now_ms)
            if job['state'] not in {'claim_pending', 'claimed'}:
                fail('FENCED')
            self._check_source(job, port.inspect_source(job['task_id'], job['batch_id'], job['source_id']))
            claim = port.lookup_claim(job['batch_id'], job['source_id'])
            if (not claim or any(claim.get(k) != job[k] for k in ('batch_id', 'source_id', 'revision', 'worker_id'))
                    or claim['lease_until'] <= now_ms or claim['state'] not in {'claimed', 'fetching'}):
                with self.transaction() as conn:
                    self._transition(conn, job, 'reconciling', now_ms)
                return None
            if job['state'] == 'claim_pending':
                self._bind_claim(job, claim, now_ms)
            elif any(claim[k] != job[k] for k in ('attempt', 'fencing_token')):
                fail('FENCED')
            if claim['state'] == 'claimed':
                port.transition(claim, 'fetching')
            with self.transaction() as conn:
                self._transition(conn, job, 'fetching', now_ms)
                self._attempt_state(conn, job, 'fetching', now_ms)
            return job

    def heartbeat(self, job_id, port, now_ms):
        from .authorization import check_job_authorization
        with self.guard.hold():
            job = self.get_job(job_id)
            check_job_authorization(self, job, now_ms)
            if job['state'] not in {'claimed', 'fetching', 'prepared', 'submitting'}:
                fail('FENCED')
            claim = port.lookup_claim(job['batch_id'], job['source_id'])
            self._check_claim(job, claim, now_ms)
            if claim['state'] not in {'claimed', 'fetching'}:
                fail('LEASE_LOST')
            updated = port.heartbeat(claim, min(now_ms + DEFAULT_CLAIM_LEASE_MS, job['deadline']))
            self._check_claim(job, updated, now_ms)
            with self.transaction() as conn:
                job['lease_until'] = updated['lease_until']
                self._save(conn, job)
                self._attempt_state(conn, job, updated['state'], now_ms)
            return job

    def schedule_retry(self, job_id, error, retry_at, port, now_ms):
        from .authorization import check_job_authorization
        from .internal_errors import ErrorCode, ERROR_INFO
        code = ErrorCode(error)
        integer(retry_at); integer(now_ms)
        if not ERROR_INFO[code][1]:
            fail(code)
        with self.guard.hold():
            job = self.get_job(job_id)
            check_job_authorization(self, job, now_ms)
            if job['state'] not in {'claimed', 'fetching'}:
                fail('RECEIPT_PENDING')
            if not now_ms <= retry_at < job['deadline']:
                fail('JOB_TIMEOUT')
            claim = port.lookup_claim(job['batch_id'], job['source_id'])
            self._check_claim(job, claim, now_ms)
            if claim['state'] == 'fetching':
                port.transition(claim, 'retry_wait', retry_at)
                job['lease_until'] = retry_at
            elif claim['state'] != 'claimed':
                fail('LEASE_LOST')
            # A claimed acquisition cannot transition directly to retry_wait in
            # the old API. Wait for its lease instead; never steal it.
            job.update(retry_at=max(retry_at, job['lease_until']), last_error=code.value,
                       failure_count=integer(job['failure_count'] + 1))
            with self.transaction() as conn:
                self._transition(conn, job, 'retry_wait', now_ms)
                self._attempt_state(conn, job, 'retry_wait', now_ms)
            return job

    @staticmethod
    def _check_claim(job, claim, now_ms):
        if not claim:
            fail('LEASE_LOST')
        claim_record(claim)
        if any(claim.get(k) != job[k] for k in ('batch_id', 'source_id', 'revision', 'worker_id', 'attempt', 'fencing_token')):
            fail('FENCED')
        if not now_ms < claim['lease_until'] <= job['deadline']:
            fail('LEASE_LOST')

    def pin(self, job_id, package_id, package_digest, package_length, spool_ref, now_ms):
        from .authorization import check_job_authorization
        identifier(package_id); sha(package_digest); integer(package_length, 0, 16777216); identifier(spool_ref)
        with self.guard.hold(), self.transaction() as conn:
            job = self.get_job(job_id)
            check_job_authorization(self, job, now_ms)
            values = dict(package_id=package_id, package_digest=package_digest, package_length=package_length, spool_ref=spool_ref)
            if job['package_id'] is not None:
                if any(job[k] != v for k, v in values.items()):
                    fail('DIGEST_CONFLICT')
                if job['state'] == 'fetching':
                    self._transition(conn, job, 'prepared', now_ms)
                return job
            if job['state'] != 'fetching':
                fail('FENCED')
            if conn.execute('SELECT 1 FROM jobs WHERE package_id=?', (package_id,)).fetchone():
                fail('DIGEST_CONFLICT')
            job.update(values, spool_state='pinned')
            self._transition(conn, job, 'prepared', now_ms)
            return job

    def begin_submit(self, job_id, port, now_ms):
        from .authorization import check_job_authorization
        with self.guard.hold():
            job = self.get_job(job_id)
            check_job_authorization(self, job, now_ms)
            claim = port.lookup_claim(job['batch_id'], job['source_id'])
            self._check_claim(job, claim, now_ms)
            if claim['state'] not in {'fetching', 'sealed'}:
                fail('LEASE_LOST')
            if not job['package_id'] or job['spool_state'] not in {'pinned', 'retained'}:
                fail('DIGEST_CONFLICT')
            with self.transaction() as conn:
                self._transition(conn, job, 'submitting', now_ms)
            return job

    def uncertain(self, job_id, now_ms):
        with self.guard.hold(), self.transaction() as conn:
            job = self.get_job(job_id)
            self._transition(conn, job, 'reconciling', now_ms)
            return job

    def accept_ack(self, job_id, receipt, now_ms):
        """Protected sender callback after validating its bounded wire ACK.

        Historical commit confirmation does not grant a new acquire and is not
        invalidated by policy changes that happened after acquisition COMMIT.
        """
        with self.guard.hold(), self.transaction() as conn:
            job = self.get_job(job_id)
            if job['state'] == 'committed':
                if job['receipt'] != receipt:
                    fail('DIGEST_CONFLICT')
                return job
            self._transition(conn, job, 'committed', now_ms, receipt=receipt)
            self._attempt_state(conn, job, 'sealed', now_ms)
            return job

    def stop(self, job_id, reason, now_ms):
        """Block immediately; possibly submitted jobs must reconcile before terminal."""
        require(reason in {'JOB_CANCELLED', 'POLICY_REVOKED', 'JOB_TIMEOUT', 'INTERNAL_FAILURE'})
        with self.guard.hold(), self.transaction() as conn:
            job = self.get_job(job_id)
            if job['state'] in TERMINAL:
                return job
            if reason == 'JOB_CANCELLED':
                job.update(cancel_state='requested', cancel_at=now_ms)
            job['last_error'] = reason
            target = {'JOB_CANCELLED': 'cancelled', 'POLICY_REVOKED': 'revoked',
                      'JOB_TIMEOUT': 'expired', 'INTERNAL_FAILURE': 'failed'}[reason]
            if job['state'] in {'submitting', 'reconciling'}:
                target = 'reconciling'
            self._transition(conn, job, target, now_ms)
            self._save(conn, job)
            return job

    def consume_outbox(self, event, now_ms):
        """Protected acquisition-port event, never decoded from producer/HELLO."""
        event = outbox_event(event)
        with self.guard.hold(), self.transaction() as conn:
            ed = digest(event)
            old = conn.execute('SELECT event_digest FROM consumed_events WHERE event_id=?', (event['event_id'],)).fetchone()
            if old:
                if old[0] != ed:
                    fail('DIGEST_CONFLICT')
            job = self.get_job(event['job_id'])
            for key in ('task_id', 'batch_id', 'source_id', 'revision', 'attempt', 'fencing_token',
                        'policy_epoch', 'policy_digest', 'credential_ref', 'credential_generation', 'package_id', 'package_digest'):
                if event[key] != job[key]:
                    fail('DIGEST_CONFLICT')
            receipt = {k: event[k] for k in ('package_id', 'package_digest', 'content_fingerprint')}
            if job['state'] == 'committed':
                if job['receipt'] != receipt:
                    fail('DIGEST_CONFLICT')
            else:
                self._transition(conn, job, 'committed', now_ms, receipt=receipt)
            if old:
                return job
            self._attempt_state(conn, job, 'sealed', event['committed_at'])
            if not old:
                conn.execute('INSERT INTO consumed_events VALUES(?,?)', (event['event_id'], ed))
            return job

    def recovery_plan(self, job_id, port, now_ms, *, spool_present=True):
        """Offline planner using authoritative fake receipt/claim; never fetches.

        Step 2.2 supplies the actual guarded receipt query. A negative result is
        only acted on while guard is held and new sends are blocked by state.
        """
        from .authorization import check_job_authorization
        from .internal_errors import ContractError
        with self.guard.hold():
            job = self.get_job(job_id)
            if job['state'] in TERMINAL:
                return job['state']
            if job['state'] in {'claim_pending', 'claimed'}:
                try:
                    check_job_authorization(self, job, now_ms)
                    return 'recover_claim'
                except ContractError as exc:
                    reason = ('JOB_CANCELLED' if job['cancel_state'] != 'none' else
                              'POLICY_REVOKED' if exc.code.value in {'CREDENTIAL_STALE', 'POLICY_REVOKED', 'SCOPE_DENIED'} else
                              'JOB_TIMEOUT' if now_ms >= job['deadline'] else 'INTERNAL_FAILURE')
                    return self.stop(job_id, reason, now_ms)['state']
            if job['state'] in {'registered', 'retry_wait'}:
                try:
                    check_job_authorization(self, job, now_ms)
                except ContractError as exc:
                    reason = ('JOB_CANCELLED' if job['cancel_state'] != 'none' else
                              'POLICY_REVOKED' if exc.code.value in {'CREDENTIAL_STALE', 'POLICY_REVOKED', 'SCOPE_DENIED'} else
                              'JOB_TIMEOUT' if now_ms >= job['deadline'] else 'INTERNAL_FAILURE')
                    return self.stop(job_id, reason, now_ms)['state']
                if job['state'] == 'registered':
                    return 'claim'
                return 'wait' if job['retry_at'] is not None and now_ms < job['retry_at'] else 'claim'
            if job['state'] not in {'submitting', 'reconciling'}:
                with self.transaction() as conn:
                    self._transition(conn, job, 'reconciling', now_ms)
            elif job['state'] == 'submitting':
                with self.transaction() as conn:
                    self._transition(conn, job, 'reconciling', now_ms)
            observed = port.receipt(job)
            if observed['status'] == 'pending':
                return 'pending'
            if observed['status'] == 'conflict':
                with self.transaction() as conn:
                    job.update(receipt_state='conflict', last_error='DIGEST_CONFLICT')
                    self._save(conn, job)
                return 'conflict'
            if observed['status'] == 'committed':
                with self.transaction() as conn:
                    self._transition(conn, job, 'committed', now_ms, receipt=observed['receipt'])
                    self._attempt_state(conn, job, 'sealed', now_ms)
                return 'committed'
            require(observed['status'] == 'not_committed')
            error = None
            try:
                check_job_authorization(self, job, now_ms)
            except ContractError as exc:
                error = exc.code.value
            target = ('cancelled' if job['cancel_state'] != 'none' else
                      'revoked' if error in {'POLICY_REVOKED', 'CREDENTIAL_STALE', 'SCOPE_DENIED'} else
                      'expired' if now_ms >= job['deadline'] else
                      'failed' if error or (job['package_id'] and not spool_present) or job['last_error'] == 'INTERNAL_FAILURE' else None)
            with self.transaction() as conn:
                job['receipt_state'] = 'not_committed'
                if target:
                    self._transition(conn, job, target, now_ms, no_commit=True)
                    self._attempt_state(conn, job, 'abandoned', now_ms)
                    return target
                self._save(conn, job)
            if job['package_id']:
                claim = port.lookup_claim(job['batch_id'], job['source_id'])
                try:
                    self._check_claim(job, claim, now_ms)
                    require(claim['state'] in {'fetching', 'sealed'})
                    return 'resend_same_spool'
                except (ContractError, ValueError):
                    pass
            with self.transaction() as conn:
                self._transition(conn, job, 'retry_wait', now_ms, no_commit=True)
                job['retry_at'] = now_ms
                self._save(conn, job)
            return 'retry_wait'
