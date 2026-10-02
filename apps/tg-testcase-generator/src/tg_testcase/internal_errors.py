"""Closed diagnostics from CONTRACT §7. Never include submitted data in errors."""
from dataclasses import dataclass
from enum import Enum
from .contract_types import fields, identifier, integer, sha

# priority, enum, retryable, audit category, Application code
_ROWS = '''10 AUTH_INVALID 0 identity forbidden
11 AUTH_REPLAY 0 identity forbidden
12 OWNER_MISMATCH 0 identity forbidden
20 POLICY_INVALID 0 configuration forbidden
21 ROOT_AMBIGUOUS 0 configuration forbidden
22 CREDENTIAL_STALE 0 authorization forbidden
23 POLICY_REVOKED 0 authorization forbidden
24 SCOPE_DENIED 0 authorization forbidden
25 WORKSPACE_MISMATCH 0 authorization forbidden
30 JOB_CANCELLED 0 lifecycle invalid_request
31 JOB_TIMEOUT 0 lifecycle invalid_request
32 FENCED 0 lifecycle invalid_request
33 LEASE_LOST 0 lifecycle invalid_request
40 REFERENCE_INVALID 0 input invalid_request
41 ROOT_TYPE_MISMATCH 0 scope invalid_request
42 VERSION_CONFLICT 0 concurrency stale_version
43 REQUEST_CONFLICT 0 integrity request_conflict
44 DIGEST_CONFLICT 0 integrity request_conflict
45 PACKAGE_INVALID 0 integrity invalid_request
46 PROPERTY_ID_INVALID 0 adapter invalid_request
47 LOCATOR_COLLISION 0 integrity invalid_request
50 NOTION_AUTH_FAILED 0 upstream_auth forbidden
51 NOTION_RESOURCE_UNAVAILABLE 0 upstream_scope not_found
52 PAGINATION_INVALID 0 upstream_data invalid_request
53 VIEW_UNRESOLVED 0 scope invalid_request
54 RESOURCE_LIMIT 0 budget invalid_request
55 ATTACHMENT_SSRF_BLOCKED 0 egress invalid_request
56 ATTACHMENT_REDIRECT_REJECTED 0 egress invalid_request
57 ATTACHMENT_EXPIRED 1 attachment invalid_request
60 RATE_LIMIT_EXHAUSTED 0 upstream_capacity invalid_request
61 UPSTREAM_UNAVAILABLE 1 upstream_capacity storage_error
62 STORAGE_FAILURE 1 local_storage storage_error
63 RECEIPT_PENDING 1 reconciliation storage_error
64 RECEIPT_NOT_FOUND 0 reconciliation not_found
90 INTERNAL_FAILURE 0 internal storage_error'''
ErrorCode = Enum('ErrorCode', {r.split()[1]: r.split()[1] for r in _ROWS.splitlines()}, type=str)
ERROR_INFO = {ErrorCode(n): (int(p), bool(int(r)), a, c)
              for p, n, r, a, c in (line.split() for line in _ROWS.splitlines())}


class ContractError(ValueError):
    def __init__(self, code):
        self.code = ErrorCode(code)
        super().__init__(self.code.value)

    @property
    def application_code(self):
        return ERROR_INFO[self.code][3]

    @property
    def retryable(self):
        return ERROR_INFO[self.code][1]


def fail(code):
    raise ContractError(code)


def select_error(codes):
    return min((ErrorCode(c) for c in codes), key=lambda c: ERROR_INFO[c][0])


def classify_exception(exc):
    return exc.code if isinstance(exc, ContractError) else ErrorCode.INTERNAL_FAILURE


@dataclass(frozen=True)
class AuditEvent:
    error: ErrorCode
    principal_alias: str
    job_id: str
    request_id: str
    policy_epoch: int
    policy_digest: str
    at: int
    count: int
    endpoint: str

    def __post_init__(self):
        ErrorCode(self.error)
        sha(self.principal_alias)
        identifier(self.job_id); identifier(self.request_id)
        integer(self.policy_epoch, 1); sha(self.policy_digest)
        integer(self.at); integer(self.count)
        if self.endpoint not in {'none', 'page', 'block', 'children', 'database', 'data_source', 'query', 'property'}:
            fail('INTERNAL_FAILURE')

    @classmethod
    def parse(cls, value):
        fields(value, 'error principal_alias job_id request_id policy_epoch policy_digest at count endpoint')
        # Aliases must be supplied by a protected mapping, not raw Telegram IDs.
        sha(value['principal_alias'])
        identifier(value['job_id']); identifier(value['request_id'])
        integer(value['policy_epoch'], 1); sha(value['policy_digest'])
        integer(value['at']); integer(value['count'])
        if value['endpoint'] not in {'none', 'page', 'block', 'children', 'database', 'data_source', 'query', 'property'}:
            fail('INTERNAL_FAILURE')
        return cls(**dict(value, error=ErrorCode(value['error'])))
