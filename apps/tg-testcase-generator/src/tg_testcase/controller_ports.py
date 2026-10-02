"""Typed internal boundaries. Implementations receive trusted bindings, not URLs.

Property locator algorithm is frozen in CONTRACT §8; its implementation belongs
in Step 2.3. Only the safe locator DTO crosses into the future package builder.
"""
from dataclasses import dataclass
from typing import Optional, Protocol
from .contract_types import fields, identifier, integer, id32, require, sha, name

DEFAULT_CLAIM_LEASE_MS = 60000
HEARTBEAT_INTERVAL_MS = 20000


@dataclass(frozen=True)
class SourceSnapshot:
    task_id: str
    batch_id: str
    source_id: str
    owner: str
    revision: int
    root_id: str
    finalized: bool
    sealed: bool
    cancelled: bool

    def __post_init__(self):
        for v in (self.task_id, self.batch_id, self.source_id):
            identifier(v)
        require(type(self.owner) is str)
        integer(self.revision, 1); id32(self.root_id)
        require(all(type(v) is bool for v in (self.finalized, self.sealed, self.cancelled)))


@dataclass(frozen=True)
class SafeProperty:
    locator: str
    text: str

    def __post_init__(self):
        from .contract_types import string
        string(self.locator, r'(?:np1|npi1)-[0-9a-f]{64}')
        require(type(self.text) is str)


class AcquisitionPort(Protocol):
    def inspect_source(self, task_id: str, batch_id: str, source_id: str) -> SourceSnapshot: ...
    def claim(self, source: SourceSnapshot, worker_id: str, lease_until: int) -> Optional[dict]: ...
    def lookup_claim(self, batch_id: str, source_id: str) -> Optional[dict]: ...
    def transition(self, claim: dict, target: str, retry_at: Optional[int] = None) -> None: ...
    def heartbeat(self, claim: dict, lease_until: int) -> dict: ...
    def receipt(self, job: dict) -> dict: ...


class NotionReadPort(Protocol):
    def get_page(self, binding, page_id: str) -> dict: ...
    def get_block(self, binding, block_id: str) -> dict: ...
    def list_children(self, binding, block_id: str, cursor: Optional[str]) -> dict: ...
    def get_database(self, binding, database_id: str) -> dict: ...
    def get_data_source(self, binding, data_source_id: str) -> dict: ...
    def query_data_source(self, binding, data_source_id: str, cursor: Optional[str]) -> dict: ...
    def get_property(self, binding, page_id: str, property_id: str, cursor: Optional[str]) -> dict: ...


def outbox_event(value):
    fields(value, 'event_id job_id task_id batch_id source_id revision attempt fencing_token policy_epoch policy_digest credential_ref credential_generation package_id package_digest content_fingerprint committed_at')
    for k in ('event_id', 'job_id', 'task_id', 'batch_id', 'source_id', 'package_id'):
        identifier(value[k])
    for k in ('revision', 'attempt', 'fencing_token', 'policy_epoch', 'credential_generation'):
        integer(value[k], 1)
    for k in ('policy_digest', 'package_digest', 'content_fingerprint'):
        sha(value[k])
    name(value['credential_ref']); integer(value['committed_at'])
    return dict(value)


def claim_record(value):
    """Acquisition authority in milliseconds; producer fields cannot construct it."""
    fields(value, 'batch_id source_id revision worker_id attempt fencing_token lease_until state')
    for k in ('batch_id', 'source_id', 'worker_id'):
        identifier(value[k])
    for k in ('revision', 'attempt', 'fencing_token'):
        integer(value[k], 1)
    integer(value['lease_until'])
    require(value['state'] in {'claimed', 'fetching', 'retry_wait', 'sealed', 'abandoned', 'lost'})
    return dict(value)


def lease_milliseconds(seconds):
    """Only for trusted acquisition seconds. Floor, never round up a lease."""
    import math
    require(type(seconds) in (float, int) and math.isfinite(seconds) and seconds >= 0)
    return integer(math.floor(seconds * 1000))


class DeadlineBudget:
    """A monotonic execution budget may shorten, but never extend, a UTC deadline."""
    def __init__(self, deadline, now_ms, monotonic_now):
        integer(deadline); integer(now_ms)
        self.deadline = deadline
        self.end = monotonic_now + max(0, deadline - now_ms) / 1000

    def remaining_ms(self, now_ms, monotonic_now):
        integer(now_ms)
        self.end = min(self.end, monotonic_now + max(0, self.deadline - now_ms) / 1000)
        return max(0, min(self.deadline - now_ms, int((self.end - monotonic_now) * 1000)))
