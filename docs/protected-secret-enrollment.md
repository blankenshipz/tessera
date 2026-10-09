# Protected secret enrollment, custody and incident runbook

This adds **local macOS custody**, not provider activation or production identity
management. Salad's live entry point remains blocked **before** resolution or
network. AI Gateway activation is likewise not implemented. No Keychain/provider
credential was accessed in the default acceptance tests: native functions and the
OS store are stubbed, and all values are synthetic.

## Actual supported topology

```text
sandboxed renderer -- metadata only --> Electron main
                                          | authenticated metadata GET
                                          v
                                  native local control API + SQLite
                                          ^
                                          | same database; live auth + one-use claim
main -- private inherited auth/metadata pipe --> one-shot native Python helper
main <-- closed metadata + commit handshake --  |             |
                                      hidden macOS dialog   Security.framework
                                                            macOS Keychain
```

* macOS: native `Security.framework` generic-password add/find/delete APIs via
  Python's standard-library `ctypes`; no installation, `security -w` argv, shell,
  plaintext config or secret file. The service is `org.tessera.protected-secrets.v1`.
  An account is a generated `credential-ref:<32 hex>` plus integer version.
  There is no enumeration/import from existing Keychain accounts.
* Windows Credential Manager and Linux Secret Service/libsecret have **explicit
  unavailable adapters**. No reviewed native binding is shipped for either.
* `DevelopmentMemoryStore` is an **injected NON-PRODUCTION test fake**, never a
  platform/default/config fallback. Existing `backend: fake` admin records remain
  synthetic metadata-only and cannot import real values.
* The desktop requires a loopback origin; the metadata handoff API also requires
  the `local-loopback-v1` serving profile. Loopback is not proof of locality (SSH
  tunnels exist): the helper must find the exact server-minted approval ID and
  revision in the **same native control database**, using the current authenticated
  principal. Another local database, remote profile or held restore is rejected.
* Docker/Kubernetes control-plane enrollment from this desktop is **unsupported**.
  Do not share/mount/copy a remote SQLite database to defeat this topology check.
  A desktop Keychain is not a remote Kubernetes secret backend. Remote options
  requiring further implementation are (a) a separately authenticated native broker
  colocated with the trusted proxy and its OS store, with metadata receipts back to
  control, or (b) an organization-managed secrets service accessed through scoped
  proxy workload identity. There is no implemented network value handoff, Kubernetes
  Secret materialization, desktop relay or automated migration to those services.

The control API's `production_secret_backend: unavailable` still means **no
in-process API value backend**. Native enrollment does not enable an API value
upload/read route. `health: available` records successful local custody, not a
provider probe; explicitly check local OS-store presence for a current boolean.

## Configuration (non-secret values only)

With the existing native Python/Electron dependencies already provisioned, launch
the desktop from its normal directory with explicit absolute paths:

```sh
TESSERA_SECRET_PYTHON=/absolute/path/to/tessera/.venv/bin/python \
TESSERA_SECRET_STATE_DIR=/absolute/path/to/private/native-control-state \
npm start
```

These environment variables contain **executable/state paths only**, never key
values. The helper inherits a minimal environment, not provider/auth variables.
There is no default state discovery. Its executable and code must be trusted and
not writable by a less-trusted principal. The state directory is the already
initialized native control service's private directory, not a new database or
backup. No setup command here deploys a service or installs dependencies.

Metadata proposal example: [protected-secret-proposal.json](examples/protected-secret-proposal.json).
The example expiry is illustrative; choose a current expiry within 30 days. The
HTTP project path supplies scope; organization is always authenticated, not JSON.

## Enrollment/import workflow

1. Connect using a **Tessera** credential, not a Salad/provider key. The native
   credential-header picker now requires a regular, no-symlink, owner-private
   file. Recognizable provider-key prefixes are rejected before Connect transport
   and at Tessera credential creation. Random opaque keys have no reliable
   detectable provider: prefix rejection is defense in depth, not a provider-key
   classifier. Never register a provider key as a Tessera authentication token.
2. As an existing project-scoped **human session**, select Administration → Secrets,
   protected macOS Keychain, purpose/provider/audience and expiry. Scope is the
   selected project. Preview then submit metadata only. Version starts at 1;
   callers cannot choose references or overwrite versions. Agent/worker/delegated
   configuration authority does not grant secret enrollment.
3. A different **root human principal** reviews the exact revision. Self-review
   fails closed. Both identities must still be live at claim and final store.
   Local bearer identities do not prove two independent people: organization-grade
   human authentication/separation of duties remains an activation prerequisite.
   A root-authored proposal cannot self-approve; use the independently authored flow.
