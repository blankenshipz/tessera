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
`tests/test_mcp_steering_e2e_delegated.py` has one agent approve its own campaign with
`tessera_delegated_approve_campaign` under an owner-signed delegation (ADR 0122), run it to a
receipt and see an out-of-rule campaign refused (`outside_delegation`) and left for a person; it
also checks that every acting step left an event or receipt, that no read changed stored state, and
that no response carried an approver identifier.

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
   Choose a snapshot first: `tessera_list_snapshots` for ids and digests, then
   `tessera_get_snapshot` for the files it holds, so a stage can name a file.
2. `tessera_submit_campaign` with an idempotency key. A person approves the returned spec
   digest. This is the one authority step per spec. For a wave of similar campaigns,
   `tessera_render_campaign_spec` renders a template (`${NAME}` placeholders, values into string
   leaves only, every placeholder and every value used) and dry-runs it, and
   `tessera_submit_rendered_campaign` submits the same rendering
   ([ADR 0124](decisions/0124-mcp-operator-loop-tools.md)). It is an ordinary submission; a person
   still approves the digest.
3. Wait. Tessera's own runner advances stages (grants, data releases, measurements, custody) with
   no operator call. Read progress with `tessera_get_campaign`, `tessera_campaign_logs`,
   `tessera_campaign_grants` and `tessera_campaign_budget`. The stage watcher is on by default for
   every stage (ADR 0125); `tessera_campaign_stage_watch(campaign_id, offset=0, limit=20)`
   (`campaign:logs`) reads its recorded reports newest first: per attempt the counts of heartbeat,
   progress, stuck and failed reports, a page of reports and never-admitted retry or resize
   requests. They are unverified worker claims, with closed codes and integer counters only (no
   paths or worker text); they never decide cost or success.
   `tessera_campaign_stage_status`, `tessera_campaign_grants` and `tessera_campaign_budget`.
