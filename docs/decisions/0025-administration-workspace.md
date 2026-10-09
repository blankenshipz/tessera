# 0025 — Administrative configuration without implicit activation

Status: accepted for the offline administrative slice, 2026-09-23.

Subsequent extension: [decision 0026](0026-protected-secret-enrollment.md) implements
protected same-host native macOS enrollment. The original slice's unavailable
custody statements below describe its historical boundary, not the newer helper.

## Contract and threat model

DESIGN §§3.3–5.2 separate configuration, human authority, secret custody and
execution. A compromised renderer, agent proposal, forged provider status, stale
approval, cross-project ID, restored database or replay must not manufacture
authority. Existing owner-bearer authentication is not an organizational identity
provider: two credentials do not prove two independent people.

Add an explicit project administration workspace. Configuration is a typed proposal;
an independent human principal reviews an exact revision. Approval records desired
metadata, never alters the running deployment, dispatch policies or provider gates.
Verification checks local schema/bindings only and remains blocked for live use.
No client-supplied readiness booleans, arbitrary commands, environment, headers,
secret values, remote inspection URLs or provider calls are admitted.

Use the existing transactional operator audit ledger as the administrative state
event source (`admin.state`), with bounded latest-state queries and project-scoped
access. The project event contains only record ID, kind, action and revision. The
same transaction retains state, event, minimal decision audit and idempotency.
No schema migration or runtime-state edit is needed. Public metadata IDs are not
backend locators; no backend locator or secret value is stored in either ledger.

Secrets have purpose/provider/audience/project binding, immutable version history,
expiry, revocation, tombstone, health and last-used (unknown until actual use).
The fake adapter creates only a fixed synthetic marker inside the server; no input
value exists. Production enrollment/import and OS keychain are explicitly unavailable.
This is safer than putting a secret field into a renderer and promising redaction.
Future enrollment must be an authenticated native/server boundary with external-effect
journaling, backend revocation and no-body logging, not an extension to renderer IPC.

Delegations bind an existing live principal, exact configuration target, provider,
resource and closed action set, with expiry bounded by the subject credential.
Only the root owner may approve; the reviewer cannot be proposer or subject.
Agents can request access but cannot issue privileged delegation. This slice grants
only `configuration.read`, `configuration.propose` and `readiness.read` to human,
operator or agent subjects. Worker/proxy/monitor roles and organization invitations
are shown as unavailable, not mapped onto an agent identity or bearer-token UI.
There is no secret-read, execution, further-delegation or grant-approval action.
Revocation, credential ancestry and expiry are rechecked at use and replay. Existing
research API roles are unchanged; this is not a global RBAC retrofit.

Desktop operations use the existing sender validation and connection-generation
fence. Switching projects clears forms and pending state; stale async responses
are discarded. Uncertain commands are not automatically replayed. Offline state
is not persisted in renderer storage. HTTPS is required off loopback; HTTP remote
metadata is rejected rather than weakening the existing transport contract.

## Workflow / acceptance

1. Inspect dirty-tree contracts, auth/credentials, recovery, proxy and UI (done).
2. Record this decision before implementation (done).
3. Add closed models, transactional state and typed API; wire narrow IPC and six
   panels: Setup, Targets, Secrets, Delegations, Readiness, Audit.
4. Test fake/unavailable custody, non-leakage, revision/replay, scope, self-review,
   tenant boundaries, expiry/revocation and held recovery; test desktop fencing.
5. Run scoped lint/format, offline Python/Node tests and whitespace checks. Record
   exact results in the runbook. Do not infer full integration or GUI acceptance.

Non-goals: live checks, provider effects, cloud deployment, dependency installation,
production identity/secret enrollment, changing any existing execution gate,
commits or pushes. Remaining enablement work is explicit in the admin runbook.

Implementation checkpoint: the offline slice and its scoped verification are complete;
see [recorded checks and remaining blockers](../admin-setup.md#recorded-validation-offline-2026-09-23).
This checkpoint is not production, full-repository integration or visible-GUI acceptance.
