# Directing campaigns through `tessera_mcp`

An enrolled external agent can submit a campaign, find the snapshots it may cite, read
what ran and read what it cost, without a human relaying data. Authority is unchanged:
a human still approves the exact spec digest, stages and releases snapshots, and
classifies data. Nothing here approves, spends, releases or stages.

## Tools and scopes

A tool is registered only when the credential's live scopes (`tessera_status`) include
its action; `--campaign-tools` / `--no-campaign-tools` force the whole family on or off
(forced on registers every tool and the control API still enforces each scope).

| Tool | Route | Action |
| --- | --- | --- |
| `tessera_submit_campaign` (v1 or v2 spec) | `POST /projects/{p}/campaigns` | `campaign:submit` |
| `tessera_get_campaign` | `GET /campaigns/{id}` | `campaign:read` |
| `tessera_list_campaigns` | `GET /projects/{p}/campaigns` | `campaign:read` |
| `tessera_get_campaign_receipt` | `GET /campaigns/{id}/receipt` (agent view: approver and pre-approval identifiers removed, `view: agent_redacted`) | `campaign:read` |
| `tessera_cancel_campaign` | `POST /campaigns/{id}/cancel` | `campaign:cancel` |
| `tessera_list_snapshots` | `GET /projects/{p}/agent-snapshots` | `snapshot:read` |
| `tessera_get_snapshot` | `GET /projects/{p}/agent-snapshots/{id}` | `snapshot:read` |
| `tessera_campaign_outputs` | `GET /campaigns/{id}/outputs` | `artifact:read` |
| `tessera_campaign_logs` | `GET /campaigns/{id}/logs` | `campaign:logs` |
| `tessera_campaign_stage_watch` | `GET /campaigns/{id}/stage-watch` | `campaign:logs` |
| `tessera_campaign_stage_status` | `GET /campaigns/{id}/stage-status` | `campaign:logs` |
| `tessera_campaign_budget` | `GET /campaigns/{id}/budget` | `budget:read` |
| `tessera_project_spend` | `GET /projects/{id}/agent-spend` | `budget:read` |
| `tessera_campaign_retries` | `GET /campaigns/{id}/retries` | `campaign:read` |
| `tessera_campaign_standing_status` | `GET /projects/{p}/agent-standing-status` | `campaign:read` |
| `tessera_campaign_gate_decisions` | `GET /campaigns/{id}/gate-decisions` | `campaign:read` |
| `tessera_campaign_holdout` | `GET /campaigns/{id}/holdout` | `campaign:read` |
| `tessera_campaign_holdout_claims` | `GET /projects/{p}/holdout-claims[?partition_digest=]` | `campaign:read` |
| `tessera_validate_campaign_spec` | `POST /projects/{p}/campaign-specs/validate` | `campaign:submit` |
| `tessera_render_campaign_spec` | `POST /projects/{p}/campaign-specs/validate` (after offline rendering) | `campaign:submit` |
| `tessera_submit_rendered_campaign` | the submit route (after offline rendering) | `campaign:submit` |
| `tessera_campaign_wave_receipts` | `GET /campaigns/{id}`, `/receipt` and `/gate-decisions` per id (max 20) | `campaign:read` |
| `tessera_campaign_grants` | `GET /campaigns/{id}/grants` | `campaign:logs` |
| `tessera_reduce_campaign_budget` | `POST /campaigns/{id}/budget-reduction` | `campaign:cancel` |
| `tessera_revoke_campaign_release` | `POST /campaigns/{id}/releases/{release}/revoke` | `campaign:cancel` |
| `tessera_pause_campaign` | `POST /campaigns/{id}/pause` | `campaign:cancel` |
| `tessera_resume_campaign` | `POST /campaigns/{id}/resume` | `campaign:cancel` |
| `tessera_limit_campaign_concurrency` | `POST /campaigns/{id}/concurrency-limit` | `campaign:cancel` |
| `tessera_cancel_campaign_stage` | `POST /campaigns/{id}/stages/{stage}/cancel` | `campaign:cancel` |
| `tessera_approve_campaign` (only with `--approval-tool`) | `GET /campaigns/{id}`, `POST /campaigns/{id}/approve` | a person's session credential; no enrollable scope grants it |
| `tessera_record_gate_decision` (only with `--approval-tool`) | `GET /campaigns/{id}`, `POST /campaigns/{id}/gate-decision` | a person's session credential; no enrollable scope grants it |
| `tessera_rerun_campaign` | `POST /campaigns/{id}/rerun` | `campaign:submit` + `campaign:read` + `artifact:read` (all three needed to register it) |
| `tessera_prepare_stage_retry` | `GET /campaigns/{id}` and `/outputs` (offline build) | `campaign:read` + `artifact:read` (both needed to register it) |

