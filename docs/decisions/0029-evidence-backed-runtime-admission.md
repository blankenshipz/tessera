# 0029 — Evidence-backed runtime admission v2

Status: reviewed intent-6 semantic choice, implemented explicit opt-in. Legacy v1
remains strict/default. This is not approval of a genuine installed runtime.

## Decision

Separate measured completed native local OAuth method/opaque owner account match,
approved OpenAI model/ChatGPT endpoint/no-key/no-custom-override/no-paid-fallback,
fresh quota and exact installed runtime bindings from authenticated scoped operator
financial attestations and unknown provider entitlement/billing facts. An explicit
v2 policy approves the unknown provider facts; it does not weaken local enforcement
or pretend that source, signatures or operator assertions measure provider billing.

V2 uses a separate closed protocol/assurance/schema, independently expected enrolled
publisher/issuer/implementation/principal/session/role/project/destination/context/
grant/policy pins. Financial observed/asserted times, explicit required policy max
age, expiry/revocation and zero purchased balance/top-up/reload-off are mandatory.
No invented timestamp or default recency; no blanket `complete=true`, diagnostic
conversion, legacy verified flag, fake entitlement pin or mixed-version fallback.

Session preparation requires independently measured installed mandatory terminal
capability and full build/SDK/schema/config/loaded-skill/full-catalog/tool-mapping
pins. It only reports ready-without-dispatch. Dispatch requires native final live
pre-effect verification after HTTP/WS mutations/lowering, with exact request,
fingerprint, order/sequence and independently issued nonce/ticket. No pre-verifier
`continuePrepared` or post-dispatch join is acceptable. The owner-approved native
port must enforce final adjacency/one-shot use, not a receipt-provided boolean.

The local verifier rereads current authority, installed pins, actual native checks/
quota and authenticated operator attestation. Private SQLite permanently claims
nonce/ticket/request-scope before validation; crashes/failures remain uncertain and
cannot replay after restart. A stored earlier admission cannot replace live checks.
All existing bounded supervisor/grant/context/budget/disclosure fences remain.

## Consequences and limits

The factory requires explicit v2 version/policy plus approved adapters and otherwise
fails closed; v1 remains the default. No genuine enrolled principal/owner publisher
or installed native port is provided by this change. Synthetic local fixtures test
the actual verifier/ledger/factory/consumer but not account/provider/live behavior.
Actual isolated HTTP/WS terminal-to-Python pre-effect interoperability is tested
with synthetic credentials/authority/operator/quota and fake approved primitives.
Native fixture claims are in-memory; Python SQLite restart tests do not establish
production native durable grant/budget atomicity. An installed mandatory bridge,
genuine enrolled authority/current financial resolver and native private-guard/
quota/full-build-pin/primitive-trust adapters remain required and unavailable.
The policy's `max_financial_age_seconds` is explicit operator max-age input without
a default; unknown provider facts do not permit unknown required local checks.
Historical v5 frozen artifacts/receipts retain their original meaning; changed
source requires a new parent-owned inventory/candidate and affected gates.

See [the exact protocol and handoff](../runtime-admission-v2.md) and
[old-to-new field/evidence table](../guard-evidence-contract-decision.md).
