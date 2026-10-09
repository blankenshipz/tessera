# Opt-in installed remote-agent journey

Local record IDs in this document identify historical records retained outside
the source distribution. They are not downloadable artifacts or independent proof;
the measured facts and limits are summarized here.

## Installed local checkpoint — 2026-10-04

Durable sanitized receipt record ID: `REMOTE-PLATFORM-PARTIAL-2026-10-04`.
This is **partial installed/protocol proof**, not complete integrated acceptance.

| Boundary | Observed installed evidence |
| --- | --- |
| Distribution | Wheel SHA256 `1c74141cab11fc468800aee21467f5c5ef8c7c30086555ba5c6aa10c13ec5686`; MCP 1.26.0, A2A SDK 1.2.1, httpx 0.28.1; host/image `pip check` passed. |
| Image | `docker.io/library/tessera-platform-installed@sha256:d318a56424abe080967b3efb0e75ffa2237bb25d3e0e492b89d74b2a4be18bf2`; verified OCI import into owned Docker Desktop node. |
| Backup/migration | Installed backup preserved schema 50, event watermark 8848, 43,905,024 bytes, database SHA256 `cdcc5995ceb636f594e86b2c5b96b1585b8df719e86b5b25daff68ec1901473c`; UID/resourceVersion-tested Recreate rollout ran installed `tessera migrate`, reporting 51. |
| Route/control | Installed `tessera connection-tunnel`; TLS readiness 200; independent owned restart passed container-start generation-change assertion and returned ready. |
| Worker | Fresh human-enrolled worker ran with separate private journal; authenticated status 200 before expiry. Historical expired worker/journal preserved, not renewed. |
| Network | Causal allow/deny/allow witness at 18:49:29 UTC. Gateway selector repair affected only two run-owned policies; empty critic ledger verified before retry. |
| Protocol | Installed official SDK passed planner proposal, authenticated critic discovery, reference-only critic task with real status-only delegation, authority request, separate human grant issue and agent admission. |
| Execution | Package HTTP 409: `Execution lease is no longer current.` No Job submitted; job UID/output digest are null. |
| Restart/revocation/export | Human revocation succeeded. Human experiment export 200, SHA256 `fec1276aefe87875fd2cff266700c2aa07794b9d317c874b7f4cb62a31dd077c`; joined notebook watermark sequence 8/event cursor 9190, explicitly `partial_page`, `has_more: true`. |

Source frozen gate digest remains
`8973ecfd5617903b5a108d961a97f1607928b3f54338ac288b0904a9175454d9`.
At the time of this first checkpoint the full gate had not been repeated; a later
sequential frozen-gate receipt completed all shards on this unchanged digest (see
the latest continuation below). That gate predates the later recovery repair.
Docker builds timed out and cached Python 3.12
had zero-byte Python/pip files. The executable retained Tessera r4 image was
therefore used in a dedicated labeled credential-free installer container: a new
`/platform` environment installed the final wheel, then the container filesystem
was committed/saved/converted to verified OCI. No git commit or registry push.
Private build/deploy scripts and logs remain under
`.tessera/private/remote-agent-platform-20261004/` (never commit this directory).

Control/workload namespace UIDs remained
`84ffd885-ed3e-4cf9-8941-59a5123cb3e1` /
`1869980f-998a-4ef9-a7ea-d500326150b5`. Existing Deployment UIDs were preserved.
Historical Jobs were retained; workload object quota was narrowly increased
5→6 Jobs and 8→13 Pods, storage unchanged at 1 GiB. Only three run-owned witness
Pods were UID-precondition deleted for refresh. Two separately supervised
installed reference peers have independent credentials/PVCs; public-CA bundle
mounts keep TLS verification enabled. This is not full two-peer acceptance.

### Precise continuation blocker

Lease `lease_8de99f8bedbd4fdba77e6038b2ed39d7`, frozen epoch 2, was issued
18:53:59.581419Z, expired 18:54:29.581419Z, hard expiry 18:55:59.581419Z.
Authority/critic preparation after leasing exceeded its actual 30-second window.
Admission remains retained for experiment
`experiment_e62132d209d24a6ab05db2d17e4da55f`, execution grant
`grant_f4fe9a57fa9642cf916c4218c81b7b20`; no execution receipt is fabricated.

Resume at 19:08 UTC rechecked namespace/Deployment ownership and all three
loopback listeners. Same image remained installed. The planner, critic and new
worker credentials expired at **19:02:01.910842Z**, and all three real authenticated
API checks returned **401**. Expired worker credentials cannot be revived
(`store.renew_worker_credential`). Current continuation explicitly prohibits
recreating identities, so fresh enrollment requires a parent assignment correction,
not a bypass or silent replacement. Preserve and reconcile this admission/history;
then explicitly authorize a distinct fresh bounded run with adequate credential
lifetime. Acquire lease immediately before final execution bindings/admission and
issue package within 30 seconds, dispatch before hard expiry. Reuse the installation
and passing evidence; do not rebuild or rerun the full gate.

Remaining: ordinary Job/output custody, expansion/replay/expiry/revocation consume,
full two-peer cancellation/cross-ledger denial, correlated critic outbox and complete
notebook pagination. Human notebook access uses experiment export; the agent-only
notebook route correctly returned 403 for a human credential. No model inference
ran: critic is reference-only, research OAuth profile is not admitted. Independent
host/laptop-off, Linux install and native desktop picker remain unverified.

### Distinct authorized run 2 — supersedes the enrollment blocker

The parent explicitly authorized a distinct fresh run, preserving prior identities,
grants, journals and receipts. The harness setup lifetime cap was increased from
30 minutes to **two hours**, still bounded; the existing actual isolated enrollment
test now exercises the two-hour lifetime, with new 59/7201-second rejection tests.
`45 passed in 15.56s`; scoped Ruff lint,
format (2 files) and `git diff --check` passed. Product/service source and installed
wheel/image are unchanged; the prior frozen full gate was not rerun.

Fresh planner/critic/worker enrollment and UID/resourceVersion-tested owned
rollouts succeeded. New worker journal `/worker/platform-journey-r2` and separate
peer databases `/peer/run2.sqlite3` preserve older files. Setup had confirmed
planner/critic enrollment before rejecting stale worker target evidence; a real
causal witness was refreshed and the operator explicitly rebound only its target
digest, preserving pre-rebind state and both confirmed credentials. No unknown
command was retried under a new key. Private reconciliation receipt documents it.

**Actual product recovery blocker, not missing permission:** old execution grant
`grant_f4fe9a57fa9642cf916c4218c81b7b20` holds the project's single concurrency
reservation. Installed read-only SQLite observation confirms **1 lease, 1 admitted
attempt, 0 operations, 0 dispatches**. Ordinary
`reconcile-expired-undispatched` returned 409:
`Expired operation is not provably undispatched.` Fresh grant issuance returned
409: `Grant would exceed the project execution concurrency limit.`
`ExecutionAuthority.reconcile_expired_undispatched_grant` currently requires exactly
one operation; zero operations after a lease expires before package issuance are
not recoverable through that endpoint. Ordinary reconciliation instead requires
authenticated termination evidence, which does not exist. Do not fake termination,
revive the expired worker, modify SQLite, or widen project concurrency to bypass it.
The backend authority owner must implement/test a narrowly fenced transactional
no-package/no-dispatch recovery path (preserving all history) and install the repair,
then resume this same child with existing fresh credentials/resources. This is a
high-risk authority/accounting boundary outside the harness's write ownership.

Independent installed checks completed in run 2:

- Both actual peer Agent Cards authenticated with the fresh identities, JSONRPC
  protocol 1.0; invalid bearer POSTs returned 401. Full delegated exchanges remain
  gated on executable grant recovery.
- Real installed MCP `tessera_request_review` succeeded for the fresh experiment.
  A blocked-execution observation against the older admitted attempt was rejected;
  it is not counted as passing observation/execution evidence.
- Real installed MCP notebook export followed **6 pages** using both cursors,
  reaching `{sequence: 8, event_cursor: 9275}`, `has_more: false`; page-set SHA256
  `a49cfaed190c2f4d64e8e0eebc4b293a0d61148b76f78e82cd2a1f7315649f54`.
  Pagination is exhausted, but `completeness: partial_page` and historical/off-platform
  unknown coverage remain truthful. This does not establish complete execution or
  critic outbox delivery.

