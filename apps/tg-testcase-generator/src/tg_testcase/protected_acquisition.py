"""Final acquisition commit authorization. No socket or reconciliation service."""
from contextlib import contextmanager
import hashlib
import json
import time

from .acquisition import OfflineAcquisition
from .acquisition_adapter import read_claim, read_source
from .authorization import authorize_commit, check_job_authorization
from .contract_types import digest, identifier
from .controller_ports import DeadlineBudget, lease_milliseconds, outbox_event
from .internal_errors import fail
from .notion_package import encode, load_package, MAX_BATCH_BYTES
from .registry_reader import RegistryReader
from .streaming import Staging, CHUNK_SIZE


class ProtectedReceiverAcquisition(OfflineAcquisition):
    def __init__(self, store, registry_reader, job_id, *, staging_root,
                 clock=time.time, monotonic=time.monotonic):
        if not store.protected_acquisition or type(registry_reader) is not RegistryReader or staging_root is None:
            fail('AUTH_INVALID')
        identifier(job_id)
        super().__init__(store, clock)
        self.reader, self.job_id = registry_reader, job_id
        self.staging_root, self.monotonic = staging_root, monotonic
        # Anchor before input/queue waits, never rebuild a longer budget at commit.
        with self.reader.guard.hold():
            job = self.reader.get_job(job_id)
        self.budget = DeadlineBudget(job['deadline'], self.now_ms(), monotonic())

    def now_ms(self):
        return lease_milliseconds(self.clock())

    def receiver_binding(self):
        """Trusted early lookup; its result never replaces final authorization."""
        with self.reader.guard.hold():
            snapshot = self.reader.snapshot(self.job_id)
            job = snapshot.get_job(self.job_id)
            check_job_authorization(snapshot, job, self.now_ms())
            with self.store._locked() as conn:
                claim = read_claim(conn, job['batch_id'], job['source_id'])
            if claim is None:
                fail('LEASE_LOST')
            return job, claim

    def seal_stream(self, claim, package_stream, package_length, artifacts, *, staging=None):
        if staging is not None:
            fail('AUTH_INVALID')
        return super().seal_stream(claim, package_stream, package_length, artifacts,
                                   staging=Staging(self.staging_root))

    @contextmanager
    def _commit_protection(self, claim, p, staged):
        # Fix validated package bytes and private local handles before the guard.
        p = load_package(encode(p).encode())
        if set(staged.files) != {a['id'] for a in p['artifacts']}:
            fail('PACKAGE_INVALID')
        for artifact in p['artifacts']:
            f = staged.files[artifact['id']]
            f.flush(); f.seek(0)
            size, h = 0, hashlib.sha256()
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk); h.update(chunk)
                if size > artifact['size']:
                    fail('PACKAGE_INVALID')
            if size != artifact['size'] or h.hexdigest() != artifact['sha256']:
                fail('PACKAGE_INVALID')
            f.seek(0)
        with self.reader.guard.hold():
            snapshot = self.reader.snapshot(self.job_id)
            yield _CommitBoundary(self, snapshot, claim, p)


