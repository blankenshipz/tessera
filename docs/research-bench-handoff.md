# Operate the owned local research bench

The durable local metadata is indexed as evidence ID
`SHIPPABLE-RESEARCH-BENCH-2026-10-04`. It identifies the final wheel, installed user environment, public CA, private
header/profile **references**, and exact commands. No credential values belong
in that file. Old artifact/receipt hashes remain historical; follow its current
artifact pointers instead of a private temporary launcher.

## Install and start the ordinary route

For another installation, install the trusted wheel in a venv and use its
`bin/tessera`; see [installation](installation.md). On the recorded bench, set
`TESSERA` to the installed CLI named by the handoff, then run in a terminal:

```sh
"$TESSERA" connection-tunnel \
  --kubeconfig "$KUBECONFIG" --context docker-desktop \
  --namespace tessera-lab --namespace-uid 84ffd885-ed3e-4cf9-8941-59a5123cb3e1 \
  --gateway-deployment tessera-control-tls \
  --gateway-deployment-uid b612159e-c69d-45ae-83ae-687532c0c7ea \
  --public-ca-file "$PWD/.tessera/connections/bench/public-ca.crt" \
  --state-dir "$PWD/.tessera/connections/bench/tunnel"
```

These are recorded installation facts, not universal example identities. Recheck
them before another deployment. The default is foreground until Ctrl-C, on
`https://localhost:18443`; `--max-seconds` is optional. The live receipt used an
explicit 7,200-second bound and expires 2026-10-04 09:54 UTC. Worker credential
expiry was recorded at 11:54 UTC. These timestamps are **not fresh readiness**.
Inspect current receipt/credential/witness state before mutations; the network
witness has its own 15-minute lifetime. See [the tunnel contract](connection-tunnel.md).

Do not run a competing route while the existing owner is using port 18443.
Restart means the operator first ends their own foreground command with Ctrl-C
(or lets its declared deadline expire), confirms its receipt reports stopped,
then runs the same installed command. Never kill a process merely from a cached
PID or clear another project's listener. This route does not deploy a gateway,
proxy bearer tokens, provide permanent ingress or establish off-host availability.

## Native execution and the remaining OS selection step

The new native/API journey passed with actual retained Kubernetes inputs, task
revision 3 / plan 2, separate human grant, native admission/dispatch, an ordinary
new Job and automatic timely 385 / 36-byte output. Native human review explicitly
recorded `retain_quarantined`, then separately released custody and saved the exact
bytes, exported a reproduction bundle and reconnected without redispatch.
Scientific outcome remains null. Follow [the operator/worker/native sequence](../desktop/EXECUTION.md).
A backend `interrupted` label can map an exited observation; inspect Job Succeeded,
timeliness and human accounting separately from a scientific outcome.

For actual human file selection, launch the normal desktop from the repository,
with the public CA loaded before startup and a private profile outside receipts:

```sh
NODE_EXTRA_CA_CERTS="$PWD/.tessera/connections/bench/public-ca.crt" \
  env -u ELECTRON_RUN_AS_NODE \
  desktop/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron \
  --user-data-dir="$PWD/.tessera/private/native-execution/native-execution-r1/profile" \
  desktop
```

This macOS command uses the installed Electron binary, not an unsupported global
Node runtime. In Connect, invoke the actual OS picker and manually select the
private header reference in the handoff; never paste/print its contents or copy it
into receipt storage. For Save, manually choose the destination and verify the
resulting byte count/hash. The earlier actual Open dialog timed out after eight
seconds; labelled selection fallbacks do **not** verify these OS boundaries and
were not a permission denial. Existing functional native receipts and 192 Node
tests (with separate focused guard evidence) remain distinct from this human step.

## Private vulnerability reporting

The selected reporting policy is GitHub private vulnerability reporting. Its
enablement is still unverified: the CLI is unauthenticated and the desktop browser
tool is disconnected. A maintainer can complete the external authentication step:

```sh
gh auth login --hostname github.com --web
gh api repos/blankenshipz/tessera/private-vulnerability-reporting
# If supported and disabled, the authorized maintainer can enable it:
gh api --method PUT repos/blankenshipz/tessera/private-vulnerability-reporting
```

Read back the setting before claiming enablement. The feature is public-repository
only; do not change visibility merely to enable it. An unsupported private-repo
case needs a maintainer-selected private channel. See [SECURITY.md](../SECURITY.md).
Do not include credentials or exploit details in public issues. No commit, push,
deployment, provider spending, model inference or scientific acceptance is implied
by this handoff.
