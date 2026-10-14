# Recovery and authoritative local storage

This guide is for a future, separately authorized operator. **No live account
recovery or pilot is authorized by the reviewer-remediation task.** Use
[MAINTAINER.md](../MAINTAINER.md) first. Do not bypass a stop with a new database,
new operation marker, altered approval, or a fresh copy of the original manifest.

## Storage contract

Maintain one mailbox-specific private SQLite database, its lifecycle `.lock`, and
an independently retained write-once recovery directory (`--journal`). Defaults use
`<state>.recovery`, but directory proximity does not make it safe to restore both
from an old snapshot. Put journal retention outside the database/environment restore
unit. Inspect `<state>.inspection` legacy notes as reports, not approval authority.
SQLite sidecars (`-journal`, `-wal`, `-shm`) are protected and must be private regular
singly linked files when present. One operator, Linux/Python 3.12, trusted owned
private directories, supported local storage honoring fsync/link/rename are required.

Initialize only genuinely new state/journal with `--initialize-state --mailbox-key
EXPECTED_KEY`. The Gmail key is SHA-256 of the verified profile email address
stripped/case-folded and UTF-8 encoded. It must match the authenticated Gmail profile
before any mutation. Changing that key or account is not a migration procedure.
The offline inventory key is `offline:` plus the canonical source-inventory digest.

Each journal entry binds store/account, sequence/previous hash, and the complete
operation/backup/intent snapshot. Mutation intent additionally binds original ID,
operation marker, plan digest, action and, where relevant, allocated Drive ID or
exact replacement/approval. Journal publication and directory fsync precede SQL
commit and remote submission. SQL uses synchronous=FULL. A crash after publication
but before SQL commit deliberately leaves recoverable journal-ahead evidence.

For database backups use SQLite's supported backup API, or close **all** database
connections before copying a complete database/sidecar set. Do not copy an active
main file while ignoring rollback/WAL state. Tests use `sqlite3.Connection.backup`
with closed destinations. Retain current journal separately and securely. The
journal hash chain detects missing/conflicting evidence relative to surviving
copies; it is not tamper-proof against a privileged local operator.

## Stop conditions and local restoration

Every mutation compares current journal/database head and maps. Missing, torn,
truncated, unknown, linked, changed-genesis or conflicting history blocks execution.
Historical status and reconciliation remain possible where records are readable.
If only SQLite was restored and the **current complete journal survived**:

```sh
gmail-cleanup recover-state --state PRIVATE_STATE --journal CURRENT_JOURNAL \
  --mailbox-key EXPECTED_MAILBOX_KEY
```

This explicit command writes only local SQL/recovery records. It never authenticates,
inserts, uploads or Trashes. It rebuilds maps from complete evidence, preserves IDs,
approvals and intents, and derives uncertain lifecycle states where a pending
intent precedes the last snapshot. It does not make old policies current or infer
remote absence. A conflicting configured mailbox, malformed evidence, or missing
journal blocks restoration. A completely missing database is not automatically
recreated; independent manual recovery review is required.

**Rollback/loss of every copy of database and journal cannot be detected locally.**
Do not resume from an old whole-directory/VM/environment snapshot as if fresh.
Stop and gather independent account/audit/backup evidence for manual reconciliation.
No external anti-rollback witness or globally exactly-once guarantee is implemented.

## State-specific recovery

| Evidence/state | Permitted next action |
| --- | --- |
| `planned`, no insert intent | Recheck original and Drive under current policies; same immutable plan may proceed only with authorization. |
| Drive allocation intent with no recorded ID | Outcome unknown. Block a new allocation/upload; manual independent recovery. |
| Known Drive ID, no create intent | Resume using the same saved ID after identity checks. |
| Known Drive create intent / `upload_pending` | Read that ID. Existing expected file must pass independent proof. Missing read does not clear intent. Explicit authorized `preflight --recover-drive-upload --upload-backups` may submit at the **same** ID once with durable recovery intent; never allocate another ID or automatically repeat. |
| `insert_pending`, no replacement ID | Search/read only when authorized; exact operation marker + candidate IDs must be reviewed. Empty/delayed search is not proof of non-insertion. No new insert. |
| One exact known insert candidate | `reconcile --replacement-id ID` inspects. `--execute` persists local current-policy recovery after verification; no mailbox mutation. |
| Zero/multiple/conflicting candidates | Remain ambiguous. Do not choose, delete duplicates, change headers or clear intent automatically. |
| `inserted`, `verified`, `verify_failed` with known replacement | Reverify the same ID. Failed checks retain original; do not regenerate the plan or replacement. |
| `trash_pending`, `trashed`, `trashed_verify_failed` | Re-read exact original/replacement/Drive using saved approval; never submit another Trash or insert. Fetch/auth/network failure is not proof of successful Trash. |
| Legacy/old-policy record | Inspect/historically reconcile only, derived `recovery_only`, `current_policy_eligible=False`; never silently rewrite approved bytes/hash/approval or acquire new Trash eligibility. |

Read-only candidate inspection, with explicit account-access authorization:

```sh
gmail-cleanup reconcile --env-auth --manifest PRIVATE_MANIFEST \
  --message-id ORIGINAL_ID --state PRIVATE_STATE --journal CURRENT_JOURNAL \
  --replacement-id REVIEWED_CANDIDATE_ID
```

Add `--execute` only to persist local reconciliation. Legacy inspection notes are
stored separately and never replace saved approvals. For healthy current records,
reconciliation still requires exact binding and genuine current checks. Unknown
insert outcomes are not cleared merely because no candidate is visible yet.

After Trash intent, the same approved `trash` command takes the read-only recovery
path: original must still be fetched intact in Trash, with expected thread/date,
while replacement and Drive remain verified. A substantive failure after actual
Trash cannot be undone by this tool. Stop progression and manually inspect; never
turn a read exception into confirmation of absence/success.

## Common failures

- **Alias/path/private-permission error:** choose distinct private report paths.
  Do not point an output at SQL, journal, credentials, manifests or raw sources.
  Reject final symlinks/hardlinks; move copies deliberately, do not weaken checks.
- **Another operator holds lock:** stop the second process. Do not remove the lock
  or start a different store to run concurrently. Same-account alternate stores or
  hosts are outside the local flock's protection.
- **Drive version changes twice:** suspend writers and recheck; two complete
  brackets are the hard bound. Missing/invalid versions, checksum contradiction,
  content/identity/name/parent/size mismatch remain blockers.
- **Unsupported MIME/HTML:** retain original and report the explicit rejection.
  Do not remove a CID/Location or Content-Type parameter to make it pass. Support
  expansion is separate reviewed work, not recovery.
- **Ordering, header, labels or thread mismatch:** retain original and any created
  replacement. Inspect exact audit; do not force a success by rewriting headers.
- **SDK/transport lock mismatch:** recreate the environment from requirements.lock.
  Retest any dependency update; SDK retry flags alone do not establish single send.
- **JSON write/directory fsync failure:** a complete old or new file may be present;
  uncertain durability is a stop. Inspect authoritative journal and SQL, not the
  report alone. Torn/unexpected journal temporaries force recovery review.
- **OAuth/TLS/proxy error:** follow GOOGLE_SETUP with separate authorization. Do not
  log tokens/raw API exceptions, disable certificate checking, broaden scopes, or
  replay an uncertain write. API access does not establish browser Drive sign-in.

Process-kill tests demonstrate process-crash behavior, not all power-loss/hardware
failures. Remote operations are non-atomic; no concurrent Drive writers and
independently retained original `.eml`/attachment backups beyond the review period
are operational requirements. Gmail's usual Trash expiry is not controlled here.
