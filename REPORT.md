# R1/R2 independent re-review remediation — 14 October 2026

**Local implementation submitted for independent re-review; no approval for live
Trash use.** Starting HEAD was exactly
`07cc713c3b76908b6d36232771ccca8709df5244`, branch `reviewer-remediation`, with empty
`git status --short`. Original baseline remains
`b21638b71f6f100abfeb1d3ad40bc041a340e8af`. No different starting revision or unrelated
user change was substituted. Existing ignored private files and the old Git bundle
were preserved. No Gmail/Drive/OAuth access, real-credential refresh, connected-account
use, live external API operation, push, merge, publication or live migration occurred.

The optional reviewer REVIEW.md, independent_regressions.py and log under
`/workspace/outputs/gmail-rereview/` were absent. The uploaded findings supplied
complete reproduction instructions; their absence was not a blocker.

## Before/after evidence and implemented fixes

### R1 — original interpreted HTML could change while verification passed

Independent full-lifecycle reproduction used the exact reviewer input with real
U+2028 and LF characters: `<html><body>\u2028xxxx\nAA&amp;BB</body></html>`, standard
MIME serialization, an explicit fake selection, temporary Store and FakeGmail.
On starting HEAD, create returned `verified`, fake original `abc123` was Trashed,
and the independent tinyhtml5 oracle found original displayed `AA&BB` changed to
literal `AA&amp;BB`. The note was present; source-span preservation alone had not
protected interpretation. This new regression failed for that exact reason.

Root cause: HTMLParser line coordinates count LF only; str.splitlines also counts
Unicode/control separators. The closing-body offset pointed between `&` and `amp;`.
The old note-subtree check verified the added note without comparing original
interpreted content.

Fix in `gmail_cleanup/html_profile.py`:

- Precompute LF-only character offsets, matching HTMLParser.getpos exactly.
- Require the calculated position to identify the actual ASCII closing-body token;
  reject invalid coordinates or ambiguous context before reconstruction proceeds.
- Retain the conservative HTML profile and original source outside the insertion span.
- Parse original and composed HTML; validate the exact appended hr/div note nodes,
  including their content, attributes, links and internal tails.
- Remove only those nodes and the one parsed LF on each side corresponding to their
  added CRLF wrappers. Reconnect **all remaining tail text** and compare the full
  original tree, including comments, attributes, text, children and tails. None/empty
  text both mean zero characters; meaningful whitespace is never stripped.

After: the exact lifecycle case preserves displayed `AA&BB`, passes genuine
verification, produces the correct visible note/hash and decoded Drive href, and
only then issues the one fake Trash. An injected entity-splitting offset now raises
Unsupported even when the note parses as a valid child. Injected changes to original
text and comment/element tails are rejected.

Expanded tests cover U+2028/U+2029, form feed/lone CR, LF/CRLF/mixed endings, separators
inside comments, named/numeric references at closing-body boundaries, full documents,
fragments, text around the note, wrong-coordinate injection, independent HTML5 trees,
exact decoded links and visible note content. U+0085 and invalid C0 separators retain
**existing** HTML5 invalid-codepoint rejection before allocation/upload/insert; the
coordinate fix does not conceal the bug by rejecting supported ordinary Unicode.
Existing malformed/style/raw-text and independent PDF-renderer tests remain intact.

New plans use safety/transformation policy **3** (`engine.py`); policy-2 and older
plans/approvals are immutable, inspectable and historical-only. No automatic migration,
silent replanning or new legacy Trash eligibility. The visible note format is unchanged.

### R2 — valid older journal could erase newer surviving SQL mutation evidence

Independent reproduction retained the planned journal prefix, accepted one fake
insert whose response was lost, and rolled back **only** the journal while leaving
SQL insert_pending and intent intact. Starting HEAD detected the mismatch, but
restore_from_journal reset SQL to planned and cleared it; retry created replacement2.
The regression failed because **two** fake insertions occurred.

Root cause: recovery trusted internal journal validity without proving that it
contained SQL's acknowledged history. It selected journal maps and replaced SQL
without validating surviving table/event evidence or direction.

