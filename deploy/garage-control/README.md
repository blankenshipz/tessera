# Garage control plane (independent of development and remote-agent profiles)

This fixed Compose project is `tessera-garage-control`, service `control`, file
`deploy/garage-control/compose.yaml`. The separate `tessera-garage-operator`
stdio tool installs these exact bytes at `/srv/tessera-garage/compose.yaml` and
selects this project and only the `control` service, not root Compose or
`deploy/remote-agent-platform/docker/compose.yaml`. This source profile has **not**
been deployed; it does not prove the garage host is reachable or has available ports.
The operator's inspect result leaves `state_present` unknown because the SSH
identity cannot traverse the private 0700 state directory; deploy checks from
the networkless maintenance container instead.

## Admission before deployment

Inspect garage Docker/port ownership, the current local `.tessera` state, backup
custody and the existing host-side trusted TLS proxy. Do not replace existing state
or initialize over a populated directory. On the **server**, create private
`/srv/tessera-garage` (accessible by the SSH operator),
`/srv/tessera-garage/control` and `/srv/tessera-garage/backups` (0700 owned by
UID/GID 65532). Initialize new state with `tessera init` only after deciding how
the existing local state will be preserved/migrated; local and remote copies must
not both be writable authorities. Keep `owner.header` (0600) private: transfer
only through authorized private operator custody, never image/build context,
Compose environment, logs, agent MCP tool or desktop renderer. Initialization,
state transfer, and remote credentials require a separate deliberate operator step.
No owner token is baked into this profile. Bind mounts refuse missing paths.

This service listens in-container on HTTP **only** behind a host loopback binding
`127.0.0.1:18766`. The operator must supply an already-reviewed trusted TLS
reverse proxy **on the Docker host**, forwarding only to that loopback port,
preserving the exact Host header; it must not expose the HTTP upstream, proxy
secrets to unrelated clients, or disable TLS verification. Do not start client
enrollment before HTTPS certificate hostname/CA trust and authenticated API
readiness are verified from the Mac. `TESSERA_TRUSTED_HOST` is the exact hostname
(not a wildcard, scheme or port) of the proxy and `TESSERA_PUBLIC_ORIGIN` is its
HTTPS origin (for example `https://control.example.invalid`). The fixed operator
sets them to `control-gateway` and `https://control-gateway`, requiring an already-
trusted certificate for that name. Both are required at Compose render time, and
the server validates the HTTPS origin/host match.
These are public routing metadata, **not credentials**. The proxy itself, network
firewall, TLS certificate, DNS and client CA trust are external admitted
prerequisites, not claimed to exist. This profile cannot provide remote HTTPS by
itself; do not publish this plain HTTP listener on a LAN address.

Build from reviewed source using the Dockerfile-specific allowlist and pin the
result to `tessera-garage-control:reviewed`; `pull_policy: never` prevents implicit
pull. The operator does not transfer source or build an image: prepare the reviewed
image separately before its `--no-build` deploy operation after host admission.
The Compose `up` command must scope project, file and service explicitly; never
use `down -v`, unscoped prune or the default Docker context. A process healthcheck
checks only the internal HTTP path, not TLS, client identity or workflow.

## Backups, migration and recovery

The optional `backup` maintenance service is networkless, has no restart and has
an independent private backup bind path. Schedule a **bounded** run externally
after checking available space/retention and verifying actual backup custody:

```sh
docker --host unix:///var/run/docker.sock compose --project-name tessera-garage-control \
  -f /srv/tessera-garage/compose.yaml --profile maintenance run --rm --no-deps backup \
  tessera backup --state-dir /state/control --output /backups/UNIQUE-NEW-ID
```

The output directory must be absent and unique per run; replace the placeholder
with an operator-generated non-secret ID. Backups exclude `owner.header` and are
not a complete replacement for securely held credential custody. Keep backup
bundles private, verify manifests and periodically perform a **held** restore
drill following [recovery](../../docs/recovery.md). Do not promote a restored copy
alongside the original writer: restored histories are read-only and credentials
are revoked. Before upgrade, fence the old writer, take and verify a private
backup, run explicit migration against state with the matching image, then take
a new backup. No startup auto-migration, backup timer or restore is asserted.
