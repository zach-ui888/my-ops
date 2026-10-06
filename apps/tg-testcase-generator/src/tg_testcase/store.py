import fcntl
import os
import stat
from contextlib import contextmanager
import sqlite3
from uuid import uuid4
from pathlib import Path

from .capability_root import CapabilityRoot
from .models import State, Task
from .migrations import migrate, register_outputs, SCHEMA_VERSION
from .storage import atomic_write, inside_project, safe_name, cleanup_sources, bounded_read


class VersionConflict(ValueError):
    pass


class Store:
    """SQLite authority; JSON is a disposable cache. All writers take one lock."""
    def __init__(self, root, *, protected_acquisition=False, capability=None):
        if type(protected_acquisition) is not bool:
            raise ValueError('Invalid acquisition mode')
        self._explicit_capability = type(capability) is CapabilityRoot
        if protected_acquisition and not self._explicit_capability:
            raise ValueError('Protected Store requires explicit CapabilityRoot')
        if capability is None:
            # Compatibility for offline callers; explicit capabilities never use PROJECT.
            self.root = inside_project(root)
            from .storage import _directory
            with _directory(self.root, create=True) as fd:
                capability = CapabilityRoot(self.root, fd)
        elif type(capability) is not CapabilityRoot or capability.path != Path(root):
            raise ValueError('Mismatched Store capability')
        self.capability = capability
        self.root = capability.path
        capability.validate()
        root_info = os.stat(self.root, follow_symlinks=False)
        self._root_inode = (root_info.st_dev, root_info.st_ino)
        self._lock_inode = None
        self._db_inode = None
        self._marker_inode = None
        marker = self.capability.checked_path(self.root / '.protected-acquisition')
        if marker.exists() and not self._explicit_capability:
            self.capability.close()
            raise ValueError('Protected Store requires explicit CapabilityRoot')
        if protected_acquisition and not marker.exists():
            atomic_write(marker, b'protected-acquisition-v1\n', capability=self.capability)
        if marker.exists():
            if bounded_read(marker, 64, capability=self.capability) != b'protected-acquisition-v1\n':
                raise ValueError('Invalid protected marker')
        self._protected_acquisition = protected_acquisition or marker.exists()
        if marker.exists():
            info = os.stat(marker, follow_symlinks=False)
            self._marker_inode = (info.st_dev, info.st_ino)
        self.db = self.capability.checked_path(self.root / "tasks.sqlite3")
        self.capability.checked_path(self.root / ".lock")
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

    @property
    def protected_acquisition(self):
        marker = self.capability.checked_path(self.root / '.protected-acquisition')
        try:
            info = os.stat(marker.name, dir_fd=self.capability.fd, follow_symlinks=False)
        except FileNotFoundError:
            if self._protected_acquisition:
                raise ValueError('Missing protected marker')
            return False
        if not self._explicit_capability:
            raise ValueError('Protected Store requires explicit CapabilityRoot')
        inode = (info.st_dev, info.st_ino)
        if self._marker_inode is not None and inode != self._marker_inode:
            raise ValueError('Replaced protected marker')
        if bounded_read(marker, 64, capability=self.capability) != b'protected-acquisition-v1\n':
            raise ValueError('Invalid protected marker')
        self._marker_inode = inode
        self._protected_acquisition = True
        return True

    def _validate_state_files(self):
        # Include SQLite sidecars in the symlink boundary check.
        for suffix in ("-journal", "-wal", "-shm"):
            self.capability.checked_path(str(self.db) + suffix)
        for path in [self.db] + [str(self.db) + suffix for suffix in ('-journal', '-wal', '-shm')]:
            try:
                info = os.stat(Path(path).name, dir_fd=self.capability.fd, follow_symlinks=False)
            except FileNotFoundError:
                if Path(path) == self.db and self._db_inode is not None:
                    raise ValueError('Missing state database')
                continue
            if Path(path) == self.db and self._db_inode is not None:
                if (info.st_dev, info.st_ino) != self._db_inode:
                    raise ValueError('Replaced state database')
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise ValueError('Invalid state file')

    @contextmanager
    def _locked(self):
        self.capability.validate()
        self.capability.checked_path(self.root / ".lock")
        self.capability.checked_path(self.db)
        root_info = os.stat(self.root, follow_symlinks=False)
        if (root_info.st_dev, root_info.st_ino) != self._root_inode:
            raise ValueError('Invalid state root')
        fd = os.open('.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.capability.fd)
        with os.fdopen(fd, 'a+b') as lock:
            info = os.fstat(lock.fileno())
            inode = (info.st_dev, info.st_ino)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_mode & 0o077):
                raise ValueError('Invalid Store lock')
            if self._lock_inode is not None and self._lock_inode != inode:
                raise ValueError('Replaced Store lock')
            self._lock_inode = inode
            from .unix_transport import bounded_flock, remaining
            bounded_flock(lock)
            self.capability.validate()
            self.protected_acquisition
            self._validate_state_files()
            current = os.stat(".lock", dir_fd=self.capability.fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != inode:
                raise ValueError("Replaced Store lock")
            conn = sqlite3.connect(self.db, timeout=remaining())
            try:
                info = os.stat(self.db.name, dir_fd=self.capability.fd, follow_symlinks=False)
                self._db_inode = (info.st_dev, info.st_ino)
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA foreign_keys=ON")
                yield conn
            finally:
                conn.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    def directory(self, task_id):
        return self.capability.checked_path(self.root / "tasks" / safe_name(task_id))

    def _snapshot(self, task):
        try:
            for sub in ("source", "review", "output"):
                with self.capability.directory(self.directory(task.id) / sub, create=True):
                    pass
            atomic_write(self.directory(task.id) / "task.json", task.dumps().encode(), capability=self.capability)
            atomic_write(self.directory(task.id) / "review" / f"v{task.version}.json", task.dumps().encode(), capability=self.capability)
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
            conn.execute("UPDATE source_fetch_runs SET status='abandoned' WHERE status!='sealed' AND batch_id IN (SELECT id FROM collection_batches WHERE task_id=?)", (task.id,))
            conn.execute("UPDATE collection_batches SET acquisition_status='abandoned' WHERE acquisition_status!='sealed' AND task_id=?", (task.id,))
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
                cleanup_sources(self.directory(task.id) / 'source', {s.locator for s in task.sources}, capability=self.capability)
                self._snapshot(task)
                recovered.append(task)
        return recovered
