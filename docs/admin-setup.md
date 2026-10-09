# Administrative workspace: offline setup and security runbook

**Implemented: project-scoped configuration metadata, synthetic secret lifecycle,
protected same-host native macOS Keychain enrollment, and administration-only
delegation. Not organization IAM, deployment, or live provider enablement.**
See the [protected enrollment runbook](protected-secret-enrollment.md) for the new
native flow, actual topology, unavailable platforms and production-hardening limits.

See [decision 0025](decisions/0025-administration-workspace.md) for the design-first
workflow and threat model. Existing [Salad proxy](salad-proxy.md),
[capability](agent-capability-policy.md), [remote routing](remote-agent-execution.md)
and [held recovery](recovery.md) boundaries are unchanged.

## Setup guide / actual desktop behavior

1. Connect to an **existing** control plane using the normal native credential-file
   picker. Its bearer value remains in main-process memory. HTTP is supported only
   on loopback; remote connections require HTTPS with normal server validation.
   This slice does not save credentials or connections to frontend storage.
2. Select a project and open **Administration** in the sidebar. Administrative
   state is loaded explicitly, not added to coordinator prompts or conversation.
3. **Setup** records an existing-local, remote-HTTPS or Kubernetes-metadata profile.
   Saving an endpoint does not connect to it, inspect it, switch accounts or deploy.
   The displayed control database observation refers to the currently authenticated
   service, **not** the proposed endpoint. Use the existing Connect flow separately.
4. **Targets** records execution target, artifact store, monitor/watchdog or model
   gateway metadata. Supported provider labels are Kubernetes, local fixture, Salad,
   AI Gateway and artifact store. Choose matching component/provider pairs. Advanced
   fields contain image/CA/cluster/profile digests, namespace, Salad account/project/
   country, opaque secret metadata IDs and CPU/GPU/memory/time/output ceilings.
   Network intent is networkless, restricted-unverified or public-egress-unverified;
   data stays public-released-only and custody quarantine-only. No arbitrary command,
   environment, headers, provider URL, kubeconfig contents or shell bridge exists.
5. Preview the exact metadata command, then explicitly confirm. Configuration
   starts **proposed**. Root human administration may approve another principal's
   proposal, never its own. Revision invalidates approval and verification. The
   local owner can save drafts, but cannot self-approve them. No second principal
   or credential is automatically created to bypass this requirement.
6. **Secrets** offers protected macOS custody or an explicit non-production fake.
   Propose scope as a project human; a different root reviewer approves and performs
   native enrollment. The renderer receives only metadata. Follow the linked native
   runbook; never paste a secret into labels, endpoints, chat prompts or issues.
7. **Delegations** requests access for an existing eligible principal. A non-owner
   can request only its own access. Root review is separate and cannot be by the
   requester or subject. Invitations and workload identity enrollment are explicitly
   unavailable; this UI never manufactures a user, sends email or issues a login.
8. **Readiness** separates configured, reviewed, unknown/stale verification and
   blocked live enablement. “Verify local bindings” is a revisioned database check,
   with **zero external effects**, not a provider health probe. Even a passing
   binding check records `verification: blocked`, `enabled: false`. The enable
   button remains unavailable; the API also denies enable requests.
9. **Audit** displays the first 100 minimal command summaries. The API supports
   cursor pagination. Existing owner-only `/operator/audit` retains denial records
   as well as administrative history. Refresh explicitly after stale, denied or
   uncertain commands; the desktop never retries them automatically.

All previews, drafts and observations are in-memory. Disconnect/project switch
clears them and fences late responses and pending key allocations. An authorization
failure also uses the connection's existing fail-closed disconnect behavior. No
read refresh or reconnect implicitly approves, grants, rotates, revokes or dispatches.
Server authority is rechecked even when a displayed button is stale.

## API and transactional state

All routes require existing **control-audience** authentication. Worker tokens are
not accepted. Organization comes from the live authenticated principal, not JSON.

| Route | Contract |
|---|---|
| `GET /admin/projects/{project_id}/records?offset=0&limit=50` | Typed workspace: visible records, next offset, root-admin indicator, held state, current database observation, unavailable capabilities. Maximum page 100. |
| `POST /admin/projects/{project_id}/records` | `{expected_revision: 0, spec: ...}`; discriminated `configuration`, `secret` or `delegation` schema. |
| `POST /admin/projects/{project_id}/records/{id}/commands` | `{expected_revision, action}`; optional `configuration` only for `revise`, `expires_at` only for `rotate`. |
| `GET /admin/projects/{project_id}/audit?after=0&limit=50` | Root-only minimal command audit; maximum page 100; advance `after` to the last returned cursor. |
| `GET /admin/projects/{project_id}/records/{id}/enrollment` | Root-only native handoff metadata; local serving profile only. No value upload/read route. |

POST requires `Idempotency-Key`. The exact canonical body is hashed. Changed body
with the same key conflicts; identical replay rechecks identity/scope and current
record revision. Stale replay cannot reissue an old grant or conceal secret rotation.
Use `approve`, `reject`, `revise`, `verify`, `revoke`, `expire`, `rotate` and `delete`
only on their valid record types/states. `enable` always fails closed.

