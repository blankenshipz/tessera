# Guard evidence contract — historical v1 review and reviewed opt-in v2 (intent 6)

**Reviewed intent-6 semantic choice is implemented explicitly in v2**; it is not
waiting for generic approval and does not lower v1/default admission. See
[ADR 0029](decisions/0029-evidence-backed-runtime-admission.md) and
[the exact closed protocol/owner handoff](runtime-admission-v2.md).
Historical phase-only checkpoints retain their original non-admitting meaning;
no frozen release candidate or receipt is relabelled as v2 evidence.

## Implemented boundary

Revision 5 adds an **opt-in diagnostic-only factory composition**, described in
[the RC factory runbook](rc-research-factory.md). It persists sanitized non-admitting
evidence and prevents restart replay; it is not a replacement for complete observer
admission or the research consumer. The runtime owner owns the isolated publisher/
authority continuation. Its final actual public publisher is now exercised by the
bounded factory integration below; genuine installed bindings remain separate.

`SupervisedPhaseObserver` in `tessera_mcp.guard_phase_join` accepts independently
provided `read_phase()` and `consume_phase()` byte-returning callbacks wrapping the
owning SDK's `ModelPhaseChannel.read/consume` in its current authority/Effect scope.
There are no default callbacks, principal capabilities, network endpoints or auth
discovery. Constructor inputs use `GuardPhaseExpectation`, `CorePhaseContext`,
and an aware-time clock.

```python
observer = SupervisedPhaseObserver(
    read_phase=authorized_sdk_read_bytes,
    consume_phase=authorized_sdk_consume_bytes,
    expected=independent_phase_expectation,
    context=approved_channel_context,
    clock=aware_clock,
)
phase = observer.read()
joined = observer.consume_and_join(
    owner_publish=authorized_sanitized_owner_check_callback,
    owner_trust=approved_owner_publisher_trust,
)
```

The callbacks are normal trusted embedding interfaces, not JSON
`authorized:true`. Their real principal/enrollment/SDK binding is **not provided**
by this repository slice. SDK authority remains an in-memory principal capability;
no account/token/session-server credentials are passed to this parser. A callback's
serialized output is not independent authentication or execution proof. All reports
retain `production_verified=false`, `admission_allowed=false`; `admission_fields()`
raises. Existing `V2AttestedHarnessObserver`, binding verifier and financial controls
are unchanged. No existing runtime factory is silently rewired.

### Exact separate publisher contract

An independently approved owner callback returns closed bounded JSON bytes for
`tessera-owner-guard-checks-v1`: fixed publisher reference/origin, nonce/session/
proof refs, phase/order revision/sequence, structural projection fingerprint and
model ID, actual `executed_phase` (`model.request`, HTTP, or WS), `executed_at` and
expiry, plus the existing typed local checks, quota, operator financial settings
and unknown provider-fact classes. It must publish outcomes of **completed checks**,
not translate source/config/options presence into successful checks. The receipt's
join phase is post-model-request-hooks; its separate execution phase prevents
confusing a guard callback point with post-hook/final transport measurement.

Owner origin/reference must match independently configured `OwnerPublisherTrust`.
The join matches every phase/challenge/model/order/fingerprint field. Local checks
must be passed, native quota passed with the actual cached snapshot time/expiry
within 30 seconds, operator attestations valid/present. Quota denied/unavailable,
stale snapshots, unsafe fields, account IDs, auth headers, prompts, false entitlement,
and financial evidence relabeled as measured are rejected. Financial freshness is
always unknown; an old date is not manufactured into a fresh verification.

`consume()` is one-shot in the Python adapter. Failed/uncertain consumption cannot
retry or fall back. SDK consumption itself must live-recheck authority/order inside
its owning scope before returning. `notebook_projection()` exposes only typed safe
fields and explicit proof limits; it does not write a notebook event or authorize
disclosure. Durable notebook idempotency/replay remains with its assigned owner.

## Historical v1 rule review (superseded only by explicit opt-in v2)

