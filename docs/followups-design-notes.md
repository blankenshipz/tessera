# Follow-up design notes

Status of this document: design only. The code these notes describe (loop
delegations, delegated decisions, delegated code upload, the placement
availability skips, and automatic backup retention) is not implemented yet.
Nothing here is implemented here. Every item below is marked "design only, code
deferred until that code is on main". Each note is written so it can be turned
into a change and tests once the code lands.

Suggested order across groups: A first (it changes what a user sees as the
reason for a refusal), then B (security and receipts), then C (performance and
contention, lowest risk).

## A. Placement ordering and unbounded transient waits

### A1. Permanent refusals must be evaluated before transient skips

Status: design only, code deferred until that code is on main.

- Problem: the placement check runs the provider availability and memory skips
  before the trust check. A stage whose input is restricted (for example
  non-commercial or restricted-license data) and whose first candidate is an
  unavailable provider gets a transient "credential expired" wait. It should
  get the permanent "input not admitted" refusal. The placement plan view shows
  the same misleading state. The data-destination rule still holds (the data is
  never sent), but the user is told to wait for something that can never help.
- Invariant: a permanent refusal (trust, data destination, license) takes
  precedence over any transient skip, for the same candidate, in both the
  placement decision and the plan view.
- Smallest fix: reorder the per-candidate check so all permanent refusals run
  first and the transient availability and memory skips run only for a candidate
  that passed them. Share the ordering between the decision and the plan view
  so they cannot drift.
- Tests that must fail without it: a stage with restricted input and an
  unavailable first provider must report the permanent refusal, not a wait, in
  both the decision and the plan view (see A2).
- Ordering: do first in this group; A2 is its regression test; A3 builds on the
  corrected reason codes.

### A2. Invariant test combining an unavailable provider with restricted input

Status: design only, code deferred until that code is on main.

- Problem: no test combines an unavailable provider with restricted input, so
  the ordering defect in A1 was invisible to the suite.
- Invariant: as A1.
- Smallest fix: add a table-driven test over (provider available or not,
  input permitted or restricted). Assert that restricted input is always
  refused permanently, whatever the provider state, and that permitted input
  waits only when the provider is unavailable.
- Tests that must fail without it: the restricted and unavailable row fails
  before A1 is applied.
- Ordering: write together with A1, and confirm it fails first.

### A3. Bound the transient wait or alert the operator

Status: design only, code deferred until that code is on main.

- Problem: every new skip reason is transient, so a stage can wait forever (a
  credential that is never renewed, a memory limit that stays unknown). Nothing
  tells an operator that the wait is not ending.
- Invariant: a wait caused by a transient skip is either bounded or visible. It
  never silently becomes permanent, and it is never reported as progress.
- Smallest fix: give transient skips a stated maximum wait (configurable, with a
  conservative default). When it is exceeded, record an operator-visible
  finding (an event and a stuck-campaign finding naming the reason) instead of
  continuing to wait. Optionally end the stage with an honest "could not be
  placed" state. Do not change the reason to hide the cause.
- Tests that must fail without it: with a test clock, a stage waiting on an
  unavailable provider produces no finding before the bound and exactly one
  finding after it; the finding names the original skip reason.
- Ordering: after A1, so the reason shown in the alert is the right one.

Related small items from the same review (documentation and robustness, no
separate design): state in the operator docs that the memory-limit check is
global (setting a limit on one provider makes an unknown limit block heavy local
stages on all providers); have the plan view use the same clock as placement and
log the class of any exception it swallows; confirm that every consumer of skip
detail accepts both a string and a mapping.

## B. Delegation lifecycle (delegated decisions and delegated code upload)

### B1. Bind a delegation to the issuing credential and re-check project access

Status: design only, code deferred until that code is on main.

- Problem: a delegation is tied to the issuing person, not to the credential
  that issued it, and project access is not re-checked when it is used.
  Revoking the issuing session credential therefore does not end the
  delegation. Standing approvals already bind to the credential and re-check
  project access at use.
