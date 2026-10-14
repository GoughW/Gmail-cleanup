# Maintainer and operator handoff — read this first

Purpose: remove explicitly selected Gmail attachments only after retaining their
exact decoded bytes on Drive. Gmail cannot edit attachments in place, so the tool
imports a replacement, verifies it, then separately approves original-message
Trash. A new Gmail ID is unavoidable. Nothing is sent and no thread-wide write is
exposed. This revision implements reviewer findings F1–F8 from baseline
`b21638b71f6f100abfeb1d3ad40bc041a340e8af`; **independent re-review is required before
any live pilot**. Historical disposable-account success does not validate the new
policies. No live account/OAuth access occurred during remediation.

Read this first, [README.md](README.md) for commands,
[docs/GOOGLE_SETUP.md](docs/GOOGLE_SETUP.md) for consent/hosts,
[docs/RECOVERY.md](docs/RECOVERY.md) for recovery, and [REPORT.md](REPORT.md) for
before/after evidence, full test results and unresolved validation.

## Architecture and repository map

Linux/Python 3.12, one synchronous CLI/operator, standard-library MIME parser,
html5lib validation, private SQLite with synchronous=FULL, an independent write-once
hash-chained journal, and the exactly locked Google SDK/auth/HTTP stack. No web
service, worker, GUI, scheduler or connector. SDK discovery is static; messages
and upload payloads are in memory. Ordinary logs never contain raw correspondence
or credentials. Audits contain sensitive IDs, filenames, header values and hashes.

| File | Role / next place to inspect |
| --- | --- |
| `cli.py`, `synthetic.py`, `workflow.py` | CLI defaults, explicit allowlists, synthetic guards, sequential stop-on-failure orchestration. |
| `manifest.py` | Version-1 retained selections; version-2 backup selections; exact IDs, part paths and decoded hashes. |
| `paths.py` | Pre-authentication alias policy, pinned directory/leaf descriptors, protected input/output/state/journal paths. |
| `storage.py`, `recovery.py` | Atomic artifacts, lifecycle flock, SQLite/journal comparison, durable intents, local restoration and non-authoritative legacy inspection notes. |
| `api.py`, `transport.py`, `requirements.lock` | OAuth/direct API adapters and exact reviewed single-submission transport. |
| `mime.py`, `html_profile.py` | Supported MIME/body profile, protected resources, source-preserving notes, inventories and narrow transport exceptions. |
| `drive.py`, `preflight.py` | Preallocated uploads/deduplication, two bounded coherent complete downloads, canonical links and locally reconstructed plans. |
| `engine.py` | Policy version 2, immutable approval/bytes, exact create/verify/reconcile/Trash and post-Trash gates. |
| `fixtures.py`, `examples/` | Fictional inputs; synthetic cases and selection formats. |
| `tests/helpers.py`, `process_helpers.py` | Observable fake accounts; independent file-backed remote state for process-kill tests. |
| `tests/test_mime_policy.py`, `test_drive_stability.py` | Independent correspondence/HTML rendering and content-stability regressions. |
| `tests/test_artifact_safety.py`, `test_recovery_journal.py`, `test_process_interruptions.py` | Aliases, syscall failures, concurrent operators, stale restores, unsafe legacy fixtures and SIGKILL drills. |
| `tests/test_transport_faults.py` | Actual locked SDK + local fault/TLS/proxy servers, never real Google endpoints. |
| `tests/baseline_reproductions.py` | Defect evidence run against an archived reviewed baseline only, not an acceptance test. |
| `tests/run_offline.py`, `tests/network_guard/`, `.github/workflows/offline.yml` | Required complete lock-validated, loopback-only gate and CI. |

Paths in the map are relative to `gmail_cleanup/` unless under `tests/` or explicitly
at repository root. Keep execution inside the engine; do not bypass lifecycle
checks by making a direct API write in a new helper.

## Required runtime configuration

Only these production OAuth scopes:

```text
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/drive.file
```

Only these cloud API hosts: `oauth2.googleapis.com` for refresh,
`gmail.googleapis.com` for Gmail, `www.googleapis.com` for Drive. The browser's
`drive.google.com` viewer and local `accounts.google.com` consent are separate.
Use a user-owned Desktop OAuth project. Credentials imply capabilities, not task
permission. Never extract connector credentials or broaden scopes to bypass a stop.

| Variable / setting | Purpose (no secret values belong here) |
| --- | --- |
| `GMAIL_CLIENT_ID` | Desktop application's OAuth client ID. |
| `GMAIL_CLIENT_SECRET` | Matching client secret, securely routed only to the OAuth host. |
| `GMAIL_REFRESH_TOKEN` | Account-specific two-scope grant, securely routed only to the OAuth host. |
| `GMAIL_OAUTH_SCOPE` | Exact space-separated two scope URLs above; setting it does not extend the grant. |
| `HTTPS_PROXY` / inherited proxy settings | Upstream httplib2 connection/proxy routing; preserve environment policy. |
| `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` | Supported trusted CA bundle fallbacks; TLS verification remains enabled. |
| CLI `--state`, `--journal` | Mailbox-specific SQLite and independently retained recovery directory. |
| CLI `--mailbox-key` | Expected profile-address SHA-256 (strip + casefold + UTF-8); required for new state and checked against authenticated Gmail. |

