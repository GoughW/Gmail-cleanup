# Gmail attachment cleanup

**Taking over this repository? Start with [MAINTAINER.md](MAINTAINER.md)** for the
architecture, safety gates, state recovery, simulations and pilot checklist.

A small Python CLI using the Gmail and Drive APIs directly. It backs up explicitly
selected attachments, verifies the retained bytes, inserts a reconstructed email
without sending, and verifies preservation before moving its original to Trash.
Gmail assigns a new message ID. Each message must be explicitly allowlisted; search
results are never processed automatically. There is no GUI, scheduler or connector.

The five-message disposable-account workflow passed API verification and the user
confirmed correct Gmail UI presentation. Start production use with a small,
individually reviewed pilot; see [REPORT.md](REPORT.md) for remaining limitations.

## Install, credentials and network

Python 3.11+; offline processing/tests need only the standard library. Use the
existing isolated checkout:

```sh
cd /workspace/Gmail-cleanup
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[gmail]'
.venv/bin/python -m unittest discover -v
.venv/bin/gmail-cleanup --help
```

Enable Gmail API and Drive API in your own Google Cloud project. Use Desktop OAuth
and your own credentials, following [the full setup guide](docs/GOOGLE_SETUP.md).
The production workflow requires exactly:

```text
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/drive.file
```

The cloud runtime needs exactly these API hosts: `oauth2.googleapis.com` (refresh),
`gmail.googleapis.com` (Gmail), `www.googleapis.com` (Drive metadata/upload/download).
Local browser consent and dependency installation have separate network needs
described in the setup guide. Preserve proxy settings, CA trust and TLS verification.

## Inspect and choose attachments

Use a private directory and one durable state database per mailbox. Do not mix a
real-mailbox pilot with the disposable account's database or manifests. Back up that
database; it records uncertain remote operations and prevents duplicate retries.

Start with a version-1 inspection manifest containing only explicitly selected IDs
and empty attachment selections:

```json
{"version": 1, "allowlist": ["GMAILID"], "messages": {"GMAILID": []}}
```

```sh
gmail-cleanup inspect --env-auth --manifest .private/pilot/inspect.json \
  --message-id GMAILID --output .private/pilot/inventory.json
```

Select by original MIME part path and decoded SHA-256, never filename alone. `0` is
the root; `0.1` is its second child. Replace all placeholders with actual inspected
values. The [backup example](examples/backup-selection.example.json) illustrates a
version-2 manifest; its IDs/hashes are fictional:

```json
{
  "version": 2,
  "allowlist": ["GMAILID"],
  "messages": {
    "GMAILID": [{"part": "0.1", "decoded_sha256": "64 lowercase hex digits"}]
  }
}
```

An optional `drive_file_id` pins an intended existing binary counterpart that the
OAuth application can access. `drive.file` does not expose your whole Drive; if an
earlier archived file is inaccessible, authorize it to this application through a
supported Google file-selection flow or enable a new app-created backup. This tool
does not include that file-selection UI and does not broaden its required scopes.

## Back up, hash-verify and plan

```sh
# Default preview: locate/read/download only; no new Drive files or Gmail changes.
gmail-cleanup preflight --env-auth --manifest .private/pilot/selections.json \
  --state .private/pilot/audit.sqlite --output .private/pilot/preview.json \
  --resolved-manifest .private/pilot/resolved.json

# Explicitly enable Drive backup creation; Gmail remains read-only.
gmail-cleanup preflight --env-auth --upload-backups \
  --manifest .private/pilot/selections.json --state .private/pilot/audit.sqlite \
  --output .private/pilot/preview.json --resolved-manifest .private/pilot/resolved.json

gmail-cleanup plan --env-auth --manifest .private/pilot/resolved.json \
  --message-id GMAILID --state .private/pilot/audit.sqlite \
  --output .private/pilot/plan.json
```

