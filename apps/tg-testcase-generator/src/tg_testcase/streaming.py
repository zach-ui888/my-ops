"""Bounded offline I/O. Staging roots are trusted controller configuration.

Production must supply a protected root and protected ancestors; mode bits alone
cannot protect a directory whose parent is writable by an untrusted runner.
"""
import os
import stat
from uuid import uuid4
from .capability_root import CapabilityRoot
from .notion_package import require

CHUNK_SIZE = 64 * 1024


def read_exact(reader, size):
    result = bytearray()
    while len(result) < size:
        wanted = min(CHUNK_SIZE, size - len(result))
        chunk = reader.read(wanted)
        require(type(chunk) is bytes and 0 < len(chunk) <= wanted, 'Truncated/invalid stream')
        result.extend(chunk)
    return bytes(result)


class Staging:
    def __init__(self, root):
        self.capability = root if type(root) is CapabilityRoot else CapabilityRoot.local(root)
        self.owns_capability = type(root) is not CapabilityRoot
        self.files = {}
        self.fd = None

    def __enter__(self):
        self.capability.validate()
        self.name = 'seal-' + uuid4().hex
        os.mkdir(self.name, mode=0o700, dir_fd=self.capability.fd)
        try:
            self.fd = os.open(self.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.capability.fd)
            info = os.fstat(self.fd)
            self.inode = (info.st_dev, info.st_ino)
        except BaseException:
            if self.owns_capability:
                self.capability.close()
            raise
        return self

    def create(self, aid):
        require(self.fd is not None, 'Inactive staging')
        require(aid not in self.files, 'Duplicate staging artifact')
        self.capability.validate()
        name = uuid4().hex
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            os.unlink(name, dir_fd=self.fd)
            f = os.fdopen(fd, 'w+b')
        except BaseException:
            os.close(fd)
            raise
        self.files[aid] = f
        return f

    def __exit__(self, *args):
        try:
            for f in self.files.values():
                f.close()
            info = os.stat(self.name, dir_fd=self.capability.fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == self.inode:
                os.rmdir(self.name, dir_fd=self.capability.fd)
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self.owns_capability:
                self.capability.close()


def write_blob(conn, package_id, a, reader):
    values = (package_id, a['id'], a['kind'], a['path'], a['sha256'], a['size'])
    if hasattr(conn, 'blobopen'):
        cursor = conn.execute('INSERT INTO source_package_artifacts VALUES(?,?,?,?,?,?,zeroblob(?))',
                              values + (a['size'],))
        with conn.blobopen('source_package_artifacts', 'body', cursor.lastrowid) as blob:
            left = a['size']
            while left:
                chunk = read_exact(reader, min(CHUNK_SIZE, left))
                blob.write(chunk)
                left -= len(chunk)
    else:
        # Python 3.10 fallback: at most one 10 MiB attachment (+ bounded copies).
        body = reader.read(a['size'])
        require(type(body) is bytes and len(body) == a['size'], 'Truncated staging')
        conn.execute('INSERT INTO source_package_artifacts VALUES(?,?,?,?,?,?,?)', values + (body,))
