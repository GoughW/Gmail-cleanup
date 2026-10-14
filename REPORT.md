# Reviewer-remediation completion report — 14 October 2026

**Submitted for independent re-review, not approved for live use.** Reviewed baseline
was exactly `b21638b71f6f100abfeb1d3ad40bc041a340e8af`; HEAD and clean working tree were
confirmed before creating local branch `reviewer-remediation`. No alternate starting
revision, unrelated user change, push, publication, account access, OAuth consent,
real-token refresh, live migration or remote mutation occurred. Public package
retrieval was used for local development; API tests use fake credentials and
loopback servers only. The historical five-message live test below is separate
from the new policy-2 local evidence.

## Focused local implementation commits

- `2271f3d2c5623ebf7542e924571785c889693d7a` — exact SDK lock and single-attempt transport.
- `4531281edb4bef2d83f73ac44ed00d2a84152345` — durable independent history, paths, intents and interruption drills.
- `e20b4d9cecba826a5931b99aff12a49365a1ec23` — conservative MIME/HTML and coherent Drive proof.
- `493819e620b2fbc480096f8e96d53a1a5be1f7b7` — final path/recovery hardening and independent baseline/legacy/crash regressions.

The final documentation/CI/offline-gate commit is the HEAD containing this report;
use `git log b21638b..HEAD` for its identity without a self-referential report hash.
All commits are local; no push/publication. Tracked working tree is clean after the
final commit. Existing ignored `.private/`, virtualenv/build metadata and caches
remain untouched; no committable private/untracked artifact was introduced. A
changed-file secret-pattern scan found no matches, and decoded legacy test inputs
were confirmed to be fictional `.invalid` messages. That scan is a guard, not a
proof that arbitrary secret formats are impossible.

## Finding-by-finding implementation and reproduced evidence

The archived baseline was executed with fake services, temporary files and the
same deterministic synthetic messages used by independent regression tests.

| Finding | Reproduced baseline defect | Revised behavior and evidence | Implementation / tests |
| --- | --- | --- | --- |
| F1 unsupported body semantics | `format=flowed; delsp=yes` lost semantics but reached fake Trash: **1 request**. | Only supported charset body parameters; flowed/delsp, unknown, empty, malformed and duplicate parameters rejected **before allocation/upload/insert**, zero mutations. Legacy charsets and Content-Language retained, original correspondence independently compared. | `mime.py:178`; `test_mime_policy.py`, `test_mime.py:188`. |
| F2 Drive content stability | Content changed immediately after the second download; fake Trash **1**, detected only by postcheck (`trashed_verify_failed`). | At most **two complete version-bracketed downloads**; accepted bracket must be unchanged valid positive version and exact downloaded hash/size. Final churn/missing version/contradictory populated SHA-256 blocks. Same late-change regression now **zero Trash**. Actual SDK two-chunk media corruption rejected independently. | `drive.py:178`; `test_drive_stability.py`, `test_drive_integrity.py`. |
| F3 referenced resources | Selected Content-Location attachment removed: **1**. | All selected CID/Location resources protected, even apparently unreferenced/attachment-disposition. Encoded/entity/CSS-escaped references cannot bypass protection; unselected resource bytes independently retained. | `mime.py:314`; `test_mime_policy.py`, `test_drive_preflight.py`. |
| F4 path aliasing | CLI report path equaled SQLite path: exit **0**, database overwritten by JSON. | Central pre-auth input/state/sidecar/journal/output canonical/inode policy, output/output collisions, private singly linked targets, pinned descriptors and race rechecks. Same CLI alias exits **2**, auth not called, input bytes unchanged. Linked SQLite sidecars rejected before connection. | `paths.py:83,168`, `storage.py:70,156`; `test_artifact_safety.py`. |
| F5 stale/restored state | Restore planned SQL after accepted insert/lost response: **2 inserts**. | Independent write-once history, account/identity/plan/action binding, durable intent before mutations, lifetime flock. SQL-only restore blocks; explicit local journal recovery preserves pending state/known IDs/approval; reconciliation reuses existing replacement: **1 insert total**, no blind retry. Missing/conflicting/torn evidence blocks. Unsafe historical flowed fixture remains inspection-only. | `recovery.py`, `storage.py:174,249,286`, `engine.py:165,218,259,291`; journal/process tests. |
| F6 HTML note insertion | Unfinished HTML comment swallowed appended note: **0 parsed anchors**, yet **1 Trash**. | Bounded passive balanced HTML eligibility; unsupported contexts fail before mutations. Exact source-preserving span inside body / safe fragment; composed standards-parsed note/anchors checked. Independent parser plus offline PDF renderer verifies visible filename/hash and exact URL. | `html_profile.py`, `mime.py:320`; `test_mime_policy.py`, `test_mime.py`, `test_drive_links.py`. |
| F7 transport replay | Actual Google SDK call with `num_retries=0`, httplib2 `RETRIES=1`, accepted POST then missing response: **2 identical POSTs** on both **0.22.0** and baseline-installed **0.32.0**. | Locked stack and isolated single-attempt adapter. Actual SDK fault servers observe **one submission** for missing/bad/partial response, reset/timeout, auth/rate/server errors/redirects. Engine keeps ambiguous intent/original; no implicit second insert or Trash. TLS trust and loopback HTTP CONNECT proxy preserved. | `requirements.lock`, `transport.py`, `api.py:104`; `test_transport_faults.py`. |
| F8 artifact durability | Serialization failure destroyed old report (22 partial bytes); injected write failure left **0 bytes**. | Serialize first, same-directory private temp, full write/flush/fsync, atomic replace + directory fsync; true exclusive publication. Old/new complete artifact only on tested failures/SIGKILL; concurrent creator cannot clobber winner. Directory-fsync failure reports uncertain durability. Journal torn records block; SQL FULL + supported backup drills. | `storage.py:23`, `paths.py:13`, `recovery.py`; artifact/journal/process tests. |

