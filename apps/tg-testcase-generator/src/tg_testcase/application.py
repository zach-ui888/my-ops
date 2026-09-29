"""Offline controller boundary. No transport, network, parser or AI execution."""
import base64
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from .coverage import check_coverage, source_gaps
from .engine import Engine
from .gates import gate, GenerationBlocked
from .migrations import register_outputs
from .models import Source, State, Task
from .protocol import READS, response, validate_request
from .review import unresolved_p0
from .storage import atomic_write, inside_project, bounded_read, cleanup_sources
from .store import VersionConflict


class _TransactionStore:
    """Reuse engine domain actions under the application's single transaction/lock."""
    def __init__(self, store, conn):
        self.store, self.conn = store, conn
        self.changed = {}
        self.ingestion_tasks = set()

    def directory(self, task_id):
        return self.store.directory(task_id)

    def get(self, task_id):
        return self.store._read(self.conn, task_id)

    def save(self, task, new=False):
        self.store._project_processing(self.conn, task)
        self.store._discard_cancelled_staging(self.conn, task)
        if new:
            self.conn.execute('INSERT INTO tasks VALUES (?, ?, ?, ?, ?)',
                              (task.id, task.user_id, task.state, task.version, task.dumps()))
        else:
            self.conn.execute('UPDATE tasks SET state=?, version=?, payload=? WHERE id=?',
                              (task.state, task.version, task.dumps(), task.id))
        self.conn.execute('INSERT INTO history VALUES (?, ?, ?)', (task.id, task.version, task.dumps()))
        register_outputs(self.conn, task)
        self.changed[task.id] = task
        return task

    def cleanup_ingestion(self):
        for tid in self.ingestion_tasks:
            task = self.get(tid)
            cleanup_sources(self.directory(tid) / 'source', {s.locator for s in task.sources})

    def change(self, task_id, user_id, version, action):
        task = self.get(task_id)
        if task.user_id != user_id:
            raise PermissionError('Task owner mismatch')
        if task.version != version:
            raise VersionConflict('Stale task version')
        action(task)
        task.version += 1
        return self.save(task)


