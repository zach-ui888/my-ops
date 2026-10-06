"""Offline leased processor. SQLite holds both staging and published envelopes.

No network, subprocess, environment access, or user-supplied filesystem paths.
The worker API is trusted internal infrastructure, not an RPC boundary.
"""
import hashlib
import json
import time
from threading import Event, Thread
from pathlib import PurePosixPath

from .content import parse_content, CONTENT_SCHEMA_VERSION, PARSER_VERSION, POLICY_VERSION
from .models import State
from .sources import MAX_SOURCE_BYTES
from .storage import bounded_read

TERMINAL = {'completed', 'partial', 'failed'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class LeaseLost(ValueError):
    pass


class Processor:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def claim(self, batch_id, worker_id, lease_seconds=60):
        if not worker_id or not 0 < lease_seconds <= 3600:
            raise ValueError('Invalid worker or lease')
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            batch = conn.execute('SELECT task_id,status,processing_status FROM collection_batches WHERE id=?', (batch_id,)).fetchone()
            if not batch:
                raise KeyError(batch_id)
            task = self.store._read(conn, batch[0])
            if batch[1] != 'finalized' or task.state == State.CANCELLED or batch[2] in TERMINAL:
                return None
            from .acquisition import seal_inputs
            # Legacy local inputs without recorded hashes are bounded and pinned before claim.
            for source in self._inputs(conn, batch_id):
                if source['kind'] != 'notion' and source['sha256'] is None:
                    try:
                        blob = self._read(source)
                    except (OSError, ValueError):
                        # Deterministic unavailable-input marker; processing reports failure.
                        blob = None
                    conn.execute('UPDATE source_inputs SET sha256=?,byte_length=? WHERE batch_id=? AND source_id=?',
                                 (hashlib.sha256(blob if blob is not None else b'legacy-unavailable').hexdigest(),
                                  len(blob) if blob is not None else 0, batch_id, source['source_id']))
            seal_inputs(conn, batch_id)
            if not conn.execute('SELECT input_manifest_digest FROM collection_batches WHERE id=?', (batch_id,)).fetchone()[0]:
                return None
            now = self.clock()
            old = conn.execute('SELECT attempt,fencing_token,lease_until FROM processing_runs WHERE batch_id=?', (batch_id,)).fetchone()
            if old and old[2] > now:
                return None
            conn.execute('DELETE FROM processing_staging WHERE batch_id=?', (batch_id,))
            attempt, token = (old[0] + 1, old[1] + 1) if old else (1, 1)
            conn.execute('INSERT INTO processing_runs(batch_id,worker_id,attempt,fencing_token,lease_until,status) '
                         'VALUES(?,?,?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET worker_id=excluded.worker_id, '
                         'attempt=excluded.attempt,fencing_token=excluded.fencing_token,lease_until=excluded.lease_until,status=excluded.status,result_digest=NULL',
                         (batch_id, worker_id, attempt, token, now + lease_seconds, 'claimed'))
            conn.execute("UPDATE collection_batches SET processing_status='claimed' WHERE id=?", (batch_id,))
            return dict(batch_id=batch_id, worker_id=worker_id, fencing_token=token, attempt=attempt)

    def _check(self, conn, claim, terminal=False):
        row = conn.execute('SELECT worker_id,fencing_token,lease_until,status,result_digest,attempt FROM processing_runs WHERE batch_id=?', (claim['batch_id'],)).fetchone()
        if not row or row[:2] != (claim['worker_id'], claim['fencing_token']) or row[5] != claim['attempt']:
            raise LeaseLost('Fencing token or worker mismatch')
        tid = conn.execute('SELECT task_id FROM collection_batches WHERE id=?', (claim['batch_id'],)).fetchone()[0]
        task = self.store._read(conn, tid)
        if task.state == State.CANCELLED:
            raise LeaseLost('Task cancelled')
        if not (terminal and row[3] in TERMINAL) and (row[2] <= self.clock() or row[3] in TERMINAL):
            raise LeaseLost('Lease expired or run terminal')
        return row, task

    def heartbeat(self, claim, lease_seconds=60):
        if not 0 < lease_seconds <= 3600:
            raise ValueError('Invalid lease')
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._check(conn, claim)
            conn.execute('UPDATE processing_runs SET lease_until=? WHERE batch_id=?', (self.clock() + lease_seconds, claim['batch_id']))

    def _inputs(self, conn, bid):
        cursor = conn.execute('SELECT * FROM source_inputs WHERE batch_id=? ORDER BY rowid', (bid,))
        keys = [d[0] for d in cursor.description]
        inputs = [dict(zip(keys, r)) for r in cursor]
        for source in inputs:
            if source['kind'] == 'notion':
                package = conn.execute('SELECT content_fingerprint,byte_length FROM source_packages WHERE batch_id=? AND source_id=? AND revision=?',
                                       (bid, source['source_id'], source['revision'])).fetchone()
                if package:
                    source['sha256'], source['byte_length'] = package
        return inputs

    def _read(self, source):
        path = PurePosixPath(source['locator'])
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != 'source' or '..' in path.parts:
            raise ValueError('Invalid registered source locator')
        return bounded_read(self.store.directory(source['task_id']) / str(path), MAX_SOURCE_BYTES,
                            source['sha256'], source['byte_length'], capability=self.store.capability)

    def parse(self, claim):
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._check(conn, claim)
            conn.execute("UPDATE processing_runs SET status='processing' WHERE batch_id=?", (claim['batch_id'],))
            conn.execute("UPDATE collection_batches SET processing_status='processing' WHERE id=?", (claim['batch_id'],))
            inputs = self._inputs(conn, claim['batch_id'])
            # Legacy bytes have no historical fingerprint evidence. Pin the first
            # bounded observation transactionally; never infer provenance from name.
            for source in inputs:
                if source['sha256'] is None and source['kind'] != 'notion':
                    try:
                        blob = self._read(source)
                    except (OSError, ValueError):
                        continue
                    source['sha256'] = hashlib.sha256(blob).hexdigest()
                    source['byte_length'] = len(blob)
                    conn.execute('UPDATE source_inputs SET sha256=?,byte_length=? WHERE batch_id=? AND source_id=?',
                                 (source['sha256'], source['byte_length'], claim['batch_id'], source['source_id']))
        results = []
        for source in inputs:
            try:
                if source['kind'] == 'notion':
                    from .acquisition import OfflineAcquisition
                    result = OfflineAcquisition(self.store).parse(source)
                elif source['sha256'] is None or source['byte_length'] is None:
                    result = parse_content(source, error='input_verification_failed')
                else:
                    result = parse_content(source, self._read(source))
            except (OSError, ValueError):
                result = parse_content(source, error='input_verification_failed')
            results.append(result)
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._check(conn, claim)
            conn.execute('INSERT INTO processing_staging(batch_id,fencing_token,payload) VALUES(?,?,?) '
                         'ON CONFLICT(batch_id) DO UPDATE SET fencing_token=excluded.fencing_token,payload=excluded.payload',
                         (claim['batch_id'], claim['fencing_token'], encode(results)))
        return results

    def publish(self, claim, results):
        digest = hashlib.sha256(encode(results).encode()).hexdigest()
        with self.store._locked() as conn:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                run, task = self._check(conn, claim, terminal=True)
                if run[3] in TERMINAL:
                    if run[4] != digest:
                        raise ValueError('Conflicting terminal replay')
                    return self._manifest(conn, claim['batch_id'])
                if run[3] != 'processing':
                    raise ValueError('Run must be processing')
                staged = conn.execute('SELECT fencing_token,payload FROM processing_staging WHERE batch_id=?',
                                      (claim['batch_id'],)).fetchone()
                if staged != (claim['fencing_token'], encode(results)):
                    raise ValueError('Result does not match staged parser output')
                inputs = self._inputs(conn, claim['batch_id'])
                if not inputs or len(results) != len(inputs):
                    raise ValueError('Incomplete result set')
                for source, result in zip(inputs, results):
                    expected = parse_content(source, error='input_verification_failed')
                    for key in ('content_schema_version', 'trust', 'source_id', 'origin', 'input', 'parser_version', 'policy_version', 'locator'):
                        if result.get(key) != expected[key]:
                            raise ValueError('Result identity mismatch')
                    if result['status'] not in {'completed', 'partial', 'failed'} or (result['status'] == 'completed') != (result['completeness'] == 'complete'):
                        raise ValueError('Invalid result status')
                count = sum(r['status'] == 'completed' for r in results)
                status = 'completed' if count == len(results) else 'partial' if any(r['status'] != 'failed' for r in results) else 'failed'
                manifest = dict(content_schema_version=CONTENT_SCHEMA_VERSION, batch_id=claim['batch_id'],
                    task_id=task.id, status=status, trust='untrusted_source_data',
                    batch_input_fingerprint=conn.execute('SELECT input_manifest_digest FROM collection_batches WHERE id=?', (claim['batch_id'],)).fetchone()[0],
                    finalized_version=conn.execute('SELECT finalized_version FROM collection_batches WHERE id=?', (claim['batch_id'],)).fetchone()[0],
                    parser_version=PARSER_VERSION, policy_version=POLICY_VERSION,
                    processing_run=dict(claim), sources=[dict(source_id=r['source_id'], origin=r['origin'],
                    input=r['input'], locator=r['locator'], status=r['status'], completeness=r['completeness'],
                    content_sha256=hashlib.sha256(encode(r).encode()).hexdigest(),
                    content_ref=dict(batch_id=claim['batch_id'], source_id=r['source_id'])) for r in results])
                by_id = {s.id: s for s in task.sources}
                for result in results:
                    conn.execute('INSERT INTO sanitized_contents(batch_id,source_id,payload) VALUES(?,?,?)',
                                 (claim['batch_id'], result['source_id'], encode(result)))
                    source = by_id[result['source_id']]
                    source.status = 'parsed' if result['status'] != 'failed' else 'failed'
                    source.complete = result['completeness'] == 'complete'
                    source.content = '\n'.join(b['text'] for b in result['blocks'])
                    source.failure = ','.join(i['code'] for i in result['issues'])
                    source.input_sha256 = result['input']['sha256']
                    source.byte_length = result['input']['byte_length']
                conn.execute('DELETE FROM processing_staging WHERE batch_id=?', (claim['batch_id'],))
                conn.execute('INSERT INTO batch_manifests(batch_id,payload) VALUES(?,?)', (claim['batch_id'], encode(manifest)))
                conn.execute('UPDATE processing_runs SET status=?,result_digest=? WHERE batch_id=?', (status, digest, claim['batch_id']))
                conn.execute('UPDATE collection_batches SET processing_status=? WHERE id=?', (status, claim['batch_id']))
                task.state, task.confirmation = State.REVIEW, None
                task.version += 1
                for barrier in task.processing_barrier:
                    if barrier['batch_id'] == claim['batch_id']:
                        barrier['status'] = status
                conn.execute('UPDATE tasks SET state=?,version=?,payload=? WHERE id=?', (task.state, task.version, task.dumps(), task.id))
                conn.execute('INSERT INTO history(task_id,version,payload) VALUES(?,?,?)', (task.id, task.version, task.dumps()))
            self.store._snapshot(task)
            return manifest

    @staticmethod
    def _manifest(conn, bid):
        row = conn.execute('SELECT payload FROM batch_manifests WHERE batch_id=?', (bid,)).fetchone()
        if not row:
            raise KeyError(bid)
        return json.loads(row[0])

    def result(self, batch_id):
        with self.store._locked() as conn:
            return self._manifest(conn, batch_id), [json.loads(r[0]) for r in conn.execute(
                'SELECT payload FROM sanitized_contents WHERE batch_id=? ORDER BY rowid', (batch_id,))]

    def process(self, batch_id, worker_id, lease_seconds=60):
        claim = self.claim(batch_id, worker_id, lease_seconds)
        if claim is None:
            return None
        stop = Event()
        lost = []

        def renew():
            while not stop.wait(lease_seconds / 3):
                try:
                    self.heartbeat(claim, lease_seconds)
                except Exception:
                    lost.append(True)
                    return

        worker = Thread(target=renew, name='processor-heartbeat', daemon=True)
        worker.start()
        try:
            results = self.parse(claim)
        finally:
            stop.set()
            worker.join()
        if lost:
            raise LeaseLost('Heartbeat failed')
        return self.publish(claim, results)
