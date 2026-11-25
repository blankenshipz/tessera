# Campaign compute providers

Contract: [ADR 0040](decisions/0040-multi-stage-multi-vendor-campaigns.md), which amends
[ADR 0038](decisions/0038-campaign-specs-and-platform-campaign-runner.md). A campaign
stage names a compute *provider* and a *runtime*; the runner calls only the provider
interface, so a vendor is a plugin rather than a branch in the runner. This page is the
operator side: which providers exist, how to configure them, and how the self-hosted
provider and the Salad provider are run. Which data classes each provider may receive is a
separate, deployment-owned setting: see [provider trust](provider-trust.md) and do not
duplicate it here.

## Providers

| Provider id | Kind | Runtime | Runs where | Billable |
| --- | --- | --- | --- | --- |
| `local-docker` | `worker_grant` | `docker-cpu-v1` | the control host's development worker | no (`local_zero`)¹ |
| `self-hosted` | `worker_grant` | `docker-cpu-v1` | a server you run, under its own worker principal | no (`local_zero`) |
| `salad` | `salad_route` | `salad-workload-v1` | Salad, through the operator-run executor | yes |

¹ A grant a human reconciled with a recorded cost settles `human_reconcile` instead.

`local-docker` is built in. `self-hosted` is another instance of the same code: the
unchanged development worker and grant poller on a server of your own, with no GPU. Salad
is a plugin over the Salad stage driver. Every provider declares the delegated steps
(`campaign_delegation.STEP_TABLE`) it may use, the output limits (Salad: 16 files, 16 MiB)
and its settlement basis; adding a step or a basis is a reviewed core change, not part of a
plugin.

Without a provider file only `local-docker` and the `salad` provider with no profiles
exist, so a Salad stage is refused with 409 until the operator supplies Salad profiles
(through the provider file or `--salad-campaign-profiles`).

### Provider plugins

A third-party vendor ships as a Python module (or installed package) with a
`register_from_config(section)` function that calls `register_provider(...)`. The operator
enables it by name in the provider file; nothing is loaded just because it is installed:

```json
{"schema": "tessera.campaign-providers.v1",
 "plugins": {"vendor_plugin": {"any": "plugin-owned settings"}}}
```

The name is resolved first as an entry point in the group `tessera.campaign_providers`,
then as a dotted module path. A plugin may not take an id another provider owns. Registration
validates the descriptor (id pattern, kind and target agree, positive output limits, known
delegated steps) and refuses a malformed one. Optional runner hooks (`sweep`, `on_settled`,
`experiment_ready`) form the `ProviderHooks` protocol; the runner skips any a provider lacks.
A settlement whose provider is no longer registered fails closed and is recorded rather than
skipping that provider's bookkeeping. A provider may also implement the optional method
`placement_capability(stage) -> PlacementCapability(tier: local|remote, hardware: cpu|gpu)` to tell
default placement (ADR 0088) what a candidate is; without it the provider is `other` and stays
where the spec put it, so a new vendor needs no change to placement.

Registration also refuses a descriptor that is a subclass of `ProviderDescriptor`, a
`worker_slots` that is not an integer from 1 to 8, and a `max_scratch_bytes` that is not a
positive integer within the operator-file ceiling (64 GiB). It refuses a provider object that
lacks any required `ComputeProvider` method, has a non-callable method or optional hook, or has a
`config_model` that is neither `None` nor a pydantic model class, and the error lists every
problem found. A refused provider leaves the registry unchanged.

### Adapting a local-docker CPU stage to Salad

Contract: [ADR 0076](decisions/0076-salad-cpu-stage-adapter-and-cpu-runtime.md). A v2 stage on
`local-docker` (`docker-cpu-v1`) lists a Salad CPU profile as an alternate with no Salad-form copy:

```json
"alternate_providers": [{"provider": "salad", "runtime": "salad-workload-v1",
                         "provider_config": {"adapt": {"profile": "cpu-2c-4g"}}}]
```

Submit (and `tessera_validate_campaign_spec`, which returns the result as `expanded_spec`) replaces
the request with the explicit `salad` block, so the approver sees and approves the exact workload
by digest and a later profile edit cannot change it. The block holds the profile and its worker, a
`gpu_runtime_v1` contract whose manifest is the stage's own argv as one image entry (no env, and
no fetch or redirect host other than the stage's `public_artifact` pins, below), one output per stage output, one `input` for the stage's snapshot, the
profile's program template, and a cost estimate of the full duration at the profile's CPU rate. The
ordinary submit, approval and placement checks then apply to it.

The operator opens a CPU profile to this by adding a `stage_adapter` section to it (its presence
is the opt-in; [ADR 0077](decisions/0077-campaign-prep-from-the-spec.md)). Every field is optional:
`program` (absent: a generic program template naming only the stage), `max_input_bytes` (absent: the
profile's `disk_mb` bounds the input) and `grant_ttl_seconds` (default 21600). The profile's
`allowed_image_digests` must hold the stage's image digest, so the same image has to run on both
providers: build it from the CPU runtime variant (`workloads/gpu_runtime_v1/README.md`, section "CPU runtime variant and baked wheels"; a source-tree file, not installed with the docs)
(it carries the runtime; the local worker overrides the entrypoint), with the dependencies baked in.
Tessera builds no image: it is operator-built and pinned by digest.

**Inputs.** A stage may read several pinned `snapshot` inputs and pinned `artifact` inputs. The runner
composes them into one snapshot before staging, and that snapshot's digest is a pure function of the
files' digests, so submit computes it from the platform's recorded inputs (read only) and writes it,
with the exact total size as `input.max_bytes`, into the contract. The runner still composes under the
approved campaign's existing delegation and fails closed if its digest differs. A `stage` input (another
stage's output in the same campaign) has no digest at submit and is refused; an `artifact` input must
already exist when the spec is submitted.

**Pinned public downloads** (amendment 2026-11-23,
[ADR 0097](decisions/0097-provider-side-fetch-of-pinned-public-artifacts.md)). A stage's
`public_artifact` inputs (model weights, public datasets) are carried into the candidate: the
contract's `egress.fetches` are exactly the pins (url, sha256, `max_bytes`), its
`egress.redirect_hosts` exactly the pins' redirect hosts (with the argv `redirect-hosts:` token), and
the inline manifest holds, beside the image entry, one `file` item per pin with `dest`
`<input>/<name>` (and `cache: true` when the source asks). The in-container runtime downloads and
verifies each file by sha256 and size before the claim, and fails closed on a mismatch; the
control plane never sees the bytes. The reviewable destinations (`capability_network()`) are
therefore exactly the pinned hosts and their redirect hosts on 443. On the local worker the same
files are mounted at `/inputs-public/<input>/<name>`; the launcher maps that prefix to the fetched
files, so the same argv runs on both. The pins' `max_bytes` plus the staged input must fit the
profile's `disk_mb`. Free-form `egress` (an allowlist proxy policy) is still refused, and the
provider refuses an adapted workload whose fetch rows, redirect hosts or manifest items are anything
but the stage's pins (`public_artifact_contract_mismatch`, or the adapted-stage message for the
manifest). Note that a `public_artifact` input carries the licence label `unknown-lineage`
([ADR 0113](decisions/0113-license-placement-guard.md)), so each provider that receives the
stage must be listed in the trust file without an `allowed_license_labels` limit that excludes it.