class Application:
    def __init__(self, store, allowed_users):
        self.store = store
        self.allowed_users = {str(u) for u in allowed_users}

    def handle(self, request):
        rid = request.get('request_id') if isinstance(request, dict) else None
        try:
            validate_request(request)
            actor = request['actor']
            if actor['chat_type'] != 'private' or actor['user_id'] not in self.allowed_users:
                raise PermissionError('Whitelisted private-chat users only')
            fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True,
                                                    separators=(',', ':')).encode()).hexdigest()
            with self.store._locked() as conn:
                with conn:
                    conn.execute('BEGIN IMMEDIATE')
                    cached = conn.execute('SELECT fingerprint, response FROM requests WHERE request_id=?',
                                          (rid,)).fetchone()
                    if cached:
                        if cached[0] != fingerprint:
                            return response(rid, 'request_conflict', message='request_id reused with different content')
                        if request['operation'] == 'get_artifact':
                            task = self.store._read(conn, request['task_id'])
                            if task.user_id != actor['user_id']:
                                raise PermissionError('Task owner mismatch')
                            self._artifact(conn, task, request['payload']['artifact_id'])
                        return json.loads(cached[1])
                    tx = _TransactionStore(self.store, conn)
                    conn.execute('SAVEPOINT operation')
                    try:
                        result = self._dispatch(tx, request)
                    except (ValueError, KeyError, PermissionError, OSError, sqlite3.Error) as exc:
                        conn.execute('ROLLBACK TO operation')
                        tx.changed.clear()
                        tx.cleanup_ingestion()
                        result = self._error(rid, exc)
                    conn.execute('RELEASE operation')
                    conn.execute('INSERT INTO requests(request_id,fingerprint,response,actor_id,operation,task_id) '
                                 'VALUES (?, ?, ?, ?, ?, ?)',
                                 (rid, fingerprint, json.dumps(result), actor['user_id'],
                                  request['operation'], result['task_id']))
                    conn.execute('INSERT INTO events(event_id,request_id,task_id,operation,before_version,after_version) '
                                 'VALUES (?, ?, ?, ?, ?, ?)',
                                 (rid, rid, result['task_id'], request['operation'],
                                  request.get('expected_version'), result['version']))
                for task in tx.changed.values():
                    self.store._snapshot(task)
                return result
        except (ValueError, KeyError, TypeError, PermissionError, OSError, sqlite3.Error) as exc:
            return self._error(rid, exc)

    @staticmethod
    def _error(rid, exc):
        code = ('forbidden' if isinstance(exc, PermissionError) else
                'stale_version' if isinstance(exc, VersionConflict) else
                'generation_blocked' if isinstance(exc, GenerationBlocked) else
                'not_found' if isinstance(exc, KeyError) else
                'storage_error' if isinstance(exc, (OSError, sqlite3.Error)) else 'invalid_request')
        # Do not expose filesystem paths or submitted content in error responses.
        return response(rid, code, message=code)

    def _dispatch(self, tx, req):
        op, p, uid = req['operation'], req['payload'], req['actor']['user_id']
        data = {}
        if op == 'create_or_get_active':
            row = tx.conn.execute("SELECT id FROM tasks WHERE user_id=? AND state!='cancelled'", (uid,)).fetchone()
            task = tx.get(row[0]) if row else tx.save(Task(uuid4().hex, uid), new=True)
        else:
            task = tx.get(req['task_id'])
            if task.user_id != uid:
                raise PermissionError('Task owner mismatch')
            if op not in READS and task.version != req['expected_version']:
                raise VersionConflict('Stale task version')
            engine = Engine(tx, self.allowed_users)
            if op in {'cancel', 'reset', 'confirm_generation'}:
                if op == 'confirm_generation':
                    if tx.conn.execute("SELECT 1 FROM collection_batches WHERE task_id=? AND status='open'", (task.id,)).fetchone():
                        raise ValueError('Finish collection first')
                    task = engine.confirm(task.id, uid, task.version, p['text'])
                    gate(task, task.confirmation['mode'])
                else:
                    task = getattr(engine, op)(task.id, uid, task.version)
                    if op == 'cancel':
                        tx.conn.execute("UPDATE collection_batches SET status='abandoned' WHERE task_id=? AND status='open'", (task.id,))
            elif op.startswith('append_'):
                def append(current):
                    Engine._edit(current)
                    sid = uuid4().hex
                    if op == 'append_notion_reference':
                        source = Source(sid, 'notion', p['reference'], origin='notion_reference')
                    else:
                        filename = p['filename'] if op == 'append_upload' else 'text.txt'
                        blob = base64.b64decode(p['data_base64']) if op == 'append_upload' else p['text'].encode()
                        locator = f'source/{sid}-{filename}'
                        tx.ingestion_tasks.add(current.id)
                        atomic_write(tx.directory(current.id) / locator, blob)
                        source = Source(sid, filename.rsplit('.', 1)[-1].lower(), locator,
                                        origin='telegram_text' if op == 'append_text' else 'upload',
                                        input_sha256=hashlib.sha256(blob).hexdigest(), byte_length=len(blob))
                    current.sources.append(source)
                    row = tx.conn.execute("SELECT id,source_ids FROM collection_batches WHERE task_id=? AND status='open'", (current.id,)).fetchone()
                    if row:
                        batch_id, ids = row[0], json.loads(row[1]) + [sid]
                        tx.conn.execute('UPDATE collection_batches SET source_ids=? WHERE id=?', (json.dumps(ids), batch_id))
                    else:
                        batch_id = uuid4().hex
                        tx.conn.execute('INSERT INTO collection_batches(id,task_id,status,source_ids,finalized_version) VALUES (?, ?, ?, ?, NULL)',
                                        (batch_id, current.id, 'open', json.dumps([sid])))
                    tx.conn.execute('INSERT INTO source_inputs VALUES (?,?,?,?,?,?,?,?,?)',
                                    (batch_id, sid, current.id, source.locator, source.kind, source.origin,
                                     source.input_sha256, source.byte_length, source.revision))
                    data.update(source_id=sid, batch_id=batch_id, status='pending')
                task = tx.change(task.id, uid, task.version, append)
            elif op == 'finish_collection':
                def finish(current):
                    Engine._edit(current)
                    row = tx.conn.execute("SELECT id,source_ids FROM collection_batches WHERE task_id=? AND status='open'", (current.id,)).fetchone()
                    if not row:
                        raise ValueError('No open collection batch')
                    tx.conn.execute("UPDATE collection_batches SET status='finalized',finalized_version=? WHERE id=?",
                                    (current.version + 1, row[0]))
                    if current.state == State.COLLECTING:
                        current.transition(State.REVIEW)
                    data.update(batch_id=row[0], source_ids=json.loads(row[1]), status='pending_processing')
                task = tx.change(task.id, uid, task.version, finish)
            elif op == 'submit_user_message':
                def pending(current):
                    Engine._edit(current)
                    message = {'id': uuid4().hex, 'text': p['text'], 'status': 'pending',
                               'submitted_version': current.version + 1}
                    current.pending_messages.append(message)
                    data.update(message_id=message['id'], status='pending')
                task = tx.change(task.id, uid, task.version, pending)
            elif op == 'get_artifact':
                data = self._artifact(tx.conn, task, p['artifact_id'])
            elif op == 'get_summary':
                data = check_coverage(task).to_dict()
                data.update(unresolved_p0=unresolved_p0(task), incomplete_sources=source_gaps(task))
        data.update(state=task.state, pending_message_count=len(task.pending_messages))
        if op in {'get_status', 'get_summary'}:
            data['batches'] = [dict(id=r[0], status=r[1], source_ids=json.loads(r[2]), finalized_version=r[3], processing_status=r[4])
                              for r in tx.conn.execute('SELECT id,status,source_ids,finalized_version,processing_status FROM collection_batches WHERE task_id=? ORDER BY rowid', (task.id,))]
            data['artifacts'] = [dict(artifact_id=r[0], version=r[1], mode=r[2]) for r in tx.conn.execute(
                'SELECT id,version,mode FROM artifacts WHERE task_id=? AND owner_id=?', (task.id, uid))]
        return response(req['request_id'], 'ok', task=task, data=data)

    def _artifact(self, conn, task, artifact_id):
        row = conn.execute('SELECT relative_path,version,mode FROM artifacts WHERE id=? AND task_id=? AND owner_id=?',
                           (artifact_id, task.id, task.user_id)).fetchone()
        if not row:
            raise KeyError('Unknown artifact')
        relative = Path(row[0])
        if relative.is_absolute() or '..' in relative.parts or len(relative.parts) != 2 or relative.parts[0] != 'output':
            raise ValueError('Invalid artifact boundary')
        target = inside_project(self.store.directory(task.id) / relative)
        blob = bounded_read(target, 64 * 1024 * 1024)
        return dict(artifact_id=artifact_id, version=row[1], mode=row[2],
                    filename=target.name, data_base64=base64.b64encode(blob).decode())
