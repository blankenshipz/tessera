# Schema maintenance observation and supervised effect contract

Status: local unexposed observation and supervised journal implemented; no native
mutation entrypoint installed. This does not change the control image or active store.

## Journey and scope

Operator setup separately provisions an exact hash-reviewed read-only observer Pod
and establishes independent host/admission/guardian fencing. Neither operation is
hidden in discovery. Everyday maintenance entry uses a durable exact-target plan;
useful action collects fixed read-only Deployment/PVC/PV/namespace Pod GETs, then
one fixed read-only DB probe in that existing observer. Visible result binds object
UIDs/resourceVersions, the observer spec, `/state/control/tessera.sqlite3`, inode,
device, schema, incarnation and stable DB SHA256. Exit leaves no new Pod or daemon.
Reconnect retains receipts; no automatic replay of an uncertain call.

`src/tessera_operator/stopped_observation.py` implements only this scoped collector.
It is not imported by the MCP server and has no registration/mutation entry point.
The configuration pins must be operator-owned. The collector requires Deployment
zero replicas with observed generation, rejects every other PVC-consuming Pod
(even terminal ones), and checks an unchanged before/after metadata snapshot.
The separately created observer must use a single pinned container and read-only
PVC/mount without init/ephemeral containers or service-account token mounting.
The fixed probe rejects symlinks, hardlinks and WAL/SHM/journal files; opens SQLite
immutable/read-only; and compares stat and byte hashes across the read. This is
not an atomic filesystem snapshot or proof against privileged/non-Kubernetes
writers. Consequently `stopped_db_proof` is always false. Host/admission/guardian
fencing and authenticated transport provenance remain necessary before accepting
these observations as maintenance preconditions. Backup custody is explicitly null.
The adapter is not wired into the prior transport's boolean evidence interface.

## Exact absent authority protocol

Inspected contracts:

* Retained native79 `schema_transport.py:20–24`: `NativeMaintenanceGate.require`
  unconditionally denies schema58 scope; it has no verifier or issuer protocol.
* Installed native86 `schema_upgrade.py:164–173,316–318`: guarded transport still
  requires that native gate; no mutation registration is installed.
* `src/tessera_control/agent_authority_models.py:18–20,23–46,114–119` closes grant
  actions to attempt admission/dispatch/status, cancellation and delegation,
  bound to research variants/execution grants. There is no schema-maintenance
  action. `agent_authority_api.py:51–88` issues/revokes/consumes those research
  grants only. Adding a schema action there would widen a different authority.

**Selected contract (intent89):** human-supervised infrastructure maintenance.
Existing operator client-certificate rights plus native permission on EACH exact
exposed effect can bound infrastructure actions; a new autonomous issuer is NOT
inherently required. These are not research grants. No detached continuation,
approval boolean, plan-hash-as-permission or invented token is permitted. Research,
stop-only guardian and same-schema scope remain unchanged. The default retained
`NativeMaintenanceGate.require` still denies; this route does not replace it.
No request ID, human identity, permission expiry API or token is fabricated.

For future autonomous maintenance ONLY, an additional issuer protocol would need
review (not selected for this route; NOT callable endpoints): authenticated
issuer identity/transport/trust anchor; human approver identity and decision ID;
subject/operator identity; grant/request ID, revision and revocation epoch;
issued/not-before/expires timestamps; exact operation ID, immutable plan SHA256,
target cluster/namespace/Deployment/PVC/PV UIDs and observed generations/versions;
phase/action and canonical method/URI/body digest; observer/transport/script/image
digests; DB relative path, device/inode, incarnation and phase-specific DB SHA256;
outage deadline, recovery restrictions and nondelegable single-use action identity.
Use-time authoritative consumption must atomically check current epoch/revocation,
expiry, scope, phase order and one-use claim, returning a durable authenticated
receipt. A lost acknowledgement is queried by the same action ID, never resubmitted.
Unavailable issuer/status or mismatched identity denies. A caller-provided boolean
or signed-but-stale offline assertion cannot establish current nonrevocation.
No such issuer or verifier is created; native tool admission owns supervised
permission, including denial. Application deadline checks do not prove permission.

## Local supervised entrypoints and exact remaining boundary

