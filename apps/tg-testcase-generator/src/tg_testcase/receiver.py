"""Offline v1 framed receiver. No sockets, discovery, paths, or network adapters.

Trusted jobs must be resolved by the controller independently of wire input.
Wire: 1-byte type + uint32 big-endian length + payload.
HELLO, PACKAGE, (ARTIFACT, CHUNK*, END)*, COMMIT, EOF.
"""
import hashlib
import json
import re
import struct
import sqlite3
from io import BytesIO

from .acquisition import OfflineAcquisition
from .notion_package import (require, fields, identifier, hash_value, load_package,
                             encode, MAX_JSON_BYTES, MAX_ATTACHMENT, reference_id)
from .streaming import CHUNK_SIZE, read_exact

HELLO, PACKAGE, ARTIFACT, CHUNK, END, COMMIT, ACK, ERROR = range(1, 9)
LIMITS = {HELLO: 4096, PACKAGE: MAX_JSON_BYTES, ARTIFACT: 512,
          CHUNK: CHUNK_SIZE, END: 0, COMMIT: 0, ACK: 1024, ERROR: 128}
BINDING = ('job_id', 'task_id', 'batch_id', 'source_id', 'revision',
           'root_type', 'canonical_root_id', 'attempt', 'fencing_token')


def frame(kind, payload=b''):
    require(kind in LIMITS and type(payload) is bytes and len(payload) <= LIMITS[kind],
            'Invalid frame')
    return struct.pack('!BI', kind, len(payload)) + payload


def read_frame(reader, allowed):
    kind, length = struct.unpack('!BI', read_exact(reader, 5))
    require(kind in allowed and kind in LIMITS, 'Out-of-order/unknown frame')
    require(length <= LIMITS[kind], 'Frame length limit')
    return kind, read_exact(reader, length)


def object_frame(payload):
    def unique(pairs):
        result = {}
        for k, v in pairs:
            require(k not in result, 'Duplicate field')
            result[k] = v
        return result
    try:
        return json.loads(payload, object_pairs_hook=unique)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError('Malformed frame') from exc


class ChunkReader:
    def __init__(self, reader):
        self.reader, self.buffer, self.ended = reader, b'', False

    def read(self, size):
        if not self.buffer and not self.ended:
            kind, data = read_frame(self.reader, {CHUNK, END})
            if kind == END:
                self.ended = True
            else:
                require(bool(data), 'Empty chunk')
                self.buffer = data
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        return result


class ReceiverAcquisition(OfflineAcquisition):
    def _commit_staged(self, claim, p, staged):
        self._require_protected()
        # A retry still consumes and validates the entire stream, including COMMIT.
        with self.store._locked() as conn:
            row = conn.execute('SELECT package_digest,content_fingerprint,id FROM source_packages '
                               'WHERE batch_id=? AND source_id=? AND revision=?',
                               tuple(claim[k] for k in ('batch_id', 'source_id', 'revision'))).fetchone()
            if row:
                self._check(conn, claim, {'sealed'})
                require(hashlib.sha256(encode(p).encode()).hexdigest() == row[0],
                        'Receipt digest conflict')
                return dict(package_id=row[2], package_digest=row[0], content_fingerprint=row[1])
        return super()._commit_staged(claim, p, staged)


def receive(acquisition, claim, trusted_job, reader):
    """Return a fixed ACK/ERROR object. trusted_job is protected registry data.

    Retry ACK requires the same still-live sealed claim; expired/replaced claims
    fail closed. Caller must retain the immutable job binding across ACK loss.
    """
    try:
        _, payload = read_frame(reader, {HELLO})
        h = object_frame(payload)
        fields(h, ('protocol_version', *BINDING, 'package_length', 'package_sha256'))
        require(type(h['protocol_version']) is int and h['protocol_version'] == 1, 'Protocol version')
        for k in ('job_id', 'task_id', 'batch_id', 'source_id'):
            identifier(h[k])
        for k in ('revision', 'attempt', 'fencing_token'):
            require(type(h[k]) is int and h[k] > 0, 'Invalid counter')
        require(h['root_type'] in {'page', 'database', 'data_source'}
                and type(h['canonical_root_id']) is str
                and re.fullmatch('[0-9a-f]{32}', h['canonical_root_id']), 'Invalid root')
        if acquisition.store.protected_acquisition:
            acquisition._require_protected()
            authoritative_job, authoritative_claim = acquisition.receiver_binding()
            require(all(trusted_job[k] == authoritative_job[k] for k in BINDING), 'Untrusted job binding')
            require(all(claim.get(k) == authoritative_claim[k] for k in
                        ('batch_id', 'source_id', 'revision', 'worker_id', 'attempt', 'fencing_token')), 'Untrusted claim')
            trusted_job, claim = authoritative_job, authoritative_claim
        require(all(h[k] == trusted_job[k] for k in BINDING), 'Job binding mismatch')
        require(all(h[k] == claim[k] for k in
                    ('batch_id', 'source_id', 'revision', 'attempt', 'fencing_token')), 'Claim mismatch')
        with acquisition.store._locked() as conn:
            source = acquisition._check(conn, claim, {'fetching', 'sealed'})
            require(source[0] == h['task_id'] and reference_id(source[1]) == h['canonical_root_id'],
                    'Registry binding mismatch')
        require(type(h['package_length']) is int and 0 <= h['package_length'] <= MAX_JSON_BYTES,
                'Package length limit')
        hash_value(h['package_sha256'])
        # Check declared length against header before allocating the package.
        kind, length = struct.unpack('!BI', read_exact(reader, 5))
        require(kind == PACKAGE and length == h['package_length'], 'Package frame mismatch')
        raw = read_exact(reader, length)
        require(hashlib.sha256(raw).hexdigest() == h['package_sha256'], 'Package digest mismatch')
        p = load_package(raw)
        require(raw == encode(p).encode(), 'Canonical package required')
        require(p['root']['type'] == h['root_type']
                and reference_id(p['root']['id']) == h['canonical_root_id'], 'Root binding mismatch')
        require(all(p[k] == h[k] for k in ('task_id', 'batch_id', 'source_id', 'revision')),
                'Package binding mismatch')

        def artifacts():
            declared = {a['id']: a for a in p['artifacts']}
            while True:
                kind, payload = read_frame(reader, {ARTIFACT, COMMIT})
                if kind == COMMIT:
                    require(reader.read(1) == b'', 'Trailing frame')
                    return
                a = object_frame(payload)
                fields(a, ('artifact_id', 'size', 'sha256'))
                identifier(a['artifact_id'])
                hash_value(a['sha256'])
                require(type(a['size']) is int and 0 <= a['size'] <= MAX_ATTACHMENT, 'Artifact limit')
                expected = declared.get(a['artifact_id'])
                require(expected is not None and (a['size'], a['sha256']) ==
                        (expected['size'], expected['sha256']), 'Artifact metadata mismatch')
                chunks = ChunkReader(reader)
                yield a['artifact_id'], chunks
                require(chunks.ended, 'Unconsumed artifact')
        acquisition._require_protected()
        importer = acquisition if acquisition.store.protected_acquisition else ReceiverAcquisition(acquisition.store, acquisition.clock)
        receipt = importer.seal_stream(claim, BytesIO(raw), len(raw), artifacts())
        return dict(protocol_version=1, type='ACK', receipt=receipt)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, sqlite3.Error) as exc:
        # Never reflect attacker-controlled input or internal exception text.
        return dict(protocol_version=1, type='ERROR', code='seal_rejected')