Fix in `gmail_cleanup/storage.py` and strict journal reads in `recovery.py`:

- Validate SQL's sole acknowledgment record: store/account identity, typed sequence
  and canonical hash. It must identify the exact compatible journal prefix.
- Reject a journal older than acknowledged SQL, equal/longer divergent ancestry,
  another store for the same mailbox, account mismatch and unreadable/malformed data.
- Require SQL operation/backup/intent maps to equal that acknowledged prefix; every
  surviving operation/backup event must match in order. Logs can be a subsequence
  after earlier compatible replay; no surviving event is ignored.
- Validate complete candidate snapshot shapes/digests/bindings, including the
  unacknowledged suffix. Reject duplicate JSON keys and non-JSON numeric constants.
- Require the full extension to retain known original/replacement/Drive identities,
  immutable plans/approvals, intent bindings and observed results. A valid hash
  extension cannot retract those facts.
- Hold the lifetime store lock and a SQL transaction through compatibility validation
  and replay. No authoritative SQL replacement or recovery append occurs before proof.
  Rejection rolls back, retains both sources byte-for-byte and remains recovery-only.
  Normal execution also checks surviving SQL evidence; reopening cannot bypass refusal.

After: exact journal-only rollback is rejected, original SQL insert_pending/intent
and journal bytes remain unchanged, and retry still has **one** fake insertion.
Valid SQL-behind recovery remains supported, including durable journal publication
before SQL commit. Repeated compatible recovery retains pending uncertainty, IDs,
approvals and immutable plan bytes; no new marker or blind insertion is created.

New tests also reproduce/test SQL-ahead known replacement, uncertain Drive allocation/
upload, pending/completed Trash, SQL-only intents/backups/events, malformed metadata,
wrong store/account, incompatible hash extensions and retracted identities. Rejected
cases preserve both evidence sources; positive cases reuse exact known IDs and
approvals without additional allocation/upload/Trash. A new SIGKILL test interrupts
local recovery after its durable derived journal append but before SQL commit; a
subsequent compatible recovery retains the uncertainty barrier.

The reviewer independently reproduced Gmail duplication, not Drive/Trash duplication.
Our added variants establish evidence-preservation/no-replay under the specified
fake failures; they do **not** claim that every prior variant would actually have
produced a second remote mutation. Other original-label/identity gates could also
block a repeated Trash. Whole-history rollback remains a separate limitation.

## Local test commands and results

Working directory: `/workspace/Gmail-cleanup`. Reused isolated
`/tmp/gmail-review-locked`, Python **3.12.14**, exact existing 42-package
`requirements.lock`; no dependency or scope changes. Critical transport remains
SDK 2.201.0, google-auth 2.61.0, google-auth-httplib2 0.4.4, httplib2 0.32.0.
All test commands used the external-network guard and fake credentials; actual SDK
fault/TLS/proxy tests used loopback servers only.

```sh
# On the exact starting code, before production edits:
PYTHONPATH=tests/network_guard:. /tmp/gmail-review-locked/bin/python -m unittest tests.test_rereview_regressions -v
# 2 tests, 2 expected reproduced failures: HTML text corruption and second insert.

# R1 targeted gate:
PYTHONPATH=tests/network_guard:. /tmp/gmail-review-locked/bin/python -m unittest tests.test_rereview_regressions.IndependentRereviewTests.test_R1_exact_unicode_entity_lifecycle_preserves_interpreted_original tests.test_html_preservation tests.test_mime_policy tests.test_mime tests.test_server_headers tests.test_engine -v
# 60 passed at that stage.

# R2 expanded gate with prior Drive/Trash/crash coverage:
PYTHONPATH=tests/network_guard:. /tmp/gmail-review-locked/bin/python -m unittest tests.test_recovery_direction tests.test_rereview_regressions tests.test_recovery_journal tests.test_drive_preflight tests.test_trash_workflow tests.test_process_interruptions -v
# 102 passed before final supplemental cases.

# Expanded independent cases and final suffix/crash evidence:
PYTHONPATH=tests/network_guard:. /tmp/gmail-review-locked/bin/python -m unittest tests.test_recovery_direction tests.test_rereview_regressions tests.test_html_preservation -v
# 27 passed before the final malformed-suffix case.
PYTHONPATH=tests/network_guard:. /tmp/gmail-review-locked/bin/python -m unittest tests.test_recovery_direction tests.test_recovery_journal tests.test_rereview_regressions tests.test_process_interruptions -v
# 35 passed including malformed suffix and local recovery SIGKILL.

/tmp/gmail-review-locked/bin/python tests/run_offline.py
/tmp/gmail-review-locked/bin/python -m pip check
git diff --check
```