`src/tessera_operator/schema_supervised.py` implements a separate, unregistered
`SupervisedOperation` journal with `prepare`, `apply`, and retained `reconcile`.
It reuses the existing fsync/write-once helper and nonblocking flock convention.
Prepare seals operation/plan/phase/deadline/profile, target UIDs, DB incarnation/
phase hash, image/script, command/CAS and evidence digest from the owner profile.
The request names only a phase; it never supplies commands or approval. One
durable claim precedes one dispatch. Repeated guard checks bind the SAME action,
not repeated permission consumption. Expiry/drift/ordering/duplicates fail closed.
Lost acknowledgements retain claims; reconcile reads local claims/results only,
including after deadline. It does not infer remote success, adopt Jobs or retry.
Any migration claim forces admission-held compatible58 forward-only recovery.

**Current concrete composition:** `KubernetesSupervisedProfile` renders fixed
Deployment JSON-patch CAS (UID/resourceVersion/full-spec tests), deny-network
policy creation, a separate same-PVC maintenance Pod, old-image backup plus held
restore, new-image migration, new-image backup plus held restore, and compatible58
activation CAS. `BoundedKubernetes` executes fixed-context argv/stdin with timeout,
bounded accepted responses, no shell/retry/background controller. Production
`apply` now accepts only this exact concrete profile, not the disposable profile;
it is NOT installed or registered. A future native handler must surround EACH
invocation with native permission. This method neither introspects permission nor
issues tokens, and arbitrary Python invocation is not evidence of tool admission.
No new native identity/receipt/expiry API is invented or made a prerequisite.

Each phase locally pins independently produced evidence via owner-only setup
`pin_evidence`, separate from request input. Hash-pinned records bind target,
operation, phase, observation time and validity through the outage deadline;
separate admission/host-writer/liability witnesses require owner identity,
matching target and bounded validity. Admission must be held, host-writer witness
must account for writers, affected provider work must have an explicit guardian
mapping or terminal settlement (see intent90 below), aggregate
exposure must match and remain within 5000c. Migrate/validate/activate additionally
require backup custodian and hash-pinned manifest/restore-receipt readback.
These are required external evidence inputs, NOT a new evidence issuer or proof
that their contents were actually observed. Their producing owners, exact host
coverage and current authenticated provenance must be established before live
admission. Merely creating these files is not live evidence; synthetic tests are
explicitly labeled. The stopped observer's false proof is never changed to true.

The profile independently checks Deployment/PVC/PV UIDs/claim binding, rejects
unknown PVC consumers, requires observed zero replicas after scale, and compares
maintenance Pod UID/spec/runtime image and immutable DB readback to pinned evidence.
The fixed scripts guard schema/incarnation/hash/device/inode, then create one
durable remote claim and result. CLI logs remain in scratch, not JSON output.
Transport acknowledgement loss leaves host claim uncertain; `reconcile_remote`
reads only the existing result/object, validates its phase/object/post-CAS identity,
and never resubmits or automatically promotes the claim to a completed phase.
An observed object post-state alone is not proof of the original acknowledgement.

Phases are split into `scale_zero`, `network_policy`, `maintenance_pod`, `backup`,
`migrate`, `validate`, `activate`: the old multi-effect quiesce is deliberately not
called. Admission/affected-worker stop is a separately scoped action, not a hidden
side effect. No phase auto-continues or creates a background controller. Scripted
tests execute this concrete compiler/request/readback on scripted Kubernetes and
real temporary claims/DBs, not a substitute dispatcher alone. They are NOT native
permission or live custody evidence. Separate admission/affected-worker stop,
independent host/guardian witness production, optional read-only observer creation,
durable backup export/custody and final application reconnect/shutdown acceptance
remain external effects/contracts; no nonexistent stop endpoint is invented.
The maintenance Pod is a separately listed writable maintenance effect, not a
read-only observer. Its scratch backup is ephemeral until separately exported.
Kubernetes named-Pod exec has no UID CAS: the external fence must prevent Pod
replacement during a phase; pre-exec identity inspection alone cannot prove this.

## Intent90: persistent supervised Pod-creation hold

`maintenance_hold.py` adds unregistered, separately admitted `prepare`/`apply` and
read-only `reconcile` for the fixed `tessera-maintenance-hold` ResourceQuota in the
fixed control namespace. Setup creates the maintenance Pod first, without running
database commands. Hold creates a new owned quota with `hard.pods: "0"`; it never
adopts, edits or deletes unrelated quota. Existing Pods are not evicted. A quota
GET or successful create is **not enforcement proof**: DB windows additionally
require an independently owned denial receipt bound to this quota UID/spec.

