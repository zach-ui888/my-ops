# Phase 2 Step 2B — offline complex source parsing

## Delivery status

Implementation and automated fixtures are present. **Runtime-format acceptance is pending**:
this restricted project environment has Python 3.10 but no pypdf, Pillow, defusedxml,
pip, local virtual environment or wheel bundle. Network access was prohibited and
no dependency download, system installation or external filesystem search was attempted.
Do not release as fully accepted until the pinned dependencies are provisioned by the
normal offline deployment process and every dependency-gated test runs without skips.

Runtime dependencies are formally pinned in pyproject.toml:
`pypdf==6.1.1`, `Pillow==11.3.0`, `defusedxml==0.7.1`.
These pins require offline deployment validation; no claim of current vulnerability
review or installed-version verification is made.

## Integration and compatibility

The Step 2A application, protocol, database migrations, collection barrier, claim,
staging and transactional publication architecture is retained. No business entities
are created. All envelopes, blocks and manifests remain untrusted_source_data.
Parser/policy versions are now offline-content/2 and bounded-offline/2.

The sanitized-content-v1 schema is additively extended with source status `partial`
and page/part/sheet/cell/value_kind/sheet_state/image descriptor locator fields.
Existing v1 text envelopes remain valid. Consumers that hardcode the old two-status
or line-only locator model must accept this updated schema before using Step 2B.
`completed` means nonempty extracted text with no detected gap; `partial` means safe
processing with explicit gaps, possibly no text; `failed` contains no retained blocks.
Batch completed requires every source completed; a batch with any completed or partial
source and any gap/failure is partial; all-failed is failed. Partial sources remain
`complete=False`, so the existing critical-source formal-generation gate blocks them.

## Parser behavior

* PDF: strict pypdf, PDF signature, no password decryption, page locators, existing text
  layer only. Textless pages, XObjects, paint operators, annotations, Type3 fonts and
  active/auxiliary document content create gaps. No attachments/actions are executed,
  no external resources fetched. Page content streams are preflighted with bounded
  Flate decompression; unsupported filter chains fail closed. This intentionally rejects
  some otherwise valid PDFs. This is not a renderer or a claim of layout comprehension.
* DOCX: verified ZIP and content-type/main-part identity. Body paragraphs, including
  paragraphs in table cells, use stable document-order paragraph indexes and part names.
  Textboxes, drawings, SmartArt parts, revision markers, fields, math, auxiliary stories,
  embedded objects and external relationships create explicit gaps. Retained text in
  revisions/complex components is incomplete source text, never an accepted revision.
* XLSX: workbook sheet relationships are resolved only into validated in-memory ZIP
  parts; they never become filesystem paths. Cells carry sheet name, coordinate, sheet
  state and value kind. Formula text and cached values are separate blocks, with an
  unevaluated-formula gap; caches are not asserted current. Hidden sheets are retained
  and flagged conservatively. Drawings, external links, merges and complex parts create
  gaps. No formula, DDE, macro, script or linked content is evaluated.
* PNG/JPEG: signature plus Pillow format verification and pixel decoding. Only a
  metadata-free format/width/height descriptor appears in an issue locator. No image
  files are published, no EXIF enters text, no OCR or visual semantics are generated.
  Every valid image is partial with visual_unresolved and an empty block list.
* DOCM/XLS/XLSM are unsupported. Macro-enabled OOXML containers and VBA payloads fail.
  Nested archive bytes are never expanded. ZIP entries are never extracted to disk.

## Resource policy

Upload/source remains 10 MiB. OOXML: 2,000 entries, 32 MiB per entry, 100 MiB total
uncompressed, 100:1 compression ratio; encrypted entries, unsupported compression,
traversal, absolute/drive paths, symlinks and duplicate normalized paths are rejected.
Every XML/rels part is validated with DTD, entity and external entity forbidden;
maximum depth 64 and 100,000 nodes per XML part. Aggregate XML bytes are bounded by
ZIP limits. XML/parser failures return fixed codes, never exception messages.

PDF: 300 pages, 32 MiB expanded page content stream budget. General complex text:
64 KiB per block, 8 MiB total UTF-8 output and the existing schema's 20,000 block cap.
XLSX: 50 sheets, 50,000 rows per sheet, 512 columns, 500,000 visited cells, 64 KiB text.
The existing 20,000 output-block cap may reject a spreadsheet below the cell limit;
it is deliberately retained to avoid enlarging the validated envelope contract.
Formula and cache each consume a block. Limits fail the source instead of silently
truncating content. Images: 20,000 pixels per side and 25 million pixels total,
plus Pillow decompression-bomb checks. These are bounded parser limits, not OS-level
CPU/memory isolation; in-process library parsing still requires trusted pinned runtimes.

## Lease policy

`process()` starts one lifecycle-bound heartbeat thread, renewing via the existing
heartbeat transaction every lease/3, including while the parser is running. The thread
is stopped and joined before publishing. Any renewal error prevents publication.
No alternative claim/fencing mechanism was introduced. Staging and publication both
still check worker, attempt, fencing token, cancellation, expiry and run status.
Scheduler stalls or GIL starvation can lose a lease; the old worker then cannot publish,
and an expired claim can be reclaimed normally. Direct claim/parse/publish callers
remain responsible for heartbeat, exactly as in Step 2A. No parser cancellation or
hard wall-clock kill is claimed.

## Validation

Commands (from project root):

```
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src tests examples
sha256sum templates/testcase_template.xlsx
```

Final run: 159 tests, 139 passed, 20 dependency-gated tests skipped, no failures.
All original 126 test cases remain, with the obsolete unsupported-PDF assertion updated
to signature_mismatch because PDF is now supported. New executed cases cover ZIP safety,
controlled errors, periodic renewal, lost-lease fencing, partial publication/gating and
mixed completed/partial/failed batches. Skipped fixtures cover real PDF, DOCX, XLSX and
Pillow image processing; these are mandatory remaining acceptance work.

compileall passed. Template unchanged, SHA256:
`f9b29ba0a28105cd5d8bb00fd749fc03665f65fbc2f050ca7504b7337d69f2a6`.
Test scratch data stays under ignored .test-runtime; bytecode uses ignored __pycache__.
No credentials, real environment files, external integrations, TG-Codex Agent changes,
network access, subprocess execution of source files, template edits or Git publication.
The supplied workspace is not recognized as a Git repository by git status; modified
files are recorded explicitly below rather than inferred from Git.

Files: src/tg_testcase/complex_content.py (new), src/tg_testcase/content.py,
src/tg_testcase/processor.py, schemas/sanitized-content-v1.schema.json,
pyproject.toml, tests/test_complex_content.py (new), tests/test_processor.py,
docs/PHASE2_STEP2B_DELIVERY.md (new).