File paths in that table refer to `gmail_cleanup/` and `tests/` respectively. The
selected approaches were implemented rather than the earlier preliminary fixes.
No revisions/keepForever, broad scopes, duplicate cleanup, general HTML repair or
live state migration was introduced. No library/API contradiction required relaxing
a gate. We chose the baseline-installed 0.32.0 as the exact reviewed transport and
also independently reproduced the reviewer's 0.22.0 failure.

## Exact dependency and transport evidence

Python **3.12.14** on Linux; declared support narrowed from 3.11+ to **3.12 only**
(`>=3.12,<3.13`) because other versions/platforms did not receive this gate.
`requirements.lock` pins the entire 42-package SDK/auth/HTTP/build/renderer dependency
set, not just top-level ranges. Key versions:

- google-api-python-client **2.201.0**
- google-auth **2.61.0**, google-auth-httplib2 **0.4.4**
- httplib2 **0.32.0**, google-auth-oauthlib **1.5.0**, PySocks **1.7.1**
- html5lib **1.1**; independent renderer WeasyPrint **66.0**, tinyhtml5 **2.1.0**,
  PDF assertions pypdf **6.1.1**
- build setuptools **78.1.0**, wheel **0.45.1**

The adapter checks the critical four SDK/transport versions at runtime. It bypasses
upstream connection-replay, challenge and redirect response machinery without a
production global retry monkeypatch. Credentials refresh before first submission;
post-submission 401 does not refresh/replay. Each connection is closed. Upstream
connection creation, proxy routing, trusted CA and certificate validation remain.
Untrusted local TLS reached zero POSTs; explicitly trusted local TLS reached one.
HTTP CONNECT proxy test used a loopback proxy and a fictional target, never an
external forwarded connection. Drive multipart upload and exact-message Trash
also reached one POST when their responses were lost.

This establishes tested **client submission** behavior, not a guarantee against
external proxy/server replay. The lock is version-complete, not a package-hash or
OS-library supply-chain lock; CI/system Pango/font packages are separately installed.
Any dependency update requires a new lock and the complete transport gate.

## Test commands and results

All original 177 tests remain in the suite, with stricter expectations described
below. The final expanded gate is **228 tests; no failures, errors or skips**.

Fresh locked environment setup executed:

```sh
python3 -m venv /tmp/gmail-review-locked
/tmp/gmail-review-locked/bin/python -m pip install --no-deps -r requirements.lock
/tmp/gmail-review-locked/bin/python -m pip install --no-deps --no-build-isolation -e .
/tmp/gmail-review-locked/bin/python -m pip check
/tmp/gmail-review-locked/bin/python tests/run_offline.py
```

Installations succeeded; pip check found no broken requirements. The gate validates
every lock entry and Python version, strips credential bindings, blocks external
Python DNS/connections (including spawned children), and rejects skipped tests.
CI installs the exact same stack/system renderer libraries and runs this gate with
fake bindings. **Hosted CI was not executed**; no commits were pushed/published.

Earlier successful local commands included:

