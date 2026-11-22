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
| `tessera_get_campaign_receipt` | `GET /campaigns/{id}/receipt` | `campaign:read` |
| `tessera_cancel_campaign` | `POST /campaigns/{id}/cancel` | `campaign:cancel` |
| `tessera_list_snapshots` | `GET /projects/{p}/agent-snapshots` | `snapshot:read` |
| `tessera_campaign_outputs` | `GET /campaigns/{id}/outputs` | `artifact:read` |
| `tessera_campaign_logs` | `GET /campaigns/{id}/logs` | `campaign:logs` |
| `tessera_campaign_budget` | `GET /campaigns/{id}/budget` | `budget:read` |
| `tessera_campaign_retries` | `GET /campaigns/{id}/retries` | `campaign:read` |
| `tessera_campaign_standing_status` | `GET /projects/{p}/agent-standing-status` | `campaign:read` |
| `tessera_campaign_gate_decisions` | `GET /campaigns/{id}/gate-decisions` | `campaign:read` |
| `tessera_validate_campaign_spec` | `POST /projects/{p}/campaign-specs/validate` | `campaign:submit` |
| `tessera_campaign_grants` | `GET /campaigns/{id}/grants` | `campaign:logs` |
| `tessera_reduce_campaign_budget` | `POST /campaigns/{id}/budget-reduction` | `campaign:cancel` |
| `tessera_revoke_campaign_release` | `POST /campaigns/{id}/releases/{release}/revoke` | `campaign:cancel` |
| `tessera_pause_campaign` | `POST /campaigns/{id}/pause` | `campaign:cancel` |
| `tessera_resume_campaign` | `POST /campaigns/{id}/resume` | `campaign:cancel` |
| `tessera_limit_campaign_concurrency` | `POST /campaigns/{id}/concurrency-limit` | `campaign:cancel` |
| `tessera_cancel_campaign_stage` | `POST /campaigns/{id}/stages/{stage}/cancel` | `campaign:cancel` |
| `tessera_approve_campaign` (only with `--approval-tool`) | `GET /campaigns/{id}`, `POST /campaigns/{id}/approve` | a person's session credential; no enrollable scope grants it |
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
enables `tessera_campaign_budget` only beside another campaign scope. Experiment and
program budget roll-ups remain in `tessera_experiment_results` / `tessera_program_results`.

Every route requires the real agent bearer and `X-Agent-Destination` matching the
enrollment, stays inside the credential's project and organization, and returns explicit
column allowlists.

## Specs

`tessera_submit_campaign` accepts v1 and v2 specs. v2 adds providers, runtimes, typed
outputs, up to 64 stages, stage chaining and `kind: artifact` inputs pinning another
campaign's output. The tool validates with the control plane's own parser, so an invalid
spec never leaves the adapter; provider-registry checks still run at submit and approve.
Find snapshot ids and digests with `tessera_list_snapshots` and a source campaign's output
sha256 with `tessera_campaign_outputs`.

## Scope semantics

- `tessera_get_campaign`, `tessera_list_campaigns` and the receipt read **any campaign in
  the credential's project**, not only the caller's own. Agents in one project can
  see each other's campaigns. Cancel is submitter-only. Cross-project and cross-tenant
  reads return 404.
- `tessera_list_snapshots` lists metadata (id, digest, size, file count, recorded data
  class). It never returns host paths. It grants no bytes. Staging stays a human step.
- `tessera_campaign_outputs` lists recorded output metadata only (name, sha256, size, class).
- `tessera_campaign_logs` returns stage status and attempt diagnostics (status, exit code,
  failure class, bounded detail). Tessera retains no raw stdout/stderr.
- `budget:read` also unlocks the HTTP budget read of any campaign in the credential's
  project, for credentials enrolled for results roll-ups as well; MCP registration gating
  does not limit direct HTTP. The figures are the campaign ledger (cap, held, settled,
  remaining).
- Outputs and attempts are capped per read; a `truncated` flag marks a capped list.
- Receipts and statuses are recorded platform state, not scientific verdicts. Never claim a
  campaign ran without reading its receipt.

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
