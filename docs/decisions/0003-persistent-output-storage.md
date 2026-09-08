# 0003: Persistent, bounded output storage before automatic collection

Status: accepted contract; storage backend and runtime activation pending.

## Problem

The local CPU adapter uses a read-only container root and a bounded `/work` tmpfs.
Docker [removes tmpfs contents when the container stops](https://docs.docker.com/engine/storage/tmpfs/).
Its contents therefore cannot support collection after cancellation, OOM,
container exit, or a worker restart that misses the exit. Current cancellation
uses Docker KILL and must remain independent of artifact transfer.

Replacing tmpfs with an ordinary writable directory would preserve files but
remove the enforced storage limit. The control plane's artifact upload limit
bounds retained artifact bytes; it does not limit container writes, inode usage,
or accumulation of abandoned workspaces.

## Decision

Before enabling automatic post-exit collection, require an explicitly versioned
persistent output-storage capability with actual enforcement evidence. Start
with one operator-provisioned, fixed-capacity storage slot on a supported local
Linux Docker host. Runtime code must not invoke privileged mount helpers or
silently accept an unconstrained directory.

Admission must verify filesystem/mount identity, byte and inode capacity,
`noexec/nosuid/nodev`, UID 65532 write access, supervisor custody, and absence of
nested mounts. Backend-specific enforcement probes must demonstrate that a real
container cannot exceed the slot's bounds. A configuration boolean, free-space
check, or quota measured only on a Docker Desktop host is insufficient: bind
mounts are interpreted on the [Docker daemon's host](https://docs.docker.com/engine/storage/bind-mounts/).

The worker now has a read-only Linux inspection probe for an already-provisioned
dedicated output mount. It returns the existing version 1 contract only after
non-following path/replacement, ownership/mode, mount identity/options, nested
mount, capacity, and UID 65532 access checks pass. It never mounts, resizes,
changes custody, writes a test file, or invokes a privileged helper. macOS
Docker Desktop and every non-Linux platform are non-admissible from host facts.
This is not the backend enforcement probe: decisive Linux Docker-host byte/inode
exhaustion, container exit, and worker-restart evidence remains required.

Freeze the allocation identity and capacity in a new versioned runtime contract
before provider submission. Preserve existing operation IDs and journals; do not
retrofit old operations with new writable mounts.

Reserve the slot across worker crashes, unknown termination, cancellation,
incomplete collection, and upload retries. Require explicit verified reclamation
before reuse. Bound storage for sealed copies separately so copying retained
outputs cannot bypass the aggregate storage limit.

A wrapper or log stream may eventually support best-effort checkpoints, but it
is not the durability boundary for this slice. A wrapper can be interrupted by
KILL or OOM before it exports data, and the existing rotating log limit is not an
artifact transfer protocol.

## Collection boundary

A caller must obtain a fresh provider observation matching operation identity and
confirm termination before initial collection, or resume an already sealed
immutable snapshot. A recovered journal's terminal phase alone is not evidence
of current provider state.

`collect_outputs` is a low-level reader for a caller-established stopped workspace.
It opens only historically declared paths using anchored directory descriptors,
rejects links, aliases, special files, cross-device traversal, and oversized files,
and returns immutable bytes only when the entire read succeeds. It neither
establishes provider termination nor attests storage enforcement.

Synthetic fixture coverage now has a local SQLite sealed-output journal under a
private synthetic runtime root. It requires a fresh matching control-acknowledged
termination observation, seals immutable bytes/digests and a local manifest before
the existing resumable transfer, and resumes lost upload/manifest responses after a
worker restart. It is not wired to the CPU runtime or an output-storage backend.
The explicit `tessera_worker.development_storage` helper is available for local
synthetic retention through a fixed development-only path and marked stopped
workspace input. It is test/recovery tooling only and does not satisfy backend
activation, runtime admission, or scientific acceptance.
Next real integration steps remain verified reserved staging capacity, recovery,
and resumable transfer outside the cancellation loop. Missing or unsafe outputs
remain collection failures; neither file integrity nor provider exit establishes
scientific success. The control plane retains attributable late output in quarantine.

## Activation evidence required

- Real container write, exit, worker restart, and unchanged upload into quarantine.
- Cancellation and disconnect retain collectible files without delaying stops.
- Byte and inode exhaustion remain within the slot, including sparse files and
  undeclared output floods; another operation cannot allocate the reserved slot.
- Mount replacement, links, special files, aliases, and permission failures cannot
  escape custody or block the collector indefinitely.
- Sealing and uncertain-upload crash points resume the same operation and bytes.

The current macOS Docker Desktop environment has passed synthetic isolation,
read-only input, control revocation, and duration probes. No persistent output
quota backend has been verified there.

### macOS fixed-image feasibility result

A disposable 32 MiB read/write UDIF image with case-sensitive journaled HFS+
was mounted as the ordinary user with `noexec,nosuid,nodev`. A synthetic Docker
container used UID 65532, a read-only root, no network, dropped capabilities and
`no-new-privileges`, with only the image's workspace shared using `:consistent`.
Inside Linux, `/work` reported `rw,nosuid,nodev` on virtiofs, without `noexec`;
direct execution of a newly written executable script succeeded with exit 0.

This candidate therefore fails the current profile's execution restriction.
Capacity/exhaustion and recovery acceptance were not attempted after that failure.
The synthetic container exited and was removed. Normal detach reported a busy
volume; after verifying no container retained the fixture mount, its owned image
was detached and the temporary fixture removed. Host mount options alone cannot
admit this Docker Desktop backend, and cleanup behavior also needs a supported
recovery contract before activation.
