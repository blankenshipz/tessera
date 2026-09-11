# Local Minikube provisioning

`tessera minikube-provision` is a narrow local-development operator for exactly
one disposable target: Docker-driven profile `tessera-dev` (2 CPUs, 4 GiB memory,
20 GiB disk), namespace `tessera-dev`, retained `tessera-output` PVC
(`ReadWriteOnce`, 1 GiB), and `tessera-deny-all` NetworkPolicy. The policy denies
ingress and egress for Tessera-managed Pods and is the target-local enforcement
evidence required by the development `network_mode=none` contract. It is not a Kubernetes execution target, attachment,
credential/Secret path, worker enrollment, or research dispatch path.

First inspect the immutable dry run. It makes no subprocess call:

```sh
.venv/bin/tessera minikube-provision
```

Only an eligible human may then explicitly approve the printed digest and run the
mutation. The digest is an exact-plan acknowledgement, not a research-plan,
agent, worker, harness, or skill approval. Store the sanitized local result in an
existing private (`0700`, owned by the invoking user) directory:

```sh
.venv/bin/tessera minikube-provision --apply \
  --approved-plan PLAN_DIGEST --journal .tessera/minikube-provision.json
```

The command uses only `minikube --profile tessera-dev kubectl -- ...`; it never
reads, switches, or falls back to an ambient Kubernetes context. Before mutation
it rejects an existing profile whose Docker/CPU/memory/disk configuration differs
from the plan. It validates bounded JSON identities after the profile, namespace,
PVC, and policy steps. It freezes cluster and namespace UIDs across bootstrap;
replacement fails closed for reconciliation rather than continuing on a different
target. Every command has a deadline and an 8 KiB per-stream output bound.
Timeout, malformed data, nonzero process exits, or unclassified errors fail
closed; records contain only state, plan digest, named resource UIDs, and a
sanitized category, never command output.

The mandatory `--journal` custody check happens before `--apply` constructs or
calls Minikube. Its parent must not be a symlink and must already be private; the
destination must not exist. Results use an exclusive random temporary file and
atomic no-clobber publication, so a symlink, hardlink, FIFO, existing result, or
concurrent writer is rejected rather than followed, overwritten, or waited on.

Read status explicitly with `--status`; it performs profile-scoped reads and can
also write a result when `--journal` is provided. Resources are retained by
default. `--teardown` records/reports that retention and performs no command or
automatic deletion. Destructive deletion remains a separately authorized,
unimplemented human decision.
