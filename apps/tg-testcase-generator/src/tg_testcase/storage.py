"""Constrained paths and durable atomic writes. Runtime root must be private."""
import os
from pathlib import Path
import re
from uuid import uuid4


PROJECT = Path(__file__).resolve().parents[2]


def inside_project(path):
    p = Path(path).absolute()
    if not p.is_relative_to(PROJECT):
        raise ValueError("Path must be inside project")
    current = PROJECT
    for part in p.relative_to(PROJECT).parts:
        if part in {"..", "."}:
            raise ValueError("Unsafe path")
        current /= part
        if current.is_symlink():
            raise ValueError("Symlinks are forbidden")
    return p


def safe_name(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name) or ".." in name:
        raise ValueError("Unsafe filename or identifier")
    return name


def atomic_write(path, content):
    path = inside_project(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if tmp.exists():
            tmp.unlink()


def bounded_read(path, limit, sha256=None, byte_length=None):
    """Descriptor-relative walk; callers must supply a database-registered path.

    Never follows a symlink, opens devices/FIFOs, or reads past limit + 1.
    Hash/size bind bytes to the registered revision despite path replacement.
    """
    import hashlib
    import stat
    target = Path(path).absolute()
    if not target.is_relative_to(PROJECT):
        raise ValueError('Outside project')
    parts = target.relative_to(PROJECT).parts
    if not parts or any(p in {'.', '..'} for p in parts):
        raise ValueError('Unsafe path')
    fd = os.open(PROJECT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        before = os.fstat(leaf)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            os.close(leaf)
            raise ValueError('Not a bounded private regular file')
        with os.fdopen(leaf, 'rb') as stream:
            blob = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('Input changed during read')
    finally:
        os.close(fd)
    if len(blob) > limit or (byte_length is not None and len(blob) != byte_length):
        raise ValueError('Input size mismatch')
    if sha256 is not None and hashlib.sha256(blob).hexdigest() != sha256:
        raise ValueError('Input hash mismatch')
    return blob


def cleanup_sources(directory, registered):
    """Remove only generated, unregistered ingestion files under a locked task.

    Called on rollback/recovery; SQLite source locators are the authority.
    Descriptor-relative unlink cannot follow a replaced parent or leaf symlink.
    """
    target = inside_project(directory)
    fd = os.open(PROJECT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            for part in target.relative_to(PROJECT).parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
        except FileNotFoundError:
            return
        for name in os.listdir(fd):
            generated = re.fullmatch(r'[0-9a-f]{32}-[A-Za-z0-9_.-]+', name)
            temporary = re.fullmatch(r'\.[0-9a-f]{32}-[A-Za-z0-9_.-]+\.[0-9a-f]{32}\.tmp', name)
            if (generated or temporary) and 'source/' + name not in registered:
                import stat
                if not stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)