Preview validates source hashes and conservative MIME support before any upload.
It locates app-tagged counterparts by hash or creates one only when enabled. The
default folder is **Gmail attachment backups**; `--drive-folder-id ID` chooses an
accessible destination. Equal-byte attachments may share a retained file. Multiple
matching files require an explicit file ID. A corrupt tagged file is a failure,
rather than permission to create another copy and hide the problem.

Drive verification requires the same intended file ID, expected name/parent
destination, non-trashed binary state, expected size, and complete downloaded
SHA-256 matching the decoded source attachment. Metadata is checked before and
after downloading, with expected identity/destination retained across later gates.
Deduplication pins the retained counterpart's name, which can differ from the
source filename. Google-native documents are not accepted as binary counterparts.

Version counters, timestamps, asynchronously populated checksums and binary MIME
detection are advisory. Metadata changes and their classifications are audited;
metadata churn triggers a bounded second complete download/hash check. A real
identity/destination/size/content mismatch blocks insertion or Trash. Drive checksum
metadata alone is insufficient. The tool exposes creation, listing, reading and
downloading; it does not update, move, share or trash existing Drive files.

Only a fully successful preview writes resolved version-1 selections and plans.
A manually supplied link/hash can be used for planning, but cannot bypass fresh
Drive download verification before creation and Trash. The exact plan contains
selected parts, decoded hashes, retained URLs, original/replacement inventories,
header edits, labels, thread, timestamps, size deltas and a plan SHA-256.

## Create, verify, then separately approve Trash

```sh
# Default dry-run; no authentication or mailbox request.
gmail-cleanup create --manifest .private/pilot/resolved.json \
  --message-id GMAILID --state .private/pilot/audit.sqlite

# After reviewing the exact plan and authorizing this message's replacement:
gmail-cleanup create --execute --env-auth --manifest .private/pilot/resolved.json \
  --message-id GMAILID --state .private/pilot/audit.sqlite

gmail-cleanup verify --env-auth --manifest .private/pilot/resolved.json \
  --message-id GMAILID --state .private/pilot/audit.sqlite

gmail-cleanup status --manifest .private/pilot/resolved.json \
  --message-id GMAILID --state .private/pilot/audit.sqlite

# After reviewing the verified replacement and explicitly approving Trash:
gmail-cleanup trash --execute --enable-trash --env-auth \
  --manifest .private/pilot/resolved.json --message-id GMAILID \
  --state .private/pilot/audit.sqlite \
  --approve-plan EXACT_PLAN_SHA256 --approve-replacement EXACT_REPLACEMENT_ID
```

Creation regenerates approved bytes, checks the original and Drive, persists insert
intent, and calls `users.messages.insert` with `internalDateSource=dateHeader`.
It never sends. The original remains untouched until the separately enabled Trash
operation passes fresh original, replacement, thread and Drive checks. Trash is the
production behavior; it calls `users.messages.trash` on the exact original ID.

Verification requires exact From/To/Cc/Subject and other non-transport root headers,
Message-ID/In-Reply-To/References, Date/internalDate, labels including
UNREAD/STARRED/SENT, thread identity and membership, operation marker, decoded
plain/HTML content including the approved note, attachment removal and every
remaining resource hash/header. Signed/encrypted MIME, nested attached emails,
malformed encodings and ambiguous layouts are skipped with reasons. Inline images
and CID-referenced resources are protected. Unselected attachments remain exact.

The Date header and Gmail internalDate can differ in real mail. Any replacement
internalDate difference from the original fails verification even if it agrees
with the requested dateHeader policy. The plan reports the predicted ordering
delta; choose a zero-delta pilot first. Headers are never silently changed to force
threading or duplicate Message-ID handling. No general preservation guarantee is
inferred from the limited live test.

## Cleanup note and transport headers

Original decoded correspondence is retained and a note is appended to each text
and HTML body. Text uses this format once per removed attachment:

```text
[Attachment cleanup: retained copies; SHA-256 recorded below]
Removed attachment: <original filename>
Retained copy: https://drive.google.com/file/d/<verified file ID>/view
SHA-256: <verified decoded source/Drive SHA-256>
```

