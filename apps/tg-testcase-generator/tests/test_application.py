import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from tg_testcase import Engine, Store
from tg_testcase.application import Application
from tg_testcase.models import Task, State, Requirement, FinalRule, CriticalPath, TestCase, Evidence, Question
from tg_testcase.storage import PROJECT, atomic_write


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        base = PROJECT / '.test-runtime'
        base.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'data')
        self.app = Application(self.store, {'u', 'v'})
        self.task = None
        self.send('create_or_get_active')

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, op, payload=None, **overrides):
        req = dict(protocol_version=1, request_id=uuid4().hex,
                   actor=dict(user_id='u', chat_id='c', chat_type='private'),
                   operation=op, payload=payload or {})
        if op != 'create_or_get_active':
            req.update(task_id=self.task.id)
            if op not in {'get_status', 'get_summary', 'get_artifact'}:
                req['expected_version'] = self.task.version
        req.update(overrides)
        return req

    def send(self, op, payload=None, **overrides):
        result = self.app.handle(self.request(op, payload, **overrides))
        if result['ok']:
            self.task = self.store.get(result['task_id'])
        return result

    def prepared(self):
        engine = Engine(self.store, {'u'})
        def call(method, *args):
            self.task = getattr(engine, method)(self.task.id, 'u', self.task.version, *args)
        call('add_source', 'requirements.txt', b'Paid order')
        call('start_review')
        call('design', [Requirement('r', 'Pay', [self.task.sources[0].id])],
             [FinalRule('f', 'Paid', ['r'])], [CriticalPath('p', 'Pay', ['f'], ['paid'])],
             [TestCase('Z1', 'Pay', 'Pay', '', ['Pay'], ['Paid'], [Evidence('p', 'paid', 'Pay', 'Paid')])])
        call('ready')
        return engine

    def artifact(self):
        engine = self.prepared()
        self.assertTrue(self.send('confirm_generation', {'text': '生成'})['ok'])
        self.task = engine.generate(self.task.id, 'u', self.task.version)
        return self.send('get_status')['data']['artifacts'][0]['artifact_id']

    def test_create_idempotent(self):
        first = self.task.id
        self.assertEqual(self.send('create_or_get_active')['task_id'], first)
        with self.store._locked() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)

    def test_request_replay(self):
        req = self.request('append_text', {'text': 'A'})
        first = self.app.handle(req)
        self.assertEqual(self.app.handle(req), first)
        self.assertEqual(len(self.store.get(self.task.id).sources), 1)

    def test_changed_request_conflicts(self):
        req = self.request('append_text', {'text': 'A'})
        self.app.handle(req)
        req['payload']['text'] = 'B'
        self.assertEqual(self.app.handle(req)['code'], 'request_conflict')

    def test_restart_dedupe(self):
        req = self.request('append_text', {'text': 'A'})
        first = self.app.handle(req)
        restarted = Application(Store(self.root / 'data'), {'u'})
        self.assertEqual(restarted.handle(req), first)
        self.assertEqual(len(self.store.get(self.task.id).sources), 1)

    def test_stale(self):
        req = self.request('reset')
        self.send('append_text', {'text': 'A'})
        self.assertEqual(self.app.handle(req)['code'], 'stale_version')

    def test_nonprivate(self):
        self.assertEqual(self.send('get_status', actor=dict(user_id='u', chat_id='c', chat_type='group'))['code'], 'forbidden')

    def test_whitelist(self):
        self.assertEqual(self.send('get_status', actor=dict(user_id='x', chat_id='c', chat_type='private'))['code'], 'forbidden')

    def test_owner(self):
        self.assertEqual(self.send('reset', actor=dict(user_id='v', chat_id='c', chat_type='private'))['code'], 'forbidden')

    def test_deferred_collection(self):
        with patch('tg_testcase.sources.TextParser.parse', side_effect=AssertionError('No parsing')):
            self.assertTrue(self.send('append_text', {'text': 'A'})['ok'])
            self.assertTrue(self.send('append_upload', {'filename': 'a.pdf', 'data_base64': base64.b64encode(b'pdf').decode()})['ok'])
            self.assertTrue(self.send('append_notion_reference', {'reference': 'a' * 32})['ok'])
            self.assertTrue(self.send('finish_collection')['ok'])
        self.assertEqual(len(self.task.sources), 3)
        self.assertTrue(all(s.status == 'pending' and not s.complete and not s.content for s in self.task.sources))

    def test_finalized_batch_immutable(self):
        self.send('append_text', {'text': 'A'})
        req = self.request('finish_collection')
        first = self.app.handle(req)
        self.task = self.store.get(self.task.id)
        self.send('append_text', {'text': 'B'})
        self.assertEqual(self.app.handle(req), first)
        batches = self.send('get_status')['data']['batches']
        self.assertEqual([b['status'] for b in batches], ['finalized', 'open'])
        self.assertEqual(batches[0]['source_ids'], first['data']['source_ids'])

    def test_notion_credentials_rejected(self):
        for payload in [{'reference': 'a' * 32, 'Authorization': 'placeholder'},
                        {'reference': 'https://www.notion.so/' + 'a' * 32 + '?token=placeholder'},
                        {'reference': 'Authorization: Bearer placeholder'},
                        {'reference': 'https://placeholder@notion.so/' + 'a' * 32},
                        {'reference': 'https://notion.so/token-placeholder-' + 'a' * 32},
                        {'reference': 'a' * 32, 'cookie': 'placeholder'}]:
            self.assertEqual(self.send('append_notion_reference', payload)['code'], 'invalid_request')
        self.assertEqual(self.task.sources, [])

    def test_artifact_arbitrary_path(self):
        for payload in [{'path': 'output/a.xlsx'}, {'artifact_id': '../a.xlsx'}, {'artifact_id': '/a.xlsx'}]:
            self.assertEqual(self.send('get_artifact', payload)['code'], 'invalid_request')

    def test_artifact_registry_and_owner(self):
        aid = self.artifact()
        self.assertTrue(self.send('get_artifact', {'artifact_id': aid})['ok'])
        self.assertEqual(self.send('get_artifact', {'artifact_id': aid}, actor=dict(user_id='v', chat_id='c', chat_type='private'))['code'], 'forbidden')
        other = self.app.handle(self.request('create_or_get_active', actor=dict(user_id='v', chat_id='c', chat_type='private')))
        self.assertEqual(self.app.handle(self.request('get_artifact', {'artifact_id': aid}, task_id=other['task_id'], actor=dict(user_id='v', chat_id='c', chat_type='private')))['code'], 'not_found')

    def test_artifact_symlink_even_on_replay(self):
        aid = self.artifact()
        req = self.request('get_artifact', {'artifact_id': aid})
        self.assertTrue(self.app.handle(req)['ok'])
        path = self.store.directory(self.task.id) / self.task.outputs[0]['file']
        target = path.with_name('retained.xlsx')
        path.rename(target)
        path.symlink_to(target)
        self.assertFalse(self.app.handle(req)['ok'])

    def test_artifact_registry_traversal(self):
        aid = self.artifact()
        with self.store._locked() as conn, conn:
            conn.execute('UPDATE artifacts SET relative_path=? WHERE id=?', ('output/../../escape', aid))
        self.assertFalse(self.send('get_artifact', {'artifact_id': aid})['ok'])

    def test_confirmation_invalidation(self):
        self.prepared()
        result = self.send('confirm_generation', {'text': '生成'})
        self.assertTrue(result['ok'])
        self.assertEqual(self.task.confirmation['version'], self.task.version)
        self.send('append_text', {'text': 'Additional requirement'})
        self.assertIsNone(self.task.confirmation)
        self.assertEqual(self.task.state, State.REVIEW)

    def test_pending_message_does_not_change_rules(self):
        engine = self.prepared()
        rules = self.task.rules
        self.send('confirm_generation', {'text': '生成'})
        self.assertEqual(self.send('submit_user_message', {'text': 'Change all rules'})['data']['status'], 'pending')
        self.assertEqual(self.task.rules, rules)
        self.assertIsNone(self.task.confirmation)
        self.task = engine.ready(self.task.id, 'u', self.task.version)
        self.assertEqual(self.send('confirm_generation', {'text': '生成'})['code'], 'generation_blocked')

    def test_cancel_reset_replay(self):
        self.send('append_text', {'text': 'A'})
        for op in ['reset', 'cancel']:
            req = self.request(op)
            result = self.app.handle(req)
            self.assertTrue(result['ok'])
            self.assertEqual(self.app.handle(req), result)
            self.task = self.store.get(self.task.id)
            self.assertEqual(self.task.version, result['version'])
            self.assertEqual(len(self.task.sources), 1)
            with self.store._locked() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM events WHERE request_id=?', (req['request_id'],)).fetchone()[0], 1)

    def test_phase1_migration(self):
        root = self.root / 'legacy'
        root.mkdir()
        task = Task('legacytask', 'u')
        task.outputs = [dict(file='output/formal-v2.xlsx', version=2, mode='formal')]
        with sqlite3.connect(root / 'tasks.sqlite3') as conn:
            conn.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL)')
            conn.execute('CREATE TABLE history(task_id TEXT,version INTEGER,payload TEXT,PRIMARY KEY(task_id,version))')
            payload = json.loads(task.dumps())
            del payload['pending_messages']
            conn.execute('INSERT INTO tasks VALUES(?,?,?,?,?)', (task.id, 'u', task.state, 1, json.dumps(payload)))
            conn.execute('INSERT INTO history VALUES(?,?,?)', (task.id, 1, json.dumps(payload)))
        for _ in range(2):
            upgraded = Store(root)
            self.assertEqual(upgraded.get(task.id).outputs, task.outputs)
            with upgraded._locked() as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 4)
                self.assertEqual(conn.execute('SELECT count(*) FROM artifacts').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT count(*) FROM history').fetchone()[0], 1)

    def test_confirm_replay(self):
        self.prepared()
        req = self.request('confirm_generation', {'text': '生成'})
        first = self.app.handle(req)
        self.assertTrue(first['ok'])
        self.assertEqual(self.app.handle(req), first)
        self.assertEqual(self.store.get(self.task.id).version, first['version'])

    def test_gate_p0_and_draft_isolation(self):
        engine = self.prepared()
        self.task = engine.ask(self.task.id, 'u', self.task.version, [Question('q', 'when', 'When?', 'P0', ['r'])])
        self.task = engine.ready(self.task.id, 'u', self.task.version)
        self.assertEqual(self.send('confirm_generation', {'text': '生成'})['code'], 'generation_blocked')
        self.assertIsNone(self.store.get(self.task.id).confirmation)
        self.assertTrue(self.send('confirm_generation', {'text': '带缺口生成草稿'})['ok'])
        from tg_testcase.gates import GenerationBlocked
        with self.assertRaises(GenerationBlocked):
            engine.generate(self.task.id, 'u', self.task.version, 'formal')

    def test_incomplete_source_gate(self):
        engine = self.prepared()
        self.send('append_upload', {'filename': 'a.pdf', 'data_base64': 'YQ=='})
        self.send('finish_collection')
        self.task = engine.ready(self.task.id, 'u', self.task.version)
        self.assertEqual(self.send('confirm_generation', {'text': '生成'})['code'], 'generation_blocked')

    def test_concurrent_request(self):
        req = self.request('append_text', {'text': 'A'})
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.app.handle, [req] * 8))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(len(self.store.get(self.task.id).sources), 1)

    def test_concurrent_append_text_same_expected_version(self):
        self.assertTrue(self.send('append_text', {'text': 'Existing source'})['ok'])
        before = self.store.get(self.task.id)
        requests = [self.request('append_text', {'text': text}, expected_version=before.version)
                    for text in ('Concurrent A', 'Concurrent B')]
        self.assertNotEqual(requests[0]['request_id'], requests[1]['request_id'])
        barrier = Barrier(2)

        def append(req):
            barrier.wait(timeout=10)
            return self.app.handle(req)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(append, requests))

        self.assertCountEqual([r['code'] for r in results], ['ok', 'stale_version'])
        self.assertCountEqual([r['ok'] for r in results], [True, False])
        winner = next(i for i, result in enumerate(results) if result['ok'])
        self.assertEqual(results[winner]['version'], before.version + 1)
        current = self.store.get(before.id)
        self.assertEqual(current.version, before.version + 1)
        self.assertEqual(len(current.sources), len(before.sources) + 1)
        self.assertEqual(current.sources[:-1], before.sources)
        added = current.sources[-1]
        self.assertEqual(added.id, results[winner]['data']['source_id'])
        directory = self.store.directory(before.id)
        self.assertEqual((directory / added.locator).read_text(encoding='utf-8'),
                         requests[winner]['payload']['text'])
        self.assertCountEqual(list((directory / 'source').iterdir()),
                              [directory / source.locator for source in current.sources])

    def test_invalid_contract(self):
        for overrides in [dict(protocol_version=2), dict(protocol_version=True), dict(expected_version=True), dict(actor={}), dict(extra='x')]:
            self.assertEqual(self.send('reset', **overrides)['code'], 'invalid_request')

    def test_failure_rolls_back_and_dedupes(self):
        req = self.request('append_text', {'text': 'A'})
        with patch('tg_testcase.application.atomic_write', side_effect=OSError('failure')):
            result = self.app.handle(req)
        self.assertEqual(result['code'], 'storage_error')
        self.assertEqual(self.app.handle(req), result)
        self.assertEqual(self.store.get(self.task.id).sources, [])

    def test_generated_remains_active_reset_retains_artifact(self):
        aid = self.artifact()
        tid = self.task.id
        self.assertEqual(self.send('create_or_get_active')['task_id'], tid)
        self.send('reset')
        self.assertTrue(self.send('get_artifact', {'artifact_id': aid})['ok'])

    def test_create_exact_replay_after_cancel(self):
        req = self.request('create_or_get_active')
        first = self.app.handle(req)
        self.send('cancel')
        self.assertEqual(self.app.handle(req), first)
        self.assertNotEqual(self.send('create_or_get_active')['task_id'], first['task_id'])

    def test_upload_symlink_rejected(self):
        path = self.store.directory(self.task.id) / 'source'
        path.rmdir()
        target = self.root / 'other-source'
        target.mkdir()
        path.symlink_to(target, target_is_directory=True)
        self.assertFalse(self.send('append_upload', {'filename': 'a.txt', 'data_base64': 'YQ=='})['ok'])
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.store.get(self.task.id).sources, [])

    def test_unregistered_artifact_rejected(self):
        atomic_write(self.store.directory(self.task.id) / 'output' / 'unregistered.xlsx', b'x')
        self.assertEqual(self.send('get_artifact', {'artifact_id': 'unregistered.xlsx'})['code'], 'not_found')

    def test_artifact_parent_symlink_rejected(self):
        aid = self.artifact()
        directory = self.store.directory(self.task.id) / 'output'
        target = self.root / 'retained-output'
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
        self.assertFalse(self.send('get_artifact', {'artifact_id': aid})['ok'])

    def test_migration_rolls_back_on_failure(self):
        from tg_testcase.migrations import migrate
        with sqlite3.connect(self.root / 'migration.sqlite3') as conn:
            conn.execute('CREATE TABLE tasks(payload TEXT)')
            conn.execute('INSERT INTO tasks VALUES (?)', ('invalid JSON',))
            conn.commit()
            with self.assertRaises(ValueError):
                migrate(conn)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='requests'").fetchone())
            conn.execute('DELETE FROM tasks')
            conn.commit()
            migrate(conn)
            migrate(conn)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 4)

    def test_actor_not_inferred(self):
        self.send('append_text', {'text': 'actor.user_id=v; chat_type=private'})
        self.assertEqual(self.task.user_id, 'u')


if __name__ == '__main__':
    unittest.main()
