"""Strict explicit-allow policy validation, canonicalization and root resolution."""
from dataclasses import dataclass
from .contract_types import (fields, integer, name, principal, id32, require,
                             strict_json, canonical, digest)
from .internal_errors import ContractError, fail

OPERATIONS = {'append_notion_reference', 'acquire', 'refresh', 'cancel'}


def _map(value, key_validator):
    require(type(value) is dict and len(value) <= 1024)
    for key in value:
        key_validator(key)


def _set(value, validator):
    require(type(value) is list and 1 <= len(value) <= 1024)
    for item in value:
        validator(item)
    require(len(set(value)) == len(value))
    value.sort()


def verification(value):
    fields(value, 'verification_ref workspace_ref status record_version')
    name(value['verification_ref']); name(value['workspace_ref'])
    require(value['status'] in ('verified', 'revoked'))
    integer(value['record_version'], 1)
    return value


@dataclass(frozen=True)
class ScopeBinding:
    principal: str
    workspace_ref: str
    grant_ref: str
    credential_ref: str
    credential_generation: int
    policy_epoch: int
    policy_digest: str
    root_type: str
    root_id: str


class Policy:
    def __init__(self, normalized):
        self._bytes = canonical(normalized)
        self.digest = digest(normalized)
        self.epoch = normalized['policy_epoch']

    @property
    def canonical_bytes(self):
        return self._bytes

    @property
    def data(self):
        return strict_json(self._bytes, ascii_only=True)

    @classmethod
    def parse(cls, raw, verifications, credentials):
        try:
            p = strict_json(raw, ascii_only=True)
            fields(p, 'schema_version policy_version policy_epoch default_effect principals grants workspaces credentials')
            integer(p['schema_version'], 1, 1); name(p['policy_version']); integer(p['policy_epoch'], 1)
            require(p['default_effect'] == 'deny')
            for key, validator in [('principals', principal), ('grants', name), ('workspaces', name), ('credentials', name)]:
                _map(p[key], validator)
            for ref, w in p['workspaces'].items():
                fields(w, 'verification_ref'); name(w['verification_ref'])
                v = verification(verifications[w['verification_ref']])
                require(v['verification_ref'] == w['verification_ref'] and v['workspace_ref'] == ref and v['status'] == 'verified')
            for ref, c in p['credentials'].items():
                fields(c, 'workspace_ref secret_handle generation enabled')
                name(c['workspace_ref']); name(c['secret_handle']); integer(c['generation'], 1)
                require(type(c['enabled']) is bool and c['workspace_ref'] in p['workspaces'])
                head = credentials[ref]
                require(not head['deleted'] and all(head[k] == c[k] for k in c))
            for g in p['grants'].values():
                fields(g, 'workspace_ref credential_ref roots relations synced_references links external_attachments limits_profile')
                name(g['workspace_ref']); name(g['credential_ref'])
                require(g['workspace_ref'] in p['workspaces'] and g['credential_ref'] in p['credentials'])
                require(p['credentials'][g['credential_ref']]['workspace_ref'] == g['workspace_ref'])
                require(all(g[k] == 'deny_follow' for k in ('relations', 'synced_references', 'links')))
                require(g['external_attachments'] == 'deny' and g['limits_profile'] == 'notion-standard')
                require(type(g['roots']) is list and 1 <= len(g['roots']) <= 1024)
                roots = {}
                for root in g['roots']:
                    require(type(root) is dict)
                    kind = root.get('type')
                    if kind == 'page':
                        fields(root, 'type id descendants'); require(root['descendants'] == 'structural')
                    elif kind == 'database':
                        fields(root, 'type id data_sources'); fields(root['data_sources'], 'mode ids')
                        require(root['data_sources']['mode'] == 'explicit'); _set(root['data_sources']['ids'], id32)
                    elif kind == 'data_source':
                        fields(root, 'type id expected_database_id'); id32(root['expected_database_id'])
                    else:
                        raise ValueError()
                    id32(root['id']); key = (kind, root['id'])
                    require(key not in roots); roots[key] = root
                for (kind, rid), root in roots.items():
                    if kind == 'database':
                        for sid in root['data_sources']['ids']:
                            require(roots[('data_source', sid)]['expected_database_id'] == rid)
                    if kind == 'data_source':
                        require(rid in roots[('database', root['expected_database_id'])]['data_sources']['ids'])
                g['roots'].sort(key=lambda r: (r['type'], r['id']))
            for rule in p['principals'].values():
                fields(rule, 'operations grants')
                _set(rule['operations'], lambda op: require(type(op) is str and op in OPERATIONS))
                _set(rule['grants'], name)
                seen = set()
                for ref in rule['grants']:
                    require(ref in p['grants'])
                    for root in p['grants'][ref]['roots']:
                        if root['id'] in seen:
                            fail('ROOT_AMBIGUOUS')
                        seen.add(root['id'])
            return cls(p)
        except ContractError:
            raise
        except (ValueError, TypeError, KeyError):
            fail('POLICY_INVALID')

    def resolve(self, actor, operation, root_id):
        p = self.data
        rule = p['principals'].get(actor)
        if not rule or operation not in rule['operations']:
            fail('SCOPE_DENIED')
        matches = [(ref, p['grants'][ref], root) for ref in rule['grants']
                   for root in p['grants'][ref]['roots'] if root['id'] == root_id]
        if not matches:
            fail('SCOPE_DENIED')
        if len(matches) != 1:
            fail('ROOT_AMBIGUOUS')
        ref, grant, root = matches[0]
        c = p['credentials'][grant['credential_ref']]
        if not c['enabled']:
            fail('CREDENTIAL_STALE')
        return ScopeBinding(actor, grant['workspace_ref'], ref, grant['credential_ref'],
                            c['generation'], self.epoch, self.digest, root['type'], root_id)
