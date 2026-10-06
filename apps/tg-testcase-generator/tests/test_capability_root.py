import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tg_testcase.capability_root import CapabilityRoot
from tg_testcase.storage import atomic_write, bounded_read, cleanup_sources
from tg_testcase.store import Store
from tg_testcase.streaming import Staging

PROJECT = Path(__file__).resolve().parents[1]


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT, prefix='cap-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.cap = CapabilityRoot(self.root, fd)
        finally:
            os.close(fd)
        self.addCleanup(self.cap.close)

    def test_explicit_store_and_io_do_not_use_project(self):
        with patch('tg_testcase.storage.PROJECT', self.root / 'unrelated'):
            store = Store(self.root, capability=self.cap, protected_acquisition=True)
            task = store.create('user')
            self.assertEqual(store.get(task.id).id, task.id)
            self.assertEqual(store.snapshot_errors, [])
            target = store.directory(task.id) / 'source' / ('a'*32 + '-input.txt')
            atomic_write(target, b'bytes', capability=self.cap)
            self.assertEqual(bounded_read(target, 5, hashlib.sha256(b'bytes').hexdigest(), 5,
                                          capability=self.cap), b'bytes')
            cleanup_sources(target.parent, set(), capability=self.cap)
            self.assertFalse(target.exists())
            self.assertEqual(len(store.recover()), 1)

    def test_scope_escape_and_dot_segments(self):
        for path in (str(self.root) + '/../escape', str(self.root) + '/./file',
                     str(self.root) + '//file', PROJECT / 'outside-capability', 'relative'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                atomic_write(path, b'blocked', capability=self.cap)

    def test_symlink_parent_and_leaf(self):
        (self.root / 'real').mkdir()
        (self.root / 'alias').symlink_to(self.root / 'real', target_is_directory=True)
        (self.root / 'leaf').symlink_to('missing')
        for target in (self.root / 'alias' / 'file', self.root / 'leaf'):
            with self.assertRaises((ValueError, OSError)):
                atomic_write(target, b'blocked', capability=self.cap)
        self.assertFalse((self.root / 'real' / 'file').exists())

    def test_special_and_hardlink_files_rejected(self):
        original = self.root / 'original'
        original.write_bytes(b'unchanged')
        os.link(original, self.root / 'hardlink')
        os.mkfifo(self.root / 'fifo')
        for name in ('hardlink', 'fifo'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    bounded_read(self.root / name, 100, capability=self.cap)
                with self.assertRaises(ValueError):
                    atomic_write(self.root / name, b'blocked', capability=self.cap)
        self.assertEqual(original.read_bytes(), b'unchanged')

    def test_replaced_root_rejected(self):
        moved = self.root.with_name(self.root.name + '-moved')
        self.root.rename(moved)
        self.root.mkdir(mode=0o700)
        try:
            with self.assertRaises(ValueError):
                atomic_write(self.root / 'file', b'blocked', capability=self.cap)
        finally:
            self.root.rmdir()
            moved.rename(self.root)

    def test_changed_permissions_and_closed_capability(self):
        self.root.chmod(0o750)
        with self.assertRaises(ValueError):
            self.cap.validate()
        self.root.chmod(0o700)
        self.cap.close()
        with self.assertRaises(ValueError):
            self.cap.validate()

    def test_sidecars_are_rejected_and_retained(self):
        store = Store(self.root, capability=self.cap)
        for suffix in ('-journal', '-wal', '-shm'):
            path = Path(str(store.db) + suffix)
            path.symlink_to('missing')
            with self.assertRaises(ValueError):
                store.create('user')
            self.assertTrue(path.is_symlink())
            path.unlink()

    def test_invalid_marker_is_not_repaired(self):
        marker = self.root / '.protected-acquisition'
        marker.write_bytes(b'invalid')
        with self.assertRaises(ValueError):
            Store(self.root, capability=self.cap, protected_acquisition=True)
        self.assertEqual(marker.read_bytes(), b'invalid')

    def test_staging_uses_explicit_descriptor_and_cleans_up(self):
        with patch('tg_testcase.storage.PROJECT', self.root / 'unrelated'):
            with Staging(self.cap) as staged:
                stream = staged.create('id/never-used-as-path')
                stream.write(b'content')
                self.assertEqual(os.listdir(staged.fd), [])
                with self.assertRaises(ValueError):
                    staged.create('id/never-used-as-path')
            self.assertTrue(stream.closed)
            self.assertEqual(os.listdir(self.cap.fd), [])

    def test_staging_retains_replacement_directory(self):
        with Staging(self.cap) as staged:
            original = self.root / staged.name
            moved = self.root / 'moved'
            original.rename(moved)
            original.mkdir()
            (original / 'unknown').write_bytes(b'retain')
        self.assertEqual((original / 'unknown').read_bytes(), b'retain')
        moved.rmdir()

    def test_cleanup_preserves_registered_and_unknown_files(self):
        registered = 'a'*32 + '-file'
        for name in (registered, 'unknown', 'b'*32 + '-file'):
            (self.root / name).write_bytes(b'content')
        cleanup_sources(self.root, {'source/' + registered}, capability=self.cap)
        self.assertEqual(set(os.listdir(self.cap.fd)), {registered, 'unknown'})

    def test_local_factory_rejects_symlink_ancestor(self):
        (self.root / 'real').mkdir(mode=0o700)
        (self.root / 'alias').symlink_to(self.root / 'real', target_is_directory=True)
        with self.assertRaises(ValueError):
            CapabilityRoot.local(self.root / 'alias')

    def test_protected_marker_replacement_and_removal_fail_closed(self):
        store = Store(self.root, capability=self.cap, protected_acquisition=True)
        marker = self.root / '.protected-acquisition'
        marker.rename(self.root / 'saved-marker')
        with self.assertRaises(ValueError):
            store.create('user')
        marker.write_bytes(b'protected-acquisition-v1\n')
        with self.assertRaises(ValueError):
            store.create('user')

    def test_database_replacement_and_lock_hardlink_fail_closed(self):
        store = Store(self.root, capability=self.cap)
        store.db.rename(self.root / 'saved-db')
        store.db.write_bytes(b'unknown')
        with self.assertRaises(ValueError):
            store.create('user')
        store.db.unlink()
        (self.root / 'saved-db').rename(store.db)
        os.link(self.root / '.lock', self.root / 'lock-alias')
        with self.assertRaises(ValueError):
            store.create('user')

    def test_staging_exception_closes_files_and_inactive_create_rejected(self):
        staged = Staging(self.cap)
        with self.assertRaises(ValueError):
            staged.create('inactive')
        with self.assertRaises(RuntimeError):
            with staged:
                stream = staged.create('id')
                raise RuntimeError('injected')
        self.assertTrue(stream.closed)
        self.assertEqual(os.listdir(self.cap.fd), [])

    def test_protected_store_requires_explicit_capability_before_path_access(self):
        with patch('tg_testcase.store.inside_project', side_effect=AssertionError('fallback')):
            with self.assertRaisesRegex(ValueError, '^Protected Store requires explicit CapabilityRoot$'):
                Store(self.root / 'unused', protected_acquisition=True)
        self.assertFalse((self.root / 'unused').exists())

    def test_durable_marker_requires_explicit_capability_on_reopen(self):
        store = Store(self.root, capability=self.cap, protected_acquisition=True)
        task = store.create('user')
        before = store.db.read_bytes()
        with self.assertRaisesRegex(ValueError, '^Protected Store requires explicit CapabilityRoot$'):
            Store(self.root)
        self.assertEqual(store.db.read_bytes(), before)
        reopened = Store(self.root, capability=self.cap)
        self.assertTrue(reopened.protected_acquisition)
        self.assertEqual(reopened.get(task.id).id, task.id)

    def test_dev_store_rejects_later_protected_marker(self):
        dev = Store(self.root)
        self.addCleanup(dev.capability.close)
        Store(self.root, capability=self.cap, protected_acquisition=True)
        with self.assertRaisesRegex(ValueError, '^Protected Store requires explicit CapabilityRoot$'):
            dev.create('user')

    def test_ordinary_dev_store_and_staging_path_compatibility(self):
        store = Store(self.root)
        self.addCleanup(store.capability.close)
        self.assertFalse(store.protected_acquisition)
        task = store.create('user')
        self.assertEqual(store.get(task.id).id, task.id)
        with Staging(self.root) as staged:
            stream = staged.create('ordinary')
            stream.write(b'ordinary')
        self.assertTrue(stream.closed)

    def test_receiver_service_requires_explicit_staging_capability(self):
        from tg_testcase.receiver_service import ReceiverService
        from tg_testcase.unix_transport import TransportRejected
        store = Store(self.root, capability=self.cap, protected_acquisition=True)
        with patch.object(CapabilityRoot, 'local', side_effect=AssertionError('fallback')):
            for root in (self.root, str(self.root), None):
                with self.subTest(kind=type(root)), self.assertRaisesRegex(TransportRejected, '^transport_rejected$'):
                    ReceiverService(store, None, None, root)
            with patch('tg_testcase.storage.PROJECT', self.root / 'unrelated'):
                service = ReceiverService(store, None, None, self.cap)
            self.assertIs(service.staging_root, self.cap)