Separate owner-pinned records must account for exclusive Pod edit/exec rights,
drained affected dispatchers/attempts, terminated old runtimes and any other host
writers. Their transport provenance and complete live inventory remain setup
prerequisites, not facts established by hashing a file. No new issuer, privilege,
approval boolean, automatic enforcement probe or native mutation entrypoint exists.
Quota excludes new/replacement Pods only while actually enforced; it cannot exclude
another existing writer, competing exec or a principal changing/removing the hold.
This route assumes verified exclusive supervised ownership, not an adversarial
administrator/root. Unknown node termination or competing ordinary access denies.

Provider inventory must be explicit even when empty; unknown is never absent.
Every affected operation maps either to terminal plus settlement receipts, or to
its exact stop-only guardian identity/assignment, lease and reserved exposure.
Aggregate exposure including reserves remains at most 5000c. The old unconditional
PVC-guardian witness is replaced by this applicability mapping, not waived via a
synthetic guardian UID. Dispatch must still be drained separately; provider work
may remain only with its actual independent guardian and accounted exposure.

All DB probe, phase dispatch and remote-result reconnect paths require a captured
hold, current prerequisites and enforcement evidence. A shared nonblocking local
lock serializes these DB windows against release; it is not the infrastructure
fence. Release is a distinct UID/resourceVersion/annotations/full-spec tested JSON
patch to `hard.pods: "1"`, only after the real phase-linked pre/post exports and
acknowledged terminal maintenance handoff. Activation requires captured release.
No automatic release occurs on expiry, failure or reconnect. A durable release
claim immediately prohibits further DB exec, including uncertain release ack.
Lost acknowledgements retain failure/claim identity; observed poststate never
creates an acknowledgement, adopts a replacement or allows replay.

The handoff exports must additionally match a named custodian's destination
identities, authenticated topology and retention through recovery (no automatic
cleanup at expiry). Complete materialized, fsynced pre/post bundles occupy their
storage already: they do **not** require a second reservation for their byte size,
nor guessed sizes for nullable manifest entries. The owner pins the materialized
receipt; release rechecks every file's identity, actual bytes, complete inventory
and phase/restore lineage against it, not a caller-supplied byte count/hash alone.
Reconnect still revalidates retained bytes after expiry; expiry never releases
admission or deletes custody. Short retention, aliasing or changed bytes deny.

Outstanding writes remain separate: this finite release writes at most four
256KiB-bounded receipts (prepared, claim, failure, acknowledgement), using the
existing durable bounded writer. Claim and unknown-ack fallback are materialized
and fsynced before the quota call. This is not a future-space guarantee or an
external 1MiB reservation: later ENOSPC/I/O failure remains unconfirmed and never
allows replay, activation or further DB exec. This bound covers only release receipts,
not later activation/recovery writes, which require their own admission/capacity.

Release acknowledgement data is written create-only to a fixed same-directory
staging inode and file-fsynced before create-only hardlink publication, then the
directory is fsynced. The staging link is retained as immutable evidence, not
deleted or reused; it is the same bytes, not a fifth 256KiB payload allocation.
This ordering prevents publication of partially written or file-fsync-failed ack
data. It does **not** make visible publication proof of final directory-fsync
success. A successful in-process apply returns only after its writer's fsyncs and
readback complete; a fresh read-only reconnect reports valid visible ack bytes as
`observed_unconfirmed`, otherwise `unknown_ack`, always without replay or writes.
No same-filesystem completion marker can certify its own last fsync. Promotion to
confirmed release requires actual independent native completion evidence.
`maintenance_completion` implements a narrow reader for an explicitly supplied,
already-authorized pinned runtime connection; absent connection denies. It uses
V2 `session.message.get`, GET `/api/session/{sessionID}/message/{messageID}`,
whose `data` is the assistant message, with tool parts under `content` and
completed `state.input`/`state.content`. Original runtime/session/message/call,
tool/build, operation/plan/claim and exact structured post-fsync/readback result
must match; current quota/custody and unexpired prerequisites are rechecked.
The invocation is the existing `MaintenanceEntrypoint.effect(operation, action,
expected_action_sha256)`, not a phase/plan/build wrapper. The reader derives its
exact arguments from identical retained upper prepared/claim packets, checks the
upper packet's lower digest against the exact release claim, and checks the
entrypoint's returned structured result against both claims and ack. The lower
claim is not rewritten to insert native identities: upper/lower digests therefore
remain acyclic. Tool/build and runtime/session/message/call are trusted integration
context, not request arguments. No current native handler supplies this context;
caller-selected identifiers alone are not production provenance. Local integration
tests run real entrypoint prepare/effect and hold/journal/custody code with synthetic
owner loading and a fake runtime message containing the actual returned result.
Model text, generic completion, pasted receipts, provider-executed flags,
errors, truncated/oversized responses cannot promote. No caller boolean/hash,
new finalize effect, expiry cleanup or quota replay fills this gap. The published
API schema and fake authorized-runtime tests are not installed-runtime proof.
No production runtime connection or native per-effect admission is installed
by this change; exclusive ownership, drain and topology producers remain required.

