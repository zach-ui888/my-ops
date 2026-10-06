# Step 2.2-B Capability-Root — offline delivery

Date: 2026-10-05. This task implements the scoped storage/staging work identified
in the previous delivery's capability-root blocker. Frozen contracts, wire
formats, migrations, and production topology are unchanged.

## Result and API

`CapabilityRoot(path, directory_fd)` duplicates an explicitly supplied directory
FD. It checks directory identity, current-user ownership, exact 0700 mode, and
rejects unreviewed root ACLs. Closing the capability invalidates later operations;
the caller's original descriptor is not owned or closed. This is an internal
composition object, never a wire/job field or authorization credential.

`Store(path, capability=capability)` uses that root without consulting PROJECT.
Store snapshots, Engine/Application source and output writes, Processor reads,
and recovery cleanup all carry the same scoped capability. Storage operations
walk beneath its FD with directory-relative/no-follow opens; writes create
exclusive temporary files, fsync, and rename relative to the opened parent.
Reads remain bounded and hash/size checked. Escapes, symlinks, unsafe write
leaves, and hardlinked read/write leaves are rejected. Cleanup preserves
registered files and unknown names.

Store opens its lock relative to the capability, checks its inode again after
bounded flock acquisition, and checks DB/sidecars before connecting. DB identity
and protected-marker identity are pinned for each Store instance; replacement,
missing protected marker, invalid marker bytes, and unsafe sidecars fail closed.
A new Store still discovers the durable protected marker.

`Staging(capability)` creates a per-submission directory relative to the root FD.
Artifacts use exclusive generated names, immediately unlinked while their handles
remain open. Artifact IDs are only dictionary keys. Exception paths close handles;
cleanup removes only the matching staging directory inode, retaining a replacement.
Receiver admission checks residue and available space through that same root FD,
and passes the capability to the existing protected importer/final commit boundary.

Ordinary, unprotected path-only Store and staging callers retain project-only
**offline/dev compatibility**. Protected Store and Receiver callers must supply
explicit `CapabilityRoot` objects, including protected offline composition.
`CapabilityRoot.local(path)` also uses the project-only no-follow walk. These are
not production entry points or a fallback for missing production configuration.
Template loading remains a separate project asset operation.

## Trust assumptions and limitations

The explicit FD constructor requires trusted composition to supply a stable
pathname with trusted, non-replaceable ancestors. It verifies the bound root,
not a complete installation/ancestor ACL policy. Python sqlite3 lacks a dir_fd
connection interface, so SQLite still connects through that stable pathname;
root/sidecar checks alone do not eliminate races against a malicious writer who
can replace ancestors. No /proc descriptor pathname, permission relaxation,
privileged provisioner, or production FD-delivery topology was introduced.

All exercised roots were inside this project. Independence from PROJECT was
tested by replacing the module's PROJECT constant with an unrelated project-local
path while explicit-capability Store/I/O/staging operations ran. No external
filesystem root was accessed to demonstrate independence.

Production remains **NO-GO**: independent Receiver access to registry 0700,
Controller access to acquisition, shared socket-root ownership, protected release
and ancestry verification, production provisioning, C reconciliation, and Step2.5
resource/recovery acceptance are unresolved. This focused task is not completion
of the entire B transport/pressure/crash acceptance matrix.

## Changed files

Created:
- `src/tg_testcase/capability_root.py`
- `tests/test_capability_root.py`
- `docs/PHASE2_STEP2C_B_STEP2_2_B_CAPABILITY_ROOT_DELIVERY.md`

Modified:
- `src/tg_testcase/storage.py`
- `src/tg_testcase/store.py`
- `src/tg_testcase/streaming.py`
- `src/tg_testcase/acquisition.py`
- `src/tg_testcase/receiver_service.py`
- `src/tg_testcase/engine.py`
- `src/tg_testcase/application.py`
- `src/tg_testcase/processor.py`
- `tests/test_application.py` — migration fixture explicitly creates its private
  state directory with 0700; migration assertions are unchanged.

## Validation

Python 3.10.12, project venv. New capability suite: 15 tests passed, 0 skipped.
Tests cover scoped Store/recovery, PROJECT independence, bounded hashed reads,
escapes/dot segments, symlink parents/leaves, hardlinks/FIFO, changed root mode,
closed/replaced roots, SQLite sidecars, marker replacement/removal, DB replacement,
lock hardlinks, staging cleanup on exceptions, and preservation of unknown files.

