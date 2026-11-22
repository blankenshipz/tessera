# ADR 0076: a Salad stage adapter for local-docker CPU stages, and a CPU runtime variant

Status: accepted and implemented. No schema change. Builds on ADR 0036 (generic Salad workload
contract, `gpu_runtime_v1`), 0040 (providers and data-class routing), 0047 (agent-routed data
destinations), 0060 (placement candidates), 0062 (CPU-only Salad profiles) and 0066 (pre-approval
of Salad alternates). The runner core, the guardian and the executor are unchanged.

## Context

ADR 0060 lets a stage list `alternate_providers`, so a campaign can try Salad and fall back to
the local host. A Salad candidate, however, replaces only the stage's runtime, `provider_config` and
cost estimate. The stage's own image and argv stay, and the Salad provider requires the stage argv
to equal `[/tessera/bin/run, *workload.argv_template]`, with a hand-written `salad` block (profile,
worker, a `tessera.salad.workload.v1` contract, a program template). A local-docker CPU stage
(image plus argv) therefore could not list Salad as an alternate without a Salad-form duplicate of
the stage.

Separately, the only way `gpu_runtime_v1` gets Python dependencies is wheels fetched by URL
(`egress.fetches`), and a stage with any non-public input may not declare fetches. Input-sourced
manifest items are `entry` or `file` only. The recipe's base is a CUDA image. A CPU stage with
private inputs and non-trivial dependencies (numpy, scipy, scikit-learn, torch-cpu, ...) had
nothing to run on.

## Decision

### 1. A CPU runtime variant, with hash-pinned wheels baked into an operator-pinned image

`workloads/gpu_runtime_v1/Dockerfile.cpu` builds the same runtime (`/tessera/bin/run`,
`runtime.py`, UID 65532, fixed entrypoint) on a non-CUDA base the operator supplies by digest
(`CPU_RUNTIME_BASE`, a Python 3.10+ image; there is no default). It adds no apt packages. Optional
dependencies come from a `requirements.lock` the operator supplies, installed with
`pip install --require-hashes --only-binary=:all:` (every wheel pinned by sha256, no source builds,
no dependency resolution). Network access happens at **image build time** only, to the index the
operator chose. At run time the image needs, and gains, no egress.

The built image digest is the trust anchor: the operator records it in the profile's
`allowed_image_digests`, so the wheels are exactly the ones in the reviewed image and a campaign
cannot choose another. The image name and digest of the base, the lock file's sha256 and the
recipe's commit are recorded with it (documented in the workload README).

Input-sourced wheels (a `wheel` item with `source: input` and a sha256) were considered and not
built. They would add a manifest item shape, a second install path in the runtime and a matching
rule in the contract, to deliver what an image digest already pins once and reviews once, and the
runtime would still install into a read-only tree. They can be added later behind this ADR's image
requirement without changing it.

### 2. An image-resident stage entry in the manifest

`gpu_runtime_v1` manifests gain one item shape,

    {"kind":"entry","source":"image","path":"stage_argv.py","argv":[...]}

`path` must be a name the runtime ships under `/tessera/lib/entries/` (only `stage_argv.py`); the
item has exactly these four keys, counts as the manifest's single entry, is not a fetch (so it is
absent from `egress.fetches` and from the capability network), and needs no declared input. The
runtime runs `python -B /tessera/lib/entries/stage_argv.py WORK OUT -- ARGV...`. The launcher
runs the argv as a child process with `cwd` at the work directory, no stdin, and with path
arguments that start at `/inputs` or `/outputs` (as a whole argument, or after `--name=`) rewritten
to the staged input tree and the output directory: the local-docker mount points, so the same argv
works in both places. It exits with the child's exit code. Nothing else is rewritten and it adds no
environment. The argv is part of the manifest, hence of `MANIFEST_SHA256` in the reviewed argv, hence
of the contract and route digests a human approves. An image older than this change refuses the item
(exact key set), before the claim for an inline manifest.

### 3. The stage adapter