For the declared maintenance-Pod/PVC failure domain, independent durable private
Mac storage outside the source/PVC suffices; off-host custody is not a blanket
requirement. Local filesystem device/inode/hash checks do not prove this topology,
ownership, actual capacity or survival of Mac/physical-host loss. No such broader
failure-domain acceptance is claimed. Release opens Pod admission, not research
authority; final compatible58 single-writer/application/research reconnect remains
separate acceptance. No maintenance DB exec is allowed after opening admission.

## Bounded future one-time action packet (not authorization)

Historical native87 observation: Deployment UID
`06a087a0-4e3b-4591-a2d1-aaaaa15c5006` rv4279653; PVC UID
`b32ecff3-2ec1-41af-985d-a107797b8c27` rv3464970; PV UID
`da32482f-4159-41f1-9557-3329c09736e7` rv3464968. These active-era versions
will change on stop; they are not fabricated post-stop admission pins.
Active image SHA256 `04a80b00a45f5a5cde838c95b4fc15665c0dc40c22cc8a6c5c60962f19d05112`;
reviewed candidate image SHA256
`fb6a9f420de0f49bb7ccf096eb44ac302a59d1dc2d77ea67b708b171a54a3b8e`.
Preserve IMAGE85 proof; do not rerun image/build discovery.

Before any action request, populate missing exact fields:

1. Separate stop/outage scope: affected admission and clients/worker identities,
   operator subject, guardian identity/lease/epoch, start/deadline, stop-only grant
   reference, external work/custody readback, new-spend reservations/liability and
   shutdown margin within 5000c. Historical zero spend is not a launch prerequisite.
2. Separate observer provisioning scope, if needed: exact Pod spec/image/script
   digests and target PVC. New observer UID/resourceVersion, post-stop Deployment
   resourceVersion/generation and independently verified host/admission fence are
   missing. This collector cannot create or authorize the observer or fence.
3. Prebackup/restore-proof scope: freshly observed schema/incarnation/DB hash and
   path binding; exact matching old backup CLI digest, reserved capacity,
   destination/custodian identity, byte manifests and durable independent readback;
   held restore validation identity/result/digest. Backup can write health state:
   retain its resulting DB hash as the next phase pin, not the obsolete input hash.
4. Migrate scope: exact new CLI/image, schema56→58 operation/plan digest, retained
   prebackup receipt and phase DB hash, and exact supervised action identity/claim
   (not yet prepared); native permission requested on that exposed effect, not a
   fabricated grant or approval ID.
5. Validate/activate/reconcile scope: matching-version restore proof, retained
   postmigration hash, compatible58 forward reader and CAS Deployment spec/version;
   guardian continuity, sole writer and custody readback. No schema56 rollback once
   migration has been claimed, even if result acknowledgement was lost.

Preclaim abort is allowed only with independently unchanged DB and matching
incarnation/path/target; postclaim recovery is compatible58 forward-only with
admission held. Lost-ack reconciliation reads retained receipts; it never replays
an action. No tools are registered and the existing native gate remains closed.

Eventual human-facing permission wording (template, not an approval request now):
"Permit ONLY [operation/phase/action digest] on [cluster/namespace/Deployment,
PVC/PV UID + resourceVersion] using [exact CAS/body or script/image digest],
DB [path/incarnation/phase hash], before [outage deadline], with [stop/fence,
backup/restore custody and liability receipt identities]. One effect; no automatic
next phase, grant/delegation or spend. On uncertain acknowledgement inspect the
same claim/remote receipt; never replay. After migration claim use compatible58
forward recovery only, with admission held." All bracketed live pins are missing.

Tests are real disposable SQLite plus scripted Kubernetes command/readback cases,
not live cluster, filesystem isolation, guardian, backup or authority evidence.
