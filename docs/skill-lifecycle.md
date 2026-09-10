# Skill lifecycle

Schema v16 stores project-scoped, immutable metadata-only skill snapshots.
`POST /projects/{project_id}/skills` records validated `SKILL.md` metadata, a
deterministic manifest, and digest from bounded `snapshot_skill` validation. It
stores no skill bytes and never runs setup code, scripts, or a runtime loader.

Humans create worker-targeted, expiring releases at `POST /skills/{id}/releases`
and revoke them at `POST /skill-releases/{id}/revoke`. A pinned plan needs every
digest to have a matching live release. Grant/lease admission freezes and rechecks
those releases, fencing stale plans and revoked or expired releases.

`local-cpu-v1` package preparation and CPU runtime still reject nonempty pins:
skill materialization and verification are not implemented.

Skills may describe or request a target, provisioning change, or credential access,
but are never approval principals. They cannot approve immutable execution or
provisioning plans, materialize Kubernetes Secrets, or receive/use raw `kubectl`
or cloud credentials. These are planned authority boundaries, not runtime support.