The receipt's `public_fetch` object for a provider-side stage carries `egress_allowlist`: the
sorted pinned and redirect `hosts`, `declared: true`, `redirect_enforced: true` and
`network_enforced: false`, with a statement that the hosts are declared for review and the
redirect set is enforced by the workload runtime while the provider's network does not restrict
the stage to them (Salad egress is provider-wide, ADR 0036), and `entries`, the per-host
`{host, port, source}` rows of the stage allowlist (ADR 0134). Every `public_fetch` object also
carries `egress_verdict` (`declared_only`, `within_allowlist` or `violation`) and, on a
violation, `refused_transfers` (host and reason only). Per-file `bytes`, `seconds` and
`cache` are `provider_reported` only when control recorded validated fetched rows
(`execution.public_fetch_recorded`, derived from retained output, never a worker claim), and
`declared_only` otherwise. Which redirect hosts were actually contacted is not journaled yet.

On Salad the argv runs with its working directory at the run's work directory; arguments at
`/inputs`, `/inputs-public` and `/outputs` (whole, or after `--name=`) are mapped to the staged
input, the fetched public files and the output directory. Refused with a message that names the limit or the fix: a profile that is not a CPU profile
with a `stage_adapter`; an image outside the allowlist (the message gives the exact
`"sha256:..."` entry and profile to add to `allowed_image_digests`); a duration above 3600 s (or under
60); `env`, `egress`, `scratch_bytes` or `retry.transient_exit_codes`; a `stage` input; inputs larger
than the profile's `disk_mb` or `max_input_bytes`; pinned public files that with the input exceed
`disk_mb`; an output path that is not a flat Salad output
name; outputs above 16 MiB or 16 files; an argv too large for the workload argv (about 4 KiB); and a
stage `max_exposure_cents` below the estimate. When provider trust does not admit the Salad worker
for the stage's data, the 409 says what to merge into the provider trust file (see
[provider trust](provider-trust.md)); the policy itself stays the operator's.
Check the argv before opting in: glued short flags (`-o/outputs/x`) and paths inside config files are
not rewritten, and a hard-coded `/work/...` differs from the Salad work directory. It is verified with
fakes only: no adapted stage has run on Salad.

`registry_auth_file` (`--registry-auth-file`) and `build_repositories` (repeatable
`--build-repository`, `host[/path]` prefixes) let this worker run a *pushing* image build
([ADR 0123](decisions/0123-image-build-stage-and-image-lineage.md)). Both are operator-owned and
absent by default, which refuses every pushing build; setting one without the other is a
configuration error. The auth file is checked, never read, by the worker: it must be a regular
file (no symlink or extra hard link) owned by the worker user with no group or world access, and
is mounted read-only into the build container only after the stage's repository is shown to be
under a registered prefix. Keep it outside the repository; it is the operator's own push
credential. List the same prefixes under `build_repositories` of the matching provider in the
`--campaign-providers` file, which control checks at submit.

The builder image and the stage spec are described in [image build stages](image-build.md).

### Code reaches a provider as an input

The same stage runs on local Docker, a self-hosted server or Salad because its code is an input, not
part of an image ([ADR 0095](decisions/0095-stage-code-as-input-artifacts.md)). Name the snapshot that
holds the code as the stage's `code_input`; the provider receives it through its normal input path (on
Salad, the staged-input ticket and the adapter's launcher above) and runs it in a generic image pinned
by digest. Private code is never baked into an image pushed to a registry, and the receipt records the
code digest and the image digest. Provider trust still decides which providers may receive the input.

### Placement across candidates

Contract: [ADR 0060](decisions/0060-capacity-aware-stage-placement.md). A v2 stage may list
`alternate_providers` after its own provider, in order, so independent stages spread over every
registered provider instead of queueing for one slot:

```json
{"key": "featurize", "provider": "local-docker", "runtime": "docker-cpu-v1",
 "alternate_providers": [
   {"provider": "self-hosted", "runtime": "docker-cpu-v1"},
   {"provider": "salad", "runtime": "salad-workload-v1",
    "provider_config": {"...": "a Salad CPU profile"}, "max_exposure_cents": 20}],
 "max_exposure_cents": 30, "...": "..."}
```

Each candidate is checked at submit and approval like the stage's own provider (registered, worker
enrolled for the project, provider trust for every input, grant lifetime), so the approver
approves every destination. A candidate's `max_exposure_cents` is its cost estimate and hold; it
may not exceed the stage's, which stays the per-run cap.

When the stage needs an attempt the runner takes the first candidate that is still registered,
enrolled, trusted for the stage's inputs, fits the per-run cap, the campaign's remaining budget,
the program budget and the project limits, and has a free slot (`worker_slots` minus live grants
and attempts already placed there). If none fits it waits while a slot or a hold may still free
up, and otherwise fails exactly as a single-provider stage would (budget refusal,
`input_not_admitted`, `provider_unavailable`). It never chooses a provider the spec does not
list. The choice and every skipped candidate's reason are a `campaign.stage_placed` event, the
`placement` field of `tessera_get_campaign` and the attempt's `placement` section in the receipt.
An agent can pin or exclude candidates for the next placement with
`tessera_steer_campaign_placement`; each call replaces the previous one and can only filter the
approved list. A stage without
`alternate_providers` behaves exactly as before.

A stage with a pinned `public_artifact` input hands its executor a pinned-fetch manifest, so
placement also requires the candidate's executor image to declare the runtime capability
`pinned_fetch_manifest`. A provider reports what the image that would run the stage can do
through its optional `runtime_capabilities(stage)` hook; a candidate that does not declare the
capability, or whose capability cannot be determined (no hook, or the hook cannot tell), is
skipped with the closed reason `runtime_image_below_floor` (the detail says `capability unknown`
or what is missing). It is permanent for the attempt: another candidate is taken, and when none
is left the stage fails with `runtime_image_below_floor`, before anything is prepared or held.
This holds for a stage with one provider too. Stages without pinned public inputs are never
asked, so their candidates, events and receipts are unchanged. Worker-grant providers
(`local-docker`, `self-hosted`) declare the capability, because their worker supervisor, not the
stage image, fetches the pins. A Salad candidate answers from its profile's
`image_capabilities` (see the Salad profile example below).

Give each instance its own worker principal (a principal binds one provider in the trust file),
and set `worker_slots` to the pollers that share it, so placement sees real capacity.

### Placement preference by resource class

Contract: [ADR 0070](decisions/0070-salad-first-placement-preference.md). The
provider file may carry `placement_preferences`, so heavy stages try a preferred provider first
without every spec author ordering their candidates:

```json
{"schema": "tessera.campaign-providers.v1",
 "placement_preferences": {"rules": [
   {"name": "heavy",
    "when": {"min_duration_seconds": 1800, "any_tags": ["heavy"]},
    "prefer": ["salad", "local-docker"]}]}}
```