- Invariant: authority derived from a delegation can never outlast the
  credential that issued it, or the issuer's access to the project, at the
  moment of use.
- Smallest fix: store the issuing credential id on the delegation. At each use,
  in the same transaction as the state write, check that the credential is still
  live and that the issuer still has the required project access. Reuse the
  standing-approval check rather than writing a second one.
- Tests that must fail without it: (1) revoking the issuing credential ends use;
  (2) revoking the issuing person ends use (no test covers this today, and
  removing the owner-still-live check still passes the suite); (3) removing the
  issuer's project access ends use; (4) the same agent continuing an upload with
  a second credential is refused (no test covers this either; removing the
  subject-credential check or the uploader match alone still passes).
- Ordering: first in this group. B2 depends on the stored credential id.

A related robustness point: the creator-liveness check looks the issuer up
through a join that can return an arbitrary one of several credentials for one
person, so a valid delegation can be refused. Once the credential id is stored
(B1), look up that exact credential. This removes the flakiness.

### B2. Do not report an ended delegation as active

Status: design only, code deferred until that code is on main.

- Problem: a delegation can outlive its issuing session credential while the
  status response still says "active" with quota remaining. Use is refused, but
  the status misleads.
- Invariant: the reported status never claims more authority than use would
  grant.
- Smallest fix: either cap the delegation expiry at the issuer credential's
  expiry at creation, or compute an "issuer ended" status at read time from the
  stored credential (B1). The read-time status also covers early revocation,
  which a creation-time cap cannot, so prefer it, with the cap as an addition.
- Tests that must fail without it: after the issuing credential is revoked or
  expires, the status is not "active"; the expiry never exceeds the issuer
  credential's expiry.
- Ordering: after B1.

### B3. Store the stable reason on receipts and events

Status: design only, code deferred until that code is on main.

- Problem: the receipt stores the finding's suggested action as the reason,
  instead of the documented stable `outside_delegation:<code>`. The real reason
  is only in a details note that neither the receipt nor the event includes.
  Receipts disagree with the ADR and the stuck-campaign documentation.
- Invariant: receipt and event reasons are stable, documented codes. The action
  is data, not the reason.
- Smallest fix: write `outside_delegation:<code>` as the reason on both receipt
  and event, and keep the suggested action in the details. Do not change the
  documentation to match the defect unless the codes are deliberately dropped.
- Tests that must fail without it: a receipt test asserting the stored reason is
  exactly `outside_delegation:<code>` for each code, and that the event carries
  the same value.
- Ordering: independent of B1 and B2; do before any consumer reads the field.

### B4. Route permanent no-progress refusals to escalation

Status: design only, code deferred until that code is on main.

- Problem: a refused no-progress replacement (no alternative candidate,
  replacement cap reached, steering forbidden) is put in the refused set and
  retried, never escalated, although those reasons are already listed as
  escalation reasons. Retrying a permanent refusal wastes work and hides the
  problem from a person.
- Invariant: a refusal that retrying cannot change goes to a person, once.
  Retry is only for refusals that can change.
- Smallest fix: when a refusal reason is in the escalation set, take the
  escalation path and record the escalation; keep retry for the others.
- Tests that must fail without it: each of the three reasons ends in escalation
  and is not retried; a transient refusal is still retried.
- Ordering: must land before any runner schedules the action.

Smaller points from the same reviews: the decision client ignores the project
argument of its list call and uses its configured project (pass it through); a
test that loosened an exact latest-schema check to "at most" should be tightened
again once migrations settle; the policy that any human decider in the project
can revoke any delegation (it only reduces authority) and that delegation upload
reservations are not refunded on expiry (safe over-counting) should both be
written down in the ADR and the operator notes.

### B5. Delegated code upload: narrow before limits (related, not lifecycle)

Status: design only, code deferred until that code is on main.

- Problem: building the upload manifest scans the whole upload root before
  narrowing to the requested paths, so uploading a small sub-path of a large
  root is refused by whole-root size and file limits.
