# ADR 0168: review jobs can run — destination policy, research-kind runner, generic tiers

Status: Implemented (schema 89: review-job tiers become configuration and tier approvals bind
a grant). Builds on ADR 0084 (research and review jobs), ADR 0047 (agent-routed data
destinations), ADR 0040 (licence labels), ADR 0082 (the Anthropic destination profile),
ADR 0090 (model-spend ledger) and ADR 0142 (grants debited by model spend).

## Context

ADR 0084 review jobs (`plan_review`, `dataset_scout`, `technique_scout`, `pre_gate_review`,
`code_review`) existed with their MCP tools, but none could run:

1. The control API hard-wired `NoDestinationsPolicy`, so every submission was refused with
   `destination_not_enrolled`. There was no operator-attested way to admit a data class to a
   registered model destination.
2. Only the code review runner existed. Nothing claimed the four research kinds.
3. The tiers were a fixed database enum (`haiku`, `sonnet`, `opus`, `fable`), `fable` alone
   needed approval, and an approval named no budget that would pay for the model.

## Decision

### Review destination policy

`tessera serve --review-destinations FILE` (env `TESSERA_REVIEW_DESTINATIONS`) loads an
operator-attested `tessera.review-destinations.v1` file into `app.state.review_policy`. Without
it `NoDestinationsPolicy` stays the default and every submit is refused.

- **File checks:** the file gets the provider trust file's checks: a regular file, not a
  symlink, owned by the service user, not group- or world-writable, at most 64 KiB, valid JSON.
  A refused file stops serve (exit 2) rather than starting a server that silently refuses.
- **Destinations:** each entry names a destination id that must be registered in the
  code-reviewed model destination registry, with a model-runtime host allowlist that Tessera
  enforces itself. An unregistered id, or one Tessera does not reach through its own allowlist,
  is refused at load. So no file can name a public-internet or unregistered destination
  (ADR 0047).
- **Entry fields:**
  - `kind` is a closed set (`model_api`).
  - `data_classes` lists exactly which of `public`, `private_code_approved` and
    `private_derived` the destination may receive. There is no wildcard, and an unknown class
    is refused both in the file and at check.
  - `tiers` (optional) limits tier names.
  - `max_budget_cents` (optional, at most 10000) caps a job's budget.
  - `allowed_license_labels` (optional) lists the licence labels the destination may receive.
    Without it only `unlabelled` inputs qualify, and `unknown-lineage` can never be listed.
- **Submit:** after the policy's `check` passes, the submit also applies the inputs' effective
  licence labels (ADR 0040) through the policy's `license_labels_refusal`. So data labelled
  e.g. `non-commercial` never reaches a destination not explicitly allowed it.
- **Recorded admission:** the `review_job.submitted` event records `destination_id`,
  `data_classes`, `license_labels` and the policy's canonical `policy_digest`.
- **Refusals** use the existing ADR 0084 reason codes.
- **Runners** load the same file with
  `--policy tessera_control.review_destination_policy:EnvironmentReviewDestinationPolicy`.
- The file is read at start; changing it needs a restart.

### Research-kind runner

`python -m tessera_control.research_review_runner` claims queued `plan_review`,
`dataset_scout`, `technique_scout` and `pre_gate_review` jobs, never `code_review`.

- **Credential:** it acts as an external-agent credential enrolled with exactly `review:read`,
  `review:findings` and `model_spend:record`, and is refused at startup otherwise.
- **Seams:** it uses the ADR 0084 seams (`mark_not_started`, `mark_running`,
  `record_findings`, `mark_failed`), so separation of duties applies unchanged.
- **Launch checks:** before claiming, it re-checks:
  - the operator review policy;
  - the job's tier approval (below);
  - that the job's `prompt_digest` is the sha256 of an operator-pinned prompt file
    (`prompt_not_pinned`);
  - `model_runtime` egress (`egress_not_permitted`);
  - a destination equal to the configured client's provider (`destination_not_supported`).

  A refusal ends the job `not_started`.
