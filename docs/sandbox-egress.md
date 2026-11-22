# Sandbox egress (ADR 0034)

Contract: [ADR 0034](decisions/0034-governed-sandbox-egress-and-dataset-staging.md).
This page covers the control-plane half implemented so far: the frozen plan field,
the runtime package fields and the worker journal route. The Docker proxy adapter
lives in the worker.

## Plan field

`ExecutionSpec.egress` is optional and accepted only with `profile: "local-cpu"`.
Every other profile (including `agent-sandbox`, phase 2) refuses it.

```json
{
  "profile": "local-cpu",
  "network_enabled": false,
  "egress": {
    "profile": "allowlist-proxy-v1",
    "destinations": [{"host": "example.org", "port": 443}],
    "max_egress_bytes": 104857600
  }
}
```

- 1-32 unique destinations; hosts must be public FQDNs or public IP literals
  (no localhost, private, link-local, metadata, `.local` or `.internal`).
- `max_egress_bytes` is 1..2 GiB, both directions combined.
- `sources` is optional: `[{"host", "license", "source"}]`, one entry per allowlisted
  host (unique, each host must be in `destinations`). `license` is an identified
  license id (`NONE`/`UNKNOWN`-style values are refused) and `source` a URL or stable
  reference. It is part of the approved, digest-bound plan and is a human declaration
  that Tessera records but does not verify; it never makes any output public. Absent
  `sources` is omitted from serialization, so existing plan digests are unchanged.
- Absent `egress` means network-none. It is omitted from serialization, so
  existing plan digests are unchanged. `network_enabled` stays `false`.
- Egress is part of the plan a human approves; agents cannot approve or widen it.

## Runtime package

Every local-cpu package carries `network_profile`: `"none"` (default) or
`"allowlist-proxy-v1"`. Only the latter adds `egress` with the frozen policy.
This adds a field to all local-cpu package digests.

## Journal route

`POST /worker/operations/{operation_id}/egress-journal` (worker audience,
`Idempotency-Key` required):

```json
{"lease_id": "...", "expected_epoch": 1, "journal_sha256": "<64 hex>",
 "summary": {"connections_allowed": 2, "connections_denied": 1,
             "bytes_up": 10, "bytes_down": 90,
             "destinations": [{"host": "example.org", "port": 443, "decision": "allow",
                               "connections": 2, "bytes_up": 10, "bytes_down": 90}]}}
```

- Fenced by worker ownership, lease id and epoch. 409 unless the operation's
  approved plan uses `allowlist-proxy-v1`.
- Accepted once per operation; an exact idempotent replay returns the first
  response, a different second summary is 409.
- Any `allow` destination outside the frozen allowlist (exact host and port) is
  409. Totals must equal the sums over destinations.
- Exceeding `max_egress_bytes` is recorded with `cap_exceeded: true`, not rejected;
  the proxy enforces the cap and the record stays truthful.
- Recorded as the append-only event `execution.egress_journal_recorded`
  (no new table). `egress_journal.egress_journal_for_operation` reads it back.

## Kubernetes Agent Sandbox (ADR 0034 phase 2)

`egress` is also accepted with profile `agent-sandbox`, off by default. Network-none
Sandbox plans, packages, targets and witnesses are unchanged and byte-identical.

- **Target pin.** The approved target must carry a digest-pinned
  `egress_proxy_image` (omitted from serialization when unset). An egress plan on a
  target without it is refused (409) when the package is built.
- **Package.** `profile_version` is `agent-sandbox-egress-allowlist-v1`, plus
  `network_profile: "allowlist-proxy-v1"` and the frozen `egress` policy.
- **Objects.** Before the Sandbox is POSTed, the control store creates four
  operation-scoped objects, derived deterministically from the package
  (`egress_manifests`), in this order: the sandbox NetworkPolicy
  (`tessera-egress-<op>`, egress only to the proxy port and cluster DNS), the proxy
  NetworkPolicy (`tessera-egress-proxy-<op>`, ingress only from the sandbox Pod,
  egress only to public addresses on allowlisted ports plus DNS), the Service
  (`tessera-egress-<op>`) and the proxy Pod (`tessera-egress-proxy-<op>`, journal on
  stdout). The manifests are stored in the binding blueprint and re-derived and
  compared on every access. The single create claim also covers these objects: each
  object's attempt and UID are recorded in `state_json`, and a lost response permits
  exact-name lookup only, never another POST. Policies must match exactly on re-read;
  the Service allows only API-server defaults; the proxy Pod is validated by
  containment (no extra containers, no host namespaces).
