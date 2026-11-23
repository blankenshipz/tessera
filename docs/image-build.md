# Image build stages

A build stage builds a container image from a public, pinned source snapshot and, if asked,
pushes it to a registry you registered. A later stage or campaign can then run that image
([ADR 0123](decisions/0123-image-build-stage-and-image-lineage.md)).

Status: verified with fakes only. Nothing has run against a live registry. Two points need a
check against a real builder: `RUN` steps under chroot isolation inside a capability-dropped
container, and the `noexec` `/work` tmpfs.

## The builder image

The stage runs a builder image whose command is `tessera-image-build`. The recipe is
`Dockerfile.image-build`. It adds python3 and one script (the wrapper) to a buildah image and
sets up vfs storage under `/work`. It never picks a base: you pass a buildah image you have
pinned by digest.

```
docker build -f Dockerfile.image-build \
  --build-arg BUILDAH_IMAGE=<digest-pinned buildah image> \
  -t REGISTRY/tessera-image-build:<git-sha> .
docker push REGISTRY/tessera-image-build:<git-sha>
```

Push to a registry you control, then pin the digest the push reports
(`REGISTRY/tessera-image-build@sha256:<hex>`). Nothing else from the repository is copied into
the image; it holds no credential and no experiment code.

## Operator setup

1. In the `--campaign-providers` file, list `build_repositories` (`host[/path]` prefixes) on the
   local-docker or `self_hosted` provider that will run builds.
2. Start the worker with `--registry-auth-file FILE` and one or more `--build-repository
   PREFIX`. Give both or neither. With neither, every pushing build is refused. See
   [campaign providers](campaign-providers.md).
3. Add the builder image digest to the worker's `--allowed-image`.

## The stage spec

A build stage uses runtime `docker-build-v1` and a `build` block:

- `context_input`: key of a public, pinned `snapshot` input of the stage; it is the build context.
- `dockerfile`: a plain path inside the context (default `Dockerfile`).
- `base_images`: digest-pinned base images (1 to 8, distinct). Every `FROM` in the Dockerfile
  must use one of them, an alias of an earlier stage, or `scratch`.
- `repository`: where the image is pushed; must sit under a registered prefix.
- `push` (default true) and `max_transfer_bytes`.

The argv is derived from the spec; you do not write it.

## Using the built image

A later stage (or a later campaign) names the image by lineage instead of by digest:

```
image_from: {kind: receipt_image, predecessor: <name>, stage: <stage>}
image_from: {kind: receipt_image, campaign_id: <id>, stage: <stage>}
```

Submit resolves it from the build stage's receipt and records where it came from.

## What the receipt records

One `build` entry per attempt: context digest, builder image, base images, repository,
`produced_image`, its digest (`sha256:<hex>`), size, and evidence. Evidence is
`provider_reported` when the builder reported the digest, or `declared_only` when nothing
confirms it. A recorded build is not proof the image is trustworthy.

## Security notes

- No registry credential appears in the spec, environment, argv or logs. The auth file is only
  ever a read-only mount at `/run/registry-auth/auth.json`, added after the repository check.
  The worker checks the file and never reads it.
- Pushes go only to registered repositories, checked at submit, at plan and at the worker.
- The build context is public only, so private experiment code is never baked into an image.
  Experiment code stays a content-addressed `code_input` of a later stage.
- The produced image still has to be allowlisted by the operator before it can run.
- The wrapper refuses a push without a readable auth file, a `FROM` outside the pinned base
  images, an `ARG`-substituted `FROM`, and `COPY --from` of an external image.
