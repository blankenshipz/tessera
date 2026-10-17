# Bounded local control upgrade (candidate implementation)

This is a separate occupied-target path, not another bootstrap or migration. It
does not resize/prune Docker storage, build an image, issue credentials, approve
delegation, call Salad, or enable execution. The existing operator source and
activation receipts remain the ownership authority. Do not hand-edit receipts.

## Remaining prerequisites before any live attempt

The node previously had only 1.59 GiB available. A supported Docker Desktop
increase and fresh capacity/health verification are still required. Preparation
and apply require at least 5 GiB node space and 5 GiB host space, not sparse-disk
logical capacity. Existing independent backups and source fences must remain.

`local_control_candidate_inventory` describes the published Git tree plus three
explicit overlays: the already-running `store.py` fence safeguard and the existing
Dockerfile/allowlist. It does not adopt the entire dirty checkout or any private
runtime files. The active store source hash must match that exact overlay, and
the candidate schema must match the active schema; there is no migration path.

The build producer is implemented but **not live-build verified**. After capacity
and focused provenance review, use `local_control_build_plan` and then
`local_control_build(operation,expected_build_plan_sha256)` with a fresh 32-hex ID.
The candidate must match locally recorded `origin/main`; root still verifies the
actual published remote revision before the live attempt. This path does not
invoke the namespace-absent bootstrap planner or mutate an occupied namespace.

The producer materializes Git archive blobs (checking every Git blob identity),
applies only the explicit hash-bound overlays, and records a complete SHA-256 file
inventory in its private build directory. Context files/directories are made
read-only. Logs, image export and progress evidence remain outside that context.
It builds Linux amd64 using the reviewed pinned Dockerfile, inspects immutable
image ID/platform, and runs only a network-disabled, read-only, unprivileged
package inspection with no state/credential mounts. Installed control/operator
Python hashes, schema and fence source must match the frozen inventory.

Commands use a fixed minimal environment, not inherited Docker/Buildx endpoints,
builder selection, source-policy, proxy, Git or loader overrides. The normal
`/Users/zach/.docker` context/registry credential-helper references remain in
place; credential/config contents are neither copied nor logged. Buildx explicitly
selects `desktop-linux`, whose inspected identity must be a single running local
`docker`-driver node at the verified Desktop context, with no custom driver options
or daemon flags. Selected public builder identity/BuildKit version and the fixed
execution-policy identifier are bound into the plan/runtime and rechecked.

Only after successful tagged export/import, containerd manifest digest/config-ID
comparison, immutable node pin verification and unchanged source/runtime/context
checks does it publish `candidates/IMAGE_SHA256.json`. Upgrade admission requires
that receipt to match the producer's separate verified receipt and current target
identity (Kubernetes configuration, Docker endpoint/node ID, owned namespace/
Deployment/PVC/PV, old image and backend incarnation/schema/fence).
Final pin verification reads its full descriptor, requires the exact previously
verified manifest digest, then verifies that content digest and config ID again.
Docker inspection IDs are explicitly typed: config-ID runtimes anchor config
bytes, while descriptor-ID runtimes anchor the exported manifest/index descriptor.
The producer verifies the OCI export's descriptor sizes/hashes, config platform,
inspected rootfs diff IDs and every layer's compressed and uncompressed digest.
Both OCI and Docker save metadata must agree. A single-platform index with exactly
one linux/amd64 member is supported; ambiguous/multi-platform or non-OCI exports
are rejected, not guessed. Imported and final pinned root/config chains must match
the verified archive; a config digest is never compared interchangeably with a
manifest digest. Failed attempts are never retroactively marked successful.
A final name pointing at different content fails closed, even if the imported
mutable source tag was valid earlier. This is verified provenance at observation,
not protection against an administrator mutating containerd after verification.

Build/import failures retain complete private per-command stdout/stderr, exit or
timeout reason and source/target intent. `local_control_build_status(operation)`
returns the saved result, or explicitly unknown interrupted/running state; it
does not infer success from a loaded tag, replay commands or clean up evidence.
An existing operation ID cannot rebuild/reimport. A new reviewed attempt uses a
new ID, preserving all prior logs. No caller can supply an attestation boolean
or request publication of a receipt without the build/verification sequence.

## Normal bounded operation

