# Non-admitting guard phase evidence (intent revision 4)

`src/tessera_mcp/guard_phase_evidence.py` defines a closed, typed receipt consumer.
It is **not** a guard producer, authentication adapter or admission verifier.
Existing observer admission, `verify_binding`, runtime policies and source pins
are unchanged. No new endpoint, credential resolution or environment access exists.

## Boundary

```python
from tessera_mcp.guard_phase_evidence import parse_guard_phase_evidence

report = parse_guard_phase_evidence(raw_json_bytes, expected=expected, now=aware_now)
# expected is GuardPhaseExpectation, supplied from explicit caller expectations.
# report.admission_fields() ALWAYS raises PhaseEvidenceRejected.
```

Input is bounded to 16 KiB of **bytes**, never a config/options dictionary. Unknown
and duplicate JSON keys, NaN, mismatched nonce/session/proof references, phase,
ordering revision/sequence, model/provider/tool-policy digests or fingerprint are
rejected with a fixed sanitized error. Caller expectations are comparison inputs,
not observed production facts. SHA-256 references are opaque: no account/connection
IDs, credential objects, headers, prompts, reasoning, raw frames or provider context
are accepted. Mapping getters are never traversed.

Protocol `tessera-guard-phase-evidence-v1` recognizes only
`phase="post-model-request-hooks"`, `complete=false`,
`phase_finality="unproven"`, `ordering_enforced=false`. This names a future
post-hook observation point, **not enforced final ordering or final transport**.
No final/complete receipt is accepted by this slice. The safe projection contains
only OpenAI provider identity, public model ID, provider/model/tool-policy digests
and a canonical phase/order/sequence/projection fingerprint. It is not a complete
effective-config export and carries no source config or provider options.

Every returned report has `production_verified=false`, `admission_allowed=false`,
`observation_provenance="unverified_typed_phase_receipt"` and
`origin_authentication="not_observed_by_parser"`. A future authenticated adapter can
establish who supplied a receipt; that is not proof of truth or permission to change
these flags. Source fixtures and self-reported caller claims never establish a
running producer. Nothing here can replace the existing complete admission fence.

## Evidence classes and freshness

| Class | Allowed contents | What it does not prove |
| --- | --- | --- |
| `local_measured_enforcement` | Sanitized passed/failed/unknown outcomes for OpenAI-only, ChatGPT route/method, account match, absent API-key overrides; native quota passed/denied/unavailable outcome | Parser independently ran these checks; final transport; provider-wide guarantees |
| `operator_attested_financial_settings` | Top-up/reload disabled and zero purchased-balance attestations; optional ISO date/timestamp `verified_at` | Live/provider billing telemetry or latest settings; freshness is always `unknown` |
| `provider_unknown` | Entitlement and billing guarantee both `unknown` | Verified included entitlement or irreversible absence of charges |

These classes classify **claims from the source**, not independent validation of
their truth. Operator financial evidence cannot be relabeled local measurement;
unknown provider facts cannot become verified booleans. A negative local outcome
can still be a structurally valid, non-admitting receipt.

Observed and quota-check timestamps require time zones, cannot be future, and
require an unexpired validity interval of at most **30 seconds**. Passed/denied
quota outcomes require a native snapshot timestamp no older than 30 seconds;
snapshot validity cannot extend beyond its 30-second cache horizon. Unavailable
quota may have no snapshot and stays unavailable—not exhausted quota or permission
for fallback. A date-only financial verification is preserved as date-only; no time
or recency is manufactured from it.

## Source basis and actual implementation limit

The prior read-only five-gap map inspected public/private subscription guard source:
effective registry checks, ChatGPT endpoint restrictions, OAuth method/account
matching, clean API-key overrides, operator financial validation and quota checks
exist in code. The model-request, HTTP and WS callbacks run at distinct phases;
registration order allows later mutations. This consumer does not resolve private
credentials or duplicate that logic, and source existence is not proof it ran now.

Historical first checkpoint: core dispatch paths were blocked and no producer was
then implemented. Subsequently the same runtime owner completed the approved sparse
checkout (local evidence ID `OBSERVER-OBSERVABILITY-V4`); this lane did not take
over, alias or modify its source. The handoff declares source artifact SHA-256
`346bbd4629dc2acff0d8606d5d9c25f2fe2f26cf37498904c4d6665610875e64` and
18 executor cases. This is not a full SDK typecheck or actual session dispatch.

