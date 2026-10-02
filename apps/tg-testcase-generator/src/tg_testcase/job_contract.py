"""Strict persisted job DTO validation, kept separate from transition operations."""
from .contract_types import fields, identifier, integer, name, principal, id32, sha, require
from .internal_errors import ErrorCode


def validate_job(j):
    fields(j, 'job_id principal request_id intent_digest semantic_key task_id batch_id source_id task_expected_version '
           'revision workspace_ref grant_ref credential_ref policy_epoch credential_generation policy_digest '
           'root_type root_id canonical_root_id state attempt fencing_token worker_id created_at updated_at deadline '
           'lease_until retry_at cancel_state cancel_at package_id package_digest package_length spool_state spool_ref '
           'receipt_state receipt last_error failure_count')
    for key in ('job_id', 'request_id', 'task_id', 'batch_id', 'source_id'):
        identifier(j[key])
    principal(j['principal'])
    for key in ('intent_digest', 'semantic_key', 'policy_digest'):
        sha(j[key])
    for key in ('workspace_ref', 'grant_ref', 'credential_ref'):
        name(j[key])
    for key in ('task_expected_version', 'revision', 'policy_epoch', 'credential_generation'):
        integer(j[key], 1)
    for key in ('attempt', 'fencing_token', 'created_at', 'updated_at', 'deadline', 'failure_count'):
        integer(j[key])
    for key in ('lease_until', 'retry_at', 'cancel_at'):
        if j[key] is not None:
            integer(j[key])
    id32(j['root_id']); id32(j['canonical_root_id'])
    require(j['root_id'] == j['canonical_root_id'])
    require(j['root_type'] in {'page', 'database', 'data_source'})
    require(j['deadline'] == j['created_at'] + 900000)
    require(j['state'] in {'registered', 'claim_pending', 'claimed', 'fetching', 'retry_wait', 'prepared', 'submitting',
                           'reconciling', 'committed', 'cancelled', 'revoked', 'expired', 'failed'})
    require(j['cancel_state'] in {'none', 'requested', 'effective'})
    require((j['cancel_state'] == 'none') == (j['cancel_at'] is None))
    require(j['spool_state'] in {'none', 'pinned', 'retained', 'removed'})
    require(j['receipt_state'] in {'unknown', 'committed', 'not_committed', 'conflict'})
    require((j['attempt'] == 0) == (j['fencing_token'] == 0))
    if j['worker_id'] is not None:
        identifier(j['worker_id'])
    if j['attempt'] > 0:
        require(j['worker_id'] is not None and j['lease_until'] is not None)
    pin = [j[k] for k in ('package_id', 'package_digest', 'package_length')]
    require(all(v is None for v in pin) or all(v is not None for v in pin))
    if j['package_id'] is not None:
        identifier(j['package_id']); sha(j['package_digest']); integer(j['package_length'], 0, 16777216)
        require(j['spool_ref'] is not None and j['spool_state'] != 'none')
    else:
        require(j['spool_ref'] is None and j['spool_state'] == 'none')
    if j['spool_ref'] is not None:
        identifier(j['spool_ref'])
    if j['receipt'] is not None:
        fields(j['receipt'], 'package_id package_digest content_fingerprint')
        identifier(j['receipt']['package_id']); sha(j['receipt']['package_digest']); sha(j['receipt']['content_fingerprint'])
        require(j['receipt_state'] == 'committed' and all(j['receipt'][k] == j[k] for k in ('package_id', 'package_digest')))
    require((j['receipt_state'] == 'committed') == (j['receipt'] is not None))
    if j['state'] == 'committed':
        require(j['receipt_state'] == 'committed')
    if j['last_error'] is not None:
        ErrorCode(j['last_error'])
    return j