class _CommitBoundary:
    def __init__(self, importer, snapshot, supplied_claim, package):
        self.importer, self.snapshot = importer, snapshot
        self.job = snapshot.get_job(importer.job_id)
        self.supplied_claim, self.p = dict(supplied_claim), package
        self.canonical = encode(package)
        self.package_digest = hashlib.sha256(self.canonical.encode()).hexdigest()
        self.decision = None

    def _state(self, conn, expected):
        j, p = self.job, self.p
        source = read_source(self.importer.store, conn, j['task_id'], j['batch_id'], j['source_id'])
        if 'telegram:' + source.owner != j['principal']:
            fail('OWNER_MISMATCH')
        if source.cancelled:
            fail('JOB_CANCELLED')
        if (not source.finalized or source.revision != j['revision'] or source.root_id != j['root_id'] or
                any(p[k] != j[k] for k in ('task_id', 'batch_id', 'source_id', 'revision'))):
            fail('FENCED')
        if p['root'] != {'type': j['root_type'], 'id': j['canonical_root_id']}:
            fail('ROOT_TYPE_MISMATCH')
        actual = read_claim(conn, j['batch_id'], j['source_id'])
        if not actual:
            fail('LEASE_LOST')
        for k in ('batch_id', 'source_id', 'revision', 'worker_id', 'attempt', 'fencing_token'):
            if actual[k] != j[k] or self.supplied_claim.get(k) != actual[k]:
                fail('FENCED')
        if actual['state'] != expected:
            fail('FENCED')
        return actual

    def _time(self, actual):
        now = self.importer.now_ms()
        if self.importer.budget.remaining_ms(now, self.importer.monotonic()) <= 0:
            fail('JOB_TIMEOUT')
        if now >= min(actual['lease_until'], self.job['lease_until'] or 0):
            fail('LEASE_LOST')
        return now

    def _rows(self, conn):
        j = self.job
        return conn.execute('SELECT id,task_id,batch_id,source_id,revision,package_digest,content_fingerprint FROM source_packages WHERE id=? OR (batch_id=? AND source_id=?) OR (task_id=? AND source_id=? AND revision=?)',
                            (j['package_id'], j['batch_id'], j['source_id'], j['task_id'], j['source_id'], j['revision'])).fetchall()

    def _expected_row(self):
        j = self.job
        return tuple(j[k] for k in ('package_id', 'task_id', 'batch_id', 'source_id', 'revision', 'package_digest')) + (self.p['content_fingerprint'],)

    def _event(self, now):
        j = self.job
        e = {k: j[k] for k in ('job_id', 'task_id', 'batch_id', 'source_id', 'revision', 'attempt', 'fencing_token', 'policy_epoch', 'policy_digest', 'credential_ref', 'credential_generation', 'package_id', 'package_digest')}
        e.update(event_id='acq1-' + digest(['acquisition-commit-v1'] + [j[k] for k in ('job_id', 'task_id', 'batch_id', 'source_id', 'revision', 'package_id', 'package_digest')]),
                 content_fingerprint=self.p['content_fingerprint'], committed_at=now)
        return outbox_event(e)

    def before(self, conn):
        j, p = self.job, self.p
        if j['package_length'] != len(encode(p).encode()) or j['spool_state'] not in {'pinned', 'retained'}:
            fail('DIGEST_CONFLICT')
        rows = self._rows(conn)
        actual = self._state(conn, 'sealed' if rows else 'fetching')
        self.decision = authorize_commit(self.snapshot, j['job_id'], actual, p['package_id'], self.package_digest, self.importer.now_ms())
        self._time(actual)
        if rows:
            if rows != [self._expected_row()]:
                fail('DIGEST_CONFLICT')
            cursor = conn.execute('SELECT * FROM acquisition_commit_outbox WHERE job_id=?', (j['job_id'],))
            row = cursor.fetchone()
            if row is None:
                fail('RECEIPT_PENDING')
            event = outbox_event(dict(zip([d[0] for d in cursor.description], row)))
            if event != self._event(event['committed_at']):
                fail('DIGEST_CONFLICT')
            self.snapshot.guard.validate_decision(self.decision)
            self._time(actual)
            return {k: event[k] for k in ('package_id', 'package_digest', 'content_fingerprint')}
        if j['state'] not in {'submitting', 'reconciling'}:
            fail('FENCED')
        return None

    def finish(self, conn):
        # All BLOB and manifest writes already completed. Time is checked again
        # after outbox/invariants, immediately before the caller's SQLite COMMIT.
        event = self._event(self.importer.now_ms())
        conn.execute('INSERT INTO acquisition_commit_outbox (' + ','.join(event) + ') VALUES (' + ','.join('?' for _ in event) + ')', tuple(event.values()))
        actual = self._state(conn, 'sealed')
        if self._rows(conn) != [self._expected_row()]:
            fail('DIGEST_CONFLICT')
        package = conn.execute('SELECT payload,byte_length,outcome FROM source_packages WHERE id=?', (self.p['package_id'],)).fetchone()
        if package != (self.canonical, len(self.canonical.encode()) + sum(a['size'] for a in self.p['artifacts']), self.p['outcome']):
            fail('DIGEST_CONFLICT')
        cursor = conn.execute('SELECT * FROM acquisition_commit_outbox WHERE event_id=?', (event['event_id'],))
        stored = cursor.fetchone()
        if stored is None or dict(zip([d[0] for d in cursor.description], stored)) != event:
            fail('INTERNAL_FAILURE')
        artifacts = conn.execute('SELECT artifact_id,kind,path,sha256,size,length(body) FROM source_package_artifacts WHERE package_id=? ORDER BY artifact_id', (self.p['package_id'],)).fetchall()
        expected = sorted((a['id'], a['kind'], a['path'], a['sha256'], a['size'], a['size']) for a in self.p['artifacts'])
        if artifacts != expected:
            fail('PACKAGE_INVALID')
        used = conn.execute('SELECT coalesce(sum(byte_length),0) FROM source_packages WHERE batch_id=?', (self.job['batch_id'],)).fetchone()[0]
        remaining = conn.execute("SELECT count(*) FROM source_inputs i WHERE i.batch_id=? AND i.kind='notion' AND NOT EXISTS (SELECT 1 FROM source_packages p WHERE p.batch_id=i.batch_id AND p.source_id=i.source_id)", (self.job['batch_id'],)).fetchone()[0]
        if used + remaining * 4096 > MAX_BATCH_BYTES:
            fail('RESOURCE_LIMIT')
        batch = conn.execute('SELECT acquisition_status,input_manifest_digest FROM collection_batches WHERE id=?', (self.job['batch_id'],)).fetchone()
        manifest = conn.execute('SELECT digest,payload FROM batch_input_manifests WHERE batch_id=?', (self.job['batch_id'],)).fetchone()
        if batch[1] is not None and (batch[0] != 'sealed' or not manifest or manifest[0] != batch[1]):
            fail('INTERNAL_FAILURE')
        ids = json.loads(conn.execute('SELECT source_ids FROM collection_batches WHERE id=?', (self.job['batch_id'],)).fetchone()[0])
        entries = []
        for sid in ids:
            source = conn.execute('SELECT revision,kind,sha256 FROM source_inputs WHERE batch_id=? AND source_id=?', (self.job['batch_id'], sid)).fetchone()
            if source is None:
                fail('INTERNAL_FAILURE')
            fingerprint = source[2]
            if source[1] == 'notion':
                row = conn.execute('SELECT content_fingerprint FROM source_packages WHERE batch_id=? AND source_id=? AND revision=?', (self.job['batch_id'], sid, source[0])).fetchone()
                fingerprint = row[0] if row else None
            if fingerprint is None:
                break
            entries.append(dict(source_id=sid, revision=source[0], input_fingerprint=fingerprint))
        if len(entries) == len(ids):
            expected_manifest = encode(dict(task_id=self.job['task_id'], batch_id=self.job['batch_id'], sources=entries))
            if not manifest or manifest != (digest(entries), expected_manifest) or batch != ('sealed', digest(entries)):
                fail('INTERNAL_FAILURE')
        elif manifest is not None or batch[1] is not None:
            fail('INTERNAL_FAILURE')
        self.snapshot.guard.validate_decision(self.decision)
        self._time(actual)