Final full gate: **256 tests passed, zero failures/errors/skips, in 107.854 seconds**. Pip check found no broken requirements;
git diff --check passed. All prior 228 tests remain unchanged; 28 new tests cover
R1/R2. Existing 29 SIGKILL boundaries plus the new local-recovery boundary give **30**
distinct process-kill boundaries. No tests were removed, skipped or weakened.

Failures/unavailable checks: the initial two regression failures were intentional
before-fix evidence and now pass. A broad optional AGENTS search encountered an
unrelated restricted /tmp directory; repository/ancestor inspection completed and
no required test was blocked. The optional reviewer artifacts were absent. Pip
reported an unwritable cache warning, without affecting installation/check results.
No test timeout or socket restriction occurred. Hosted CI and new live-account
validation were not run; neither is authorized by this task.

## Local commits and documentation

- `c0eb2cb4eb35398ca0c7aa50d13d539ce2dc5f0a`: faithful independent R1/R2 regressions.
  This deliberately red intermediate commit records the defects, not readiness.
- `052a59ceea219609ca2d8fae6259dae81e484264`: R1 coordinates/parsed-content preservation,
  policy 3 and expanded HTML regressions.
- `0157a4f26b7a41d66002febaadd9b8f6b3ae418a`: R2 compatible recovery direction, strict
  complete candidate evidence and expanded recovery/crash regressions.
- Final documentation commit: HEAD containing this report; inspect
  `git log 07cc713..HEAD` for its SHA without a self-referential report hash.

README, MAINTAINER, RECOVERY and GOOGLE_SETUP now explain exact parsed-content
preservation, compatible recovery direction, evidence-preserving refusal and policy-2
inspection-only behavior. No CI change was needed: its existing locked full discovery
gate automatically includes the new tests and inherited safety matrix. No push or
publication of any new commit. Working tree is clean after the final local commit.
Only code/tests/necessary docs were committed; existing private artifacts, generated
state, credentials, virtualenvs and the old bundle remain outside Git.

## Historical evidence and operational limits

The prior 228-test gate at 07cc713 passed but did not cover these two defects. Its
passing result was not evidence that R1/R2 were fixed. The first-remediation report
and F1–F8 evidence are preserved at `git show 07cc713:REPORT.md`.

The earlier five-message disposable-account test and user-reported Gmail UI rendering
predate both re-reviews. They were **not rerun** and do not validate policy 3. No
current live-use or reviewer approval is claimed. Historical sent/large/custom-label/
other-thread evidence remains limited; a later pilot needs separate authorization.

Residual requirements/risks remain: independently retained original .eml/attachment
backups and current journal outside the SQL/environment restore unit; one operator,
trusted private storage honoring fsync/link/rename, no concurrent Drive writers,
separate manual Trash approval and stop on first uncertainty. Remote changes after
verification and external proxy/server replay are possible. No atomic cross-service
or global exactly-once guarantee. Losing/rolling back every history copy is locally
undetectable; it cannot justify discarding newer surviving SQL evidence. SIGKILL is
not proof against every power-loss/hardware/dishonest-storage failure. Conservative
MIME/HTML exclusions remain; parser/PDF tests do not establish actual Gmail UI
behavior. Trash expiry and long-term Drive retention/access are external.

Production scopes remain exactly gmail.modify + drive.file; API runtime hosts remain
oauth2.googleapis.com, gmail.googleapis.com and www.googleapis.com. No broader scopes,
sharing change, permanent removal, thread-wide destructive action or live migration.
