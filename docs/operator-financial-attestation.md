# Operator financial attestation: existing authority mapping

This is an interface/ownership map for intent revision 6, not a new API, schema,
financial attestation issuer, or summary of the concurrently developed admission
contract. It identifies reusable control authority and the missing trusted ports
for the runtime embedding. No inspected control record proves provider entitlement,
billing guarantees, private guard execution or current provider account settings.

## Existing authenticated paths

All source paths below are relative to `src/tessera_control/`. References describe
implemented local control behavior, not installed remote service acceptance.

| Need | Actual API and source symbols | Authority available / limit |
|---|---|---|
| Authenticate a human issuer | `api.principal` → `Store.authenticate` → `Store._authenticate`, `_credential_principal` in `store.py` | Bearer credential resolves to stored principal role and control audience; caller JSON cannot choose the role. Current credential/principal/ancestor revocation, structural validity, tenant and expiry are checked. Possession of a valid human credential is not independent identity-provider or provider-account verification. |
| Separately enroll human and agent principals | `POST /credentials` → `api.create_app`'s `issue_credential` → `Store.issue_credential` | Only `_is_root_owner` can issue credentials. `human_session` is a human/control credential; agent is agent/control and worker is agent/worker. Non-owner credentials expire within 24 hours. Keep human credentials in the human/control issuer path, never copy them into workers or an agent proxy. |
| Bind external-agent recipient and destination | Same credential issuance with `external_agent`; `ExternalAgentStore._scope`, `_actor`, `_recipient` in `external_agents.py`; `external_agent_api.external_actor` | Immutable `external_agent_scopes` binds credential actions and destination. `X-Agent-Destination` is an untrusted claim compared with stored enrollment, not authorization by itself. `_recipient` requires a human, same-tenant project and live enrolled recipient with `context:read`. This is recipient enrollment, not enrollment of a financial issuer or runtime owner. |
| Issue human-reviewed research-plan approval | `POST /tasks/{task_id}/approve` → `Store.approve_task` | Human role, task revision/status and optional expected plan digest are checked; `task_approvals` records approver and original server approval time. Its event explicitly says `research_plan_only` and `execution_granted:false`. It is not a financial assertion. |
| Issue bounded authority to an agent | `POST /projects/{project_id}/agent-authority-grants` → `AgentAuthorityStore.issue_grant`, `_recipient_bounds`, `_validate_binding`, `_insert_grant` in `agent_authority.py` | Human root grant issuance, project/subject/destination/experiment bindings, bounded expiry and subject lifetime. Server records issuer, revision/epoch and creation time. Agent requests/delegations/consumption cannot create a human financial assertion. |
| Resolve and revoke agent grants | `GET /agent-authority-grants/{grant_id}` → `AgentAuthorityStore.get_grant`; `POST /agent-authority-grants/{grant_id}/revoke` → `revoke_grant`; `require_active_agent_lineage` | Tenant/project/subject lookup, active ancestry, subject and root-human issuer, expiry/revocation and revision/epoch fences. Revocation does not prove external termination. A grant's `effective_status` is not a financial-attestation status. |
| Resolve a human-approved policy reference | `GET /capabilities/{policy_id}` → `CapabilityStore.get`; `CapabilityStore._policy`, `_current_policy` in `capabilities.py` | `capability_policies` stores project/task/plan revision and digest, declaration digest, approval principal, expiry, status and revision. GET is a scoped read, not an automatic current-policy validation. The separate internal `_current_policy` checks approved status, digest, expiry, current task basis and live human approver. This governs research capabilities, not subscription billing settings or an arbitrary financial-policy profile. |
| Approve/revoke that capability policy | `POST /capabilities/{policy_id}/approve` and `/revoke` in `capability_api.py` → `CapabilityStore.approve`, `revoke` | `_human` check, expected revision/declaration basis, <=24-hour approval expiry; replay approval rechecks current policy. `CapabilityDeclaration` in `capability_models.py` is closed and contains environment/image/network/actions/budgets, not financial assertions. Do not add billing keys to this existing API or reinterpret an approved policy as those assertions. |
| Release/read an immutable human-curated context | `POST /projects/{project_id}/agent-contexts`, `GET /agent-contexts/{context_id}` in `external_agent_api.py` → `ExternalAgentStore.create_context`, `get_context`, `_context` | Stored creator principal, creation time, revision 1, exact recipient/project/destination and human disclosure. It can carry approved public metadata as an untrusted body, but has no typed financial issuance, observation/max-age semantics, policy-profile revision resolver or independent financial revocation ledger. Reading JSON does not authenticate its claimed issuer. |
| Preserve command atomicity and replay fences | `Store._mutate`, `_live_principal`, `_project` in `store.py`; `ExternalAgentStore._mutate` | `BEGIN IMMEDIATE` wraps state/event/idempotency writes and rollback. Keys are scoped to principal/tenant/method/path; changed digest conflicts. Replay rechecks live identity and any supplied endpoint-specific `replay_guard`. A new resolver must supply its own semantic currentness check; idempotency alone does not establish fresh financial evidence. |

Credential revocation is real: `POST /credentials/{credential_id}/revoke` →
`Store.revoke_credential` allows root-owner revocation of other credentials or
self-revocation, with audit in the same mutation. Credential ancestry is checked
again through `_credential_principal`. `Store._live_principal` reloads identity
inside the transaction; a Python dictionary claiming `role:human` is not enough.
`Store._project` requires tenant membership and a matching credential project scope.
Generic routes reject scoped external agents; worker-audience credentials cannot
authenticate control routes. Existing authority must not be widened to solve the
mapping problem.

