"""Independent, single-request Gateway framing; dispatch only after real EOF."""
import json
import struct
import time
import sqlite3
from contextlib import contextmanager
from .job_registry import JobRegistry
from .controller_bridge import ControllerBridge
from .protocol import response
from .streaming import read_exact
from .telegram_identity import MAX_ENVELOPE_BYTES, Peer
from .unix_transport import SocketReader, check, local_budget, write_response, remaining


class GatewayTransport:
    def __init__(self, bridge, *, clock=time.time):
        check(type(bridge) is ControllerBridge)
        check(type(bridge.registry) is BoundedGatewayRegistry)
        check(bridge.identity.registry is bridge.registry)
        self.bridge, self.clock = bridge, clock

    def __call__(self, conn, peer, accepted):
        reader = SocketReader(conn, accepted, MAX_ENVELOPE_BYTES + 5)
        try:
            reader.header()
            size, = struct.unpack('!I', read_exact(reader, 4))
            check(0 < size <= MAX_ENVELOPE_BYTES)
            reader.payload()
            raw = read_exact(reader, size)
            raw.decode('utf-8', errors='strict')
            reader.eof()
            with local_budget(lambda: reader.deadline):
                result = self.bridge.handle(raw, Peer(peer.uid), int(self.clock()*1000))
            # Incremental encoding caps output before accumulating another huge copy.
            encoded = bytearray()
            for chunk in json.JSONEncoder(separators=(',', ':'), ensure_ascii=True).iterencode(result):
                check(len(encoded) + len(chunk) <= MAX_ENVELOPE_BYTES)
                encoded.extend(chunk.encode('ascii'))
        except Exception:
            encoded = json.dumps(response(None, 'storage_error', message='storage_error'),
                                 separators=(',', ':')).encode('ascii')
        write_response(conn, struct.pack('!I', len(encoded)) + encoded)


class BoundedGatewayRegistry(JobRegistry):
    """Same writer transaction semantics, with transport-local SQLite wait budget."""
    @contextmanager
    def transaction(self):
        self.guard.require_held()
        current = getattr(self._local, 'connection', None)
        if current is not None:
            yield current
            return
        conn = sqlite3.connect(self.db_path, timeout=remaining())
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA synchronous=FULL')
            self._local.connection = conn
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                yield conn
        finally:
            self._local.connection = None
            conn.close()
