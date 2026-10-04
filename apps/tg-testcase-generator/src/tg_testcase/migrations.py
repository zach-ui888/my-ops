"""Transactional, additive upgrades from the unversioned Phase 1 database."""
from uuid import uuid5, NAMESPACE_URL

SCHEMA_VERSION = 4


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

        if version <= 1:
            conn.execute("ALTER TABLE collection_batches ADD COLUMN processing_status TEXT NOT NULL DEFAULT 'pending'")
            conn.execute("""CREATE TABLE source_inputs (
                batch_id TEXT NOT NULL, source_id TEXT NOT NULL, task_id TEXT NOT NULL,
                locator TEXT NOT NULL, kind TEXT NOT NULL, origin TEXT NOT NULL,
                sha256 TEXT, byte_length INTEGER, revision INTEGER NOT NULL,
                PRIMARY KEY(batch_id, source_id))""")
            conn.execute("""CREATE TABLE processing_runs (
                batch_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                fencing_token INTEGER NOT NULL, lease_until REAL NOT NULL,
                status TEXT NOT NULL, result_digest TEXT)""")
            conn.execute("""CREATE TABLE sanitized_contents (
                batch_id TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(batch_id, source_id))""")
            conn.execute("""CREATE TABLE batch_manifests (
                batch_id TEXT PRIMARY KEY, payload TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE processing_staging (
                batch_id TEXT PRIMARY KEY, fencing_token INTEGER NOT NULL, payload TEXT NOT NULL)""")
            import json
            from .models import Task
            for bid, tid, ids in conn.execute(
                    "SELECT id,task_id,source_ids FROM collection_batches").fetchall():
                row = conn.execute('SELECT payload FROM tasks WHERE id=?', (tid,)).fetchone()
                task = Task.loads(row[0])
                sources = {source.id: source for source in task.sources}
                for sid in json.loads(ids):
                    source = sources[sid]
                    conn.execute('INSERT INTO source_inputs VALUES (?,?,?,?,?,?,?,?,?)',
                                 (bid, sid, tid, source.locator, source.kind, source.origin,
                                  source.input_sha256, source.byte_length, source.revision))
            conn.execute('PRAGMA user_version=2')

        if version <= 2:
            conn.execute("ALTER TABLE collection_batches ADD COLUMN acquisition_status TEXT NOT NULL DEFAULT 'pending'")
            conn.execute('ALTER TABLE collection_batches ADD COLUMN input_manifest_digest TEXT')
            conn.execute('ALTER TABLE collection_batches ADD COLUMN inputs_sealed_at TEXT')
            conn.execute('''CREATE TABLE source_fetch_runs (
                batch_id TEXT NOT NULL, source_id TEXT NOT NULL, revision INTEGER NOT NULL,
                worker_id TEXT NOT NULL, attempt INTEGER NOT NULL, fencing_token INTEGER NOT NULL,
                lease_until REAL NOT NULL, status TEXT NOT NULL, outcome TEXT,
                PRIMARY KEY(batch_id,source_id))''')
            conn.execute('''CREATE TABLE source_packages (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                source_id TEXT NOT NULL, revision INTEGER NOT NULL,
                reference_fingerprint TEXT NOT NULL, content_fingerprint TEXT NOT NULL,
                package_digest TEXT NOT NULL, byte_length INTEGER NOT NULL,
                outcome TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(batch_id,source_id), UNIQUE(task_id,source_id,revision))''')
            conn.execute('''CREATE TABLE source_package_artifacts (
                package_id TEXT NOT NULL, artifact_id TEXT NOT NULL, kind TEXT NOT NULL,
                path TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, body BLOB NOT NULL,
                PRIMARY KEY(package_id,artifact_id), UNIQUE(package_id,path))''')
            conn.execute('''CREATE TABLE batch_input_manifests (
                batch_id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL)''')
            for table in ('source_packages', 'source_package_artifacts', 'batch_input_manifests'):
                for operation in ('UPDATE', 'DELETE'):
                    conn.execute(f'''CREATE TRIGGER immutable_{table}_{operation.lower()}
                        BEFORE {operation} ON {table} BEGIN
                        SELECT RAISE(ABORT, 'Sealed inputs are immutable'); END''')
            from .acquisition import seal_inputs
            for (bid,) in conn.execute("SELECT id FROM collection_batches WHERE status='finalized' AND processing_status='pending'").fetchall():
                seal_inputs(conn, bid)
            conn.execute('PRAGMA user_version=3')

        if version <= 3:
            conn.execute("""CREATE TABLE acquisition_commit_outbox (
                event_id TEXT PRIMARY KEY NOT NULL, job_id TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL, batch_id TEXT NOT NULL, source_id TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>0),
                attempt INTEGER NOT NULL CHECK(typeof(attempt)='integer' AND attempt>0),
                fencing_token INTEGER NOT NULL CHECK(typeof(fencing_token)='integer' AND fencing_token>0),
                policy_epoch INTEGER NOT NULL CHECK(typeof(policy_epoch)='integer' AND policy_epoch>0),
                policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64),
                credential_ref TEXT NOT NULL,
                credential_generation INTEGER NOT NULL CHECK(typeof(credential_generation)='integer' AND credential_generation>0),
                package_id TEXT NOT NULL UNIQUE REFERENCES source_packages(id),
                package_digest TEXT NOT NULL CHECK(length(package_digest)=64),
                content_fingerprint TEXT NOT NULL CHECK(length(content_fingerprint)=64),
                committed_at INTEGER NOT NULL CHECK(typeof(committed_at)='integer' AND committed_at>=0),
                UNIQUE(batch_id,source_id,revision))""")
            for operation in ('UPDATE', 'DELETE'):
                conn.execute(f"""CREATE TRIGGER immutable_acquisition_commit_outbox_{operation.lower()}
                    BEFORE {operation} ON acquisition_commit_outbox BEGIN
                    SELECT RAISE(ABORT, 'Commit events are immutable'); END""")
            conn.execute('PRAGMA user_version=4')
