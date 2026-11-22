# 0045 — Conditional campaign unlock, paired-delta groups and program budgets

## Status

Implemented (schema v69). Extends the admission rules of
[ADR 0038](0038-campaign-specs-and-platform-campaign-runner.md) §2 and
[ADR 0042](0042-standing-approvals-and-scheduled-campaign-starts.md) §3, and uses the typed
metric records and replicate groups of the gate-evidence design. Schema migration
v68 → v69; v68 is ADR 0044's (review pre-approvals).

## Context

A research plan often says "run campaign B only if campaign A went well". Today a person
must watch A, decide, and approve B by hand. A standing approval (ADR 0042) can chain B
after A with `follow_on`, but only on A's operational success: the scientific result is
never consulted. Separately, each campaign, standing approval and project has a spend cap,
but nothing bounds the money committed across a line of work.

Facts this design relies on:

- `approve_campaign` runs inside `Store._mutate` (`BEGIN IMMEDIATE`), and every scheduler
  start runs checks and `_start` in one `runner_connection()` (`BEGIN IMMEDIATE`) under the
  runner lease. SQLite serializes these writers.
- Scheduled, retry and follow-on starts all go through `_attempt`.
- `campaign_stage_attempts.admitted_attempt_id` links a campaign attempt to an admitted
  experiment attempt, whose variant and pre-registered seed place it in a metric series.
- `series_accounting` reports a series' members, `missing_record`, `blocking` and `excluded`
  attempts and never imputes. `record_metric` is human-only.

## Decision

### 1. Unlock requirements

An **unlock** is a list of one to four `UnlockRequirement`s. A campaign (on its first,
forward approval) or a standing approval (on every start, including retries and
follow-ons) is admitted only if **every** requirement is satisfied:

```
{ campaign_id,                  # the earlier campaign
  accept_human_go: bool,        # a current human "go" on that campaign satisfies it
  bound_outcome: null | {       # a platform-computed outcome satisfies it
      arm_a_variant_id, arm_b_variant_id, metric, split_lock_id, partition,
      ci_method, statistic: mean|ci_low|ci_high, comparison: ge|gt|le|lt, threshold,
      minimum_pairs, minimum_samples, allowed_sources,
      allowed_outcomes: subset of [pass, fail] } }
```

At least one acceptance must be set; either holds. Before either is consulted the named
campaign must exist in the same org **and** project (otherwise "not found"), have status
`succeeded`, `stopped` or `failed`, and have `held_cents = 0` and no `unknown` attempt.

**Human go.** `POST /campaigns/{id}/gate-decision` with `{expected_revision, decision:
go|no_go, note}` appends a row to `campaign_gate_decisions` for a finished campaign with
resolved exposure. The actor rule is `approve_campaign`'s: `role == "human"` plus
`require_decision_actor`; agents, workers and any external-scope credential get 403.
There is no MCP tool. The latest row per campaign is effective, so a later `no_go`
retracts a `go`. Only a decision whose class is `human_session` counts as a go: the owner
credential with an automation label may record one, but it never unlocks. An effective `no_go` (any class) is also a **veto**: it refuses the requirement even
if a bound outcome would pass. A go is a human direction, not scientific acceptance.

**Bound outcome.** The human pre-registers the rule inside the requirement, so no agent
chooses a threshold. The platform computes the paired-delta (§2) from typed metric
records, restricted to the named campaign, and compares the chosen statistic with the
threshold. The outcome is `pass` or `fail`; incomplete, unpaired, stale or otherwise
unevaluable data produce no outcome and the requirement is refused (`unevaluable_<code>`).

**Where it is evaluated.**

- *Forward approval.* `ApproveCampaign` gains an optional `unlock`, evaluated in the
  approval's own transaction when the campaign is `submitted`. A refusal is a 409
  (`Admission refused: unlock_not_satisfied:<index>:<code>`) and nothing is written. An approve
  that carries an `unlock` for a campaign that is not `submitted` is refused with a 409,
  not ignored.
- *Standing approval.* `CreateStandingApproval` gains an optional `unlock`, validated at
  creation (named campaigns exist in the project), stored immutably in `unlock_json`, and
  evaluated by `_attempt` on every start kind, after the existing checks. A refusal
  follows the existing refusal dedupe (`campaign.standing_start_refused`, one event per
  change of reason, distinct codes for `human_no_go`, `no_human_go`, `unevaluable_*`). It
  is temporary: a pending trigger stays pending, and one go unlocks every remaining start
  within the standing approval's own caps. Unparseable stored requirements refuse.

