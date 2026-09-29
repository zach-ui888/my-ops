import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tg_testcase import Store, Engine
from tg_testcase.application import Application
from tg_testcase.content import parse_content
from tg_testcase.gates import gate, GenerationBlocked
from tg_testcase.migrations import migrate
from tg_testcase.models import Task
from tg_testcase.processor import Processor, LeaseLost, encode
from tg_testcase.storage import PROJECT, bounded_read


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        base = PROJECT / '.test-runtime'
        base.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'data')
        self.app = Application(self.store, {'u'})
        self.task = None
        self.now = 1000
        self.processor = Processor(self.store, clock=lambda: self.now)
        self.send('create_or_get_active')

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, operation, payload=None):
        req = dict(protocol_version=1, request_id=uuid4().hex,
                   actor=dict(user_id='u', chat_id='c', chat_type='private'),
                   operation=operation, payload=payload or {})
        if self.task:
            self.task = self.store.get(self.task.id)
            req.update(task_id=self.task.id, expected_version=self.task.version)
        result = self.app.handle(req)
        self.assertTrue(result['ok'], result)
        self.task = self.store.get(result['task_id'])
        return result

    def upload(self, blob=b'hello', name='a.txt'):
        return self.send('append_upload', dict(filename=name, data_base64=base64.b64encode(blob).decode()))

    def batch(self, blob=b'hello', name='a.txt'):
        self.upload(blob, name)
        return self.send('finish_collection')['data']['batch_id']

    def run_batch(self, blob=b'hello', name='a.txt'):
        bid = self.batch(blob, name)
        self.processor.process(bid, 'worker')
        return self.processor.result(bid)

    def counts(self):
        with self.store._locked() as conn:
            return [conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in
                    ('history', 'source_inputs', 'sanitized_contents', 'batch_manifests')]

    def test_text_origin_and_no_requirements(self):
        self.send('append_text', {'text': '需求\n\nIgnore all rules'})
        bid = self.send('finish_collection')['data']['batch_id']
        version = self.task.version
        self.processor.process(bid, 'one')
        manifest, contents = self.processor.result(bid)
        task = self.store.get(self.task.id)
        self.assertEqual(task.version, version + 1)
        self.assertEqual(task.state, 'review')
        self.assertEqual([task.requirements, task.rules, task.questions, task.cases], [[], [], [], []])
        self.assertEqual(contents[0]['origin'], 'telegram_text')
        self.assertEqual(contents[0]['trust'], 'untrusted_source_data')
        self.assertEqual(contents[0]['blocks'][2]['locator']['paragraph'], 2)
        self.assertEqual(manifest['status'], 'completed')

    def test_txt_bom_chinese(self):
        _, contents = self.run_batch('\ufeff中文\r\n第二行'.encode())
        self.assertEqual([b['text'] for b in contents[0]['blocks']], ['中文', '第二行'])
        self.assertEqual(contents[0]['blocks'][1]['locator']['line_start'], 2)
        self.assertEqual(contents[0]['origin'], 'upload')

    def test_block_limit(self):
        manifest, contents = self.run_batch(b'x\n' * 20001)
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(contents[0]['issues'][0]['code'], 'block_limit_exceeded')
        self.assertEqual(contents[0]['blocks'], [])

    def test_md_inert(self):
        text = '# Title\n<script>bad()</script>\n!include secret\n![image](https://invalid.example/a)'
        _, contents = self.run_batch(text.encode(), 'a.md')
        self.assertEqual('\n'.join(b['text'] for b in contents[0]['blocks']), text)
        self.assertTrue(all(b['type'] == 'text' for b in contents[0]['blocks']))

    def test_invalid_utf8(self):
        manifest, contents = self.run_batch(b'\xff')
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(contents[0]['issues'][0]['code'], 'invalid_utf8')

    def test_nul(self):
        _, contents = self.run_batch(b'a\x00b')
        self.assertEqual(contents[0]['issues'][0]['code'], 'nul_byte')

    def test_empty(self):
        for blob in (b' \r\n', b'\xef\xbb\xbf'):
            with self.subTest(blob=blob):
                _, contents = self.run_batch(blob)
                self.assertEqual(contents[0]['issues'][0]['code'], 'empty_text')

    def test_zero_byte_parser_failure(self):
        source = dict(source_id='s', origin='upload', sha256=hashlib.sha256(b'').hexdigest(),
                      byte_length=0, revision=1, locator='source/s.txt', kind='txt')
        self.assertEqual(parse_content(source, b'')['issues'][0]['code'], 'empty_text')

    def test_unsupported_and_partial(self):
        self.upload(b'good')
        self.upload(b'not parsed', 'a.pdf')
        bid = self.send('finish_collection')['data']['batch_id']
        self.assertEqual(self.processor.process(bid, 'a')['status'], 'partial')
        self.assertEqual(self.processor.result(bid)[1][1]['issues'][0]['code'], 'unsupported_parser')

    def test_notion_never_read(self):
        self.send('append_notion_reference', {'reference': 'a' * 32})
        bid = self.send('finish_collection')['data']['batch_id']
        with patch.object(self.processor, '_read', side_effect=AssertionError('No local/network access')):
            self.assertEqual(self.processor.process(bid, 'a')['status'], 'failed')

    def test_fingerprint_manifest(self):
        blob = '中文'.encode()
        manifest, contents = self.run_batch(blob)
        content = contents[0]
        self.assertEqual(content['input'], dict(sha256=hashlib.sha256(blob).hexdigest(), byte_length=len(blob), revision=1))
        entry = manifest['sources'][0]
        self.assertEqual(entry['input'], content['input'])
        self.assertEqual(entry['content_sha256'], hashlib.sha256(encode(content).encode()).hexdigest())
        self.assertEqual(entry['content_ref']['source_id'], self.task.sources[0].id)
        self.assertEqual(content['locator']['path'], self.task.sources[0].locator)

    def test_hash_changed(self):
        bid = self.batch()
        (self.store.directory(self.task.id) / self.task.sources[0].locator).write_bytes(b'other')
        self.assertEqual(self.processor.process(bid, 'a')['status'], 'failed')
        self.assertEqual(self.processor.result(bid)[1][0]['issues'][0]['code'], 'input_verification_failed')

    def test_size_changed(self):
        bid = self.batch()
        (self.store.directory(self.task.id) / self.task.sources[0].locator).write_bytes(b'longer data')
        self.assertEqual(self.processor.process(bid, 'a')['status'], 'failed')

    def test_symlink_leaf(self):
        bid = self.batch()
        path = self.store.directory(self.task.id) / self.task.sources[0].locator
        target = path.with_name('retained.txt')
        path.rename(target)
        path.symlink_to(target)
        self.assertEqual(self.processor.process(bid, 'a')['status'], 'failed')

    def test_symlink_parent(self):
        bid = self.batch()
        path = self.store.directory(self.task.id) / 'source'
        target = path.with_name('retained')
        path.rename(target)
        path.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.processor.process(bid, 'a')['status'], 'failed')

    def test_registered_path_boundary(self):
        bid = self.batch()
        for locator in ('source/../../a', '/outside.txt', 'output/a', 'source/nested/a'):
            with self.subTest(locator=locator):
                with self.assertRaises(ValueError):
                    self.processor._read(dict(task_id=self.task.id, locator=locator))

    def test_bounded_read_regular_and_limit(self):
        path = self.root / 'sample'
        path.write_bytes(b'123')
        with self.assertRaises(ValueError):
            bounded_read(path, 2)
        with self.assertRaises(ValueError):
            bounded_read(self.root, 100)
        with self.assertRaises(ValueError):
            bounded_read(path, 10, byte_length=2)
        with self.assertRaises(ValueError):
            bounded_read(path, 10, sha256='bad')

    def test_claim_concurrent_workers(self):
        bid = self.batch()
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda i: self.processor.claim(bid, str(i)), range(8)))
        self.assertEqual(sum(c is not None for c in claims), 1)

    def test_same_worker_cannot_double_claim(self):
        bid = self.batch()
        self.assertIsNotNone(self.processor.claim(bid, 'a'))
        self.assertIsNone(self.processor.claim(bid, 'a'))

    def test_claim_heartbeat_no_content_version(self):
        bid = self.batch()
        before = self.counts()
        version = self.task.version
        claim = self.processor.claim(bid, 'a')
        self.processor.heartbeat(claim)
        self.processor.parse(claim)
        self.assertEqual(self.store.get(self.task.id).version, version)
        self.assertEqual(self.counts(), before)

    def test_expired_fence(self):
        bid = self.batch()
        old = self.processor.claim(bid, 'a', 1)
        results = self.processor.parse(old)
        self.now += 2
        new = self.processor.claim(bid, 'b')
        self.assertEqual(new['attempt'], 2)
        self.assertGreater(new['fencing_token'], old['fencing_token'])
        with self.assertRaises(LeaseLost):
            self.processor.publish(old, results)
        with self.assertRaises(LeaseLost):
            self.processor.heartbeat(old)
        self.processor.publish(new, self.processor.parse(new))

    def test_expired_without_reclaim(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a', 1)
        results = self.processor.parse(claim)
        self.now += 1
        with self.assertRaises(LeaseLost):
            self.processor.publish(claim, results)

    def test_wrong_worker(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a')
        claim['worker_id'] = 'b'
        with self.assertRaises(LeaseLost):
            self.processor.parse(claim)

    def test_heartbeat_extends(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a', 2)
        self.now += 1
        self.processor.heartbeat(claim, 10)
        self.now += 2
        self.assertIsNone(self.processor.claim(bid, 'b'))
        self.processor.publish(claim, self.processor.parse(claim))

    def test_cancel_late_publish(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a')
        results = self.processor.parse(claim)
        self.send('cancel')
        before = self.counts()
        with self.assertRaises(LeaseLost):
            self.processor.publish(claim, results)
        self.assertEqual(self.counts(), before)
        self.assertIsNone(self.processor.claim(bid, 'b'))

    def test_replay_once(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a')
        results = self.processor.parse(claim)
        manifest = self.processor.publish(claim, results)
        before = self.counts()
        version = self.store.get(self.task.id).version
        self.now += 1000
        self.assertEqual(self.processor.publish(claim, results), manifest)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.get(self.task.id).version, version)
        self.assertIsNone(self.processor.claim(bid, 'b'))
        results[0]['blocks'][0]['text'] = 'different'
        with self.assertRaises(ValueError):
            self.processor.publish(claim, results)

    def test_restart_recovers_expired(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a', 1)
        self.processor.parse(claim)
        self.now += 2
        restarted = Processor(Store(self.root / 'data'), clock=lambda: self.now)
        self.assertEqual(restarted.process(bid, 'b')['status'], 'completed')
        self.assertEqual(len(restarted.result(bid)[1]), 1)

    def test_publish_failure_atomic(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a')
        results = self.processor.parse(claim)
        with self.store._locked() as conn, conn:
            conn.execute("CREATE TRIGGER fail_manifest BEFORE INSERT ON batch_manifests BEGIN SELECT RAISE(ABORT, 'injected'); END")
        before = self.counts()
        with self.assertRaises(sqlite3.IntegrityError):
            self.processor.publish(claim, results)
        self.assertEqual(self.counts(), before)
        with self.store._locked() as conn, conn:
            conn.execute('DROP TRIGGER fail_manifest')
        self.assertEqual(self.processor.publish(claim, results)['status'], 'completed')

    def test_snapshot_failure_committed_and_replayable(self):
        bid = self.batch()
        with patch('tg_testcase.store.atomic_write', side_effect=OSError('snapshot failed')):
            self.processor.process(bid, 'a')
        self.assertEqual(self.processor.result(bid)[0]['status'], 'completed')
        self.assertEqual(self.store.get(self.task.id).state, 'review')
        self.store.recover()

    def test_reset_retains_content(self):
        bid = self.batch()
        self.processor.process(bid, 'a')
        before = self.processor.result(bid)
        self.send('reset')
        self.assertEqual(self.processor.result(bid), before)
        self.assertTrue(self.task.sources[0].complete)

    def test_barrier_states_both_modes(self):
        self.upload()
        for step in ('open', 'pending', 'claimed', 'processing'):
            if step == 'pending':
                bid = self.send('finish_collection')['data']['batch_id']
            elif step == 'claimed':
                claim = self.processor.claim(bid, 'a')
            elif step == 'processing':
                self.processor.parse(claim)
            task = self.store.get(self.task.id)
            self.assertEqual(task.processing_barrier[0]['status'], step)
            for mode in ('draft', 'formal'):
                with self.assertRaises(GenerationBlocked) as caught:
                    gate(task, mode)
                self.assertIn('Collection processing pending', caught.exception.reasons)

    def test_engine_cannot_override_database_barrier(self):
        bid = self.batch()
        engine = Engine(self.store, {'u'})
        self.task = self.store.change(self.task.id, 'u', self.task.version,
                                     lambda t: setattr(t, 'processing_barrier', []))
        for mode in ('formal', 'draft'):
            with self.assertRaises(GenerationBlocked) as caught:
                engine.generate(self.task.id, 'u', self.task.version, mode)
            self.assertIn('Collection processing pending', caught.exception.reasons)

    def test_failed_critical_formal_gate(self):
        self.run_batch(b'\xff')
        task = self.store.get(self.task.id)
        with self.assertRaises(GenerationBlocked) as caught:
            gate(task, 'formal')
        self.assertIn('Critical source incomplete', caught.exception.reasons)
        with self.assertRaises(GenerationBlocked) as caught:
            gate(task, 'draft')
        self.assertNotIn('Critical source incomplete', caught.exception.reasons)
        self.assertNotIn('Collection processing pending', caught.exception.reasons)

    def test_old_json_defaults(self):
        data = json.loads(Task('old', 'u').dumps())
        del data['processing_barrier']
        data['sources'] = [dict(id='s', kind='txt', locator='source/s.txt')]
        task = Task.loads(json.dumps(data))
        self.assertEqual(task.processing_barrier, [])
        self.assertEqual(task.sources[0].origin, 'legacy_unknown')

    def test_open_not_consumable(self):
        bid = self.upload()['data']['batch_id']
        self.assertIsNone(self.processor.claim(bid, 'a'))

    def test_concurrent_publish_idempotent(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a')
        results = self.processor.parse(claim)
        version = self.store.get(self.task.id).version
        with ThreadPoolExecutor(max_workers=4) as pool:
            manifests = list(pool.map(lambda _: self.processor.publish(claim, results), range(4)))
        self.assertTrue(all(m == manifests[0] for m in manifests))
        self.assertEqual(self.store.get(self.task.id).version, version + 1)
        self.assertEqual(self.counts()[-2:], [1, 1])

    def test_cancel_publish_concurrent(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a')
        results = self.processor.parse(claim)
        def publish():
            try:
                return self.processor.publish(claim, results)
            except LeaseLost:
                return None
        def cancel():
            with self.store._locked() as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                task = self.store._read(conn, self.task.id)
                from tg_testcase.application import _TransactionStore
                _TransactionStore(self.store, conn).change(task.id, 'u', task.version,
                    lambda t: t.transition('cancelled'))
        with ThreadPoolExecutor(max_workers=2) as pool:
            published = pool.submit(publish)
            cancelled = pool.submit(cancel)
            result = published.result()
            cancelled.result()
        self.assertEqual(self.store.get(self.task.id).state, 'cancelled')
        self.assertEqual(self.counts()[-1], 0 if result is None else 1)
        with self.assertRaises(LeaseLost):
            self.processor.publish(claim, results)

    def test_staging_private_cleanup_reclaim_publish(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a', 1)
        self.processor.parse(claim)
        with self.assertRaises(KeyError):
            self.processor.result(bid)
        self.now += 2
        new = self.processor.claim(bid, 'b')
        with self.store._locked() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM processing_staging').fetchone()[0], 0)
        self.processor.publish(new, self.processor.parse(new))
        with self.store._locked() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM processing_staging').fetchone()[0], 0)

    def test_cancel_cleans_staging(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a')
        self.processor.parse(claim)
        self.send('cancel')
        with self.store._locked() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM processing_staging').fetchone()[0], 0)

    def test_recovery_removes_only_unregistered_ingestion(self):
        self.batch()
        directory = self.store.directory(self.task.id) / 'source'
        orphan = directory / (uuid4().hex + '-orphan.txt')
        orphan.write_bytes(b'orphan')
        other = directory / 'manual.txt'
        other.write_bytes(b'kept')
        self.store.recover()
        self.assertFalse(orphan.exists())
        self.assertTrue(other.exists())
        self.assertTrue((self.store.directory(self.task.id) / self.task.sources[0].locator).exists())

    def test_upload_registry_failure_cleans_file(self):
        with self.store._locked() as conn, conn:
            conn.execute("CREATE TRIGGER fail_input BEFORE INSERT ON source_inputs BEGIN SELECT RAISE(ABORT, 'injected'); END")
        req = dict(protocol_version=1, request_id=uuid4().hex,
                   actor=dict(user_id='u', chat_id='c', chat_type='private'),
                   operation='append_text', payload=dict(text='test'),
                   task_id=self.task.id, expected_version=self.task.version)
        self.assertFalse(self.app.handle(req)['ok'])
        self.assertEqual(list((self.store.directory(self.task.id) / 'source').iterdir()), [])

    def test_leaf_replacement_during_open(self):
        bid = self.batch()
        source_path = self.store.directory(self.task.id) / self.task.sources[0].locator
        import os
        original = os.open
        replaced = False
        def swapping_open(path, flags, *args, **kwargs):
            nonlocal replaced
            if path == source_path.name and not replaced:
                replacement = source_path.with_name('replacement.txt')
                replacement.write_bytes(b'other')
                replacement.replace(source_path)
                replaced = True
            return original(path, flags, *args, **kwargs)
        with patch('tg_testcase.storage.os.open', side_effect=swapping_open):
            self.assertEqual(self.processor.process(bid, 'a')['status'], 'failed')
        self.assertTrue(replaced)

    def test_no_external_effects(self):
        bid = self.batch(b'<script/> !include arbitrary [url](https://invalid.example)', 'a.md')
        with patch('socket.socket', side_effect=AssertionError('network')), \
             patch('subprocess.Popen', side_effect=AssertionError('shell')), \
             patch('os.getenv', side_effect=AssertionError('environment')):
            self.assertEqual(self.processor.process(bid, 'a')['status'], 'completed')

    def test_forged_blocks_rejected(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a')
        results = self.processor.parse(claim)
        results[0]['blocks'][0]['text'] = 'forged'
        with self.assertRaises(ValueError):
            self.processor.publish(claim, results)

    def test_result_identity_rejected(self):
        bid = self.batch()
        claim = self.processor.claim(bid, 'a')
        results = self.processor.parse(claim)
        results[0]['input']['sha256'] = 'forged'
        with self.assertRaises(ValueError):
            self.processor.publish(claim, results)
        with self.assertRaises(KeyError):
            self.processor.result(bid)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        base = PROJECT / '.test-runtime'
        base.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def legacy(self):
        root = self.root / 'legacy'
        store = Store(root)
        task = store.create('u')
        from tg_testcase.models import Source
        task = store.change(task.id, 'u', task.version,
                            lambda t: t.sources.append(Source('s', 'txt', 'source/text.txt')))
        path = store.directory(task.id) / 'source/text.txt'
        path.write_bytes(b'legacy text')
        with store._locked() as conn, conn:
            for table in ('source_inputs', 'processing_runs', 'sanitized_contents', 'batch_manifests', 'processing_staging'):
                conn.execute('DROP TABLE ' + table)
            conn.execute('ALTER TABLE collection_batches DROP COLUMN processing_status')
            conn.execute('INSERT INTO collection_batches(id,task_id,status,source_ids,finalized_version) VALUES(?,?,?,?,?)',
                         ('batch', task.id, 'finalized', '["s"]', task.version))
            conn.execute('PRAGMA user_version=1')
        return root, task

    def test_one_to_two_and_old_finalized(self):
        root, task = self.legacy()
        store = Store(root)
        Store(root)
        self.assertEqual(store.get(task.id).processing_barrier, [dict(batch_id='batch', status='pending')])
        proc = Processor(store)
        self.assertEqual(proc.process('batch', 'a')['status'], 'completed')
        self.assertEqual(proc.result('batch')[1][0]['origin'], 'legacy_unknown')
        with store._locked() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_one_to_two_failure_rolls_back(self):
        root, task = self.legacy()
        with sqlite3.connect(root / 'tasks.sqlite3') as conn:
            conn.execute("UPDATE collection_batches SET source_ids='[\"missing\"]'")
            conn.commit()
            with self.assertRaises(KeyError):
                migrate(conn)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertNotIn('processing_status', [r[1] for r in conn.execute('PRAGMA table_info(collection_batches)')])
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='source_inputs'").fetchone())
            self.assertEqual(conn.execute('SELECT count(*) FROM history').fetchone()[0], 2)

    def test_existing_registries_preserved(self):
        root, task = self.legacy()
        with sqlite3.connect(root / 'tasks.sqlite3') as conn:
            conn.execute("INSERT INTO requests(request_id,fingerprint,response,actor_id,operation,task_id) VALUES('r','f','{}','u','reset',?)", (task.id,))
            conn.execute("INSERT INTO events(event_id,request_id,task_id,operation,before_version,after_version) VALUES('e','r',?,'reset',1,2)", (task.id,))
            conn.execute("INSERT INTO artifacts VALUES('a',?,'u','output/example.xlsx',2,'draft')", (task.id,))
            conn.commit()
            tables = ('tasks', 'history', 'requests', 'events', 'artifacts')
            before = {t: conn.execute('SELECT * FROM ' + t).fetchall() for t in tables}
        store = Store(root)
        with store._locked() as conn:
            self.assertEqual(before, {t: conn.execute('SELECT * FROM ' + t).fetchall() for t in tables})

    def test_zero_to_two_second_step_failure_atomic(self):
        with sqlite3.connect(self.root / 'zero.sqlite3') as conn:
            conn.execute('CREATE TABLE tasks(payload TEXT)')
            conn.execute('CREATE TABLE source_inputs(dummy TEXT)')
            conn.commit()
            with self.assertRaises(sqlite3.OperationalError):
                migrate(conn)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='requests'").fetchone())
            self.assertEqual(conn.execute('PRAGMA table_info(source_inputs)').fetchone()[1], 'dummy')

    def test_future_rejected(self):
        store = Store(self.root / 'future')
        with store._locked() as conn, conn:
            conn.execute('PRAGMA user_version=99')
        with self.assertRaises(ValueError):
            Store(store.root)
        with sqlite3.connect(store.db) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 99)

    def test_zero_two_repeat(self):
        store = Store(self.root / 'new')
        Store(store.root)
        with store._locked() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 2)