A stage matches a rule when any stated criterion holds: its `max_duration_seconds` or
`scratch_bytes` is at least the bound, or it declares one of the tags in its `placement_tags`
(a spec field, for example `"placement_tags": ["heavy"]` on a memory-heavy stage; stages have no
memory request of their own). The first matching rule moves the listed providers that the stage
already names (its own provider and `alternate_providers`) to the front in the rule's order; the
rest keep the spec order. It only reorders approved candidates: it never adds a provider, skips
a check, or changes a hold, and a stage without alternates is unaffected. Every check above still
runs, so a preferred provider that is busy, untrusted, unenrolled or over budget falls back to the
next. A placement a rule ordered carries `preference` (rule, criteria matched, spec order and the
order used) on its `campaign.stage_placed` event and receipt. Rules are validated strictly at
start (at most 8; each `prefer` 1 to 5 registered provider ids); absent, placement is unchanged.
Salad grants no `scratch_bytes` and refuses a stage that sets it, so a `min_scratch_bytes` rule
suits providers that grant scratch (local or self-hosted workers), not Salad.
`tessera_get_campaign` shows the rules under `placement_preference`, with each matching stage
and whether the rule `applies` to it (a stage must list the preferred provider as an alternate).

### Default placement by stage class

Contract: [ADR 0088](decisions/0088-default-placement-by-stage-class.md). With no operator rule
matching, a stage that lists `alternate_providers` is classed from what its approved spec
declares: `egress_bound` (declares `egress`), `gpu` (tag `gpu`), `heavy` (declared `memory_bytes`
of at least 1.5 GiB, declared `expected_duration_seconds` of at least 1800 (signal
`long_expected_runtime`), or a `heavy` or `training` tag), else `light` (no signal). Heavy tries a
remote CPU candidate first, gpu a remote GPU one, and egress-bound stages (which cannot list a
rented-capacity alternate at submit anyway; see
`test_a_stage_that_declares_egress_and_lists_a_salad_alternate_is_refused_at_submit`) keep remote candidates behind the
others. A light stage is not reordered: it keeps the order its spec wrote, so put the local
provider first in the spec to keep light stages local. What a candidate is comes from the
provider's own report (`PlacementCapability`): a worker-grant provider is local CPU, the Salad
provider is remote and CPU or GPU per the stage's Salad block, and a provider with no report is
`other` and stays where the spec put it. Candidate kinds on the receipt are `remote_cpu`,
`remote_gpu`, `local`, `other` and `unknown`. `memory_bytes` (1 MiB to 2 TiB) and
`expected_duration_seconds` (1 to 604800, not above `max_duration_seconds`) are declared
estimates read only by placement, not requests or limits, and absent from the serialized spec
unless set; `max_duration_seconds` is a cap and never a signal. The heavy and GPU tag lists
(`heavy`, `training`, `gpu`) are fixed generic labels, not operator-configurable (known limit).
The policy only reorders candidates the spec already lists and the human approved: the spec must
list the Salad candidate (ADR 0076's `adapt` form helps), it still needs its own review or
pre-approval, and the checks above still run, so an unregistered, unenrolled, untrusted or
over-cap or over-budget remote candidate is skipped with its reason and the local provider is
tried next. A matching operator rule wins. Turn it off with
`"placement_preferences": {"default_policy": false}`; set `heavy_memory_bytes` and
`long_runtime_seconds` (default 1800) beside it to change the thresholds; `rules` is optional. To
read a decision, open `placement.preference` on the receipt attempt or the `campaign.stage_placed`
event: `source: default_policy`, `class`, `signals`, `kinds`, `spec_order`, `order`, `reordered`,
`chosen` and `fell_back` (each skipped candidate and why). `tessera_get_campaign` shows a
`default_policy` block under `placement_preference` (enabled, thresholds, and per stage the
class, signals, order and whether it applies).

### Image pull-size preflight

A node on interruptible capacity can be rescheduled while a very large image is still being pulled,
and the attempt is lost with it. An operator may state a ceiling on a stage image's compressed pull
size per provider:

- `max_image_pull_bytes`: an integer from 1 up to 2^50, or `null` for no ceiling. It defaults to
  `null` (off) for `local-docker` and every `self_hosted` provider, and to 4 GiB (4294967296) for a
  Salad campaign profile (set it to `null` there to turn the check off).
- `strict_image_pull`: a boolean, `false` by default; it needs a ceiling. When true, an image known
  to be over the ceiling is refused at placement instead of warned about.
- Salad profiles may also record `image_pull_sizes`, a map from an allowed image digest to its
  compressed size in bytes. Other sources register through `campaign_image_pull.register_size_resolver`
  or a provider's optional `image_pull_bytes(stage)` hook. Nothing contacts a registry.

Each placed or started attempt on a provider with a ceiling records an `image_pull_preflight`
section (a `campaign.image_pull_preflight` event, the campaign status stage entry, and each
attempt in the receipt): `status` (`within_limit`, `over_limit`, `refused`, `unknown`), `reason`,
`size_bytes`, `limit_bytes` (integers; `size_bytes` is null when unknown) and `strict`. The reasons
are closed: `image_pull_over_limit` (a warning, or the refusal when strict) and
`image_size_unknown` (recorded only; an unknown size is never refused or warned about). A strict
refusal is the permanent placement reason `image_pull_refused`, checked after the licence and trust
refusals and before any wait; a stage with one provider ends as `image_pull_refused`, unless its
inputs are refused for trust or licence labels, which it then reports instead
(`input_not_admitted`).

Before dispatch, the spec dry run (`POST /projects/{p}/campaign-specs/validate`,
`tessera_validate_campaign_spec`) shows the same check as a plan: each stage entry carries an
`image_pull_preflight` list with one section per candidate provider that has a ceiling, in
candidate order, each naming its `provider_id`. A `refused` entry there means placement would
refuse that candidate. The list is absent when no candidate has a ceiling, and the dry run records
nothing.

### Heavy-stage placement guard

Contract: [ADR 0107](decisions/0107-heavy-stage-placement-guard.md). When a heavy stage is placed
on a local or self-hosted candidate (after it passed every other check) while a Salad profile is
registered for the project, placement records a warning; nothing is recorded when a remote
candidate is chosen.

Heavy signals: ADR 0088's `declared_memory`, `long_expected_runtime`, `tag:heavy` and
`tag:training`, plus `tag:pretraining`, `large_inputs` and `large_outputs`. `large_inputs` is the sum
of the pinned input sizes the store knows for the campaign's organization and project (snapshot
manifest sizes and artifact `size_bytes`); unknown sizes are ignored and stage-to-stage inputs are
not counted. `large_outputs` is the stage's summed declared output `max_bytes` limits, which is a
cap, not an estimate. A GPU tag (`tag:gpu*`) is also a guard signal (amendment 2026-11-23).
`large_public_inputs` (amendment 2026-11-23) is the summed `max_bytes` of the stage's pinned
`public_artifact` files, which the executing provider downloads by digest; such a stage is not
egress-bound. The
threshold is `heavy_memory_bytes` (default 1.5 GiB). Egress-bound stages are never guarded. Placement
ordering uses the same signals, so a stage heavy only by one of them is ordered Salad first.

Reasons, in order: `no_remote_profile` (alone: the project has no usable Salad
profile; recorded as a warning in both modes, because refuse mode never waits for capacity that
cannot exist),
`credential_unavailable`, `no_fitting_profile`, `remote_failed_infra` (a remote candidate's earlier
attempt failed on infrastructure and the retry went local; `detail` names the provider and failure
class), `remote_skipped`,
`operator_override` (an operator rule put local first, the default policy is off, or a steering
pin or exclusion), `not_listed`, `remote_not_tried` (a remote candidate is listed after the chosen
local one with no operator action).