The grants, budget-reduction, release-revocation, pause, resume, concurrency and stage-cancel tools, and the retry tool, are the steering tools of [ADR 0055](decisions/0055-mcp-steering-tools.md) and [ADR 0061](decisions/0061-campaign-pause-concurrency-stage-cancel.md) (pause and resume, a lower-only concurrency limit, single-stage cancel); the audit is in
[mcp-steering-gaps.md](mcp-steering-gaps.md). Validation is a dry run; the budget reduction and the
release revocation only reduce authority and apply to the submitter's own campaigns; the retry tool
builds a new spec and submits nothing. No tool raises a cap, reconciles a grant or releases data, and
none approves unless an operator starts the separate `--approval-tool` server described below.

`tessera_campaign_standing_status` shows why a scheduled or follow-on start did or did not happen
(status, window, limits, started campaigns, pending/abandoned triggers) so an agent need not
guess. It omits the template spec and every principal id; standing approvals are still
created, shown in full and revoked only by a human. Unlock evaluations are in the receipt. `tessera_campaign_gate_decisions` returns each decision and its note but not who decided or the credential, matching the standing-status view.

`budget:read` alone (a results-only credential) never turns the campaign tools on; it
enables `tessera_campaign_budget` and `tessera_project_spend` only beside another campaign scope. Experiment and
program budget roll-ups remain in `tessera_experiment_results` / `tessera_program_results`.

Every route requires the real agent bearer and `X-Agent-Destination` matching the
enrollment, stays inside the credential's project and organization, and returns explicit
column allowlists.

## Specs

`tessera_submit_campaign` accepts v1 and v2 specs. v2 adds providers, runtimes, typed
outputs, up to 64 stages, stage chaining and `kind: artifact` inputs pinning another
campaign's output. `receipt_output` inputs resolve at submit from a succeeded campaign's receipt;
pass `predecessors` (name to campaign id) to `tessera_submit_campaign` to name them
([ADR 0080](decisions/0080-campaign-inputs-from-a-predecessor-receipt.md)). The tool validates with the control plane's own parser, so an invalid
spec never leaves the adapter; provider-registry checks still run at submit and approve.
Find snapshot ids and digests with `tessera_list_snapshots`, the files a snapshot holds with `tessera_get_snapshot`, and a source campaign's output
sha256 with `tessera_campaign_outputs`.

## Scope semantics

- `tessera_get_campaign`, `tessera_list_campaigns` and the receipt read **any campaign in
  the credential's project**, not only the caller's own. Agents in one project can
  see each other's campaigns. Cancel is submitter-only. Cross-project and cross-tenant
  reads return 404.
- `tessera_list_snapshots` lists metadata (id, digest, size, file count, recorded data
  class). It never returns host paths. It grants no bytes. Staging stays a human step.
- `tessera_get_snapshot` lists one snapshot's files (relative path, size, sha256, classification,
  at most 1000, with a `truncated` flag), its data class and licence labels
  ([ADR 0126](decisions/0126-mcp-snapshot-file-list.md)). It never returns the manifest root,
  provenance, exclusions or bytes. File names and digests are visible to any credential with
  `snapshot:read`.