4. When the campaign finishes read `tessera_get_campaign_receipt` and
   `tessera_campaign_outputs`. A gate (standing approval with an unlock requirement) reads that
   receipt: `tessera_campaign_standing_status` shows the refusal reason while it holds
   (`no_human_go`, `measurement_condition_false`, ...) and `tessera_campaign_gate_decisions` shows
   the recorded go. For a chain of waves, `tessera_campaign_wave_receipts` returns up to 20
   campaigns' status, receipt and gate decisions in one read (a null receipt for one that has not
   finished; one unreadable id does not fail the rest). Both receipt tools return the agent
   view of the receipt (`"view": "agent_redacted"`): no approving principal, pre-approval id or
   digest, or resolved decision-request id; caps, expiry and resolved kinds stay. A person reads
   the stored receipt whole; `receipt_digest` names that stored receipt (see
   [campaigns](campaigns.md#receipt)).
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
7. A stage is not progressing: read `tessera_campaign_stage_status` before cancelling or steering
   placement, instead of reading service logs. It gives codes, ages and counts only (what the stage
   waited on, the latest attempt's phase and failure class, executor lease and heartbeat age with a
   stale flag, and the latest control refusal of a worker step, from any provider, as a short code with counts; only lease
   acquire, renewal, package and observation refusals are recorded, not other worker routes).
   For a Salad stage it also lists the approved profile keys in order, which profile the latest
   attempt ran on, each pre-start switch (from, to, reason code, attempts, `started`/`spent` false)
   and a `profile_not_approved` or `post_start_switch_refused` outcome (`salad_profiles`), so no
   journal grep is needed.

## Smoke test on a running Tessera ($0, dry run by default)

`scripts/mcp_steering_smoke.py` drives one tiny local-docker campaign through the MCP tools
against a running control plane and checks that every step left a record: validate and submit,
read it back, a refused approval by the agent's own credential, a person's approval (the delegated
decision), pause, resume, cancel, then the terminal receipt (it must record the pause and the
approval) and a zero settled budget. It uses two credentials, an enrolled agent credential and a
person's session credential used only for the approval. It makes no call and reads no credential
unless run with `--execute` and the exact confirmation phrase. The refused approval passes only
when control answered HTTP 403 (recorded as `refused_with: http_403`); a dropped connection, a
timeout or any other failure fails the check instead of counting as a refusal.

    scripts/mcp_steering_smoke.py                       # prints the plan and the exact spec
    scripts/mcp_steering_smoke.py --execute --confirm mcp-steering-smoke-local-docker \
        --control-url https://CONTROL --project-id PROJECT --destination DESTINATION \
        --agent-token-file /abs/agent.token --person-token-file /abs/person.token \
        --image REGISTRY/IMAGE@sha256:DIGEST [--snapshot-id ID] [--ca-file CA] \
        [--receipt-file /abs/new-report.json]

### Credentials from a smoke grant (owner-run)

The smoke needs an agent credential enrolled for a destination and a person-side credential for
the approval, and neither can be hand-issued. The owner of the state directory issues a scoped,
short-lived pair instead ([ADR 0120](decisions/0120-scoped-smoke-credentials.md)):

    tessera dev smoke-credentials issue --project-id PROJECT \
        --destination DESTINATION --out-dir /abs/DIR [--ttl-minutes 30]
    scripts/mcp_steering_smoke.py --execute --confirm mcp-steering-smoke-local-docker \
        --control-url https://CONTROL --smoke-credentials-dir /abs/DIR \
        --image REGISTRY/IMAGE@sha256:DIGEST --receipt-file /abs/new-report.json
    tessera dev smoke-credentials revoke --grant-id GRANT_ID

The commands find the control state directory themselves. They take, in order, `--state-dir`, the
`TESSERA_CONTROL_STATE_DIR` environment variable (the shipped control unit sets it for the service only,
so an owner shell exports the same value; keep it equal to the unit's `--state-dir`), or `--plan FILE` (this host's deploy plan, whose `state_dir` is used). If
more than one is given and they name different directories, or none is given, or the directory has no
control database, the command refuses and names which sources it looked at, never a path. There is
deliberately no built-in default path. Run them as the user that owns the state directory.

`--smoke-credentials-dir` reads `smoke-grant.json` and refuses an expired grant, a grant that is
not local-docker only with a zero budget, or files that are not owner-only; it fills in the
project, destination and token files unless you pass them explicitly. The dry run prints the grant
id and expiry (never a token), and the report records `smoke_grant_id`. The campaign spec is $0
(`budget_cap_cents` 0, stage `max_exposure_cents` 0).

`issue` also makes sure the project holds a tiny synthetic public snapshot (fixed bytes, `CC0-1.0`,
adopted by the owner with the `smoke-credentials` automation label; reused if it is already there)
and records its `snapshot_id` and `snapshot_digest` in `smoke-grant.json`. The smoke then uses that
snapshot, after checking the catalog lists it as public with that digest, so no human staging step
is needed; an explicit `--snapshot-id` still wins. See the amendment in ADR 0120.

The agent credential needs the campaign scopes (submit, read, cancel), `snapshot:read` and
`budget:read`; the image must be one the local docker provider allows. The campaign is paused
right after approval and cancelled straight after the resume, so at most a local zero-cost
container is reserved. It needs the campaign runner running to reach the terminal receipt and
fails loudly if the campaign is not terminal within `--wait-seconds`. Its own test is
`tests/test_mcp_steering_smoke.py` (an in-process control plane and a fake worker).

### Steering a running campaign (end-to-end check)

`scripts/mcp_steering_running_check.py` is the companion check where the campaign actually runs:
it submits a two-stage $0 `local-docker` campaign, has the person-side credential approve the digest,
waits until stage one is live, reads the plain-words report (`tessera_campaign_report`), PAUSES while
the stage runs, proves the running stage finishes but no new stage starts while paused, resumes,
lets the campaign succeed, reads any gate decision (`tessera_campaign_gate_decisions`: with no gate
configured it records that there is none) and checks the terminal receipt records the pause and the
approval and that nothing was settled. Every agent act is an MCP tool call; a gate's go is a human
act and has no tool. Like the smoke it is a dry run by default and takes the same owner-issued
credentials:

    scripts/mcp_steering_running_check.py --execute \
        --confirm mcp-steering-running-check-local-docker \
        --smoke-credentials-dir /abs/DIR --control-url https://CONTROL \
        --image REGISTRY/IMAGE@sha256:DIGEST --receipt-file /abs/new-report.json

Stage one sleeps a few seconds (`--stage-seconds`, default 8) so it can be caught running; if it
finishes first the check fails and says so. It needs the campaign runner and the local docker poller
running. Its test, `tests/test_mcp_steering_running_check.py`, runs the whole flow in process,
including a gate (a standing approval and an owner go) that starts a follow-on campaign.

## Where a person or operator CLI is still required

Found while writing the end-to-end test. "By design" items protect an authority boundary and
are not planned to change; the rest are gaps.

| # | Step | Needed today | Status |
| --- | --- | --- | --- |
| 1 | Approve a submitted (or retry) spec digest | a person's session; over MCP only on the approve-only server with that person's credential (`tessera_approve_campaign`, ADR 0065) | by design |
| 2 | Create the standing approval that carries a gate (spec, caps, `unlock`) | a person's session; no MCP tool, and an agent cannot submit a gated campaign | by design; see gap 10 |
| 3 | Record a gate `go` | a person's session, or the owner credential with an automation label where the operator started `tessera serve --accept-automation-gate-go`. No agent path. A person's own credential can also record it over MCP on the approve-only server (`tessera_record_gate_decision`, ADR 0124); the control API still refuses any other credential | by design for the go; the opt-in is operator config |
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

No steering tool available to an agent credential approves a spec, records a go, raises a cap,
reconciles a grant or releases data. The operator-enabled `tessera_approve_campaign` (ADR 0065)
and `tessera_record_gate_decision` (ADR 0124) run only on a separate approve-only server holding a
person's own credential, and the control API refuses any other credential there; the first test
does not register them, the second registers the approval tool; the
end-to-end test marks each human act `HUMAN` and asserts the approval count does not change
while the runner advances the stages.