File-token OAuth is also supported via `--token`; never combine identity/state from
different accounts. Runtime versions must match the reviewed transport lock.
Install **all** `requirements.lock` entries then the project with `--no-deps
--no-build-isolation`; resolving `.[gmail]` alone is not a reproducible installation.

## Exact production sequence and invariants

1. Inspect only explicitly allowlisted original IDs. Fetch raw MIME plus Gmail
   labels/thread/internalDate. Require response ID equals requested ID. Select by
   original MIME path + independently decoded SHA-256, not filename.
2. Validate all selected source bytes and the MIME/body profile before allocation
   or upload. Save a provisional selection/operation digest for backup intents.
3. Locate a tagged intended binary Drive counterpart or explicitly enable a new
   backup. Journal allocation intent; persist the pre-generated Drive ID; journal
   upload intent before create. Inspect that exact ID after any uncertain outcome.
4. Verify identity/name/parent/size/non-trashed/binary state before and after a
   **complete independent download**. Require source SHA-256 and stable valid
   positive version within the accepted bracket. Discard a changed attempt and
   begin a fresh complete one, at most twice. Missing checksum is okay; contradictory
   populated SHA-256 is not. Version regression/missing version is a blocker.
5. Save the exact final plan: source snapshot, selected hashes/URLs, inventories,
   header fingerprints, expected reconstructed-byte hash, labels/thread/dates and
   policy versions. Plans and existing approvals are immutable. Regenerated bytes
   must match the approved hash, never silently substitute a new transformation.
6. Recheck source and Drive, journal exact account/original/operation/plan insert
   intent, persist `insert_pending`, then call `users.messages.insert` with
   `internalDateSource=dateHeader`. No sending. Persist a distinct replacement ID.
7. Fetch and strictly verify replacement, original, current Drive proof and thread
   membership. Failed verification leaves the original unchanged and records any
   created replacement. Known-ID retries reverify it, never insert another copy.
8. Require current policies, intact matching journal/database, authenticated account,
   exact original and replacement identities, successful fresh checks, and separate
   `--execute --enable-trash --approve-plan HASH --approve-replacement ID` approval.
   Journal that approval-bound intent and persist `trash_pending` **before** calling
   `users.messages.trash` on the exact original ID.
9. Verify the original still exists in Trash with intact bytes/date/thread, plus
   replacement and Drive preservation. Post-write uncertainty/failure stops the
   workflow; it cannot reverse a remote write.

Metadata/content that stay exact: From/To/Cc/Subject and all other non-exempt root
headers, Message-ID/In-Reply-To/References, Date/internalDate, requested labels
including UNREAD/STARRED/SENT, thread ID/membership, operation marker, approved
plain/HTML note-bearing content, selected removal, and every remaining MIME
resource's decoded bytes/headers. Original decoded plain correspondence is an
exact prefix; original HTML source is exact outside the validated insertion span.
Charset/transfer encoding becomes UTF-8/base64 with audited metadata changes;
Content-Language is retained. Original Date versus internalDate can differ; any
ordering delta blocks eligibility. Duplicate Message-ID/thread import behavior is
verified, never repaired by changing headers.

## Conservative MIME/HTML and cleanup note

Body Content-Type accepts only a recognized nonempty charset parameter. Reject
flowed/delsp, all other parameters, duplicates, parser defects, unsupported/lossy
charsets, ambiguous MIME, attached emails, signed/encrypted messages. Reject
selected inline resources and **all** selected Content-ID/Content-Location parts,
regardless of disposition or apparent reference absence. This intentionally blocks
many normal Gmail attachments with IDs. Unselected resources remain exact.

HTML profile accepts balanced `html/head/body`, `p/div/span`, `br/hr`, `ul/ol/li`,
`b/strong/i/em/u`, `blockquote/pre/code`, `a/img`. Attributes are tightly allowlisted:
link href/title, CID-only images with alt/positive dimensions, limited lang. No
CSS/style/class/id/hidden, scripts/raw-text, external images, implicit closing/repair,
unknown elements, malformed comments or ambiguous document structure. HTML5 parsing
must agree with a safe top-level insertion boundary. Source is not repaired or
reserialized. The composed note must parse as the exact new body/fragment subtree;
an independent offline PDF renderer tests visible filename/hash and exact link.
This is bounded HTML support, not arbitrary-email rendering support.

Plain note, once per selected attachment:

