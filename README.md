# Gmail attachment cleanup

A small synchronous Python CLI using the Gmail and Drive APIs directly. An explicit
message-ID / MIME-part / decoded-SHA-256 allowlist selects attachments. The workflow
is **Gmail read → Drive backup → independent content verification → reconstruct →
Gmail insert → strict verification → separately approved original-message Trash**.
Nothing is sent. The replacement has a new Gmail ID. The tool has no other removal
path, scheduler, GUI, connector, or automatic search-result cleanup.

**This revision is submitted for independent re-review, not approved for live use.**
The historical five-message disposable test predates the reviewer fixes. This task
used only fake services and loopback servers; no account access was performed.
Start with [MAINTAINER.md](MAINTAINER.md) for the complete operator/agent mental model,
[REPORT.md](REPORT.md) for evidence and limitations, and
[docs/RECOVERY.md](docs/RECOVERY.md) before any interrupted-run recovery.

## Reviewed installation and offline gate

Supported: **Linux, Python 3.12**, owned private local storage honoring fsync and
hard-link/rename semantics. Other Python versions/platforms are not declared safe.
Pango/font libraries are needed for the independent offline renderer; on Ubuntu:

```sh
sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core
python3.12 -m venv .venv
.venv/bin/python -m pip install --no-deps -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -m pip check
.venv/bin/python tests/run_offline.py
.venv/bin/gmail-cleanup --help
```

The gate verifies every exact locked dependency, strips Gmail credential bindings,
blocks non-loopback Python network access in the runner and spawned children, and
fails on any test failure or skip. It includes the actual Google SDK/HTTP stack,
local TLS/proxy/fault servers, independent HTML parser/PDF renderer, and SIGKILL
recovery drills. `python -m unittest discover -v` is useful during development but
is not the complete network-isolated, lock-validated acceptance gate.

## Local dry-run walkthrough

All commands here are local. The generated links and IDs are fictional.

```sh
mkdir -m 700 -p .private
.venv/bin/gmail-cleanup-synthetic generate --directory .private/demo
.venv/bin/python - <<'PY'
from gmail_cleanup.api import OfflineMailbox
print(OfflineMailbox('.private/demo/offline-sources.json').mailbox_identity())
PY
```

Use that `offline:<hash>` as `OFFLINE_MAILBOX_KEY` below. Initialization is explicit
and occurs only once for a genuinely new local inventory/state/journal:

```sh
.venv/bin/gmail-cleanup plan --offline-sources .private/demo/offline-sources.json \
  --manifest .private/demo/offline-selections.json --message-id offlineplain \
  --state .private/demo/audit.sqlite --journal .private/demo/recovery \
  --initialize-state --mailbox-key OFFLINE_MAILBOX_KEY --output .private/demo/plan.json
.venv/bin/gmail-cleanup create --manifest .private/demo/offline-selections.json \
  --message-id offlineplain --state .private/demo/audit.sqlite \
  --journal .private/demo/recovery
```

The second command defaults to dry-run and makes no network request. The offline
inventory adapter cannot insert or Trash. Full simulated backup/insert/Trash flows
use observable fake services in `tests/test_engine.py`, `test_drive_preflight.py`,
`test_trash_workflow.py` and the interruption/recovery tests.

## Later, separately authorized operations

Do not run these against an account before independent review and explicit task
authorization. [GOOGLE_SETUP.md](docs/GOOGLE_SETUP.md) explains account consent,
identity confirmation and secure configuration. Required production scopes are
exactly:

```text
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/drive.file
```

Required API runtime hosts: **oauth2.googleapis.com**, **gmail.googleapis.com**,
**www.googleapis.com**. Browser consent, Drive-viewer links and public dependency
installation have separate hosts described in the setup guide.

Create a version-2 selection manifest after read-only inspection, for example
[backup-selection.example.json](examples/backup-selection.example.json). Select
parts by path/hash, never filename alone. Prepare private original `.eml` and
attachment backups independently, confirm the intended Gmail and Drive accounts,
and calculate the expected mailbox key as SHA-256 of the Gmail profile email
address stripped and case-folded (UTF-8). Do not infer identity from a token name.