**Recording.** Each satisfied requirement appends a row to `campaign_unlock_evaluations`
in the admitting transaction (decision id for a go; outcome and `result_digest` for a bound
outcome). The campaign receipt gains an `unlock` section when any exist; receipts of
unconditionally admitted campaigns are unchanged.

### 2. Paired-delta replicate group

Generic, with no experiment-specific names. It pairs the per-seed typed metric records of
two experiment variants on one split lock, metric and partition and aggregates
`delta = A − B` with the platform statistics (`t_interval_95` or
`percentile_bootstrap_95`). It refuses, never subsets: any unresolved attempt, succeeded
attempt without a record, **or excluded (failed/cancelled) attempt in either arm** (so a
bad seed cannot be cancelled out of both arms), differing seed sets, mixed units, or fewer
than two pairs. With a campaign scope, every member attempt must have been admitted by that
campaign's stages, otherwise it refuses rather than filters.

`POST /projects/{id}/paired-replicate-groups` (human only, like replicate groups) persists
the result in `gate_paired_groups` / `gate_paired_members`; `GET
/paired-replicate-groups/{id}` reads it. The unlock check **never reads a stored group**: it
recomputes from the records inside the admission transaction, so a later record change or
new attempt cannot leave a stale pass. Wiring paired groups into evaluation-protocol v2
gates is out of scope.

### 3. Program budget

`tessera serve --program-budget FILE` loads a `tessera.program-budget.v1` owner-only file
(0600, no symlink, single link) with `programs: [{id, cap_cents, project_ids?}]`. A program
without `project_ids` covers every project of the installation. A program's **committed**
amount is the sum over its campaigns of `budget_cap_cents` (non-terminal, admitted) or
`held + settled` (terminal). Every first admission and every standing start must satisfy
`committed + new cap <= cap_cents` for every covering program. An unusable file stops
`serve` at startup; an in-process program budget marked broken refuses everything
(`program_budget_unavailable`; `mark_broken` exists as a test hook and nothing in `serve` calls it). Refusal reasons name the program id and never carry
totals, so no tenant sees another's commitments. The budget only narrows: the guardian cap,
campaign caps, standing caps and the project spend limit all still apply. A re-approval of
an already admitted (lapsed) campaign is not a new admission and is not double-counted.

### 4. Invariants

- Only a human decision actor writes gate decisions, creates requirements, creates
  standing approvals and creates paired groups. Agents can read decisions and receipts.
- Rule, threshold and allowed outcomes are fixed by the human before evaluation and cannot
  be edited on a standing approval.
- Evaluation, the program-budget check and the admission write share one `BEGIN IMMEDIATE`
  transaction. Decisions, records and campaign state are read inside it.
- Every check fails closed. Gate decisions, unlock evaluations and paired groups/members are
  append-only (triggers).
- Unlock is a condition of admission only: a later retraction stops future starts, not a
  campaign already admitted.

### 5. Data model (v68 → v69, additive)

`campaign_gate_decisions`, `campaign_unlock_evaluations`, `gate_paired_groups`,
`gate_paired_members` (all append-only) and `campaign_standing_approvals.unlock_json`. Event
`campaign.gate_decision_recorded` and `gate.paired_group_created`. The migration marks the
last backup stale. Without ADR 0044 present, a version-only v67 → v68 step is registered with
`setdefault`, so a real v67 → v68 migration is never overridden; deploy the two together.

## Consequences and limitations

- No cross-project unlock.
- Program caps bound **committed** amounts, not money spent.
- A bound outcome needs metric records. Manual `record_metric` is human-only; a stage that
  declares `gate_evidence` has them recorded by the runner under the campaign approval
  ([ADR 0049](0049-campaign-gate-evidence.md)).
- A first-admission check only: re-approving a lapsed campaign does not re-evaluate an
  unlock.
- Program budget recording on events and re-reading the file at each admission are not
  implemented; the file is read once at `serve` startup.
- An unlock evaluation or outcome is not scientific validation (`not_assessed`).

## Alternatives rejected

- **Unlock in the campaign spec:** it would change the digest and let the submitter author
  the condition.
- **An agent evaluates or reports the outcome:** it would choose data and threshold.
- **Trust a stored outcome:** records and attempts change; admission recomputes.
- **Reuse the study-level `go_no_go` review:** it is scoped to a study plan, not a campaign.
