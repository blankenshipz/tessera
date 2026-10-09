# Human-issued research control and input records

The ordinary factory now uses native authenticated backend ports, not injected
fixture callbacks. It consumes human-created ordinary `agent-contexts` records
with body schema `tessera-human-research-record-v1` (`HumanJourneyRecord` in
`research_backend_ports.py`). These records do **not** mint execution authority.

Fields are closed: `schema_version`, `journey_id`, increasing integer `sequence`,
`references`, `disclosures`, optional `review`, `critic_input`, `planner_input`.
`references` contains only the existing journey control/disclosure/transport and
issued-command references accepted by `ResearchJourney._poll`. `disclosures` maps
registered output IDs to current human-issued recipient disclosure IDs. Each peer
input is exactly an `output_id`/`disclosure_id` pair. A review is exactly an immutable
proposal `subject_id` and `decision` (`approved` or `rejected`). Model text is never
interpreted as any of these records.

Human issues the record using the existing ordinary context API, then separately
issues its policy context. Policy `input_context_id` names the returned record ID;
both input hashes equal SHA256 of the actual record body's UTF-8 bytes. Other policy
pins retain the role's actual run/session/model/harness/skill/config and allowed
capture events. The consumer discovers the matching policy through authenticated
context listing (bounded to 400 rows), selects a unique highest sequence, and calls
the [native resolver](capture-context-http.md). Its actual current human issuer,
tenant/project/recipient/destination, bodies and policy hashes must verify. No
`created_by` role inference, caller approval flag or policy self-reference is used.
An ambiguous latest sequence/policy fails closed. Historical revoked issuers are
not revalidated as authority for a later independently issued record.

Operator updates are new immutable records with a greater sequence, never agent
self-approval or mutable files. Record reads are ephemeral: bodies and prompt/tool
text are not journaled. Current record/peer output/disclosure/digest identities are
bound before the associated input effect. Grant consumption additionally requires
actual backend proposal promotion and separately human-issued current bounded
execution authority. A human review context cannot substitute for either.

Critic input contains the actual curated body plus the separately disclosed planner
bytes; planner continuation contains its actual curated body plus the independently
registered/disclosed critic bytes. The source is labelled data, not authority.
The composed input digest and opaque initial-input references are durably admitted
before the same-session prompt, and current human/source fences are re-read before
the actual harness POST. Unknown prompts remain held and are never re-prompted.

The final-output producer supports **only** separately human-admitted registered
`generated_output` events. It reads the actual pinned V2 assistant message matching
the registered source ID, drops reasoning/tool/provider parts, verifies text SHA256
against the real registry, and emits approved metadata with `capture_state:unknown`.
It does not count private parts or invent public visibility/full-stream watermarks.
Policies asking for other categories fail closed. Full interaction coverage remains
unknown. Registered critic capture pins are read back from the actual notebook,
then joined to actual writer and planner recipient registry/disclosure reads.

Attempt status uses the native admitted-attempt `state`, not an invented `status`
field. Terminal success/failure/cancellation is not treated as operational termination
without actual `operational_observed_state:exited` and termination confirmation.
Unknown remains polling/held. Observation records link the actual terminal attempt
revision and explicitly do not assess scientific validity; no artifact, model or
experiment receipt is invented.

The default observer is now `V2AttestedHarnessObserver` over authenticated strict
`ObserverHttpEndpoint` subclasses with the role destination and explicit verified
CA mapping. Each role requires `guard` enrollment configuration: independent HTTPS
`origin`, private `credential_file`, optional verified `ca_file`, plus public
`issuer`, `trust_reference`, `producer_sha256`, `enrollment_id`, `instance_id`,
`principal_id`, `entitlement_reference`, `entitlement_sha256`, `policy_sha256`.
Enrollment equals the role's `observer_admission_id`; principal and instance equal
the actual role/capture pins. Guard origin cannot be either harness, control or
known coding-service origin. No default issuer, coding endpoint or fixture flag exists.

The constructor consumes the [guard protocol](../plugins/tessera-harness-observer/README.md)
using fresh nonces, strict closed fields, <=30-second validity, current revocation
and config/catalog/skill/account/hook measurements. HTTPS authenticates the approved
issuer, not the truth of its claims. **The guard producer is not shipped or admitted**:
its endpoint is an explicit required dedicated integration contract, not an existing
V2 route or measured installed proof. Current V2 lacks effective-state/final-hook/
build-identity/included-billing fields. A separately authorized truthful producer
needs human-measured secure handoff and/or an authorized OpenCode observability
extension for those fields. No runtime/context adapter supplies them from a manifest
or boolean. Factory verifier/source wiring is complete for this contract; producer
implementation, dedicated endpoint admission and installed/live acceptance remain
open. Source and synthetic trusted-response tests do not complete architect scope.
