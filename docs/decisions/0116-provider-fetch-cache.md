# ADR 0116: An optional provider-side cache of pinned public files, keyed by digest

Status: accepted and implemented. No schema change. Extends
[ADR 0097](0097-provider-side-fetch-of-pinned-public-artifacts.md); the host rules, the pins and the
data-destination invariant of [ADR 0047](0047-agent-routed-data-destinations.md) are unchanged.

## Context

ADR 0097 moved the download of large pinned public files onto the executing provider and gave the
local worker a cache keyed by digest. It left a Salad-side cache out ("Salad containers are
ephemeral"). A provider may nevertheless offer storage that outlives one container (a persistent
volume, for example). When it does, a rerun on the same provider should not download the same
multi-gigabyte public file again. A cache must not become a way around the digest, a place that
holds anything but pinned public bytes, or a path that carries bytes to the control plane.

## Decision

1. **Per-item flag, same source of truth.** The `cache` flag of a `public_artifact` source (ADR 0097)
   also reaches the in-container runtime (`gpu_runtime_v1`): its hash-pinned manifest may carry an
   optional `cache` boolean on a URL item of kind `file` or `wheel` (never `entry`: code is not kept
   between runs). Admission requires every item's flag to equal the approved spec's flag for that
   file and requires an inline manifest (a manifest carried in the input cannot be compared at
   plan time), so a reviewed spec decides what may be cached and the workload contract cannot widen it.
2. **The provider supplies the storage; Tessera does not.** The runtime uses a directory only when the
   provider environment names one with `TESSERA_FETCH_CACHE_DIR` (in the reviewed contract `env` or the
   image) and `TESSERA_FETCH_CACHE_MAX_BYTES` is the store quota (default 0: lookups only, nothing is
   stored). The directory is usable only when it is absolute, a real directory (no symlink), owned by the
   runtime's uid, mode 0700, and outside the work, output and input trees. Otherwise the cache is
   `unavailable` and the file is fetched normally. Tessera creates and mounts no storage.
3. **Keyed by digest, verified on every reuse.** An entry is a regular, single-link file named by the
   file's sha256. A hit re-hashes the whole entry and checks the size against the pin's bound before
   anything uses it; a mismatch or a wrong size deletes the entry and the file is fetched (fail
   closed). A hit is copied to the run's file, never linked, so each mounted file keeps one link.
4. **Stored only after verification.** A fetched file is stored after its sha256 and size verify, by
   temporary file, fsync and rename, within the quota, evicting the oldest completed entries first. A
   storage failure never fails the run.
5. **Never transferred.** Cached bytes are not staged, uploaded as outputs, or sent to the control
   plane; the cache directory is not part of any input or output tree. Only pinned public bytes are
   cached; a staged (private) input never is.
6. **Receipt.** Each `public_fetch` file entry gains `cache`: `hit`, `miss`, `unavailable` or `off`
   (the flag was not set), and `bytes` as before; it is null, with the file `declared_only`, when no
   provider report is available. The `public_fetch` object gains `cache_requested: true` only when a
   source asked for caching, so receipts of specs without the flag are unchanged. Local-worker rows map
   their `cached` boolean to `hit` or `miss`.
7. **Partial files are locked, and a stale one is reclaimed.** The writer holds an exclusive,
   non-blocking `flock` on its `.<sha>.part` file for the whole copy; the kernel drops the lock when
   the process ends, even when it is killed, so a part file whose lock can be taken is not being
   written. At the start of `store()` the runtime reclaims stale part files in the cache directory: a
   regular file, owned by this uid, with one link, not modified for at least 60 seconds, whose lock
   can be taken. A partial that is not reclaimed (locked by another process, younger than 60 seconds, owned by
   another user, hard-linked, or unreadable) is treated as live and is never
   removed; its current size counts toward the quota, and a stale reclaimed partial does not. Eviction
   still removes only completed 64-hex entries, oldest first. A live partial for the same digest makes
   `store()` return without storing. `store()` stays best effort and never raises. A lock is used
   rather than only an age check because an age guess can remove a slow live writer. The 60 second
   minimum age exists only so that a just-created file whose writer has not locked it yet is never
   reclaimed. Before this, a container killed mid-copy (preemption, a deadline kill) left its
   `.<sha>.part`; every later store of that digest then failed with EEXIST, so the digest was never
   cached again, and the leftover bytes were outside the quota.

## Consequences

- A rerun on a provider with persistent storage skips the download, and the receipt says so with the
  digest and byte count.
- With no persistent storage, `cache: true` is a harmless no-op: `unavailable` and a normal fetch.
- An older runtime image refuses the new manifest key (the item's key set is exact) before the claim
  for an inline manifest, so a spec that sets `cache` needs a current image.

## Limits

- Persistent storage is the operator's: the volume, its size, its access and its clearing. A volume
  shared with other workloads is outside this decision; the digest check is what keeps a poisoned
  entry from being used.
- A hit proves the bytes match the pinned digest, not that they were ever on this provider's storage
  intact in between beyond that check; the quota and eviction are best effort.
- Storing an entry does not check the stage deadline and does not re-hash the bytes while copying them
  from the verified file; the copy is bounded by the quota but not by time. A bad copy is not detected
  at store time: it is caught on the next hit, which verifies the entry's digest and deletes a
  mismatch before anything uses it.
- The quota and oldest-first eviction apply to this provider-side cache only. The local worker's cache
  of pinned files (ADR 0097) has no size bound and no eviction. It is operator-owned storage, and its
  files are digest-checked each time one is used.
- The provider's own report supplies hit and miss; without it the receipt says `declared_only`.
- The reclaim needs a filesystem where `flock` works. On one where it does not, `store()` cannot prove
  a part file is stale and leaves it, which only costs the cache for that digest.
- A live partial counts toward the quota and is not evictable, so a quota smaller than the artifacts in
  flight can leave `store()` skipping.
- No live Salad run was made; behaviour is tested with fakes and the runtime against a local directory.

## Discard rule

A cache entry is deleted only on a confirmed fingerprint mismatch: the whole entry was streamed to its
end within the pin's `max_bytes` and its sha256 differs from the pin. Every other failure is a miss for
that call and the entry stays: an I/O error (EMFILE, EACCES, EIO, a vanished file), a size above this
pin's `max_bytes` (a rerun with a smaller bound does not delete a good entry), a non-regular, symlinked
or hard-linked entry (not trusted, not deleted; `store()` replaces entries by rename), and a size that
changed under the reader. The mismatch delete is skipped if the entry was replaced since it was opened.
The `.part` file is removed on every failure path of the process that wrote it, and unverified bytes
are never served; a part file left by a process that died is reclaimed by a later `store()` (decision 7).

## Tests

`tests/test_gpu_runtime_fetch_cache.py` (the runtime: hit, miss, corrupt entry, wrong size, unusable
directory, quota and eviction, copy not link, nothing cached unless flagged or for entries),
`tests/test_campaign_public_artifact_cache.py` (admission equality with the spec flag, the inline-manifest
requirement and the receipt fields).
`tests/test_gpu_runtime_fetch_cache_integrity.py` (the discard rule above).
`tests/test_gpu_runtime_fetch_cache_partfile.py` (partial files: stale reclaim, live and young partials
kept, partial size counted toward the quota, a live partial for the same digest skipping the store).
