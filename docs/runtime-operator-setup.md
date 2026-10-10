# Runtime operator setup

This is the single-owner local bootstrap/admission boundary, not a production
deployment recipe. Follow [installation](installation.md) for wheel/services,
worker credential renewal and interruption recovery, and
[capabilities](implemented-capabilities.md) for current limits.

## Private state and upgrade

Use a reviewed artifact, dedicated account, operator-owned private state outside
the checkout and an unused port. Inspect process/listener ownership first; never
kill another project's service. Keep control, worker, MCP and researcher processes
separate; workers never mount control state or receive an owner header.

```sh
umask 077
/absolute/path/to/tessera-env/bin/tessera init --state-dir /private/operator/new-state
/absolute/path/to/tessera-env/bin/tessera serve --state-dir /private/operator/new-state --port 8765
```

Initialize once. `owner.header` is a high-authority private bearer reference, not
a value to copy into arguments, environment variables, prompts or logs. Loopback
is default; changing the bind address alone is not secure remote deployment.
Remote access needs verified TLS, trusted hosts/origin and supervised ingress.
The [connection tunnel](connection-tunnel.md) is narrower than permanent ingress.

Before upgrading, fence the old writer, verify a backup and preserve the old
artifact. Run `tessera migrate --state-dir STATE` explicitly with the intended
artifact. Check its supported schema and the backup manifest, not a dated status
page. Never downgrade against newer state or edit schema metadata. Verify a new
post-migration backup/restore. [Recovery](recovery.md) restores only to an absent
directory as permanently held history with old credentials revoked; it neither
fences original external work nor supports clear-hold/promotion/failover.

## Governed runtime enrollment

[Runtime setup](runtime-setup.md) owns the exact commands and secure owner handoff:

```sh
python -m tessera_mcp.runtime_setup --help
```

It supports `issue-policy`, `prepare-profile`, `enroll`, `operator-financial-ref`,
`doctor`, `current-resolve`, `revoke` and readback-only `reconcile`. Use genuine
issuer/principal/session/private-resource references, original financial
observation times and independently verified installed linkage. Do not invent
identities/signatures or discover/copy OAuth/keychain credentials.

Strict v1 is default. Runtime admission v2 needs explicit human opt-in; missing
required local guard/quota/runtime checks deny. Provider entitlement/billing may
remain unknown only under the reviewed v2 policy, never as permission for a
fallback. A zero-dollar internal grant is not a provider spend cap.

`generate_binding` then `bind_runtime_profile` adds v2 policy/profile pins while
retaining the enclosing `tessera-opencode-v2-binding-v1` version. Generation is not
admission: owner-verified signatures, installed manifest/process readback and
fresh enrollment must match the final bytes. Missing trusted runtime integration
denies; request flags, fixture identities and self-attestation cannot bypass it.

See [agent setup](remote-agent-platform-install.md) for stdio MCP/A2A boundaries.
Genuine planner/critic reasoning, independent-host/laptop-off and native dialogs
remain separate acceptance gates. Privacy, retention, signing, capacity and
incident ownership prerequisites are in the [release checklist](release-checklist.md).
