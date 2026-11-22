# Steering a campaign over MCP

This is the operator runbook for letting an agent drive a multi-stage campaign through
`tessera_mcp` alone, and the list of places where a person or the operator CLI is still needed.
The tool-by-tool audit is in [mcp-steering-gaps.md](mcp-steering-gaps.md); the contract is
[ADR 0055](decisions/0055-mcp-steering-tools.md). The end-to-end proof is
`tests/test_mcp_steering_e2e.py` and `tests/test_mcp_steering_e2e_rerun.py` (real control app
and runner in process, fake worker, no network, no Docker). The second drives one local-docker
campaign through submit, inspect, approve, adjust, stop, re-run of the failed stages under the
re-run tree's spend bound (ADR 0074) and outcome and receipt retrieval, with every step a tool
call; approval is `tessera_approve_campaign` on an approve-only server holding a person's own
credential, and an agent credential is refused there.

`tests/test_mcp_research_steering_e2e.py` does the same for the research loop: the loop's own
agent creates review jobs when a plan is admitted and advanced, a client pauses the loop with
`tessera_pause_agent_loop`, the fake model call writes a model-call receipt whose digest lands on
the job, a reviewer identity ingests findings, and a person accepts one and rejects another.

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
   `tessera_pause_campaign` holds new stage reservations while running attempts finish
   (`tessera_resume_campaign` continues; a person's pause only a person lifts),
   `tessera_limit_campaign_concurrency` caps how many stages run at once (lower-only), and
   `tessera_cancel_campaign_stage` unwinds one unfinished stage, skipping what read it, so the
   campaign ends `stopped` with reason `stage_cancelled`. The receipt of a steered campaign has a
   `steering` section. Each of these leaves an event (`campaign.budget_reduced`, `campaign.paused`, `campaign.resumed`,
   `campaign.concurrency_limited`, `campaign.stage_cancel_requested`, `campaign.cancel_requested`,
   `workspace.snapshot_release_revoked`) and the cancel ends in a terminal receipt.

## Smoke test on a running Tessera ($0, dry run by default)

`scripts/mcp_steering_smoke.py` drives one tiny local-docker campaign through the MCP tools
against a running control plane and checks that every step left a record: validate and submit,
read it back, a refused approval by the agent's own credential, a person's approval (the delegated
decision), pause, resume, cancel, then the terminal receipt (it must record the pause and the
approval) and a zero settled budget. It uses two credentials, an enrolled agent credential and a
person's session credential used only for the approval. It makes no call and reads no credential
unless run with `--execute` and the exact confirmation phrase.

    scripts/mcp_steering_smoke.py                       # prints the plan and the exact spec
    scripts/mcp_steering_smoke.py --execute --confirm mcp-steering-smoke-local-docker \
        --control-url https://CONTROL --project-id PROJECT --destination DESTINATION \
        --agent-token-file /abs/agent.token --person-token-file /abs/person.token \
        --image REGISTRY/IMAGE@sha256:DIGEST [--snapshot-id ID] [--ca-file CA] \
        [--receipt-file /abs/new-report.json]

The agent credential needs the campaign scopes (submit, read, cancel), `snapshot:read` and
`budget:read`; the image must be one the local docker provider allows. The campaign is paused
right after approval and cancelled straight after the resume, so at most a local zero-cost
container is reserved. It needs the campaign runner running to reach the terminal receipt and
fails loudly if the campaign is not terminal within `--wait-seconds`. Its own test is
`tests/test_mcp_steering_smoke.py` (an in-process control plane and a fake worker).

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
| 6 | Pause and resume, cancel one stage, a lower-only per-campaign concurrency limit | `tessera_pause_campaign`, `tessera_resume_campaign`, `tessera_cancel_campaign_stage`, `tessera_limit_campaign_concurrency` (ADR 0061). Still human: lifting a pause a person set, raising a limit, extending a deadline | closed |
| 7 | Reconcile a grant that is not a confirmed exit; restore a revoked release | a person with evidence | by design |
| 8 | Infrastructure faults other than a worker-reported exit code | exit code 137 and two lost-worker faults are injected in `tests/test_mcp_steering_e2e_rerun.py`. A container that started with every report lost leaves the attempt `unknown`, the campaign `blocked` and the grant active; the agent sees all three, and its `tessera_cancel_campaign` is recorded (a `campaign.cancel_requested` event) while the campaign stays `blocked` as long as the grant is unresolved; the test does not reconcile the grant, so it does not show what happens after a person does. A launch the worker reports as refused is reconciled by the platform itself (ADR 0046) and the campaign finishes. That auto-reconcile was exercised with a v1 spec. Observation, pinned by `test_a_refused_launch_on_a_v2_stage_stays_blocked_for_a_person`: the same fault on a v2 `local-docker` stage is not auto-reconciled (attempt `unknown`, campaign `blocked`, grant unresolved, no `campaign.attempt_not_started` event). The cause read from `campaign_autoreconcile.assess`: it only applies to an attempt with a zero hold and a zero-exposure grant, and a v2 stage holds its `max_exposure_cents`, so the guard fails before the launch error is read. A v2 stage whose launch is refused therefore still needs a person; this is how the code behaves today, not a stated design rule | partly proven; see the v2 difference |
| 9 | Enrollment, token renewal and the runner/worker processes | operator CLI | by design |
| 10 | A gate between stages of one campaign | does not exist: a gate is an unlock between campaigns (stages 1-2 are one campaign, stage 3 the next). The retry of a gated campaign is a plain submission and a person approves its digest; the unlock is not re-applied | design note |
| 11 | Gate on a measurement without a go | a measurement only narrows a go or a bound outcome (ADR 0051), so some human or opted-in script act always precedes a gated start | by design |
| 12 | Learn why a call was refused (for example how many cents a re-run may still ask for) | the tool returns only "HTTP 409": `tessera_mcp` deliberately never relays a denial body, which could hold protected content. The reason is in the control API's own response (the end-to-end test reads it at the store). The remaining re-run amount is readable from `rerun_tree` in `tessera_get_campaign`. Passing a reason through would be a decision on that rule, not made here | gap, by the adapter's design |
| 13 | A cap reduction does not change a re-run tree's first cap | the tree is bounded by the first spec's cap less what the tree committed (ADR 0074); a reduced cap lowers only what that campaign may spend, and a finished campaign commits what it settled | design note |
| 14 | Approve over MCP | needs a separate approve-only server with a person's credential (`--approval-tool`); covered by the second end-to-end test | by design |

No steering tool approves a spec, records a go, raises a cap, reconciles a grant or releases data (the
operator-enabled approval tool of ADR 0065 runs only on a separate server holding a person's own
credential; the first test does not register it, the second does); the
end-to-end test marks each human act `HUMAN` and asserts the approval count does not change
while the runner advances the stages.