Commands:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_capability_root.py -q
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPYCACHEPREFIX="$PWD/.test-runtime/capability-compile" .venv/bin/python -m compileall -q src tests
```

Compile completed successfully. Final full suite: **365 tests in 41.931s;
354 passed, 11 errors, 0 skipped**. All 11 errors are existing real AF_UNIX tests:
three socket lifecycle tests, the kernel-peer test, and seven Receiver socket
tests. Socket bind/sendall return sandbox `PermissionError: Operation not
permitted`; SO_PEERCRED failure is converted to the fixed transport rejection.
These tests remain errors, not skips or mocked passes. Re-run the complete suite
in an authorized Linux environment permitting those socket operations before
claiming B acceptance. No other test failures/errors occurred. The prior generated `.capability-tests.log` has been removed by the fail-closed
follow-up; logs and compile caches are not delivery artifacts.
Git metadata is unavailable (`not a git repository`); no commit/push, deployment,
external upload, secret access, or Agent changes were performed.


## Fail-closed follow-up — Task 20261005-100122-1450EE baseline

Scope: resolve only the Capability-Root Contract §9 offline blocker caused by
protected runtime reaching project-local compatibility. The independently verified
baseline was Python 3.10.12, 365 tests passed, TEST_RC=0 and COMPILE_RC=0.

- `Store(..., protected_acquisition=True)` rejects a missing explicit capability
  before attempting path compatibility or creating files. Fixed diagnostic:
  `ValueError('Protected Store requires explicit CapabilityRoot')`.
- Durable protected markers still enforce protection. Path-only reopen rejects
  before database initialization/migration, without modifying marker or database.
  A previously opened compatibility Store also rejects operations when a durable
  protected marker appears. Explicit-capability reopen retains marker semantics.
- `ReceiverService` rejects string/path/None staging roots using the existing
  fixed `TransportRejected('transport_rejected')` error. It no longer calls
  `CapabilityRoot.local()` or falls back to PROJECT.
- `ProtectedReceiverAcquisition` requires an explicit staging capability at
  construction and checks again at `seal_stream`, before creating `Staging`.
  Rejection uses the existing `ContractError('AUTH_INVALID')`. Protected staging
  call sites pass capabilities; the base acquisition path uses Store's capability.
- Ordinary `Store(path)`, `Staging(path)` and `CapabilityRoot.local()` compatibility
  remain available. The local factory is not a production verifier.

Follow-up modified files: `src/tg_testcase/store.py`,
`src/tg_testcase/receiver_service.py`, `src/tg_testcase/protected_acquisition.py`,
`tests/test_capability_root.py`, `tests/test_step22a_commit.py`,
`tests/test_step22b_transport.py`, and this delivery document. No source files
were created. `.capability-tests.log` was deleted. Test fixtures now explicitly
open directory FDs and supply capabilities, including the process lock probe.

Seven regression tests cover protected Store rejection and explicit reopen,
marker appearance after a dev Store opens, ordinary dev Store/Staging behavior,
Receiver path rejection and explicit composition, successful protected sealing
with the local factory forbidden, and rejection if the importer's staging root
is replaced with a path. Existing marker integrity tests remain in place.

Frozen design/contract/review documents, schema/migrations, transaction behavior,
wire format, package/gap/formal gates, receipts, deployment/systemd, production
UID/socket topology, and TG-Codex Agent were not modified. No commit or push.

### Follow-up validation and judgment

Final full run (Python 3.10.12): **372 tests in 38.937s; 361 passed,
0 failures, 11 errors, 0 skipped; TEST_RC=1**. All seven added regressions passed.
The 11 errors are the same sandbox-limited AF_UNIX cases: three socket lifecycle,
one kernel-peer (fixed transport rejection), and seven Receiver socket tests.
No AF_UNIX tests were skipped or mocked. An initial run exposed a missing fixture
helper alias and an incorrect new success assertion; both were corrected before
this final complete rerun.

Commands executed on the final source/test version:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPYCACHEPREFIX="$PWD/.test-runtime/fail-closed-compile" .venv/bin/python -m compileall -q src tests
```

Compile passed, **COMPILE_RC=0**. `.capability-tests.log`, `.test-runtime/cache`,
and capability compile caches are absent from the delivered changes; generated
compile caches were removed after validation.

Judgment: the **Capability-Root Contract §9 offline compatibility-fallback
blocker is resolved** by the explicit-capability checks and regression coverage.
The final full suite still requires independent server verification because of
the 11 sandbox errors. This does **not** mark Step2.2-B CLOSED: crash/barrier/
pressure matrix acceptance remains subsequent work.