4. Within five minutes, the reviewer selects **Enroll secret** or **Import existing
   secret**. Both use one hidden native dialog; neither creates a renderer token
   field. The dialog displays purpose/provider/audience/project/version. Import
   means entering an existing value, not reading arbitrary Keychain items/files.
5. Input is exact UTF-8 text, 16–16,384 bytes, with no C0/DEL controls, NUL, newline,
   trimming or normalization. Multiline YAML kubeconfigs are unsupported; a purpose
   label alone does not broaden the accepted encoding. Single-line JSON can be
   enrolled, but no parser/provider validation or live-use support is implied.
   An existing Tessera authentication token is refused as a provider value.
   Purpose/provider/audience tuples are closed; Salad management and AI Gateway
   references cannot substitute. Without a provider call, arbitrary key bytes
   cannot establish which account/service issued them; native confirmation supplies
   the declared purpose and activation must independently establish suitability.
6. The helper commits an **enrolling claim before prompting**. The claim consumes
   the approval; cancel, timeout or crash cannot replay it. After entry, main
   rechecks the selected project and connection generation before sending a
   one-time commit confirmation. The helper rechecks database authority/revision/
   deadline, commits a separate one-use write intent, adds the version to Keychain,
   checks presence without reading it back,
   then commits metadata, audit, project event and idempotency together.
7. Confirmation is metadata only: version, lifecycle, cleanup state, store and
   provider-disabled status. There is no echo, copy/reveal button or secret-get IPC.
   Lookup returns only presence/version/store metadata. Locked/denied/unavailable
   Keychain is an error, never permission to save a plaintext fallback. Unlock or
   repair Keychain through trusted OS administration, then reconcile the consumed
   enrollment rather than retrying it.

Advanced same-host import uses `python -m tessera_control.secret_enrollment_cli
--state-dir /absolute/private/state --secret-fd 3`. This is a native integration
protocol, not a shell command for pasting credentials: a trusted launcher provides
current auth + the closed `ProtectedRequest` on inherited stdin, and secret bytes
on a separate **private inherited pipe/socket** ending at EOF. No regular file,
TTY, argv, env value or renderer-selected FD is admitted. It must answer the
metadata `confirm` phase with `{"commit":true}` only while its context remains
current. The pipe is UID-checked, byte/time-bounded, and never reopened by path.
Use the desktop prompt unless implementing that trusted launcher.

## Lifecycle, failure and recovery

* **Rotate:** the original independent human proposer requests a new version at
  the current revision. This immediately fences the old version and invalidates
  bound configuration verification; root must review the new proposal, then enroll
  the new value. Other project humans and configuration delegations cannot rotate.
  The same opaque reference is retained, version increments atomically, maximum
  100. Rotation is not provider-side revocation and does not terminate jobs.
* **Revoke/expire:** root's metadata command immediately blocks new resolver use.
  Natural expiry is checked at use/read without a background job. Already returned
  buffers cannot be recalled, and provider sessions/jobs may still exist.
* **Delete:** revoke/expire first, then tombstone metadata. History is retained;
  tombstone is not a claim of physical deletion. **Purge inaccessible OS-store
  versions** deletes old/revoked/expired/deleted versions and clears cleanup-pending
  only after absence checks. Active current versions are never purged. Unavailable
  stores leave cleanup pending. Reconcile again explicitly; no effect auto-retries.
* **Crash/partial store:** the DB claim precedes the nontransactional OS write. An
  exception attempts deletion best-effort. A killed process may leave an orphan
  Keychain item while the ledger remains `enrolling/unknown/cleanup_pending`.
  Never promote it by guessing or replaying. Inspect, revoke, purge, and create a
  fresh reviewed proposal. No automatic orphan scan or secret enumeration exists.
* **Stale/disconnect:** main cancels the helper and suppresses old responses. A
  prompt cancelled before commit cannot store. A commit already in progress may
  have completed when disconnect arrives: the UI must report uncertain outcome,
  inspect authoritative state, then revoke/purge if necessary. Distributed
  cancellation cannot promise rollback of an already committed effect.
* **Backup:** control backups retain references, version history, claims and audit,
  **not Keychain values**. Treat them as sensitive metadata. Keychain backup,
  access-control/recovery and OS account continuity are separate operator duties;
  copying SQLite cannot recover missing keys. A restored held database cannot
  mutate or resolve custody. Never activate a copied DB against the original
  Keychain; re-enroll under fresh approved authority on the intended host.
* **Incident:** halt new admission; revoke affected control authority; retain
  sanitized audit/claim history; use independently trusted provider administration
  to revoke/rotate the actual key under separate authorization. Local purge does
  not revoke a provider key. Reconcile jobs/costs independently; no stop/deletion
  claim follows from logging out. Re-enroll only after trusted-host recovery and
  fresh human review. Never paste keys, raw process dumps or headers in tickets.

