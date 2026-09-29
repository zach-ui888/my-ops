"""Offline importer control plane; deliberately no network adapter or credentials.

Only internal controller code calls this API. A package cannot grant authority:
claims and source/revision bindings are obtained independently from SQLite.
"""
import hashlib
import json
import time

from .notion_package import (load_package, encode, digest, require, reference_fingerprint,
    reference_id, MAX_BATCH_BYTES, MAX_BATCH_SOURCES, MAX_ATTACHMENT)
from .processor import LeaseLost


def seal_inputs(conn, bid):
    batch = conn.execute('SELECT task_id,status,source_ids,input_manifest_digest FROM collection_batches WHERE id=?', (bid,)).fetchone()
    if not batch or batch[1] != 'finalized' or batch[3]:
        return
    ids = json.loads(batch[2])
    require(len(ids) <= MAX_BATCH_SOURCES, 'Batch source limit')
    entries = []
    for sid in ids:
        row = conn.execute('SELECT revision,kind,sha256 FROM source_inputs WHERE batch_id=? AND source_id=?', (bid, sid)).fetchone()
        if row[1] == 'notion':
            bound = conn.execute('SELECT content_fingerprint FROM source_packages WHERE batch_id=? AND source_id=? AND revision=?', (bid, sid, row[0])).fetchone()
            if not bound:
                return
            fingerprint = bound[0]
        else:
            fingerprint = row[2]
            if fingerprint is None:
                return  # legacy input is pinned by Processor before its first claim
        entries.append(dict(source_id=sid, revision=row[0], input_fingerprint=fingerprint))
    manifest = dict(task_id=batch[0], batch_id=bid, sources=entries)
    fingerprint = digest(entries)
    conn.execute('INSERT INTO batch_input_manifests VALUES(?,?,?)', (bid, fingerprint, encode(manifest)))
    conn.execute("UPDATE collection_batches SET acquisition_status='sealed',input_manifest_digest=?,inputs_sealed_at=CURRENT_TIMESTAMP WHERE id=?", (fingerprint, bid))