State snapshots are typed, normalized metadata events in the existing root-only
operator audit ledger (`admin.state`). They are not raw request-body logs. Minimal
`admin.command` audit and `admin.changed` project events commit with the snapshot
and idempotency result in **one SQLite transaction**. Project/agent labbooks receive
only ID/kind/action/revision, not the configuration or secret metadata snapshot.
No migration is required; the existing schema-49 backup boundary retains these rows.
Do not prune `admin.state`: it is authoritative state, not disposable diagnostic log.

Limits: 200 aggregates per project including tombstones; 100 versions per secret.
Pagination is a current projection, not a cross-request snapshot. Refresh during
concurrent changes; no page is labeled a complete historical export. Large-scale
indexed storage, quotas/rate limiting and retention/compaction require later design.

## Secret lifecycle and custody

| Operation | Implemented guarantee / limitation |
|---|---|
| Create | Root-only `backend: fake` creates a fixture. Project humans can propose `os_keychain` metadata for independent root review. `production_unavailable` is rejected. |
| Import/enroll | Protected same-host native macOS helper only; independent human review and one-use claim. No API/renderer value field. Remote/Windows/Linux unavailable. |
| Purpose | Exact tuples: Salad management / Salad / server proxy; AI Gateway / AI Gateway / gateway; kubeconfig / Kubernetes / operator; artifact store / artifact store / artifact service; monitor / none / monitor. |
| Scope | Metadata belongs to one authenticated organization/project. Configuration can bind only active same-project metadata with the exact purpose. |
| Rotate | Active unexpired record; fake metadata or a new protected version requiring fresh independent review/native entry. Old version is fenced atomically. |
| Revoke / expire | Terminal control metadata fence, with revision and audit; natural expiry is checked on reads/use without a background job. |
| Delete | Tombstone after revocation/expiry; history retained. Native physical purge is separate and may remain cleanup-pending. |
| Health / last-used | Fake is `synthetic_only`; native custody can be `available`, not provider health. Native lookup is boolean only. Last-used projection remains null; trusted resolver use has a separate redacted audit. |
| Read / resolve | No API, IPC, agent or workload secret-read permission. Public `admin_<random>` IDs identify metadata, not resolver handles. |

Configuration verification pins current secret **versions**; rotation, revocation or
expiry makes the projection stale or blocks reverification. Salad management and
AI Gateway references cannot substitute for one another. These public metadata IDs
are not `credential-ref:` locators and cannot be passed directly to the existing
Salad resolver or credential-access-lease broker to gain access. No integration
between fake administration metadata and production proxy custody is claimed.

The fake buffer and trusted resolver use the redacted/non-serializable `SecretBuffer`,
wiped on exit. Python does not guarantee whole-process memory erasure. The native
helper implements private entry, version fencing, durable claims and explicit
cleanup; see its runbook for OS ACL, diagnostics and production-conformance limits.

## Delegation matrix

| Principal / role | Propose new configuration | Read/revise existing config | Approve/revoke access | Secret management |
|---|---|---|---|---|
| Root local owner | Yes | Yes | Review another principal, not self/subject; revoke | Fake metadata; approve/enroll/revoke/purge protected custody |
| Human session / operator label | Yes, same credential project scope | Exact active grant required | No | Propose protected metadata; original proposer may propose rotation, not approve/read |
| Agent | Yes, same project | Exact active grant required | Can request own access; cannot approve or subdelegate | No |
| Worker / proxy / monitor | No control-audience admission as workloads | Unavailable in this administration slice | Requires separate service identity enrollment | No |
| Organization member invitation | Unavailable | Unavailable | Identity-provider/membership implementation required | No |

Supported actions are **only** `configuration.read`, `configuration.propose` (revise
the named configuration proposal) and `readiness.read` (record a blocked local
binding check for an approved configuration). Read and revise/verify are distinct:
include `configuration.read` if the subject should browse a target in the workspace.
The desktop request form selects one action; the API accepts up to three unique
actions. Requests carry exact target ID, provider, `resource: configuration` and
expiry within 24 hours and no later than the subject credential.

Use rechecks credential ancestry/revocation, tenant/project, subject kind, reviewer,
expiry, target lifecycle and provider match. No wildcards, secret-read, execute,
provider operation, approval or further delegation action is accepted. Delegation
revocation does **not** revoke pre-existing research permissions or the login.
Existing research API human/agent behavior is unchanged: this is **not global RBAC**.

The root credential authenticates possession, not an independent person's identity.
This enforces principal-level separation of duties only. Do not claim organization
two-person approval by minting two tokens for one person. Production collaboration
requires human identity enrollment, membership/session revocation and global policy
intersection, including existing API routes, before exposing organizational roles.

## Deployment topology / threat boundaries

```text
sandboxed renderer -- named, sender-fenced IPC --> desktop main (human credential)
       metadata only                              |
                                    authenticated control API
                                                 |
                              transactional metadata / audit database

native same-host helper --> macOS Keychain
future server-side broker --> remote secret backend --> enrolled server proxy
                                                       (not connected here)
execution target <-- independent monitor / custody services (not deployed here)
```