```sh
.venv/bin/python -m unittest discover -v
.venv/bin/python -m unittest tests.test_process_interruptions -v
.venv/bin/python -m unittest tests.test_recovery_journal tests.test_artifact_safety -q
/tmp/gmail-review-locked/bin/python -m unittest tests.test_artifact_safety tests.test_api_cli tests.test_drive_preflight -q
```

Results: baseline full suite **177 passed**; direct process-interruption gate **3
aggregate tests passed across 27 SIGKILL lifecycle boundaries**; artifact/recovery suite **26 passed**; final focused CLI/path/Drive suite **81 passed**. Artifact tests additionally SIGKILL before/
after JSON replacement, making **29 distinct kill boundaries** overall. Fresh
full-gate intermediate runs passed 218 and 225 tests before final additions.
Independent local fixture-generation/CLI plan/dry-run walkthrough also passed with
zero remote services. `git diff --check` passed.

Baseline evidence commands (archive source, no live services):

```sh
mkdir -p /tmp/gmail-reviewed-baseline
# Executed as a pipeline into that separate archive, not the working tree:
git archive b21638b71f6f100abfeb1d3ad40bc041a340e8af | tar -x -C /tmp/gmail-reviewed-baseline
cd /tmp/gmail-reviewed-baseline
env -u GMAIL_CLIENT_ID -u GMAIL_CLIENT_SECRET -u GMAIL_REFRESH_TOKEN -u GMAIL_OAUTH_SCOPE \
  PYTHONPATH=/tmp/gmail-reviewed-baseline \
  /workspace/Gmail-cleanup/.venv/bin/python \
  /workspace/Gmail-cleanup/tests/baseline_reproductions.py
```

The script reproduced the F1–F8 counts in the table. Repeated with public httplib2
0.22.0 wheel extracted under `/tmp/gmail-transport-old/package` and prepended to
PYTHONPATH, it again observed **two identical POSTs**, zero SDK retries and RETRIES=1;
its injected artifact write failure again left zero bytes. The deliberately unsafe
baseline script is evidence only and is not run by the acceptance gate.

Unsuccessful checks are not counted as passes: initial headless Chromium attempts
timed out with host sandbox/read-only-cache/DBus errors; we use the independent
WeasyPrint/PDF renderer instead (the selected plan permits an offline renderer).
An early new runner lacked a `__main__` guard and spawned workers re-entered test
discovery; that invocation was invalid, stopped and fixed before valid full runs.
A privacy-check assertion initially looked for plain text in the base64 synthetic
fixture; it failed and the shell continued to a local commit. A corrected decoded
fixture check and changed-file secret scan passed; no private artifact was staged.
Two newly added regression assertions initially mismatched the existing API
contract (approval includes action; a known legacy replacement returns inspection
rather than raises). Their expectations were corrected to the stronger intended
invariants and rerun successfully. No tests were removed, skipped, or weakened to
obtain a passing result.

### Explained changes to original tests

- Drive continuous version-change success now expects refusal; missing version and
  contradictory SHA-256 now block. Settling-version tests inspect discarded first
  attempt and accepted stable second attempt. Same-size corruption can fail on
  contradictory checksum immediately rather than await a second corrupt download.
- Benign canonical-URL change with **unchanged version** needs one complete bracket,
  so the old unconditional two-download expectation was changed while exact link
  and content assertions remain.
- Apparently unreferenced attachment CID removal now expects rejection. A second
  independently eligible attachment is removed to prove CID/image preservation.
- Complete HTML's old whole-source-prefix assertion now checks exact source before/
  after its insertion span, because safe insertion occurs before closing body.
  Independent span removal additionally reconstructs the full original source.
- Tests explicitly initialize new fake-mailbox SQL/journal and supply mailbox keys;
  restarts never auto-reset. Fake APIs expose only a fixed fake account identity.
  Upload failure test now requires explicit same-ID recovery before retry.
- Linked private-output errors now expect a policy CleanupError. Existing unsafe
  local state fixtures remain rejected; boundaries/identity/content checks remain.
- Trash-state test setup now records the necessary durable insert intent before
  calling current-policy verification, rather than inventing authority in SQL.

## Compatibility and recovery

New plans require safety/transformation policy **2**. Saved plans and approvals
remain immutable; old reconstruction is not regenerated for a possibly existing
replacement. Old records remain inspectable/historically reconcilable, with derived
`recovery_only` and `current_policy_eligible=False`; the SQL approval/plan is not
rewritten. A baseline-generated **synthetic** flowed-text legacy fixture demonstrates
historical byte verification really passes without granting current eligibility.
No automatic legacy migration/replanning tool is provided. An unexecuted old plan
requires independent explicit replanning/review; do not erase history to bypass it.