Sanitized run-2 receipt record ID: `REMOTE-PLATFORM-RUN2-BLOCKED-2026-10-04`.
No new Job, output digest, inference, commit, push or external deployment occurred.

### Installed recovery and execution continuation — 20:32 UTC

The backend no-package recovery repair was installed, not merely unit-tested:

- New wheel SHA256 `116006bf571e78122b96474c379ec2799d44d837abefd6d5117ea5bf385c506a`.
  Image `docker.io/library/tessera-platform-installed@sha256:42739147450ce86901a76ea872594d78edf5b2de1cf79b623c4c54e033155e3a`.
  Tested source digest `2d93d6ecc1fe8134d4aa4cf82e85ab8bbf79dc7f4e180292a5e1a1c95c1d879c`;
  27 targeted recovery/lifecycle checks were supplied by the backend owner.
  **No new full-gate pass is claimed.** Image `pip check` passed. The owned
  control/worker rollouts succeeded after correcting the imported containerd tag
  alias; state, namespace and Deployment UIDs were preserved.
- Separately authenticated human recovery POST returned 200; same-key replay was
  identical. Installed read-only observation proves one recovery event, retained
  one lease/one admission, zero operations, `outcome: not_executed`,
  `package_absent: true`, `outbox_absent: true`, and
  `provider_termination_inferred: false`. A fresh grant subsequently issued,
  demonstrating reservation release without increasing project concurrency.
- Both installed independent A2A peers passed official SDK submit/get/result,
  exact-message replay, changed-key denial, stale-epoch denial, cancellation and
  cross-ledger denial. Actual task IDs:
  `3c214920-d245-4547-b8ee-5ef97c8cacab` and
  `79c6fa2b-a1d2-4106-95b1-b8c47a1c6942`.
  Results are explicitly **reference-only**, not model reasoning or critic outbox
  provenance delivery.

Integration ordering corrections are retained honestly:

1. Preparing a package before admission produced a real operation; subsequent
   agent admission returned 409 `Execution grant already has an operation or
   package intent and cannot be admitted.` The supported sequence is **lease →
   freeze pins → agent admission → package → dispatch**, automated within the
   30-second lease window. A package-before-admission instruction is incompatible
   with this contract. The package-only grant was retained until its real expiry,
   then recovered through the ordinary human API; no early termination was faked.
2. A private wrapper initially rejected package **201 Created** as if only 200
   were success. It was corrected to accept both. That undispatched package was
   likewise retained/recovered at its short expiry. Snapshot releases were
   separately human-revoked before replacement, as the API requires.
3. Execution attempt 4 passed installed proposal, critic task, authority request,
   human issue, agent admission, package 201 and dispatch. The worker correctly
   did **not** claim its target: private preparation omitted `network_none_evidence`
   when computing the cluster identity digest. Actual worker digest was
   `205911d6dc13f519e4b946cf92297978b574699f9c50039ceb9f66548a717339`,
   while queued dispatch declared
   `65dc37b46a1193858ea873a203b8e91c278b8a5474ce1bcd7ae2db365f45d2dc`.
   Private preparation now uses the same witness-inclusive contract as ordinary
   worker `configure`. Already-issued immutable bindings were **not** rewritten.

Retained operation `6b7a9a43c713b288f556acd6faea0f08`, dispatch
`kubernetes_dispatch_1d16b8f4b04943d78abf470738776360`, grant
`grant_ff4b97f3a5b941cfb540a57b91d97504` were human-fenced: revoke returned 200,
`cancel_requested`, epoch 3. No provider termination is inferred. Exact Job lookup
`tessera-op-6b7a9a43c713b288f556acd6faea0f08` found **zero** matches; historical
Job count remained five. Job UID/output digest remain null. The output poll was
operation-filtered and did not accept old artifacts.

After repeated integration objective failures, this leaf hands back the retained
queued/outbox cancellation boundary rather than bypassing its accounting. Next
owner: root ordinary-worker/control recovery lane. Use the original valid run-2
worker identity/journal and fresh provider absence evidence to complete supported
never-submitted/queued cancellation reconciliation; do not assert provider
termination or release reservation from absence alone. Then resume this same
journey with the corrected witness-inclusive digest, fresh witness, and legal
admit-before-package sequence. The run-2 credentials expired at 21:14 UTC; any
distinct bounded replacement requires explicit authorization and must preserve
all prior histories. Remaining actual acceptance: Job/output collection,
dispatch replay/expansion/expiry/revocation fences, correlated critic outbox and
final joined execution notebook. No inference, off-host or laptop-off proof.

### Latest installed recovery handoff — 2026-10-04

The previous source baseline later completed sequentially on unchanged digest
`8973ecfd5617903b5a108d961a97f1607928b3f54338ac288b0904a9175454d9`: Ruff passed,
format covered 564 files, and shards 1/2/3/0 yielded **3454 passed, 0 failed,
1 skipped** (1056/591/1129/678). The optional A2A SDK was absent for the skip.
This baseline predates the recovery change below; local evidence record ID:
`SOURCE-GATE-PRE-RECOVERY-2026-10-04`.

The no-package recovery route was implemented and installed in wheel
`116006bf571e78122b96474c379ec2799d44d837abefd6d5117ea5bf385c506a` / image
`sha256:42739147450ce86901a76ea872594d78edf5b2de1cf79b623c4c54e033155e3a` from
source revision `2d93d6ecc1fe8134d4aa4cf82e85ab8bbf79dc7f4e180292a5e1a1c95c1d879c`.
The owner reports 13 focused recovery cases (53.42s), 14 existing lifecycle tests
(24.89s), and 27 targeted installed checks passed; no full-gate pass on this
revision. Human
recovery returned 200 with identical same-key replay. It retained lease/admission
history, recorded `not_executed`, found no operation/package/outbox, and inferred
no provider termination. This handles proven no-package recovery only.

The subsequent installed journey passed proposal, human approval, admission,
package 201 and dispatch, but failed retained-output retrieval. Private prep had
omitted `network_none_evidence` from the worker identity digest: observed
`205911d6dc13f519e4b946cf92297978b574699f9c50039ceb9f66548a717339` differed from
the immutable queued-dispatch digest
`65dc37b46a1193858ea873a203b8e91c278b8a5474ce1bcd7ae2db365f45d2dc`. Preparation
now includes that witness and admits before package; already-issued bindings were
not rewritten. Human-fenced cancellation is `cancel_requested`, not reconciled.
The exact expected Job lookup found zero matches (five historical project Jobs);
Job UID/output digest remain null. Resume with the original worker/control and
provider-absence evidence. Do not use SQL, infer termination from absence, or
release authority without supported reconciliation. Handoff record ID:
`REMOTE-PLATFORM-RECOVERY-EXECUTION-2026-10-04`.

Two installed independent A2A peers passed protocol exchange but returned
reference-only results, not reasoning. MCP review passed while observation was
blocked with no authority effect. Six-page notebook stream metadata exhausted at
sequence 8/event cursor 9275 but remained `partial_page`, with historical/off-platform
coverage unknown. Thus there is no retained-output or full integrated acceptance.
No reasoning, independent-host or laptop-off proof is present. Quota telemetry
temporarily timed out; the root later observed healthy (~35% remaining) telemetry
and resumed the same child. No paid inference fallback was used.

### Installed queued recovery — original credential expired

The tested queued-recovery source was installed on the owned control:
source digest `d0215950de207469c2945ef4af162ec55dfd073183130e3a8bdd8668962420ef`,
wheel SHA256 `16d7fad8b4d500f7983daef856692488e7bcb2c6da8d26cdd126a4181d7f8827`,
image `docker.io/library/tessera-platform-installed@sha256:2f91a7ecbb920d1f51af40c9a4a2f2c3b7bc128a60e1768bae018f460a4fc981`.
Image `pip check` passed; owned control rollout and TLS readiness 200 passed.
The implementation owner supplied 28 isolated recovery cases; this is **not** a
new full-gate or live-recovery pass.

At **21:08:40.841950 UTC**, the original worker
`principal_dd9393466e764873ae6353380dab5c0b` authenticated with `/worker/status`
HTTP 200, expiry **21:14:03.413936Z**. The exact original Deployment was
UID/resourceVersion-fenced to **zero replicas**, and its prior Pod deletion was
observed before an exclusive recovery Pod mounted the preserved original workspace.
Recovery Pod UID `f302945d-e79e-43f1-a3bd-7d51614b961f`, ServiceAccount
`tessera-dispatch`; no replacement bearer or journal was minted. The new installed
CLI was run with original target, workspace and Kubernetes credential references,
plus the non-secret current grant/dispatch binding and
`--exclusive-stopped-worker`.

