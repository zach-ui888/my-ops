"""Strict deployment metadata and injected filesystem verification; no discovery.

Step 2.1 tests provide fake stat/identity data. This module neither creates paths
nor reads users, groups, secrets or production configuration.
"""
from dataclasses import dataclass, fields as dc_fields
from pathlib import PurePosixPath
from typing import Protocol
from .contract_types import fields, integer, require, strict_json


@dataclass(frozen=True)
class RuntimeOwners:
    administrator_uid: int
    gateway_uid: int
    controller_uid: int
    receiver_uid: int
    producer_uid: int
    downloader_uid: int
    guard_gid: int
    gateway_gid: int
    receiver_gid: int

    def __post_init__(self):
        for f in dc_fields(self):
            integer(getattr(self, f.name), 0, 4294967294)
        services = [getattr(self, k + '_uid') for k in ('gateway', 'controller', 'receiver', 'producer', 'downloader')]
        require(0 not in services and len(set(services)) == 5)
        require(self.administrator_uid not in services)

    @classmethod
    def parse(cls, raw):
        value = strict_json(raw, ascii_only=True)
        fields(value, ' '.join(f.name for f in dc_fields(cls)))
        return cls(**value)


@dataclass(frozen=True)
class PathMetadata:
    uid: int
    gid: int
    mode: int
    kind: str  # directory/file/socket/symlink
    runner_writable: bool  # Include ACLs, supplementary groups and parent access.
    producer_writable: bool
    service_writable: bool


class FilesystemVerifier(Protocol):
    def inspect(self, path: str) -> PathMetadata: ...
    def login_disabled(self, uid: int) -> bool: ...
    def runner_in_group(self, gid: int) -> bool: ...


def _path(value):
    require(type(value) is str and 1 <= len(value.encode('ascii')) <= 4096)
    require(value.startswith('/') and '\x00' not in value and '\\' not in value)
    require(all(p not in ('.', '..', '') for p in value.split('/')[1:]))
    return PurePosixPath(value)


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


@dataclass(frozen=True)
class RuntimePaths:
    release_root: str
    config_root: str
    registry_state_root: str
    acquisition_state_root: str
    receiver_staging_root: str
    producer_spool_root: str
    downloader_staging_root: str
    socket_root: str
    authorization_lock_path: str

    def __post_init__(self):
        paths = {f.name: _path(getattr(self, f.name)) for f in dc_fields(self)}
        mutable = [paths[k] for k in ('registry_state_root', 'acquisition_state_root', 'receiver_staging_root', 'producer_spool_root', 'downloader_staging_root')]
        for i, p in enumerate(mutable):
            require(not any(_overlap(p, other) for other in mutable[i + 1:]))
            require(not any(_overlap(p, paths[k]) for k in ('release_root', 'config_root')))
        lock = paths['authorization_lock_path']
        require(not _overlap(lock, paths['producer_spool_root']))
        for basename in ('gateway.sock', 'receiver.sock'):
            require(len(str(paths['socket_root'] / basename).encode('ascii')) <= 107)

    @classmethod
    def parse(cls, raw):
        value = strict_json(raw, ascii_only=True)
        fields(value, ' '.join(f.name for f in dc_fields(cls)))
        return cls(**value)

    @property
    def registry_db(self):
        return str(PurePosixPath(self.registry_state_root) / 'registry.sqlite3')

    @property
    def acquisition_db(self):
        return str(PurePosixPath(self.acquisition_state_root) / 'tasks.sqlite3')

    @property
    def gateway_socket(self):
        return str(PurePosixPath(self.socket_root) / 'gateway.sock')

    @property
    def receiver_socket(self):
        return str(PurePosixPath(self.socket_root) / 'receiver.sock')

    def validate(self, owners, filesystem):
        require(type(owners) is RuntimeOwners)
        for service in ('gateway', 'controller', 'receiver', 'producer', 'downloader'):
            require(filesystem.login_disabled(getattr(owners, service + '_uid')))
        for k in ('guard_gid', 'gateway_gid', 'receiver_gid'):
            require(not filesystem.runner_in_group(getattr(owners, k)))
        expected = {'release_root': owners.administrator_uid, 'config_root': owners.administrator_uid,
                    'registry_state_root': owners.controller_uid, 'acquisition_state_root': owners.receiver_uid,
                    'receiver_staging_root': owners.receiver_uid, 'producer_spool_root': owners.producer_uid,
                    'downloader_staging_root': owners.downloader_uid, 'authorization_lock_path': owners.controller_uid}
        for f in dc_fields(self):
            path = PurePosixPath(getattr(self, f.name))
            for current in (path, *path.parents):
                meta = filesystem.inspect(str(current))
                require(meta.kind != 'symlink' and not meta.runner_writable)
                if current != path:
                    require(meta.kind == 'directory')
                if f.name in {'release_root', 'config_root'}:
                    require(not meta.service_writable)
                if f.name == 'authorization_lock_path':
                    require(not meta.producer_writable)
            meta = filesystem.inspect(str(path))
            if f.name == 'socket_root':
                require(meta.uid in {owners.gateway_uid, owners.controller_uid, owners.receiver_uid})
                require(meta.kind == 'directory' and meta.mode == 0o750)
            else:
                require(meta.uid == expected[f.name])
            if f.name == 'authorization_lock_path':
                require(meta.kind == 'file' and meta.mode == 0o660 and meta.gid == owners.guard_gid)
            else:
                require(meta.kind == 'directory')
                if f.name in {'registry_state_root', 'acquisition_state_root', 'receiver_staging_root', 'producer_spool_root', 'downloader_staging_root'}:
                    require(meta.mode == 0o700)
        return self

    def validate_sockets(self, owners, filesystem):
        """Validate provisioned socket nodes using the same injected verifier.

        Provisioning and SO_PEERCRED enforcement are Step 2.2/2.5 work.
        """
        self.validate(owners, filesystem)
        for path, uid, gid in ((self.gateway_socket, owners.controller_uid, owners.gateway_gid),
                               (self.receiver_socket, owners.receiver_uid, owners.receiver_gid)):
            meta = filesystem.inspect(path)
            require(meta.kind == 'socket' and meta.mode == 0o660 and meta.uid == uid and meta.gid == gid)
            require(not meta.runner_writable)
        return self