Recording: once per attempt, in the placement transaction, the event
`campaign.heavy_stage_local_fallback` (`stage_key`, `attempt`, `provider_id`, `mode`, `signals`,
`reasons`, `detail` as `[{provider_id, reason}]`) and `placement.heavy_guard` on the
`campaign.stage_placed` payload and the receipt attempt. Stages without alternates are also guarded
before reservation (reason `not_listed`; the receipt `placement` is `{heavy_guard}` from the
event). Set the operator choice beside the other preferences:

```json
"placement_preferences": {"heavy_local_fallback": "refuse"}
```

`warn` (default, also when absent) keeps placement as it was and only records the warning.
`refuse` skips the local candidate with the transient reason `heavy_local_refused` (detail: the
reason codes); with no other candidate the stage waits and the `campaign.stage_waiting_placement`
event carries `heavy_guard {warning: false, refused: true, ...}`. It is placed when a remote
candidate qualifies; a single-provider stage in `refuse` mode waits until the operator switches to
`warn`. Non-heavy stages and projects without a usable Salad profile are unchanged.

No unexplained local fallback (amendment 2026-11-23b): a heavy stage uses a local candidate only
with an explicit reason: any code above except `not_listed` and `remote_not_tried`, or
`remote_candidate_not_added` (the stage lists no remote candidate and its ADR 0141
`heavy_remote_candidate` record says why none was added; `detail` gains `{provider_id, reason,
source: "heavy_remote_candidate"}`). Otherwise, by default, a remote candidate still to be tried
is tried first, and when none is left the stage fails with stop reason
`stage_failed:<key>:heavy_local_unexplained` and one `campaign.heavy_stage_local_refused` event
(the fallback event's fields plus `code` and `unexplained_local`); it never waits, because an
approved spec cannot gain a candidate. This applies in both `heavy_local_fallback` modes and to
stages without alternates. To keep the earlier recorded warning instead:

```json
"placement_preferences": {"heavy_unexplained_local": "warn"}
```

Such warnings carry `unexplained_local: "warn"` on the event, placement and receipt.
`tessera_get_campaign` shows per stage `heavy_guard {mode, reasons, signals, provider_id?, waiting,
recorded_at}` only when an event exists. The [stuck detector](campaign-stuck.md) flags
`heavy_stage_on_local` and `heavy_stage_refused_local`.

### Heavy stages get a Salad candidate at submit

Contract: [ADR 0141](decisions/0141-heavy-stage-remote-candidate.md). With

```json
"placement_preferences": {"heavy_remote_candidate": {"provider": "salad", "max_exposure_cents": 40}}
```

(optional `profile` names one Salad profile), submit, the approved-submission create and the
validate dry run give every heavy stage (the guard's signals and `heavy_memory_bytes` threshold
above) that lists no remote candidate an adapted Salad CPU candidate (the stage adapter above, on
an adapter-enabled CPU profile registered for the project) as its **first alternate**, before the
spec is digested, so a person or delegation approves it and placement then tries it first
(heavy-first ordering). The stage gets a `heavy_remote_candidate` record in the stored spec and
on its receipt stage entry: `{added: true, reason: "operator_heavy_remote_candidate", provider,
profile, max_exposure_cents, signals}`, or `added: false` with why none was added:
`egress_bound` (free-form `egress` only: a stage whose only reach is pinned `public_artifact`
downloads gets the candidate, with the pins as its only fetches; amendment 2026-11-23),
`no_gpu_profile` (a GPU stage and no GPU profile opened as below),
`gpu_profile_unfit` (the adapter refused the GPU profile; `detail` says why),
`forbidden_by_campaign`, `provider_unregistered`, `scratch_requested`, `runtime_not_adaptable`,
`input_not_pinned` (an input is neither pinned nor another stage's output in this campaign),
`alternates_full`,
`no_fitting_profile`, `profile_unfit` (the adapter refused; `detail` says why),
`over_operator_cap`, `over_stage_cap`, `over_campaign_budget`, `program_budget`,
`candidate_refused` (a submit check such as trust refused it). The candidate's hold is its
full-duration estimate and never exceeds the setting, the stage's `max_exposure_cents`, the
campaign cap or what a program budget admits; no cap is raised and the submission is never
refused for it. A submitted record is discarded and recomputed. Light stages, stages that already
list a remote candidate and v1 specs are unchanged. Without the setting nothing is added, and a
heavy local placement is an unexplained ADR 0107 fallback (`not_listed`): refused by default (see
above). A record with `added: false` explains the fallback (`remote_candidate_not_added`).

A heavy stage tagged `gpu` gets a **GPU** candidate only, never a CPU one (ADR 0141 amendment
2026-11-23). The operator opens a GPU profile to it by giving the profile a `stage_adapter`
section that names the operator's price for its GPU class:

```json
"stage_adapter": {"gpu_hour_usd": "0.30"}
```

`gpu_hour_usd` (USD per hour, positive) is required on a GPU profile's `stage_adapter` and refused
on a CPU profile's. The candidate is the stage adapter's derivation on the first such GPU profile
registered for the project (or the setting's optional `gpu_profile`), with `gpu` set, and its
`max_exposure_cents` is the full duration at `gpu_hour_usd`, rounded up, under the same operator,
stage, campaign and program caps as a CPU candidate. That price is an estimate input set by the
operator, never invoice or cost proof: Salad admission still reads the published GPU rate and
refuses that attempt (`budget_insufficient`) when its run would exceed the candidate's cap at it,
so a price set too low fails the Salad attempt rather than spending past the cap. Set
`gpu_hour_usd` at or above the class's published rate. A GPU candidate skipped at placement
(for example an expired credential) falls back to the local provider with its ADR 0107 reason. An explicit `{"adapt": {"profile": ...}}`
request still takes a CPU-only profile only. CPU heavy stages are unchanged and never get a GPU
profile.

A heavy stage that reads an earlier stage's output (`kind: stage`, e.g. a train stage after a
prep stage) gets the candidate too: the DAG edge is in the approved spec, and the runner binds
the upstream digest at stage start (ADR 0140), re-checking data class and licence for Salad; the
Salad driver then pins the bound snapshot digest exactly as for a pre-pinned input. The contract's
input cap is the upstream outputs' declared `max_bytes` (an unbounded output, or a bound over the
profile's `disk_mb` or `stage_adapter.max_input_bytes`, records `profile_unfit`). The exposure
estimate is the full-duration cost as for any adapted stage; input size does not change it. If
the upstream has not succeeded and settled at bind time, the stage fails with
`late_bind_refused:<code>` and nothing reaches Salad.

## The provider file

`tessera serve --campaign-providers FILE` takes one closed, owner-only (0600), secret-free
JSON file. It names provider instances and, under `salad_profiles`, the Salad operator
profiles (a `tessera.salad.campaign-profiles.v1` document, documented in
[campaigns](campaigns.md#salad-gpu-stages)). `--salad-campaign-profiles FILE` remains as an
alias for the profiles alone. Credentials are never in the file: a profile holds a
credential *reference*, and a worker credential is a file path. The file is read at start.

Provider ids and worker principal ids are placeholders for enrolled principals; the
deployment's real file is not kept in the repository.

### Registering providers and profiles

Contract: [ADR 0081](decisions/0081-validated-provider-registration.md). Instead of editing the file by
hand, add, update and list its entries through one validated operation:

```
tessera providers list   --file /path/to/campaign-providers.json
tessera providers add    --file /path/to/campaign-providers.json --section salad-profile --entry-file cpu.json
tessera providers update --file /path/to/campaign-providers.json --section salad-profile --key cpu-2c-4g \
    --entry-json '{"max_run_cents": 40}' --expected-digest <digest from list> [--dry-run] \
    [--provider-trust /path/to/provider-trust.json]
tessera providers audit  --file /path/to/campaign-providers.json
```

The same operation is `GET` and `POST /admin/campaign-providers` (writes are human-only and
idempotent) and, when the operator enables `provider_registration_tools`, the MCP tools
`tessera_list_providers` and `tessera_register_provider`. Sections are `salad-profile`, `self-hosted`,
`local-docker` and `plugin` (a vendor plugin may add its own through `config_sections()`). `tessera_deploy_report`
(same flag) is the read-only deploy report of the control plane ([deploy check](deploy-check.md)).

Who may write through the API depends on the section. A `salad-profile` belongs to its organization:
a human of that organization may add one for its own organization and project and a worker principal
of that project, and change only profiles it owns (another organization's name is "not
available", though that tells a caller the name is taken).
`local-docker`, `self-hosted`, `plugin` and plugin sections are deployment-wide, so only the
deployment's root owner may change them through the API; the offline command is for whoever owns the
file. A `self-hosted` instance registers and is trusted under its own `id`, which the list and the trust
readout use. `GET` hides worker principal ids on deployment-wide entries from other callers.

A CPU and a GPU Salad profile (example values; the credential is a reference, never a value):

```json
{"name": "cpu-2c-4g", "org_id": "org-example", "project_id": "project-example",
 "worker_principal_id": "worker-example", "salad_organization": "example-org",
 "salad_project": "example-project", "country": "US",
 "cpu_rate": {"vcpu_hour_usd": "0.005", "memory_gb_hour_usd": "0.001"},
 "cpu_millicores": 2000, "memory_mb": 4096, "disk_mb": 8192, "route_max_seconds": 4200,
 "capability_environment_id": "salad-cpu-campaign",
 "artifact_destination": "artifact-ref:quarantine", "checkpoint_destination": "checkpoint-ref:external",
 "monitor_profile": "<64 hex>", "watchdog_profile": "<64 hex>",
 "custody_host": "custody.example-tailnet.ts.net",
 "credential_reference": "credential-ref:salad-management", "credential_version": 1,
 "allowed_image_digests": ["sha256:<64 hex>"], "max_run_cents": 50, "max_task_cents": 50}
```

The GPU profile is the same with `"gpu_class": "<GPU class UUID>"` instead of `cpu_rate`.

Optional `image_capabilities` maps an allowlisted image digest to the runtime capabilities the
operator has verified that image has, e.g. `"image_capabilities": {"sha256:<64 hex>":
["pinned_fetch_manifest"]}` for a runtime image built with pinned-fetch manifest support (the
`file` items a stage's `public_artifact` pins become). The only name so far is
`pinned_fetch_manifest`; a key that is not in `allowed_image_digests`, or an unknown name, is
refused. It defaults to empty, so existing profiles keep loading and stages without pinned
public inputs run as before, but a pinned-fetch stage is refused on an image with no entry
(`runtime_image_below_floor`, capability unknown) until the operator declares it. Declare it only
for an image rebuilt from a runtime that has the support; an older image is refused at placement
rather than handed a manifest it cannot process.

`MAX_RUN_CENTS = 5000` and `MAX_TASK_CENTS = 10000` in `salad_campaign_profiles.py` are hard
schema ceilings only. The 50-cent per-run cap used for live GPU runs is operator configuration in
the profile (`max_run_cents` of 50 or less); no other code enforces it.

Refused before anything is written: an unknown section, provider or plugin (or one that is not
installed); a field the provider's model does not define; an image not given as a `sha256:` digest;
a cost ceiling or resource outside the provider's limits; a secret-like field name or a
credential-shaped or high-entropy value; a duplicate name on add, a missing one on update; a changed
key field; a document that is invalid as a whole; a file changed since `--expected-digest`; and, in
the API, a Salad profile whose project is not in the caller's organization or whose worker is not a
registered worker of that project, and a `local-docker` or `self-hosted` worker principal the control
database does not know (a plugin section has no such check). The write is atomic and
owner-only, and appends a hash-chained record to `<file>.audit.jsonl`. A change applies at the next
`serve` start. A result reports which data classes the current provider trust setting admits for the
entry's worker and what to merge into its trust entry; provider trust stays operator-edited
([provider trust](provider-trust.md)). The service user must be able to write the file and its
directory for API writes (otherwise they are refused; the offline command then edits the file).
Profiles given through `--salad-campaign-profiles` are not editable here.

### Listing the eligible Salad GPU classes

Pick a class from the operator's own profiles instead of calling the provider by hand:

```
tessera providers gpu-classes --file /path/to/campaign-providers.json \
    [--state-dir /path/to/state] [--days 14] [--format json|text]
```

The same read is `GET /admin/campaign-providers/salad-gpu-classes?days=N` (a person, or an agent
with `provider_trust:read`, limited to its own organization's profiles) and the MCP tool
`tessera_list_salad_gpu_classes` (registered with the `provider_trust:read` tools). Eligible means
a GPU profile in the file's `salad_profiles` names the class; the provider's class catalog is read
live at admission and is not copied locally. Per class the listing gives the id, the profiles that
name it, the largest `max_run_cents`, the hourly rate the latest guardian record in the window carried (the latest recorded, not an average), and over
a window of 1 to 90 days (default 14) the attempts with a guardian record, the pre-start re-binds
and the attempts flagged `allocation_uncertain`. Names are never recorded locally and are `null`; a
rate or count no receipt recorded is `null` with a `not_recorded` status, never zero. The receipt
counts are read through a read-only database connection (without `--state-dir` the CLI reports
`not_recorded`). At most the newest 2000 attempts in the window are read; when that cap is reached each class's `allocation_instability` carries `truncated: true` and a note says the counts may be too low (the field is absent otherwise). A providers file or database that cannot be read fails closed with no listing. The
output names profiles and class ids only, never credential references or custody hosts. It changes
nothing and does not rank classes.

## Self-hosted provider

1. Enroll a worker principal for the project that will run on the server (the same
   enrollment as any development worker; a human does it) and keep its credential in a 0600
   file on that server. `scripts/executor_enrollment.py enroll --provider self-hosted`
   (or `--provider local-docker` for the control host's own poller) creates one stable,
   renewable principal; see [provider trust](provider-trust.md#stable-executor-route-setups-that-mint-principals).
2. List the instance in the provider file under the id `self-hosted` with that worker
   principal and the runtime `docker-cpu-v1`. Optionally set `worker_slots` (1 to 8,
   default 1) to the number of pollers that share the principal: the runner issues a grant,
   and starts its lifetime, only while the worker holds fewer live grants than that (also
   available on `local_docker`; [ADR 0048](decisions/0048-grant-lifetime-and-slot-release.md)).
3. Decide, in the [provider trust](provider-trust.md) file, which data classes that
   principal may receive. The default is public data only, so a private input is refused
   with a 409 naming the provider and principal until the file says otherwise.
4. Run `tessera-worker development-poll` under a supervisor; example systemd units ship in
   [`deploy/systemd`](../deploy/systemd/README.md):

```sh
tessera-worker development-poll --config /etc/tessera/poll.json --health-file /run/tessera/poll.health
```

The poller config (`tessera.worker.development-poll.v1`) is a closed JSON file of paths and
settings, never a secret:

```json
{
  "schema": "tessera.worker.development-poll.v1",
  "control_url": "https://control.example",
  "credential_file": "/etc/tessera/worker.token",
  "workspace_root": "/var/lib/tessera/poll",
  "development_output_root": "/var/lib/tessera/out",
  "allowed_images": ["python@sha256:<digest>"],
  "poll_seconds": 2,
  "memory_bytes": 1073741824,
  "scratch_bytes": 67108864,
  "max_scratch_bytes": 2147483648,
  "container_user": "1000:1000"
}
```

`control_timeout_seconds` (optional, 1 to 20, default 15; also `--control-timeout-seconds` on
`development-poll` and `development-run`) is how long one HTTP request to control may take. A
slower answer is a transient failure that is retried (a running container is not stopped for it,
see [ADR 0054](decisions/0054-durable-observation-delivery.md)); raise it for a control plane that
answers slowly under load. The ceiling is the 30 s worker lease minus the 10 s renewal interval, so
one unanswered request cannot lapse the lease (the worker refuses a larger value, and a slower
`renew_after_seconds` lowers the ceiling accordingly). Each observation report is also capped by
the lease time left minus one renewal interval, never below 1 s.

A grant the poller is recovering (an interrupted run, or one whose runtime descriptor is on
disk) that control permanently refuses (an HTTP 4xx other than 401/403/408/425/429, such as a
409 for a grant control already reconciled) is retried after `poll_seconds` doubling up to
15 minutes, with jitter, not every cycle. After 5 such refusals in a row an admitted grant gets a
local `abandoned.json` (reason `control_refused_recovery`, the HTTP status and timestamps) and is
no longer recovered; this is not a result and records no outcome. Delete the marker to resume
recovery. Transient failures (no status, 5xx, 408/425/429) are retried without a bound. The
poller's exit summary counts `refused` and `abandoned`.

`memory_bytes` and `scratch_bytes` are optional, operator-owned limits applied to every
container this poller runs (memory, with swap equal to it, and the `/work` tmpfs). A plan
cannot raise them. Unset keeps the defaults of 256 MiB and 16 MiB; accepted ranges are
32 MiB to 64 GiB and 1 MiB to 64 GiB, and a value outside them refuses the config (exit 2).
`max_scratch_bytes` (also `--max-scratch-bytes`) is the ceiling on the scratch a stage may
*request* with its spec's `scratch_bytes`; unset it is 64 MiB. The `/work` tmpfs is
memory-backed, so a scratch above 64 MiB must fit `memory_bytes` with 64 MiB to spare, and
a memory above 1 GiB or a ceiling above 64 MiB is checked against the host's memory (at most
75% and 50%) before the poller runs a grant. A request above the ceiling is refused at
admission, never mid-run. Declare the same ceiling to control as `max_scratch_bytes` on the
provider's entry in the provider file (`local_docker`, or a `self_hosted` instance), so
submit and approval refuse an over-large request with `scratch_exceeds_operator_ceiling`
([ADR 0059](decisions/0059-operator-scratch-ceiling.md)); keep the two equal: if control's
is higher the worker still refuses the run with its own ceiling, and if lower control refuses
what the worker would grant. A ceiling above what `memory_bytes` can back (the 256 MiB
default backs 192 MiB) is logged as a warning at startup, and a request over that is refused at
run. A default `scratch_bytes` above 64 MiB must fit `memory_bytes`
(256 MiB when unset) or the poller config is refused at load. Size them to the host: one poller
runs one container at a time.

`public_root` (also `--public-root`) and the optional `public_cache_dir` (`--public-cache-dir`)
enable pinned public inputs ([ADR 0097](decisions/0097-provider-side-fetch-of-pinned-public-artifacts.md))
on this worker: the supervisor, not the container, downloads each `public_artifact` file, checks its
sha256, and mounts it read-only at `/inputs-public/<input>` in a container that stays network
none. Both are operator-owned directories, created `0700` if missing and refused when a symlink,
owned by another user or accessible to group or world; the cache is used only for inputs that set
`cache`. Without `public_root` a plan carrying a public fetch is refused with a message naming the
flag. The fetch ignores proxy environment variables (`HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, ...)
and connects directly; to use a proxy, set `public_fetch_proxy` (`--public-fetch-proxy`) to
`http://host:port` with a public host name (no credentials or path). It is used only when that host
is one of the stage's allowed hosts (its pinned hosts plus redirect hosts); otherwise the fetch fails
closed with `proxy_not_allowed`, and a malformed value is refused at startup (`proxy_invalid`). The local fallback currently supports stages that also have a snapshot input (mixed stages),
because an execution grant binds a snapshot; a stage whose only inputs are `public_artifact` fails
closed on a worker-grant provider with `public_only_stage_needs_a_snapshot_input` and runs
on Salad.

On Salad, `cache` is honoured by the in-container runtime only when the provider environment mounts a
persistent directory and names it with `TESSERA_FETCH_CACHE_DIR` (and a store quota with
`TESSERA_FETCH_CACHE_MAX_BYTES`) in the reviewed contract `env` or the image
([ADR 0116](decisions/0116-provider-fetch-cache.md)). Tessera creates no storage; without it the flag is
a no-op recorded as `unavailable`. Entries are keyed by sha256 and re-hashed on every reuse, and cached
bytes never go to the control plane.

`docker_network_pool` (optional, with `docker_network_prefix_length`, default 24; also
`--docker-network-pool` / `--docker-network-prefix-length` on `development-poll` and
`development-run`) gives the Docker networks Tessera creates their own address block.
Use it when other software has created so many Docker networks that Docker's default
address pools are exhausted and a run fails at network create with "all predefined
address pools have been fully subnetted" (only runs with an egress policy create a network;
`--network none` runs never do). Set it to a private IPv4 CIDR that nothing else uses, for
example `"docker_network_pool": "10.213.0.0/16"`. Each run's network is then created with an
explicit `--subnet` and `--gateway` from that block, skipping every subnet used by an
existing Docker network or a host route (read from `/proc/net/route` on Linux; on Docker
Desktop only the daemon's own overlap check applies). Concurrent runs are safe because
Docker refuses an overlapping subnet and the allocator then picks the next free one. The
prefix length must lie between the pool's own prefix and `/29`, giving at most 65536
networks; a full pool fails the run with a clear "no free subnet" error. Every network is
labelled `tessera.operation=<operation id>`, removed when its run ends, and the poller
removes leftover Tessera-labelled networks at startup, only those with no attached
container (in any state) and at least ten minutes old; it never deletes other software's
networks and you never need to restart the Docker daemon. Unset keeps Docker's default
pools exactly as before. In every case, a failed network create is recorded as a
`launch_failed_network_create` outcome carrying Docker's error: no container was created
or started, so nothing ran and nothing was spent.

`container_user` is an optional, operator-owned numeric `uid:gid` the stage containers run
as (`--user`); also `--container-user` on `development-poll` and `development-run`. A plan
or spec cannot set it. The poller's 0700 output workspace is owned by the poller's own uid,
and a stage can only write its declared outputs if the container runs as that uid (or the
workspace's owner). Default: on native rootful Linux, the poller's own `uid:gid`; on other
platforms (Docker Desktop maps bind-mount ownership) it stays `65532:65532` as before.
The same fallback applies when the poller's uid or gid is 0, including a non-root uid with
gid 0, because a 0 id is never chosen implicitly. Run the poller as a non-root user with a
non-zero gid. If it must run as root, its workspace is root-owned 0700 and, with all
capabilities dropped, only `"container_user": "0:0"` plus `allow_root_container_user` can
write it (a different non-root value fails at run time); a non-root uid with gid 0 needs an
explicit `container_user` set to its uid and a non-zero gid. Names are
refused, ids are bounded to 0-4294967294, and uid or gid 0 is refused unless
`"allow_root_container_user": true` (`--allow-root-container-user`) is also set. The
workspace stays 0700 and every private-custody check is unchanged; nothing is made
world-writable. The effective user is bound into the supervisor's durable fence digest beside the
memory and scratch limits, so a restart with a different user refuses the recorded operation
instead of reusing it. The journal stores the digest, not the user itself, so the user cannot
be read back from it; read it from the poller config.

**Rootless Docker, user-namespace remapping and Docker Desktop for Linux.** "Native Linux"
here means a rootful daemon without `userns-remap`. With rootless Docker or `userns-remap`,
container uid N maps to a subordinate host uid, and Docker Desktop for Linux uses a VM with
mapped file sharing, so the poller's own uid:gid does not own the workspace as seen from the
container and the default can still fail to write outputs. The poller does not detect this.
Set `container_user` to the id that the daemon's mapping makes own the bind mount (for
rootless Docker, usually `0:0` with `allow_root_container_user`, which maps to the poller's
host uid; for `userns-remap`, the mapped id from `/etc/subuid`), and verify with a one-stage
run. The opt-in Docker test skips on such daemons.

**Shared host uid.** By default the stage container now runs with the poller's host uid,
which also owns the poller's 0600 credential file and state directory. Only `/inputs`
(read-only) and `/outputs` are mounted, and a stage that changes modes inside its outputs
fails closed in the custody checks, but a container escape would land as the poller and
per-user kernel limits are shared with it. Run the poller as a dedicated service user that
owns nothing else. `Dockerfile.guard` asserts uid 65532 and is not a development-poller
image.

**Image requirement.** The container no longer runs as the image's own user (65532 in this
repository's example and workload images), so every file the stage needs from the image
(interpreter, libraries, code, data) must be world-readable (and world-executable for
programs and directories), or owned by the configured user. The images built from this
repository already `chmod a+rX`/`0555` their files. An image whose files are readable
only by uid 65532 (for example mode 0700 or 0750 owned by it) fails under another user;
rebuild it with `chmod -R a+rX` on the application tree, or set `container_user` to the
owner. The stage's `/work` tmpfs is mode 1777 and is writable by any user.

The credential file must be a private regular file (0600, owned by the service user), and
the config file must not be a symlink or group- or world-writable. The individual flags
(`--control-url`, `--credential-file`, and so on) still work for a one-off run.

One Salad executor serves every profile its config names: `"profile": "gpu-main"` as before,
or `"profiles": ["gpu-main", "cpu-main"]` (exactly one of the two; all for the executor's worker
principal and custody host). Work for a profile it does not name is refused (`refused_profile`)
and waits at `awaiting_lease` until the grant expires, so list every profile that stages may use,
including a CPU profile that heavy stages are placed on ([ADR 0070](decisions/0070-salad-first-placement-preference.md)).

The executor's `custody` block names `host` (the admin endpoint it connects to and pins by
`certificate_sha256`) and, optionally, `workload_host`: the name workloads are told to reach
custody by, when that differs from `host`. Every profile's `custody_host` must equal
`workload_host` (or `host` when it is absent), or the executor refuses to start. The executor
never connects to `workload_host`, and nothing proves it is the same service; the custody
service itself still refuses a request whose destination is not its own.

## Service loop contract

`development-poll` and the Salad executor (`python -m
tessera_control.campaign_salad_executor`) share one loop, the contract in ADR 0038's
amendment:

- Runs forever by default. `--once` runs one cycle and `--max-cycles N` stops after N
  (tests and one-off runs); `max_grants` also ends it once that many grants have run.
- SIGTERM or SIGINT stops new work, finishes the current step and exits 0.
- `--health-file PATH` is replaced atomically (0600) after each loop with the pid, start
  time, last loop, loop count, consecutive errors, last error class and state. No port is
  opened.
- A running operation's control lease lasts 30 seconds and only the worker's renewal extends
  it. The worker renews as soon as it starts driving an operation (a recovered lease may have
  only seconds left), then every 10 seconds of elapsed time (and at least every 15 ticks), and
  never sleeps past the next renewal, whatever `poll_seconds` is. A renewal failure cancels
  the operation.
- Logs are bounded JSON lines with error classes only. A cycle that raises is counted and
  the loop continues.
- Exit 2 means the configuration or credential was refused: do not restart. Restart is
  otherwise idempotent (per-grant directories; a grant with a recovered runtime descriptor
  is reconciled, never re-leased).

## Salad provider

A Salad stage names an operator profile that is either a GPU profile (a `gpu_class`) or a CPU-only
profile (`cpu_rate`, no GPU); a stage that declares `salad.gpu: false` runs only on the latter. See
[campaigns](campaigns.md#salad-gpu-stages) and [ADR 0062](decisions/0062-salad-cpu-only-profiles.md).

### Reading a campaign Salad route

`GET /remote-executions/{id}` on the route of a campaign Salad attempt reports the path that
route really takes: the Salad executor dispatches it through the guardian, not through control's
built-in proxy (which stays disabled). The response carries `control_surface:
campaign_executor_guardian` and a `live_path` object instead of a top-level `go_no_go` and
`proxy_safety`; the built-in proxy's state is still shown, labelled, under `builtin_proxy`. See
[remote agent execution](remote-agent-execution.md#reading-a-route) for the fields.

It is a reading, not a grant. `readiness` (`ready`, `degraded`, `not_ready`, `finished`) is
derived from the executor's registered worker (lifecycle state, credential status, project and a heartbeat within the worker registry's bound; see the reasons in the linked section),
the attempt's execution lease, and what the guardian has recorded (evidence, live receipt, final
state). Nothing is enabled, authorized or renewed by reading it, and `conformance` is always
`not_attested_by_this_view`: the view does not claim the guardian conforms. There is no MCP tool
that reads a route today; use the API.

### Checking a CPU profile against Salad (opt-in, bounded)

No offline test can show that Salad accepts a container group with no GPU class, or what a short
run costs at the profile's rates. `scripts/salad_cpu_live_check.py` checks both, and is **not**
part of default pytest or CI: it is a dry run unless you pass `--run` and an exact `--confirm`.

```
scripts/salad_cpu_live_check.py check --profiles FILE --profile NAME --contract FILE \
    --image IMAGE@sha256:DIGEST [--max-cents N]                     # plan only, no network
scripts/salad_cpu_live_check.py check ... --run --confirm spend-up-to-Nc \
    (--credential-file FILE | --credential-dir DIR --credential-version N) \
    --receipt-directory DIR                                         # spends, up to the ceiling
scripts/salad_cpu_live_check.py billing --receipt FILE --portal-cents X
```

- **Dry run (default).** It reads your profile file and workload contract, derives the exact
  route, and prints the shape, the hourly rate, the ceiling, the running seconds that ceiling buys
  and the `--confirm` token. It makes no network call and reads no credential.
- **Spend bound.** The ceiling is `--max-cents` (default 5) and never more than **9 cents**, a
  constant in the script, not a setting. Billing continues until the instance is actually down,
  so 30 s are reserved out of the ceiling for the stop to take effect: the running time is cut at
  the seconds the ceiling buys minus 30 (at most 300 s), and the estimate counts through the
  observed stop (through the whole 90 s settle wait if a stop is never seen, which is then
  reported as outside the bound). A rate that leaves under 30 s of running, or a workload that
  declares longer than the bound, is refused. The group is always stopped and deleted, on every
  path including an error; if that fails the receipt says so and names the group to stop by hand.
  An unexpected error does not lose the receipt: it is recorded as `aborted` (its type only, never
  its message) beside the group name and the cleanup outcome.
- **What a run does.** It makes only the vetted Salad transport's calls: read the quota, create
  one stopped group (free), read it until it settles (stopped, no change in flight, no instance:
  at most 60 s, unbilled, [ADR 0071](decisions/0071-salad-start-settles-the-group.md)) and compare
  its shape (**no GPU class**, the shape check), start it, poll until it ends or the bound, stop,
  delete. A group that never settles is not started, and the receipt records the observed state,
  the change flag and the instance-count keys (no secret). Salad publishes no CPU availability read,
  so the check cannot show capacity.
- **Inputs.** The profile must be a CPU profile; the image must be allowlisted by it; the contract
  is your reviewed `tessera.salad.workload.v1` JSON for a short CPU job (a stub that exits in
  seconds is enough). The credential is either an owner-only (0600) absolute file holding only the
  Salad management token, or the operator's credential directory read by reference
  (`--credential-dir`, `--credential-version`, `--credential-ref`, default
  `credential-ref:salad-management`), like the rest of the Salad stack; it is held in a wiped
  buffer and never printed or stored.
- **Run it once before relying on Salad CPU.** The offline tests use a fake group, so what Salad
  reports for a real CPU group's instance counts is unconfirmed: a group that omits them is
  started but never reported as a confirmed stop ([ADR 0071](decisions/0071-salad-start-settles-the-group.md)).
  One bounded `--run` shows the real shape in the receipt's `settle` block and the `start` step's
  `instance_counts_complete`. The credential is wiped on every path, including a failure before
  the journey starts.
- **Billing is a manual comparison.** The receipt records the running seconds and the estimate at
  your rates (`estimated_cents`). Copy the figure Salad's own usage page shows for that group and
  run `billing --receipt FILE --portal-cents X`: it agrees if the two are within the larger of one
  cent or half the estimate (usage figures are coarse), otherwise correct the rates in the profile.
  The tool never calls a billing endpoint and never reports an estimate as an invoice.

Until a person has run this once and recorded an agreeing receipt for a provider account, a CPU
profile's group shape and billing are unverified, as ADR 0062 says.

The Salad provider wraps the Salad stage driver; its settlement bases are unchanged
(`unclaimed_zero`, `human_reconcile`, `human_charge`, `guardian_bounded_hold`; the last is
provisional and confirmed by the first human charge). See
[campaigns](campaigns.md#salad-gpu-stages) for what the runner automates and where a human
is still needed, and for the executor, which runs under the same loop contract.

A Salad stage may take stage-output or artifact inputs, composed by the core into one
snapshot, when the provider trust setting approves `salad` for `private_derived`. Then the
route's `data_policy` is `provider_trust_policy_v1` rather than `public_released_only`,
and the executor re-derives the same value from the stage. Two refusals apply even then:

- A stage with any non-public input may not declare workload fetch or redirect hosts,
  because Salad's egress is provider-wide. This is decided statically from the spec: stage
  and artifact inputs always count as non-public, and so does a `send_classes` beyond
  `public`.
- Provider trust is checked again at ticket issue and at each pull, so narrowing the setting
  stops later pulls. The stage's own `send_classes` and its licence-label rule are re-checked
  there too (found through the attempt's grant, so the first ticket is already narrowed), and
  a label added to the input after release stops later pulls.

## Termination after a Salad reallocation (ADR 0149)

When Salad moves a running instance (the route records `reallocated`), the guardian stops the
group but never confirms that stop itself. The executor then reads the group through the route
(one `observe` every `termination_poll_seconds`, default 15) until the provider's own read shows
the group stopped with no instance, for at most `termination_confirm_seconds` (executor config,
default 600, at most 3600, 0 disables the wait). Only that recorded provider read confirms; the
guardian's status or a worker report never does.

- Confirmed: the executor reports the exit once, control checks the route itself, and the attempt
  ends `failed` (outputs are never accepted after a reallocation) and settles through the usual
  path (a recorded charge or a human reconcile). The campaign's input release is revoked when the
  campaign finishes, as usual.
- Not confirmed by the bound: the attempt is `unknown` with waiting reason
  `termination_unconfirmed` (or `termination_unconfirmed_new_allocation` when another allocation
  was still present), one `campaign.salad_termination_unconfirmed` event is recorded, the campaign
  blocks, and the hold and the input release stay until a person reconciles the grant.

Stage status shows the code; the campaign receipt carries `salad_termination` for such attempts.

## Guardian read for Salad settlement (ADR 0041)

`tessera serve --salad-guardian-read FILE` (or `"salad_guardian_read": "/abs/path"` in the
`--campaign-providers` file; not both) names an owner-only (0600) JSON file,
`{"schema": "tessera.salad.guardian-read.v1", "host", "port": 443, "token_file", "ca_file",
"certificate_sha256"}`, for the guardian's pinned status endpoint. Control reads each Salad
frame's guardian record itself and settles at the guardian-bounded hold only on a `stopped`
verdict it read. Without the file, a Salad stage never settles on the bounded hold: the hold stays
until a human records the charge. The cost estimate still comes from the executor's recorded final.

Frames already in flight across an upgrade keep the verdict digest the earlier executor-final code
recorded. A later `stopped` read still settles them, but that frame's verdict digest then differs
from the `record_digest` on the receipt's `guardian_record`; the receipt still names the read that
was made. New frames take the read's digest as their verdict digest.

## Optional hook: image pull size

A provider may implement `image_pull_size_bytes(stage) -> int | None`, the total compressed layer size
of the stage image from its registry manifest. It is read once when an attempt is granted, outside the
database transaction, and waited on for at most 10 seconds; a read still running then is abandoned
(a reader that calls a registry should still set its own network timeout). Its absence, a timeout, a
`None` result, a non-positive or implausible value, or any exception is recorded on the attempt
receipt as a null size with a closed reason (see
[campaigns.md](campaigns.md#runtime-image-provenance)). The result is never used for admission.
