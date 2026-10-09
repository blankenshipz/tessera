# Installed operator connection tunnel

`tessera connection-tunnel` is a normal foreground operator process for an existing
owned Kubernetes TLS gateway. Install Tessera and `kubectl`, retain the explicitly
authorized operator kubeconfig, and obtain the public CA plus namespace and gateway
Deployment UIDs from your installation records. The gateway certificate must trust
`localhost`; its protected `/projects` route must reject unauthenticated requests
with HTTP 401. This command is not login, a bearer-token proxy, a public relay,
deployment, or permission enrollment. It does not change the current context.

```sh
tessera connection-tunnel \
  --kubeconfig /operator/kubeconfig \
  --context YOUR_EXPLICIT_CONTEXT \
  --namespace YOUR_NAMESPACE --namespace-uid YOUR_NAMESPACE_UID \
  --gateway-deployment YOUR_TLS_GATEWAY \
  --gateway-deployment-uid YOUR_GATEWAY_DEPLOYMENT_UID \
  --public-ca-file /operator/public-ca.crt \
  --state-dir /operator/private-route-state
```

The state directory is created with mode 0700; its parent must already exist.
Existing directories must be privately owned (0700), without symlink ancestors.
It binds **only** `127.0.0.1:18443` and forwards to the selected gateway Pod's port
443. `--listen-port` and `--gateway-port` select other explicit ports. The client
endpoint is `https://localhost:18443`; clients separately configure the public CA
and their own scoped Tessera credentials. No insecure TLS/global trust fallback
exists. Existing authentication and permissions stay with the operator kubeconfig.
Kubernetes API TLS verification is also explicitly enforced; a kubeconfig's
insecure-skip setting is not adopted as a fallback.

By default this stays in the foreground until Ctrl-C or SIGTERM. An external
ordinary process supervisor can run the same command; it must not start a competing
copy or treat a stale PID receipt as kill authority. Optional `--max-seconds` sets
an explicit deadline; no hidden fixture deadline applies to a normal operator run.
The command owns a private exclusive state lock and writes an atomic mode-0600
`connection-tunnel.json`: supervisor/child PID, hashed child command/start identity,
namespace/Deployment/Pod UID, endpoint, public CA reference, readiness, optional
expiry and sanitized stop/error classes. It never copies kubeconfig credentials,
HTTP headers, provider tokens, certificates' private keys or subprocess output.

Selection rechecks the namespace UID and Deployment UID, derives exact matchLabels,
and follows each ready Pod's controller ReplicaSet to that same Deployment. Exactly
one healthy candidate is required. Optional `--gateway-selector` must equal the
sorted Deployment selector; optional `--gateway-pod` and `--gateway-pod-uid` pin a
specific Pod within that same ownership chain. Replacing a pinned UID or the
namespace/Deployment stops the route; it does not silently adopt a replacement.
The automatic Deployment-owned mode can select a new healthy owned Pod after a
failed forward, not a broad arbitrary gateway Service.

Readiness validates the CA and hostname over a literal-loopback TLS connection,
with no token, proxy or response-body logging. One failed probe marks the receipt
degraded **without** tearing down client connections. Three consecutive failures
or an exited forward cause only the owned child to be restarted, with bounded
exponential backoff. Ownership/certificate violations fail closed. Port availability
is checked before launching; listener PIDs are inspected when `lsof` is available.
An occupied port is never cleared by killing another project. Cleanup checks the
owned child's recorded start/command fingerprint and refuses a mismatched or
unverifiable PID rather than killing an unrelated process. The receipt reports that
refusal for operator diagnosis; there is no stale-PID recovery or name-based delete.

Supported operator hosts are macOS/Linux. Credential-free `--help` performs no
cluster access. Unit checks use synthetic process/Kubernetes responses; a deployed
route requires separate live acceptance against the intended installation.
