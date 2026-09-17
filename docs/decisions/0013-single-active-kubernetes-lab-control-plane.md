# 0013: Single-active Kubernetes lab control plane

Status: accepted implementation slice, 2026-09-16.

## Decision

The first Kubernetes control-plane profile is one SQLite-backed Pod, not an HA
service. It uses one `ReadWriteOnce` PVC and a `Recreate` Deployment strategy,
so Kubernetes must never make two writers active for that profile. This is a
lab deployment contract, not PostgreSQL, HA, federation, target enrollment, or
an execution-control claim.

Remote binding is opt-in. `tessera serve` remains loopback-only with its local
trusted-host defaults unless a non-loopback bind is supplied with one or more
concrete trusted hosts and an HTTPS public origin whose hostname is trusted.
Wildcards, browser origins, and implicit current-host behavior are rejected.
The public origin is an identity projection for clients and operators; it does
not add browser support or TLS. Browser `Origin` headers remain rejected.

Liveness reports only that the process can answer. Readiness reads the durable
SQLite metadata and is false for an old/new schema, unavailable state, or held
restore. Schema/state failures gate all API routes. A held recovery remains
available for authenticated forensic reads while Store continues to reject every
write. Readiness includes a stable, opaque digest of the persisted recovery
incarnation so a restart test can prove it retained the same backend without
exposing owner credentials or database contents.

## Consequences

`deploy/kubernetes/lab-control-plane.yaml` runs UID/GID 65532, disables service
account token mounting and privilege escalation, drops capabilities, uses the
runtime-default seccomp profile, and has a read-only root filesystem. The PVC is
the only durable writable mount; `/tmp` is an ephemeral emptyDir. The manifest
contains a namespace-wide deny policy and paired ingress/egress policies that
permit only explicitly labelled in-namespace client Pods to reach the Service on
TCP/8765, plus DNS lookup to the labelled `kube-system` resolver Pods. The probe
headers repeat the configured trusted host because Kubernetes does not expand a
container environment variable in an HTTP probe header. An
operator supplies separately managed TLS/identity-aware ingress or a private
network route before a remote client uses the advertised HTTPS origin.

The profile intentionally has no Secret, Ingress, automatic migration,
provisioning API, teardown controller, or target attachment. Its generated owner
header remains PVC-resident private state. Recovery holds require human recovery
work and stay unready rather than becoming writable through a Pod restart.