**Real provider permission denial:** proof stopped at the operation-scoped
Job-list GET with **Kubernetes HTTP 403**:
`kubernetes_queued_recovery.py:264` →
`kubernetes_preparation_recovery.operation_objects` →
`/apis/batch/v1/namespaces/tessera-workloads/jobs?limit=100&labelSelector=...`.
Namespace/deterministic reads did not substitute for that required complete list.
No proof/attestation was submitted, no human release was submitted, and permission
was not widened or bypassed after identifying the denial.

At **21:14:21.603449 UTC**, the same original bearer returned **401 Unauthorized**.
Historical recovery is now blocked by original-identity expiry as well as the
required provider read permission. A replacement identity cannot attest this
historical effect, and expired credentials must not be revived. The queued grant
remains `cancel_requested`, revision 2, epoch 3. Original worker remains stopped;
its journals, tokens and historical records are preserved. Provider termination
and `not_executed` for this queued attempt remain **unknown/unestablished**.

Concrete next owner: root control/operator recovery lane. Correct the documented
recovery capability's required workload Job/Pod list permissions through an
explicitly approved, narrowly scoped ServiceAccount policy; separately resolve
the now-expired-original-identity lifecycle via the supported lost-worker/quarantine
path or an explicitly reviewed contract change. Do not fake original attestation,
renew the expired credential, use a replacement bearer, modify SQLite, or release
accounting from absence alone. Original proof effect is stopped; unchanged prior
MCP/A2A evidence remains valid. Ordinary Job/output/full integrated acceptance and
real reasoning/off-host proof remain open.

Sanitized receipt record ID: `REMOTE-PLATFORM-QUEUED-EXPIRY-BLOCKED-2026-10-04`.

`dev/remote_agent_platform_live.py` is an acceptance **orchestrator**, not a
replacement control plane, reasoning runtime, worker or critic. It does not
deploy/start/restart services, provision Kubernetes or invoke a model. Its explicit
opt-in setup enrolls bounded identities through a separate human API credential.
It uses the actual MCP 1.26 SDK stdio client against an installed
`tessera_mcp.server` subprocess, authenticated HTTP against the separately running
critic's A2A 1.0 Agent Card / JSON-RPC via official `a2a-sdk==1.2.1`, and a separately authenticated
human client for issue/revoke/read. Protocol calls alone do not prove reasoning.

## Installed human maintenance and distinct run 5 (2026-10-04)

This supersedes the preceding *current blocker*, not its historical 403/401
evidence. Receipt record ID: `REMOTE-PLATFORM-MAINTENANCE-RUN5-2026-10-04`
(SHA256 `850a9a03728b0c72f812d68ca914420eace25a0fccd0a28ecae56b7783d96bbd`).

- Explicit parent-approved namespace-only Job/Pod GET/LIST Role and RoleBinding
  were created after cluster/namespace UID and operator authority checks. Role UID
  `a931f921-6329-44bd-b2bf-75fe571d77a8`; existing `tessera-dispatch` ServiceAccount
  UID `f64ea279-bf88-4281-a197-b20e9808767b`. A new ten-minute token stayed in private
  references. No cluster-wide RBAC, secret-read permission or Job write was added.
- Installed maintenance source `73fd7d1c7b19d6136a188f4eeaa7a90139bc9a1122276e5da9de121085a21183`,
  image `docker.io/library/tessera-platform-installed@sha256:fac7adde2679c3ff6a36ff3e1c1103c653988ec3b9cab6b42983d198b834a63d`.
  Image `pip check` and control rollout passed. Original human issuance returned
  201; actual proof CLI used the preserved exclusively held original workspace,
  original target and new authorized read reference. Reconciliation returned 200
  with exact same-key replay and `reconciled`. No expired bearer was revived,
  replacement worker attestation substituted, or provider termination inferred.
- Distinct bounded run 5 used fresh worker/planner/critic credentials, a new
  workspace, fresh causal network witness, lease-before-pins and
  admit-before-package. All 16 configured installed journey stages passed through
  actual MCP SDK/services, including dispatch reply-discard replay, real control
  restart generation, expansion 403, expiry 403 and revoked epoch 409. These last
  status expectations were corrected in the private wrapper; prior passing
  stages were reused, not replayed as new effects.
- Actual Job operation `5eec1cadfab46be42210a9661b128312`, UID
  `6fbfa403-6481-4dc6-a004-32982777474f`, completed. Ordinary automatic collector
  retained 36 bytes, SHA256
  `db53c572656c33d1268675110505a1fe3f4e7a9a72a9cac82c9e769cb36e815f`.
  Human `retain_quarantined` disposition and custody release permitted actual
  content GET; size/digest and public CPU result 385 matched.
- **Lifecycle limitation:** control lease cancellation recorded an interrupted
  operation with attributed exit/termination, and output was late/quarantined.
  Human success resolution was correctly rejected 409. Supported human cancelled
  resolution and prior execution accounting reconciliation passed. Kubernetes
  completion is not relabelled as control success or scientific acceptance.
- Additional installed MCP observation/review calls passed. A separate bounded
  grant exercised admit/status/cancel in a new session after restart. This was
  pre-submission cancellation: no lease/package/Job and no running-Job termination
  claim. After real expiry its no-operation accounting recovery returned 200.
- Final dual-cursor MCP export exhausted eight pages at sequence 50/event cursor
  10877, `has_more=false`, SHA256
  `399382ab60cb55c9f1beb5f5ac643009eeca2f3d37c6c93cc19d1eb27966d786`.
  It remains `partial_page`, historical/off-platform coverage unknown and
  `redacted_metadata_only`: operation/review correlation exists, but this export
  does not include the retained output manifest ID. Correlated critic provenance
  outbox delivery is not established. Reference-only critic is not reasoning.

Ordinary worker lease renewal already exists: the supervised service renews every
10 seconds via durable-key `POST /worker/leases/renew`, respecting grant epoch and
immutable hard expiry. Focused lifecycle tests reported 24 passed, 38 deselected,
two dependency warnings and 115.45 seconds; the outer shell timed out after the
pytest summary, with no shell exit code. See
[`ordinary-worker-renewal-diagnosis.md`](ordinary-worker-renewal-diagnosis.md).
Renewal is not a missing implementation seam.

Two implementation seams remain: validated output-manifest/artifact/attempt/
operation/Job UID correlation in notebook provenance for late/quarantined/
cancelled/unknown states; and content-free reference-peer message/result/cancel
outbox with ordinary drain independent of the reasoning bridge. Latest local
collector integration still fails its final acknowledgement check because the
shared client sends optional null-reference keys the collector response omits; the
two-peer run reached but did not pass the final ack assertion. The run-5 receipt
does not contain a causal timeline for renewal actions, cancellation reason/actor,
revocation timing, terminal Job reporting or collector upload. Collect that
sanitized timeline after the provenance/A2A writers quiesce, then run normal lease
→ final pins → admit → package → dispatch and let the existing worker supervise
to supported success. Test expiry/revocation separately. Then run the final full
Python/install gate and verify a complete joined export.
Prior two-independent-peer A2A replay/denial/cancellation evidence remains valid.
No dedicated reasoning OAuth admission or inference, independent-host/laptop-off,
Linux installation or native picker proof is claimed. Historical records and
journals were preserved; no commits or pushes occurred.

## Schema 52 installed; historical joins and peer drains verified

Sanitized receipt record ID: `REMOTE-PLATFORM-SCHEMA52-QUOTA-BLOCKED-2026-10-04`
(SHA256 `6f5af0cdcd06ed19f63fa7b43886d40c44ad7de091cfcbdac7f0b996f30cdef4`).

- Before rollout, installed `tessera backup` preserved schema-51 state, objects
  and manifest; a private copy remains under the run6 directory. Historical
  schema-50 backup and all journals remain intact. Explicit migration init
  reported **52**, control rollout passed, and image `pip check` passed.
  Installed image:
  `docker.io/library/tessera-platform-installed@sha256:ff90ef05b7ba771c2ee400b3f475e29eeda48cc84a74fe2b61583eb5a2f2b950`.
