# Runtime operator bootstrap and release boundary

This guide describes the currently implemented single-owner local bootstrap and
upgrade controls. It is **not** a production deployment recipe or evidence that
the current source tree has been packaged, installed, or admitted. For the exact current
status, see the [implementation progress](implementation-progress.md),
[release checklist](release-checklist.md), and
[remote-agent platform install guide](remote-agent-platform-install.md).

## Before starting

1. Use only an approved Tessera artifact whose release owner has recorded its
   source, artifact hash, dependency/license inventory, and supported runtime. The
   latest source is schema 55; the latest recorded installed platform journey is
   schema 52. Source readiness is not an installed schema-55 migration or release
   gate; consult the release owner's exact candidate and gate receipts.
2. Choose an operator-owned private state directory **outside the checkout**. Use
   a dedicated service account and storage with owner-only permissions. Inspect
   the process and port owner before starting anything; do not kill another
   project's process or change a shared Docker context.
3. Keep control, worker, researcher and MCP processes separate. A worker receives
   only its scoped, expiring credential and durable worker workspace; it never
   mounts control state or receives the owner's header.

## Fresh local control bootstrap

The existing CLI supports explicit local initialization and loopback serving:

```sh
umask 077
/absolute/path/to/tessera-env/bin/tessera init \
  --state-dir /private/operator/tessera-state
/absolute/path/to/tessera-env/bin/tessera serve \
  --state-dir /private/operator/tessera-state --port 8765
```

`init` creates `owner.header` privately and prints its path, not the header value.
Treat that file as a high-authority local bearer credential: never paste it into a
prompt, command argument, environment variable, log, repository, worker, MCP
process, or research artifact. Local bearer possession is not federated human
identity or privileged reauthentication. Use the ordinary authenticated control
API for separately scoped worker/agent enrollment and human actions; see
[installation](installation.md) and [agent authority](agent-authority-interface.md).

`serve` defaults to `127.0.0.1`. Do not expose it by changing the bind address
alone. Remote access requires a separately verified TLS gateway, trusted-host and
origin configuration, protected state, ingress/rate/cache controls, and an
operator-owned lifecycle. Follow the [connection tunnel contract](connection-tunnel.md)
for its narrower installed path; a laptop-local tunnel is not permanent ingress or
an always-on host.

## Upgrade and migration

Tessera does not silently migrate on startup. Before an upgrade, stop or fence the
old writer according to the installed deployment's lifecycle, make and verify a
private backup, preserve the exact old artifact, then run the explicit migration
with the intended artifact:

```sh
/absolute/path/to/tessera-env/bin/tessera backup \
  --state-dir /private/operator/tessera-state \
  --output /private/operator/backups/new-unique-backup
/absolute/path/to/tessera-env/bin/tessera migrate \
  --state-dir /private/operator/tessera-state
```

The current source target is schema 55. Migration 54-to-55 adds the runtime-binding
schema, but **does not** install production owner resources or create approvals.
Confirm the exact candidate and migration evidence before using this command on
valuable state. No schema-55 installed migration is claimed here. Do not downgrade
the binary against a newer database or hand-edit schema metadata.

Restore only into a new absent state directory from a verified backup:

```sh
/absolute/path/to/tessera-env/bin/tessera restore \
  --backup /private/operator/backups/verified-backup \
  --state-dir /private/operator/recovered-held-state
```

A restored copy is permanently held read-only history, with credentials revoked.
There is no supported command to clear the hold, resume its grants, or use it as
failover. The original writer and external work are not automatically fenced or
reconciled by restore. Follow [recovery](recovery.md); preserve both branches until
an operator has reconciled external operations and established fresh authority.

## Governed agent/runtime enrollment status

The reviewed contract preserves strict v1 as the default. Runtime admission v2
requires explicit human opt-in; missing required local guard/quota/runtime checks
deny. Provider entitlement or billing can remain unknown only under the reviewed
v2 policy; this does not make provider status known or authorize a fallback.

Source implements a local Python Store/API/verifier, native HTTP/WS binding path
and the ordinary setup module documented in [runtime setup](runtime-setup.md).
Use `python -m tessera_mcp.runtime_setup --help` for `issue-policy`, `prepare-profile`,
`enroll`, `operator-financial-ref`, `doctor`, `current-resolve`, `revoke` and
readback-only `reconcile`. The guide specifies exact private credential references,
typed human choices, original financial observation times and current issuer-derived
resolution; it does not invent identities, observations or signing proof.

The setup source helper and temporary authenticated HTTPS/Store tests are ready;
clean-wheel installation and genuine production owner integration remain separate
checks. Genuine publisher/principal/session/issuer registration, owner-held private
guard/quota resources and signed full installed SDK/configuration/skill/catalog/
process linkage require the concrete secure owner handoff in that guide. Current
financial/revocation/profile-age resolution is implemented, not a missing endpoint;
its production use requires those genuine bindings. The server rejects admission when its trusted runtime
adapter is absent. Do not work around that rejection with request flags, copied
credentials, test fixtures, or a local self-attestation. No OAuth/keychain discovery
or credential copy is part of setup.

Binding generation is also not admission: `generate_binding` then
`bind_runtime_profile` adds the nested `runtime_admission` v2 policy/profile pins
while keeping the enclosing `tessera-opencode-v2-binding-v1` version. Independently
approved installed manifest/process readback and fresh authenticated enrollment
must match those final bytes. A signature status flag or generated JSON cannot
replace the owner's signature verification and current private resource capability.

`tessera-mcp` is the session-based stdio adapter documented in
[installation](installation.md); it is not an HTTP MCP listener. Its installed
catalog and full researcher workflow must be verified against the exact artifact.
Real planner/critic inference, human experiment review, run restart/cancellation,
device, independent-host and laptop-off acceptance remain separate gates.

## Operations and still-open owner decisions

- Set support, recovery and incident ownership before a public release. No support
  response SLA, retention period, backup service level, or account-recovery promise
  is established by this guide.
- Decide and document privacy/data lifecycle for prompts, public outputs, datasets,
  artifacts, journals, backups and held history. Existing retention metadata and
  storage limits are not a deletion policy or aggregate capacity guarantee.
- Verify private vulnerability reporting in the repository settings. It is currently
  unverified; do not put sensitive details in public issues and do not change
  repository visibility to enable a channel.
- Provide an independent security review, supported OS/runtime/install matrix,
  upgrade/rollback drill, dependency provenance, artifact signature and update
  verification before calling a distribution supported. No signing proof is claimed.
- Keep operator-attested financial settings separate from provider entitlement and
  billing facts. A zero-dollar internal grant is not a provider-spend cap.

These are release prerequisites, not steps an agent may approve for itself. See the
[release checklist](release-checklist.md) for the evidence owner must attach.