Entry: run `python -m tessera_operator.upgrade_server` with the installed MCP
extra in the local operator environment. This upgrade-only stdio server registers
only the nine build/upgrade tools; it does not import or expose the unpublished
bootstrap, migration, binder, authority-import, activation or route tool surface.
`upgrade_support.py` is the single shared implementation of the existing fixed
target/receipt/TLS/readiness/backup guards; legacy local operators forward to it.
Stop the stdio client/server to exit; reconnect with the same retained operation
ID. Disconnect does not delete receipts, restore authority or replay mutations.

1. Choose a fresh 32-hex operation ID. `local_control_upgrade_plan(operation,image)`
   validates the existing namespace/Deployment/PVC/PV, sole Ready writer, Recreate
   strategy, source fence, backend/schema and pinned old/new node images. It binds
   Deployment resourceVersion, full spec hash and source inventory into the plan.
2. `local_control_upgrade_prepare(operation,image,expected_plan_sha256)` creates
   an exclusive private operation directory and fresh consistent online backup in
   the owned Pod, retained independently on the Mac. Its generated backup path is
   unique to this operation; old before/after backups are never overwritten. A
   partial preparation is retained and cannot silently retry under the same ID.
   Preparation binds the backup's complete logical SQL-state digest (not merely
   events/schema/incarnation) to matching live read transactions before and after
   the backup. It retains a separate immutable freshness receipt, timestamped at
   backup start. Raw SQL/private values never leave these hashing routines.
3. `local_control_upgrade_apply(operation,expected_plan_sha256)` rechecks the
   plan/capacity/backup and commits a global one-use apply claim **before** a JSON
   Patch testing Deployment UID/resourceVersion, replicas=1, Recreate and old image.
   Only the container image is replaced. PVC/other spec fields are never edited.
   The normal Kubernetes Recreate controller owns old/new writer sequencing.
   Immediately before the claim, apply requires backup age in `[0,300]` seconds
   and a matching fresh full SQL-state observation. Age expiry, changed authority
   state, or unavailable observation rejects **before** claiming or patching.
   Refresh uses a new operation ID, plan and create-only backup; retained evidence
   is never overwritten. This is a state-at-observation binding, **not** an atomic
   writer fence: a write may commit after that observation and before Recreate
   stops the old writer. It does not promise zero-loss restoration from this
   backup or justify rollback. A recovery requiring that guarantee still needs a
   separately authorized verified quiescent snapshot/held-recovery protocol.
4. Submitted/failed/lost responses never mean readiness. Once a claim exists,
   neither a repeat call nor a new operation ID can resubmit another mutation.
   `local_control_upgrade_reconcile(operation)` only inspects the new sole Ready
   owned Pod, unchanged spec/PVC, backend incarnation/schema/store hash and actual
   Pod-loopback application health. On success it appends readiness/current-image
   receipts; the original activation record remains unchanged. Existing route
   supervision can then recognize the admitted image and reconnect normally.
   Readiness evidence is immutable and content-addressed per observed Pod. A
   crash before current-image publication can resume with a replacement Ready
   Pod and a new proof without altering the previous proof or resending PATCH.
   A published current-image pointer pins historical admission evidence, not a
   permanent Pod UID; normal readiness still inspects the actual live Pod.
5. Exit/reconnect reuses the same operation and receipts. An ambiguous unapplied
   intent stays blocked for explicit recovery admission, not automatic replay.
   This slice supports one admitted upgrade generation; subsequent upgrades need
   a separately reviewed next-generation admission path.

## Rollback is intentionally refusal, not unsafe automatic undo

`local_control_upgrade_rollback(operation)` is a read-only refusal/projection. The
old strict model cannot parse newly written `salad_proposed_project` metadata.
SQL schema equality does not make image-only rollback safe; checking for writes
and then patching an image has an unfenced writer race. The tool therefore never
patches back, restores a database or overwrites authority, even if no writes were
observed. Use a reviewed compatible forward image or separately authorized held
recovery. Preserve every backup/claim/uncertainty receipt while resolving it.

Local tests simulate cluster/backup effects and use a real stdio handshake only
to verify tool registration. Producer tests also use real temporary Git archive
materialization and a failing local command, but Docker build/import/inspection
outputs are synthetic. They are not live build, rollout or rollback acceptance.