Untrusted renderer/agent inputs are closed schemas, not shell/network instructions.
Server errors for invalid schemas do not echo inputs. Main-process validation also
rejects unexpected fields before network serialization. All UI data is rendered as
text; no secrets in frontend storage or responses. Configuration cannot change
serving trust, TLS validation, remote dispatcher policy, capability grants, network
enforcement or fake/live selection. No ambient credential or current kube context
lookup is added. The pre-existing native namespace workflow remains separate.

Threats not solved: compromised root/control host, independently malicious backend,
real secret material leaked through unrelated user-authored text, credential sharing,
production memory/log custody, large-scale abuse, and runtime workload isolation.
Fields are metadata-only, not a scanner that can prove arbitrary user text harmless.

## Recovery / revocation runbook

1. Revoke the affected administration delegation with its current revision. Revoke
   the subject's credential separately if all login authority must end. Refresh the
   workspace; effective scope becomes false. Neither action proves job termination.
2. Revoke affected secret metadata. Bound verification becomes stale; do not
   rebind to a different purpose to bypass the fence. Tombstone only after revocation
   or expiry, retaining versions and attributable audit.
3. A real-key incident requires independently authorized provider/backend revocation,
   plus native custody purge as appropriate. Provider revocation is not integrated.
   Preserve minimal evidence, stop
   new admissions, reconcile exact jobs with stop-only authority, and quarantine
   outputs. Never paste keys or diagnostics containing keys into the UI/agent.
4. Back up with the existing control backup command. Restore creates a permanently
   held history copy, revokes original credentials and cannot revive administration
   grants. Reads show historical metadata; writes **and idempotent replays** are denied.
   Held denial audit goes to the existing external sink, not into immutable history.
5. Restore is not failover: fence/reconcile the original authority and jobs separately.
   No resume, import, reenrollment or credential materialization is performed here.

## Exact live enablement checklist — no checkbox can enable it

- [ ] Implement/review independent human identity, organization membership and
      project-role enforcement across **all** affected APIs, with no self-approval.
- [ ] Implement authenticated worker/proxy/monitor identities and parent-scoped
      resource/action grants; never reinterpret an agent token as a trusted service.
- [ ] Complete production hardening/conformance of native custody and implement a
      separately authenticated server-side broker for remote deployments. Local
      macOS enrollment alone does not pass this gate.
- [ ] Prove absence of secret material from serialized API responses, logs, traces,
      renderer state, receipts, prompts, backups of metadata and error diagnostics.
- [ ] Bind exact deployed control/target identity, TLS/CA, immutable image, artifact
      destinations, data classifications, monitor and network/resource enforcement.
- [ ] Retain independent runtime isolation, deadline, quota, startup/run/output limit
      and disconnect/compromise witnesses for that exact target/version.
- [ ] Prove termination/no continued writers, lost-response reconciliation, stop-only
      recovery and absence of duplicate effects across process death and restore.
- [ ] Prove external artifact/checkpoint byte custody, integrity, quarantine,
      retention and recovery independently of provider claims.
- [ ] Implement attributed usage/cost/exposure accounting with explicit maximum
      financial exposure, unknown-cost stops and orphan reconciliation.
- [ ] Satisfy the provider-specific [Salad checklist](salad-proxy.md#live-enablement-checklist--all-currently-unresolved)
      or the corresponding Kubernetes/capability conformance contracts.
- [ ] Obtain explicit authorization for a bounded live acceptance run, with target,
      credentials, call/time/retry/spending limits; keep it outside default CI.
- [ ] Review a separate implementation that consumes retained fresh evidence and
      enables exact effects. This release has no such activation implementation.

## Recorded validation (offline, 2026-09-23)

On the pre-existing dirty `main` tree, without dependency installation:

```sh
.venv/bin/pytest -q tests/test_admin_configuration.py \
  tests/test_operator_observability.py tests/test_credential_access_leases.py \
  tests/test_organization_collaboration.py tests/test_provider_activation.py \
  tests/test_recovery_safety.py
# 88 passed; 2 existing dependency deprecation warnings; 106.39 seconds.

.venv/bin/pytest -q tests/test_salad_contract.py tests/test_remote_execution_routing.py \
  tests/test_autonomy.py tests/test_capabilities.py tests/test_kubernetes_lab_deployment.py
# 144 passed; 2 existing dependency deprecation warnings; 190.75 seconds.

node --test desktop/test/*.test.mjs
# 151 passed; 0 failed; includes real-source renderer and preload/main fixtures,
# not a visible GUI or live control/provider deployment.
```

Scoped Ruff lint passed for the new Python modules, API integration and administration
tests; format checks passed for all six touched Python files. Node syntax checks
passed for connection, main, preload, IPC contract and renderer. Tracked diff
whitespace checks passed. The initial lint pass found formatting/import issues; these were repaired
before the passing run. No Python/Node functional test failures occurred in the
recorded runs. Full-repository integration and native graphical acceptance were
not run. No live Salad, paid provider, cloud deployment, dependency install, commit
or push occurred.
