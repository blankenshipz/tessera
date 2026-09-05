# Tessera

Tessera is a desktop workspace for directing local and remote research agents.
A researcher works with a desktop agent to plan an investigation, author reusable
skills, run experiments on approved infrastructure, and review findings in a
persistent labbook.

A tessera is a piece of a mosaic. The name reflects how observations, experiments,
and different perspectives come together to form a research finding.

The desktop, control plane, and execution workers run independently. Here,
“remote” means outside Electron: all three can run on one machine or across
separate machines and clusters.

**Status:** A runnable local control-plane foundation is implemented: owner bearer
authentication, tenant-scoped projects and draft tasks, revision-checked human
plan approval, idempotent writes, and persistent labbook events. Approval records
intent only; it never grants execution. Desktop, agents, experiment execution,
and the broader workflows below remain planned.

## Run the foundation

Docker Compose is the development/test workflow; uv manages the Python lockfile.

```sh
docker compose up --build -d --wait control
docker compose run --build --rm test
curl http://127.0.0.1:8765/health
```

For an authenticated request without printing the owner credential, copy its
header to a private temporary directory, use it, and delete it when finished:

```sh
credential_dir=$(mktemp -d)
docker compose cp control:/state/control/owner.header "$credential_dir/owner.header"
chmod 600 "$credential_dir/owner.header"
curl -H @"$credential_dir/owner.header" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: first-project' \
  -d '{"name":"First investigation"}' http://127.0.0.1:8765/projects
rm "$credential_dir/owner.header"
rmdir "$credential_dir"
```

Stop with `docker compose down`; the named volume retains the database and owner
credential. See [CONTRIBUTING.md](./CONTRIBUTING.md) for native uv setup and the PR
workflow. Compose is not required for deployment. No license has been selected.

The API exposes project creation/listing, task creation/listing/retrieval,
`POST /tasks/{id}/approve` with `expected_revision`, and
`GET /projects/{id}/labbook?after=0&limit=100`. Mutations require an
`Idempotency-Key`; reusing one with different input returns a conflict.
Project/task lists accept `limit` (1–200, default 100) and `offset` (default 0);
increase the offset by the number returned until the page is shorter than the limit.
Task inputs are `title`, `purpose`, `budget_cents`, and `max_duration_seconds`.
Budgets are recorded bounds, with no execution or spending enforcement yet.
Browser origins are rejected; the owner credential is for trusted local human
clients. It is not production organization authentication.

## The first workflow

1. Connect to a personal, organization-managed, or Tessera-hosted control plane.
   Create a project or adopt an existing repository, notebook, or results folder;
   imported work keeps its provenance gaps.
2. Ask the desktop agent to investigate a question or help define the next
   activity, including inputs, method, expected outputs, cost, and stop conditions.
3. Author or select a skill, review the plan and access requested, and approve
   bounded execution on one local or remote target.
4. Ask the remote agent questions through the desktop agent. Inspect progress,
   revise work, resolve decisions, or stop execution and take over editing.
5. Follow the labbook: attempts, sources, findings, alternatives, decisions,
   costs, and corrections. Revisit past conclusions without rerunning anything.
6. Review and export the result, including negative or inconclusive outcomes.
   A rerun or new branch is a separate authorized activity.

## What the workspace includes

- Chat, project history, selected files, artifact previews, and change review.
- Skills authored in chat, the UI, or a repository using `SKILL.md`; optional
  scripts and resources. Runs record the exact skill version used.
- A labbook linking conclusions to evidence and the wider investigation,
  including failed, excluded, and abandoned work.
- A decision inbox for direction, cost, access, methodology, and publication.
- Configurable agent harnesses and compute targets, with supported capabilities
  and limitations shown before dispatch.
- Named control-plane connections showing the signed-in organization, project,
  data owner, and execution destinations.

The scientific model covers literature, theory, observation, analysis, simulation,
and experiments. Domain-specific methods and validation belong in research
packages. The first pilot uses one existing computational workflow; it does not
attempt to support every domain or infrastructure combination.

## System boundaries

| Part | Responsibility | Runs where |
|---|---|---|
| Desktop client | Chat, skills, labbook, review and approvals | Researcher's machine |
| Control plane | Identity/access, tasks, budgets, research records and credential brokering | Local processes/cluster, organization infrastructure, or Tessera hosting |
| Execution plane | Agent supervisors, harnesses, tools and experiment jobs | Same machine, other hosts, clusters or cloud compute |

Three deployment modes use the same authenticated API:

- **Personal:** a separately running local control plane, initially as processes;
  a local cluster such as Minikube is another deployment option.
- **Organization-managed:** the organization operates the control plane and
  connects its identity, secrets, storage, and compute services.
- **Tessera-hosted:** a planned paid service operates the control plane;
  customers can retain their own workers, compute accounts, and secrets backend.

Each project has one authoritative control plane. Organization isolation covers
data, credentials, and operations; a Kubernetes namespace alone is not sufficient.
Switching desktop connections does not move projects or transfer credentials.

Closing Electron does not stop the separately running control plane. If its
machine sleeps or becomes unreachable, workers follow their disconnect and
credential-expiry policies. Stopping an agent session does not necessarily stop
its jobs; the UI distinguishes requested stops from confirmed termination and
reports unresolved cost exposure.

Skills are instructions, not permissions. Authorization applies to files, tools,
models, conversations, jobs, secrets, and publication. Credential storage is
provider-neutral: the control plane can broker access to an existing secrets
service without storing its values. Secrets do not belong in prompts, skills,
or evidence bundles. Local deployments also require authentication.

GitHub is an optional publication destination, not the job queue or labbook.
A separate publisher may open research-scoped PRs; research agents cannot modify
platform code or policies.

## Build scope

Start with one user, a separately runnable process-based control plane, one
supported harness, one worker supervisor, one CPU target, and one credential
integration. The desktop connects over an authenticated API even on the same
machine. Prove adoption, skill authoring, bounded execution,
human intervention, labbook review, and portable export. Security and evidence
checks apply from the first real-data run.

Cluster packaging, organization login/administration, paid hosting, additional
runtimes, GPUs, schedules, and automatic memory follow the pilot. Compare it with
the researcher's current workflow, including setup, review, verification, and
cost—not just agent execution speed.

See [DESIGN.md](./DESIGN.md) for the architecture, implementation sequence,
failure handling, and release tests.
