# ADR 0123: An image build is a receipted stage; a built image is referenced by lineage

Status: accepted

## Context

Experiment images were built by hand (`docker build`, then the digests copied into a pins file)
before a campaign could run. That step had no receipt, no check on what went into the image, and
no link from the image a stage ran to the context it was built from.

## Decision

A v2 stage with a `build` block builds an image and pushes it to a registry the operator allows.

- **Registered destinations.** The operator, not the author, names where a build may push:
  `build_repositories` (`host[/path]` prefixes, matched on whole path segments) on `local_docker`
  and on each `self_hosted` instance of the providers file, and `build_repositories` on the
  worker's `CpuRuntime`. A pushing build whose `repository` is not under a registered prefix is
  refused at submit and approval, again when the execution plan is derived, and again in the
  worker before the registry auth file is named for a mount. An empty list refuses every pushing
  build; `push: false` needs no destination. The author's `build` block still names the host the
  egress allowlist opens, but only inside what the operator registered.
- **Fresh output per operation.** The worker removes `build-output` when it admits a new build
  operation (recovery of an existing one keeps it), so a result file an earlier build left behind
  is never read as this build's; a clean exit with no new file is `result_missing`.
- **Receipt shape.** `build` is recorded on each attempt entry (not the stage entry); the image
  digest is `sha256:<hex>` and the resolver reads it from the succeeded attempt.

- **Builder.** The stage's own `image` is a digest-pinned, rootless, daemonless builder. The
  stage declares `runtime: docker-build-v1`, `argv: ["tessera-image-build"]` and no `env` or
  `egress`: the provider derives the builder's command line and the narrow allowlist from the
  `build` block, so an author supplies no free-form commands. It runs in the same hardened
  container as any local-docker stage (read-only root, no capabilities, no docker socket, no host
  paths, memory and process limits), with outbound reach only through the allowlist proxy.
- **Context.** `build.context_input` names one pinned `snapshot` input (the stage's only input),
  so the context is content-addressed and approved by digest. It must be public data with no
  approved private code and must not be any stage's `code_input` in the campaign: a pushed image
  keeps every file it ever copied, so private code or data is never baked in (ADR 0095, ADR 0047).
  The Dockerfile path must be a file of the context.
- **Bases.** `build.base_images` are digest-pinned; the builder is told to pull nothing else.
- **Destination.** `build.repository` is `host[:443]/path`. The only hosts the builder may reach
  are that registry host and the registries of the pinned base images, on 443, through the
  allowlist proxy, with a byte cap (`max_transfer_bytes`). The registry credential is never part
  of the spec or the environment: the worker supervisor mounts an operator-configured read-only
  auth file for the push, and without one a pushing build refuses.
- **Receipt.** The attempt entry gains `build`: context digest, Dockerfile, builder image, base
  images, repository, and the produced image. The inputs come from the approved spec; the produced
  image comes only from the worker's validated report (an event `execution.image_built`, naming an
  image of the same repository), marked `provider_reported`, else `declared_only`. It is a report,
  not an attestation of the image's contents.
- **Lineage.** A later campaign's stage may say `image_from: {kind: receipt_image, predecessor |
  campaign_id, stage}` instead of a literal `image`. Submit resolves it, in the submit
  transaction before the spec is digested (as ADR 0080 does for outputs), to the predecessor's
  succeeded build stage's reported image, and records where it came from. The stored spec never
  holds an unresolved reference, and standing approvals and approved submissions refuse it. There
  is no pins file.

## What this does not do

- **Producing an image does not make it runnable.** The operator still lists the digest in the
  worker's `--allowed-image` (or the Salad profile's `allowed_image_digests`); the resolved image
  is checked there at dispatch like any other.
- It does not run a build on a remote provider, does not accept a private context, does not give
  stage code a secret, and does not verify what a registry stored.
- Nothing here was run against a live registry or builder; the builder's command line and the
  worker report are tested with fakes only.

## Not finished (stated plainly)

- The builder wrapper `tessera-image-build` (`src/tessera_worker/builder_wrapper.py`, stdlib
  only) and the builder image recipe (`Dockerfile.image-build`, which needs an operator-pinned
  buildah image) are in the repository and tested with a fake `buildah` only. The wrapper checks
  that every `FROM`, `COPY --from` and `RUN --mount from=` names a pinned base image or an
  earlier stage, refuses a pushing build without the mounted auth file, and never reads it. What
  is here besides is the contract: the fixed command line the provider derives (`builder_argv`),
  the `buildah` steps (`buildah_commands`, which the wrapper repeats and a test compares), and
  the result file (`/outputs/image-build.json`) the worker validates. No real buildah, registry
  or push has been exercised.
- The worker's registry auth file and `build_repositories` are set by the operator through
  `--registry-auth-file` / `--build-repository` or the poller config keys of the same names
  (both or neither; absent refuses every pushing build). The file is checked private and never
  read by the worker.
- The hardened container's `/work` is `noexec` and its output directory is owned by the
  supervisor, so a rootless build's `RUN` steps and the container user's right to write the result
  need checking against a real builder.
- The push target is a content-derived tag (never `latest`) because a registry needs a name before
  its digest exists; the image is reported and used by digest only.
- A stage with only the build runtime, or a plain stage naming it, is refused at validation; the
  local-docker provider offers `docker-build-v1` and a self-hosted instance must list it to opt in.
- No end-to-end run of a build exists; the report route, event and receipt are tested with fakes
  and the receipt reader is stubbed in the receipt tests.

## Consequences

- No schema change: the fields are optional and absent from existing specs' digests; the report
  is an append-only event.
- A build then use takes two submissions (the predecessor must have succeeded), as for ADR 0080.
