# 0042 — Standing approvals and scheduled campaign starts

## Status

Implemented (schema v67). Extends the authority model of
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) §2 and the provider-trust
rules of [ADR 0040](0040-multi-stage-multi-vendor-campaigns.md). Schema migration v66 → v67
(v66 is [ADR 0041](0041-execution-frames.md)'s).

## Context

Every campaign today needs a fresh human approval of its exact digest (`approve_campaign`).
A recurring evaluation, a retry after a failure, or a second campaign after the first one
succeeds each need a person to approve again. The platform goal asks for an operator-configured way for
Tessera to start campaigns with no new approval for each start, without letting automation
gain authority a human approval would not give.

Facts this design relies on: `campaign_approvals` already records `decision_class` and
`automation_label`. The runner acts as `approving_principal_id` and pauses when that
principal is no longer live (`approver_is_live`). A forward approval expires at most
`deadline_seconds` after the first approval. The enforcement points take the intersection
of the approval's `trust_policy_digest`, the current provider-trust setting and the stage's
`send_classes`. The runner holds a single lease and does all its work in
`runner_connection()` (`BEGIN IMMEDIATE`).

## Decision

### 1. A standing approval is a human decision about a template

A **standing approval** binds one spec digest (the template) to explicit caps. A human
creates it with the same actor rule as `approve_campaign`: `role == "human"` plus
`require_decision_actor`, so the owner credential needs an automation label. Agents,
workers, and any credential with external-agent scopes are refused (403), even if they
hold `campaign:*`. No MCP tool is added. The body carries the full spec and an
`expected_spec_digest`. The digest must match, and the spec is validated as at submit and
forward approve (`_check_references`, providers on). The caps are:

- `per_start_budget_cap_cents`: at most the spec's `budget_cap_cents` and at least the
  largest stage `max_exposure_cents`. It becomes `campaigns.budget_cap_cents` for every
  campaign started from the template. A start can lower the cap but never raise it.
- `max_starts` (1–100) and `max_total_budget_cents`: the sum of the per-start caps of all
  campaigns started (a committed amount, not the amount spent). Retries and follow-ons
  each count as a start, against the standing approval that started them.
- `min_interval_seconds`: null, or 60 to 30 days, measured from the last start. Missed
  intervals do not accumulate, so a restart never causes a catch-up burst.
- `max_concurrent` (1–4, default 1): the number of non-terminal campaigns this standing
  approval may have at once.
- `not_before` and `not_after`, with `not_after` at most 30 days after creation. These
  bound when starts can happen. They do not bound campaigns already running.
- `trigger`: `schedule` (the scheduler starts it within the caps) or `follow_on` (only a
  predecessor's success can start it).
- `follow_on_standing_approval_id` (optional): a `follow_on` standing approval in the same
  project that is active and already exists. It must not be this standing approval. This
  field is immutable, so a cycle cannot form. In v1 no data passes from one campaign to
  the next. The follow-on's spec pins its own inputs, and parameterized templates are out
  of scope.
- `retry_max` (0–5) and `retry_on` (`failed` or `failed_or_stopped`): restart the same
  template after the campaign ends that way. A retry never follows `cancelled`,
  `blocked` or `succeeded`, a `budget_refused:*` or `cancel:*` stop reason, a campaign
  with `held_cents > 0`, or a campaign with an `unknown` attempt.

Creation records the creator's `decision_class`, `automation_label` and the current
`trust_policy_digest`. None of the caps can be edited after creation. To change one, revoke
the standing approval and create a new one.

### 2. A started campaign gets no more than a human approval would give

Each start writes an ordinary `campaigns` row (`submitted_by_principal_id` = the creator)
and an ordinary `campaign_approvals` row:

- `approving_principal_id` = the creator. Delegation, liveness pauses and event
  attribution stay unchanged.
- `decision_class = 'standing_approval'` (new constant `CLASS_STANDING_APPROVAL`).
- `automation_label = 'standing_approval:<id>'`, plus a new `standing_approval_id` column.
- `expires_at = start + deadline_seconds`, the same rule as a human approval.
- `trust_policy_digest` = the standing approval's recorded digest, not the current one.
  The existing intersection rule therefore lets a later change to the trust setting narrow
  a start but never widen it.

The closed step table, data-class checks, retry limits, budget holds and cancel behavior
are unchanged. A paused automated campaign can be resumed only by a normal human
re-approval, which still respects the bound of the first approval plus
`deadline_seconds`. Delegated events carry `approval_decision_class = standing_approval`.

### 3. Scheduler in the runner tick

`CampaignRunner.tick` calls `_schedule()` after it takes the lease and before it drives
campaigns. `tessera serve --no-campaign-scheduler` turns it off, and
`--no-campaign-runner` turns it off as well. Each tick makes at most one start per
standing approval and at most `MAX_STARTS_PER_TICK = 4` in total. As with campaigns,
an error in one standing approval does not stop the others. Each candidate is handled in
**one** `runner_connection()` transaction, which checks the following in order:

1. The standing approval is `active`, `not_before <= now < not_after`, and the caps are
   not exhausted. Otherwise it moves to `expired` or `exhausted`, with an event.
2. The creator is live and human (the delegation hook's check) and still a member of the
   project (`Store._project` with the creator's live actor).
3. `starts_used < max_starts` and `committed_budget_cents + per_start <= max_total`.
4. `min_interval_seconds` has elapsed and the non-terminal count is below
   `max_concurrent`.
5. Fail closed on exposure: no campaign from this standing approval is `blocked`, has an
   `unknown` attempt, or has unknown experiment exposure.
6. Project spend: an `execution_project_limits` row exists, and `monetary_limit_cents -
   held - settled`, minus the unspent cap (`budget_cap - held - settled`) of every
   non-terminal campaign in the project, is at least `per_start`.
7. The stored spec parses with providers enabled and its digest still matches.
   `_check_references` runs with the creator's live actor and the current trust setting,
   and `check_stage_inputs` gets `approval_digest` = the recorded digest. An unregistered
   provider (such as Salad without a profile), a retired worker or snapshot, or a
   data-class or trust refusal stops the start.

If any check fails, nothing is created. The transaction only sets `last_refusal_reason`
and appends `campaign.standing_start_refused`, and only when the reason changes, so a
refusal that keeps happening does not flood the log. If every check passes, the same
transaction inserts the campaign, its stages, the approval row (campaign `running`,
revision 2) and the `campaign_standing_starts` row with
`start_index = starts_used + 1`. It then bumps the counters and revision, resolves any
trigger row (§4), and appends `campaign.submitted`, `campaign.approved` and
`campaign.standing_started`.

**Idempotency.** `PRIMARY KEY(standing_approval_id, start_index)`,
`UNIQUE(trigger_campaign_id, kind)` and the trigger table's primary key, together with
the IMMEDIATE transaction and the runner lease, ensure that a crash or a second runner
cannot start anything twice. A crash before commit leaves nothing behind, and a crash after
commit leaves a complete start.

### 4. Retries and follow-ons

When a campaign started from a standing approval reaches a terminal status, the scheduler
writes at most one `campaign_standing_triggers` row per `(trigger_campaign_id, kind)`:

- `kind = retry` targets the same standing approval, when the conditions in §1 hold and
  `retry_number < retry_max`.
- `kind = follow_on` targets the follow-on standing approval, and only when the campaign
  is `succeeded` with its receipt written. This is an operational outcome. The
  scientific verdict is not consulted.

A trigger stays `pending` until a start under §3 resolves it to `started` (in the same
transaction). It becomes `abandoned` when its target is revoked, expired or exhausted, or
when the target's spec no longer validates, and that step appends
`campaign.standing_trigger_abandoned`. A temporary refusal (spend headroom, concurrency,
interval) leaves the trigger pending.

### 5. Revoke, caps exhausted, budget refusal, restart

- **Revoke.** Any human in the project (same actor rule, `expected_revision`) can revoke.
  Revoking stops all new starts: in the same transaction the status becomes `revoked` and
  pending triggers become `abandoned`. Running campaigns keep their own approval and
  deadline, as they would after a human approval. The revoke response and event list them,
  and `tessera campaign standing revoke --cancel-running` then sends an ordinary cancel
  for each one, separately. Revoking the creator's principal also stops starts, through
  check 2.
- **Caps exhausted** or **`not_after` passed.** The status becomes `exhausted` or
  `expired` (a terminal status) with one event. Running campaigns are not affected.
- **Budget refusal.** A refusal at start time (check 3 or 6) creates nothing. A
  `refused_budget` stage inside a started campaign follows ADR 0038 §3 unchanged and is
  never retried.
- **Restart.** The scheduler keeps no state in memory. It derives every decision from
  SQLite and `now` (the runner's clock). The interval counts from the last committed
  start.

### 6. Data model (v66 → v67, additive)

`campaign_standing_approvals`: `id` (`standing_<hex>`), `org_id`, `project_id`,
`spec_json`, `spec_digest` (64 hex), `created_by_principal_id`, `created_credential_id`,
`decision_class`, `automation_label`, `trust_policy_digest`, `per_start_budget_cap_cents`
(0–5000), `max_starts` (1–100), `max_total_budget_cents` (≥ per-start), `min_interval_seconds`,
`max_concurrent` (1–4), `trigger` (`schedule`|`follow_on`), `follow_on_standing_approval_id`
(self-FK, ≠ id), `retry_max` (0–5), `retry_on` (`failed`|`failed_or_stopped`),
`not_before`, `not_after`, `status` (`active`|`exhausted`|`expired`|`revoked`),
`starts_used`, `committed_budget_cents`, `last_start_at`, `last_refusal_reason` (≤ 300),
`revoked_by_principal_id`, `revoked_at`, `revoke_reason`, `revision`, `created_at`,
`updated_at`. The table has `CHECK(starts_used <= max_starts AND committed_budget_cents <=
max_total_budget_cents)` and an index on `(project_id, status)`.

`campaign_standing_starts` is append-only (UPDATE and DELETE triggers abort). Its columns
are `standing_approval_id`, `start_index`, `campaign_id` (UNIQUE FK), `approval_id` (UNIQUE
FK), `kind` (`scheduled`|`retry`|`follow_on`), `trigger_campaign_id`, `retry_number` (0–5),
`budget_cap_cents` and `created_at`, with `PRIMARY KEY(standing_approval_id, start_index)`
and `UNIQUE(trigger_campaign_id, kind)`.

`campaign_standing_triggers`: `trigger_campaign_id`, `kind` (`retry`|`follow_on`),
`target_standing_approval_id`, `retry_number`, `status` (`pending`|`started`|`abandoned`),
`start_index`, `reason`, `created_at`, `resolved_at`, with
`PRIMARY KEY(trigger_campaign_id, kind)`.

`campaign_approvals` gains `standing_approval_id TEXT` (null for human approvals). The
migration marks the last backup stale, as v66 does. `LATEST_SCHEMA_VERSION = 67`.

**Events.** `campaign.standing_approval_created` and `campaign.standing_approval_revoked`
(both added to `DECISION_EVENT_TYPES`), plus `campaign.standing_approval_exhausted`,
`campaign.standing_approval_expired`, `campaign.standing_started`,
`campaign.standing_start_refused` and `campaign.standing_trigger_abandoned`. The
`campaign.approved` event for an automated start carries `standing_approval_id`,
`start_index`, `kind` and the creator's original `standing_decision_class`.

### 7. Surfaces

The routes are human-only: agents, workers and external-scope credentials get a 403,
including on GET. Every POST goes through `_mutate` with an `Idempotency-Key`.

- `POST /projects/{project_id}/standing-approvals`
- `GET /projects/{project_id}/standing-approvals` (paged)
- `GET /projects/{project_id}/standing-approvals/{id}` (with starts and triggers)
- `POST /projects/{project_id}/standing-approvals/{id}/revoke` (`expected_revision`,
  `reason`)

CLI: `tessera campaign standing create --project-id P --spec FILE --expected-spec-digest D
--per-start-budget-cents N --max-starts N --max-total-budget-cents N --not-after T
[--not-before T] [--min-interval-seconds S] [--max-concurrent N] [--trigger
schedule|follow_on] [--follow-on ID] [--retry-max N --retry-on failed|failed_or_stopped]
--automation-label L`. Also `tessera campaign standing list|show --project-id P [--id S]`,
`tessera campaign standing revoke --project-id P --id S --expected-revision N --reason R
[--cancel-running]`, and `tessera serve --no-campaign-scheduler`. There is no desktop UI
in v1.

### 8. Tests (default pytest, fake providers, injected clock)

`tests/test_campaign_standing.py`:

1. `test_human_creates_standing_approval_with_digest_and_provenance`
2. `test_agent_worker_and_external_scope_cannot_create_read_or_revoke`
3. `test_unlabelled_owner_credential_refused`
4. `test_digest_mismatch_and_invalid_spec_refused`
5. `test_per_start_cap_bounds` (above the spec cap, below the largest hold)
6. `test_follow_on_must_be_same_project_follow_on_trigger_not_self`
7. `test_scheduled_start_creates_campaign_and_automation_approval`, which checks the class,
   label, `standing_approval_id`, `expires_at = start + deadline_seconds`, and the recorded
   trust digest
8. `test_start_runs_through_existing_delegation_to_success`
9. `test_max_starts_and_total_budget_exhaust`
10. `test_min_interval_no_catch_up_after_clock_jump`
11. `test_max_concurrent_blocks_overlap`
12. `test_not_before_not_after_window_and_expiry_event`
13. `test_project_spend_headroom_refusal_creates_nothing`
14. `test_blocked_or_unknown_exposure_refuses_start`
15. `test_trust_setting_widening_not_inherited_narrowing_refuses`
16. `test_data_class_and_unregistered_provider_refuse_start`
17. `test_creator_revoked_principal_stops_starts_and_pauses_running`
18. `test_refusal_event_once_per_reason`
19. `test_revoke_stops_starts_abandons_triggers_running_continue`
20. `test_retry_on_failed_counts_as_start_and_respects_retry_max`
21. `test_no_retry_after_cancel_budget_refused_blocked_or_held`
22. `test_follow_on_starts_once_after_success_only`
23. `test_follow_on_abandoned_when_target_revoked_or_exhausted`
24. `test_crash_after_commit_no_double_start` (same `start_index` replay, two runners)
25. `test_restart_resumes_schedule_from_sqlite`
26. `test_scheduler_disabled_flag`
27. `test_human_reapproval_of_paused_automated_campaign_keeps_first_bound`

In `tests/test_campaign_migration.py`: `test_v66_to_v67_adds_standing_tables_and_column`
and `test_standing_starts_append_only` (`test_migration_convergence.py` must also pass
for v67). In `tests/test_campaign_cli.py`: `test_campaign_standing_cli_round_trip`.

## Consequences

- A standing approval never carries a review pre-approval. A scheduled start inherits no
  Salad source-assessment or plan / source-use review decision and pauses for a human
  there. The standing-approval create body has no review field. Any opt-in review
  pre-approval is a separate decision made in a human approve body, and a standing
  approval does not carry it.
- Creator liveness is bound to the credential that created the standing approval: if that
  credential is expired, revoked or replaced, starts stop.
- Without `min_interval_seconds`, starts can be back to back, limited by `max_concurrent`,
  `max_starts` and the budget caps.
- Unattended recurring, retried and chained campaigns become possible. Each start is
  bounded by a human decision that names its digest, money, count, time window and
  successors.
- The authority of a single campaign never exceeds that of a human approval. The total
  is bounded only by caps the human set: `max_starts × per_start` up to
  `max_total_budget_cents`, within `not_after`.
- Automated starts are visible as such in the approval row, events and receipt.
- A recorded start, like a recorded approval, is not evidence that a run happened. Read
  the campaign receipt.
- If the creator's session credential expires, starts stop and running campaigns pause
  until a human acts. This follows ADR 0038, and v1 keeps it on purpose.
- Not covered: parameterized templates, data handoff between chained campaigns, cron
  expressions, editable caps, desktop UI, and windows longer than 30 days.

Implemented in `campaign_standing.py`, `campaign_standing_store.py`,
`campaign_standing_schema.py` with tests in `tests/test_campaign_standing.py`.