- `tessera_campaign_outputs` lists recorded output metadata only (name, sha256, size, class).
- `tessera_campaign_logs` returns stage status and attempt diagnostics (status, exit code,
  failure class, bounded detail). Tessera retains no raw stdout/stderr.
- `tessera_campaign_stage_status` (read-only) returns, per stage, the chosen provider and profile,
  the recorded placement (candidates and skip reason codes), what the stage waited on with counts
  (paused, concurrency, exposure, limits, slot, release, placement), the latest attempt (phase,
  failure class, grant and spend-limit state, executor lease held or expired and its age, executor
  heartbeat age with a stale flag at the 60 s worker heartbeat maximum age) and failure counts by
  class, and `rejections` (the latest control refusal of a worker step, from any provider, for lease acquire, renewal, package and observation only: attempt, step, code, time
  and age, plus counts by code; see the Salad executor in [campaigns](campaigns.md)). Codes, ages and counts only: no failure detail or log text, no principal, credential or
  worker ids, no hosts or addresses; a free-text waiting reason is returned as `detail_withheld`.
  A caller in another organization gets the same 404 as for a missing campaign.
- `budget:read` also unlocks the HTTP budget read of any campaign in the credential's
  project, for credentials enrolled for results roll-ups as well; MCP registration gating
  does not limit direct HTTP. The figures are the campaign ledger (cap, held, settled,
  remaining).
- Outputs and attempts are capped per read; a `truncated` flag marks a capped list.
- Receipts and statuses are recorded platform state, not scientific verdicts. Never claim a
  campaign ran without reading its receipt.

## Storage retention scopes

An agent enrolled with `retention:read` gets `tessera_storage_retention_status`
(`GET /storage-retention/status`) and `tessera_storage_retention_plan`
(`POST /storage-retention/plan`, a dry run that records a receipt and deletes nothing). One enrolled
with `retention:apply` also gets `tessera_storage_retention_apply` (`POST /storage-retention/apply`
with a `plan_digest`), which deletes only what that still-current plan listed and only under the
operator's policy file; a stale digest or missing policy is a 409 and a refused receipt. Agents
never set policy or paths, workers are refused, and responses carry no absolute paths.
`--storage-retention-tool` still registers the status tool for a person's credential.

## Harness catalog warning

The pinned OpenCode harness catalog (`PERMITTED_TOOLS`, 22 tools) does not list any campaign,
snapshot or provider-trust tool. Enrolling a harness credential with `campaign:*`,
`snapshot:read`, `artifact:read`, `campaign:logs` or `provider_trust:read` registers extra
tools and the harness blocks with `catalog_inventory_changed`. Use a separate credential.

Enrollment: `scripts/enroll_local_mcp.py --allow-campaigns` (submit/read/cancel) and
`--allow-campaign-evidence` (`snapshot:read`, `artifact:read`, `campaign:logs`).

`tessera_approve_campaign` ([ADR 0065](decisions/0065-operator-enabled-mcp-approval-tool.md)) is off unless
the operator starts a separate server with `--approval-tool` and a person's session credential file
(never the submitting agent's). That flag alone gives an approve-only server with no submit or cancel
tool; add `--campaign-tools` to expose those as well. It names the exact spec digest,
reads the current revision, uses a fresh idempotency key per call unless one is supplied, and returns
the approval record. The control API makes every decision: an agent credential gets HTTP 403, a stale
revision or digest, an unapproved state, a reused key or an exceeded program/campaign cap gets 409,
and nothing is written. Unlock conditions and pre-approvals are not exposed.

The same server registers `tessera_record_gate_decision` ([ADR 0124](decisions/0124-mcp-operator-loop-tools.md)):
a gate `go` or `no_go` with a note on a finished campaign, a thin client of the gate-decision route. The
control API refuses any credential that is not a person's. `tessera_render_campaign_spec`,
`tessera_submit_rendered_campaign` and `tessera_campaign_wave_receipts` are described in the same ADR:
rendering substitutes `${NAME}` values into string leaves of a template and then follows the ordinary
validate and submit paths, and the wave read returns up to 20 campaigns' receipts and gate decisions.
