# Public native observer/runtime source bundle — version 8

This **new** bundle preserves the older parent-directory observer bundle as
historical version5 (27 originals/36 targets). It does not relabel those artifacts.
The version8 bundle reconstructs **50 licensed originals →79 exact target files**,
including the actual SDK terminal facade, real AI HTTP/WS schema/helper closure,
owner guard/quota/check reader, native pipe/controller and reproduction tests.

## What this is, and is not

- A deterministic **FILEPINNEDSPARSE** public source bundle. OpenCode source version
  is2.0.18. Public source origin is declared, not independently verified; upstream
  OpenCode commit is unknown. The separately identified public harness snapshot
  reference is57a921a40a189004d345bd8e9738e28962de6c2b. No invented commit/license.
- Runtime-only native HTTP/WS and SDK facade closure, NOT the full upstream
  OpenCode/Core/server/CLI/TUI package or a full TypeScript/package build.
- No original private guard code, OAuth/account material, credentials, raw prompts,
  runtime receipts, dependency trees or workstation paths are shipped.
- Installed dependencies and cold imports prove executable source linkage, not
  enrollment, financial authority, installed operator readiness or real research.

`baseline.json` uses closed schema `tessera-opencode-observer-runtime-v3`; it binds
every original/target, both original MIT grants, the gzip archive/patch and all
public reconstruction/bootstrap files. `target_namespace_sha256` hashes sorted
`digest  filename\n` bytes (double space), **not** JSON encoding. The manifest's raw
file hash is a different artifact identity. `reconstruct.py` verifies all50originals,
safe deterministic tar metadata, native patch application and all79 final targets.

## Reconstruct and install the bounded public runtime dependencies

Prerequisites: Python≥3.11, Git/patch, Bun1.4.2 and access to the public npm registry.
Use a **fresh** destination and a separate dependency directory; never run a broad
install from the reconstructed sparse root's upstream workspace manifest.
Keep the source path short enough for the test Unix socket's platform limit
(Darwin104-byte sun_path including terminator); long source paths fail, not skip.

```sh
python3 reconstruct.py /absolute/new/source-tree
mkdir /absolute/new/runtime-deps
cp runtime-deps/package.json runtime-deps/bun.lock /absolute/new/runtime-deps/
cd /absolute/new/runtime-deps
bun install --frozen-lockfile --ignore-scripts --registry https://registry.npmjs.org
cd /absolute/path/to/this/bundle
python3 link_runtime.py /absolute/new/source-tree /absolute/new/runtime-deps
bun verify_runtime.mjs /absolute/new/source-tree
```

The exact Effect4.0.0-rc.112 package and all its transitive/optional public dependency
versions/integrities are pinned in the shipped Bun lock. Install scripts are disabled.
No Ray/Jupyter/MLflow/cloud dependency is added. `link_runtime.py` requires matching
dependency manifest/lock bytes and actual installed Effect version; it links only
Effect and the reconstructed real Schema/SDK package manifests. Nothing resolves
through an ignored checkout or an implicit private auth path.

Cold verification imports the actual SDK/HTTP/WS/producer/quota/controller/check IPC
and full artifact reader without mocked barrels/transports. Missing dependencies
fail the required gate. This source setup does NOT install or replace OpenCode.

## Python adapters and isolated reproduction tests

Create a Python environment and install the matching Tessera release artifact through
its ordinary documented install flow. It must export the actual v2 verifier,
RuntimeBindingStore/NativeCheckWindow and closed runtime binding models. This bundle
does not vendor/reimplement those modules. A missing or incompatible Tessera install
causes **FAIL/incomplete**, never skip or a fabricated cold-pass receipt.

```sh
/absolute/venv/bin/python -B verify_python.py /absolute/new/source-tree
cd /absolute/new/source-tree/harness
TESSERA_PHASE_TEST_PYTHON=/absolute/venv/bin/python \
  bun test ./tests/owner-bindings.test.mjs ./tests/native-pipe-process.test.mjs
/absolute/venv/bin/python -B tests/test_runtime_control_client.py
```

Tests execute actual public guard predicates, quota executable, authenticated local
IPC/Python Store adapter, full FD hashing and durable SQLite audit/client behavior.
Upstream OAuth/account/authority/installation/native resources are **SYNTHETIC** and
no provider inference runs. The required public test explicitly injects the shipped
public guard as its synthetic factory: no private source path is needed. This is
not the production factory and cannot authorize a production effect. Earlier
private opt-in source checks are external development evidence, not OSS prerequisites.
`TESSERA_PHASE_TEST_SOURCE_ROOT` is an optional explicit development source reference
only; tests without it use the installed Tessera package. Source-reference evidence
must not be relabeled as a clean installed-wheel check.

## Explicit owner setup: fail closed until genuinely configured

The approved embedding supplies `makeStorePipeBoundRuntime`, using actual independently
enrolled publisher/session/project/destination/grant/policy/financial refs and full
installed build/SDK/config/skill/catalog/tool-map pins. It must not echo a caller's
requested pins, stage diagnostic or serialized `authorized:true` into authority.

Register `makeOwnerGuardProducer` with existing **secure in-process owner resources**
and an independently approved `privateGuardFactory`. Its API intentionally has **no
production default/fallback** when private resources/factory are missing. The owner's
original guard/quota code executes privately against its already-owned auth handles.
No credential is inherited from a prompt, copied by reconstruction, discovered via
keychain, or replaced by the synthetic tests. Only safe executed outcomes/opaque
runtime-scoped check refs cross the authenticated proof IPC.

Register the Python `RegisteredOwnerCheckAdapter` through the trusted server
constructor, not agent request JSON. The actual Python verifier precedes fresh201
claim;200 readback never permits. Production uses canonical **StoreInvocationGate**,
not the SQLite unit-test fence. Canonical writer fencing remains held through child
termination. Current owner-private resource/expiry/controller/verifier checks execute
synchronously inside the native owner's genuine lock immediately before the exact
immutable primitive. HTTP/IPC snapshots/heartbeat leases do not prove live authority.

Native launcher: `harness/bin/native-invocation-pipe.mjs`; approved command names the
independently enrolled owner module. It receives only explicit owner runtime/publisher
credential **references**, never secrets in argv/environment. Metadata frames:
`ready{operation_ref,request_ref,final_fingerprint}`, `invoke{operation_ref}`,
`native-returned{operation_ref}`. HTTP maps terminal-http/native-http-fetch; WS
handshake/frame each maps terminal-ws with its own distinct native phase/operation.
Reused sockets require fresh checks; custom connectors/unknown locks deny.

Lost claim/entry/return, crash or uncertain native effect consumes its reservation;
no automatic retry/fallback/refund. A claim owning the final unit remains valid when
global remaining0. Canonical DB writer serialization linearizes an admitted native
primitive before a waiting revocation; it is NOT DB+network exactly-once, provider
billing assurance or a distributed private-resource revocation guarantee.

Real enrolled/provider/installed full SDK/private resources and planner/critic,
experiments, independent-host/device/laptop-off journeys remain separate live gates.
Do not change the active coding service or run provider inference during setup tests.
