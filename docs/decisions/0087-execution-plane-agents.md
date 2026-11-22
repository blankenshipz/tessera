# 0087 — Execution-plane agents: a bounded agent beside the compute

## Status

Draft for review (2026-11-22). Documentation only: this record changes no code, schema, route,
unit or default, and nothing in it is active until a later slice lands under its own review. It is
the second tier of the agent design. The first tier, the control-plane loop, is
[ADR 0082](0082-agent-loop-anthropic-destination.md) (enrolled model destination),
[ADR 0083](0083-model-call-receipts.md) (model-call receipts) and
[ADR 0085](0085-standing-agent-loop-and-auto-created-research-jobs.md) (standing loop, kill switch);
those three are proposed on other branches, and so are ADR 0071 and ADR 0086 cited in section 9
(links to all five resolve only once they merge). Every reference to them below is to their
proposed text, and the credential model below depends on the *revised* ADR 0082 (an Anthropic
Messages API key held by reference, called directly), not on its current subscription-login
wording. This draft also widens ADR 0085 decision 2 (the marker "touches nothing else") to gate
the agent's model calls and its retry, resize and re-route requests (nothing else: observe, report,
stop, salvage and pacing are never gated); that widening needs a matching amendment of 0085. The no-public-internet invariant of
[ADR 0047](0047-agent-routed-data-destinations.md) is unchanged, but section 3.2 records that
allowing a registered model destination for a stage with non-public input is an amendment of its
spec-validation rule ("no egress for a non-public stage"), which this draft does not make. Closes no
[ADR 0033](0033-freeze-over-built-planes.md) freeze: see "Freeze" below.

Implementation status: the first slice (S1 contract digest + S2 observe-only watcher, disabled by
default) is implemented. The worker starts it beside a local Docker stage only with the explicit
`--stage-watcher` operator flag and a kill-switch path; see
[the stage watcher](../stage-watcher.md). The worker wiring (supervisor hook, service attach, CLI
flags) touches the ordinary-docker plane that ADR 0033 freezes (bug and security fixes only). It
is additive, disabled by default, read-only, spend-neutral and cannot change execution results.
It is treated as a narrowly scoped amendment of ADR 0033, proposed and pending owner
confirmation: the owner has not yet confirmed it in a decision record. The second slice is
[ADR 0111](0111-stage-watcher-decision-requests-and-remote-wiring.md): it records and limits the
Salad-plane amendment for two disabled-by-default Salad hooks (the snapshot write and the
executor's watcher ticker), and adds the watcher's retry and resize *requests*, recorded by
control as advisory events that are always `not_admitted`. That record concerns the Salad plane
only. Runner admission of a request remains open and needs its own reviewed ADR. Everything else
in this record (admission, model calls, the Salad workload sidecar, escalation) remains a draft
and inactive.

## Context

Tessera has two kinds of decision-maker today and a gap between them.

- **Control plane.** The campaign runner (ADR 0038) advances an approved, digest-pinned spec on a
  2 s tick. It owns every retry, placement, cancel and settle decision (ADRs 0046, 0053, 0058,
  0060, 0061, 0063, 0064, 0067). It never starts a container or calls a provider; a worker or an
  executor does. The control-plane agent loop (ADRs 0082, 0083, 0085) will sit above it, turning
  plans into campaigns and research jobs.
- **Execution plane.** The worker supervisor leases a grant, runs the container and reports
  observations through a durable outbox (ADR 0054). It cancels a container only when it loses its
  authority. The container is passive. A Salad workload is the same shape behind the guardian
  (ADRs 0024, 0036). Nothing on the box decides anything.

The gap is local, time-critical judgement while a run is in flight. Today a person (a live session
or a driver script) fills it: reading a stuck start and stopping it, deciding that a missed rate
read is not a reason to cancel a good run, salvaging what a crashed container left behind,
resizing inside a profile, re-trying a failed upload, and reporting what happened. Two things make
that slow and unsafe. A human is not beside the compute, so the decision is late or wrong
(a heartbeat refusal misread as a failure, a stuck group stopped by hand, orphaned groups cleaned
up with scripts outside Tessera). And a decision made by hand leaves no receipt, so it cannot be
audited, which breaks the receipts-for-everything rule.

The north star names the second tier: execution agents "handle local decisions (retries,
resource sizing, data placement within the allowlist) and escalate bigger ones."
This ADR defines what such an agent is allowed to be, with the same hard limits the platform
already enforces on everything else.

