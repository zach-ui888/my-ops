"""Shared guard and short-lived decisions used by the acquisition commit boundary."""
from contextlib import contextmanager
import fcntl
import os
import stat
import threading
from .contract_types import integer, identifier, sha
from .internal_errors import fail, select_error


class AuthorizationGuard:
    """Open a precreated stable lock. Never creates, replaces or unlinks it.

    Deployment must validate all parent ownership through RuntimePaths first.
    Separate opens/flock serialize threads, processes and independently made guards.
    """
    def __init__(self, path):
        self.path = path
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            fail("POLICY_INVALID")
        self._inode = (info.st_dev, info.st_ino)
        self._local = threading.local()

    @property
    def held(self):
        return getattr(self._local, 'token', None) is not None

    @contextmanager
    def hold(self):
        if self.held:
            yield self
            return
        fd = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != self._inode:
                fail('POLICY_INVALID')
            from .unix_transport import bounded_flock
            bounded_flock(fd)
            self._local.token = object()
            self._local.decisions = set()
            try:
                yield self
            finally:
                self._local.token = None
                self._local.decisions = set()
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def require_held(self):
        if not self.held:
            fail('INTERNAL_FAILURE')

    def issue(self):
        self.require_held()
        result = object()
        self._local.decisions.add(result)
        return result

    def validate_decision(self, decision):
        self.require_held()
        if decision not in self._local.decisions:
            fail('POLICY_REVOKED')


def check_job_authorization(registry, job, now_ms):
    """Re-read authoritative configuration under the guard; no cached grants."""
    registry.guard.require_held()
    integer(now_ms)
    policy, heads = registry.authority()
    errors = []
    head = heads.get(job['credential_ref'])
    if (not head or head['deleted'] or not head['enabled'] or
            head['generation'] != job['credential_generation']):
        errors.append('CREDENTIAL_STALE')
    if policy.epoch != job['policy_epoch'] or policy.digest != job['policy_digest']:
        errors.append('POLICY_REVOKED')
    if not errors:
        binding = policy.resolve(job['principal'], 'acquire', job['root_id'])
        for key in ('workspace_ref', 'grant_ref', 'credential_ref', 'credential_generation', 'root_type', 'root_id'):
            if getattr(binding, key) != job[key]:
                errors.append('SCOPE_DENIED')
    if job['cancel_state'] != 'none':
        errors.append('JOB_CANCELLED')
    if job['receipt_state'] == 'conflict':
        errors.append('DIGEST_CONFLICT')
    if job['last_error'] in {'INTERNAL_FAILURE', 'POLICY_REVOKED', 'JOB_TIMEOUT'}:
        errors.append(job['last_error'])
    if now_ms >= job['deadline']:
        errors.append('JOB_TIMEOUT')
    if job['state'] in {'cancelled', 'revoked', 'expired', 'failed'}:
        errors.append({'cancelled': 'JOB_CANCELLED', 'revoked': 'POLICY_REVOKED',
                       'expired': 'JOB_TIMEOUT', 'failed': 'FENCED'}[job['state']])
    if errors:
        fail(select_error(errors))


def authorize_commit(registry, job_id, claim, package_id, package_digest, now_ms):
    """Internal port. The caller must retain guard through its DB COMMIT.

    Acquisition itself must also recheck owner/cancel/revision/root/budget and
    lease/deadline immediately before commit.
    """
    registry.guard.require_held()
    identifier(package_id); sha(package_digest)
    from .controller_ports import claim_record
    claim = claim_record(claim)
    job = registry.get_job(job_id)
    check_job_authorization(registry, job, now_ms)
    if job['state'] not in {'submitting', 'reconciling', 'committed'}:
        fail('FENCED')
    if any(claim.get(k) != job[k] for k in ('batch_id', 'source_id', 'revision', 'attempt', 'fencing_token', 'worker_id')):
        fail('FENCED')
    if claim.get('state') not in {'fetching', 'sealed'} or claim.get('lease_until', 0) <= now_ms:
        fail('LEASE_LOST')
    if job['lease_until'] is None or job['lease_until'] <= now_ms:
        fail('LEASE_LOST')
    if (job['package_id'], job['package_digest']) != (package_id, package_digest):
        fail('DIGEST_CONFLICT')
    return registry.guard.issue()