A v2 stage whose provider is `local-docker` (`docker-cpu-v1`) lists Salad as

    {"provider": "salad", "runtime": "salad-workload-v1",
     "provider_config": {"adapt": {"profile": "cpu-2c-4g"}},
     "max_exposure_cents": N?}

The submit and validate endpoints **expand** this form to the full, explicit `salad` block before
the spec is digested and stored, from the stage itself and the operator's profile:

- `profile`, the profile's worker and `gpu: false`;
- a `gpu_runtime_v1` contract: `workload_seconds` equal to the stage's `max_duration_seconds`; one
  output per stage output (name equal to the path, cap equal to the output's cap); empty
  `egress.fetches` and `redirect_hosts` (the adapter never opens an egress destination, and no
  profile or stage field can add one); empty `env`; the profile's custody host and watchdog; an
  `input` for the stage's single snapshot (digest from the spec, `max_bytes` from the profile's
  `stage_adapter.max_input_bytes`); and an inline manifest holding exactly the image entry with the
  stage's argv;
- the program template from the profile's `stage_adapter.program` (operator-authored, generic;
  never from the stage), and `grant_ttl_seconds` from the profile;
- the candidate's estimate, when the author gave none: the whole duration at the profile's CPU
  rate, rounded up to a cent (at least 1);
- `adapted: true`, so the Salad provider validates the stage argv as the contract's embedded argv
  instead of requiring the two to be equal, and the plan's execution spec carries the contract's
  argv (the one a human reviews and the guardian binds).

Expansion is a pure function of the request, the stage and the profile. The stored spec holds the
expanded block, so the approver sees and approves the exact workload by digest, and a later edit to
the operator profile cannot change an approved workload. It is not a way around any check: the
expanded candidate then passes the ordinary submit-time, approval-time and placement-time checks of
a hand-written one (profile, worker, image allowlist, per-run and per-task cents, trust, review
rules).

The adapter refuses with a 409 that names the limit when the stage cannot run on Salad:

| Refusal | Reason |
| --- | --- |
| unknown profile, GPU profile, no `stage_adapter` | the operator has not opted that profile in |
| image digest not in `allowed_image_digests` | operator allowlist (also checked again by `check_stage`) |
| `max_duration_seconds` > 3600 (or < 60) | Salad route and runtime window limits |
| `env`, `egress`, `scratch_bytes`, `retry.transient_exit_codes` | not expressible on Salad |
| not exactly one `snapshot` input | the contract binds one pinned input digest; stage and artifact inputs are not known at submit |
| output path not a flat Salad output name, or outputs above 16 MiB / 16 files | provider output limits |
| argv too large to carry in the argv template (about 4 KiB) | contract limit |
| the stage's `max_exposure_cents` below the candidate's estimate | the per-run cap the approver saw is never raised |

## Consequences

- "Salad first, local-host fallback" works for an existing CPU spec by adding one alternate; no
  Salad-form duplicate of the stage exists.
- The same CPU image runs on both providers. It must carry the runtime, which the local worker
  bypasses with `--entrypoint`.
- No new egress. A Salad stage still has Salad's provider-wide egress as every Salad stage does;
  the adapter declares no host, so ADR 0047's public-internet invariant holds: data reaches only
  registered, trusted providers and no fetch or redirect destination is added.
- A pipeline stage that reads another stage's outputs cannot use the adapter (its input digest is
  not known when the spec is submitted). Only stages reading one pinned snapshot do.
- Not verified against Salad: no live call was made. The launcher and runtime are tested with fakes
  and a real subprocess; the CPU image recipe is not built by tests.

## Tests

`tests/test_salad_stage_adapter.py`: expansion (contract, manifest, digests, determinism), every
refusal above, the placement of an adapted candidate, and the unchanged digest of a spec with no
adapter. `tests/test_salad_image_entry.py`: contract rules and the runtime's handling of the image
entry, and the launcher's path rewriting and exit code.

## Amendment

[ADR 0077](0077-campaign-prep-from-the-spec.md) extends the adapter to several pinned snapshot and
artifact inputs and makes `stage_adapter` fields optional; the single-snapshot limit above is lifted.
