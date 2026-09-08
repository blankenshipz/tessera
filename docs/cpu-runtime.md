# CPU runtime integration

The local CPU components now connect control-owned packages, input delivery,
verified read-only mounts and the durable supervisor. Integration tests use the
real authenticated API and a simulated provider. No unattended dispatch command
is enabled, and this is not end-to-end research acceptance.

`POST /worker/leases/{lease_id}/package` takes `expected_epoch` and an idempotency
key. It rechecks live worker/grant/release authority and reconstructs the approved
plan from immutable history, verifying the plan checksum. The package freezes
the image, input digest, command, output limits, task revision, lease epoch and
hard deadline. Its operation ID is deterministic for the lease. The preparation
event does not claim provider submission.

Retries preserve the package digest while returning current lease expiry.
Expired, revoked or changed authority cannot recover a package through a cached
response. The initial profile rejects network access, pinned skills not yet
supported by the runtime, and output bounds above 16 MiB.

`CpuRuntime` holds a private journal, obtains the package and released inputs,
and persists a complete descriptor before the supervisor can submit anything.
Restart uses the same operation identity and input tree. A shared private worker
input root binds each lease to one journal; a second journal cannot restart the
same local operation. Workers must retain that root and binding on restart.
Deleting local journals or cloning worker identity onto independent state roots
is outside this guarantee and requires enrollment/recovery fencing.

The Docker adapter accepts only allowlisted digest-pinned images. The only
permitted host mount is the exact verified snapshot directory at `/inputs`,
read-only, beneath an explicitly configured `0700` supervisor root. It rechecks
manifest integrity, exact file membership, hashes, sizes, modes, links and path
aliases before create and start. It bounds reads and directory depth. Files are
`0444` and snapshot directories `0555` so the container's non-root UID can read
them; the private parent remains outside the container. Input custody must stay
trusted between verification and Docker mounting. There are no socket or
credential mounts.

Each supervisor tick rechecks current control authority. Control failure, expiry
or revocation requests cancellation of possible execution. Confirmed exit and
unknown termination remain distinct. Lease renewal does not reset operation
duration. Reports contain attributed provider observations; they never infer
scientific success or infrastructure charges.

`HttpControlChannel` uses normal HTTPS verification, or HTTP to literal loopback
addresses only. It binds a worker credential to one origin, rejects redirects,
disables environment proxies and cookies, and bounds requests to 1 MiB and
responses to 25 MiB. The default total response deadline is five seconds. An
unresolved request blocks further requests on that channel without spawning more
threads. Its daemon thread cannot hold up supervisor shutdown. A timed-out request
may still commit at the server; callers must retain their idempotency keys and
reconcile, never infer that a timeout means an action did not occur.

Synthetic live Docker probes now verify isolation, read-only released inputs,
control-plane revocation, and duration stops. Run `scripts/check_cpu_runtime.py`
and `scripts/check_control_runtime.py` with the native environment and the pinned
image already available. The latter uses a temporary loopback HTTP service and
records actual provider observations; it does not collect outputs or settle costs.

Worker trust/build enrollment, capability probes, persistent output storage and
collection/validation, pinned skill execution, accounting feeds and restore
fencing remain required before enabling unattended research execution. A private
test fixture and a successful simulated stop are not proof of those guarantees.

See [the persistent output-storage decision](decisions/0003-persistent-output-storage.md)
for the quota and custody gates before collection can be enabled.

## Persistent output-storage admission contract

A package request can explicitly request persistent output collection only with
the `persistent-output-storage` contract version 1. The trusted supervisor's
capability declaration must name the allocation, filesystem and mount and prove
fixed byte and inode capacity, `noexec`/`nosuid`/`nodev`, UID 65532 write access,
supervisor custody, and no nested mounts. Missing, malformed, unverified, or
undersized declarations fail package admission before input staging or any
supervisor/provider call. The validated identity and capacities are frozen into
that new package and digest; a changed declaration cannot replay it.

This is only a synthetic, fail-closed capability contract. Tessera does not yet
activate a storage backend, create a writable mount, reserve/reclaim a slot,
automatically collect outputs, release quarantine, or accept results.

Local development may explicitly retain synthetic stopped-workspace outputs with
`DevelopmentArtifactStorage` in the isolated `development_storage` module. It
uses durable sealing and the existing quarantine upload path, but does not
satisfy this backend contract, enable `CpuRuntime`, admit packages, mount output
storage, dispatch pilot/real work, release quarantine, or accept results.
