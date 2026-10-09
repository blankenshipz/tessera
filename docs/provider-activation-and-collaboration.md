# Provider activation and organization collaboration gates

This is a **future-capability contract**, not a provider integration or an
organization feature announcement. Tessera is currently a single-owner local lab:
the bootstrap owner and existing scoped human/agent/worker credentials retain their
current behavior. No organization member, invitation, shared writer, provider
credential, live provider operation, or billing settlement is implemented by this
document or its default tests.

## Provider activation claim matrix

| Claim | Required accepted evidence for the exact selected provider/profile | Current default |
|---|---|---|
| Provider-enabled | Pinned adapter/model and target identity; an opt-in live fixture record; credential-custody provenance; bounded spend; stable operation/provider IDs; cancellation reconciliation; attributable usage receipt | **Disabled** |
| Credential usable | Explicit owner binding plus an active, unexpired, unrevoked scoped credential delivered through the approved broker | No provider credential path exists |
| Usage or billing known | Provider-attributable receipt reconciled to the operation; unknown remains visible after a missing/uncertain response | **Unknown**; no settlement claim |
| Cancellation complete | Attributed provider observation confirms termination; an abort acknowledgement or lost reply is insufficient | Unimplemented; exposure remains unknown |
| API-key fallback | Never permitted. Quota/account failure must not select an API key, another account, pooled personal account, or another billing mode | **Disabled** |

The provider-disabled status is not a transient error and must not be relabeled as
provider-enabled because configuration, a synthetic fake, a harness session, or a
claimed fixture result is present. The default contract exposes `disabled`, with
usage and billing `unknown`; it makes no provider or live calls.

### Credential lifecycle fence

| Lifecycle | May a future provider adapter use it? | Required behavior |
|---|---:|---|
| Absent | No | Fail closed; do not discover ambient credentials. |
| Active and owner-bound | Only after all activation gates | Deliver an opaque, scoped reference through the approved broker. |
| Expired | No | Reject renewal/use and reconcile any already-running operation. |
| Revoked | No | Reject use; request bounded cancellation/reconciliation where applicable. |
| Owner/tenant mismatch | No | Reject without disclosure or cross-tenant lookup. |

Raw credential values must not enter the control database, API response, event,
receipt, test output, or diagnostics. Control credentials cannot be reused as model
or compute credentials.

## Organization collaboration claim matrix

| Claim | Required accepted evidence | Current default |
|---|---|---|
| Shared organization | Authenticated membership, invitation/revocation, tenant-scoped rows/artifacts/events/secrets, audit retention, concurrent-writer conflict handling, and held-recovery fencing fixture | Not implemented; single owner only |
| Researcher action | Can draft/propose bounded research work; cannot approve execution, review evidence, or operate infrastructure solely by this role | Future role contract only |
| Approver action | Can approve eligible execution scope; cannot draft as researcher, accept evidence review, or operate infrastructure solely by this role | Future role contract only |
| Reviewer action | Can review evidence/claims; cannot approve execution or operate infrastructure solely by this role | Future role contract only |
| Operator action | Can administer approved infrastructure boundary; cannot use human research approval or review authority solely by this role | Future role contract only |

Role names are not claims of enrollment. A future organization implementation must
enforce actions at the data-access and transactional command boundary, preserve
actor/delegated-actor audit records, and reject stale/concurrent writes using the
resource revision. Membership removal must fence renewal, streams, caches, pending
commands, and new authority; it must not silently report existing provider work as
terminated.

## Required opt-in evidence before promotion

Provider promotion requires one separately authorized, bounded live-provider
fixture using existing runtime credentials, outside pytest/CI. Organization
promotion requires a separately authorized non-provider two-user fixture covering
invitation, role separation, conflict, audit, revocation, and recovery fencing.
Neither fixture is present or run here. Synthetic/default tests only lock down the
disabled/single-owner policy and must never be cited as live compatibility evidence.