## Threat model and limits

Protected against: renderer state/snapshots/localStorage value retention, generic
read IPC, untrusted subframes, metadata value injection, cross-project/tenant IDs,
stale connection/project context, self-review, replay, concurrent overwrite,
wrong-purpose use, unavailable-store downgrades and inferred crash success.

Plaintext exists only in the native prompt/helper and Keychain, or an explicitly
admitted trusted server resolver's short-lived `SecretBuffer`. Owned mutable
buffers are wiped best-effort, native allocated result memory is wiped/freed, core
dumps are disabled in the one-shot helper, and the helper has 90-second total,
60-second entry, bounded pipe/output and no-retry limits. Native stderr is discarded;
errors are closed codes; no request/argument/body logging is added. Audit fingerprints
hash **reference + version metadata**, not low-entropy secret values. There are no
secret bytes in SQLite, responses, events, idempotency or provider receipts.

Not protected against: a compromised OS/root/same-user administrator, malicious
helper/runtime, debugger, keylogger, screen/clipboard capture, swap or privileged
memory inspection. Python/AppleScript/OS libraries may make immutable copies;
whole-process erasure and OS diagnostic exclusion cannot be guaranteed by buffer
wiping. Do not enable tracing/crash-memory capture on the helper/proxy. Keychain's
default application ACL may trust the Python executable rather than a dedicated
signed Tessera helper: review/restrict OS custody before production deployment.
This is OS-backed storage, **not a hardware-backed enclave or production-hardening
claim**. A dedicated signed native helper and platform-specific conformance remain
future hardening work.

`TrustedServerResolver` is a non-HTTP, non-IPC composition interface. Its required
admission callback runs within the DB transaction and must authenticate the trusted
server/proxy and validate grant/lease/epoch/purpose for **each use**; no default
admission is provided. Resolution rechecks tenant/project, current approved root,
version, purpose/audience, expiry, lifecycle, custody backend and recovery hold,
then records a redacted use audit. Audit failure wipes the returned buffer and
fails closed. No runtime service identity, worker raw-read endpoint, credential-lease
auto-attachment or live proxy wiring is added. OS adapters are trusted internals,
not independent authorization services; do not expose their low-level methods.

Provider live activation still requires all [Salad proxy conformance gates](salad-proxy.md#live-enablement-checklist--all-currently-unresolved),
service identity/least-privilege deployment, independent monitor and termination,
cost/exposure accounting and external artifact custody. Enrollment success is
neither provider validity nor permission to spend.

## Verification

Default tests use temporary control databases, a conspicuously non-production
memory store, stubbed Security-framework functions/native prompt/child process,
and synthetic values. They include real helper-process death after a claim but
**no real Keychain or provider calls**. Protected native-prompt/real-Keychain E2E
conformance remains unrun; do not infer it from fake ABI or desktop shell tests.
No schema migration or provider activation is introduced.

Recorded task checks (2026-09-24 UTC; no provider effects):

* `.venv/bin/pytest -q tests/test_secret_native_boundary.py
  tests/test_protected_secrets.py tests/test_admin_configuration.py
  tests/test_salad_proxy.py tests/test_salad_contract.py
  tests/test_credential_access_leases.py tests/test_credentials.py` — **231 passed**,
  two existing FastAPI/Starlette deprecation warnings, 340.61 seconds. Earlier
  120/300-second invocations timed out; the completed run used a 600-second bound.
* After the final natural-expiry/tombstone projection correction: the two
  `test_natural_expiry_purge_confirmation_and_tombstone_preserve_truth` cases,
  two `test_revoke_expire_delete_and_wrong_resolver_purpose` cases and
  `test_concurrent_claim_and_versioning` — **5 passed**, same two warnings,
  14.17 seconds.
* `npm test` in `desktop/` — **167 passed**, no failures/skips. Includes actual
  main/preload dispatcher and renderer-state tests with synthetic native boundaries.
* `TMPDIR=/tmp/opencode node test/electron-smoke.cjs` in `desktop/` — passed:
  packaged app loaded, sandbox/context isolation true, Node integration false,
  renderer errors empty, hidden native window, teardown exit 0. This was a shell
  smoke test, not a visible interactive enrollment demonstration.
* Scoped Ruff lint and format checks — passed on the eight changed Python runtime
  files and two new Python test files. `node --check` — passed on main, connection,
  protected helper bridge, renderer, preload and IPC contract. `git diff --check`
  passed. No dependency installation, real credential access, cloud deployment,
  commit or push occurred. Full-repository integration pytest/lint was not run;
  substantial unrelated pre-existing edits were preserved.
