# ADR 0126: an agent can read one snapshot's file list

Status: accepted. Addendum to ADR 0055 (MCP steering tools) and to the agent-reads work that
added `tessera_list_snapshots` and the other metadata reads. No schema change.

## Context

An agent holding `snapshot:read` could list a project's snapshots and see each one's id, digest,
size and file count, but not which files a snapshot holds. It could pin a snapshot in a spec, yet
it could not write a stage that reads a named file from it, because the file names and their
digests were not visible to it.

## Decision

1. `GET /projects/{project_id}/agent-snapshots/{snapshot_id}` (MCP tool `tessera_get_snapshot`)
   returns one snapshot's file list. It is read-only and needs `snapshot:read`, the same scope as
   `tessera_list_snapshots`.
2. The response holds the snapshot id, digest, creation time, total bytes, file count, recorded
   data class and licence labels, and per file the relative path, size, sha256 and recorded
   classification.
3. The list is capped at 1000 files. When the snapshot holds more, `truncated` is true.
4. The response never holds the manifest root (a host path), provenance, exclusions or file
   bytes.
5. The tenant and project checks are the catalog's: the credential must be in the snapshot's
   organization and project, and an unknown, cross-project or cross-tenant snapshot returns 404.
6. Nothing else changes. No tool stages a snapshot, releases data or reads bytes; staging,
   release and artifact bytes stay human steps.

## Consequences

- File path names and their sha256 digests become visible to any credential holding
  `snapshot:read` in that project. An operator who considers file names sensitive should not
  enrol that scope.
- An agent can now write a stage that reads a named file and pin it by digest. A person still
  approves the spec digest and still performs staging and release.
- A snapshot with more than 1000 files shows only the first 1000; the agent sees `truncated` and
  the true `file_count`.
