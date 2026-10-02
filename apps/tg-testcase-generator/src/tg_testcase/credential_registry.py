"""Metadata only: generations, tombstones and atomic policy publication."""
from .contract_types import (canonical, digest, fields, integer, name, identifier,
                             strict_json, require)
from .scope_policy import Policy, verification
from .internal_errors import ContractError, fail


def validate_credential(row):
    fields(row, 'credential_ref generation workspace_ref secret_handle enabled deleted changed_at')
    for k in ('credential_ref', 'workspace_ref', 'secret_handle'):
        name(row[k])
    integer(row['generation'], 1); integer(row['changed_at'])
    require(type(row['enabled']) is bool and type(row['deleted']) is bool)
    require(not row['deleted'] or not row['enabled'])
    return row


class CredentialRegistryMixin:
    def authority(self):
        """Validate stored bytes and all metadata every time; corruption fails closed."""
        self.guard.require_held()
        try:
            with self.transaction() as conn:
                row = conn.execute('SELECT epoch,digest,canonical_json FROM current_policy WHERE singleton=1').fetchone()
                if row is None:
                    fail('POLICY_INVALID')
                heads = {k: validate_credential(strict_json(v)) for k, v in conn.execute('SELECT credential_ref,payload FROM credential_head')}
                for k, h in heads.items():
                    require(k == h['credential_ref'])
                    history = conn.execute('SELECT payload FROM credential_history WHERE credential_ref=? AND generation=?', (k, h['generation'])).fetchone()
                    require(history is not None and history[0] == canonical(h))
                vs = {k: verification(strict_json(v)) for k, v in conn.execute('SELECT verification_ref,payload FROM verifications')}
                policy = Policy.parse(row[2], vs, heads)
                require(policy.epoch == row[0] and policy.digest == row[1] and policy.canonical_bytes == row[2])
                return policy, heads
        except ContractError:
            raise
        except (ValueError, KeyError, TypeError):
            fail('POLICY_INVALID')

    def publish(self, raw_policy, expected_previous_epoch, mutation_id, now_ms,
                *, credential_changes=(), verification_changes=()):
        """Protected admin transaction: head/history + verification + policy + audit.

        Changes are complete metadata DTOs with next generation/version. No secret
        resolver exists. Caller must remove deleted/revoked references in policy;
        dangling references cause atomic rejection rather than partial publication.
        """
        try:
            integer(expected_previous_epoch); identifier(mutation_id); integer(now_ms)
            # Decode before computing administrative mutation digest (duplicate keys).
            parsed = strict_json(raw_policy, ascii_only=True)
            changes = [validate_credential(dict(c)) for c in credential_changes]
            verifications = [verification(dict(v)) for v in verification_changes]
            require(len({c['credential_ref'] for c in changes}) == len(changes))
            require(len({v['verification_ref'] for v in verifications}) == len(verifications))
            md = digest([parsed, expected_previous_epoch, changes, verifications])
            with self.guard.hold(), self.transaction() as conn:
                old_mutation = conn.execute('SELECT digest,result FROM mutations WHERE mutation_id=?', (mutation_id,)).fetchone()
                if old_mutation:
                    if old_mutation[0] != md:
                        fail('REQUEST_CONFLICT')
                    return strict_json(old_mutation[1])
                old = conn.execute('SELECT epoch,digest,canonical_json FROM current_policy WHERE singleton=1').fetchone()
                if old:
                    self.authority()  # A damaged authority cannot be replaced via cache.
                heads = {k: validate_credential(strict_json(v)) for k, v in conn.execute('SELECT credential_ref,payload FROM credential_head')}
                vs = {k: verification(strict_json(v)) for k, v in conn.execute('SELECT verification_ref,payload FROM verifications')}
                for c in changes:
                    previous = heads.get(c['credential_ref'])
                    require(not previous or not previous['deleted'])
                    require(c['generation'] == (previous['generation'] + 1 if previous else 1))
                    heads[c['credential_ref']] = c
                for v in verifications:
                    previous = vs.get(v['verification_ref'])
                    require(v['record_version'] == (previous['record_version'] + 1 if previous else 1))
                    vs[v['verification_ref']] = v
                policy = Policy.parse(raw_policy, vs, heads)
                if old and old[2] == policy.canonical_bytes and not changes and not verifications:
                    result = {'epoch': old[0], 'digest': old[1]}
                else:
                    require(expected_previous_epoch == (old[0] if old else 0))
                    require(policy.epoch == expected_previous_epoch + 1)
                    for c in changes:
                        blob = canonical(c)
                        conn.execute('INSERT INTO credential_history VALUES(?,?,?)', (c['credential_ref'], c['generation'], blob))
                        conn.execute('INSERT INTO credential_head VALUES(?,?) ON CONFLICT(credential_ref) DO UPDATE SET payload=excluded.payload', (c['credential_ref'], blob))
                    for v in verifications:
                        conn.execute('INSERT INTO verifications VALUES(?,?) ON CONFLICT(verification_ref) DO UPDATE SET payload=excluded.payload', (v['verification_ref'], canonical(v)))
                    vals = (policy.epoch, policy.digest, policy.canonical_bytes, now_ms)
                    conn.execute('INSERT INTO policy_history VALUES(?,?,?,?)', vals)
                    conn.execute('INSERT INTO current_policy VALUES(1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET epoch=excluded.epoch,digest=excluded.digest,canonical_json=excluded.canonical_json,published_at=excluded.published_at', vals)
                    conn.execute('INSERT INTO audit(payload) VALUES(?)', (canonical({'policy_epoch': policy.epoch, 'policy_digest': policy.digest, 'at': now_ms, 'count': 1}),))
                    result = {'epoch': policy.epoch, 'digest': policy.digest}
                conn.execute('INSERT INTO mutations VALUES(?,?,?)', (mutation_id, md, canonical(result)))
                return result
        except ContractError:
            raise
        except (ValueError, KeyError, TypeError):
            fail('POLICY_INVALID')

    def mutate_credential(self, credential_ref, operation, mutation_id, now_ms,
                          *, workspace_ref=None, secret_handle=None):
        """Admin-only generation mutation; automatically prunes deleted grants.

        Creation is publish(..., credential_changes=...) so creation and first
        policy reference have one transaction. A rotation contains labels only.
        """
        name(credential_ref); identifier(mutation_id); integer(now_ms)
        require(operation in {'rotate', 'enable', 'disable', 'delete'})
        if workspace_ref is not None:
            name(workspace_ref)
        if secret_handle is not None:
            name(secret_handle)
        md = digest(['credential-mutation-v1', credential_ref, operation, workspace_ref, secret_handle])
        with self.guard.hold(), self.transaction() as conn:
            previous = conn.execute('SELECT digest,result FROM mutations WHERE mutation_id=?', (mutation_id,)).fetchone()
            if previous:
                if previous[0] != md:
                    fail('REQUEST_CONFLICT')
                return strict_json(previous[1])
            current, heads = self.authority()
            c = dict(heads.get(credential_ref, {}))
            if not c or c['deleted']:
                fail('CREDENTIAL_STALE')
            c.update(generation=integer(c['generation'] + 1, 1), changed_at=now_ms)
            if workspace_ref is not None:
                c['workspace_ref'] = workspace_ref
            if secret_handle is not None:
                c['secret_handle'] = secret_handle
            if operation in {'enable', 'disable', 'delete'}:
                c['enabled'] = operation == 'enable'
            if operation == 'delete':
                c['deleted'] = True
            p = current.data
            p['policy_epoch'] = integer(current.epoch + 1, 1)
            if operation == 'delete':
                p['credentials'].pop(credential_ref, None)
                p['grants'] = {k: g for k, g in p['grants'].items() if g['credential_ref'] != credential_ref}
                for rule in p['principals'].values():
                    rule['grants'] = [r for r in rule['grants'] if r in p['grants']]
                p['principals'] = {k: r for k, r in p['principals'].items() if r['grants']}
            elif credential_ref in p['credentials']:
                p['credentials'][credential_ref] = {k: c[k] for k in ('workspace_ref', 'secret_handle', 'generation', 'enabled')}
                for grant in p['grants'].values():
                    if grant['credential_ref'] == credential_ref:
                        grant['workspace_ref'] = c['workspace_ref']
            # Keep one administrative mutation key: publish's temporary key is
            # transaction-local and removed before the outer transaction commits.
            internal_id = 'cm-' + md
            result = self.publish(canonical(p), current.epoch, internal_id, now_ms, credential_changes=[c])
            conn.execute('DELETE FROM mutations WHERE mutation_id=?', (internal_id,))
            conn.execute('INSERT INTO mutations VALUES(?,?,?)', (mutation_id, md, canonical(result)))
            return result
