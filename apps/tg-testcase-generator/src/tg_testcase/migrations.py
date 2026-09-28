"""Transactional, additive upgrades from the unversioned Phase 1 database."""
from uuid import uuid5, NAMESPACE_URL

SCHEMA_VERSION = 1


def register_outputs(conn, task):
    for output in task.outputs:
        artifact_id = uuid5(NAMESPACE_URL, task.id + ':' + output['file']).hex
        conn.execute('INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?, ?, ?)',
                     (artifact_id, task.id, task.user_id, output['file'],
                      output['version'], output['mode']))


def migrate(conn):
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError('Unsupported future database schema')
        if version == 0:
            statements = [
                '''CREATE TABLE requests (request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                   response TEXT NOT NULL, actor_id TEXT NOT NULL, operation TEXT NOT NULL,
                   task_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
                '''CREATE TABLE events (event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                   task_id TEXT, operation TEXT NOT NULL, before_version INTEGER,
                   after_version INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
                '''CREATE TABLE collection_batches (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                   status TEXT NOT NULL, source_ids TEXT NOT NULL, finalized_version INTEGER)''',
                '''CREATE UNIQUE INDEX one_open_batch ON collection_batches(task_id)
                   WHERE status = 'open' ''',
                '''CREATE TABLE artifacts (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                   owner_id TEXT NOT NULL, relative_path TEXT NOT NULL, version INTEGER NOT NULL,
                   mode TEXT NOT NULL, UNIQUE(task_id, relative_path))''',
            ]
            for statement in statements:
                conn.execute(statement)
            from .models import Task
            for (payload,) in conn.execute('SELECT payload FROM tasks').fetchall():
                register_outputs(conn, Task.loads(payload))
            conn.execute('PRAGMA user_version=1')