- **Preflight.** Every create, release and release-claim preflight for an egress
  binding passes the run's sandbox policy: the selecting policies must be the
  namespace default-deny plus exactly that policy, compared by spec. Before the
  policy exists (first create) only default-deny is accepted. Any extra allow policy
  refuses. Release additionally refuses a missing or drifted sandbox policy.
- **Witness.** For targets with an `egress_proxy_image`, the `cni` witness must also
  contain `egress-proxy-allow` (control receipt connected through the proxy with
  `bytes_received > 0`) and `egress-proxy-deny` (isolated receipt refused: errno
  `ETIMEDOUT`, `EACCES`, `EPERM` or `ECONNREFUSED`, or proxy HTTP 403) and carry
  profile `agent-sandbox-egress-allowlist-v1`. Other targets still require exactly
  the network-none cases and profile. No new witness kind exists.
- **Journal action.** `POST /worker/agent-sandboxes/{operation_id}/egress-journal`
  (`agent-sandbox egress-journal` in the CLI; worker audience, `Idempotency-Key`,
  `{"expected_epoch": n}`) is accepted only for egress bindings and only once fresh
  evidence shows the Sandbox suspended with no live Pod. The control plane itself
  reads the proxy Pod log (bounded to 2 MiB; a full buffer is refused), computes its
  sha256 and summary, and records them through the same
  `execution.egress_journal_recorded` event, allowlist check and once-per-operation
  rule as the local-cpu route. Workers cannot post a Sandbox journal summary.
- **Cleanup.** The human cleanup also deletes the proxy Pod, Service, proxy policy
  and sandbox policy by name with UID and resourceVersion preconditions, after the
  PVC and Sandbox. A UID mismatch or unrecorded object is `cleanup_uncertain` and
  deletes nothing further. Deleting the proxy Pod discards its log, so cleanup
  refuses (409) until the journal is recorded. If the log cannot be read, the proxy
  Pod is retained for operator recovery.
- **Operator prerequisites.** The namespace needs default-deny ingress and egress;
  kube-dns pods labelled `k8s-app: kube-dns` reachable by the DNS rule; a CNI that
  enforces NetworkPolicy; and a proxy image that contains `tessera_worker`
  (`python -m tessera_worker.egress_proxy`), pinned by digest. Live activation is
  operator-gated and no default test contacts a cluster.

## Proxy image recipe

Egress runs are refused until an operator pins a proxy image. The recipe is
`Dockerfile.egress-proxy` at the repository root. It uses the repository's
digest-pinned Python 3.11 slim base and packages only the stdlib-only
`tessera_worker/egress_proxy.py`, running as uid 65532. Tests keep it that way
(`tests/test_egress_proxy_image.py`). Build it, push it to a registry you control,
and pin the digest Docker reports:

```sh
docker build -f Dockerfile.egress-proxy -t REGISTRY/tessera-egress-proxy:<git-sha> .
docker push REGISTRY/tessera-egress-proxy:<git-sha>
docker buildx imagetools inspect REGISTRY/tessera-egress-proxy:<git-sha> \
  --format '{{.Manifest.Digest}}'
```

Use the resulting `REGISTRY/tessera-egress-proxy@sha256:<digest>` reference:

- **Local Docker:** pass `--egress-proxy-image <ref>` to `tessera-worker
  development-run`. The adapter mounts the current proxy module read-only and runs
  it with this image's Python, so the image supplies only the interpreter.
- **Kubernetes Agent Sandbox:** set `egress_proxy_image` on the reviewed
  `SandboxTarget`. The proxy Pod runs the packaged module, so rebuild and re-pin the
  image whenever `egress_proxy.py` changes. A target change requires the usual human
  plan review and witness refresh.

Neither a tag nor a local image ID is accepted: the reference must be
`name@sha256:<64 hex>`. The base image digest changes only together with the main
`Dockerfile`.

## Honesty limits

- No TLS inspection: the record is destinations and bytes, not downloaded content.
- Allowlisting is by hostname and port at CONNECT time; an allowlisted host can
  serve anything.
- The journal bytes stay with the worker; the control plane holds a digest and a
  bounded summary reported by a trusted worker, not independent proof.
- Staged dataset snapshots are bounded to 16 MiB and 1,000 files.
