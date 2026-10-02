"""Trusted peer injection and durable Telegram delivery ledgers; no transport."""
from copy import deepcopy
from dataclasses import dataclass
import secrets
from .contract_types import (canonical, digest, fields, integer, name, id32,
                             identifier, require, sha, strict_json)
from .internal_errors import ContractError, fail
from .protocol import validate_request

AUDIENCE = 'notion-controller-v1'
# Existing Application text permits 10 MiB of decoded UTF-8. JSON escaping can
# expand that sixfold. Reservations retain both original action and fixed request.
MAX_ENVELOPE_BYTES = 64 * 1024 * 1024
MAX_REPLAY_BYTES = 2 * MAX_ENVELOPE_BYTES + 65536


@dataclass(frozen=True)
class Peer:
    """Constructed by trusted transport/test harness, never decoded from an envelope."""
    uid: int

    def __post_init__(self):
        integer(self.uid, 0, 4294967294)


def validate_context(c):
    fields(c, 'issuer bot_instance telegram_user_id chat_id chat_type update_id issued_at expires_at nonce')
    name(c['issuer']); name(c['bot_instance']); id32(c['nonce'])
    integer(c['telegram_user_id'], 1, 4503599627370495)
    integer(c['chat_id'], -4503599627370495, 4503599627370495)
    require(c['chat_type'] == 'private' and c['chat_id'] == c['telegram_user_id'])
    integer(c['update_id'], 0, 2147483647)
    integer(c['issued_at']); integer(c['expires_at'])
    require(c['expires_at'] == c['issued_at'] + 60000)
    return c


def request_id(context):
    return 'tg-' + digest([context[k] for k in ('issuer', 'bot_instance', 'update_id')])


def application_request(envelope):
    c = envelope['auth_context']
    action = deepcopy(envelope['action'])
    require(type(action) is dict and 'actor' not in action and 'request_id' not in action)
    action.update(request_id=request_id(c), actor={
        'user_id': str(c['telegram_user_id']), 'chat_id': str(c['chat_id']), 'chat_type': 'private'})
    # The strict reference parser also accepts UUID URLs the old parser does not.
    # Normalize ONLY after action_digest was verified against the original action.
    if action.get('operation') == 'append_notion_reference':
        from .notion_reference import parse_notion_reference
        action['payload']['reference'] = parse_notion_reference(action['payload']['reference'])
    validate_request(action)
    if 'expected_version' in action:
        integer(action['expected_version'], 1)
    return action