- Invariant: limits apply to what is uploaded, not to what is nearby.
- Smallest fix: narrow to the requested paths first, then count and enforce the
  limits on that set. Keep path-escape checks on the narrowed set.
- Tests that must fail without it: a small sub-path under an oversized root is
  accepted; an oversized requested set is still refused.
- Ordering: independent. Also update the ADR status once the schema number is
  settled.

## C. Credential metadata reads

### C1. A short-TTL cache must never hide a renewal or expiry

Status: design only, code deferred until that code is on main.

- Problem: every health call, admission and scaffold tick takes the credential
  directory lock to read metadata, which contends with the guardian's resolves.
  A short TTL cache would help, but a cache can serve a stale value.
- Invariant: a cached value never hides an expiry, and never hides a renewal for
  longer than a stated bound (state the bound in the ADR, for example a few
  seconds). Read failure is never turned into "healthy".
- Smallest fix: cache only successful reads, keyed by credential reference, with
  a short TTL. Fail closed to a fresh read when the cached value is within a
  safety margin of its expiry (margin at least the TTL), and on any cache miss,
  error or version change. Invalidate on every write path in this process.
  Separate from this, give the "could not read" reasons distinct codes (no
  directory configured, permission error, unreadable) so a misconfiguration is
  not shown as an unknown state.
- Tests that must fail without it: with a test clock, a value cached before
  expiry is not served after the safety margin begins; a renewal becomes visible
  within the stated bound; an error is not cached; distinct read failures give
  distinct codes.
- Ordering: do C2 first, since moving the read outside the lock may remove most
  of the contention and the cache may then be unnecessary.

### C2. The stuck scan should not read credential metadata under its write lock

Status: design only, code deferred until that code is on main.

- Problem: the stuck-campaign detector reads credential metadata (taking the
  directory lock) while holding the scan's immediate write transaction, once per
  campaign and profile. This holds the database write lock across file-lock
  waits.
- Invariant: no wait on an external lock or file I/O while holding the database
  write lock.
- Smallest fix: read the metadata for the needed profiles once, before the write
  transaction begins, and pass the values in. Check expiry against the value read
  just before the transaction, and accept that it may be a moment old.
- Tests that must fail without it: a test whose metadata read blocks proves that
  the scan has not yet opened its write transaction (another writer can proceed
  during the block).
- Ordering: before C1.

### C3. Share one profile helper

Status: design only, code deferred until that code is on main.

- Problem: status and the stuck detector derive the profile in two different
  ways, so they can drift apart.
- Invariant: one definition of "which profile does this candidate use".
- Smallest fix: one helper used by both. Test both paths against the same table
  of candidates.
- Ordering: independent; do with C2 since both touch the detector.

### C4. Migration test compares the whole kept row

Status: design only, code deferred until that code is on main.

- Problem: the schema-78 migration test checks only the id and code of the kept
  row, so a dropped column (evidence, acknowledgement, note) would pass. The old
  table shape in the test also leaves out the identity trigger.
- Invariant: a migration preserves the full content of kept rows and the table's
  triggers.
- Smallest fix: compare the whole row before and after, and build the old table
  with its trigger.
- Tests that must fail without it: a migration that drops a column fails.
- Ordering: independent.

### C5. Say plainly who can see credential details

Status: design only, code deferred until that code is on main.

- Problem: campaign status and stuck findings show credential reference,
  version and expiry to readers of the campaign, a wider audience than the
  credential health endpoint. This is accepted: the values are not secret and
  are already in events.
- Smallest fix: state it in the ADR, so it is a decision and not an oversight.
  No code change.

## Not covered

Automatic backup retention had no correctness, security or invariant defects.
Its follow-ups are small and do not need a design: check that the state
directory exists before creating anything under it, state in the ADR that the
report returns only a receipt file name, note that a dry-run estimate credits
only artifact deletions (it errs on the safe side), confirm the ADR number at
merge, and wire retention into the upgrade command later.