- Run5 original ordinary-worker journal was inspected read-only: zero renewal
  actions, terminal exit observed `21:53:20.499345Z`, lease expired
  `21:53:32.979713Z`, manifest recorded `21:53:34.220823Z`. Collection crossed the
  deadline after terminal-before-first-renewal. Human cancellation at
  `21:55:11.242538Z` was later, not the cause of initial late collection. The
  closed cancellation reason remains unrecorded. No new keeper/renewal loop or
  worker behavior was introduced.
- Current explicit human artifact disclosure returned 201. Agent typed
  `output_proof` returned 201 after an actual deterministic Job GET matched the
  original UID. Nine-page installed agent notebook export joined the exact run5
  manifest digest, artifact hash/36 bytes, admitted attempt, operation and
  `recorded_uid_hash_match` Job UID. It preserves `late=true`, quarantine,
  `retain_quarantined`, released custody, cancelled outcome and scientific
  validity not assessed. Agent export SHA256
  `e5a0ea74a1e54103d31105773170129c9a3e896937eaa470e8d69014402758d7`.
- Both independently installed peers completed new SDK submit/result/exact replay.
  Their metadata-only installed `drain-outbox` CLIs each acknowledged four
  records, pending zero; the paginated notebook contains all eight with null
  model attribution. Task IDs `4456bcde-f73a-4d77-8f5f-dcadbdb62046` and
  `bb4dd15d-116d-42c2-a894-1c53fe87dc4d`. Metadata delivery profiles are not
  reasoning admission; no prompt, harness connection or inference was used.
- New peer startup correctly rejected a projected Secret symlink under the strict
  private-file reader. UID/resourceVersion-fenced init now materializes that same
  approved config as an owned regular mode-600 file; both rollouts passed.
- Setup attempts6/7 created no package: normal-only wrapper preflight and reused
  A2A message ID were corrected. Supported no-package reconciliation returned200
  for each; releases were human-revoked. Histories were not rewritten.

**Current actual execution blocker:** distinct run8 passed installed proposal,
critic reference exchange, authority request, human approval, admission, package
201 and dispatch. Original ordinary worker failed real Job create at
`2026-10-04T23:27:33.082525+00:00` with **403 `kubernetes_quota_exceeded`**.
Workload quota UID `9f8eb40f-b008-4bfa-bb88-64acfdbe952c` limits Jobs to6; used6,
all retained historical Jobs. No Job/output exists for operation
`59a660bc4931e050c240e35a33d75677`, dispatch
`kubernetes_dispatch_0ee5b2038a6d4501ba1065ccd99d11c4`, grant
`grant_6ba68b6bef8247b08ae68482336a77f2`. Control attempt remains unknown; worker
retains cancel-requested failure evidence. No termination or accounting release
was fabricated. No quota widening or historical Job deletion occurred.

Root operator next action: preserve original run6 worker identity/workspace and
recover its run8 failed preparation through supported proof/reconciliation, then
choose explicitly permitted exact owned terminal cleanup versus a freshly owned
namespace with normal target approval. Do not delete unrelated/historical Jobs or
silently raise quota. Resume a distinct normal attempt only after accounting and
resource headroom are honest, retain ordinary supervision until timely output and
supported success, then execute separate negative stages. Prior run5 negative
evidence remains historical; it is not a schema52-image rerun. Final source gate,
reasoning profile and external-host/device acceptance remain pending.

## Fresh isolated target: normal installed success (2026-10-05)

The root explicitly authorized a fresh owned workload namespace and, if needed,
an isolated same-tenant acceptance project. No old quota, NetworkPolicy, Job or
held reservation was mutated. Six historical Jobs remain in `tessera-workloads`;
run5 late/cancelled history and run8 actual quota403/unknown operation remain held.

- Namespace `tessera-platform-workloads-fresh`, UID
  `abbba7c3-ddf9-4ce2-9f1c-e4a756ece1f8`, has restricted Pod security,
  default-deny CNI policy and causally witnessed allowed/denied traffic. Quota:
  **3 Jobs, 8 Pods, 1GiB output storage**. Peak calculation is one PVC binder +
  three witness Pods + three Job Pods + one transient collector = eight. Observed
  final usage is one Job/five Pods/1GiB. The ordinary worker Role is limited to
  this namespace; identity GET is restricted to exact names `kube-system` and the
  new namespace. No secret-read, arbitrary exec or cluster-admin grant was added.
- Human created isolated same-tenant project
  `project_869149ff7bae473bbfee654731f413c1`, configured zero monetary exposure and
  **concurrency one**, and adopted explicitly reviewed public input through the
  installed CLI. New bounded two-hour identities and a separate supervised worker
  Deployment/PVC use the new target. Worker Deployment UID
  `235e3a58-29d4-4bc7-8143-74590009d152`; original run6 worker/journals remain intact.
  The initial enrollment rejected mismatched old namespace/PVC pins before worker
  admission; corrected enrollment was distinct and its history was preserved.
- Ordinary supervised execution used reviewed 180-second task/hard bounds,
  lease-before-pins, legitimate admission-before-package, package201 and dispatch.
  No harness keeper or new renewal behavior was added. Actual Job UID
  `fbfc603e-9d6a-4a41-a0ad-1ffb51c778fb`, operation
  `9fb39348944202be13192dbd93699585`, completed. Manifest
  `artifact_manifest_c8ab0f2c805cfbf8446edf1a517b7806` was recorded **on time**
  (`late=false`) at `23:57:33.928568Z`, before lease expiry `23:57:45.444268Z`.
  Manifest digest `3346efc4e16b5791b3a1d997f89a77e9451f041604a5b8aca3d9302edcc45f57`.
  Actual human content GET matched 36 bytes, public value385 and SHA256
  `db53c572656c33d1268675110505a1fe3f4e7a9a72a9cac82c9e769cb36e815f`.
- Supported human resolution returned200/`succeeded` for attempt
  `experiment_admitted_attempt_8635fa71bfde44c1b71496331c8e322d` **before** any
  negative stages. Human retention/custody release/current artifact disclosure were
  separate decisions; quarantine remains and scientific validity is not assessed.
  Typed output proof201 and exact replay validated real Job/cluster/namespace UID.
- All16 configured installed stages passed: MCP proposal/request/human approval/
  admission/dispatch, reference-only A2A critic, actual output, expansion403,
  client reply-discard/dispatch replay, real control restart-generation change,
  separate short-grant expiry403 and separate status-only revocation409, export.
  Restart occurred after committed success, not during active Job execution.
- Both installed peers completed SDK result/replay. Integrated critic task
  `4f18495b-8007-470c-b5a3-87736300bc30` and reverse task
  `6f3266be-639c-40e1-bb35-8780c34857cd` each drained four metadata records through
  the installed CLI, all acknowledged/pending zero. All eight appear in notebook
  readback with exact receiving actor/credential, causal peer refs and null model.
  This is reference-only protocol/provenance, **not model reasoning**.
- MCP observation/review and a new post-restart session passed. A separate bounded
  intervention grant exercised MCP admit/status/pre-submission cancel without a
  lease/package/Job. After actual expiry, no-operation accounting recovery returned
  200/`reconciled`; provider termination was not inferred.
- Final installed MCP notebook readback exhausted one page at sequence112/event
  cursor11983, `complete_recorded_project_metadata_snapshot`, `has_more=false`,
  SHA256 `34e016e8938d99c23c3dd632e36af684e2703537db43a15ad73ff129e9a73795`.
  It joins output manifest/artifact/hash/36bytes/attempt/operation/validated Job UID,
  on-time/succeeded control state, human custody and all eight peer records.
  Historical uninstrumented/off-platform coverage remains explicitly unknown.

Sanitized receipt record ID: `REMOTE-PLATFORM-FRESH-NORMAL-2026-10-04`.
At this historical checkpoint the full-source Ruff/four-shard gate was **not
collected**: the same journey child paused at coding-agent admission with
`misalignment_policy_violation` / `Potentially unintended activity`; no gate
results were retrieved. A later candidate's exact-source gate is summarized in the
[release evidence note](release-evidence-intent9.md); it does not turn this older
receipt into a current-candidate run. Root must recover
admission in that same child and obtain its already-known gate handles/results.
Do not start a replacement child, switch models or rerun/claim the gate from this
runbook. The historical 3454-pass/1-skip baseline is not current proof. Remaining
external acceptance is dedicated reasoning OAuth admission, independent host/
laptop-off, Linux installation and native picker; no inference or external host
was used. No commits/pushes or broad cleanup occurred.

