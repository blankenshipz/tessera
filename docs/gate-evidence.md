# Gate evidence: verdicts bound to what actually ran

A research-program go/no-go gate can be declared so that it is satisfied only by
platform-computed aggregates of stored per-seed measurements, each bound to an
admitted attempt that was pre-registered against a locked, leakage-audited
split. This page describes those records and the version-2 ("bound") gate
protocol. The version-1 caller-reported protocol in
[the program evaluation contract](program-evaluation-and-salad-contract.md) remains
available for existing plans and is labelled as legacy.

None of these records grants execution, imputes a value or certifies scientific
validity. Every response carries `scientific_validity: "not_assessed"`. Every
write is human-only, idempotent (`Idempotency-Key`), tenant-scoped and
append-only. No update or delete routes exist.

## Evidence chain

```text
split lock (exact membership, server digest)
  ├─ within-lock leakage audit (recorded at lock creation)
  └─ cross-set leakage audits (for example pretraining corpus vs eval partitions)
admitted attempt ── split binding (only while the attempt is still `admitted`)
  └─ measurement (server-set source: human_entered | derived)
       └─ metric record (metric = measurement name, seed = bound seed; value read by server)
replicate group (all metric records of one variant/metric/partition series)
  └─ n, mean, SD, 95% CI computed by the platform
version-2 gate observation ── cites the replicate group id, never a number
```

## Measurement source

Measurement rows have a `source` column that only the server sets:

- `POST /experiment-attempts/{id}/measurements` records `human_entered`.
- `POST /experiment-attempts/{id}/derived-measurements` records `derived`. The
  value is read from retained output bytes.

A caller-supplied value whose top-level keys include `derivation`,
`json_pointer`, `artifact_sha256`, `source` or `measurement_source` is rejected.
The gate-evidence migration classifies existing rows only from durable evidence: a row is
`derived` when its creating transaction emitted `experiment.measurement_derived`.
Every other row is `human_entered`, even if its value contains a `derivation` key.

## Split locks and leakage audits

`POST /projects/{project_id}/split-locks`

```json
{
  "name": "example scaffold split v1",
  "partitions": {
    "train": {"keys": ["mol-1", "mol-2"], "groups": ["scaf-A", "scaf-B"]},
    "test": {"keys": ["mol-9"], "groups": ["scaf-Z"]}
  },
  "dataset_snapshot_id": null,
  "expected_membership_digest": null
}
```

- **Partitions.** Up to 16 partitions and 200,000 members in total.
- **Keys and groups.** Keys must be unique within a partition. `groups` is
  optional and parallels `keys`. Use it for scaffold, connected-component or
  patent-family ids.
- **Digest.** The server computes each partition digest and the
  `membership_digest` from the sorted membership. Order does not matter. If
  `expected_membership_digest` is given and differs, the request returns 409.
  Registering the same membership twice in a project also returns 409.
- **Within-lock audit.** The `within_lock` audit is computed in the same
  transaction. It counts member keys and group keys that appear in more than one
  partition. A lock with overlap is still recorded, and its audit says
  `overlap_detected`.
- **Responses.** A response never returns the member list. It returns counts,
  digests and an overlap sample of at most 20 keys or groups.

`POST /split-locks/{lock_id}/leakage-audits` records a `cross_set` audit:
`{"reference_lock_id", "reference_partitions": [...], "eval_partitions": [...]}`.
The server counts key and group overlap between the reference partitions (for
example a pretraining corpus registered as its own lock) and the eval
partitions. `GET /split-locks/{lock_id}` returns the lock and its audits.

Locks are immutable, so every recorded overlap stays true. For each reference
lock, the gate evaluator reads the cross-set status like this:

- `overlap_detected` if any audit against that reference found overlap;
- `clean` only if a clean audit covers the gate's partition and every partition
  of the reference lock;
- `missing` otherwise.

A later, narrower clean audit cannot mask an earlier overlap.

Dataset snapshots may declare `split_strategy.leakage_status: "audited"` with a
`split_lock_id`. Registration verifies that the lock is in the study's project
and that its within-lock audit is clean. `declared` and `unsupported` are
unchanged.

## Binding an attempt to a split

`POST /experiment-attempts/{attempt_id}/split-binding` with
`{"expected_revision", "split_lock_id", "seed", "metrics"}` pre-registers the split,
the seed and the metrics that an admitted attempt will report. `metrics` is 1 to 32
declarations `{"name", "partition", "unit"}` with unique names: `name` is the
measurement name the attempt will record, `partition` the locked partition it is
computed on, and `unit` its unit.

- It is allowed only while the attempt state is `admitted`, before dispatch.
- It is allowed only once per attempt, and a seed only once per variant and lock.
- The lock must be in the same project, and every declared partition must exist in
  the lock (409 otherwise).
- The declarations are stored canonically (sorted by name) and returned as
  `metrics` by the binding responses and `GET`. Bindings that predate metric
  pre-registration have no declarations.