HTML uses a separator and list containing each escaped filename, clickable Drive
URL and the same hash. The link identifies the verified retained file; creating the
note does not change sharing permissions. Bodies normalize to UTF-8/base64, with
changes audited. Tiny attachments may cost less space than the note; actual size
deltas are reported. Removing invalid transport signatures can also reduce size.

Reconstruction removes root DKIM-Signature, DomainKey-Signature,
X-Google-DKIM-Signature, ARC-Seal, ARC-Message-Signature,
ARC-Authentication-Results, Authentication-Results, Received-SPF and
X-Google-Original-Authentication-Results. Original Received trace remains in the
reconstruction. Post-insert comparison tolerates only these root transport/auth
headers and Received differing or being added by Gmail. It regenerates the exact
approved bytes and validates their persisted hash/inventory/header fingerprints
before applying this narrow exception. All other root headers and all nested MIME
headers/content remain strict. Both original-to-replacement and expected-to-observed
header differences are audited for every MIME part, including value hashes and
order/casing sequences. The replacement does not retain original DKIM/ARC authenticity.

## Interrupted runs and retries

Keep the same mailbox-specific database and manifests. Run one operator at a time;
do not bypass an incomplete operation by starting a new database. API writes have
no automatic retry. SQLite saves operation intent and events before mutations.

| Saved state | Recovery |
| --- | --- |
| Drive `upload_pending` | Reuse the saved pre-generated file ID; inspect it before a same-ID create retry. |
| `planned` | Recheck source and Drive; creation may proceed from the identical saved plan. |
| `insert_pending` without replacement ID | Outcome is uncertain. Inspect Gmail for the operation marker and reconcile the actual ID; never blindly insert again. |
| `inserted` / `verified` | Reverify the known replacement; creation retries reuse its ID. |
| `verify_failed` | Inspect the audit and created replacement; after fixing the substantive cause, run explicit `verify` on that ID. The original stays untouched. |
| `trash_pending` / `trashed` / `trashed_verify_failed` | Recover by read-only inspection of the recorded original/replacement/Drive state, without another Trash or insert request. |

Inspect a candidate with `reconcile --replacement-id ID`; add `--execute` only to
persist local recovery and run verification, not to insert another email. `trash`
with the recorded approvals also performs read-only recovery once Trash intent
exists. After Trash, the original must still be present with its exact bytes,
thread and timestamp and a TRASH label. Replacement and Drive checks run again.
A fetch/auth error cannot establish success. A confirmed post-Trash failure is
reported and stops progression; it cannot undo a completed remote write.

## Disposable synthetic harness

`gmail-cleanup-synthetic generate --directory .private/fixtures` produces eleven
offline fixtures covering bodies, inline images, Unicode/duplicate names, selective
removal, large attachments, sent-style messages and a reply pair. Import fixtures
yourself into a disposable mailbox without sending, and record their IDs for
`register`. Registration checks exact generated bytes, marker and metadata.
`preflight` backs up selected attachments; separately enabled `run --execute`
creates replacements only. The harness never sends or trashes originals.

The `workflow` command completes each allowlisted message before the next and stops
at the first failure. Its execution requires `--execute --enable-trash
--disposable-test-account`; use it only for disposable accounts. A production
pilot uses the separately approved individual operations above. The completed live
test's manifests/database remain private and must not be rerun as fresh operations.

## Private records and retention

Keep credentials, tokens, raw email, manifests, reports and SQLite audits in ignored
`.private/` or outside the repository. New private files are mode 600 and directories
mode 700; final symlinks are refused. Audits can contain subjects, filenames, Gmail/
Drive IDs, labels, Date and threading-header values, links and hashes. They do not
contain raw email bodies, recipient lists or OAuth tokens. Treat them as sensitive.
Never enable HTTP debug logging or shell tracing. `.gitignore` is a guard against
accidental staging, not permission to force-add private files.

Trash is temporary retention: Gmail normally removes originals after 30 days.
Attachment backups must remain accessible and unchanged; ongoing retention
monitoring is outside this first version. Remote calls are not atomic, so external
changes between verification and mutation remain a residual risk.
