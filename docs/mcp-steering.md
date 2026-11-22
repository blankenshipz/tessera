# Steering a campaign over MCP

This is the operator runbook for letting an agent drive a multi-stage campaign through
`tessera_mcp` alone, and the list of places where a person or the operator CLI is still needed.
The tool-by-tool audit is in [mcp-steering-gaps.md](mcp-steering-gaps.md); the contract is
[ADR 0055](decisions/0055-mcp-steering-tools.md). The end-to-end proof is
`tests/test_mcp_steering_e2e.py` (real control app and runner in process, fake worker, no
network, no Docker).

## Runbook

One-time operator setup (a person, once per project):

1. Enroll the agent with the campaign scopes: `campaign:submit`, `campaign:read`,
   `campaign:logs`, `campaign:cancel`, `artifact:read`, `snapshot:read`, `budget:read`
   (`scripts/enroll_local_mcp.py`). A tool is registered only for a scope the credential holds.
2. Set project limits (concurrency, program budget) and, if a script may stand in for a person
   at a gate, start the service with `--accept-automation-gate-go` (ADR 0051).
3. If the campaign has a gate, create the standing approval that carries it (below).

Then the agent, over MCP only:

1. `tessera_validate_campaign_spec`: dry run; nothing is stored. Fix refusals.
2. `tessera_submit_campaign` with an idempotency key. A person approves the returned spec
   digest. This is the one authority step per spec.
3. Wait. Tessera's own runner advances stages (grants, data releases, measurements, custody) with
   no operator call. Read progress with `tessera_get_campaign`, `tessera_campaign_logs`,
   `tessera_campaign_grants` and `tessera_campaign_budget`.
4. When the campaign finishes read `tessera_get_campaign_receipt` and
   `tessera_campaign_outputs`. A gate (standing approval with an unlock requirement) reads that
   receipt: `tessera_campaign_standing_status` shows the refusal reason while it holds
   (`no_human_go`, `measurement_condition_false`, ...) and `tessera_campaign_gate_decisions` shows
   the recorded go.
5. A stage failed: `tessera_prepare_stage_retry` builds a new spec (reads only), pinning
   succeeded outputs by digest. Validate it, submit it, and a person approves the new digest.
6. Spend control: `tessera_reduce_campaign_budget` lowers the cap (never below held plus
   settled, never up). `tessera_cancel_campaign` stops further spend; once it is cancelling or
   finished, `tessera_revoke_campaign_release` withdraws a data release one of its attempts used.
   Each of these leaves an event (`campaign.budget_reduced`, `campaign.cancel_requested`,
   `workspace.snapshot_release_revoked`) and the cancel ends in a terminal receipt.

## Where a person or operator CLI is still required

Found while writing the end-to-end test. "By design" items protect an authority boundary and
are not planned to change; the rest are gaps.

| # | Step | Needed today | Status |
| --- | --- | --- | --- |
| 1 | Approve a submitted (or retry) spec digest | a person's session | by design |
| 2 | Create the standing approval that carries a gate (spec, caps, `unlock`) | a person's session; no MCP tool, and an agent cannot submit a gated campaign | by design; see gap 10 |
| 3 | Record a gate `go` | a person's session, or the owner credential with an automation label where the operator started `tessera serve --accept-automation-gate-go`. No agent or MCP path | by design for the go; the opt-in is operator config |
| 4 | A gate that needs no go (a pre-registered paired-delta `bound_outcome`) | a person creates the split lock and the standing approval | gap 11 |
| 5 | Raise a cap, raise concurrency, extend a deadline | new approval / operator limits | by design |
| 6 | Pause and resume, cancel one stage, per-campaign concurrency | not available | gap (needs runner changes) |
| 7 | Reconcile a grant that is not a confirmed exit; restore a revoked release | a person with evidence | by design |
| 8 | Infrastructure faults other than a worker-reported exit code (lost worker lease, hung container) | the test injects an exit code 137 through the fake worker only; recovery of a lost attempt is the platform's auto-reconcile (ADR 0053), not exercised here | gap, not proven |
| 9 | Enrollment, token renewal and the runner/worker processes | operator CLI | by design |
| 10 | A gate between stages of one campaign | does not exist: a gate is an unlock between campaigns (stages 1-2 are one campaign, stage 3 the next). The retry of a gated campaign is a plain submission and a person approves its digest; the unlock is not re-applied | design note |
| 11 | Gate on a measurement without a go | a measurement only narrows a go or a bound outcome (ADR 0051), so some human or opted-in script act always precedes a gated start | by design |

No tool approves a spec, records a go, raises a cap, reconciles a grant or releases data; the
end-to-end test marks each human act `HUMAN` and asserts the approval count does not change
while the runner advances the stages.