- It bumps the attempt revision.

`GET` on the same path returns the binding.

## Metric records

`POST /experiment-attempts/{attempt_id}/metric-records`

```json
{"measurement_id": "measurement_…", "checkpoint_sha": "<64 hex or null>"}
```

The caller names a stored measurement. It does not send a number, a metric name,
a partition, a unit or a seed (any such field is a 422). The metric is the
measurement's recorded `name`; the seed, partition and unit come from the
declaration pre-registered at binding, so a result cannot be relabelled or moved
to another partition after it is seen. Requirements:

- The attempt must have a split binding with a pre-registered seed and metric
  declarations. A binding without declarations is a 409 ("predates metric
  pre-registration").
- The measurement `name` must be one of the declared metric names (409 otherwise:
  "not pre-registered for this attempt").
- The measurement must belong to this attempt.
- The measurement value must be an object with a finite numeric top-level
  `"number"`.

The server copies the value, the measurement `source`, the lock,
`membership_digest` and the partition size (`sample_count`). There is one record
per attempt/metric/partition and one record per variant/lock/metric/partition/seed.
`checkpoint_sha` is a plain digest string. A later artifact-lineage change will link
it to checkpoint identity.

## Replicate groups

`POST /projects/{project_id}/replicate-groups`

```json
{"experiment_variant_id": "…", "metric": "rmse", "split_lock_id": "…",
 "partition": "test", "ci_method": "t_interval_95"}
```

The server selects the members, so seeds cannot be cherry-picked. The series is
the attempts bound to that variant and lock whose binding declared this metric
name on this partition. Attempts bound only for other metrics are not part of it.
A legacy binding without declarations declared nothing, so it is counted in
every series of its variant and lock and can never be silently dropped. One shared accounting (`series_accounting`) is used by
creation and by the currency check. Creation returns 409 when:

- a series attempt is not resolved (`admitted`, `preparing`, `running`, `unknown`,
  `interrupted`, `restart_pending`). A pre-registered seed that was never
  dispatched blocks too: aggregating only the seeds that were run would be optional
  stopping. A never-dispatched seed is withdrawn only by resolving it `cancelled`
  with a recorded reason (`POST /experiment-admitted-attempts/{id}/resolve`);
  cancellation before dispatch needs no execution evidence because nothing ran, and
  a resolved attempt can never be packaged or dispatched afterwards.
- a `succeeded` series attempt has no metric record for the series. Record it, or
  resolve the attempt as failed with a reason.
- the members mix units.
- there are fewer than two distinct seeds.

Series attempts that are `failed` or `cancelled` are listed in `excluded_attempts`
with their outcome and reason, so withdrawn seeds stay visible in the group. They
are never imputed. If a new attempt joins the series later, the group becomes
stale.

Statistics (`statistics_version` 1, `gate_evidence_stats.py`, stdlib only):

- `mean` and sample `std` (ddof 1).
- `t_interval_95`: mean ± t(0.975, n−1)·sd/√n. The t table covers df 1–30, 40,
  60 and 120, and uses 1.959964 beyond that. A df that is not in the table uses
  the next lower tabulated df, which gives a wider, conservative interval.
- `percentile_bootstrap_95`: 10,000 resamples of the seed values, using a PRNG
  seeded from a digest of the member ids. It is deterministic. The interval is
  the sorted resample means at index ⌊0.025·B⌋ and ⌈0.975·B⌉−1. With few seeds
  this interval is too narrow, so prefer the t-interval for small n.
- When sd is 0, the interval collapses to the mean.

The group stores `n`, `mean`, `std`, `ci_low`, `ci_high`, `min_sample_count`,
`sources`, `excluded_attempts` and a `result_digest` over the full computed
result. `GET /replicate-groups/{group_id}` returns it with its members.

## Version-2 bound gates

A plan's `evaluation_protocol` may use `version: 2`. Each gate is a
`BoundMetricGate`:

| field | meaning |
| --- | --- |
| `variant_id` | registered study variant (as in version 1) |
| `experiment_variant_id` | experiment variant the group must measure; its experiment must be linked from the study variant's `metadata.experiment_id` |
| `metric`, `unit` | must equal the group's |
| `split_lock_id`, `split_partition`, `split_digest` | `split_digest` must equal the lock's `membership_digest` and the partition must exist in the lock (both checked when the plan is proposed) |
| `ci_method` | `t_interval_95` (default) or `percentile_bootstrap_95`; must equal the cited group's method |
| `statistic` | `mean`, `ci_low` or `ci_high` compared with `threshold`; use `ci_low` for a conservative "≥" gate |
| `minimum_replicates` | at least 2; compared with the group's `n` |
| `minimum_samples` | compared with the group's `min_sample_count` (partition size) |
| `allowed_sources` | default `["derived"]`; set `human_entered` explicitly to accept hand-typed numbers |
| `required_reference_lock_ids` | locks (for example pretraining corpora) that need a clean cross-set audit covering the gate partition |

A version-2 evaluation record has `version: 2`,
`evidence_kind: "platform_replicate_aggregate"` and observations
`{"gate_id", "replicate_group_id"}` or `{"gate_id", "missing_reason"}`. It has
no value field. A version-1 caller-reported record against a version-2 plan
returns 409.

Each gate evaluates `inconclusive` when any binding fails. The reasons are:

- `replicate_group_not_found`
- `variant_not_linked`
- `variant_mismatch`
- `metric_unit_mismatch`
- `split_mismatch`
- `ci_method_mismatch`: the gate pre-registers `ci_method`
  (default `t_interval_95`), so a narrower method cannot be chosen later
- `replicate_group_stale`: the group no longer equals its current series (a
  series attempt was added, is unresolved, or changed outcome)
- `split_leakage_not_clean`
- `cross_set_leakage_not_clean`
- `disallowed_measurement_source`
- `insufficient_replicates`
- `insufficient_samples`

A missing observation is also inconclusive. Otherwise the gate compares
`group[statistic]` with the threshold. The aggregate status rules are the same
as in version 1, and any inconclusive gate blocks `go`. All existing go fences
still apply: an approved current plan, a human-approved current evaluation, a
human-approved source-use review, accepted findings and complete execution
accounting. A human must still approve the `go`.

The evaluation is recomputed when it is approved and when a go is approved.
Groups are immutable. An overlap audit recorded later turns a passing gate
inconclusive, which is fail-closed.

### New plans require version-2 gates

A plan proposed now must declare an `evaluation_protocol` with `version: 2`.
Proposing a version-1 (caller-reported) or protocol-less (prose-only) plan returns
409, for humans and agents alike. A go on a new plan therefore always rests on
bound gates.

### Legacy paths

Plans recorded before this rule are legacy. They stay readable, nothing is
rewritten, and a legacy proposal that was already pending can still be resolved by
a human. Their existing evaluation and go flows keep working. Program context
labels each evaluation and go review with `gate_evidence_binding`:

- `platform_replicate_aggregate`: version 2.
- `unbound_caller_reported_legacy`: version 1. A caller-reported float can still
  satisfy a legacy version-1 plan.
- `none_prose_only_legacy`: a go on a legacy plan with no evaluation protocol. Such
  a go needs no gate at all.

Version-1 reports also carry `evidence_binding: "unbound_caller_reported_legacy"`.

## Research programs and outcomes

Outcome ids and condition accounting are declared by each plan. An outcome id is
a snake_case identifier (`^[a-z][a-z0-9_]{0,63}$`). Every plan, for every
outcome, declares its own conditions: at least one and at most 20 snake_case
keys, at least one `selected`. Selected conditions need variants and deferred
ones have none. The platform holds no per-outcome catalogs and no
experiment-specific schema; any catalogs live as research-program data under
`experiments/` (for example `experiments/gate-evidence/outcome_condition_catalogs.json`,
not committed). `program_context` returns `"condition_catalog": {}`.

- `POST /projects/{project_id}/research-programs` with `{"name", "objective"}`.
- `POST /research-programs/{program_id}/outcomes` with
  `{"outcome", "study_id", "title"}`. A program has one study per outcome, and a
  study belongs to one outcome. Any snake_case outcome id is accepted.
- `GET /research-programs/{program_id}` lists only the attached outcomes and a
  read-only spend roll-up.

The roll-up covers each outcome's linked experiments (from study variants'
`metadata.experiment_id`). For each experiment it reports budget state,
ceiling, recorded charges, held reservations and settled reservations. It also
gives outcome totals and program totals, where each experiment is counted once.
Experiment links that cannot be resolved are listed in
`unresolved_experiment_links`. `spend_basis` is
`tessera_budget_ledger_records_not_provider_billing`: these are Tessera ledger
records, not provider invoices. The roll-up never writes to budget tables.

## Follow-ups

- **Move route approval off program plans.** A Salad (or other remote) route is
  approved today through a research-program plan review plus a source-use review,
  so an operational smoke must declare a program outcome, conditions and a
  version-2 bound-gate protocol it cannot satisfy. `scripts/salad_live_setup.py`
  does exactly that: it declares a generic `gpu_route_smoke` outcome with one
  selected condition and a version-2 gate (`reference_match eq 1.0`, mean of at
  least 2 replicates) on a one-member smoke split lock, so one admitted attempt
  leaves the gate inconclusive and makes no scientific claim. There is no gate
  exemption. Route authority should get its own review contract so the uniform
  version-2 plan rule only governs research plans.
- **Desktop plan authoring.** The platform condition catalog is now empty
  (`program_context` returns `"condition_catalog": {}`), so the desktop client
  must let plan authors declare the outcome id, the condition keys with their
  selected/deferred accounting, and a version-2 evaluation protocol (bound
  gates referencing project split locks). Until it does, new plans can only be
  proposed through the API.
