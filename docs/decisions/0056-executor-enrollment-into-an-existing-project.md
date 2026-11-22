# 0056 — Executor enrollment into an existing project

## Status

Implemented. No schema change. Extends the stable executor enrollment of
[ADR 0040](0040-multi-stage-multi-vendor-campaigns.md) section 4 and
[provider trust](../provider-trust.md#stable-executor-route-setups-that-mint-principals).

## Context

A campaign has exactly one project, and a stage runs on a worker principal that belongs to
that project: its snapshots, releases, grants, artifacts and receipts all live there. An
executor enrollment always created a **new** project for the executor. A CPU worker and a
Salad executor enrolled that way ended up in two different projects, so one campaign could
not mix stages on both: the data and the receipts of the CPU stages are not in the project
the GPU executor can be granted work in.

## Decision

### 1. Enroll into an existing project

`scripts/executor_enrollment.py enroll` takes `--existing-project PROJECT_ID`. The
enrollment then creates **no project**. It looks the project up through the owner's own
authority (`GET /projects/{id}`, journaled as `executor-project-existing`), then issues the
executor's worker credential and registers the worker in that project exactly as for a new
one. Provider, region, lifetime, renewal, revocation and the private 0700 directory are
unchanged, and `executor.json` records `project_existing: true` (an older record without
the field reads as `false`).

The tool works for any provider id the tool knows (`salad`, `local-docker`,
`self-hosted`); a new vendor adds a label entry to the tool, not a line to the campaign
runner. The runner already issues a stage's grant to the worker principal its provider
names, so mixed-provider stages need nothing more once both principals are in the project.

### 2. Authorization and refusals

- Only the local owner can do it. The tool needs a human owner header, and control
  issues credentials to the local owner only and checks the project against the owner's
  organization (`Store._project`); a worker or agent credential is refused.
- An unknown project, one in another organization, or a malformed id is refused with
  "the existing project was not found or is not available to this owner" **before** any
  credential or principal exists. Control's own detail is not echoed.
- `--project-name` cannot be combined with `--existing-project`.

### 3. Idempotency and the journal

Re-running the same `--existing-project` command in the same executor directory (same
provider, project, region and number of days, live record) changes nothing and prints the
same record. A directory that holds any other enrollment is refused and never overwritten:
another provider or project, a different `--region` or `--days` (the recorded lifetime is
compared exactly, so a changed value is a refusal, not a silently ignored option) or a revoked
record. Every control request of a first enrollment is journaled before it is sent, as before.

The idempotency covers a **completed** enrollment only. It is a check of the record in
`executor.json`, not a reconciliation with control. If a first run stops after the credential
was issued but before `executor.json` was written (a crash, a full disk), the directory then
holds a token and a journal but no record; a rerun there is refused ("executor executor.json is
missing") and issues nothing more, rather than minting a second credential. Recover by
revoking the orphaned credential if the journal shows one was issued (it expires on its own
within 24 hours) and enrolling into a **new** directory. This is the same limit as an
enrollment into a new project.

### 4. Trust is unchanged

Being in a project grants no data class. The principal still has to be listed under its
provider in the provider trust file, and a campaign still admits each stage input against
that list (ADR 0039, ADR 0040). Joining a project gives the executor no more than a
worker credential bound to it.

### 5. Agent and MCP surface

Executor enrollment is an owner command line tool. It is not exposed through the MCP
server or any agent route, and this change does not expose it: an agent cannot enroll or
move an executor. If enrollment is ever exposed there, it must call the same enrollment
module so these checks apply.

## Consequences

- One campaign project can hold a CPU worker and a Salad executor (or any other provider)
  with their data, snapshots and receipts together.
- A shared project is a shared authority boundary: see "Shared project authority" in
  provider trust. Enroll executors into the project they should work in, nothing wider.
- Rotation and revocation work per executor as before; revoking one leaves the other
  executors of the project untouched.

## Tests

`tests/test_executor_enrollment.py`: a CPU worker and a Salad executor in one project,
journal and privacy, idempotent repeat, a directory with another enrollment (including another
region or number of days) refused, an interrupted first run refused without issuing anything more,
unknown and malformed projects refused before anything is issued, `--project-name`
conflict, a worker credential refused, renew and revoke in an existing project, and a
record written before the field still validating.