The closest prior art is the independent monitor and guardian
([ADR 0021](0021-independent-sandbox-supervision.md),
[ADR 0022](0022-transferable-execution-monitor.md)): a process that runs beside the target and
outside the agent under a signed assignment that grants "observation and exact-owned stop/cleanup
only". An execution-plane agent keeps that posture and adds bounded, receipted judgement. It does
not replace the guardian, which stays the independent spend-safety backstop.

## Decision

### 1. What an execution-plane agent is

An **execution-plane agent** (EPA) is a process that runs inside a provider placement for one
attempt of one stage (a Salad container group's workload sidecar, the local runner's worker-side
sidecar, a later provider's equivalent). It is created for the attempt, receives the stage
contract, watches the run, takes the local decisions the contract lets it take, and reports only
through receipts. It ends when the attempt is settled. It is not a standing service, holds no
state beyond the attempt (the platform records it), and never outlives the grant.

An EPA is not a second runner. It cannot create, approve, start, widen or extend anything the
approved spec does not already allow, and it cannot reconcile a grant or settle money.

### 2. The stage contract

The control plane gives the EPA one signed, digest-bound **stage contract** per attempt. It is
derived from the approved spec and the attempt record by the runner, never authored by an agent,
and it is delivered the way the stage's other inputs are (a grant for the worker path, the
workload contract for Salad). It contains:

| part | content | source today |
|---|---|---|
| identity | campaign, stage key, attempt number, placed provider and runtime, grant id and epoch | attempt record, placement (ADR 0060) |
| the work | image digest, argv, declared inputs (digests) and outputs (name, path, `max_bytes`) | v2 stage (ADR 0040) |
| hard bounds | `max_duration_seconds`, grant expiry, `max_exposure_cents`, remaining campaign cap | stage fields, budget ledger |
| retry envelope | `retry.max_attempts`, `transient_exit_codes`, `infra_retries`, `infra_exit_codes`, `reroute_providers`, `resource_limit_metrics` | ADRs 0038, 0058, 0067 |
| resource envelope | the profile's maximum CPU, memory, scratch, and the sizes the stage was approved at | provider profile |
| egress | the stage's allowlist (ADR 0034 policy, or Salad's provider-wide contract) plus, if the stage declares one, the registered model destination (section 5) | spec, destination registry (ADR 0082) |
| escalation address | where and how to report a decision it may not take (section 6) | runner |
| kill-switch reference | the marker to read before each model call and each request (section 7) | operator config |
| contract digest | digest of everything above, recorded on the attempt and the receipt | new, additive |

Anything not in the contract is not allowed. The contract can only narrow the approved spec: a
bound that is absent reads as zero, never as unlimited. A contract that does not verify (digest,
signature, epoch, expiry) is refused and the stage proceeds as it does today, with no EPA.

### 3. The trust boundary

1. **Credentials.** The EPA holds no credential beyond the stage grant: the worker credential or
   the short-lived workload token the placement already issues for this attempt, and nothing else.
   It never holds a control or human credential, a provider management key, a Salad management
   credential, the guardian's key or any other stage's grant. It cannot issue, renew or widen a
   credential. Consequence: anything that needs a provider management API (create, start, stop or
   read a Salad group, resize a running group, clean up groups) is not EPA work and stays with the
   control plane, the trusted server proxy or the guardian. The model credential is an Anthropic
   Messages API key held **by reference** by the control-side relay (or agent loop) that calls the
   API (ADR 0082, revised); the key itself never appears in a placement, its environment, the
   stage contract, a prompt, a model output or a receipt, and is never delivered to the EPA. A model call from an EPA therefore has exactly one admissible shape, a relay on
   the control side that holds the reference (section 5.1); until one exists, an EPA makes no
   model call.
