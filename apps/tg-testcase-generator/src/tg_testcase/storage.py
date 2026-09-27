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