## Current acceptance and blockers

Harness safety tests are isolated synthetic tests, not live acceptance. The
integrating parent must finish the protocol, authority, output, export and runtime
lanes before creating the final concrete manifest. No service was started for
this harness implementation. No independent-host or laptop-off proof is claimed.
The receipt always leaves `reasoning_verified`, `independent_host_verified`,
`laptop_off_verified` and `integrated_acceptance_verified` false. `status=passed`
means only the configured journey's returned assertions passed; it does not close
the root platform acceptance. Evidence is self-reported.

### Revision-3 source checkpoint (not installed or live acceptance)

The schema-52 local receipt above remains the latest installed state. Later source
schema-53/54 work reports the generated-public-output registry and typed
capture/provenance join, MCP binding/catalog source checks, generated-A2A transport,
and a bounded ordinary-Docker worker HTTP/service/CLI adapter. Their focused tests
use isolated fixtures, temporary HTTP/SQLite and synthetic model/Docker effects;
they do not establish a schema-54 installed artifact, actual V2 harness capture,
generated model reasoning, approved guard-image build, or garage-host execution.
See the [generated-output contract](generated-public-output-interface.md),
[public-capture interface](public-capture-interface.md),
[ordinary-Docker HTTP binding](ordinary-docker-http-binding.md), and the
[current acceptance matrix](remote-agent-implementation-journal.md).

The current-source full gate was uncollected at that revision after the recorded same-child
admission denial. No SSH/OAuth reference values were read, and garage workload,
capacity, ports and runtime-auth references remain uninspected—not known absent.
The generated planner/critic consumer, V2 public capture/drain callsite, actual
human review/experiment loop, active-run restart and running-job cancellation are
still implementation/acceptance gaps. A2A reference or synthetic model bytes are
not generated critique. Hybrid garage-control/Mac-Kubernetes staging is not final
independent-host or physical laptop-off proof.

### Intent-7/8 source status — schema 55; no new installed receipt

Later source work completed the v7 runtime-binding Store/API/native-owner integration
and a 79-input native source closure. Separate backend, migration, API and composite
native receipts use synthetic upstream identities, guard/quota resources, installation
linkage and native primitives. No current release candidate, schema-55 installed
image, full-source gate, live provider dispatch or generated planner/critic journey
is evidenced. The schema-52 installed receipt above remains the latest installed
platform evidence and must not be relabelled. See the
[current journal matrix](remote-agent-implementation-journal.md)
and [release checklist](release-checklist.md).

Specific prerequisites:

1. Writers quiesced and installed build from the converged source. Install the
   `mcp` extra in the interpreter used for the harness. Do not silently fall back
   to a source-only stub, HTTP health endpoint or in-process MCP substitute.
2. Existing ordinary control and Kubernetes dispatch/collection services; an
   exact approved variant/design/task/execution grant and plan/target digest pins.
   No raw kube credentials or owner-token proxy are passed to an agent.
3. Separate private credential references: scoped planner enrollment,
   separate human issuer, and enrolled/authenticated critic caller. For owned local
   acceptance these are ordinary automated setup work, not a user prerequisite:
   `--setup-agents` below mints planner/critic/peer credentials using the existing
   separately authenticated human API. Use 0600
   regular owner-owned files, not symlinks. Tokens never appear in arguments,
   subprocess diagnostics, receipts or manifests. The MCP subprocess receives
   only its own runtime references, PATH and optional public CA reference.
4. A genuine independent `tessera_control.a2a_peer` process whose authenticated
   Agent Card is at `/.well-known/agent-card.json`, with A2A 1.0 / JSONRPC endpoint
   `/`. Install `a2a-sdk[http-server]==1.2.1` alongside the harness; the current
   development environment does not have this optional SDK. Supply exact
   `critic_agent_id`; discovery must match its name, protocol and exact configured
   URL before any credential-bearing task call. TLS is required off loopback;
   redirects and environment proxies are disabled. The implemented peer only
   exchanges bounded references under pre-existing human-rooted, status-only
   delegated authority; it does not perform critic reasoning. Prepare its exact
   zero-budget/depth-zero child grant before this journey. Later `human_issue`
   grants execution consumption separately. See [actual peer contract](a2a-peer.md).
5. Peer paid inference disabled. This harness is limited to protocol/workflow
   acceptance with an already configured no-paid-inference critic. Actual model
   reasoning is a **separate prerequisite/evidence boundary**: the current user
   request supplies no new provider credential/spend scope. A remotely enabled
   paid inference backend must not be exercised by setting a false attestation.
6. Actual retained output retrieval route and exact content digest. A completed
   task or provider ID is not retained bytes. Assert retained artifact identity,
   attempt/operation binding and digest, not merely a success string.
7. Read-only real process-start observation, not a cached response revision.
   The implemented control API's `/health/live` is only liveness and
   `/health/ready` is schema readiness; neither exposes process generation.
   Use the harness's `via="kubernetes"` restart stages for an explicitly pinned
   Deployment. The probe checks namespace/Deployment UIDs, ReplicaSet ownership,
   Pod controller ancestry and ready container start times/restart counters.
   An authorized operator restarts the owned service after `restart_baseline`;
   the harness only observes changed container generation. It cannot restart it.
8. Expiring and revocable grants with exact approved resource pins. Prepare a
   short-expiry grant separately from the grant used for execution/revocation;
   a `$ref` can select each actual record. All stages must fit the bounded clock.
9. Joined authorized notebook/export includes actual authority, MCP, A2A,
   attempt/operation/output provenance with completeness watermarks. Missing
   instrumentation is an implementation gap, not permission to synthesize rows.

Parent integration references: [authority](agent-authority-interface.md),
[provenance](agent-provenance-interface.md),
[MCP wire contract](mcp-full-workflow.md),
[real reasoning runtime](remote-reasoning-runtime.md),
[ordinary collection](ordinary-worker-collection.md), and the durable
[implementation journal](remote-agent-implementation-journal.md).

## Private manifest and invocation

Prepare a 0600 JSON manifest outside the repository. Do not include bearer values.
It contains `environment` (`local_owned_docker` or `authorized_remote`),
`ownership_inspected=true`, `writers_quiesced=true`,
`peer_paid_inference_disabled=true`, `max_seconds` (1–600), verified
`control_url` and `critic_url` origins, `project_id`, `agent_destination`,
`agent_token_file`, `human_token_file`, `critic_token_file`, `critic_agent_id`, optional `ca_file`,
and `steps` in the exact order below. These booleans are operator declarations,
not automatically verified ownership/financial guarantees. Before declaring
them, inspect project/container/port ownership and ensure concurrent writers
have finished; do not reuse a different project's service.

```sh
.venv/bin/python dev/remote_agent_platform_live.py --manifest /private/path/journey.json
.venv/bin/python dev/remote_agent_platform_live.py --manifest /private/path/journey.json --run
```

Without `--run`, only manifest structure is checked: no credentials are opened,
no services contacted and no live success claimed. Execution has a single
overall deadline, bounded response sizes and no automatic mutation retry. No
model fallback, purchased credits, API-key coding inference or provider launch.

Each step contains `stage`, `via`, and nonempty `assertions`. MCP steps contain
`tool` and `arguments` matching the installed typed tool schema. Human steps
contain `method`, absolute local `path`, optional `body`, and `idempotency_key`
for mutations. A2A discovery uses the official `A2ACardResolver`; the task step
contains `request` matching actual protobuf `SendMessageRequest`: `message`
with stable `messageId`, `role="ROLE_USER"`, optional `taskId`, and
`parts=[{"data": EXACT_CLOSED_REFERENCE_ENVELOPE}]`. The SDK generates the actual
A2A 1.0 JSON-RPC call, not a guessed legacy method name. The envelope uses the
peer's implemented `operation`, `reference`, `authority` fields and immutable
binding pins. Returned bodies use protobuf JSON field names. Neither an A2A
message nor Agent Card grants execution authority. A caller-supplied result
reference has `verified=false` and must not be labeled agent-generated reasoning.

Human paths can use named placeholders (for example
`/agent-authority-grants/{grant_id}/revoke`) with `path_parameters` such as
`{"grant_id":{"$ref":"human_issue.body.id"}}`. Resolved path parameters must
be single alphanumeric/underscore/hyphen segments; URLs and traversal are rejected.