- **Spend:** each job makes at most one model call, charged to the ADR 0090 model-spend ledger.
  - **Reserve:** it reserves `budget_cents` as the worst case before the call (run id = job id,
    seq 1, plus the job's bound tier grant when its approval names one), so the per-call and
    daily caps refuse it fail closed.
  - **Release or settle:** it releases the reservation when nothing was sent, and settles the
    actual cost with the receipt's sha256.
  - **Uncertain call:** never retried. The job fails `model_call_uncertain` and the
    reservation stays open for a person to resolve.
- **Crash safety:** a private, flock-held, append-only journal (`claiming`, `reserved`,
  `model_call_started`, `model_result`, `done`) with deterministic idempotency keys means a
  crash and replay neither call nor spend twice.
- **Receipt:** it lists job, kind, destination, tier, data classes, input digests, prompt
  digest, message digest, configured and reported model, tokens, spend, timestamps and outcome.
  It is written 0600 and bound as the job's `receipt_digest` and the ledger's `receipt_sha256`.
- **Findings** stay advisory, untrusted text.
- **Model client:** injected. The CLI uses the Anthropic Messages client with an ADR 0082
  profile and the `anthropic-api-key` credential reference, which the runner never opens.
  Model ids come only from the profile.
- **Unit:** an example systemd unit (manual start, admission marker, kill switch, not in
  `tessera.target`) is `deploy/systemd/tessera-research-review-runner.service`.

### Review-job tiers and per-job tier approval

- **Configuration:** the tiers a job may name, their order and the approval threshold are
  operator configuration, not code: `tessera serve --review-tiers FILE`, env
  `TESSERA_REVIEW_TIERS`, JSON `{"tiers": [...], "approval_from": tier|null, "models": {tier:
  [model ids]}}`. The default reproduces the earlier behaviour: `haiku`, `sonnet`, `opus`,
  `fable`, with only `fable` needing approval.
- **Tier names:** a job names any well-formed tier. One that is not configured is refused with
  `tier_not_configured` (422). The database only checks the tier's shape: schema 89, a same-id
  rebuild that keeps every existing row.
- **Approval:** a job at or above the threshold is submitted `awaiting_approval`. A person
  (never an agent, worker or the submitter's credential) approves it only by naming a
  reasoning-kind agent authority grant (ADR 0142) of the job's project. The grant must meet
  three conditions:
  - its `provider_id` is the job's destination;
  - its model is bound to the job's tier, by the tier's configured model ids, or else by the
    tier name being a token of the model id;
  - its remaining budget covers the job's cap.

  Any mismatch fails closed with a stable `tier_grant_*` reason and writes nothing.
- **One transaction:** the approval, the immutable grant binding (`review_job_tier_grants`),
  the state change, the `review_job.tier_approved` event (grant, provider, model) and the
  idempotency record are one transaction, tenant- and project-scoped.
- **Paying for the call:** the job record exposes `approval.grant_id`, which the runner passes
  to the model-spend reservation so the grant pays for the call. Only a grant's subject may
  spend it, so the runner refuses at launch (`tier_grant_scope_mismatch`) a job whose bound
  grant was issued to another credential.
- **Re-check at start:** the binding is re-checked when the job starts (the runner's launch
  check, `mark_running` and the code review claim). A revoked, expired, changed or exhausted
  grant, or an approval recorded before grants were required, leaves the job `not_started`
  with that reason.
- An approval is a recorded decision, not evidence that a model ran.

## Consequences

- An operator can run plan reviews and scouting jobs against an enrolled destination without
  code changes. Every admission, approval and spend is receipted.
- Private and licence-restricted inputs reach only destinations that list them explicitly.
- Tier names and approval thresholds are deployment choices. Higher tiers are paid for by a
  named, re-checked grant.

## Limitations

- The runner runs on the control host as the control service's user (it reads snapshot bytes
  from the state database, like the code review runner), for one project per runner.
- Research jobs have no lease or reaper: if a runner dies permanently, its jobs stay `running`
  until it restarts with the same journal or an operator marks them failed.
- Only the Anthropic client exists. Other destinations need a client class, and a destination
  with no Tessera-enforced runtime hosts cannot be enrolled (fail closed). The real Messages
  client was exercised only with a fake.
- The MCP tools and agent-side helpers still list the four default tiers, and the MCP approve
  tool cannot name a grant yet. Until it can, approvals through MCP are refused with
  `tier_grant_required`.
- The tiers file is read only by serve; a separate runner process uses the recorded binding,
  which is sufficient for its start gate.
- The code review runner still names its own destination, separately from this policy.
- The policy and tiers files are read at start only, with no reload or audit event.
