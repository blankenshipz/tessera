# MCP steering: audit and gap list

Goal: a client steers experiments through `tessera_mcp`, with no session calling the control API
by hand. This audit compares the tools at the time of ADR 0055 with the control API and the
README, and records what was added, what stays human on purpose, and what is still open.
Authority is unchanged throughout: a tool is registered only for a live enrolled scope, the control
API re-checks that scope, and no tool approves a spec, raises a cap, releases or stages data,
reconciles a grant or sends data to a destination.

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
| Revoke a stale release | none (human route only) | `tessera_revoke_campaign_release` -> `POST /campaigns/{id}/releases/{release}/revoke` (annotated destructive): only after the campaign finished (or is cancelling), only a release one of its attempts used, only the submitter. The runner's own cleanup reads active releases each tick, so a release the agent revoked first is simply no longer listed; a revoke the runner sent from an older read gets a stale-revision refusal that does not stall the cancel (tested) | `campaign:cancel` |
| Reconcile a grant | none (human, evidence-checked) | not exposed. A confirmed exit is resolved by the platform itself (ADR 0053); everything else needs a person's evidence. `tessera_campaign_grants` shows what is unresolved | human |
| Retrieve outcomes: receipts, artifact digests, gate results | `tessera_get_campaign_receipt`, `tessera_campaign_outputs`, `tessera_campaign_gate_decisions` (and the experiment and program roll-ups) | covered; no change | `campaign:read`, `artifact:read` |

## Open

- Extending a deadline (human) is not exposed, and a long pause counts against it. Raising a
  concurrency limit or cap is human.
- `tessera_prepare_stage_retry` refuses v1 specs, an input that reads several outputs of a stage
  it would pin (the mount layout is for a person to review), and an output with no single
  recorded digest. It cannot retry across a campaign that has no terminal receipt, because the
  platform resolves artifact pins from that receipt.
- A campaign with no active approval still skips the confirmed-exit resolution (ADR 0053 item 9).

## Audit against the steering list (approval tool)

| Steering action | Tool | State |
| --- | --- | --- |
| Submit | `tessera_submit_campaign`, `tessera_validate_campaign_spec` | present |
| Inspect | `tessera_get_campaign`, `tessera_list_campaigns`, `tessera_campaign_logs`, `tessera_campaign_grants`, `tessera_campaign_budget`, `tessera_campaign_standing_status` | present |
| Approve | `tessera_approve_campaign` | present, operator-enabled (`--approval-tool`, which alone is an approve-only server; [ADR 0065](decisions/0065-operator-enabled-mcp-approval-tool.md)), person's credential only; unlock conditions and pre-approvals not exposed |
| Adjust | `tessera_reduce_campaign_budget` | present (lower-only). A per-campaign concurrency limit is not implemented here; it lands with the separate pause/resume work |
| Pause / resume | none here | not implemented here; it lands with the separate pause/resume work, which has its own ADR |
| Stop / cancel | `tessera_cancel_campaign`, `tessera_revoke_campaign_release` | present; cancelling a single stage is not implemented here; it lands with the separate pause/resume work, which has its own ADR |
| Retrieve outcomes and receipts | `tessera_get_campaign_receipt`, `tessera_campaign_outputs`, `tessera_campaign_gate_decisions` | present |

Still missing on purpose (human authority): approving with an unlock condition or pre-approvals,
recording a gate go or no-go, creating a standing approval, raising a cap or limit, extending a
deadline, reconciling a grant that is not a confirmed exit, staging or releasing data.