## Missing ports the root embedding must own

The following are required interface semantics, **not implemented endpoint names**.
Opaque references must resolve through trusted, separately enrolled authority; do
not accept a caller-supplied receipt or reference as its own trust root.

| Required input | Reusable evidence | Missing security ownership / required binding |
|---|---|---|
| Opaque owner binding and issuer enrollment | Control credential/principal/project lineage; stored enrollment destination | Independently approved mapping from runtime owner/publisher to control issuer and permitted financial scope. No inspected enrollment API binds a runtime owner to a financial issuer. Root supplies this adapter without auth discovery/copying. |
| Operator principal and issuer provenance | Stored live human principal, server-created grants/approvals/context creator | Separately authenticated human financial issuance operation and immutable reference resolution. Derive operator identity from the issuance transaction, not JSON `issuer`, an agent credential or a receipt signature alone. Require current issuer eligibility on resolution. |
| Project, recipient and destination scope | `_project`, external-agent enrollment and `_scope` comparisons | Bind the financial assertion to that project and approved runtime/recipient destination and owner binding. A same-tenant human or similarly named destination is not enough to establish the missing runtime mapping. |
| Policy reference, revision and required max age | Capability policy IDs/revisions/digests/expiry are actual research-policy facts | Root-owned mapping to the reviewed financial/admission policy profile with an exact immutable revision, permitted claims and maximum observation age. No inspected financial profile resolver exists; capability declarations/admin configuration must not be silently repurposed. |
| Original observed and asserted times | Existing server creation/approval timestamps | Immutable original financial observation time and separately authenticated assertion time, aware timestamps, no future times, and policy-bound maximum age. Server context creation time is not the observation time. Re-export/replay must not refresh either timestamp; date-only facts cannot be turned into invented exact times. |
| Expiry and revocation | Live credential/grant/capability expiry and revocation checks | Financial-assertion lifetime and current revocation/supersession lookup, bounded by issuer/enrollment and policy lifetime. Root must supply this independently; credential validity alone cannot establish that the assertion remains current. |
| Zero purchased balance; auto top-up and automatic reload off | No inspected control API measures these settings | Human-scoped, explicit assertions of all three settings, including their original observation time and approved scope. Classify as `operator_attested_financial_settings`, not local measurement or provider proof; missing/negative/unknown assertions cannot become compliant defaults. |
| Freshness, replay and correlation | Transactional lookups/idempotency, grant revision/epoch and current policy checks | Resolve against independently pinned owner/issuer/operator/project/destination/policy revision and current time; reject stale, revoked, mismatched, replayed-for-new-use or late-mutated evidence. A prior ACK or an opaque digest proves neither currentness nor one-time runtime consumption. |

The minimal next integration is a root-supplied authenticated issuer/reference
adapter with these semantics, reusing existing control identity and scoped lookup
checks. If the necessary financial issuance or enrollment service is unavailable,
return an explicit missing-port result and leave admission fenced. This document
does not introduce a general attestation platform, new table/endpoint or authority
grant. It does not request a new blanket human approval; the reviewed semantics
are already authorized, while the actual secure binding remains implementation work.

## Evidence and trust boundaries

- Local measured OAuth method, route/account match, no-fallback, quota and runtime
  binding evidence must retain its own phase/freshness and producer provenance.
  An operator assertion cannot fill a missing local enforcement check.
- Financial assertions remain operator-attested with explicit observed/asserted
  times and policy-bounded freshness. They are not measurements of provider billing.
- Provider entitlement and billing guarantees remain unknown unless separately
  supported by actual provider evidence; neither this mapping nor an opaque issuer
  reference supplies that evidence.
- Encrypted transport and enrolled server trust are necessary for off-host use
  (`DESIGN.md` §3.4), but TLS proves channel identity, not assertion truth. Likewise,
  a JSON signature may bind a payload to a key only after enrollment/key validation;
  it does not prove financial settings or human authorization by itself.
- Never log account IDs, credentials, headers, private prompts or private guard
  bodies. Keep opaque references and sanitized classes/timestamps only. Never pass
  an owner token to an agent as an issuer shortcut or relabel incomplete observations
  as complete admission evidence.

## Bounded verification and handoff

Existing targeted tests exercise actual temporary HTTP/control-store/SQLite paths
with synthetic local principals. They validate reusable authority only, not a
financial issuer adapter, real human identity provider or installed runtime:

```sh
.venv/bin/pytest -q \
  tests/test_credentials.py::test_agent_and_worker_audience_and_authority_boundaries \
  tests/test_external_agent_authority.py::test_curated_context_exact_recipient_and_expiry \
  tests/test_agent_authority.py::test_budget_expiry_destination_scope_and_human_boundaries \
  --tb=short
```

The first covers agent denial of human approval/credential issuance and worker
audience isolation. The second covers human context release, agent self-release
denial, exact recipient and expiry/replay fences. The third covers authority budget,
destination/action scope, agent revocation denial and expiry fencing. None asserts
that financial issuance or runtime consumption occurred.

Source hashes, complete stdout/stderr and exit status belong in a unique private
receipt directory and the parent checkpoint, not in the shipped document. This
source mapping leaves the prior release candidate/artifact receipts unchanged;
adding this document requires a new package inventory/digest if the parent includes
it. Source readiness is not full release, device, off-host or live acceptance.
