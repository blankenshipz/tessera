# ADR 0135: A person issues a narrower child agent credential

Status: Proposed - needs security review. Design only: no code, schema or route in this change
alters authority.

## Context

An enrolled person can run their own agent (for example a review agent that reads findings) only
by borrowing a credential another process issued, or by using their own. The README lists "a
person's own agent path" as open (ADR 0084 open question 8). Authority today has these relevant
pieces:

- Credentials: only the local root owner issues them, with an expiry within 24 hours
  (`Store.issue_credential`). An enrolled person's root credential lasts at most 7 days from issue
  or the owner's last renewal (README, Campaigns section; `MAX_PERSON_CREDENTIAL_LIFETIME`). A
  worker credential is issued for at most 24 hours and renewed only up to seven days after issue
  (README, Opt-in Salad GPU execution milestone section; `Store.renew_worker_credential`). Each use
  walks the parent chain and refuses the credential if any ancestor is revoked or expired
  (`Store._credential_principal`).
- Agent authority grants: a human issues them, a subject may `delegate` a strict subset, a grant
  expires within 24 hours and cannot outlive its subject credential, and each use rechecks the
  lineage and the subject's live credential (`agent_authority.py`: `_recipient_bounds`,
  `delegate_grant`, `require_active_agent_lineage`).
- Standing approvals (ADR 0042) and owner-signed approval delegations (ADR 0122).

None lets a person mint a credential for their own agent. The platform's rule that
decision-making routes are human-only must not move.

## Decision (proposed)

A person may issue a **child agent credential** from their own credential. The child is a
separate principal of class `agent`, parented to the issuer, and is never the issuer's identity.

**Who may issue.** Only an interactive human session credential (not an agent, worker or
another child). A child cannot issue a child: depth is one. The issuer must be live and enrolled
in the project, and issuance is refused while the issuer is suspended or its credential is
past expiry.

**Binding and rotation of the issuer.** The child binds to the exact issuing credential (its
`parent_id`), not to the person record, so the existing per-use chain walk applies to it.
Revoking or expiring that credential makes every child fail at its next use, with no separate
cascade write needed for correctness (a revocation event per child is still recorded). An owner
renewal of the person's root keeps the same credential id (`PeopleStore.renew`) and neither
extends nor revokes a child: the child's expiry is fixed at issue. Any future re-issue that
replaces the root credential leaves old children bound to the old credential, which is refused
once revoked. Binding to the person record was rejected because it would let a child outlive a
revocation made because the root credential was compromised; binding to the credential fails
closed.

**Scope narrowing only.** The child's scopes, projects, destinations and data classes are a
subset of the issuer's at issue time; a request naming anything the issuer lacks is refused, not
trimmed. The child carries only scopes from a closed issuable set that excludes every
human-only action (approvals, grants, provider trust, standing approvals, credential issue and
revoke, retention apply, spend-cap changes) and every scope that can reserve or cause spend. The
first set is `review:findings` read and the read scopes it needs. The child's effective
authority is evaluated at each use as the intersection of its stored scopes and the issuer's
current scopes, so removing a scope or suspending the issuer narrows the child at once.

