"""Synthetic offline streaming and receiver acceptance."""
import hashlib
import sqlite3
import struct
import tracemalloc
import unittest
from io import BytesIO
from unittest.mock import patch

import test_notion_package as fixtures
from test_notion_package import artifact, raw, ROOT
from tg_testcase import notion_package as n
from tg_testcase.streaming import CHUNK_SIZE
from tg_testcase.receiver import *


class StreamingTests(unittest.TestCase):
    setUp = fixtures.AcquisitionTests.setUp
    tearDown = fixtures.AcquisitionTests.tearDown
    send = fixtures.AcquisitionTests.send
    setup_source = fixtures.AcquisitionTests.setup_source
    sql = fixtures.AcquisitionTests.sql

    def prepare(self):
        p = self.setup_source()
        c = self.acq.claim(self.bid, self.sid, 'controller')
        self.acq.transition(c, 'fetching')
        return p, c

    def stream(self, p, c, pairs):
        b = raw(p)
        return self.acq.seal_stream(c, BytesIO(b), len(b), pairs)

    def empty(self):
        self.assertEqual(self.sql('SELECT count(*) FROM source_packages')[0][0], 0)
        self.assertEqual(self.sql('SELECT count(*) FROM source_package_artifacts')[0][0], 0)
        self.assertEqual(list(self.store.root.glob('seal-*')), [])

    def test_stream_multiple_and_bounded_memory(self):
        p, c = self.prepare()
        size = 2 * 1024 * 1024
        for aid in ('a', 'b', 'c'):
            artifact(p, b'x' * size, 'pdf', aid)
        class Reader:
            def __init__(self):
                self.left = size
            def read(self, count):
                assert count <= CHUNK_SIZE
                amount = min(count, self.left)
                self.left -= amount
                return b'x' * amount
        def inputs():
            for aid in ('a', 'b', 'c'):
                reader = Reader()
                yield aid, reader
                self.assertEqual(reader.left, 0)
        tracemalloc.start()
        self.stream(p, c, inputs())
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        # blobopen uses chunks; Python 3.10 fallback stays near one artifact.
        self.assertLess(peak, 5 * size)
        self.assertEqual(self.sql('SELECT sum(size) FROM source_package_artifacts')[0][0], 3 * size)
        self.assertEqual(list(self.store.root.glob('seal-*')), [])

    def test_bad_artifact_streams_cleanup(self):
        p, c = self.prepare()
        artifact(p, b'abc', 'pdf')
        for pairs in ([], [('extra', BytesIO(b''))],
                      [('a', BytesIO(b'ab'))], [('a', BytesIO(b'abcd'))],
                      [('a', BytesIO(b'xyz'))],
                      [('a', BytesIO(b'abc')), ('a', BytesIO(b'abc'))]):
            with self.subTest(pairs=pairs), self.assertRaises(ValueError):
                self.stream(p, c, pairs)
            self.empty()

    def test_declared_limits_before_read(self):
        p, c = self.prepare()
        class Never:
            def read(self, n):
                raise AssertionError('must reject before read')
        with self.assertRaises(ValueError):
            self.acq.seal_stream(c, Never(), n.MAX_JSON_BYTES + 1, [])
        artifact(p, b'x', 'pdf')
        p['artifacts'][0]['size'] = n.MAX_ATTACHMENT + 1
        with self.assertRaises(ValueError):
            self.stream(p, c, [('a', Never())])
        p['artifacts'][0]['size'] = 1
        with patch.object(n, 'MAX_ATTACHMENTS_BYTES', 0), self.assertRaises(ValueError):
            self.stream(p, c, [('a', Never())])
        self.empty()

    def test_truncated_package(self):
        p, c = self.prepare()
        b = raw(p)
        with self.assertRaises(ValueError):
            self.acq.seal_stream(c, BytesIO(b[:-1]), len(b), [])
        self.empty()

    def test_commit_rechecks_during_input(self):
        p, c = self.prepare()
        for change in ('fencing_token', 'revision', 'lease'):
            def inputs():
                if change == 'lease':
                    self.now += 100
                else:
                    c[change] += 1
                yield from ()
            with self.assertRaises(ValueError):
                self.stream(p, c, inputs())
            if change != 'lease':
                c[change] -= 1
            self.empty()

    def test_cancel_during_input(self):
        p, c = self.prepare()
        def inputs():
            self.send('cancel')
            yield from ()
        with self.assertRaises(ValueError):
            self.stream(p, c, inputs())
        self.empty()

    def test_sqlite_rollback_cleanup(self):
        p, c = self.prepare()
        artifact(p, b'x', 'pdf')
        from tg_testcase.streaming import write_blob
        def fail(*args):
            write_blob(*args)
            raise sqlite3.OperationalError('synthetic')
        with patch('tg_testcase.streaming.write_blob', side_effect=fail), self.assertRaises(sqlite3.Error):
            self.stream(p, c, [('a', BytesIO(b'x'))])
        self.empty()

    def test_failure_root_types(self):
        p, c = self.prepare()
        # rollback each synthetic root receipt to exercise both trusted types
        for typ in ('database', 'data_source'):
            with patch.object(self.acq, 'seal', side_effect=lambda c, b, a: n.load_package(b)) as seal:
                result = self.acq.seal_failure(c | {'root_type': typ})
                self.assertEqual(result['root']['type'], typ)

    def wire(self, p, c, data=b'abc'):
        b = raw(p)
        job = {k: c[k] for k in ('batch_id', 'source_id', 'revision', 'attempt', 'fencing_token')}
        job.update(job_id='job', task_id=p['task_id'], root_type='page', canonical_root_id=ROOT)
        h = dict(protocol_version=1, **job, package_length=len(b), package_sha256=hashlib.sha256(b).hexdigest())
        parts = [frame(HELLO, encode(h).encode()), frame(PACKAGE, b)]
        for a in p['artifacts']:
            parts += [frame(ARTIFACT, encode(dict(artifact_id=a['id'], size=a['size'], sha256=a['sha256'])).encode()),
                      frame(CHUNK, data), frame(END)]
        parts += [frame(COMMIT)]
        return job, parts

    def test_receiver_retry_and_conflict(self):
        p, c = self.prepare()
        artifact(p, b'abc', 'pdf')
        job, parts = self.wire(p, c)
        first = receive(self.acq, c, job, BytesIO(b''.join(parts)))
        self.assertEqual(first['type'], 'ACK')
        self.assertEqual(receive(self.acq, c, job, BytesIO(b''.join(parts))), first)
        p['nodes'][0]['text'] = 'different'
        job, parts = self.wire(p, c)
        self.assertEqual(receive(self.acq, c, job, BytesIO(b''.join(parts)))['type'], 'ERROR')

    def test_receiver_malformed_order_truncation_duplicate(self):
        p, c = self.prepare()
        artifact(p, b'abc', 'pdf')
        job, parts = self.wire(p, c)
        bad = [b''.join(parts)[:-1], frame(HELLO, b'{'),
               parts[1] + parts[0], b''.join(parts[:2] + parts[2:5] * 2 + parts[5:]),
               b''.join(parts) + frame(COMMIT), b''.join(parts[:2]) + frame(CHUNK, b'x')]
        for wire in bad:
            self.assertEqual(receive(self.acq, c, job, BytesIO(wire))['type'], 'ERROR')
            self.empty()

    def test_oversized_before_allocation(self):
        class HeaderOnly:
            def read(self, size):
                if size == 5:
                    return struct.pack('!BI', HELLO, 4097)
                raise AssertionError('payload requested')
        with self.assertRaises(ValueError):
            read_frame(HeaderOnly(), {HELLO})

    def test_untrusted_binding(self):
        p, c = self.prepare()
        job, parts = self.wire(p, c)
        for key in BINDING:
            other = dict(job)
            other[key] = 'wrong'
            self.assertEqual(receive(self.acq, c, other, BytesIO(b''.join(parts)))['type'], 'ERROR')
        self.empty()

    def test_full_source_budget_memory(self):
        p, c = self.prepare()
        size = n.MAX_ATTACHMENT
        sha = hashlib.sha256()
        block = b'x' * CHUNK_SIZE
        for _ in range(size // CHUNK_SIZE):
            sha.update(block)
        for index in range(10):
            p['artifacts'].append(dict(id='a'+str(index), kind='pdf', path='artifacts/a'+str(index),
                                       size=size, sha256=sha.hexdigest()))
        class Reader:
            left = size
            def read(self, count):
                assert count <= CHUNK_SIZE
                amount = min(self.left, count)
                self.left -= amount
                return block[:amount]
        tracemalloc.start()
        self.stream(p, c, (('a'+str(i), Reader()) for i in range(10)))
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, 2 * size + 1024 * 1024)
        self.assertEqual(self.sql('SELECT sum(size) FROM source_package_artifacts')[0][0], n.MAX_ATTACHMENTS_BYTES)

    def test_batch_budget_before_input_and_at_commit(self):
        p, c = self.prepare()
        with patch('tg_testcase.acquisition.MAX_BATCH_BYTES', 1), self.assertRaises(ValueError):
            self.stream(p, c, [])
        original = self.acq._commit_staged
        def commit(*args):
            with patch('tg_testcase.acquisition.MAX_BATCH_BYTES', 1):
                return original(*args)
        with patch.object(self.acq, '_commit_staged', side_effect=commit), self.assertRaises(ValueError):
            self.stream(p, c, [])
        self.empty()

    def test_incremental_blob_primitive(self):
        from tg_testcase.streaming import write_blob
        class Blob:
            size = 0
            maximum = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def write(self, data):
                self.size += len(data)
                self.maximum = max(self.maximum, len(data))
        class Conn:
            lastrowid = 1
            blob = Blob()
            def execute(self, sql, values):
                assert 'zeroblob' in sql
                return self
            def blobopen(self, table, column, row):
                assert (table, column, row) == ('source_package_artifacts', 'body', 1)
                return self.blob
        conn = Conn()
        body = b'x' * (CHUNK_SIZE * 3 + 1)
        a = dict(id='a', kind='pdf', path='artifacts/a', sha256=hashlib.sha256(body).hexdigest(), size=len(body))
        write_blob(conn, 'p', a, BytesIO(body))
        self.assertEqual(conn.blob.size, len(body))
        self.assertLessEqual(conn.blob.maximum, CHUNK_SIZE)

    def test_receiver_fixed_sqlite_error(self):
        p, c = self.prepare()
        job, parts = self.wire(p, c)
        with patch('tg_testcase.acquisition.seal_inputs', side_effect=sqlite3.OperationalError('synthetic')):
            result = receive(self.acq, c, job, BytesIO(b''.join(parts)))
        self.assertEqual(result, dict(protocol_version=1, type='ERROR', code='seal_rejected'))
        self.empty()

    def test_receiver_header_duplicate_and_forbidden_fields(self):
        p, c = self.prepare()
        job, parts = self.wire(p, c)
        h = object_frame(parts[0][5:])
        for payload in (b'{"protocol_version":1,"protocol_version":1}',
                        encode(h | {'path': '/synthetic'}).encode(),
                        encode(h | {'method': 'execute'}).encode()):
            wire = frame(HELLO, payload) + b''.join(parts[1:])
            self.assertEqual(receive(self.acq, c, job, BytesIO(wire))['type'], 'ERROR')
        self.empty()

    def test_source_declaration_over_100mib(self):
        p, c = self.prepare()
        for index in range(11):
            p['artifacts'].append(dict(id='a'+str(index), kind='pdf', path='artifacts/a'+str(index),
                                       size=n.MAX_ATTACHMENT, sha256='0'*64))
        with self.assertRaisesRegex(ValueError, 'Attachment total limit'):
            self.stream(p, c, [])
        self.empty()
