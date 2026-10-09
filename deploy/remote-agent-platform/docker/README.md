# Scoped portable packaging — not deployment or reasoning acceptance

This profile is distinct from root development Compose. It creates no credentials,
environment template, enrollment, approvals or copied OAuth accounts. No build,
pull, host inspection, deployment or inference was run for this source checkpoint.

The Dockerfile reuses the repository's pinned Python/uv images, installs the locked
application non-editably with **mcp and a2a** extras, and uses installed CLI modules.
Image architecture is explicitly linux/amd64. Build/image execution is unverified.

## Source staging (operator instructions only)

From the reviewed working tree, stage into a **new**, empty-path directory:

```sh
python deploy/remote-agent-platform/docker/stage_sources.py \
  --source /absolute/reviewed/tessera \
  --destination /absolute/new/tessera-source-stage
```

Only package metadata/lock/license/readme, source Python files, the two installed
skill documents and Dockerfile/its adjacent allowlist are selected. Symlinks are
refused. Uncommitted selected source is included: review/digest it before transfer.
There is no broad workspace COPY or staging of `.tessera`, hidden configuration,
auth, environment files, caches, local databases, keys or runtime credentials.
The staged Dockerfile-specific ignore file is a second exclusion boundary. Transfer
only this selected source or a reviewed image artifact, never local runtime state.

## External operator prerequisites

All bind sources in Compose are **server paths**, not client paths. An operator
must prepare `/srv/tessera-remote/control`, plus separate `peer-a`, `peer-b` and
`research` state/credential directories. State and credential directories must be
owned by runtime UID/GID 65532, private (0700), and private regular files 0600.
Compose refuses to create missing bind paths. Control state must be initialized
through ordinary authorized administration before startup; this profile does not
proxy an owner's token into agents.

Each peer's external credential directory contains the existing mapper's `profile`
and `agent-token` references. Follow the parent installed-service runbook: distinct
enrolled principal/credential/identity sets, `/state/peer/peer.sqlite3`, fixed
`TESSERA_A2A_CONTROL_TOKEN` mapping, explicit destination and trusted control URL.
Set peer-a's private profile `listen_port=8771`, peer-b's `listen_port=8772`.
Do not share profiles/tokens or peer databases. No credential value is included
here. The mapper continues to enforce private file ownership and fixed argv/env.

Peers share the control network namespace to retain the existing mapper's fixed
loopback listeners; they are **separate processes/principals/state**, not isolated
network namespaces. They are not published. This is not proof of protocol exchange.
Control binds container interfaces, but only host **127.0.0.1:18765** is published;
the Compose network is internal. No port 80, host networking or Docker socket.

Remote agent control URLs still require trusted HTTPS. Before actual peer use, an
operator must supply an admitted TLS reverse-proxy endpoint and public CA trust
compatible with the mapper/HTTP clients, or an explicitly approved localhost tunnel
boundary. The plain loopback publication is not a remote TLS endpoint. There is no
automatic proxy, permissive trust, host-header change, credential forwarding or
TLS bypass. Internal networking blocks generic outside harness/provider access;
any future dedicated harness route needs explicit reviewed network admission.

## Explicit scoped commands (DO NOT run before host admission)

First inspect current garage ports/workloads/capacity in the existing deployment
lane; they are **unknown** here. Preserve scanner LAN port 80 and all unrelated
services/data/default context. Load/build the reviewed image tag separately through
the authorized deployment lane; `pull_policy: never` prevents opportunistic pulls.

```sh
docker --context garage compose --project-name tessera-remote-agent-platform \
  -f /absolute/operator/compose.yaml up -d control peer-a peer-b
docker --context garage compose --project-name tessera-remote-agent-platform \
  -f /absolute/operator/compose.yaml ps
```

These are documentation, not executed commands. Never change the default context,
use unscoped prune/down, destroy volumes, or reinterpret container liveness as
acceptance. Review the fixed image tag, server paths and unused loopback port before
running them. No interpolation or env-template discovery is needed by this profile.

## Research service: deliberately not admitted

`research` is excluded from default startup by profile
`research-admission-required`, has **no restart loop**, and invokes the existing
fixed mapper's `research` mode. The mode now invokes the ordinary factory's normal
private-reference/profile validation and fresh authenticated observation. Missing
installed proof returns sanitized exit **78 before MCP children or session/prompt
effects**; a correctly supplied profile may read operator credential references
for bounded authenticated observation. Mounting files or enabling a profile does
not unlock inference. It cannot fall back to the old prompt supervisor, a coding
service, shared owner OAuth, API keys, paid credits or fabricated outputs.

The available split-context, transport and durable factory adapters are wired;
native context-issuer, final-only eligible metadata and backend record/input readers
are now concrete default adapters. The default attested observer consumes the strict
guard protocol via required independent HTTPS/private-reference/public-issuer pins.
The truthful installed/account/final-hook guard **producer is not shipped or admitted**;
its route is a required dedicated integration, not an existing V2 API. Unsupported
measurements require authorized human handoff/observability extension. Missing actual
proof fails closed; this is not installed/live ordinary-service completion.
See `docs/research-consumer.md` and `docs/research-human-records.md`.
Private profile/principal/endpoints/public CA references must be securely supplied
by the operator; no values go in the image, Compose, build context or receipt.
Health, restart, peer exchange, TLS, inference, independent-host execution and
laptop-off proof remain unverified. Packaging is not a Docker experiment adapter;
actual compute remains the separately governed ordinary Docker/Kubernetes lanes.
