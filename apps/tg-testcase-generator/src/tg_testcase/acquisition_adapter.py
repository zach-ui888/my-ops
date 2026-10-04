"""Trusted Controller acquisition port. Calls require the shared guard.

Receipt reconciliation and transport are deliberately separate later steps.
"""
import json
import time
from .contract_types import identifier, integer
from .controller_ports import SourceSnapshot, claim_record, lease_milliseconds
from .internal_errors import fail
from .notion_reference import parse_notion_reference


def read_claim(conn, batch_id, source_id):
    row = conn.execute('SELECT revision,worker_id,attempt,fencing_token,lease_until,status FROM source_fetch_runs WHERE batch_id=? AND source_id=?', (batch_id, source_id)).fetchone()
    if row is None:
        return None
    return claim_record(dict(batch_id=batch_id, source_id=source_id, revision=row[0],
                             worker_id=row[1], attempt=row[2], fencing_token=row[3],
                             lease_until=lease_milliseconds(row[4]), state=row[5]))


def read_source(store, conn, task_id, batch_id, source_id):
    row = conn.execute('SELECT user_id,state,version FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        fail('FENCED')
    task = store._read(conn, task_id)
    if row != (task.user_id, task.state, task.version) or task.id != task_id:
        fail('INTERNAL_FAILURE')
    source = conn.execute('SELECT task_id,locator,revision,kind FROM source_inputs WHERE batch_id=? AND source_id=?', (batch_id, source_id)).fetchone()
    batch = conn.execute('SELECT task_id,status,source_ids,acquisition_status FROM collection_batches WHERE id=?', (batch_id,)).fetchone()
    current = [s for s in task.sources if s.id == source_id]
    if not source or not batch or source[0] != task_id or batch[0] != task_id or source[3] != 'notion' or len(current) != 1:
        fail('FENCED')
    ids = json.loads(batch[2])
    if not isinstance(ids, list) or len(ids) != len(set(ids)) or source_id not in ids:
        fail('FENCED')
    root = parse_notion_reference(source[1])
    if (current[0].revision != source[2] or current[0].kind != 'notion' or
            parse_notion_reference(current[0].locator) != root):
        fail('FENCED')
    sealed = conn.execute('SELECT 1 FROM source_packages WHERE batch_id=? AND source_id=?', (batch_id, source_id)).fetchone() is not None
    return SourceSnapshot(task_id, batch_id, source_id, task.user_id, source[2], root,
                          batch[1] == 'finalized' and batch[3] != 'abandoned', sealed,
                          task.state == 'cancelled')


class ProtectedAcquisitionAdapter:
    def __init__(self, store, guard, clock=time.time):
        self.store, self.guard, self.clock = store, guard, clock

    def inspect_source(self, task_id, batch_id, source_id):
        self.guard.require_held()
        for value in (task_id, batch_id, source_id):
            identifier(value)
        with self.store._locked() as conn:
            return read_source(self.store, conn, task_id, batch_id, source_id)

    def lookup_claim(self, batch_id, source_id):
        self.guard.require_held()
        with self.store._locked() as conn:
            return read_claim(conn, batch_id, source_id)

    def claim(self, source, worker_id, lease_until):
        self.guard.require_held()
        identifier(worker_id); integer(lease_until)
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            actual = read_source(self.store, conn, source.task_id, source.batch_id, source.source_id)
            if actual != source or actual.cancelled or not actual.finalized or actual.sealed:
                fail('FENCED')
            now = lease_milliseconds(self.clock())
            if not now < lease_until <= now + 3600000:
                fail('LEASE_LOST')
            old = read_claim(conn, source.batch_id, source.source_id)
            if old and old['revision'] != source.revision:
                fail('FENCED')
            if old and (old['state'] in {'sealed', 'abandoned'} or old['lease_until'] > now):
                return None
            attempt = integer(old['attempt'] + 1 if old else 1, 1)
            fence = integer(old['fencing_token'] + 1 if old else 1, 1)
            conn.execute('INSERT INTO source_fetch_runs VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(batch_id,source_id) DO UPDATE SET worker_id=excluded.worker_id,attempt=excluded.attempt,fencing_token=excluded.fencing_token,lease_until=excluded.lease_until,status=excluded.status',
                         (source.batch_id, source.source_id, source.revision, worker_id, attempt, fence, lease_until / 1000, 'claimed', None))
            conn.execute("UPDATE collection_batches SET acquisition_status='acquiring' WHERE id=?", (source.batch_id,))
            return read_claim(conn, source.batch_id, source.source_id)

    def _validate(self, conn, claim, states):
        claim_record(claim)
        actual = read_claim(conn, claim['batch_id'], claim['source_id'])
        if not actual or any(actual[k] != claim[k] for k in ('revision', 'worker_id', 'attempt', 'fencing_token')):
            fail('FENCED')
        if actual['state'] not in states or actual['lease_until'] <= lease_milliseconds(self.clock()):
            fail('LEASE_LOST')
        row = conn.execute('SELECT task_id FROM source_inputs WHERE batch_id=? AND source_id=?', (claim['batch_id'], claim['source_id'])).fetchone()
        if not row:
            fail('FENCED')
        source = read_source(self.store, conn, row[0], claim['batch_id'], claim['source_id'])
        if source.cancelled:
            fail('JOB_CANCELLED')
        if not source.finalized or source.revision != actual['revision']:
            fail('FENCED')
        return actual

    def transition(self, claim, target, retry_at=None):
        self.guard.require_held()
        allowed = {'fetching': {'claimed'}, 'retry_wait': {'fetching'}, 'abandoned': {'claimed', 'fetching', 'retry_wait'}}
        if target not in allowed:
            fail('FENCED')
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            actual = self._validate(conn, claim, allowed[target])
            until = actual['lease_until']
            if target == 'retry_wait':
                until = integer(retry_at)
            elif retry_at is not None:
                fail('FENCED')
            conn.execute('UPDATE source_fetch_runs SET status=?,lease_until=? WHERE batch_id=? AND source_id=?', (target, until / 1000, claim['batch_id'], claim['source_id']))

    def heartbeat(self, claim, lease_until):
        self.guard.require_held()
        integer(lease_until)
        with self.store._locked() as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            self._validate(conn, claim, {'claimed', 'fetching'})
            now = lease_milliseconds(self.clock())
            if not now < lease_until <= now + 3600000:
                fail('LEASE_LOST')
            conn.execute('UPDATE source_fetch_runs SET lease_until=? WHERE batch_id=? AND source_id=?', (lease_until / 1000, claim['batch_id'], claim['source_id']))
            return read_claim(conn, claim['batch_id'], claim['source_id'])

    def receipt(self, job):
        """Fail closed until the separately scoped receipt/reconciliation step."""
        self.guard.require_held()
        fail('RECEIPT_PENDING')