2. **Egress.** The EPA's code is written to contact only the outbox and grant endpoint and the
   artifact upload and custody endpoint of its own attempt (salvage and upload pacing need the
   latter), plus, once a relay exists, that relay. Whether the network *enforces* this depends
   on the placement, and the draft does not claim more than each placement does today:
   - **Local runner (ADR 0034).** Enforced outside the workload by the allowlist proxy and an
     internal network: the allowlist plus, later, the relay host for that attempt.
   - **Salad.** Egress is provider-wide with no per-host allowlist (ADR 0036). The network does
     **not** restrict an EPA there; the public internet is reachable by construction, with or
     without a model. The only controls are the sidecar's own code and the data-class rules
     (a stage with non-public input still may not declare fetch hosts, ADR 0047). So on Salad an
     EPA runs deterministic actions only (slice S4), and model-assisted judgement is **gated on a
     per-host enforcement mechanism that does not exist today** (open question 1).
   An EPA never receives a destination from a prompt, a model output or an input file. Sealed
   holdout data ([ADR 0068](0068-sealed-holdout-evaluation-stages.md)) is never a model input. A
   stage with a non-public input class cannot have egress at all under ADR 0047's spec validation,
   which is not configurable. Letting a registered model relay be reachable from such a stage
   would amend that rule even though the no-public-internet invariant itself would hold, so it is
   a separate decision (open question 6), not implied here.
3. **Authority.** The EPA acts only inside its own attempt and only through the **action set** in
   section 4. Actions that change what runs, where or how large (retry, resize, re-route) are
   *requests* to the runner, which admits or refuses them against the approved spec; the EPA
   never performs them itself. It cannot perform a step outside the closed `STEP_TABLE`
   (ADR 0040), and it cannot register a new one. It never touches another attempt, stage or
   campaign.
4. **Evidence.** The EPA is untrusted evidence, like any worker observation: the platform
   records what it reports as the worker's unverified claim, and every value that moves money,
   gates a result or releases custody still comes from a platform- or guardian-measured source.
   An EPA can never mark a stage succeeded, settle a hold or release an artifact. A stage cannot
   succeed without a run receipt (ADR 0038), and the run receipt does not come from the EPA's
   say-so alone.
5. **Independence of the backstop.** The guardian, the egress proxy and the grant deadline stay
   outside the EPA's reach. The EPA cannot stop the guardian from stopping a run, extend a
   monotonic deadline or silence a heartbeat the guardian watches.
6. **Prompt injection.** Anything the workload, its inputs or a model returns is data. An EPA
   action is chosen from the closed action set by the contract's rules and the agent's
   own bounded judgement; a free-text instruction in data never adds an action, a destination or a
   credential.

### 4. The action set

Everything is bounded by the contract and recorded (section 8) *before* it is taken. Anything
outside these tables, or beyond a bound, is an escalation, never an action.

