# Supervised agent research loop

A developer-alpha walkthrough of one loop for **one local owner and one
MCP-enrolled agent**: propose, approve, run, observe, critique. It uses only
routes, tools and flags that exist in this repository. It does not establish live
cluster, provider or paid-compute support, and the full MCP acceptance in
[MCP full workflow](mcp-full-workflow.md) remains pending. Body schemas are in
[Local API workflow](local-api-workflow.md), [records API](records-api.md),
[agent authority interface](agent-authority-interface.md) and
[ordinary Docker control](ordinary-docker-control.md); they are not repeated here.

All HTTP mutations need an `Idempotency-Key`. Commands marked **human** use the
owner credential; commands marked **agent** run inside the MCP client.

## 1. Start and enroll (human)

```sh
uv run --locked tessera init --state-dir .tessera
uv run --locked tessera serve --state-dir .tessera --port 8765
curl --fail-with-body -H @.tessera/owner.header -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: first-project' -d '{"name":"My research bench"}' \
  http://127.0.0.1:8765/projects
```

`.tessera/owner.header` (`Authorization: Bearer ...`) is the owner credential.
Never give it to an agent or put it in an MCP client configuration.

Enroll a distinct agent identity. `--private-dir` must be a new directory whose
parent exists; the helper creates it mode 0700 and writes `agent-token` and
`result.json` there. It accepts only an `http://127.0.0.1:PORT` control origin.

```sh
python scripts/enroll_local_mcp.py --owner-header .tessera/owner.header \
  --private-dir /abs/path/agent-private --project-id <project_id> \
  --destination <destination> --allow-proposals --allow-results
```

Without opt-in flags the agent holds `context:read`, `proposal:read` and
`notebook:read`. `--allow-proposals` adds `proposal:create`; `--allow-results` adds
`results:read`; `--allow-budget` adds `budget:read`. `--duration-minutes` defaults
to 60 and there is no automatic renewal; `--resume` reconciles an interrupted run.

**Not granted by the helper:** `authority:request`, `authority:consume`,
`observation:create` and `review:request` (also valid scopes) are not issued by
`enroll_local_mcp.py`. To use steps 3 to 5 as the agent, the owner issues the
credential directly with `POST /credentials`, passing an `external_agent` object
(`destination`, `actions`) as `tests/test_agent_authority.py` does. Without those
scopes the agent can propose and read, and the human performs the other steps.

Configure the MCP client to launch `tessera-mcp` as a stdio child with
`TESSERA_CONTROL_URL`, `TESSERA_AGENT_TOKEN_FILE` (the `agent-token` path),
`TESSERA_PROJECT_ID` and `TESSERA_AGENT_DESTINATION`; see
[governed agents](remote-agent-platform-install.md).

## 2. Propose and approve

**Agent:** `tessera_propose_research` posts to
`POST /projects/{id}/agent-workflow-proposals`. The result is a proposal record,
not a project, study or experiment. `tessera_list_research_proposals` and
`tessera_read_research_proposal` read it back.

**Human:**

```sh
uv run --locked tessera agent-proposal list --project-id <project_id> \
  --owner-header .tessera/owner.header
uv run --locked tessera agent-proposal review <proposal_id> \
  --owner-header .tessera/owner.header [--control-url http://127.0.0.1:8765]
```

`review` prints the proposal under a warning that its text is agent-authored and
untrusted, then asks `approve/decline/skip`. Approve requires typing the first 8
characters of the proposal digest and calls
`POST /agent-research-proposals/{id}/promote`; decline requires a rationale and
calls `POST /agent-research-proposals/{id}/decline`. There is no flag that supplies
the decision; without a TTY it exits 2 and records nothing. Only loopback
`http://HOST:PORT` origins are contacted, and the owner header file must be private
and owned by you.

Promotion creates ordinary records attributed to the human and **never authorizes
execution**. Limitation: the TTY prompt is an ergonomic and attribution guard, not a
security boundary. Any same-user process that can read `owner.header` can call the
HTTP routes directly.

## 3. Authorize a run (human, with agent request)

Execution authority is separate from approval. In order:

1. Human sets limits `PUT /projects/{id}/execution-limits` and issues an execution
   grant `POST /projects/{id}/execution-grants` (see
   [Local API workflow](local-api-workflow.md) steps 2 to 4 for approved task
   plans, experiments and variants).
2. Optionally, human authorizes an experiment budget with
   `POST /experiments/{id}/budget/authorization`. It is required for the Salad GPU
   route ([Salad proxy](salad-proxy.md)). Unbudgeted CPU and minikube experiments
   keep their task/project execution limits; once a budget exists, every admission
   reserves against it.
