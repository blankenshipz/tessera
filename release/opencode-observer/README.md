# Pinned isolated observer source deliverable

This directory is the redistributable handoff, not an ignored worktree dependency.
The runtime owner supplies baseline.json, LICENSE and observer.patch after
its final isolated source checkpoint. Packaging does not discover or copy a
checkout, private guard, credentials, active configuration or account data.

## Commit-pinned mode (existing v1)

baseline.json is a closed object. Use it **only** with a genuinely identified exact
upstream commit, never a fabricated 40-hex identifier for a nongit snapshot:

```json
{
  "schema_version": "tessera-opencode-observer-baseline-v1",
  "repository": "https://github.com/anomalyco/opencode",
  "commit": "<exact 40 lower-case hex baseline commit>",
  "license_sha256": "<SHA-256 of supplied upstream LICENSE bytes>",
  "patch_sha256": "<SHA-256 of supplied observer.patch bytes>"
}
```

The repository value above is illustrative; use the actual public redistributable
baseline. The owner must confirm redistribution/license and that the patch contains
only approved isolated observer/SDK/harness changes, with no secrets/private guard
source. Hashes and path checks are packaging integrity, not proof of this review.

Stage an approved public bundle with `dev/build_release_candidate.py stage-observer`
into a **new** directory, inspect it, then place the three verified files here under
the parent's source freeze. No automatic private worktree capture is provided.
A final build fails closed until these files exist and match their pins. A labelled
development snapshot may omit them; it is not the final pinned source candidate.

To reproduce later: obtain the pinned baseline through separately authorized means,
verify its commit/license, apply observer.patch with `git apply --check` first and
run the isolated owner's exact SDK/harness checks. Never apply it to or restart the
active coding service as part of packaging. Candidate packaging is not admission,
an accepted release, installed proof or live planner/critic evidence.

## File-pinned copied-snapshot mode (v2)

For a nongit isolated snapshot, supply the same three files plus the **exact** extra
file `baseline.tar.gz`. Do not invent a commit. The closed baseline.json schema is:

```
schema_version: "tessera-opencode-observer-baseline-v2"
mode: "file-pinned-snapshot"
source_origin: public HTTPS URI (declared source, no userinfo/query/fragment)
source_version: bounded declared version text (or "unknown")
source_reference: bounded declared snapshot/reference text
upstream_verification: "not_independently_verified"
source_date_epoch: explicit integer epoch
baseline_archive_sha256: SHA-256 of exact baseline.tar.gz bytes
baseline_files: {original relative file path: SHA-256 of actual original bytes}
licenses: {original LICENSE path: SHA-256} (subset of baseline_files)
patch_sha256: SHA-256 of exact observer.patch bytes
```

No `commit`, approval boolean or independently-verified claim is accepted in v2.
This mode proves exact copied original bytes, **not** independent upstream identity.
`licenses` must include root `LICENSE`, whose bytes match the outer LICENSE file.
Additional approved LICENSE members can be pinned in the archive. The owner still
must confirm redistribution scope/content; file hashes do not establish licensing
or secret-free source by themselves. A sparse original-file baseline is not a full
upstream checkout or independently buildable OpenCode distribution.

Archive contract: deterministic gzip header (no filename/extra flags, mtime epoch),
regular files only, tar uid/gid 0, empty uname/gname, mode0644, mtime epoch and no PAX
metadata. Maximum compressed size4MiB; <=64members, <=1MiB/file, <=8MiB total data.
Members must exactly equal baseline_files; duplicate/extra/missing members, links,
traversal, private/auth/env/dependency/cache/worktree/receipt paths fail closed.
Never auto-copy a worktree or dependency tree into this archive.

Public path prefixes are `packages/core/src/`, `packages/opencode/src/`,
`packages/plugin/src/`, `packages/sdk/src/`, `packages/sdk/js/src/` for .ts/.mjs
source or LICENSE; root LICENSE and explicit public `subscription-guard.mjs` or
`plugins/subscription-guard.mjs` are also supported. See dev/observer_snapshot.py
for the exact path filter. If the owner uses a different approved public layout,
hand back its exact path/schema rather than silently broadening the allowlist.

Patch old-side files must exist in the actual sparse baseline; new files must stay
within the approved public prefixes. Packaging validates pins/path/bounds, not patch
hunk application. Parent must run `git apply --check` against the sparse original
bytes and the owner's exact isolated tests. Use the same staging CLI; v2 copies
baseline.tar.gz too. Both source archive and wheel ship this bounded archive alongside
baseline.json/LICENSE/observer.patch. Other tarballs in release are excluded.

## Actual reviewed public layout (27 originals -> 36 targets)

The staged final FILEPINNEDSPARSE v2 object additionally requires
`license_path:"LICENSE.opencode"` and `target_files` (path->SHA256). Exact reviewed
member sets are enumerated in dev/observer_snapshot.py:27 originals,36 targets,
including manifests, public harness quota helpers/plugins, both licenses and all
reproduction tests. This is not a blanket harness-directory exception.
Both `LICENSE.opencode` and `harness/LICENSE` remain byte-identical in the archive;
outer LICENSE contains actual LICENSE.opencode bytes with original identity declared
by license_path. No invented root LICENSE member is required for this layout.

Preserved archive metadata: epoch0, uid/gid0, empty uname/gname, regular files and
no PAX. Only exact `harness/bin/codex-quota` may use mode0755, others0644. Gzip
epoch0/flags0 are deterministic, not source creation-date proof. Snapshot epochs
accept0..2^32-1; outer wheel/source builder epoch remains separately pinned.
All link/traversal/duplicate/private/auth/cache/dependency/oversize guards remain.

Origin https://opencode.ai is a declared public product origin, **not** verified git
provenance. Source version2.0.18; source_reference retains known clean **local**
harness commit57a921a40a189004d345bd8e9738e28962de6c2b. OpenCode upstream commit
is unknown/null in the original manifest; independently verified provenance stays
false. Final owner target index:
`6cb7fc98728449d640b9711389ef036de78735de74b0da8d60ccfe2d304c9e96`.

dev/stage_observer_deliverable.py maps explicitly supplied public manifest, original/
target indexes and archive/patch into the four shipped files. It checks original
hashes/sizes, closed provenance, both actual MIT grants and high-confidence
credential-shaped text without printing content. This is not a general secret-free
proof; no private counterpart or broad worktree copy occurs.

tests/test_observer_deliverable.py depends **only on shipped bundle files**. It
validates27/36 pins, natively applies the patch to a fresh exact baseline, compares
all36 target hashes/index and rejects dropping reproduction tests or either license.
Runtime owner's52Bun+4Python behavior receipt is separate, not rerun here. The sparse
archive supplies reproduction source, not dependencies/full OpenCode. Final full
Tessera capture/build still waits for parent freeze and exact applicable gates.

## V7 native source closure is not yet staged (2026-10-07)

The separate v7 isolated source closure now contains 79 pinned inputs: the prior 62
plus 17 actual public runtime originals with recorded license origins. Its native
namespace digest is not a raw JSON inventory-file digest. This is a source-linkage
checkpoint, not a full OpenCode checkout or a package candidate. The public staging
contract and source-bound reproduction tests above still describe the 27/36 layout;
they have not been updated to stage or validate the 79-input closure. Do not claim
v7 package/source parity, installed SDK proof, or upstream commit provenance until
the stage allowlist, bundle, licenses, patch reconstruction, and exact-source gates
are reconciled and rerun on the frozen candidate. The isolated 79-input handoff is
not itself shipped by this directory. See the maintainer
[source-deliverable runbook](../../docs/opencode-source-deliverable.md).