**Direct actions** (inside the attempt's own container and outputs; no management credential):

| action | what it may do | bound |
|---|---|---|
| observe | read run state, logs, resource use, output sizes, container-local health | read-only, this attempt only |
| salvage partial outputs | after a non-zero exit or crash, collect, hash and upload declared outputs, marked partial, never upgraded to complete | declared outputs only; custody and quarantine unchanged (ADR 0050, artifact quarantine) |
| upload pacing | choose chunk sizes and pacing for a large output within the stage's limits | the hold rules of ADR 0064 and the output hold of ADR 0086 apply unchanged |
| stop its own run | reduce-only stop of the attempt's workload when a platform-measured guardrail trips (wall clock, silence, resource limit, a measured stop metric) or when told to by grant revoke or cancel (never by the kill switch, section 7) | stop never deletes custody; the same effect as the worker's cancel today (ADR 0054, ADR 0061) |
| report | post observations, decisions, requests and model-call records through the outbox | section 8 |

**Requests** (the EPA proposes; the runner decides, because the runner owns every retry and
placement decision, ADRs 0038, 0058, 0060, 0067):

| request | meaning | who admits and how |
|---|---|---|
| retry | ask for a new attempt after a failure the stage already allows (ADR 0038 T2, ADR 0058 infra codes, ADR 0067 resource limit) | the runner, against `max_attempts` + `infra_retries`; never for an effect whose outcome is unknown ("Tessera does not retry what may still be running") |
| resize | ask that the next attempt run at a different size inside the provider profile | the runner, within the profile maximum and `max_exposure_cents`; resizing a *running* Salad group is a management call and is not available |
| re-route | ask for the next approved candidate (`alternate_providers`, `reroute_providers`) with the skip reasons it saw | the runner's placement walk (ADR 0060); only approved candidates |

Both the request and the runner's decision (admitted, refused, reason) are recorded on the
receipt; a refused request is never re-sent in another shape. A missed or late observation
delivery is *not* a retry reason: the outbox already redelivers (ADR 0054) and never creates an
attempt.

Not in either table, always an escalation: raising any cap or deadline, approving or
pre-approving anything, changing a destination or trust setting, classifying data, reading a
sealed partition, recording a gate go or a scientific verdict, issuing or renewing a credential,
any provider management call, cleaning up groups or hosts it does not own, resolving an unknown
or lost effect, reconciling a grant, billing reconciliation, a spec change, adding a provider or
candidate, deploying, migrating, or touching another stage, attempt or campaign.

### 5. Model calls

An EPA may use a model for its local judgement (reading a log to classify a failure, choosing
between approved candidates). It does so under the control-plane loop's rules, not its own:

1. **Destination.** Only a destination registered in the ADR 0082 registry and named in the stage
   contract, reached through a control-side relay that holds the credential reference (the EPA
   never holds it, section 3.1). The relay endpoint is reachable by that one attempt only while
   its contract is live. It is not added to the stage's allowlist profile, Salad fetch hosts or
   any other stage. Today nothing lets a stage container reach a model destination; the relay and
   the per-attempt rule are a prerequisite, not an assumption (see "Open questions").
2. **Receipts.** Every call is recorded in the ADR 0083 shape (configured and reported model,
   tier, prompt and input digests, policy digest, tokens, cost basis, observed time), attached to
   the attempt's receipt entry verbatim. Prompts, completions and error text are never recorded.
   `model_reported` is never treated as verified.
3. **Tier and limits.** One enrolled model and tier per contract. A rate limit or quota refusal
   backs off and the tier never downshifts (ADR 0083 decision 4). An uncertain call is never
   replayed (ADR 0085 decision 4).
4. **Data.** Prompts carry only data the stage's trust setting lets that destination receive.
   Private and `private_derived` content goes only to a destination the operator registered for
   it; otherwise the EPA runs without a model and escalates.
5. **The key is a reference, held control-side.** The Anthropic API key exists only as a runtime
   credential reference held by the relay; it never appears in a placement's environment, the
   stage contract, a prompt, a model output or a receipt. No Claude Code harness and no
   subscription login are involved on the compute side.

### 6. Escalation

When the EPA reaches a bound or needs a decision outside section 4 it records an **escalation**
(what it saw, which bound, which options it considered, what it did meanwhile, which is nothing
that spends, widens or deletes) and reports it through the same outbox. The control-plane loop
(ADR 0085) or a person resolves it; neither is blocked on the EPA to act. While an escalation is
open the EPA takes no further action that the escalation concerns, and the stage's own deadlines
keep running. A request that is refused is recorded as refused and is never retried by the EPA
with a different shape.

### 7. Kill switch

1. **One switch, shared.** The ADR 0085 operator marker is the kill switch for the EPA as well as
   for the control-plane loop. Before every model call **and every request in section 4**
   (it never gates observe, report, a reduce-only stop, salvage or upload pacing), the EPA reads it; a missing, unreadable
   or ambiguous marker reads as engaged (fail closed). Engaged, the EPA makes no model call and
   files no new retry, resize or re-route request; it still observes, reports, and may stop its
   own run on a measured guardrail or a revoke or cancel (never because of the switch). Salvage and
   upload pacing stay allowed: they preserve evidence, add no spend and no egress beyond the
   attempt's own upload endpoint, and losing partial outputs to a crash while the switch is on
   would destroy exactly what the receipt needs. The runner treats an engaged switch the same way for any EPA request it has not
   yet admitted.
2. **What it does not do.** It never cancels, interrupts or inspects a run in flight, so it is
   safe at any time (ADR 0085 decision 2 and its rejected alternative stand). The run continues
   under its deterministic guardrails: the grant deadline, the guardian, the egress proxy and
   the stage's stop rules fire without an EPA.
3. **Separate from cancel.** Stopping *work* stays campaign or stage cancel and grant revoke
   (ADR 0038, ADR 0061). An EPA told to stop by either performs a reduce-only stop; the kill
   switch is not a cancel and must not be used as one.
4. **Reaching a remote placement is unsolved.** The ADR 0085 marker is a file on the control
   host. A container on a remote provider cannot read it, and an unreadable marker reads as
   engaged forever, which would idle every remote EPA. How the engaged state reaches a placement
   (carried in the stage contract and refreshed by the grant lease, polled from the outbox
   endpoint, or enforced only control-side by refusing requests) is open question 7. Until it
   is solved, a remote EPA runs only with the switch state delivered in its contract and treats
   a stale or missing value as engaged.
5. **An EPA is optional per stage.** A stage with no contract runs exactly as it does today. The
   EPA can never be a reason a stage cannot run, fall back, retry or cancel.

### 8. Reporting and receipts

**Record before acting.** The EPA writes each action (and each request) durably to its outbox
before taking it, so an action never happens without a record in flight. An outbox does not
survive a lost container and reports are abandoned after 24 h (ADR 0054), so the receipt shows
undelivered, abandoned and missing action reports as explicit *gaps* (counts and the last
delivered action), never as silence. A gap is evidence to critique, not an error to hide.

The EPA reports only through the existing observation path (ADR 0054: durable outbox, stable
idempotency keys, backoff, abandon after 24 h), never a new channel to a human or another
plane. Control records what arrives as the worker's claim. The campaign receipt gains an
**optional, additive** section per attempt (absent when no EPA ran, so no existing receipt or
digest changes), carrying:

- the contract digest and the EPA build identity;
- each local action with its kind, time, the bound it was inside, and the platform-measured
  evidence that justified it;
- each escalation and its resolution;
- the model-call records (section 5.2), verbatim from ADR 0083;
- a closed set of outcome fields (`salvaged_partial`, `retry_requested`, `resize_requested`, `reroute_requested`,
  `stopped_on_guardrail`) that are descriptive only. Whether a request was admitted comes from the
  runner's own platform record, never from the agent's report.

The receipt marks every EPA-derived field as an unverified claim. Anything that decides cost,
success, custody or a gate keeps its platform-measured source. A scientific verdict stays
`pending_human_approval` regardless of what an EPA reports (ADR 0038, ADR 0066).

### 9. Which hand steps it absorbs

Mapped from the steps a live session or driver script does by hand today. "Absorbed" means
done by the EPA inside the action set of section 4. Steps that happen before the container
exists, or need a provider management credential, are control-plane, server-proxy or guardian
work and are *not* the EPA's, even where an agent would help.

| today, by hand | absorbed? | where it belongs |
|---|---|---|
| watch a start that sits in "creating"; judge "stuck"; stop it | no | before the sidecar exists and needs the management API: control plane and guardian (settle-before-start is [ADR 0071](0071-salad-start-settles-the-group.md)) |
| settle poll after create, before start | no | ADR 0071 (platform) |
| try another approved candidate after a stuck start or resource stop | request | the EPA may request it after a resource stop it observed; the runner's placement walk (ADR 0060) decides |
| a missed rate read or one failed observation is not a reason to cancel a healthy run | yes | observe and report; the EPA never cancels on one miss, and a miss never creates an attempt |
| retry a transient failure (timeout, lease lapse that proves nothing started) | request | the runner admits (ADR 0058); "may still be running" is never retried |
| salvage partial outputs after an exit with no delivered report | yes | salvage; partial status reported; custody unchanged |
| large-output upload that stalls | yes, pacing only | chunk pacing inside limits; the hold itself is ADR 0064 / [ADR 0086](0086-hold-an-exited-attempt-until-its-output-registers.md) |
| scratch or memory too small (ENOSPC, OOM) | request | next attempt at a larger size inside the profile, admitted by the runner; widening a profile is an escalation |
| clean up leftover groups and stale host state | no | needs a management credential and other attempts' resources: control plane or operator |
| confirm a stop and close writers when custody cannot seal | no | stop confirmation comes from the guardian or server proxy; the EPA can stop its own run and report |
| diagnose and write the reason for a failure | yes | a decision record with receipts as evidence |
| reconcile after exit with no report | partly | collect and report what exists; resolving an unknown effect or a held grant stays human or the guarded ADR 0046 / 0053 paths |
| get the real billed cost from a provider portal | no | an input the agent can only report |
| admit a data class to a provider, approve a digest, pre-approve a review | no | owner or human act |
| raise a cap, a deadline or a budget; authorize after an attempt exists | no | spend increase |
| issue or renew a credential | no | operator act |
| gate go, holdout opening, hyperparameter freeze, conclusions | no | human, sealed, pre-registered |
| deploy, migrate, restart services after a merge | no | operator |
| supervise an idle session, reconnect a client | no | scaffolding to remove, not a decision |

## Slice plan

Each slice is separately reviewed, lands behind a default-off operator setting, changes no
existing receipt or digest, and keeps a stage without a contract exactly as it runs today.

1. **S0, this ADR.** Review and agree the contract, trust boundary and action set. No code.
2. **S1, contract digest and receipt shape (no agent).** The stage contract model and its digest
   (runner and receipt code, not a frozen plane), stored on the attempt record only. The
   receipt's optional EPA section stays absent when no EPA ran; S1 only defines and tests its
   shape. Tests use fakes.
3. **S2, observe-only EPA on the local runner.** A worker-side sidecar that reads state and
   resources and reports decisions of kind `observed` only. No action. Proves the outbox path,
   the contract verification and the kill-switch read with no authority to do harm.
4. **S3, deterministic actions (no model).** Salvage, upload pacing and reduce-only stop as fixed
   rules over platform-measured evidence, plus the retry, resize and re-route *requests* and the
   runner-side admission that decides them, on the local runner. The kill switch gates every
   model call and request; observe, report, stop, salvage and pacing continue. This absorbs the in-attempt hand steps without any model
   call or new egress.
5. **S4, Salad workload sidecar (deterministic only).** The same rules inside the Salad placement
   under the guardian. Salad egress is provider-wide and the network does not restrict the
   sidecar (section 3.2), so S4 ships with no model call and with the sidecar's own code limited to
   the same list as section 3.2: the outbox and grant endpoint (on Salad, the endpoint its workload
   token already reaches) and the attempt's artifact upload and custody endpoint; it needs the kill-switch delivery of open question 7 and an explicit
   ADR 0033 amendment for the Salad plane.
   A smaller observe-only step is built beside the Salad plane: a remote watcher in the
   provider-side driver's process owner (never in the container, no management credential) that
   reads a non-secret signal snapshot (`salad_watch_signals.py` maps a poll to it; the runner's
   `on_start_remote` and probe-factory registry consume it) and reports through the worker
   credential, descriptive only. **Implemented by
   [ADR 0111](0111-stage-watcher-decision-requests-and-remote-wiring.md), which makes the
   amendment:** (i) one read-only, non-secret, spend-neutral snapshot write in the live-check poll
   loop after each successful poll, and (ii) a tick of the runner by the executor process that
   holds the worker credential while the route child process runs, both disabled by default. Request
   records are advisory; admission by the runner is still not built.
6. **S5, model-assisted judgement.** The control-side relay of section 5.1, the ADR 0083 call
   records on the attempt, the shared kill switch, an enrolled tier per contract. Optional per
   stage. On the local runner it needs the relay and the per-attempt rule; **on Salad it is
   blocked until per-host egress enforcement exists**. Every S3 rule still works without it.
7. **S6, escalation to the control-plane loop.** The loop (ADR 0085) consumes escalations and
   may pre-position decisions inside a stage's approved bounds. Still no spend, approval or
   credential authority.

A slice can be stopped after any step with the platform in a fully working state.

## Freeze

[ADR 0033](0033-freeze-over-built-planes.md) froze the Salad guardian, custody and workload-token
stack and ordinary-docker execution (bug and security fixes only, with an exception for
runtime spend-safety fixes). [ADR 0082](0082-agent-loop-anthropic-destination.md) lifted the
freeze for the agent and reasoning plane only. S1 is runner and receipt code, which the freeze
does not name, so it is expected to need no amendment. S2 and S3 add a sidecar next to the local
worker and are treated as touching the ordinary-docker plane (the S2 wiring edits the supervisor,
service and CLI), so they are proposed as a narrowly scoped amendment of ADR 0033, additive,
disabled by default, read-only and spend-neutral, pending owner confirmation in a decision
record. S4 touches the Salad plane and S5 opens the model path, each needing its own explicit
amendment, raised when those slices are scheduled. The two edits the observe-only remote watcher
needs (a snapshot write in the live-check poll loop, and a runner tick beside the executor) are
recorded and limited by [ADR 0111](0111-stage-watcher-decision-requests-and-remote-wiring.md),
which the owner directed for slice 4; it names the frozen Salad files touched and the guarantees,
and concerns the Salad plane only. Everything else in ADR 0033 stays frozen. This ADR does not
itself lift any freeze.

## Consequences

- Local judgement moves from a person at a terminal to a bounded, receipted process beside the
  compute, with no new authority: every action is one the platform already permits.
- A decision that used to leave no trace now leaves an attempt-level record with its evidence,
  strengthening the receipts-for-everything rule.
- The EPA adds an evidence stream that is explicitly unverified. Reviewers must not read
  `model_reported`, an EPA outcome field or a classification as a platform measurement.
- The largest new surface is section 5.1: letting one attempt reach a registered model host.
  Until it exists, an EPA runs deterministic actions only.
- Salad's egress is provider-wide, with no per-host allowlist, so the network does not restrict a
  sidecar there (section 3.2). That is recorded, not papered over: on Salad an EPA is deterministic
  only, and model-assisted judgement is gated on a per-host enforcement mechanism that does not
  exist today.
- Cost of a stuck run is bounded by guardrails that do not depend on the EPA; the EPA can make a
  recovery faster and cheaper, never possible only through the EPA.

## Alternatives considered

- **Keep it all in the control-plane loop.** The loop is not beside the compute, polls on a tick,
  and cannot salvage a container's local state; the failures above are local and time-critical.
  Rejected as the only tier, kept as the escalation target.
- **Give the EPA a control credential.** It would let a workload-adjacent process approve or
  widen. Rejected outright; the EPA has the stage grant only.
- **A free-form on-box agent with a tool shell.** Unbounded authority and prompt-injection
  exposure. Rejected; the action set is closed and contract-bound.
- **A second kill switch for EPAs.** Two switches drift. Rejected; one marker, ADR 0085's.
- **Make kill cancel in-flight runs.** Unsafe to use at any time; ADR 0085 already rejected it.
- **Let the EPA settle or reconcile.** Money and custody decisions stay platform-measured and
  human where unknown (ADR 0046, 0053, 0064).

## Open questions

1. The model-destination mechanism for a stage placement (section 5.1). Only a control-side relay
   fits the credential rule of section 3.1; a per-attempt proxy rule is still needed on the local
   runner, and Salad needs per-host egress enforcement it does not have. Needs a security review
   before S5.
2. Whether in-flight `stop_when` evaluation (today evaluated only after a stage succeeds) belongs
   in the platform or stays an EPA guardrail over measured metrics.
3. Whether the contract is delivered by the grant (worker path) and the workload contract
   (Salad) or by one new object; S1 should pick the smaller change.
4. How an EPA build is identified and attested so the receipt can say which agent logic ran
   without trusting its own claim.
5. The wording of the ADR 0033 amendment for S2 and S3 and whether the owner wants it before or
   with S1.
6. Whether a registered model relay may be reachable from a stage with non-public input. ADR 0047
   refuses any egress for such a stage at spec validation and that rule is not configurable, so
   this would amend it; the no-public-internet invariant would still hold. Not decided here.
7. Who signs the stage contract and how the EPA verifies it (new key material), and how the
   kill-switch state reaches a remote placement (see section 7.4). No model credential is ever
   delivered to a placement, so key delivery is not an open question; only a control-side relay is
   admissible.
8. How the runner admits, orders and rate-limits EPA requests (retry, resize, re-route) and how
   a request that races a runner decision is resolved, so the runner stays the only decision
   maker.
