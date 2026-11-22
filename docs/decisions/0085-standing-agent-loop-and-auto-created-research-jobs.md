# 0085 — A standing agent loop and auto-created research jobs

## Status

Proposed. Owner direction, 2026-11-22. This ADR adds library modules only; nothing is wired into
plan admission or a service unit yet, so existing installs are unchanged. It is meant to land after
the work it builds on:

- ADR 0082: Anthropic as an enrolled model destination and the credential-by-reference supervisor.
- ADR 0083: receipts for every model call.
- ADR 0084: client-requested research and review jobs, findings records and the `tessera_mcp`
  tools. The submission shape used below is that ADR's `SubmitReviewJob`.

These ADRs are written on other branches; the links above are plain numbers on purpose, and the
status stays Proposed until all of them are on the main branch and the cross-references are checked.
Until those land, the interfaces named here are the whole dependency, and tests stub them. The data-class
invariant of [ADR 0047](0047-agent-routed-data-destinations.md) is kept unchanged. When the loop is
wired in, DESIGN.md should record that a standing supervisor may start successor calls under
the rules below; the reasoning supervisor today starts one bounded run per explicit start.

## Context

Research and review work (plan review, dataset scouting, technique scouting, pre-gate review)
should run on the platform with receipts. A client can request such jobs (ADR 0084). The platform's
own agent loop should also create them when a plan is admitted or advanced, and keep advancing
admitted work without a person starting each model call.

A loop that does this by itself needs hard limits: it must not spend without bound, widen its
identity or model tier under pressure, repeat a paid call whose outcome is unknown, or touch private
data on a destination the operator has not registered.

## Decision

### Standing control-plane loop (`tessera_agent.standing_loop`)

1. **Successor calls under the same policy.** A standing supervisor may start a successor model
   call after the previous call finished, without a new human start, only under the project's
   `LoopPolicy`: a fixed model, tier, policy digest and enrolled credential reference. A successor
   is invoked with exactly those values. A work item that asks for another tier or identity is
   skipped (`identity_or_tier_mismatch`); the loop creates no new, unenrolled identity.
2. **Kill switch.** An operator marker file stops all new model calls. A missing, unreadable or
   ambiguous marker reads as engaged (fail closed). The switch touches nothing else: it does not
   cancel, interrupt or inspect in-flight calls, container campaigns, workers or control routes.
3. **Back off, never downshift.** A quota or rate-limit refusal is journaled and surfaced, and the
   loop waits with capped exponential backoff (honouring a longer `retry_after`). The same model and
   tier are used afterwards. Quota never changes the configured tier.