Retained independent journal evidence detects stale SQL after insert/upload/Trash.
`recover-state` is local only, restores known IDs/pending intents/approval, and does
not authenticate. Normal retries cannot repeat an ambiguous insert or Trash.
Single reviewed candidates can be reconciled; zero/multiple/conflicting/delayed
candidates cannot prove absence or clear intent. Known-ID upload recovery requires
explicit permission and durable recovery intent; unknown allocation blocks.
Missing or malformed evidence remains a stop. See [RECOVERY.md](docs/RECOVERY.md).

Path protection and durable artifacts are coordinated with the journal and SQL:
reports cannot alias execution authority or private inputs, every required intent
is durably published before submission, and the store holds a lifecycle lock.
Local state/history copied together from an old snapshot **cannot detect rollback
of every copy**. This is not an external anti-rollback oracle or global exactly-once
transaction. Whole-environment loss/rollback needs manual independent evidence.

## Historical live evidence — not rerun during remediation

Before this reviewed baseline/remediation, five allowlisted dummy messages in one
received-style thread completed backup/reuse, downloaded hash verification,
reconstruction, insert, preservation checks, original Trash and postcheck. Identical
four-byte attachments reused one private binary backup. Ordering deltas were zero;
duplicate Message-ID/thread membership and labels were preserved; Gmail added a
Received trace. DKIM/ARC assertions were removed/audited. The user reported correct
Gmail UI sender/recipient/subject/date/thread and note/link/hash rendering.

The reported viewer-link issue was resolved by the user. Earlier read-only checks
found valid hrefs/punctuation and confirmed private owner access; canonical
webViewLink propagation was implemented at baseline. No regeneration was required.
Those checks were historical, **not performed in this task**. No real IDs, tokens,
links, raw correspondence or old local live audits were added to this report.
This limited live evidence does not establish current policy-2 or sent/custom-label/
large-message/other-thread import correctness.

## Remaining validation, operational requirements and risks

- Independent re-review against the supplied definition of done remains required.
  Policy-2 live validation and hosted CI are unavailable/not authorized in this task.
- A later separately authorized 5–10-message disposable pilot must have independently
  verified original `.eml` and attachment backups, confirmed Gmail/Drive identities,
  minimal scopes, one operator, authoritative current SQL + independently retained
  journal, no concurrent Drive writers and separate manual Trash approval. Stop on
  first failure/uncertainty; retain original backups beyond the review period.
  Real-mailbox work requires additional explicit authorization after that review.
- Drive proof is coherent during a bounded bracket; it does not freeze the file.
  External modification after the last proof remains possible. Gmail/Drive have no
  atomic cross-service transaction. No duplicate cleanup or rollback automation.
- Local flock protects one store only, not alternate stores/hosts. Trusted private
  directories and disciplined backup/restore are required. Descriptor checks do not
  defend against a privileged hostile operator/storage implementation.
- SIGKILL is process-crash evidence, not all power loss. Storage must honor fsync;
  hardware loss/dishonest storage needs independent backups. Journal histories are
  local private files, not cryptographically signed external evidence.
- Every selected CID/Location, flowed/unknown-parameter body and unsupported HTML
  fails closed. This reduces coverage of ordinary Gmail messages intentionally.
  New transformations require explicit policy review/reapproval. PDF/parser tests
  do not establish actual Gmail UI behavior; a later pilot must inspect it.
- Date/internalDate divergence blocks preservation eligibility; duplicate Message-ID,
  sent labels and threading may behave differently for other real inputs. Do not
  silently change headers to force success.
- Narrow transport exceptions retain semantic verification, but reconstructed mail
  cannot preserve original delivery authenticity. External replay by proxies/servers
  is outside tested client control.
- Drive app-scoped visibility and native-document exclusion remain. Long-term
  retention/access monitoring is outside scope; Gmail normally expires Trash after
  30 days. OAuth app/token policy is external. Large MIME is held in memory; 4 MiB
  local coverage is not proof of every API size limit. Notes can exceed tiny savings.

Required production scopes remain exactly gmail.modify + drive.file, and API hosts
remain oauth2.googleapis.com, gmail.googleapis.com, www.googleapis.com. No broader
scope, sharing change, alternate destructive path or account action was introduced.