class IdentityLedger:
    def __init__(self, registry, peers):
        """peers: protected {(issuer,bot): (uid,audience)}; never request data."""
        self.registry = registry
        self.peers = dict(peers)
        for (issuer, bot), (uid, audience) in self.peers.items():
            name(issuer); name(bot); integer(uid, 0, 4294967294)
            require(audience == AUDIENCE)

    def reserve(self, raw_envelope, peer, now_ms):
        try:
            integer(now_ms)
            e = strict_json(raw_envelope, limit=MAX_ENVELOPE_BYTES, depth=32)
            fields(e, 'audience auth_context action action_digest')
            c = validate_context(e['auth_context'])
            expected = self.peers.get((c['issuer'], c['bot_instance']))
            require(type(peer) is Peer and expected == (peer.uid, e['audience']) and e['audience'] == AUDIENCE)
            sha(e['action_digest']); require(digest(e['action']) == e['action_digest'])
            req = application_request(e)
        except ContractError:
            raise
        except (ValueError, KeyError, TypeError):
            fail('AUTH_INVALID')
        with self.registry.guard.hold():
            # Persist every authenticated clock observation separately, including
            # retries and expired deliveries. A later failed reservation must not
            # roll back the high-water mark and hide a wall-clock reversal.
            with self.registry.transaction() as conn:
                clock = conn.execute('SELECT high_water FROM clock_state WHERE singleton=1').fetchone()
                rollback = bool(clock and now_ms + 5000 < clock[0])
                conn.execute('INSERT INTO clock_state VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET high_water=max(high_water,excluded.high_water)', (now_ms,))
            with self.registry.transaction() as conn:
                rows = conn.execute('SELECT payload FROM replay WHERE issuer=? AND bot_instance=? AND (nonce=? OR update_id=?)',
                                    (c['issuer'], c['bot_instance'], c['nonce'], c['update_id'])).fetchall()
                if rows:
                    old = strict_json(rows[0][0], limit=MAX_REPLAY_BYTES, depth=32)
                    if len(rows) != 1 or old['envelope'] != e:
                        fail('AUTH_REPLAY')
                    return old
                if rollback:
                    fail('AUTH_INVALID')
                if not (c['issued_at'] <= now_ms + 5000 and now_ms < c['expires_at'] + 5000):
                    fail('AUTH_INVALID')
                row = dict(envelope=e, request=req, request_id=req['request_id'],
                           action_digest=e['action_digest'], first_seen_at=now_ms,
                           retain_until=max(now_ms, c['expires_at']) + 86400000,
                           state='reserved', response_digest=None, result_ref=None)
                integer(row['retain_until'])
                conn.execute('INSERT INTO replay VALUES(?,?,?,?,?,?)',
                             (c['issuer'], c['bot_instance'], c['nonce'], c['update_id'], req['request_id'], canonical(row)))
                return row

    def complete(self, rid, result, *, rejected=False):
        identifier(rid)
        with self.registry.guard.hold(), self.registry.transaction() as conn:
            row = conn.execute('SELECT payload FROM replay WHERE request_id=?', (rid,)).fetchone()
            if not row:
                fail('AUTH_INVALID')
            record = strict_json(row[0], limit=MAX_REPLAY_BYTES, depth=32)
            rd = digest(result)
            if record['state'] != 'reserved':
                if record['response_digest'] != rd:
                    fail('REQUEST_CONFLICT')
                return record
            record.update(state='rejected' if rejected else 'completed', response_digest=rd, result_ref=rid)
            conn.execute('UPDATE replay SET payload=? WHERE request_id=?', (canonical(record), rid))
            return record

    def gc(self, now_ms):
        integer(now_ms)
        with self.registry.guard.hold(), self.registry.transaction() as conn:
            for rid, raw in conn.execute('SELECT request_id,payload FROM replay').fetchall():
                row = strict_json(raw, limit=MAX_REPLAY_BYTES, depth=32)
                if row['state'] != 'reserved' and now_ms >= row['retain_until']:
                    conn.execute('DELETE FROM replay WHERE request_id=?', (rid,))


class GatewayLedger:
    """Called only with an already authenticated update, never with body actor IDs.

    An unhandled update is never garbage collected. Once expired it is discarded,
    not re-signed. Persistent records preserve the exact original envelope.
    """
    def __init__(self, registry, issuer, bot_instance):
        self.registry = registry
        self.issuer, self.bot = name(issuer), name(bot_instance)

    def envelope(self, update_id, user_id, action, issued_at, now_ms):
        integer(now_ms)
        with self.registry.guard.hold(), self.registry.transaction() as conn:
            row = conn.execute('SELECT payload FROM gateway_updates WHERE issuer=? AND bot_instance=? AND update_id=?',
                               (self.issuer, self.bot, update_id)).fetchone()
            if row:
                old = strict_json(row[0], limit=MAX_ENVELOPE_BYTES, depth=32)
                if old['action'] != action or old['auth_context']['telegram_user_id'] != user_id:
                    fail('AUTH_REPLAY')
                if now_ms >= old['auth_context']['expires_at'] + 5000:
                    fail('AUTH_INVALID')
                return old
            c = dict(issuer=self.issuer, bot_instance=self.bot, telegram_user_id=user_id,
                     chat_id=user_id, chat_type='private', update_id=update_id,
                     issued_at=issued_at, expires_at=issued_at + 60000, nonce=secrets.token_hex(16))
            validate_context(c)
            require(issued_at <= now_ms + 5000 and now_ms < c['expires_at'] + 5000)
            e = dict(audience=AUDIENCE, auth_context=c, action=deepcopy(action), action_digest=digest(action))
            application_request(e)
            require(len(canonical(e)) <= MAX_ENVELOPE_BYTES)
            conn.execute('INSERT INTO gateway_updates VALUES(?,?,?,?)', (self.issuer, self.bot, update_id, canonical(e)))
            return e