For both restart stages set `via="kubernetes"` and supply a top-level
`restart_probe` with explicit `kubeconfig`, `context`, `namespace`,
`namespace_uid`, `deployment`, `deployment_uid`. It makes only bounded `kubectl
get` calls against that exact context/resource scope. Returned
`body.generation` hashes real container starts, not Deployment resourceVersion;
`restart_observed` must compare it with `restart_baseline.body.generation` using
`ne`. Do not use ambient kube context or infer ownership from matching labels.
An explicitly implemented HTTP process-generation route remains supported by
the old human GET mode, but no such control route exists in the inspected code.

MCP proposal/request/consume arguments are validated against actual
`ResearchProposal`, `AuthorityBounds` and `Consume` schemas in
`tessera_mcp.mcp_workflow` after response references resolve. Admission/dispatch
stage commands must match `attempt:admit` / `attempt:dispatch`. A distribution
lacking these modules is not compatible even if its version string is 0.1.0.
The implemented `project_external_agent_notebook` projection returns
`version="agent-notebook-metadata-v1"`, `provenance`, `control_events`,
`watermark.sequence`, `watermark.event_cursor`, `next.after_sequence`,
`next.after_event_cursor`, `has_more` and scoped completeness fields. A complete
export must not accept `partial_page` or ignore either stream. Final HTTP export
wrapping/pagination still needs backend convergence; adapter `offset/limit` is
not proof that the projection's dual cursors were correctly translated.

Assertions use `path` (dot-separated dictionary/list components), `op` (`eq`,
`ne`, `contains`, `nonempty`) and `value`. Responses are wrapped as
`{"http_status": 200, "body": ...}` or MCP
`{"is_error": false, "body": ...}`. References such as
`{"$ref":"human_issue.body.id"}` resolve actual earlier returned fields;
they also work recursively in arguments and human bodies. No arbitrary Python,
shell, executable hooks or model-generated route forwarding is supported.
Never copy untrusted peer text into human authorization bounds.

| Stage | Transport / required meaning |
| --- | --- |
| `planner_proposal` | MCP `tessera_propose_research`; actual durable proposal identity |
| `critic_discovery` | A2A card; exact supported protocol and identity assertions |
| `critic_task` | independent A2A 1.0 SDK `SendMessageRequest`; returned durable reference-task identity, not generated reasoning |
| `authority_request` | MCP `tessera_request_authority`; exact narrow immutable binding request |
| `human_issue` | human POST project `agent-authority-grants`; request-linked exact bounds |
| `agent_admit` | MCP `tessera_consume_grant`, `attempt:admit`; actual attempt identity |
| `agent_dispatch` | MCP consume `attempt:dispatch`; actual ordinary-worker operation |
| `retained_output` | human authorized GET; retained output digest/attempt/operation |
| `denied_expansion` | MCP consume out-of-bounds resource/action; exact HTTP 403/409/410 |
| `lost_reply_replay` | exact original dispatch arguments and key; stable returned identity |
| `restart_baseline` | pinned Kubernetes container-start generation (or actual human GET generation route) |
| `restart_observed` | same probe after external restart; `ne` baseline generation required |
| `expiry_fence` | MCP consume expired narrow grant; wait until aware `wait_until`; exact fence status |
| `human_revoke` | human POST grant revoke, expected revision/epoch |
| `revocation_fence` | MCP consume revoked grant; exact fence status |
| `joined_export` | MCP `tessera_export_notebook`; actual joined IDs/digests/watermarks/completeness |

`retained_output` and `restart_observed` may specify `poll_seconds` (0–60);
only these safe-read stages may poll. Expiry waits consume the overall deadline.
The replay stage reuses the original dispatch step regardless of supplied new
arguments. It is a **client-visible reply-discard/replay scenario**, not proof
of a network-dropped reply or uncertain provider submission reconciliation.
It compares the full returned response digest, without logging it. A timeout,
unrecognized MCP error or ambiguous response stops with an unknown receipt and
no new idempotency identity. Minimal recognized MCP HTTP fence codes are parsed
without publishing error text; a generic SDK `isError` is not proof of denial.

## Evidence and remaining integrated work

Retain only the closed JSON receipt printed to stdout; server response bodies
are held in memory and never saved. Attach actual installed build revisions and
owned-service observations separately through the parent's journal. Full prompt,
tool content, secrets and hidden reasoning must not enter that journal.

After convergence: finalize concrete resource pins and response assertions,
run the bounded owned-local-Docker journey, investigate implementation failures
in their owning lane, and append exact observations/blockers. For remote
acceptance, additionally identify an authorized off-host target and credential
handoff, instrument actual supervised reasoning, verify two independent peers,
and demonstrate laptop/desktop shutdown while remote work continues. Local
Docker, an SDK exchange and an authorized export cannot substitute for these
distinct proofs. The harness must remain opt-in and outside default pytest/CI.

## Read-only bench preflight — 2026-10-04 14:44 UTC

Observed against the documented actionable handoff and runtime relocation metadata;
no private header/token/profile content or keychain was read. Historical receipts
were used only for resource/reference metadata, never as fresh execution evidence.

| Boundary | Actual observation / next action |
| --- | --- |
| Client route | Documented `https://localhost:18443`; stopped receipt, expiry `2026-10-04T09:54:58.870749+00:00`, no current listener. Verified-public-CA anonymous `/health/ready` unavailable. Parent must resume its owned route after convergence; this worker did not. |
| Local ownership | Docker context `desktop-linux`; Docker CLI available. Four running Compose database containers belong to `demodirector` / `yrdsale`, not Tessera; do not touch them. Explicit documented Kubernetes context is `docker-desktop`, kubeconfig supplied through `$KUBECONFIG`; no ambient context or server was inferred. |
| Control namespace | `tessera-lab`, UID `84ffd885-ed3e-4cf9-8941-59a5123cb3e1`; UID-fenced probe matched. |
| Control Deployment | `tessera-control`, UID `680792b8-12d4-4688-8043-a725f4a4437f`, observed ready=1. Pod `tessera-control-f848cf7d5-mj58v`, UID `e4258b68-faf1-4a89-8683-a99169768292`, ready, restart count 0. |
| TLS gateway | `tessera-control-tls`, UID `b612159e-c69d-45ae-83ae-687532c0c7ea`, ready=1; Pod matches documented route UID `c95b3430-c2ce-4182-bd25-1d363a57b571`. No listener means the client tunnel—not gateway Pod liveness—is absent. |
| Ordinary worker | `tessera-dispatch-worker`, UID `d5634d0a-a968-4ef4-9f25-76b69cef5967`, unavailable; Pod UID `c9537088-bd50-49bb-b4e5-f5730ac1b515`, ready=false, restart count 36. Historical runbook reports worker credential expiry 11:54 UTC and short network witness lifetime; those are not fresh causes/readiness proof. Parent must inspect/refresh the owned worker's authorized credential/witness after convergence. No worker logs or secret resources were read. |
| Installed artifact | Existing handoff environment reports `tessera-control=0.1.0`, `mcp=1.26.0`, but installed module discovery returns false for `tessera_mcp.mcp_workflow`, `tessera_control.agent_authority`, `tessera_control.agent_provenance`, `tessera_agent.remote_reasoning_runtime`. Rebuild/install converged artifact before SDK acceptance. Existing control/worker image remains `tessera-bench-installed@sha256:8f3f0a25d38ec19912300cda46d9d4645bd539c506717ae332317f9bddcd9972`; source changes are not deployed. |
| Credential references | Public CA and documented human header file exist as owner-only regular files (content not read). Handoff `TESSERA_AGENT_TOKEN_FILE` points to a nonexistent placeholder; destination is instructional placeholder text. No usable enrolled planner/critic credential handoff supplied. Human header existence does not prove human credential validity. |
| Historical resources | Project `project_f080b734cd5849ec998265c7a2f34a4f`, task `task_e455b4713d9e4e15a6095367bac7d6ed`, admitted attempt `experiment_admitted_attempt_703c7b86752640bfbd160cbf8a015755`, operation `84b59fc4048c62c7c06a431307d55c28`, Job UID `e2c69a4b-ed1e-4ec0-9538-951f09f6e683`, retained manifest `artifact_manifest_269bff0d66411238c1770f2bd52d0edf`. Historical summary marks its grant `reconciled`; do not reuse as fresh admission authority. Exact current variant/design/execution-grant/task revisions, plan/target digest, workload namespace/PVC pins and new human-issued agent bounds still require a prepared manifest. No authority was reconstructed from old receipts. |
| Restart capability | New actual probe successfully observed namespace/Deployment/ReplicaSet/Pod ownership and ready container starts; baseline generation SHA-256 `e452cb5e28920eeac4416519a5e8c3b17ec0fad468f26ec02f3545d0fdb8a9a8`. No restart occurred; restart-observed acceptance remains unrun. |
| Reasoning | Named `TESSERA_REASONING_AGENT_TOKEN`, `TESSERA_REASONING_HARNESS_TOKEN`, `TESSERA_REASONING_PROFILE` references absent. Dedicated included-OAuth research harness/approved profile handoff still required; existing coding-agent service is not reused. This blocker does not prohibit repairing/installing the ordinary Kubernetes worker boundary. |
| A2A integration | Newly available `tessera_control.a2a_peer` and `a2a_store` use official A2A 1.0 SDK 1.2.1; orchestrator now uses their SDK/protobuf contract, not legacy `message/send`. Development interpreter lacks optional `a2a` package. Actual peer implements reference exchange only; generated critique/context reasoning and peer-ledger-to-authorized-notebook provenance bridge are still explicit owning-lane integration gaps. |