The only writes for this slice are this document, the new module and
`tests/test_guard_phase_evidence.py`. No service/host/deployment/inference/auth/env
effects, active harness changes, existing-admission changes or historical 4268 gate
rerun are part of this checkpoint. Initial tests were synthetic JSON contract
checks; the interoperability continuation below additionally executes the actual
isolated Effect executor with artificial inputs. Neither is live harness, native
subscription-guard execution or notebook event-projection acceptance.

## Actual isolated core partial protocol adapter

`parse_core_model_hook_phase(raw_bytes, expected=GuardPhaseExpectation(...),
context=CorePhaseContext(...), now=aware_now)` consumes the exact closed
`opencode-model-hook-phase-v1` receipt. It does **not** convert it into a complete
`tessera-guard-phase-evidence-v1` object or invent successful guard checks.

Caller-supplied context binds an approved HTTPS origin, opaque enrollment/adapter
references and an independently established nonce/session/proof challenge. These
inputs must come from the future trusted channel controller, not receipt/config
options. The parser compares context, expected challenge, actual receipt fields,
order revision/sequence, 30-second time interval and safe projection digests. It
recomputes the sorted-key compact-JSON phase fingerprint and rejects raw snapshots,
unknown fields, added quota, supplied financial success, cross-origin and drift.

The actual core artifact computes provider/model digests and a **structural tool
policy** digest. Policy includes only tool names and schema type/property structure;
descriptions/defaults/examples/enums, headers, bodies and prompts are not hashed.
This is not the full MCP catalog/config digest and is never substituted for those
existing admission pins. Python test expectations compute these structural digests
independently, exercising actual cross-language canonical-JSON parity.

Core `lease.consume()` checks the live registration revision, sequence, current
safe projection and time **before serialization**, and is one-shot. The actual
executor interop test invokes it and demonstrates rejection after `order.changed()`.
Unsigned serialized JSON cannot prove this call occurred or recheck live ordering.
Therefore every adapted report retains `production_verified=false`,
`consumption_verified=false`, `admission_allowed=false`,
`phase_finality=unproven`, and `ordering_enforced=false`. Caller context is not
independently authenticated by the parser; authenticating an origin establishes who
sent a claim, not its truth or current consumption/enrollment enforcement.

The core receipt's only local enforcement claim is its ordered hook-stage snapshot.
Financial settings are `not_supplied` with unknown freshness; provider entitlement
and billing guarantees are unknown. Quota and account/route guard outcomes are
**missing**, not default-success values. Actual separate sanitized facts must be
joined through their own provenance/class contract by a future authorized adapter.
The current strict admission verifier remains untouched.

### Interoperability checkpoint

`pytest -q -s tests/test_guard_phase_evidence.py --basetemp <owned-test-temp> --tb=short`:
**57 passed (1.20s), exit 0**. This includes the existing 37 contract regressions after
module changes and 20 new core/context/tamper cases. A test-temp TypeScript helper
imports the actual isolated `ModelPhaseOrder` and existing read-only Bun/Effect,
executes its ordered callbacks and consumes the receipt. No isolated core file is
modified; caches/helper output stay in test temp. The artificial receipt's SHA-256:
`8258be7009e79c20941f5f9489c524e4f49dd62cf5befc6e04ca09475f18c2a1`.

Remaining first-slice work: expose the phase receipt through a reviewed public SDK/
authenticated channel; test actual SessionModelRequest dispatch rather than only
its executor; bind consumption/enrollment independently; join actual private-owner
guard/quota and operator-attested financial facts without exposing identifiers or
auth values. Final transport, installed build/instance and provider guarantees are
not established. No inference, service start, auth read, dependency install, active
config/binary change or full historical gate rerun occurred.

### SDK channel and separate owner-evidence continuation

The isolated runtime owner subsequently added the public SDK Effect channel and
actual preparation-service tests, with upstream resource/authority mocks and zero
provider dispatch. `guard_phase_join.SupervisedPhaseObserver` now provides the exact
read/consume callback port and a separate sanitized owner-check join. Its focused
21-case actual-SDK/Python composition and remaining genuine authority/publisher
bindings are detailed in [the proposed evidence-contract review](guard-evidence-contract-decision.md).
That review is **not approval** to change current admission. SDK fixture authority,
owner-check fixture success and unknown provider facts remain separately classified;
neither read/consume serialization nor the join establishes live production proof.