**No grants for a child.** A child may not be the subject of any agent authority grant
(execution or reasoning-only, ADR 0155) and so cannot delegate one onward. The grant issue and
delegate paths refuse a child subject, and the lineage check refuses any use of a grant whose
subject credential is a child. This is the stricter of the two options (the other was
intersecting each grant with the issuer's current scopes at use time). It was chosen because a
grant can come from any authorised human, not only the issuer, so it could make the child wider
than its issuer, and onward delegation would let an agent pass authority on, which partly
reopens the rejected "agent issues children" path. It blocks no use case this ADR states: reading
findings needs no grant. Should a later amendment allow grants, a grant held by a child must
expire no later than the child and must be revoked when the child is revoked. The existing grant
checks already require both: a grant cannot outlive its subject credential, and each use
requires that credential to be live.

**Destinations.** A child can never name or reach, through control, an unregistered destination
or the public internet, in any mode, ADR 0047 `all_registered_providers` included. This restates
for children the README guarantee (Campaigns section): "data never goes to the public internet
or an unregistered destination in any mode, because non-public inputs cannot use egress or
workload fetch hosts and every transfer is recorded on the receipt", which ADR 0047 records as a
"Hard invariant, not configurable". The first issuable set names no destination at all. Tessera
cannot control a process that runs outside it; the invariant covers what the child names or
causes through control.

**Expiry and revocation.** A child is issued once with an expiry no later than the earlier of
issue plus 24 hours and the issuer credential's current expiry, the same 24-hour issue bound as
other owner-issued credentials. A child is never renewable: unlike a worker credential, it gets
no renewal up to seven days after issue, and a person issues a new child instead. So a child's
lifetime is at most 24 hours. A person root's lifetime is up to 7 days, and a worker's is 24
hours per issue or renewal, up to 7 days in total. The issuer, any project owner and an
administrator can revoke a child; revocation is immediate, fenced by revision and epoch like
authority grants, and idempotent.

**Spend.** In this ADR a child has no spend path. Its spend reservation is zero, it has no
sub-cap, and it draws on nothing of the issuer's. The issuable set holds no scope that can
reserve spend, and the child cannot hold a grant, which is the only budget an agent's model spend
is debited against (ADR 0142). A person has no personal spend cap to draw on: model spend is
limited per project and model destination (ADR 0090), and grants carry their own budgets. Adding
a spend-capable scope needs an amendment that defines it as a fixed sub-cap recorded at issue.
That sub-cap is lowerable only by the issuer, is never raised by the child, a renewal or a
later request, and is charged in addition to (never instead of) the project and destination
limits. Spend is attributed to the child and its issuer. Whatever the cap, the check of every
applicable cap, the reservation write, its event and the idempotency record happen in one
transaction, under the same tenant check at data access (AGENTS.md). The same applies to
issuance and revocation: the issuer and scope checks, the credential row, the event and the
idempotency record are written in one transaction, as `Store._mutate` already does for credential
issue.

**Receipts.** Issuance, each use that changes or reads governed state, refusal, expiry and
revocation each append an event naming the issuer, the child, the scopes, the expiry and the
request's idempotency key; no secret or token is recorded, only a credential digest. Campaign
and review receipts show the acting child and its issuer, so an agent action is never shown as a
person's.

**Fit with existing authority.**

- Agent authority grants: a child credential is identity and scope only, and a child holds no
  grant (above).
- Standing approvals (ADR 0042) and owner-signed approval delegations (ADR 0122): a child is
  never an approver, a delegation signer or user (it cannot hold `campaign:decide_delegated`),
  or a standing-approval holder. Decisions made by an agent stay recorded as automation, not as
  the issuing person's decision. (ADR 0044 is Salad review pre-approval and is not relevant
  here.)
- Human-only actions: the closed issuable set excludes them. A route that is human-only today
  stays human-only for a child, and a test enumerates the routes to keep that true.
- Separation of duties: a child shares its issuer's enrolment root (`review_jobs._enrolment_roots`
  walks the parent credential), so the `same_person` rule treats the child and its issuer as one
  person. A child does not reduce the three roots the default review flow needs.

## Alternatives rejected

- Letting an agent issue children: widens authority transitively and hides the person.
- A child that inherits all of the issuer's scopes: indistinguishable from sharing a credential.
- Renewable children: turns a short bound into a long-lived credential.
- Grants held by children, intersected with the issuer's scopes at use time: more surface for no
  stated use case (see "No grants for a child").
- Binding a child to the person record: survives revocation of a compromised root credential.

## Consequences (when implemented)

A schema addition for the parent link, issuance record and revocation, one issue route and one
revoke route (human only), a use-time intersection check in the credential path, and a refusal
of child subjects in the grant issue, delegate and lineage paths. Needs review of: the issuable
scope set, the use-time intersection, revocation cascade, and event contents.

These README lines must change when this is implemented:

- Campaigns section: "Only the root owner issues agent credentials, so agents and sessions the
  owner creates all share the owner's root".
- Campaigns section: "That person has their own root, cannot issue credentials, enrol people or
  act as owner".
- Campaigns section: "Only an agent holding `review:findings` can post findings, and only the
  owner issues agents".
- Campaigns section: "a person's own agent path does not exist yet, which is an open question in
  the ADR" (ADR 0084 open question 8). The path would exist, but it would not lower the
  three-root count that the same paragraph states. That count stays true.

Outside the README, docs/people-enrollment.md ("Cannot: issue credentials" and "Cannot yet own
agents under its own root") and ADR 0084 open question 8 change too.

## Not covered

Cross-organisation children, children of service credentials, hardware-bound credentials,
spend-capable child scopes, and any change to ADR 0042 standing approvals or ADR 0122
delegations.

## Review round 1 follow-ups (2026-11-23)

1. Grants held by a child: "No grants for a child".
2. Unregistered destinations and the public internet: "Destinations".
3. Wrong ADR 0044 citation: Context and "Fit with existing authority".
4. Exact child lifetime against worker and person-root limits: Context and "Expiry and
   revocation".
5. README lines to change: "Consequences (when implemented)".
6. Spend reservation and one-transaction writes: "Spend".
7. Credential binding and person-root rotation: "Binding and rotation of the issuer".
