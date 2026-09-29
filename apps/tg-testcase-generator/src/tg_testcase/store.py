import fcntl
from contextlib import contextmanager
import sqlite3
from uuid import uuid4

from .models import State, Task
from .migrations import migrate, register_outputs, SCHEMA_VERSION
from .storage import atomic_write, inside_project, safe_name, cleanup_sources


class VersionConflict(ValueError):
    pass


class Store:
    """SQLite authority; JSON is a disposable cache. All writers take one lock."""
    def __init__(self, root):
        self.root = inside_project(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = inside_project(self.root / "tasks.sqlite3")
        inside_project(self.root / ".lock")
        self.snapshot_errors = []
        with self._locked() as conn:
            if conn.execute('PRAGMA user_version').fetchone()[0] > SCHEMA_VERSION:
                raise ValueError('Unsupported future database schema')
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    state TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_user ON tasks(user_id)
                    WHERE state != 'cancelled';
                CREATE TABLE IF NOT EXISTS history (
                    task_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(task_id, version));
            """)
            migrate(conn)

    @contextmanager
    def _locked(self):
        inside_project(self.root / ".lock")
        inside_project(self.db)
        # Include SQLite sidecars in the symlink boundary check.
        for suffix in ("-journal", "-wal", "-shm"):
            inside_project(str(self.db) + suffix)
        with (self.root / ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            conn = sqlite3.connect(self.db, timeout=30)
            conn.execute("PRAGMA synchronous=FULL")
            try:
                yield conn
            finally:
                conn.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    def directory(self, task_id):
        return inside_project(self.root / "tasks" / safe_name(task_id))

    def _snapshot(self, task):
        try:
            for sub in ("source", "review", "output"):
                inside_project(self.directory(task.id) / sub).mkdir(parents=True, exist_ok=True)
            atomic_write(self.directory(task.id) / "task.json", task.dumps().encode())
            atomic_write(self.directory(task.id) / "review" / f"v{task.version}.json", task.dumps().encode())
        except (OSError, ValueError) as exc:
            # The committed SQLite state remains authoritative and recoverable.
            self.snapshot_errors.append((task.id, type(exc).__name__))

    def create(self, user_id):
        task = Task(uuid4().hex, str(user_id))
        with self._locked() as conn:
            with conn:
                conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
                             (task.id, task.user_id, task.state, task.version, task.dumps()))
                conn.execute("INSERT INTO history VALUES (?, ?, ?)", (task.id, 1, task.dumps()))
            self._snapshot(task)
        return task

    @staticmethod
    def _read(conn, task_id):
        row = conn.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(task_id)
        task = Task.loads(row[0])
        Store._project_processing(conn, task)
        return task

    @staticmethod
    def _project_processing(conn, task):
        task.processing_barrier = [dict(batch_id=r[0], status=r[1]) for r in conn.execute(
            "SELECT id, CASE WHEN status='open' THEN 'open' ELSE processing_status END "
            "FROM collection_batches WHERE task_id=? AND status!='abandoned'", (task.id,))]

    @staticmethod
    def _discard_cancelled_staging(conn, task):
        if task.state == State.CANCELLED:
            conn.execute('DELETE FROM processing_staging WHERE batch_id IN '
                         '(SELECT id FROM collection_batches WHERE task_id=?)', (task.id,))

    def get(self, task_id):
        safe_name(task_id)
        with self._locked() as conn:
            return self._read(conn, task_id)

    def change(self, task_id, user_id, version, action):
        safe_name(task_id)
        with self._locked() as conn:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                task = self._read(conn, task_id)
                if task.user_id != str(user_id):
                    raise PermissionError("Task owner mismatch")
                if task.version != version:
                    raise VersionConflict("Stale task version")
                action(task)
                self._project_processing(conn, task)
                self._discard_cancelled_staging(conn, task)
                task.version += 1
                register_outputs(conn, task)
                conn.execute("UPDATE tasks SET state=?, version=?, payload=? WHERE id=?",
                             (task.state, task.version, task.dumps(), task.id))
                conn.execute("INSERT INTO history VALUES (?, ?, ?)",
                             (task.id, task.version, task.dumps()))
            self._snapshot(task)
        return task

    def recover(self):
        """Run at startup before accepting work; retains all sources and artifacts."""
        recovered = []
        with self._locked() as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM tasks")]
            for task_id in ids:
                task = self._read(conn, task_id)
                if task.state == State.GENERATING:
                    task.transition(State.READY)
                    task.confirmation = None
                    task.recovery_note = "Interrupted generation; explicit confirmation required again"
                    task.version += 1
                    with conn:
                        conn.execute("UPDATE tasks SET state=?, version=?, payload=? WHERE id=?",
                                     (task.state, task.version, task.dumps(), task.id))
                        conn.execute("INSERT INTO history VALUES (?, ?, ?)",
                                     (task.id, task.version, task.dumps()))
                cleanup_sources(self.directory(task.id) / 'source', {s.locator for s in task.sources})
                self._snapshot(task)
                recovered.append(task)
        return recovered
