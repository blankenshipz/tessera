# Astra offline installation plans

`tessera astra-installation plan` renders a deterministic, digest-bound Astra
installation plan from **explicit** target identity inputs. It does not read a
kubeconfig, invoke `kubectl`, contact a cluster, read a Secret, apply a resource,
or attach an execution target. Its output has `verification: offline_unverified`:
an offline input or snapshot is not live verification.

Every invocation names the kubeconfig-reference digest, context, HTTPS server, CA
digest, cluster UID, namespace, and namespace UID. A later adapter must recheck
that complete identity with `assert_target_unchanged()` and reject an expired
approval with `assert_approval_current()` before doing anything mutating.

```sh
.venv/bin/tessera astra-installation plan \
  --mode create --installation-name astra-control --profile astra-phase1 \
  --kubeconfig-reference-sha256 KUBECONFIG_64_HEX --context named-target \
  --server https://cluster.example.test:6443 --ca-sha256 CA_64_HEX \
  --cluster-uid cluster-uid --namespace astra-system --namespace-uid namespace-uid \
  --desired-spec-sha256 SPEC_64_HEX --approval-expires-at 2026-09-19T00:00:00Z
```

`--observed-uid`, `--observed-spec-sha256`, and `--observed-at` may supply one
offline observation together. Alternatively, `--inspection-snapshot FILE` accepts
one strict JSON inspection record no larger than 64 KiB. The snapshot identity
must exactly match the explicit arguments and cannot claim live verification.
Snapshots may report sanitized credential handles, storage/network/permission
findings, and ownership or server-side manager conflicts; they never contain
credential values.

The four modes are `observe`, `create`, `adopt`, and `adapt`. `observe` only
reports supplied facts. Mutating modes fail closed on ownership/manager conflicts
or incompatible storage, network, or permission resources. This command has no
apply path; a future live inspection/reconciliation adapter must independently
perform bounded reads, verify the target and plan digest/expiry, and obtain the
separate human authorization required for any effect.

## Opt-in live inspection

`inspect-live` is a separate, bounded read-only adapter. It requires an absolute
`--kubeconfig` and named `--context`; it never uses a current context. It reads
only `kube-system` and the named namespace metadata plus explicitly named
Deployment, PVC, NetworkPolicy, or ServiceAccount resources. Secret resources
are rejected before `kubectl` runs. No apply, patch, delete, create, exec, log,
or arbitrary command interface exists.

```sh
.venv/bin/tessera astra-installation inspect-live \
  --kubeconfig /absolute/path/to/config --context named-target \
  --installation-name astra-control --profile astra-phase1 --namespace astra-system \
  --installation-resource deployment/astra-control \
  --resource persistentvolumeclaim/astra-outputs --resource networkpolicy/astra-deny-all
```

The output is **live inspection, not apply readiness**. `revalidate-live --plan
PLAN` repeats those reads and independently verifies the immutable plan digest,
approval expiry, complete target identity, installation UID, and spec digest.
It also fences the installation `resourceVersion` recorded by live inspection.
Drift, foreign/ambiguous ownership, manager conflict, or incompatible findings
blocks every mutating mode. Revalidation still does not provide an apply path.