4. **Unknown outcomes are not retried.** The loop records a durable start in a call ledger before
   it calls the provider. The ledger must be durable: the loop refuses to be built over a volatile
   one (an explicit opt-in exists for tests only). A refusal known to precede acceptance (quota, or an explicit "not
   started") is retryable. Any other error, or a start record with no finish (for example a crash),
   is an *uncertain* outcome: it is never repeated automatically, because the provider may already
   have accepted a paid call. Uncertain items are surfaced, and only an operator resolution clears
   them. No new call starts while any ledger row is recorded as started, whether or not that item is
   still observed, which keeps calls one at a time. Only one loop may use a call ledger: the ledger
   holds an exclusive lock for its lifetime, so a `started` row seen by the lock holder can only be
   a crash leftover. The lock is advisory and for a local
   filesystem only: it is unreliable on network filesystems and does not cover a hard link or an
   alternate path to the same file, so the ledger lives on a local disk under one canonical path.
5. **Bounds.** A rolling hourly call cap, idempotency per work item id, and a consecutive-failure
   limit after which the loop halts until an explicit reset. The call window, the failure count,
   the halted state and the backoff deadline are kept in the durable ledger (written in one
   transaction), so a restart neither
   doubles the hourly allowance nor clears a halt. State changes (engaged, halted,
   backing off) are journaled once per change, not once per tick.
6. **Journal.** Events record ids, digests and reasons only: never prompts, tokens or credentials.
7. **Boot and API key.** The loop is off by default and runs as a manual-start unit that the operator
   admits explicitly, as for the reasoning unit; it is never enabled on boot. It refuses to start,
   and refuses each successor call, if `ANTHROPIC_API_KEY` is present in the environment. Credentials
   are only enrolled references.

### Auto-created research jobs (`tessera_agent.auto_research_jobs`)

8. **Trigger.** When a plan is admitted or advanced, the loop submits the default job kinds
   (`plan_review`, `dataset_scout`, `technique_scout`, `pre_gate_review`, the closed set of ADR 0084,
   narrowed by policy). It uses ADR 0084's interface: `submit(project_id, key, payload)` where
   `payload` is a `SubmitReviewJob` body (kind, title, digest-pinned inputs, a per-kind pinned
   prompt digest, destination, tier, budget, egress; a client never supplies an input's data class,
   control derives it, and the loop's own copy of it only decides `egress`) and the result is a job
   record with an `id`. A policy refusal is raised as `ReviewSubmitRefused(reason)` carrying one of
   ADR 0084's reason codes.
9. **Provenance.** Jobs are submitted by the loop's own enrolled agent credential, so the control
   plane attributes them to it. The loop also records `created_by: agent_loop` and the trigger id in
   its ledger. ADR 0084's submission body carries no trigger field, so the ledger is the join.
10. **Idempotent per trigger.** One job per `(project, trigger_id, kind)`, enforced by a unique key
    in a private ledger and by a deterministic idempotency key sent with each submit. A crash between
    a successful submit and the ledger write is therefore deduplicated by the submission side on
    replay. A failed submission records no job and a `submit_failed` marker that a later replay may
    replace; a retry that now hits a final skip replaces that marker in the same transaction.
11. **Policy cap with recorded skips.** The policy sets a per-job cap, a maximum job count and a
    total budget. When a cap would be exceeded the job is not created and is never topped up. A skip
    with its reason is recorded once per `(trigger, kind)`: `cap_exhausted`, `disabled`,
    `input_sealed`, or a refusal code returned by the submission side. **Final skips are
    permanent for that trigger.** Re-enabling the policy or raising a cap later does not create jobs
    for an earlier trigger; the next plan advance is a new trigger. The tier is passed through
    unchanged. A slot is reserved in the ledger in a short transaction that counts committed jobs
    and live reservations, the submission runs with no lock held, and a second short transaction
    records the job or the skip. Two processes sharing a ledger therefore cannot both pass the cap,
    and a slow submission never blocks another writer. New reservations never exceed the caps: a reservation
    older than its time limit is stale but still counts against both caps. Each reservation has an owner token; a takeover issues a new one, and a late
    failure from a previous owner changes nothing, so it can never free the current owner's slot. It
    is released only when
    the same trigger is replayed (the same idempotency key is sent, so the submission side returns
    the same job) or when an operator releases it. A crashed or slow submission may therefore hold
    a slot until then; the submission side must bound its own duration. A reservation inside its
    limit is reported as a retryable `in_flight` skip. If a submission succeeds but the ledger stays
    busy, the result carries the job id with status `created_unrecorded`, the reservation keeps
    counting, and a replay records the job exactly once. A job that already exists upstream is
    always recorded. If an operator released its reservation and the slot was then taken, the late
    success still lands on the record and is flagged `over_cap`, since the receipt must match what
    ran. A failure or refusal from an orphaned owner changes nothing and leaves a row in a stale
    completions log, so every attempt stays auditable. Old skip rows can be pruned; jobs are kept
    for audit. Pruning a final skip makes that trigger and kind eligible again, so a trigger the
    destination policy refused (for example `destination_not_enrolled`) is submitted and refused
    again. That is safe for egress, since the submission side re-checks policy on every submit, but
    it repeats the refusal. An operator who wants refusals to stay recorded should
    not prune them.
12. **Private inputs.** ADR 0047 stands. The loop does not decide whether a non-public input may go
    to a destination: it sends each input's data class and its destination to the submission side,
    whose destination policy (the registry of ADR 0082; refusing everything by default in ADR 0084)
    admits or refuses it with `data_class_not_allowed_for_destination` or
    `destination_not_enrolled`. The loop never widens egress: it requests `model_runtime` only when a
    non-public input is present, and `none` otherwise. A step that needs public web fetch is a
    separate allowlisted step; a model call never opens another host. Sealed or holdout inputs are
    never review inputs.
13. **Findings stay advisory.** The loop cannot accept or reject its own findings, approve a
    campaign, promote a plan or write a gate. The planner exposes no such operation; a human-backed
    client resolves findings through `tessera_mcp` (ADR 0084).

## Consequences

The loop can keep research moving, and every automatic job is attributable, capped and receipted.
Spend is bounded by policy rather than by attention. The cost is a new operator switch and a policy
to author. A call of unknown outcome needs an operator decision rather than a silent retry. The
ledgers are private journals in `tessera_agent`, so there is no control-plane schema change.

## Alternatives considered

- **Per-plan parameterized standing approval:** sound, but it needs an exception to ADR 0042 and new
  control tables. This ADR keeps the first version to a ledger and ADR 0084's submission interface
  and can later bind to such a policy.
- **Downshifting tier under quota:** rejected. It silently changes what produced a finding.
- **Cancelling in-flight work on kill:** rejected. The switch must be safe to use at any time.
- **A plain "allow private" setting on the planner:** rejected. The check belongs to the
  destination registry on the submission side, not to an operator toggle in the caller.

## Tests

`tests/test_standing_loop.py` and `tests/test_auto_research_jobs.py`: kill switch and fail-closed
marker, backoff with no downshift, identity and tier mismatch, rate cap, halt and reset, uncertain
outcomes surviving a restart, idempotent replay, retry after a failed submit that ends in a final
skip, cap skips recorded once, concurrent writers on one ledger, refusal codes recorded as skips,
egress never wider than `model_runtime`, sealed refusal, atomic ledger. Submission is stubbed.

## Open questions

1. Whether successors steering admitted campaigns should also be gated by a campaign-level standing
   approval (ADR 0042).
2. The submission side must deduplicate on the idempotency key. ADR 0084's `Idempotency-Key`
   handling is assumed to do so; confirm when the loop is wired in.
3. Where the loop's start hook lives (plan admission vs. a polling reader) once ADR 0084 lands.
