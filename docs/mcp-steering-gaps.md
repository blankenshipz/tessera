# MCP steering: audit and gap list

Goal: a client steers experiments through `tessera_mcp`, with no session calling the control API
by hand. This audit compares the tools at the time of ADR 0055 with the control API and the
README, and records what was added, what stays human on purpose, and what is still open.
Authority is unchanged throughout: a tool is registered only for a live enrolled scope, the control
API re-checks that scope, and no tool approves a spec, raises a cap, releases or stages data,
reconciles a grant or sends data to a destination.

## History since ADR 0055

A frozen record of how the gaps found at ADR 0055 were closed (the "Before" column is the state
at ADR 0055). It is not kept current: the coverage table further down is the single current,
per-action view, so update that one.

| Steering need | Before | Now | Authority |
| --- | --- | --- | --- |
| Submit a whole multi-stage campaign from a spec | `tessera_submit_campaign` (v1/v2) | unchanged | `campaign:submit`; a human approves the digest |
| Validate a spec without submitting | client-side parse only (provider registry, snapshots, trust and data classes were checked only by a real submit) | `tessera_validate_campaign_spec` -> `POST /projects/{p}/campaign-specs/validate`: the same parse and reference checks, nothing stored | `campaign:submit` |
| List and inspect campaigns with stage and attempt status and failure detail | `tessera_list_campaigns` (summaries), `tessera_get_campaign` (stages, attempts, intents), `tessera_campaign_logs` (exit code, failure class and bounded detail) | covered; no change | `campaign:read`, `campaign:logs` |
| Read logs | `tessera_campaign_logs` (Tessera keeps no raw stdout/stderr) | covered; no change | `campaign:logs` |
| See why a slot or release is still held | none | `tessera_campaign_grants` -> `GET /campaigns/{id}/grants` (grant status, hold, settlement, expiry, revision; release id, status, revision) | `campaign:logs` |
| Adjust budget | none (the cap is set at submit; the operator sets project limits) | `tessera_reduce_campaign_budget` -> `POST /campaigns/{id}/budget-reduction`: lowers an approved, unfinished campaign's cap; never raises it; never below held plus settled | `campaign:cancel`, submitter only |
| Adjust concurrency | none | `tessera_limit_campaign_concurrency` -> `POST /campaigns/{id}/concurrency-limit` (ADR 0061): a lower-only per-campaign cap on stages running at once. The project limit (`execution_project_limits`) stays the operator's and applies on top | `campaign:cancel`, submitter only |
| Pause and resume | none | `tessera_pause_campaign`, `tessera_resume_campaign` (ADR 0061): a pause stops new stage reservations and leaves running attempts alone; a person's pause is lifted only by a person | `campaign:cancel`, submitter only |
| Cancel or stop | `tessera_cancel_campaign` (reduce-only) | covered; plus `tessera_cancel_campaign_stage` (ADR 0061) for one unfinished stage; dependents are skipped and the campaign ends `stopped` | `campaign:cancel`, submitter only |
| Retry a failed stage | none | `tessera_prepare_stage_retry`: builds a NEW v2 spec for the stages that did not succeed (and anything that consumed them), pinning succeeded stages' outputs by digest as `kind: artifact` inputs. It reads only and submits nothing; the agent validates and submits it and a human approves its digest. A pin resolves only from a succeeded attempt in a terminal receipt whose output manifest is retained and custody-released: the runner does that when the stage succeeds, `tessera_campaign_outputs` shows it as `custody_released`, and the tool refuses an output that is not (a human revoking custody makes the pin fail) | registered with `campaign:read` + `artifact:read`; `campaign:submit` to submit |
| Re-run a failed campaign | `tessera_prepare_stage_retry` plus a hand-made submit; no record linking the new campaign to the old | `tessera_rerun_campaign` -> `POST /campaigns/{id}/rerun` ([ADR 0069](decisions/0069-rerun-a-finished-campaign.md)): one call submits a NEW campaign of a failed, stopped or cancelled campaign's failed stages (succeeded outputs reused by digest through custody and lineage, v2) or its identical spec. It is an ordinary submission: nothing is approved or granted, the new digest is approved by a person, the cap may not exceed the first campaign's cap less what the whole re-run tree has committed, with `rerun_tree` on `tessera_get_campaign` ([ADR 0074](decisions/0074-rerun-spend-is-bounded-across-the-chain.md)), every submit-time check runs, and the link (source, stages, reused outputs) is on the new campaign's receipt and both campaigns' views | `campaign:submit` + `campaign:read` (+ `artifact:read` to reuse outputs) |
| Revoke a stale release | none (human route only) | `tessera_revoke_campaign_release` -> `POST /campaigns/{id}/releases/{release}/revoke` (annotated destructive): only after the campaign finished (or is cancelling), only a release one of its attempts used, only the submitter. The runner's own cleanup reads active releases each tick, so a release the agent revoked first is simply no longer listed; a revoke the runner sent from an older read gets a stale-revision refusal that does not stall the cancel (tested) | `campaign:cancel` |
| Reconcile a grant | none (human, evidence-checked) | not exposed. A confirmed exit is resolved by the platform itself (ADR 0053); everything else needs a person's evidence. `tessera_campaign_grants` shows what is unresolved | human |
| Retrieve outcomes: receipts, artifact digests, gate results | `tessera_get_campaign_receipt`, `tessera_campaign_outputs`, `tessera_campaign_gate_decisions` (and the experiment and program roll-ups) | covered; no change | `campaign:read`, `artifact:read` |

