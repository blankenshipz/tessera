# Local Minikube provisioning

`tessera minikube-provision` is a narrow local-development operator for exactly
one disposable target: Docker-driven profile `tessera-dev` (2 CPUs, 4 GiB memory,
20 GiB disk), namespace `tessera-dev`, and retained `tessera-output` PVC
(`ReadWriteOnce`, 1 GiB). It is not a Kubernetes execution target, attachment,
credential/Secret path, worker enrollment, or research dispatch path.

First inspect the immutable dry run. It makes no subprocess call:

```sh
.venv/bin/tessera minikube-provision
```

Only an eligible human may then explicitly approve the printed digest and run the
mutation. The digest is an exact-plan acknowledgement, not a research-plan,
agent, worker, harness, or skill approval. Store the sanitized local result in a
private local path:

```sh
.venv/bin/tessera minikube-provision --apply \
  --approved-plan PLAN_DIGEST --journal .tessera/minikube-provision.json
```

The command uses only `minikube --profile tessera-dev kubectl -- ...`; it never
reads, switches, or falls back to an ambient Kubernetes context. Before mutation
it rejects an existing profile whose Docker/CPU/memory/disk configuration differs
from the plan. It validates bounded JSON identities after the profile, namespace,
and PVC steps. Every command has a deadline and an 8 KiB per-stream output bound.
Timeout, malformed data, nonzero process exits, or unclassified errors fail
closed; records contain only state, plan digest, named resource UIDs, and a
sanitized category, never command output.

Read status explicitly with `--status`; it performs profile-scoped reads and can
also write a result when `--journal` is provided. Resources are retained by
default. `--teardown` records/reports that retention and performs no command or
automatic deletion. Destructive deletion remains a separately authorized,
unimplemented human decision.
