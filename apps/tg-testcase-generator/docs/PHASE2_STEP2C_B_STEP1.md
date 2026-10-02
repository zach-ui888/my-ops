# Phase 2 Step 2C-B Step 1

Implemented offline streaming seal and framed receiver primitives. No schema or
notion-source-package-v1 version changes. PNG/JPEG raw artifacts, descriptor
validation, visual_unresolved and formal/draft gates remain unchanged.

## APIs and trust boundary

- `OfflineAcquisition.seal_stream(claim, package_reader, package_length,
  artifact_pairs, staging=None)`: each pair is (artifact_id, binary reader).
  Readers implement bounded `read(n)` and return EOF at the end of their body.
- Existing `seal(claim, bytes, dict)` delegates to this API. Existing callers
  still own their input dictionary; the importer never rebuilds it.
- `Staging(root)` accepts a controller-selected directory, checks owner and
  group/other write bits, creates a private random temporary directory and
  internally named temporary files. IDs and manifest paths are labels only.
  Normal success, rejection and exceptions close and remove all staging files.
  Abrupt process death may leave an empty temporary directory; startup cleanup
  in the future protected service is not implemented here.
- The caller must own readers and staging exclusively for the duration of seal.
  It must supply trusted claims; these are not remote authorization objects.
- `seal_failure(..., root_type=...)` or a trusted claim's `root_type` supports
  page/database/data_source. Legacy callers retain page as the default.

The runner-writable checkout is **not a production trust root**. Future installation
must place reviewed code, SQLite, receiver, staging and all their parents beyond
codex-runner write access. Mode checks here do not establish that deployment
property. Root/Controller must not import this development checkout. No production
service or protected installation is created by this step.

## Memory, transactions and budgets

Package input is capped at 16 MiB before reading, read in 64 KiB requests and
validated with existing structural limits. Canonical JSON is also capped at
16 MiB. JSON decoding/canonicalization uses bounded whole-package allocations;
this is not a streaming JSON decoder.

Artifacts are processed sequentially in at most 64 KiB reads, with incremental
SHA256 and exact size checks. Existing 10 MiB individual, 100 MiB source and
256 MiB batch limits (including reserved failure receipts) apply. Declared source
and batch budgets are checked before consuming artifacts; actual size overruns
fail on the first excess byte.

No SQLite lock or write transaction is held during stream reads. After staging,
BEGIN IMMEDIATE rechecks claim, revision, worker, attempt, fencing, lease,
cancellation, package binding and current batch budget. Package/artifacts/manifest
are committed in one transaction; errors roll back all rows.

On Python with sqlite3.blobopen, INSERT zeroblob plus incremental writes keeps
artifact working buffers at chunk scale. The blob handle is closed before commit;
it is used only for newly inserted rows in the transaction. Existing immutable
UPDATE/DELETE triggers remain unchanged. As with any privileged SQLite writer,
the protected controller must not expose arbitrary blobopen access.

This environment uses Python 3.10: fallback reads **one** staged attachment
(maximum 10 MiB) for SQLite binding. Python and SQLite may each hold a copy; peak
artifact memory is O(single attachment), not O(100 MiB source). Tests exercise
100 MiB total input with Python allocation peak below 21 MiB, excluding SQLite
native memory and preexisting caller allocations. JSON structures and SQLite
cache add separate memory overhead. The incremental branch has a bounded-write
primitive test; real blobopen integration requires Python 3.11+ validation.

## Wire v1

All frames: unsigned 1-byte type, unsigned big-endian 4-byte payload length,
then exactly that many bytes. Type and length are checked before payload reads.

| Type | ID | Maximum bytes |
|---|---:|---:|
| HELLO | 1 | 4096 |
| PACKAGE | 2 | 16 MiB |
| ARTIFACT | 3 | 512 |
| CHUNK | 4 | 64 KiB |
| END | 5 | 0 |
| COMMIT | 6 | 0 |
| ACK | 7 | 1024 |
| ERROR | 8 | 128 |

Request grammar: HELLO PACKAGE (ARTIFACT CHUNK* END)* COMMIT EOF.
The future transport must provide a finite request stream / write-half close;
the primitive waits for EOF before publishing. Deadlines, peer authorization,
socket lifecycle and concurrency quotas belong to the future receiver.

HELLO is strict JSON with protocol_version=1, job_id, task_id, batch_id, source_id,
revision, root_type, canonical_root_id, attempt, fencing_token, package_length,
package_sha256. Positive counters and lowercase 32-hex root ID are required.
PACKAGE is canonical UTF-8 notion-source-package-v1 JSON. Its digest is therefore
the existing persisted package_digest. ARTIFACT contains only artifact_id, size,
sha256, which must match the package exactly. CHUNK is opaque artifact content.

Metadata fields are allowlisted; there is no path/URL/header/cookie/SQL/pickle or
method-dispatch facility. Package artifact paths remain existing validated labels;
ordinary document links retain the existing package contract. No metadata value
is opened as a path, executed or fetched.

`receive(acquisition, claim, trusted_job, reader)` returns:
- ACK: {protocol_version: 1, type: "ACK", receipt:
  {package_id, package_digest, content_fingerprint}}
- ERROR: {protocol_version: 1, type: "ERROR", code: "seal_rejected"}

These objects can be canonical-encoded into ACK/ERROR frames. Errors never echo
input or internal exception messages. Malformed, duplicate, unknown, truncated,
out-of-order and trailing frames reject the entire import.

The future protected registry resolves trusted_job independently of input.
Every HELLO binding field must equal that job; claim fields and SQLite task/root
identity are checked independently. The job registry owns job_id and root_type;
this step deliberately adds no job schema.

After ACK loss, replay the complete stream with the same immutable job binding
and still-live sealed claim. Every byte is revalidated before returning the
existing receipt. Same binding with changed canonical package digest rejects.
Expired/replaced claims and cancellation reject retries. A crash after DB commit
and before ACK can therefore be retried while the claim is live. The future
registry must retain job bindings; this primitive does not invent authority from
self-reported job IDs.
