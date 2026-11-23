"""The fixed command line and strict result parser of an image-build stage (ADR 0123).

A build stage runs the stage's pinned *builder image* through the ordinary hardened local-docker
container (read-only root, all capabilities dropped, no-new-privileges, pid and memory limits,
tmpfs /work, egress only through the allowlist proxy). The builder never gets the docker socket,
privileged mode, a host path or an author-supplied environment. Everything here is pure.

Builder contract (``tessera-image-build``, installed in the builder image):

* ``builder_argv(policy)`` is the whole command line. Every value in it is a policy field that
  was re-validated here with a strict pattern; there is no author-controlled free text.
* The wrapper runs, in order, rootless and daemonless (see ``buildah_commands`` for the exact
  flags it is expected to use and why):
  1. ``buildah pull`` of each ``--base-image`` (digest-pinned, through the egress proxy);
  2. ``buildah bud`` of ``--dockerfile`` with ``--context`` as the context directory;
  3. when ``--push``: ``buildah push`` to ``<repository>:tessera-<first 12 of image id>`` using
     only ``--auth-file``. A registry needs a tag or digest to receive a push and the digest of a
     built manifest is not known beforehand, so the tag is derived from the content (never a
     mutable name such as ``latest``) and the image is *reported and used by digest only*;
  4. writes ``--result`` (``tessera.image-build.v1``) and the log to ``--log``.
* Registry credentials are never an argument, an environment variable or part of the spec: the
  operator mounts a read-only auth file at ``REGISTRY_AUTH_PATH``, and only a pushing build gets
  the ``--auth-file`` flag.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tessera_control.campaign_models import BUILD_ARGV
from tessera_control.image_build_models import (
    FAILURE_CODES,
    MAX_RESULT_BYTES,
    RESULT_SCHEMA,
    BuildPolicyModel,
    ImageBuildRow,
    check_row_against_policy,
)

REGISTRY_AUTH_PATH = "/run/registry-auth/auth.json"
RESULT_FILE = "/outputs/image-build.json"
LOG_FILE = "/outputs/image-build.log"
INPUT_ROOT = "/inputs"

_REPOSITORY = re.compile(
    r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?/[a-z0-9]+(?:[._/-][a-z0-9]+)*\Z"
)
_DOCKERFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,198}\Z")
_CONTEXT = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_BASE_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ImageBuildError(Exception):
    """A build policy or result that must not be used; ``code`` is a closed failure code."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in FAILURE_CODES:
            raise ValueError("unknown image build failure code")
        super().__init__(message or code)
        self.code = code


def _checked_policy(policy: Any) -> dict[str, Any]:
    """The policy, re-validated field by field; anything off refuses (``policy_invalid``)."""
    if not isinstance(policy, dict):
        raise ImageBuildError("policy_invalid", "build policy must be an object")
    try:
        model = BuildPolicyModel.model_validate(policy).model_dump(mode="json")
    except ValueError as error:
        raise ImageBuildError("policy_invalid", "build policy is invalid") from error
    path = model["dockerfile"]
    base_images = model["base_images"]
    if (
        not _REPOSITORY.fullmatch(model["repository"])
        or "@" in model["repository"]
        or not _CONTEXT.fullmatch(model["context_input"])
        or not _DOCKERFILE.fullmatch(path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(not _BASE_IMAGE.fullmatch(image) for image in base_images)
        or len(set(base_images)) != len(base_images)
    ):
        raise ImageBuildError("policy_invalid", "build policy field is out of shape")
    return model


def builder_argv(policy: dict[str, Any]) -> list[str]:
    """The fixed ``tessera-image-build`` command line for ``policy``."""
    model = _checked_policy(policy)
    argv = [
        *BUILD_ARGV,
        "--context",
        f"{INPUT_ROOT}/{model['context_input']}",
        "--dockerfile",
        f"{INPUT_ROOT}/{model['context_input']}/{model['dockerfile']}",
    ]
    for image in model["base_images"]:
        argv += ["--base-image", image]
    argv += ["--repository", model["repository"]]
    if model["push"]:
        argv += ["--push", "--auth-file", REGISTRY_AUTH_PATH]
    else:
        argv += ["--no-push"]
    argv += ["--max-transfer-bytes", str(model["max_transfer_bytes"])]
    argv += ["--result", RESULT_FILE, "--log", LOG_FILE]
    return argv


def buildah_commands(policy: dict[str, Any], image_id: str = "0" * 64) -> list[list[str]]:
    """The buildah invocations the wrapper contract expects (documented, and tested here).

    * ``--isolation=chroot``: no user-namespace or privileged setup is possible in the hardened
      container (no capabilities), so RUN steps use chroot isolation inside the unprivileged
      user; ``--layers=false`` keeps no layer cache, so build state lives only under /work;
      ``--pull=never``: bases were already fetched by digest, a Dockerfile cannot pull anything
      else; ``--format=oci --timestamp=0``: reproducible bytes for a given context;
      ``--network=host`` is the container's own proxy-only network, not the host's.
    * push always uses the operator auth file and a content-derived tag.
    """
    model = _checked_policy(policy)
    context = f"{INPUT_ROOT}/{model['context_input']}"
    commands = [["buildah", "pull", "--quiet", image] for image in model["base_images"]]
    commands.append(
        [
            "buildah",
            "bud",
            "--isolation=chroot",
            "--layers=false",
            "--pull=never",
            "--format=oci",
            "--timestamp=0",
            "--network=host",
            "--iidfile=/work/image-id",
            f"--file={context}/{model['dockerfile']}",
            context,
        ]
    )
    if model["push"]:
        if not _DIGEST.fullmatch(image_id):
            raise ImageBuildError("policy_invalid", "image id is not a sha256")
        commands.append(
            [
                "buildah",
                "push",
                f"--authfile={REGISTRY_AUTH_PATH}",
                "--digestfile=/work/digest",
                image_id,
                f"docker://{model['repository']}:tessera-{image_id[:12]}",
            ]
        )
    return commands


def parse_build_result(raw: bytes, policy: dict[str, Any]) -> dict[str, Any]:
    """The closed result row from the builder's ``image-build.json``, or ``ImageBuildError``."""
    model = _checked_policy(policy)
    if not isinstance(raw, bytes | bytearray) or len(raw) > MAX_RESULT_BYTES:
        raise ImageBuildError("result_too_large" if isinstance(raw, bytes) else "result_invalid")
    try:
        document = json.loads(bytes(raw))
    except (ValueError, RecursionError) as error:
        raise ImageBuildError("result_invalid", "result is not JSON") from error
    allowed = {"schema", "image", "digest", "size_bytes", "pushed", "base_images", "seconds"}
    if (
        not isinstance(document, dict)
        or document.get("schema") != RESULT_SCHEMA
        or not set(document) <= allowed
        or not {"image", "digest", "size_bytes", "pushed", "base_images"} <= set(document)
    ):
        raise ImageBuildError("result_invalid", "result does not match the schema")
    document.pop("schema")
    try:
        row = ImageBuildRow.model_validate(document).model_dump(mode="json", exclude_none=True)
    except ValueError as error:
        raise ImageBuildError("result_invalid", "result does not match the schema") from error
    if not check_row_against_policy(model, row):
        raise ImageBuildError("result_mismatch", "result contradicts the build policy")
    return row