## Open

What a client with every tool enrolled still cannot do:

- No tool opens, reopens or releases a holdout claim, by design: the runner opens it, once per scope.
- Approve with an unlock condition or pre-approvals, record a gate go or no-go, or create a
  standing approval (human authority). An approve-only server cannot read the campaign it
  approves unless `--campaign-tools` is also set; the digest comes from whoever submitted.
- Raise a campaign cap, a concurrency limit or a deadline (a long pause counts against the
  deadline); reconcile a grant that is not a confirmed exit; stage or release data (human).
- Read artifact bytes: outputs are metadata and digests only; bytes need a human-released snapshot.
- Pause or limit one stage rather than the campaign (only single-stage cancel exists).

Limits of what exists:

- `tessera_rerun_campaign` in `failed_stages` mode inherits the retry builder's refusals below
  (use `identical` mode for a v1 spec); it does not extend a deadline or raise a cap.
- `tessera_prepare_stage_retry` refuses v1 specs, an input that reads several outputs of a stage
  it would pin (the mount layout is for a person to review), and an output with no single
  recorded digest. It cannot retry across a campaign that has no terminal receipt, because the
  platform resolves artifact pins from that receipt.
- A campaign with no active approval still skips the confirmed-exit resolution (ADR 0053 item 9).

## Coverage against the steering list

What a client needs to steer experiments without a person relaying each call, and the tool that
covers it. "Pending" marks a tool that is implemented on a separate change and not
duplicated here; this table is rechecked when those land.

| Steering action | Tool | State |
| --- | --- | --- |
| Submit a whole multi-stage campaign | `tessera_submit_campaign`, `tessera_validate_campaign_spec` | present (v1 and v2 specs; a person approves the digest) |
| Inspect campaign, stage, attempt | `tessera_get_campaign`, `tessera_list_campaigns`, `tessera_campaign_logs`, `tessera_campaign_retries` | present |
| Inspect placement | `tessera_get_campaign` (`placement`: candidates, steering, recorded placements, why a stage waits), `tessera_get_campaign_receipt` | present |
| Inspect spend | `tessera_campaign_budget` (one campaign), `tessera_project_spend` (the operator's project limits and what campaigns hold and have settled) | present (`tessera_project_spend` needs `budget:read` and is read-only) |
| Inspect held grants and releases | `tessera_campaign_grants` | present |
| Receipts | `tessera_get_campaign_receipt` | present |
| Adjust budget | `tessera_reduce_campaign_budget` | present (lower-only, never below held plus settled) |
| Adjust concurrency | `tessera_limit_campaign_concurrency` | present (lower-only; ADR 0061) |
| Adjust placement preference | `tessera_steer_campaign_placement` | present (filters approved candidates, never adds one; ADR 0060) |
| Approve within delegation | `tessera_approve_campaign` | present, operator-enabled (`--approval-tool`, which alone is an approve-only server; [ADR 0065](decisions/0065-operator-enabled-mcp-approval-tool.md)), a person's credential only; unlock conditions and pre-approvals not exposed; `tessera_campaign_standing_status` shows standing approvals read-only |
| Pause / resume | `tessera_pause_campaign`, `tessera_resume_campaign` | present (ADR 0061) |
| Stop / cancel | `tessera_cancel_campaign`, `tessera_cancel_campaign_stage`, `tessera_revoke_campaign_release` | present |
| Rerun failed stages | `tessera_prepare_stage_retry` (builds a new spec; submits nothing), `tessera_rerun_campaign` | present: a server-side re-run submits a new campaign for the failed stages ([ADR 0069](decisions/0069-rerun-a-finished-campaign.md)); its cap is bounded by what the whole re-run tree has committed and `tessera_get_campaign` shows `rerun_tree` ([ADR 0074](decisions/0074-rerun-spend-is-bounded-across-the-chain.md)) |
| Claim sealed holdout scoring | `tessera_campaign_holdout`, `tessera_campaign_holdout_claims` | read-only views, see [ADR 0068](decisions/0068-sealed-holdout-evaluation-stages.md). No tool opens, reopens or releases a claim: the runner opens it, once per scope |
| Retrieve outcomes | `tessera_get_campaign_receipt`, `tessera_campaign_outputs`, `tessera_campaign_gate_decisions`, `tessera_experiment_results`, `tessera_program_results` | present; output bytes stay behind a human-released snapshot |

`tessera_project_spend` (`GET /projects/{p}/agent-spend`):
without it a client could read one campaign's cap but not the ceiling it works within. It
returns the project's limits row (currency, monetary limit, max concurrency, held, settled,
remaining) when one is configured and per-status campaign totals, from explicit columns. It is
ledger data, not provider billing, and changes nothing. It is project-scoped on purpose: any
`budget:read` credential for the project sees the project's totals, including campaigns other
agents submitted, and nothing per-principal or per-credential. No ADR: it narrows nothing and grants
nothing, and ADR 0047's destination rules are untouched.

When a tool is still on another change, mark it "(pending)" here: the consistency test fails
while a "(pending)" tool is registered, until the marker is removed.

Still missing on purpose (human authority): approving with an unlock condition or pre-approvals,
recording a gate go or no-go, creating a standing approval, raising a cap or limit, extending a
deadline, reconciling a grant that is not a confirmed exit, staging or releasing data.
