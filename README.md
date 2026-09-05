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

**Status:** Design only. The workflows and controls below are requirements, not
implemented capabilities. There is no runnable application yet.

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