An initial read-only query guessed `tessera-gateway` and returned NotFound;
the documented actual `tessera-control-tls` name was then used. No resources were
created, updated or deleted. No service/tunnel/Pod was started, stopped or restarted.
No Job, inference, spending, enrollment, grant issue/consume or credential discovery
beyond documented references occurred. The only live harness component exercised
was the read-only generation probe. Targeted safety tests: **26 passed (2.14s)**;
scoped Ruff lint/format passed. Full installed SDK/A2A/ordinary experiment/export
journey and root platform acceptance remain open.

After the actual A2A SDK/protobuf adaptation and card destination/protocol fencing:
targeted harness suite **31 passed (1.99s)**, scoped Ruff lint/format and whitespace
checks passed. This adds synthetic card/contract coverage only; no peer, reasoning
or experiment was started and no passing live check was repeated.

## Automated ordinary local setup (implemented, opt-in)

The existing authorized human owner reference is sufficient to mint local test
identities; do not ask the user to obtain planner/critic tokens. Once writers are
quiesced and the installed control is ready, prepare a private setup manifest:

```json
{
  "environment": "local_owned_docker",
  "writers_quiesced": true,
  "ownership_inspected": true,
  "max_seconds": 120,
  "control_url": "https://localhost:18443",
  "ca_file": "/absolute/documented/public-ca.crt",
  "human_token_file": "/absolute/documented/owner-token",
  "project_id": "EXACT_OWNED_PROJECT_ID",
  "local_setup": {
    "human_approved": true,
    "private_dir": "/absolute/private/new-journey-setup",
    "duration_seconds": 1200,
    "agents": {
      "planner": {
        "destination": "EXACT_APPROVED_DESTINATION",
        "actions": ["context:read", "proposal:create", "proposal:read", "authority:request", "authority:consume", "observation:create", "review:request", "notebook:read", "provenance:append"]
      },
      "critic": {
        "destination": "EXACT_APPROVED_DESTINATION",
        "actions": ["context:read", "authority:consume", "notebook:read", "provenance:append"]
      }
    },
    "peer": {
      "public_url": "https://EXACT_OWNED_PEER_ORIGIN",
      "control_url": "https://tessera-control-tls.tessera-lab.svc.cluster.local",
      "model": "EXACT_APPROVED_ATTESTED_MODEL"
    }
  }
}
```

```sh
"$FINAL_ENV/bin/python" dev/remote_agent_platform_live.py \
  --manifest /absolute/private/setup.json --setup-agents
```

This setup action does not require the final 16-stage scenario. It calls the
actual human-only `POST /credentials` with generated high-entropy tokens,
project, <=30-minute expiry and exact `ExternalAgentScope`. Tokens are durably
written to separate 0600 files **before** the API call; intent/key/request digest
is durably recorded before every mutation. A 0700 setup directory is required.
Only the root human credential is used for enrollment; MCP and delegation use
the minted agent credentials. Agent self-enrollment remains denied. No inference,
provider effect, Job, service launch, enrollment discovery or owner-token proxy.

The private directory contains `planner-token`, `critic-token`, `peer-token`,
non-secret `setup-state.json`, private `peer-identities.json` and optional
`critic-peer.json`. Peer bearer is separate from critic's control token; the
registry binds its hash to the actual planner principal/tenant/project returned
by the control API. Generated peer configuration uses a named environment
reference `TESSERA_JOURNEY_CRITIC_CONTROL_TOKEN`, not a token value. An ordinary
supervisor must securely inject the critic control token from its private file.
No credential or raw API response is printed; stdout is only a closed setup
status. Do not copy private credential files/registry into receipt storage.

Optional `local_setup.worker` contains `target_file`, exact `target_file_sha256`,
`namespace_uid`, `pvc_uid`, and approved installed-image `build_digest`. The
target must be Docker Desktop, supported development dispatch profile, current
real allow/deny witness, exact target/PVC/namespace pins and digest-pinned
`collector_image`. It enrolls a **new** worker credential through `/credentials`
and registers that returned principal through `POST /projects/{project}/workers`;
it does not mount credentials, replace the old worker or start dispatch.

Optional `local_setup.critic_status_bounds` supplies actual already-human-approved
resource bindings, model, purpose, duration, explicit expiry, `budget_cents=0`
and `actions=["attempt:status"]`. Setup issues a human root to the planner with
status/delegate rights and depth 1, then delegates with the planner's own token
to the critic, status-only/depth 0. Both destinations must be the same exact
approved destination; delegated expiry cannot outlive the setup identities.
This creates no execution grant, admission or dispatch permission. The execution
resources must already be approved; never substitute the historical reconciled
grant or fabricate pins. Child ID/revision/epoch are retained in the private
setup journal. The journey can reference them as
`setup_critic_status_grant.id`, `.revision`, `.epoch`, and enrolled identities as
`setup_planner.credential_id` / `setup_critic.credential_id`.

A final `--run` manifest with the same `local_setup` automatically loads the
confirmed setup, selects the generated planner/peer files and critic identity,
and exposes these references to its subsequent actual protocol stages. Start
independently supervised peer services after setup and before that final run.
Repeated **confirmed** setup does not re-enroll; changed project/origin/scopes/
target/config or expired identities block. Pending/unknown replies stop without
automatic retry, token replacement or changed keys. Reconcile against the
ordinary API using the stored exact intent/key before any explicit replay.
Partial setup can leave bounded credentials requiring human revoke; unknown
does not mean that no credential was created.

The newly converged enrollment model adds explicit `provenance:append` (max nine
actions); it is included in the template only for the human-approved collector
workflow. Existing credentials do not acquire it implicitly. Source backend
schema-51 local tests now pass; the old installed cluster image still requires
the planned final-wheel install/migration before that evidence applies live.

## Current worker diagnosis and minimum fenced repair plan

Additional read-only observations (2026-10-04, after the preflight above):

- Worker Pod restart count **39**, exit code 1, BackOff events. Its bounded
  current/previous logs contain the static diagnostic **ControlChannelError**;
  raw logs and error content were not published.
- Ordinary human-authenticated `GET /credentials?limit=200`, via a UID-checked
  already-running control Pod and loopback port 8765, confirms the documented
  worker principal `principal_863671c7542943e4b2137963faa22b0e` has credential
  `credential_8f4cd2dd5b5a47eaba6c490a710e7d96`, expiry
  `2026-10-04T11:54:43.323797Z`, not revoked. A read-only `GET /worker/status`
  with the existing documented worker file returns **401**, response body omitted.
  Secure credentials traveled only through private file references/in-memory
  HTTP/stdin; no secret values or Secret resources were inspected or printed.
- Current kube-system UID, workload namespace UID, outputs PVC UID/Bound state
  and default-deny policy UID match the recorded target. Worker startup failure
  is consistent with the confirmed expired control identity, not a demonstrated
  changed Kubernetes resource. The mounted Secret's token was not read; replacing
  identities still requires reconciliation of any old journal/operations.
- Installed and source `NamespacedKubernetesClient.verify` agree: network witness
  freshness is enforced for **admission**, not startup-only recovery. Old witness
  expiry therefore remains a new-dispatch blocker but is not evidence of this
  immediate startup failure. New platform modules are missing from the old wheel,
  a separate convergence/install issue.
