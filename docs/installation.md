# Install and operate separate Tessera services

The control and execution planes are independently runnable Python processes.
Docker Compose is a development convenience, not a deployment requirement.
This runbook does not establish production hardening or acceptance on an
independent always-on host. Docker Desktop stops when its Mac host is unavailable;
closing Electron is a different failure boundary.

For current source-versus-installed schema, migration, credential and recovery
limits, see the [runtime operator setup boundary](runtime-operator-setup.md) and
[release checklist](release-checklist.md). The current source migration ceiling is
not evidence of an installed release.

## Python distribution

Use Python 3.11 or newer. From a trusted checkout build the Python wheel/source
distribution with `uv build`, then install the wheel in a fresh environment:

```sh
python3 -m venv /absolute/path/to/tessera-env
/absolute/path/to/tessera-env/bin/python -m pip install /absolute/path/to/tessera_control-0.1.0-py3-none-any.whl
/absolute/path/to/tessera-env/bin/tessera --help
/absolute/path/to/tessera-env/bin/tessera-worker --help
```

The wheel installs the Python control, agent, worker, provisioner and monitor
packages. It does not install Docker, Kubernetes, Electron, a model provider,
or a cluster target. The source distribution also contains Markdown runbooks
and operator-edited deployment examples. Desktop, development harnesses and
example workload sources remain in the checkout. See LICENSE for MIT terms
and SECURITY.md for reporting status.

For the thin stdio MCP adapter, install the optional extra using
`pip install '/absolute/path/to/tessera_control-0.1.0-py3-none-any.whl[mcp]'`.
It pins the official Python SDK to **mcp 1.26.0** and provides `tessera-mcp`.
The portable runtime skill is installed at
`TESSERA_ENV/share/tessera/skills/tessera-research-bench/SKILL.md`; copy it to the
chosen host's skill directory as a deliberate user setup step. It is not an
execution grant or the repository's development skill. The adapter supports
session-based stdio protocol **2025-11-25**, not the 2026-07-28 stateless protocol
or remote HTTP MCP. Enrollment/configuration instructions are in the skill;
never give the adapter an owner, worker or provider token.
For a control endpoint using an operator-managed public CA certificate, set
`TESSERA_CONTROL_CA_FILE` to its absolute public-CA file reference. Certificate
verification stays enabled; this is not a TLS bypass or a secret-token reference.

For reproducible application dependencies, retain the committed `uv.lock` and
use `uv sync --locked --no-dev` in the trusted checkout. A plain `pip install`
resolves compatible dependency ranges, not necessarily the locked versions.
To prepare a pinned requirements file for an isolated deployment environment:

```sh
uv export --locked --no-dev --extra mcp --no-emit-project --output-file /private/runtime-requirements.txt
uv pip install --python /absolute/path/to/tessera-env/bin/python \
  --require-hashes -r /private/runtime-requirements.txt
/absolute/path/to/tessera-env/bin/python -m pip install --no-deps /absolute/path/to/tessera_control-0.1.0-py3-none-any.whl
```

The desktop is separate: install Node **22.12.0 or newer**, run `npm ci` in
`desktop/`, then `npm start`. The lockfile pins Electron 44.2.0. Node 22.2.0 does
not satisfy the declared engine even when Electron happens to launch. CI uses
the current Node 22 release; native screen acceptance is a distinct gate.

For repository checks without replacing a global Node installation, the pinned
Electron package embeds supported Node 24.20.0:
`ELECTRON_RUN_AS_NODE=1 desktop/node_modules/.bin/electron --version`.
With that environment variable Electron runs Node, **not the desktop UI**; native
UI acceptance must unset it. This alternative does not replace `npm ci`'s declared
Node engine requirement for newcomer installation.

## Fresh control enrollment and local API example

Choose an absent private state directory and unused loopback port. Inspect the
existing listener/process owner before starting; never kill another project's
service. Initialize once, then run the installed service:

```sh
umask 077
/absolute/path/to/tessera-env/bin/tessera init --state-dir /private/path/tessera-state
/absolute/path/to/tessera-env/bin/tessera serve --state-dir /private/path/tessera-state --port 8765
```

Initialization writes a private `owner.header`, not a token on stdout. In a
second terminal create a project through the actual service:

```sh
curl --fail-with-body -H @/private/path/tessera-state/owner.header \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: first-project' \
  -d '{"name":"My research bench"}' http://127.0.0.1:8765/projects
```

Keep the header out of shell arguments, logs, prompts, Git and agent contexts.
This owner identity is local bearer possession, not federated human login.
Agents/workers receive separate scoped, expiring credentials through authenticated
`/credentials`; enrollment is not plan approval, execution admission, artifact
disclosure authority or scientific review. The [API workflow](local-api-workflow.md)
explains ordered approval/admission/dispatch/review boundaries; its default
executable example uses synthetic external effects, not live compute.

## Execution process on an operator-owned Kubernetes target

### Installed client connection tunnel

