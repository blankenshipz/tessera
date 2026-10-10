# Isolated OpenCode source deliverable and parity checks

This is a maintainer runbook for the optional isolated OpenCode observer/SDK source
bundle. It does not authorize changing or restarting the active coding service,
installing the bundle, invoking a provider, or declaring Tessera admission. The
runtime-v8 source closure is integrated into package inputs. Changed inputs need
a fresh freeze, candidate build and applicable checks; prior receipts cover only
their original artifacts.

## Current boundary

- The public source snapshot identifies OpenCode version 2.0.18, but has no Git
  metadata or independently verified upstream commit. Its original source identity
  must remain `unknown`/unverified; do not invent a commit hash.
- The historical 27-original/36-target FILEPINNED v2 bundle remains unchanged in
  [`release/opencode-observer/README.md`](../release/opencode-observer/README.md).
  The separate `release/opencode-observer/runtime-v8/` bundle is a closed
  `tessera-opencode-observer-runtime-v3` layout with 50 pinned originals and 79
  target files across 14 shipped files. Its bundle namespace, target namespace and
   external inventory-file digest are distinct digest constructions. Historical
   local evidence is indexed as `OBSERVER-RUNTIME-V8-SOURCE` and
   `RC2-PACKAGING-PHASE2`; those IDs identify records retained outside this source
   distribution and are not required package inputs or public proof links.
- The source closure repairs import linkage only. It is not a complete upstream
  checkout, full package/typecheck/build, installed SDK, or verified public release.
  The exact native files, license-origin records, test gates and exclusions are
  tracked by the release owner in private source receipts, not shipped in the
  archive.

## Required parity sequence before staging a candidate

1. Wait until all source/package writers report quiescent. The maintainer freezes a
   source inventory with exact relative paths, per-file SHA-256, byte sizes, license
   origins and an explicitly defined aggregate digest. Exclude credentials, private
   guard source/copies, auth/runtime state, dependency trees, caches, worktrees and
   receipts.
2. Use the closed runtime-v8 schema and exact 14-file stage allowlist already
   integrated into package rules; do not broaden it recursively. Include the two
   actual MIT license byte sets and all pinned public inputs. Keep upstream Git
   identity null/unverified because the source snapshot has no verified commit.
3. In a new isolated directory, verify every original hash/license, apply the patch
   to those exact bytes, and verify every target path/hash. Reject extra, missing,
   linked, traversal, secret-like, private or unreviewed files. An index/hash is
   integrity evidence, not proof of source authenticity, legal redistribution, or
   absence of secrets.
4. Build the deliverable twice in fresh owned directories and compare archive,
   patch, manifest, baseline and target-index digests. Keep complete command,
   stdout/stderr, exit status, source inventory and skips/failures in private
   receipts. Keep receipts out of shipped artifacts.
5. Run the stage/package inventory and source-only reproduction tests against the
   exact frozen target. Then rerun the applicable isolated SDK/native and Tessera
   source gates on that exact target. Package checks, source tests, installed
   compatibility, and live admitted research are different evidence classes.

The shipped runtime-v8 reconstruction/link/verification tools and public dependency
lock operate on a fresh owned directory using only the 14 shipped files. Package
integration validates the closed bundle and required inputs. Each candidate needs
its own frozen inventory/build/install and applicable gates; historical
source-handoff and package checks do not cover later changes.

## Evidence boundary

SDK HTTP/WS and installed Python checks using synthetic identity/quota/private-owner
inputs do not prove real entitlement/billing, production credentials, active
service compatibility or live provider dispatch. Preserve prior receipts at their
original revisions; they do not cover later source changes. See
the [release checklist](release-checklist.md) for the current freeze and remaining
acceptance boundary.
