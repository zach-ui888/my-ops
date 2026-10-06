"""Explicit, owned directory descriptors for trusted internal composition.

The caller supplying a descriptor must ensure its pathname and ancestors cannot
be replaced by untrusted writers. This is not a production permission verifier.
SQLite still uses that stable pathname (Python sqlite3 has no dir_fd API).
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat


class CapabilityRoot:
    def __init__(self, path, directory_fd):
        self.fd = -1
        raw = os.fspath(path)
        if '\x00' in raw or len(os.fsencode(raw)) > 4096 or not raw.startswith('/') or any(p in {'.', '..', ''} for p in raw.split('/')[1:]):
            raise ValueError('Invalid capability path')
        self.path = Path(raw)
        info = os.fstat(directory_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('Capability root must be owned and private')
        self.inode = (info.st_dev, info.st_ino)
        self.fd = os.dup(directory_fd)
        try:
            self.validate()
        except BaseException:
            self.close()
            raise

    @classmethod
    def local(cls, path):
        """Project-only compatibility factory; no production fallback."""
        from .storage import _directory
        with _directory(path) as fd:
            return cls(path, fd)

    def validate(self):
        if self.fd < 0:
            raise ValueError('Closed capability')
        if any(n in ('system.posix_acl_access', 'system.posix_acl_default')
               for n in os.listxattr(self.fd)):
            raise ValueError('Unreviewed capability ACL')
        for info in (os.fstat(self.fd), os.stat(self.path, follow_symlinks=False)):
            if (not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self.inode
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise ValueError('Replaced or unsafe capability root')

    def parts(self, path):
        raw = os.fspath(path)
        if any(p in {'.', '..', ''} for p in raw.split('/')[1:]):
            raise ValueError('Invalid scoped path')
        target = Path(raw)
        if not target.is_absolute():
            raise ValueError('Scoped path must be absolute')
        try:
            return target.relative_to(self.path).parts
        except ValueError:
            raise ValueError('Outside capability root') from None

    @contextmanager
    def directory(self, path, *, create=False):
        parts = self.parts(path)
        self.validate()
        fd = os.dup(self.fd)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise ValueError('Unsafe scoped directory')
            yield fd
        finally:
            os.close(fd)

    def checked_path(self, path):
        parts = self.parts(path)
        self.validate()
        # Validate existing components without following links; missing suffixes
        # are created later through directory(), never through this pathname.
        fd = os.dup(self.fd)
        try:
            for index, part in enumerate(parts):
                try:
                    info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    break
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError('Symlinks are forbidden')
                if index < len(parts) - 1:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
            return Path(path)
        finally:
            os.close(fd)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __del__(self):
        self.close()

    def __enter__(self):
        self.validate()
        return self

    def __exit__(self, *args):
        self.close()