| Current demanded field/rule | Evidence actually available | Proposed explicit rule | Security consequence |
| --- | --- | --- | --- |
| `evidence.account.mode="measured"` for the entire account group | Owner guard locally checks OAuth method, account match and allowed transport; operator settings and provider guarantees are different classes | Split local enforcement, operator financial attestation, and provider-unknown claims | Avoid presenting a human setting or source presence as provider telemetry; no automatic admission change |
| `account.route="chatgpt-included"` | Guard verifies ChatGPT Codex endpoint, OpenAI-only inventory, absent API-key/custom overrides | Label this **local guarded route policy**, not independently proven provider billing/entitlement | Route-policy enforcement supports no-paid-fallback intent, but is not an external financial guarantee |
| `entitlement_reference`/`entitlement_sha256` as measured provider proof | No safe supported SDK field supplies provider entitlement/billing guarantee; provider-context provenance is historical route metadata | Keep provider entitlement/billing **unknown**; root must decide whether measured local enforcement + explicit operator attestation is the intended acceptance, rather than demand an unavailable provider guarantee | Material contract decision; this implementation does not relax the current verifier |
| `policy_sha256` and exact no-fallback policy as measured | Actual isolated public guard completed checks with synthetic inputs; actual SDK read/consume and public publisher produced bound checks. Not genuine owner account/private guard execution | Require sanitized completed guard checks at exact phase/order, with implementation/publisher trust and native quota times | Source and fixture execution cannot satisfy genuine installed policy execution |
| Financial `verified_at` inferred current | Existing validation checks presence, zero purchased balance and top-up/reload off, not verification age | Operator-attested fields; preserve date/timestamp and report freshness unknown unless a separate approved recency rule exists | Do not manufacture current UI/provider state from an old attestation |
| Final ordered hook chain/config seal | SDK now measures actual ordered preparation stage and rechecks on read/consume; later HTTP/WS/lowering can change transport | Accept stage-local evidence only; keep finality unproven and admission closed pending reviewed boundary | No final transport or complete config certification from partial stage receipt |
| Issuer/principal trusted because receipt/manifest says so | SDK requires live `ModelPhaseAuthority`; tests supply explicit synthetic authority, not production auth | Bind genuine existing authenticated session/principal context via trusted embedding, never Basic-header identity or JSON boolean | Prevent owner-token proxy/agent self-approval/default service owner |
| Full catalog/config/skill/build pins supplied by structural phase hashes | Phase receipt hashes only safe provider/model/tool structural policy | Keep original full binding pins separate and unchanged | Structural policy cannot silently stand in for full harness/catalog/skill/install proof |

V1 remains strict/unchanged. Intent 6 approves the explicit semantic correction
below, not fabrication of provider proof or blanket production admission.

## Reviewed intent-6 old → new rule/source table