Use one state database and one authoritative journal for the mailbox. Put the
journal **outside the database restore unit**, e.g. a separately retained directory.
New state requires `--initialize-state --mailbox-key EXPECTED_MAILBOX_SHA256` on
its first preflight/plan only; existing state must never be reset to bypass a stop.
The default `<state>.recovery` location is convenient but still must be independently
retained, not restored with an old directory/environment snapshot.

```sh
# Read/download preview; add initialization flags only for genuinely NEW state.
gmail-cleanup preflight --env-auth --manifest PRIVATE_SELECTIONS \
  --state PRIVATE_STATE --journal INDEPENDENT_JOURNAL \
  --output PRIVATE_PREVIEW --resolved-manifest PRIVATE_RESOLVED

# Explicit Drive creation permission; no Gmail write.
gmail-cleanup preflight --env-auth --upload-backups --manifest PRIVATE_SELECTIONS \
  --state PRIVATE_STATE --journal INDEPENDENT_JOURNAL \
  --output PRIVATE_PREVIEW --resolved-manifest PRIVATE_RESOLVED

gmail-cleanup plan --env-auth --manifest PRIVATE_RESOLVED --message-id ORIGINAL_ID \
  --state PRIVATE_STATE --journal INDEPENDENT_JOURNAL --output PRIVATE_PLAN

# Review exact plan first. Default create is a local dry-run; execution is explicit.
gmail-cleanup create --execute --env-auth --manifest PRIVATE_RESOLVED \
  --message-id ORIGINAL_ID --state PRIVATE_STATE --journal INDEPENDENT_JOURNAL

gmail-cleanup verify --env-auth --manifest PRIVATE_RESOLVED --message-id ORIGINAL_ID \
  --state PRIVATE_STATE --journal INDEPENDENT_JOURNAL

# Separate manual approval AFTER inspecting verification and the Gmail UI.
gmail-cleanup trash --execute --enable-trash --env-auth --manifest PRIVATE_RESOLVED \
  --message-id ORIGINAL_ID --state PRIVATE_STATE --journal INDEPENDENT_JOURNAL \
  --approve-plan EXACT_PLAN_SHA256 --approve-replacement EXACT_REPLACEMENT_ID
```

Each command validates output aliases before authentication or state initialization.
Reports are not execution authority. Approved plans/bytes cannot be overwritten.
Stop at the first uncertainty. Do not clean up extra replacements automatically.
The sequential `workflow` command is restricted to disposable-account operation;
a later pilot uses separate single-message creation and manual Trash approval.

## Supported content and retention

Supported plain-text bodies have only recognized charset parameters; flowed/delsp,
unknown/duplicate/malformed parameters fail before backup upload. HTML supports a
small balanced, unstyled passive profile. Active/raw-text/style content, hiding
attributes, parse repair, unfinished comments and unusual nesting are rejected.
Signed/encrypted MIME, attached emails and ambiguous layouts are rejected.
**Every selected Content-ID or Content-Location resource is protected**, even if
apparently unreferenced. Unselected attachments and inline images remain exact.

Drive proof requires the same file ID/name/parent/size, non-trashed binary state,
and a complete downloaded SHA-256 matching source, bracketed by an unchanged valid
version. One changed bracket is discarded and retried once; a second changed
bracket fails. Missing checksum is allowed; a populated contradictory SHA-256 fails.
Timestamps and other benign metadata are audited, not assumed immutable.

Text notes append the original filename, canonical Drive `webViewLink` and verified
SHA-256. HTML notes insert before the unambiguous closing body or at a validated
fragment boundary; original source outside that span is untouched. Filenames and
links are escaped, parsed href equals the canonical URL, and following punctuation
stays outside the anchor. Drive backups remain private. See the exact format and
transport-header policy in [MAINTAINER.md](MAINTAINER.md).

Gmail normally expires Trash after 30 days. Retain independent originals and Drive
backups beyond the review period; ongoing retention monitoring is outside scope.
Remote operations are not atomic. See [REPORT.md](REPORT.md) for residual risks.
