"""Authenticated offline Application adapter. No new Application wire operation."""
import json
import sqlite3
from .contract_types import digest
from .internal_errors import ContractError, fail
from .notion_reference import parse_notion_reference
from .protocol import response


class ControllerBridge:
    def __init__(self, application, registry, identity, *, authorize_other):
        """authorize_other(principal,action,task) must explicitly grant non-scope ops.

        In particular content reads are never granted by scope-policy presence.
        Injection is protected composition, not envelope-supplied code or config.
        """
        self.app, self.registry, self.identity = application, registry, identity
        self.authorize_other = authorize_other

    def handle(self, raw_envelope, peer, now_ms):
        rid = None
        try:
            record = self.identity.reserve(raw_envelope, peer, now_ms)
            req = record['request']
            rid = req['request_id']
            actor = 'telegram:' + req['actor']['user_id']
            with self.registry.guard.hold():
                task = None
                if 'task_id' in req:
                    task = self.app.store.get(req['task_id'])
                    if task.user_id != req['actor']['user_id']:
                        fail('OWNER_MISMATCH')
                op = req['operation']
                if op in {'append_notion_reference', 'refresh_notion_source'}:
                    policy, _ = self.registry.authority()
                    if op == 'append_notion_reference':
                        root = req['payload']['reference']
                        operation = op
                    else:
                        source = next((s for s in task.sources if s.id == req['payload']['source_id']), None)
                        if source is None or source.kind != 'notion':
                            fail('SCOPE_DENIED')
                        root = parse_notion_reference(source.locator)
                        operation = 'refresh'
                    policy.resolve(actor, operation, root)
                elif op != 'cancel':
                    if not self.authorize_other(actor, record['envelope']['action'], task):
                        fail('SCOPE_DENIED')
                # Owner-safe cancel deliberately works even after full revocation.
                # Re-authorize before reading Application cache, including retries.
                if record['state'] != 'reserved':
                    with self.app.store._locked() as conn:
                        cached = conn.execute('SELECT response FROM requests WHERE request_id=? AND actor_id=?',
                                              (rid, req['actor']['user_id'])).fetchone()
                    if not cached:
                        fail('RECEIPT_PENDING')
                    result = json.loads(cached[0])
                    if digest(result) != record['response_digest']:
                        fail('REQUEST_CONFLICT')
                    return result
                # Reservation is durable before dispatch. A crash reuses this exact
                # normalized request_id/version/request, relying on Application CAS.
                result = self.app.handle(req)
                if op == 'cancel' and result['ok']:
                    with self.registry.transaction() as conn:
                        from .contract_types import strict_json
                        jobs = [strict_json(row[0]) for row in conn.execute('SELECT payload FROM jobs')]
                    for job in jobs:
                        if job['task_id'] == task.id:
                            self.registry.stop(job['job_id'], 'JOB_CANCELLED', now_ms)
                self.identity.complete(rid, result, rejected=not result['ok'])
                return result
        except ContractError as exc:
            return response(rid, exc.application_code, message=exc.application_code)
        except KeyError:
            return response(rid, 'not_found', message='not_found')
        except (OSError, sqlite3.Error):
            return response(rid, 'storage_error', message='storage_error')
        except Exception:
            return response(rid, 'storage_error', message='storage_error')