3. **Agent:** `tessera_request_authority` posts a typed bounded request
   (`POST /projects/{id}/agent-authority-requests`); it creates no authority.
   `tessera_list_authority_requests` and `tessera_authority_status` read state.
4. **Human:** issues the grant `POST /projects/{id}/agent-authority-grants` with
   bounds (variants, execution grant pins, budget, duration, expiry; see the
   authority interface doc). Revoke with `/agent-authority-grants/{id}/revoke`.
5. **Agent:** `tessera_consume_grant` with `attempt:admit`, then `attempt:dispatch`
   with `profile_version` `development-minikube-opencode-v1` or CPU
   `ordinary-docker-v1`. The MCP adapter offers exactly the profiles the control
   contract admits (`src/tessera_mcp/dispatch_profiles.py`). Docker CPU dispatch
   needs a native package from a worker first; see
   [ordinary Docker control](ordinary-docker-control.md).

Human alternatives for dispatch: `POST /experiment-admitted-attempts/{id}/dispatch`
or `/docker-dispatch`.

## 4. Observe

Results enter the ledger through human or worker records, not agent claims:

- Human resolves the attempt: `POST /experiment-admitted-attempts/{id}/resolve`.
- Human records measurements: `POST /experiment-attempts/{id}/measurements` (or
  `/derived-measurements`).

The agent reads recorded state with the result tools (needs `results:read`):

- `tessera_experiment_results(experiment_id)` calls
  `GET /projects/{project_id}/agent-results/experiments/{experiment_id}`. Sections:
  `attempts`, `measurements`, `artifacts`, `checkpoints`, `acceptances`,
  `conclusions`, `comparison`, `budget`. `comparison` is README workflow step 5: per design
  revision and measurement name recorded on both arms, the difference of means (intervention
  minus baseline) over succeeded attempts, one value per attempt (several measurements of a
  name on one attempt are averaged), with both sample sizes and, when each arm has at least two
  attempts and some spread, a Welch 95% interval (`not_estimable_…` below two, `degenerate_no_spread`
  when every value is equal). Each arm shows its admitted/recorded attempt counts and sources, and
  `mixed_provenance` flags a mix; one-arm-only names and the attempts that did not succeed
  are listed separately, `truncated` plus the `*_total` counts flag a capped list, and `decision` is always `none` (it is arithmetic on recorded numbers,
  not a verdict or a recommendation).
- `tessera_program_results(study_id)` calls
  `GET /projects/{project_id}/agent-results/studies/{study_id}`. Sections:
  `program_reviews` (with gates and evaluations), `experiments`, `budget` roll-up.

`budget` sections need `budget:read`; otherwise they are listed under `withheld`
with the required scope. Every response carries `authority_effect: none`,
`scientific_validity: not_assessed` and an `evidence_boundary` statement. Each
section is capped at 1000 rows and the response at 2 MiB; over a cap the route
returns 409 rather than a truncated summary. New sections are added with
`register_result_section` in `src/tessera_control/agent_research_results.py`.
The owner reads the same routes:

```sh
curl --fail-with-body -H @.tessera/owner.header \
  http://127.0.0.1:8765/projects/<project_id>/agent-results/experiments/<experiment_id>
```

Artifact bytes are not in these summaries; they stay behind the human custody
release path described in [Local API workflow](local-api-workflow.md).

## 5. Critique

**Agent:** `tessera_record_observation` records a caller-attested observation
(`authority_effect: none`) and `tessera_request_review` asks for human review. Both
need the scopes noted in step 1.

**Human:** records evidence-citing conclusions with
`POST /experiments/{id}/conclusions`, then reviews and program evaluations or
go/no-go records per [research records](research-human-records.md) and the
[records API](records-api.md). The next proposal begins at step 2.

An agent credential cannot approve, accept, measure, authorize budget or promote:
those routes return 403 even with every read scope
(`tests/test_agent_results_summary.py`).

## What this does not do

- No auto-approval: every promotion and decline is a typed human decision, and
  approval grants no execution authority.
- Results are recorded data, not verified science. Measurements are human-entered
  or derived and not verified by Tessera.
- No live provider evidence. Default tests fake external effects; nothing here
  proves cluster, Salad or other provider execution.
- The local enrollment helper issues no authority/observation/review scopes.
- Full MCP acceptance in [MCP full workflow](mcp-full-workflow.md) is pending.