- `Store.renew_worker_credential` explicitly refuses expired/revoked credentials.
  Do **not** attempt to revive this identity or weaken its lifetime guard.

After writers quiesce, the parent can perform this exact bounded sequence under
the existing local-Docker authorization (none was executed by this worker):

1. Record converged source/wheel/image revisions and full applicable gates. Build
   one final wheel, install it with `mcp,a2a` extras into a **new** private venv,
   run installed CLI/module/tool/readiness checks, and build/load one local pinned
   control/worker/collector image containing that wheel. No registry push,
   automatic fallback or external deployment. Existing Dockerfile `lab` performs
   source `uv sync --no-dev` without protocol extras; that alone is not proof of
   installed final-wheel/peer packaging. Use the trusted-wheel image workflow.

   ```sh
   uv build --wheel --out-dir "$PRIVATE/dist"
   python3 -m venv "$FINAL_ENV"
   "$FINAL_ENV/bin/python" -m pip install "$FINAL_WHEEL[mcp,a2a]"
   "$FINAL_ENV/bin/python" -m pip check
   "$FINAL_ENV/bin/tessera" connection-tunnel --help
   "$FINAL_ENV/bin/tessera-mcp" --help
   "$FINAL_ENV/bin/tessera-a2a-peer" --help
   "$FINAL_ENV/bin/tessera-reasoning" --help
   ```

2. Re-read exact kube-system/workload/control namespace UIDs, Deployment UIDs and
   resourceVersions, PVC/policy UIDs/spec digests and Docker context. Reject any
   mismatch. Check port 18443 and proposed peer ports with `lsof`; no killing an
   unrelated listener or changing another project's Compose resources. Inspect
   ordinary queued/active/unknown operations through the authenticated operator
   API before replacing a worker. Human-reconcile unresolved old-worker records;
   never give a new identity authority to replay the expired worker's effects.

3. Update only the owned control Deployment to the approved new digest using a
   JSON patch with **test** preconditions for `/metadata/uid` and current
   `/metadata/resourceVersion`. Preserve `/state/control` and old state; perform
   the documented additive migration/backup gate rather than reset it. Wait at
   most 120 seconds for ready=1 and ordinary schema readiness. Update the TLS
   gateway only if the converged contract actually requires it; preserve its UID,
   existing public CA and service identity. At most one owned control rollout.

   ```sh
    kubectl --kubeconfig "$KUBECONFIG" --context docker-desktop \
     -n tessera-lab patch deployment tessera-control --type=json \
     --patch-file "$PRIVATE/control-uid-rv-tested-image-patch.json"
    kubectl --kubeconfig "$KUBECONFIG" --context docker-desktop \
     -n tessera-lab rollout status deployment/tessera-control --timeout=120s
   ```

   The private patch must test UID `680792b8-12d4-4688-8043-a725f4a4437f`
   and the **freshly observed** resourceVersion, then replace only the approved
   container image field. Do not guess the resourceVersion from historical docs.

4. Resume the supported installed foreground client route, bounded 1200 seconds,
   only after proving port 18443 is unowned and the existing receipt stopped:

   ```sh
   "$FINAL_ENV/bin/tessera" connection-tunnel \
      --kubeconfig "$KUBECONFIG" --context docker-desktop \
     --namespace tessera-lab --namespace-uid 84ffd885-ed3e-4cf9-8941-59a5123cb3e1 \
     --gateway-deployment tessera-control-tls \
     --gateway-deployment-uid b612159e-c69d-45ae-83ae-687532c0c7ea \
     --public-ca-file "$PWD/.tessera/connections/bench/public-ca.crt" \
     --state-dir "$PWD/.tessera/connections/bench/tunnel" --max-seconds 1200
   ```

   The activity must remain owned/foreground or use the ordinary operator
   supervisor; do not replace it with an undocumented temporary launcher.
   Verify TLS `/health/ready`; a live gateway Pod is not client-route readiness.

5. Refresh the **real** allow/deny network witness against exact workload UID
   `1869980f-998a-4ef9-a7ea-d500326150b5`, policy UID
   `2fa7a756-554f-4b60-b372-5f22edcea5fd`, PVC UID
   `3957c66e-7cd4-4ef9-b6fd-2916e52e59d3`, and actual namespace ownership nonce:

   ```sh
   "$FINAL_ENV/bin/python" scripts/witness_local_workload_network.py --run --causal \
      --kubeconfig "$KUBECONFIG" \
     --workload-uid 1869980f-998a-4ef9-a7ea-d500326150b5 \
     --nonce "$EXACT_EXISTING_OWNERSHIP_NONCE" --image "$APPROVED_PINNED_PROBE_IMAGE"
   ```

   Bound effects to its three attributable probe Pods and causal temporary
   policies; cleanup only their recorded UIDs. Do not edit witness timestamps,
   change default deny, increase quotas blindly, or substitute localhost curl
   for in-cluster network evidence. Write a **new** approved target file with
   actual witness data and approved new collector image; preserve old receipts.

6. Run automated setup against the now-ready ordinary API, enabling optional new
   worker registration with that exact fresh target/image. Use the existing
   human owner token **reference** recorded in `client-runtime-references.json`.
   No user-supplied planner/critic token is needed. Copy only new generated
   worker/target references into a new narrowly named Secret using private file
   inputs; never print/base64-copy token values. Pin Secret/namespace/resource
   ownership and keep generated manifests private. Patch only the owned worker
   Deployment with UID/resourceVersion tests, new image/credential mount and a
   **new** durable workspace such as `/worker/platform-journey-RUN_ID`. Preserve
   `/worker` history and old credential identity; do not delete its PVC or journal.
   Wait at most 120 seconds for one ready worker; at most one owned worker rollout.

   ```sh
    kubectl --kubeconfig "$KUBECONFIG" --context docker-desktop \
     -n tessera-lab patch deployment tessera-dispatch-worker --type=json \
     --patch-file "$PRIVATE/worker-uid-rv-tested-new-identity-patch.json"
    kubectl --kubeconfig "$KUBECONFIG" --context docker-desktop \
     -n tessera-lab rollout status deployment/tessera-dispatch-worker --timeout=120s
   ```

   Test existing Deployment UID `d5634d0a-a968-4ef4-9f25-76b69cef5967` and
   fresh resourceVersion before changing the specific image/Secret/workspace
   references. A conflict requires read/reconcile, not force replacement.

7. Human-read current project/task/design/variant/plan/target revisions and issue
   a new bounded execution grant for the new registered worker through ordinary
   approval APIs. Do not reuse the reconciled historical execution grant. Pin one
   toy CPU workload, <=60 seconds, zero provider spend, <=1 MiB retained output,
   one admitted attempt/dispatch, exact target/image/package digests and no wider
   actions. Prepare the status-only critic child and separate execution-consume
   grant with these actual approved bindings; setup can automate the status root/
   child when supplied these bounds. No extra authority is inferred from tests.

8. Start one ordinary supervised reference peer with generated private config
   and its own scoped control credential; selected SDK endpoint/Agent Card must
   match the setup identity. A separate independent peer belongs to the A2A lane.
   Existing peer verifier needs an explicit verified **public CA reference** for
   the bench's self-signed TLS control gateway; absent trust support is an owning-
   lane implementation gap, not permission to disable TLS. Connect the actual
   reasoning/provenance bridge only once those implementations converge. Do not
   reuse the general coding-agent service or start inference without the dedicated
   approved included-OAuth process/profile/password references.

9. Run the <=600-second final installed journey, observe one bounded owned
   experiment and automatic retained bytes, fenced expansion/replay/restart/
   expiry/revocation and joined export. Preserve unknown outcomes; no new dispatch
   key or replacement Job on a lost reply. Stop/revoke only setup-owned identities
   and processes when needed, using recorded owner/UID/key evidence. Root retains
   full journey, scientific-validity and off-host/laptop-off acceptance.

Latest focused validation after setup implementation: **43 tests passed (5.55s)**,
including actual isolated FastAPI/control-store enrollment, distinct identities,
agent self-enrollment denial, private/no-token journal, unknown/no-retry,
immutable setup, optional worker registration and status-only agent delegation.
Synthetic HTTP is used only in isolated worker/delegation failure tests; it does
not satisfy installed worker, live reasoning or integrated journey acceptance.
