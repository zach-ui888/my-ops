"""Read-only registry snapshots; no migrations or registry writes in Receiver."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3

from .credential_registry import CredentialRegistryMixin
from .job_registry import JobRegistry
from .internal_errors import fail


class RegistrySnapshot:
    def __init__(self, guard, job, authority):
        self.guard = guard
        self._job, self._authority = job, authority
        self._decision = guard.issue()

    def get_job(self, job_id):
        self.guard.validate_decision(self._decision)
        if job_id != self._job['job_id']:
            fail('FENCED')
        return deepcopy(self._job)

    def authority(self):
        self.guard.validate_decision(self._decision)
        return deepcopy(self._authority)


class RegistryReader:
    # Reuse exactly the writer's strict validation, with a read-only transaction.
    get_job = JobRegistry.get_job
    authority = CredentialRegistryMixin.authority

    def __init__(self, db_path, guard):
        self.db_path, self.guard = Path(db_path), guard
        self._connection = None

    @contextmanager
    def transaction(self):
        self.guard.require_held()
        if self._connection is not None:
            yield self._connection
            return
        from .unix_transport import remaining
        conn = sqlite3.connect(self.db_path.as_uri() + '?mode=ro', uri=True, timeout=remaining())
        try:
            conn.execute('PRAGMA query_only=ON')
            self._connection = conn
            conn.execute('BEGIN')
            yield conn
            conn.rollback()
        finally:
            self._connection = None
            conn.close()

    def snapshot(self, job_id):
        with self.transaction():
            return RegistrySnapshot(self.guard, self.get_job(job_id), self.authority())
