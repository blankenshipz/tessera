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
| Adjust concurrency | none | not exposed: concurrency is the operator's project limit (`execution_project_limits`), set by a human; a per-campaign limit would be a schema change | human / operator |
| Cancel or stop | `tessera_cancel_campaign` (reduce-only) | covered; no change | `campaign:cancel`, submitter only |
| Retry a failed stage | none | `tessera_prepare_stage_retry`: builds a NEW v2 spec for the stages that did not succeed (and anything that consumed them), pinning succeeded stages' outputs by digest as `kind: artifact` inputs. It reads only and submits nothing; the agent validates and submits it and a human approves its digest. A pin resolves only from a succeeded attempt in a terminal receipt whose output manifest is retained and custody-released: the runner does that when the stage succeeds, `tessera_campaign_outputs` shows it as `custody_released`, and the tool refuses an output that is not (a human revoking custody makes the pin fail) | registered with `campaign:read` + `artifact:read`; `campaign:submit` to submit |
| Revoke a stale release | none (human route only) | `tessera_revoke_campaign_release` -> `POST /campaigns/{id}/releases/{release}/revoke` (annotated destructive): only after the campaign finished (or is cancelling), only a release one of its attempts used, only the submitter. The runner's own cleanup reads active releases each tick, so a release the agent revoked first is simply no longer listed; a revoke the runner sent from an older read gets a stale-revision refusal that does not stall the cancel (tested) | `campaign:cancel` |
| Reconcile a grant | none (human, evidence-checked) | not exposed. A confirmed exit is resolved by the platform itself (ADR 0053); everything else needs a person's evidence. `tessera_campaign_grants` shows what is unresolved | human |
| Retrieve outcomes: receipts, artifact digests, gate results | `tessera_get_campaign_receipt`, `tessera_campaign_outputs`, `tessera_campaign_gate_decisions` (and the experiment and program roll-ups) | covered; no change | `campaign:read`, `artifact:read` |

## Open

- A lower-only per-campaign concurrency limit and cancelling a single stage were considered and
  not added: the first needs a new campaign column, a migration and a runner admission check, the
  second a new stage state and unwind path in the runner. Neither is small or purely a tool
  addition; each needs its own contract change and tests.
- Pause and resume, and extending a deadline (human), are not exposed.
- `tessera_prepare_stage_retry` refuses v1 specs, an input that reads several outputs of a stage
  it would pin (the mount layout is for a person to review), and an output with no single
  recorded digest. It cannot retry across a campaign that has no terminal receipt, because the
  platform resolves artifact pins from that receipt.
- A campaign with no active approval still skips the confirmed-exit resolution (ADR 0053 item 9).
