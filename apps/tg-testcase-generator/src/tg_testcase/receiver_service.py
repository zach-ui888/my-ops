"""Offline Receiver composition; production provisioning remains NO-GO."""
import json
import os
import time

from .capability_root import CapabilityRoot
from .protected_acquisition import ProtectedReceiverAcquisition
from .receiver import receive, frame, ACK, ERROR
from .registry_reader import RegistryReader
from .unix_transport import SocketReader, check, local_budget, write_response

MIB = 1024 * 1024
MAX_INPUT = 16*MIB + 4096 + 512*50 + 100*MIB + 5*(3 + 2*50 + 100*MIB) + 1


class ReceiverReader(SocketReader):
    def __init__(self, conn, accepted):
        super().__init__(conn, accepted, MAX_INPUT)
        self.frames, self.max_frames = 0, 3 + 2*50 + 100*MIB

    def header(self):
        self.frames += 1
        check(self.frames <= self.max_frames)
        super().header()

    def package_budget(self, size, artifacts):
        total = sum(a['size'] for a in artifacts)
        count = len(artifacts)
        check(count <= 50 and total <= 100*MIB and size + total <= 116*MIB)
        # Already consumed HELLO and PACKAGE. Permit every legal one-byte chunk.
        self.max_frames = 3 + 2*count + total
        self.cap = self.count + 512*count + total + 5*(2*count + total + 1) + 1


class ReceiverService:
    def __init__(self, store, registry_path, guard, staging_root, *, clock=time.time):
        check(type(staging_root) is CapabilityRoot)
        check(store.protected_acquisition)
        self.store, self.registry_path, self.guard = store, registry_path, guard
        self.staging_root = staging_root
        self.clock = clock

    def __call__(self, conn, peer, accepted):
        reader = ReceiverReader(conn, accepted)

        def resolve(job_id):
            # Conservative global disk admission: unknown residue is never removed.
            self.staging_root.validate()
            check(not os.listdir(self.staging_root.fd))
            space = os.fstatvfs(self.staging_root.fd)
            check(space.f_bavail * space.f_frsize >= 100*MIB)
            importer = ProtectedReceiverAcquisition(
                self.store, RegistryReader(self.registry_path, self.guard), job_id,
                staging_root=self.staging_root, clock=self.clock)
            importer.budget.end = min(importer.budget.end, reader.deadline)
            reader.deadline = importer.budget.end
            return importer

        with local_budget(lambda: reader.deadline):
            result = receive(None, None, None, reader, resolver=resolve)
        data = json.dumps(result, separators=(',', ':')).encode('utf-8')
        write_response(conn, frame(ACK if result['type'] == 'ACK' else ERROR, data))
