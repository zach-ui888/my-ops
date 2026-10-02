"""Bounded offline I/O. Staging roots are trusted controller configuration.

Production must supply a protected root and protected ancestors; mode bits alone
cannot protect a directory whose parent is writable by an untrusted runner.
"""
import os
import stat
import tempfile
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
        self.root, self.files = root, {}

    def __enter__(self):
        info = os.lstat(self.root)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                and not info.st_mode & 0o022, 'Staging root must be private')
        self.directory = tempfile.TemporaryDirectory(prefix='seal-', dir=self.root)
        return self

    def create(self, aid):
        # The artifact ID is an in-memory key only, never a path component.
        f = tempfile.TemporaryFile(dir=self.directory.name)
        self.files[aid] = f
        return f

    def __exit__(self, *args):
        for f in self.files.values():
            f.close()
        self.directory.cleanup()


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
