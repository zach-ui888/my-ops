"""Constrained paths and durable atomic writes. Runtime root must be private."""
import os
from pathlib import Path
import re
import stat
from contextlib import contextmanager
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


def atomic_write(path, content, *, capability=None):
    path = capability.checked_path(path) if capability else inside_project(path)
    with _directory(path.parent, capability, create=True) as parent:
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise ValueError('Unsafe write destination')
        tmp = f".{path.name}.{uuid4().hex}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(tmp, dir_fd=parent)
            except FileNotFoundError:
                pass


@contextmanager
def _directory(path, capability=None, *, create=False):
    if capability is not None:
        with capability.directory(path, create=create) as fd:
            yield fd
        return
    target = inside_project(path)
    fd = os.open(PROJECT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in target.relative_to(PROJECT).parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def bounded_read(path, limit, sha256=None, byte_length=None, *, capability=None):
    """Descriptor-relative walk; callers must supply a database-registered path.

    Never follows a symlink, opens devices/FIFOs, or reads past limit + 1.
    Hash/size bind bytes to the registered revision despite path replacement.
    """
    import hashlib
    import stat
    target = capability.checked_path(path) if capability else inside_project(path)
    with _directory(target.parent, capability) as fd:
        leaf = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
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
    if len(blob) > limit or (byte_length is not None and len(blob) != byte_length):
        raise ValueError('Input size mismatch')
    if sha256 is not None and hashlib.sha256(blob).hexdigest() != sha256:
        raise ValueError('Input hash mismatch')
    return blob


def cleanup_sources(directory, registered, *, capability=None):
    """Remove only generated unregistered files using a scoped directory FD."""
    try:
        with _directory(directory, capability) as fd:
            for name in os.listdir(fd):
                generated = re.fullmatch(r'[0-9a-f]{32}-[A-Za-z0-9_.-]+', name)
                temporary = re.fullmatch(r'\.[0-9a-f]{32}-[A-Za-z0-9_.-]+\.[0-9a-f]{32}\.tmp', name)
                if (generated or temporary) and 'source/' + name not in registered:
                    import stat
                    if not stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                        os.unlink(name, dir_fd=fd)
    except FileNotFoundError:
        return