class OfflineAcquisition:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def claim(self, batch_id, source_id, worker_id, lease_seconds=60):
        require(type(worker_id) is str and bool(worker_id) and 0 < lease_seconds <= 3600, 'Invalid acquisition lease')
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT i.task_id,i.revision,b.status FROM source_inputs i JOIN collection_batches b ON b.id=i.batch_id WHERE i.batch_id=? AND i.source_id=? AND i.kind=\'notion\'', (batch_id, source_id)).fetchone()
            if not row:
                raise KeyError(source_id)
            if row[2] != 'finalized' or self.store._read(conn, row[0]).state == 'cancelled':
                return None
            old = conn.execute('SELECT attempt,fencing_token,lease_until,status FROM source_fetch_runs WHERE batch_id=? AND source_id=?', (batch_id, source_id)).fetchone()
            if old and (old[3] in {'sealed', 'abandoned'} or old[2] > self.clock()):
                return None
            attempt, token = (old[0]+1, old[1]+1) if old else (1, 1)
            conn.execute('INSERT INTO source_fetch_runs VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(batch_id,source_id) DO UPDATE SET worker_id=excluded.worker_id,attempt=excluded.attempt,fencing_token=excluded.fencing_token,lease_until=excluded.lease_until,status=excluded.status',
                         (batch_id, source_id, row[1], worker_id, attempt, token, self.clock()+lease_seconds, 'claimed', None))
            conn.execute("UPDATE collection_batches SET acquisition_status='acquiring' WHERE id=?", (batch_id,))
            return dict(batch_id=batch_id, source_id=source_id, revision=row[1], worker_id=worker_id, attempt=attempt, fencing_token=token)

    def _check(self, conn, claim, expected):
        row = conn.execute('SELECT revision,worker_id,attempt,fencing_token,lease_until,status FROM source_fetch_runs WHERE batch_id=? AND source_id=?', (claim['batch_id'], claim['source_id'])).fetchone()
        if not row or row[:4] != tuple(claim[k] for k in ('revision', 'worker_id', 'attempt', 'fencing_token')) or row[4] <= self.clock() or row[5] not in expected:
            raise LeaseLost('Acquisition lease lost')
        source = conn.execute('SELECT task_id,locator,revision FROM source_inputs WHERE batch_id=? AND source_id=?', (claim['batch_id'], claim['source_id'])).fetchone()
        if source[2] != claim['revision'] or self.store._read(conn, source[0]).state == 'cancelled':
            raise LeaseLost('Acquisition binding invalid')
        return source

    def transition(self, claim, target, delay=0):
        allowed = {'fetching': {'claimed'}, 'retry_wait': {'fetching'}, 'abandoned': {'claimed', 'fetching', 'retry_wait'}}
        require(target in allowed and 0 <= delay <= 3600, 'Invalid acquisition transition')
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._check(conn, claim, allowed[target])
            conn.execute('UPDATE source_fetch_runs SET status=?,lease_until=CASE WHEN ?=\'retry_wait\' THEN ? ELSE lease_until END WHERE batch_id=? AND source_id=?',
                         (target, target, self.clock()+delay, claim['batch_id'], claim['source_id']))

    def heartbeat(self, claim, lease_seconds=60):
        require(0 < lease_seconds <= 3600, 'Invalid lease')
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._check(conn, claim, {'claimed', 'fetching'})
            conn.execute('UPDATE source_fetch_runs SET lease_until=? WHERE batch_id=? AND source_id=?', (self.clock()+lease_seconds, claim['batch_id'], claim['source_id']))

    def seal(self, claim, raw, artifacts):
        """Atomically validate and import bytes. No manifest-supplied file is opened.

        `artifacts` is an exact id -> bytes map provided by the offline controller.
        Validation failures leave fetching intact; a producer may submit a small
        failed package with an explicit gap, or retry with a new attempt.
        """
        p = load_package(raw)
        require(type(artifacts) is dict and set(artifacts) == {a['id'] for a in p['artifacts']}, 'Artifact registry mismatch')
        for a in p['artifacts']:
            blob = artifacts[a['id']]
            require(type(blob) is bytes and len(blob) == a['size'] and hashlib.sha256(blob).hexdigest() == a['sha256'], 'Artifact hash/size mismatch')
        canonical = encode(p)
        package_digest = hashlib.sha256(canonical.encode()).hexdigest()
        total = len(canonical.encode()) + sum(len(b) for b in artifacts.values())
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            source = self._check(conn, claim, {'fetching'})
            require((p['task_id'], p['batch_id'], p['source_id'], p['revision']) ==
                    (source[0], claim['batch_id'], claim['source_id'], claim['revision']), 'Package binding mismatch')
            require(reference_id(p['root']['id']) == reference_id(source[1]), 'Root reference mismatch')
            used = conn.execute('SELECT coalesce(sum(byte_length),0) FROM source_packages WHERE batch_id=?', (claim['batch_id'],)).fetchone()[0]
            remaining = conn.execute("SELECT count(*) FROM source_inputs i WHERE i.batch_id=? AND i.kind='notion' AND i.source_id!=? AND NOT EXISTS (SELECT 1 FROM source_packages p WHERE p.batch_id=i.batch_id AND p.source_id=i.source_id)",
                                     (claim['batch_id'], claim['source_id'])).fetchone()[0]
            # Leave bounded space for failure receipts of still-unsealed sources.
            require(used + total + remaining * 4096 <= MAX_BATCH_BYTES, 'Batch package limit')
            conn.execute('INSERT INTO source_packages VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         (p['package_id'], p['task_id'], p['batch_id'], p['source_id'], p['revision'],
                          reference_fingerprint(source[1]), p['content_fingerprint'], package_digest, total, p['outcome'], canonical))
            for a in p['artifacts']:
                conn.execute('INSERT INTO source_package_artifacts VALUES(?,?,?,?,?,?,?)',
                             (p['package_id'], a['id'], a['kind'], a['path'], a['sha256'], a['size'], artifacts[a['id']]))
            conn.execute("UPDATE source_fetch_runs SET status='sealed',outcome=? WHERE batch_id=? AND source_id=?", (p['outcome'], claim['batch_id'], claim['source_id']))
            seal_inputs(conn, claim['batch_id'])
        return dict(package_id=p['package_id'], package_digest=package_digest, content_fingerprint=p['content_fingerprint'])

    def seal_failure(self, claim, code='acquisition_failed'):
        """Seal a small failure receipt using registry identity, never rejected bytes.

        Useful after a package exceeds validation budgets. Invalid input is not
        silently truncated, and it cannot supply the failure receipt's binding.
        """
        from uuid import uuid4
        from .notion_package import content_fingerprint, GAPS
        require(code in GAPS or code == 'acquisition_failed', 'Invalid failure code')
        with self.store._locked() as conn:
            source = self._check(conn, claim, {'fetching'})
        p = dict(schema='notion-source-package-v1', package_id=uuid4().hex,
                 task_id=source[0], batch_id=claim['batch_id'], source_id=claim['source_id'],
                 revision=claim['revision'], root=dict(type='page', id=reference_id(source[1])),
                 adapter_version='offline-failure-1', policy_version='scope-1', api_version='not-applicable',
                 outcome='failed', scope=dict(traversal_complete=False, pagination_complete=False,
                 permissions_complete=False, view_semantics_resolved=False), nodes=[], artifacts=[],
                 gaps=[] if code == 'acquisition_failed' else [dict(code=code)])
        p['content_fingerprint'] = content_fingerprint(p)
        return self.seal(claim, encode(p).encode(), {})

    def parse(self, source):
        from .notion_package import convert
        with self.store._locked() as conn:
            row = conn.execute('SELECT id,payload,package_digest,content_fingerprint FROM source_packages WHERE batch_id=? AND source_id=? AND revision=? AND task_id=?',
                               tuple(source[k] for k in ('batch_id', 'source_id', 'revision', 'task_id'))).fetchone()
            require(row is not None, 'Unsealed source')
            raw = row[1].encode()
            require(hashlib.sha256(raw).hexdigest() == row[2], 'Package digest mismatch')
            p = load_package(raw)
            require(p['package_id'] == row[0] and p['content_fingerprint'] == row[3], 'Stored binding mismatch')
            def read(a):
                # Size checked in SQL before materializing the blob, then hash checked.
                found = conn.execute('SELECT kind,path,sha256,size,body FROM source_package_artifacts WHERE package_id=? AND artifact_id=? AND length(body)<=?', (row[0], a['id'], MAX_ATTACHMENT)).fetchone()
                require(found is not None and found[:4] == (a['kind'], a['path'], a['sha256'], a['size']), 'Artifact registry mismatch')
                require(len(found[4]) == a['size'] and hashlib.sha256(found[4]).hexdigest() == a['sha256'], 'Artifact verification failed')
                return found[4]
            return convert(source, p, read)