Local proof now includes actual native/Python pre-effect interoperability with
explicitly synthetic authority/operator/private-guard/quota resources. It does not
authenticate a genuine owner account, installed publisher or production native
durable grant/budget claim. See the [distinct final gate selections](runtime-admission-v2.md#local-evidence-distinct-selections-not-additive-totals).

| Old demanded field/rule | Implemented opt-in v2 field/rule | Independent source / actual evidence available |
| --- | --- | --- |
| Entire account group measured, legacy included-verification boolean | `local-enforcement-not-provider-billing`; required local checks exactly passed; never sets legacy boolean | Approved native completed-check port; local verifier tests use explicit synthetic native/auth inputs, not genuine owner account |
| Measured entitlement and entitlement hashes | Provider entitlement/billing only unknown under explicit approved v2 policy; separate admission policy ref/digest/revision | Independently approved current policy; no provider telemetry or fake entitlement references |
| Financial date presence without age | Scoped authenticated operator, opaque owner/project/destination/policy revision, original observed/asserted times, required policy max age, expiry/current revocation, balance0/top-up/reload off | Separate current authenticated operator reference resolver; no agent assertion/date-only timestamp invention or provider-measured claim |
| Source policy hash/serialized checks suffice | Exact completed native local checks and current quota <=30s; issuer/publisher/implementation/enrollment pins, current native rereads | Independently expected scope, authenticated bytes PLUS actual native checks; same signing key cannot authorize foreign scope |
| Partial phase becomes complete | Session-preparation only ready-without-dispatch with mandatory terminal capability; dispatch only terminal-http/terminal-ws after mutation/lowering | Installed native pre-effect port; current exact request/fingerprint/order/sequence/nonce/ticket; no continuePrepared before verifier or post-effect join |
| Structural tool hash substitutes for full catalog/skill | Full instance/build/SDK/harness-schema/effective-config/loaded-skill/typed-catalog/tool-mapping/binding pins | Independent installed measurements, exact binding comparison; late changes deny |
| Receipt principal/session or cached admission | Exact publisher/issuer/implementation/principal/credential/session/role/project/destination/context/actions/grant revision/epoch/policy; current expiry/revoke/budget/context | Separately enrolled supervisor, native-check and operator ports; state booleans/Basic/env identity never instantiate capability |
| Replay only in memory | Durable unique nonce/ticket/native-request-scope pending to validated or uncertain; restart/failure denies reuse | Actual SQLite restart, changed-ticket, uncertain and cached-admission negatives |
| Implicit conversion/fallback | Explicit v2 config policy and approved observer/native ports; default remains v1, absent v2 ports deny before state/private reads | Actual factory/consumer integration; genuine installed adapter remains unavailable |

Native runtime fixture claim state is in-memory, unlike the Python SQLite ledger;
the latter cannot certify transactional remote grant/budget debit or native replay
durability. Operator max-age is required `max_financial_age_seconds`, with exact
scope/original timestamps/current revocation; date-only legacy facts do not upgrade.
Provider-unknown policy approval does not allow unknown local enforcement. Neither
signatures, state booleans nor cached prior admission create enrolled capabilities.

## Exact remaining bindings and requested source scope

1. **Genuine authority:** the existing authenticated SDK/server supervisor must
   provide `ModelPhaseAuthority.authorizeSession` with current session ownership,
   opaque principal capability and approved session reference on every operation.
   The sparse SDK channel has no default implementation; its `CHANNEL.md` identifies
   `authorization.ts`/`session-location.ts` as existing context surfaces but neither
   alone is enrolled-principal proof. No new JSON auth field can fill that gap.
2. **Owner check publisher:** no genuine installed publisher binding is supplied. The Python
   port is implemented. Actual isolated `createV2Guard({onChecks})` now invokes the
   public completed-check publisher after native checks; its real output is consumed
   by the factory integration, not replaced by a synthetic owner receipt generator.
   Credentials/quota/authority upstream inputs remain synthetic: neither the private
   guard nor genuine owner account was exercised. Runtime-reported final source artifact:
   `6cb7fc98728449d640b9711389ef036de78735de74b0da8d60ccfe2d304c9e96`.
   Independently observed `guard-check-publisher.mjs` file SHA-256:
   `b690e593fd0e9039e406ad8626f76b42b014b0f7e7b4e69fc9a07dac8fec5ce4`.
   Runtime-owned `AUTHORITY-BOUNDARY.md` requires explicit enrollment of the separate
   Tessera external-agent principal/credential/project/destination to the dedicated
   instance/session supervisor, current approval/expiry/revocation checks on each
   prepare/read/consume. Basic username, shared pairing auth or resolved session
   location alone cannot establish that enrollment. No JSON/environment identity
   fallback or owner-token proxy is allowed. Private guard/auth stays with its owner.
3. **Actual installed prerequisites:** v2 semantics are reviewed and implemented
   opt-in; `V2AttestedHarnessObserver`/`verify_binding` remain strict v1. Owner must
   supply approved enrollment, explicit financial recency policy, current scoped
   operator reference resolver and installed mandatory terminal adapter. The
   [actual authority map](operator-financial-attestation.md) supplies no financial
   issuance/runtime-owner mapping adapter yet; grants/capabilities/context JSON
   must not be repurposed. No genuine account/provider/installed fact is fabricated.

## Checkpoint

`pytest -q -s tests/test_guard_phase_join.py --basetemp <owned-test-temp> --tb=short`:
**21 passed, 0.63s, exit 0**. A test-temp Bun helper reuses bounded public fixture
setup and imports the actual isolated public SDK entry, actual preparation/hook
layers, and read/consume channel. Upstream AI/resource and principal authority are
explicit mocks; dispatch/network count is zero. Late registration invalidates
consumption. Python joins separately labelled synthetic owner callback receipts and
tests mismatches, quota age/failure, invalid financial evidence, forbidden fields and
one-shot behavior. No actual owner guard, credential resolution or genuine auth.

Isolated source handoff artifact:
`bfa2e96b7f71dbad1de004f52f0b5fc9bf89c3a330567ab147c444298d76af51`.
SDK partial JSON fixture digest (timestamp-dependent):
`24fcf3a7810f83f9829b137b0e47d467250d1dd0e676a7ef47d0a02cd2fcf8b8`.
No isolated source modifications, SDK installation, active harness/service/config/
binary/financial changes, credentials, provider inference, deployment or historical
4268 gate rerun. This historical checkpoint is preserved, not rerun.

Final revision-5 composition gate: `pytest -q -s tests/test_rc_research_factory.py
tests/test_research_factory.py --basetemp <owned-test-temp> --tb=short`:
**11 passed, 3 existing dependency warnings, 5.48s, exit 0**. The added bounded case
executes actual public guard/publisher and SDK read/consume in a temp Bun helper,
then joins their serialized outputs through actual factory durable diagnostics,
persists/reopens and rejects nonce replay. Synthetic credential/quota/authority/AI
resources; zero dispatch/network. Serialization is not production authority proof.
Timestamp-dependent joined diagnostic digest:
`715c70f8e03b1bab99d5a636165dc2a2d0d442cac93fc6cc0015420701cceeb8`.
