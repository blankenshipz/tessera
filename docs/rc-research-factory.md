# RC factory phase diagnostics — historical intent revision 5 slice

At that intent-5 source checkpoint, candidate composition only—not installed/live
research acceptance—was being recorded.
This page records the intent-5 diagnostic slice; it is not the current admission
contract. Intent 6 reviewed and approved the versioned semantics in [Decision 0029](decisions/0029-evidence-backed-runtime-admission.md)
and [runtime admission v2](runtime-admission-v2.md). Strict v1 remains the default;
v2 is explicit opt-in and does not infer missing local checks or provider facts.
`ProductionResearchFactory.construct()` and `attested_observer()` were unchanged in
that diagnostic slice; later v6/v7 integration is documented separately below and
in the current runtime-admission handoffs.
The optional phase port is neither the consumer's `observation_reader` nor an
admission override. There is no automatic session/research start.

## Explicit installed port

```python
from tessera_agent.research_factory import PhaseDiagnosticPorts, ProductionResearchFactory

factory = ProductionResearchFactory(
    phase_ports=PhaseDiagnosticPorts(
        observer_factory=approved_live_scope_observer_factory,
        owner_publish=approved_sanitized_owner_publisher,
        owner_trust=approved_owner_publisher_trust,
    ),
)
diagnostic = factory.construct_phase_diagnostics(private_state_dir, role="planner")
try:
    point = diagnostic.read()  # Live SDK read through supplied callback; does not consume.
    evidence = diagnostic.consume()  # One-shot SDK consumption + separate owner receipt join.
    saved = diagnostic.read_record()  # Durable diagnostic evidence, never fresh admission proof.
finally:
    diagnostic.close()
```

The embedding must independently provide a `SupervisedPhaseObserver` bound to the
SDK's current authority scope, explicit expectation/context, and approved owner
publisher/trust. Config dictionaries, JSON booleans and missing publisher/observer
callbacks cannot create these ports. The default factory fails with
`approved_phase_diagnostic_ports_missing` **before creating state**. No token/auth
file, endpoint, model config or private guard is read by this diagnostic constructor.
Caller owns lifetime/closure of its SDK/channel adapters; diagnostics closes its DB.

## Durable non-admitting behavior

State is isolated under `phase-diagnostics/{planner|critic}/phase-diagnostics.sqlite3`
in the explicitly supplied private directory. A `(role,nonce)` pending claim commits
**before** uncertain cross-runtime consumption. Successful joined, typed, sanitized
projection is stored with a corruption-detection digest. Failed/uncertain consumption
records `uncertain`, no unsafe exception/body; restart cannot replay either case.
Pending after a crash is also non-replayable. No reconciliation/automatic retry or
authorization is invented from stored records. The local digest is integrity
checking, not a signature or independently proven runtime receipt.

`read()` does not consume or write a run/session journal. `consume()` does not call
the main factory, attested observer, research consumer, model provider, dispatcher
or notebook API. Both return explicit `admission_allowed=false` and
`production_verified=false`; stored reports do not become fresh on restart. A live
channel/owner producer still must enforce authority, phase/order and actual check
execution. Serialization/callbacks alone do not prove this.

The join preserves measured local guard claims, scoped operator financial
attestation, and unknown provider entitlement/billing as distinct evidence classes.
It does not fill missing facts with success or interpret provider history as an
included-route guarantee. The reviewed v2 semantic correction is approved by
Decision 0029; that decision is not evidence that a genuine production owner,
principal, private guard/quota resource, financial resolver, installed bridge, or
live admission has been supplied. Those remain fail-closed prerequisites.

## Candidate check evidence

Command: `pytest -q tests/test_rc_research_factory.py tests/test_research_factory.py
--basetemp <owned-test-temp> --tb=short`.

**10 passed, 3 existing dependency warnings, 7.15 seconds, exit 0**. Eight new
diagnostic cases plus two existing constructor/profile regressions. The new tests
reuse the actual isolated SDK preparation/read/consume fixture with synthetic
authority/upstream resources and separate synthetic owner callbacks. Actual factory
composition, private temp SQLite persistence, restart, uncertain failure, config
boolean rejection, record corruption and no research construction are exercised.
No actual owner guard, credential resolution, installed supervisor or live inference.
No process MCP/A2A harness is duplicated. Prior 21/57/34 gates are not rerun.

Final continuation adds one actual public-guard composition case: **11 passed,
3 existing dependency warnings, 5.48 seconds, exit 0**, using the same command with
`-s`. A bounded temp helper invokes actual isolated public guard/publisher, actual
SDK preparation/read/consume, then actual Python join and factory persistence/
restart. No replacement synthetic owner receipt generator is used for this case.
The Python callbacks carry the actual serialized SDK/publisher outputs; they do not
prove a genuine installed live capability bridge. Credential/quota/authority/AI inputs
are synthetic; zero network/dispatch. Public source pins remain unchanged.

Portable prerequisites: set `TESSERA_OBSERVER_SOURCE` to the exact isolated source
root (default repository-relative worktree), and `TESSERA_OBSERVER_BUN` to an existing
Bun executable or provide Bun on PATH. Read-only matching Effect dependencies must
exist at `packages/core/node_modules/effect/dist/index.js`. Missing prerequisites
skip explicitly; no install/private path fallback. These test-only references do
not supply production identity, credentials or approved authority.

## Exact remaining decision/handoff

1. Runtime owner's final public publisher handoff is exercised; reported source artifact:
   `6cb7fc98728449d640b9711389ef036de78735de74b0da8d60ccfe2d304c9e96`.
   Observed actual publisher helper file SHA-256 is
   `b690e593fd0e9039e406ad8626f76b42b014b0f7e7b4e69fc9a07dac8fec5ce4`.
   The 36-target public staged bundle is documented in
   `release/opencode-observer/README.md`; this lane makes no release/source changes.
   Its `GUARD-PUBLISHER.md` and `AUTHORITY-BOUNDARY.md` distinguish actual public
   fixture execution from private-account proof and required explicit enrollment.
2. The dedicated installed embedding binds genuine authenticated session/principal
   ownership and an independently approved owner publisher to the optional port.
   Fixture authority and passed fixture checks cannot stand in for those bindings.
3. Root explicitly approves/rejects separating measured **local route enforcement**
   from operator financial settings and unknown provider entitlement/billing facts
   before changing any complete admission contract.

At that checkpoint these were source handoff and genuine installed-binding
prerequisites, not reasons to fabricate `complete:true`. No active coding harness/config/binary,
financial setting, SDK installation, host deployment or historical 4268 gate changed.