For an already installed, operator-owned TLS gateway, use the ordinary installed
`tessera connection-tunnel` command; no private temporary launcher is required.
Install `kubectl` separately and supply the authorized host kubeconfig, explicit
context, observed namespace/Deployment UIDs and the public CA reference:

```sh
/absolute/path/to/tessera-env/bin/tessera connection-tunnel \
  --kubeconfig /operator/authorized-host-kubeconfig --context EXPLICIT_CONTEXT \
  --namespace CONTROL_NAMESPACE --namespace-uid OBSERVED_NAMESPACE_UID \
  --gateway-deployment TLS_GATEWAY --gateway-deployment-uid OBSERVED_DEPLOYMENT_UID \
  --public-ca-file /operator/public-ca.crt --state-dir /operator/private-route-state
```

Replace metadata placeholders with current installation facts, not guessed IDs.
The default is foreground until Ctrl-C, literal loopback `127.0.0.1:18443` and
gateway port 443; `--max-seconds` is optional rather than a hidden fixture timeout.
Never start a competing route or clear an occupied port by killing another process.
The gateway must present a localhost-valid certificate and return unauthenticated
401 on the protected readiness route. See [the connection-tunnel contract](connection-tunnel.md)
for UID selection, state custody, TLS verification, supervision and safe cleanup.
This is neither deployment/enrollment nor permanent external ingress. Unit checks,
installed command help and actual intended-gateway acceptance remain distinct.
An owned local bench exercised installed authenticated connect/reconnect;
temporary route observations are not permanent ingress or current readiness.

### Worker process

Install the same wheel on the execution host; it need not mount control state.
The ordinary installed service entry point is:

```sh
/absolute/path/to/tessera-env/bin/python -m tessera_worker.kubernetes_remote_service \
  --control-url https://CONTROL-ENDPOINT --control-ca-file /private/control-ca.crt \
  --target-file /private/target.json --ca-file /private/kubernetes-ca.crt \
  --kubernetes-token-file /private/kubernetes-token \
  --credential-file /private/worker-token --workspace /private/worker-journals
```

Do not substitute placeholders and run. The target binding needs observed cluster,
namespace, PVC, image and network-enforcement identities; TLS trust and scoped
Kubernetes RBAC must be provisioned independently. Keep journals durable,
single-writer and separate from control state. Follow
[deployment prerequisites](../deploy/kubernetes/README.md) in the checkout;
the manifests deliberately cannot be applied unchanged. No ambient kube context,
human-token reuse, provider fallback or automatic cluster creation is supported.
Configure the ordinary collector's pinned image, helper RBAC, durable journals,
quota and fresh witness as described in [ordinary worker collection](ordinary-worker-collection.md).
That runbook records the owned Docker Desktop service evidence and its precise
boundaries; historical harness-uploaded bytes are not proof of collection.

## Credential expiry, interruption and recovery

Current owner bootstrap credentials have no CLI rotation flow. Issued worker
credentials expire: the enrollment script defaults to one hour (bounded to two
hours). Before expiry, the local owner can explicitly renew the **same active
worker credential** using `POST /credentials/{credential_id}/renew` with
`{"expires_at":"OWNER_SELECTED_FUTURE_UTC_TIME"}` and an idempotency key. Select
a time later than its current expiry, within the next 24 hours and no later than
its parent credential's expiry. The API retains credential/principal/token and
worker-journal identity; renewal does not extend a grant, lease or network witness.

```sh
curl --fail-with-body -H @/private/path/tessera-state/owner.header \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: deliberate-worker-renewal' \
  --data-binary @/private/path/owner-reviewed-renewal.json \
  http://127.0.0.1:8765/credentials/EXACT_WORKER_CREDENTIAL_ID/renew
```

The private JSON file must contain the actual reviewed timestamp, not a
placeholder. For remote control, use the explicitly trusted HTTPS endpoint and
CA instead. Reuse the same key and body after a lost response. Renewal is
owner-only and never revives expired/revoked credentials or a revoked/expired
parent. **Unattended renewal is not implemented**; never embed an owner token in
the worker. After expiry/revocation, stop new admissions, reconcile original
dispatches/jobs, and owner-enroll a fresh identity. Preserve old journals/receipts;
never reuse uncertain intent as a new submission or register a replacement to
evade outstanding operations. Fresh worker enrollment is not automatic transfer
of old operation authority.

Restart the same service with its durable workspace after interruption. Unknown
operations need exact-identity reconciliation, not replacement dispatch. For a
lost owner credential there is no supported account-recovery bypass: fence the
old writer/workers, retain state/backup custody, and perform an operator-led
rebuild with fresh authority. Never edit stored credential hashes.

Before upgrades retain a private backup and matching old binary. Run
`tessera migrate` explicitly; startup never silently migrates. Take and verify a
new post-migration backup. Restores are permanently held read-only history, not
failover or resumed execution. See [recovery](recovery.md). Non-loopback serving
requires a verified TLS gateway, trusted hosts/origin, protected storage and an
operator; `--bind-host 0.0.0.0` alone is not secure remote deployment.
