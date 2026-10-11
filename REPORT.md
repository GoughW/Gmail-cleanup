# Final validation report — 11 October 2026

The project is ready for a small, separately authorized production pilot using the
individual plan/create/verify/Trash operations. No real-mailbox access or mutation
was performed during finalization. The full offline suite and a five-message
disposable-account end-to-end test provide the evidence below; broader mailbox
preservation is not assumed.

## Offline validation

The final full suite passed: **168 tests, none failed or skipped**, using
`.venv/bin/python -m unittest discover`. Tests cover text/HTML, received/sent-style
fixtures, reply threads, selective attachment removal, inline images, Unicode and
duplicate filenames, 4 MiB attachments, Drive integrity, metadata settling,
interrupted operations, retry/reconciliation, failed verification and Trash recovery.

The Received-header regression reproduces two existing trace headers becoming
three after insertion. Narrow root transport exceptions still reject semantic
header, content, label, thread and timestamp changes. Recovery reuses the recorded
replacement without another insertion. CI runs offline tests without credentials.

## Completed disposable live test

All five explicitly allowlisted dummy messages passed, processed one at a time:
attachment backup/reuse → full downloaded Drive SHA-256 verification → reconstruction
without the selected attachment → Gmail insert → strict semantic/metadata/thread
verification → original moved to Trash → post-Trash verification. An existing
replacement was successfully reverified and reused; the other four were inserted.
A final read-only pass confirmed all five replacements in the shared thread and
all five intact originals in Trash. No sending or existing Drive-file modification
was involved.

The selected attachments were identical four-byte files and shared one verified
Drive counterpart. Every source/download hash matched. All replacements retained
From/To/Cc/Subject, Date/internalDate, Message-ID and reply headers where present,
labels and thread identity. Ordering deltas were zero. Decoded bodies with the
approved note, attachment removal and remaining MIME content matched the plan.
Gmail retained duplicate Message-IDs and added one Received header per replacement.
Invalidated DKIM/ARC authentication assertions were removed and audited.

The user additionally inspected Gmail UI and confirmed correct sender, recipient,
subject, timestamp and thread placement, plus the intended note/link/hash rendering.
This is user-reported UI evidence; the agent's independent checks used the API.

Per-message IDs, filenames, links, hashes, Date/threading-header values, metadata
snapshots, all header differences and durable events remain in ignored private live
audit files. No actual mailbox or Drive IDs, links, credential values or raw email
contents are included in the committed report or examples.

## Production contract and setup

Required scopes are exactly `https://www.googleapis.com/auth/gmail.modify` and
`https://www.googleapis.com/auth/drive.file`. Required API runtime hosts are exactly
`oauth2.googleapis.com`, `gmail.googleapis.com` and `www.googleapis.com`. Local
consent and development installation hosts are documented separately. Originals
finish in Trash only after successful fresh verification and explicit approval.

Drive checks pin file identity, expected name/destination, size, binary/non-trashed
state and complete downloaded content hash. Timestamps, version counters and
asynchronous checksum/type metadata are audited, rather than treated as arbitrary
immutability requirements; metadata churn prompts another full content hash check.

Gmail comparison tolerates only the explicitly listed root transport/authentication
headers and Received. The exact approved reconstruction is validated first;
correspondence/threading headers, nested MIME headers and contents stay strict.
Every header difference is audited with value hashes and sequence/casing data.
The readable plain/HTML note includes each removed filename, verified retained
Drive URL and matching SHA-256; it does not change Drive sharing permissions.

Retry behavior, durable state and read-only recovery are documented in README.md.
Installation/startup instructions and the minimal explicit API-host list are saved
in the environment draft. Publishing that draft requires the environment UI's
Publish action; no callable publication tool is available to this agent.

## Remaining limitations and pilot steps

- Live evidence covers five received-style emails in one thread. Sent-mail import,
  other thread layouts, custom label combinations and large live attachments still
  need separate disposable fixtures before using those cases in production.
- Signed/encrypted MIME, nested attached messages and ambiguous layouts are skipped.
  UTF-8/base64 bodies and appended notes change raw bytes and invalidate original
  delivery signatures; the copy does not preserve transport authenticity.
- Date and internalDate often differ in real mail. A nonzero replacement ordering
  delta blocks Trash; the tool does not silently change headers to force preservation.
- Gmail/Drive operations are not atomic. External edits between checks and writes
  remain possible. Run one operator per state database and retain a secure backup.
- Gmail normally removes originals from Trash after 30 days. Long-term Drive
  retention/access monitoring and reversal automation are outside this version.
- Drive file access is app-scoped. Older archives may be inaccessible without
  explicit app authorization; native Google documents are not binary counterparts.
- Large raw MIME messages are held in memory; the offline 4 MiB case is not proof
  of every API size limit. Note overhead can exceed small attachment savings.
- External/Testing OAuth tokens normally expire after seven days; Google's app
  verification requirements are separate from this repository's readiness.

Before the real-mailbox pilot: publish the final environment draft; obtain fresh
two-scope consent for the intended real account and configure it securely; perform
read-only account/grant/host checks; choose one supported message with a zero
predicted date delta and explicit attachment selections; use a separate private
manifest/database and review its Drive backup/plan; authorize creation, inspect the
verified replacement in Gmail, then approve Trash using the exact plan hash and
replacement ID. The real-mailbox pilot is not authorized by this finalization task.