```text
[Attachment cleanup: retained copies; SHA-256 recorded below]
Removed attachment: <original filename>
Retained copy: <verified canonical Drive webViewLink>
SHA-256: <matching decoded source / independently downloaded Drive SHA-256>
```

HTML uses a separator and escaped list, before closing body or at fragment end.
Store Drive's canonical `files.webViewLink` including resourcekey/query parameters;
reject missing/unsafe/wrong-file links. Parsed href must equal that URL; semicolon
and hash follow `</a>`, outside the href. Immutable historical manual links are not
silently rewritten. Backups remain private; a browser must be signed into an
owning/authorized account. Never change sharing to repair a browser sign-in issue.

Root transport/authentication assertions invalidated by reconstruction are removed:
DKIM-Signature, DomainKey-Signature, X-Google-DKIM-Signature, ARC-Seal,
ARC-Message-Signature, ARC-Authentication-Results, Authentication-Results,
Received-SPF, X-Google-Original-Authentication-Results. Received traces are retained
in reconstructed bytes. Post-insert comparison tolerates only these root headers
and Received differences/additions, after validating the approved reference first.
Audit **all** header differences, including sequence/casing and value hashes. No
nested MIME-header exception; no semantic/threading exception. The replacement
cannot retain the original message's DKIM/ARC authenticity.

## Deduplication, retries and durable local evidence

App-tagged files deduplicate by destination folder + decoded content hash; identical
attachments may reuse one retained file under its pinned existing name. Multiple
counterparts/folders require explicit IDs. A corrupt tagged file blocks rather
than prompting an automatic duplicate. Native Google documents are not byte-exact
binary counterparts. `drive.file` can see only app-created/authorized files.

One operator and one authoritative store per mailbox. A flock covers the entire
store lifecycle; different databases/machines are not globally coordinated. Local
paths are checked before auth: input inventories/raw sources/tokens, reports,
SQL/journal sidecars and independent records cannot alias. Compare canonical paths
and existing inode identity; final writable links/hardlinks are refused. Directory
and leaf descriptors are pinned/rechecked. Use private trusted directories, not
shared or attacker-writable storage. Reports never become approval authority.

Serialize JSON first; private same-directory temp, full write/flush/fsync, atomic
replace, parent fsync. Exclusive publication uses no-clobber linking, not a
check-then-replace. Journal records are write-once and hash-chained, appended before
SQLite commits and before remote submission. Missing/torn/conflicting evidence
forces recovery-only behavior. Directory fsync failure after replacement reports
uncertain durability even though a complete new file may be present.

The journal must survive a **database-only** restore. Compare its account/store/head
and full operation/backup/intent maps on every mutation. Journal-ahead state requires
explicit local `recover-state`; it restores known IDs and pending actions without
remote writes. `insert_pending` with no ID remains ambiguous: zero search results
are not proof of absence. Inspect exact candidates; zero/multiple/conflicting/delayed
candidates cannot clear intent. `reconcile` may record one genuinely verified ID.
Never regenerate a possibly existing replacement or change a saved approval.

Unknown Drive allocation requires manual recovery; known-ID uploads may be inspected
and, only via explicit `--recover-drive-upload`, retried once at that same ID with
new durable recovery intent. Trash-pending retries perform read-only recovery, not
another Trash. Legacy/old-policy records stay inspectable/reconcilable; historical
verification may pass but always has `current_policy_eligible=False`. They never
silently receive new Trash eligibility or a rewritten plan.

See [docs/RECOVERY.md](docs/RECOVERY.md) for exact stop/recovery commands. The journal
is local independent evidence, **not an external anti-rollback oracle**. Losing or
rolling back every copy of state and journal cannot be detected. A whole-environment
rollback requires manual independent evidence and blocks automatic resumption.

## Tests, later pilot and residual risks

Run the [README installation and complete gate](README.md#reviewed-installation-and-offline-gate).
Local dry-run fixtures and fake full-workflow tests need no account. The renderer
prohibits URL fetching; actual SDK requests are redirected only to loopback servers.
SIGKILL tests cover process crashes, not all power loss or dishonest-storage faults.

A later 5–10-message **disposable** pilot requires independent re-review, separate
authorization, independently verified original `.eml`/attachment backups, confirmed
Gmail/Drive identities, minimal scopes, one operator, current authoritative
state/journal outside the restore unit, no concurrent Drive writers, and separate
manual Trash approval. Check Gmail UI after verified creation before approving
Trash; retain independent originals/backups beyond review. Stop at first uncertainty.
Real-mailbox work needs an additional explicit authorization after that validation.

Residuals: remote state can change after coherent checks; proxies/servers can replay
outside this client; no global exactly-once transaction; whole-history rollback is
undetectable locally; storage must honor fsync; narrow MIME/HTML support; no new
live validation of policy 2; historical sent/large/custom-label evidence is offline
only; OAuth token lifetime/policy and Trash expiry are external. Review REPORT before
making any broader preservation or operational readiness claim.
