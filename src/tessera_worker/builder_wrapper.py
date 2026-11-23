#!/usr/bin/env python3
"""``tessera-image-build``: the builder image's command for an image-build stage (ADR 0123).

Standard library only, so it can be copied alone into the builder image
(``Dockerfile.image-build``). The worker runs it with the fixed argument list that
``tessera_worker.image_build.builder_argv`` derives from the approved plan; nothing else reaches
it (no author argv, environment or egress). It runs rootless and daemonless buildah in order:
pull each pinned base image, build the Dockerfile, and, with ``--push``, push to
``<repository>:tessera-<first 12 of the image id>`` using only the operator's read-only auth file.
It then writes the strict ``tessera.image-build.v1`` result and a bounded log.

It refuses (exit code 2, nothing built or pushed) unless:

* every value matches the same strict pattern the worker applied;
* the Dockerfile is a real file inside the context and every ``FROM`` names one of the pinned
  base images (or ``scratch``, or an earlier stage of the same file); a ``FROM`` or
  ``COPY --from`` that would fetch anything else, or that uses a build argument, is refused;
* a pushing build has the operator's auth file, a regular readable file at the path the worker
  mounts it. The wrapper never reads the file: only buildah is handed its path.

The registered-destination rule is enforced before this runs, by control at submit and by the
worker before the auth file is mounted; the wrapper adds that it can only push to the one
``--repository`` it was given and never to another name. Registry credentials are never an
argument, an environment variable or part of any output.

Exit codes: 0 built (and pushed when asked); 1 the build or push failed; 2 refused.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RESULT_SCHEMA = "tessera.image-build.v1"
REGISTRY_AUTH_PATH = "/run/registry-auth/auth.json"
MAX_LOG_BYTES = 1024 * 1024
MAX_DOCKERFILE_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 1024**4
WORK = "/work"

_REPOSITORY = re.compile(
    r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?/[a-z0-9]+(?:[._/-][a-z0-9]+)*\Z"
)
_BASE_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}\Z")
_DOCKERFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,198}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class Refused(Exception):
    """The request is not one this wrapper will run; nothing was built or pushed."""


class Failed(Exception):
    """buildah ran and failed (or produced something unusable)."""


# -- argument and Dockerfile checks (pure) -----------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tessera-image-build", allow_abbrev=False)
    parser.add_argument("--context", required=True)
    parser.add_argument("--dockerfile", required=True)
    parser.add_argument("--base-image", action="append", required=True, dest="base_images")
    parser.add_argument("--repository", required=True)
    push = parser.add_mutually_exclusive_group(required=True)
    push.add_argument("--push", action="store_true")
    push.add_argument("--no-push", action="store_true")
    parser.add_argument("--auth-file")
    parser.add_argument("--max-transfer-bytes", type=int, required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--log", required=True)
    return parser


def check_arguments(arguments: argparse.Namespace) -> None:
    """Refuse any value outside the shapes the worker's policy check allows."""
    bases = arguments.base_images
    if (
        not _REPOSITORY.fullmatch(arguments.repository)
        or "@" in arguments.repository
        or not bases
        or len(bases) > 8
        or len(set(bases)) != len(bases)
        or any(not _BASE_IMAGE.fullmatch(image) for image in bases)
        or not 1 <= arguments.max_transfer_bytes <= 2 * 1024**3
        or not Path(arguments.result).is_absolute()
        or not Path(arguments.log).is_absolute()
        or not Path(arguments.context).is_absolute()
    ):
        raise Refused("a build argument is out of shape")
    if arguments.push:
        # The operator mounts the auth file at one fixed path; any other path is not theirs.
        if arguments.auth_file != REGISTRY_AUTH_PATH:
            raise Refused("a pushing build must use the operator's registry auth file")
    elif arguments.auth_file is not None:
        raise Refused("a build that does not push takes no auth file")


def check_auth_file(path: str) -> None:
    """A pushing build needs the operator's auth file, present and regular; never read here."""
    try:
        info = os.lstat(path)
    except OSError as error:
        raise Refused("no registry auth file is mounted for a pushing build") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise Refused("the registry auth file is not a regular file")
    if not os.access(path, os.R_OK):
        raise Refused("the registry auth file is not readable")


def resolve_dockerfile(context: str, dockerfile: str) -> Path:
    """The Dockerfile's real path, which must lie inside the real context directory."""
    root = Path(context).resolve(strict=False)
    if not root.is_dir():
        raise Refused("the build context is not a directory")
    path = Path(dockerfile)
    try:
        relative = path.relative_to(Path(context))
    except ValueError as error:
        raise Refused("the Dockerfile is not inside the build context") from error
    if not _DOCKERFILE.fullmatch(str(relative)) or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise Refused("the Dockerfile path is not a plain path")
    real = (root / relative).resolve(strict=False)
    if root not in real.parents or not real.is_file():
        raise Refused("the Dockerfile is not a regular file inside the build context")
    return real


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#") and current:
            continue  # a comment line inside a continued instruction
        if raw.rstrip().endswith("\\"):
            current += raw.rstrip()[:-1] + " "
            continue
        lines.append((current + raw).strip())
        current = ""
    if current.strip():
        lines.append(current.strip())
    return lines


def check_dockerfile(text: str, base_images: list[str]) -> None:
    """Every image the Dockerfile would use is one of the pinned bases (or an earlier stage).

    Nothing else may be fetched: ``--pull=never`` backs this up at build time. A ``FROM`` or
    ``COPY --from`` using a variable, a tag, or an unpinned name is refused."""
    stages: set[str] = set()
    count = 0
    for line in _logical_lines(text):
        try:
            words = shlex.split(line, comments=False, posix=True)
        except ValueError as error:
            raise Refused("the Dockerfile has an unparseable line") from error
        if not words:
            continue
        keyword = words[0].upper()
        if keyword == "FROM":
            rest = [word for word in words[1:] if not word.startswith("--")]
            if not rest:
                raise Refused("a FROM line names no image")
            image = rest[0]
            if "$" in image:
                raise Refused("a FROM line must not use a build argument")
            if image != "scratch" and image not in stages and image not in base_images:
                raise Refused("a FROM line names an image that is not a pinned base image")
            if len(rest) >= 3 and rest[1].upper() == "AS":
                alias = rest[2]
                if not _ALIAS.fullmatch(alias):
                    raise Refused("a stage name is out of shape")
                stages.add(alias)
            elif len(rest) != 1:
                raise Refused("a FROM line is out of shape")
            count += 1
        elif keyword in {"COPY", "ADD"}:
            for word in words[1:]:
                if not word.startswith("--"):
                    break
                if word.lower().startswith("--from="):
                    source = word.split("=", 1)[1]
                    if "$" in source or not (
                        source in stages or source.isdecimal() or source in base_images
                    ):
                        raise Refused("COPY --from names an image that is not a pinned base")
        elif keyword == "RUN":
            if any(word.lower().startswith("--mount=") and "from=" in word for word in words[1:]):
                for word in words[1:]:
                    if word.lower().startswith("--mount=") and "from=" in word:
                        source = word.split("from=", 1)[1].split(",", 1)[0]
                        if "$" in source or not (source in stages or source in base_images):
                            raise Refused("RUN --mount names an image that is not a pinned base")
    if count == 0:
        raise Refused("the Dockerfile has no FROM line")


# -- buildah ---------------------------------------------------------------------------------


def commands(arguments: argparse.Namespace, image_id: str = "0" * 64) -> list[list[str]]:
    """The buildah invocations, identical to ``tessera_worker.image_build.buildah_commands``."""
    context = arguments.context
    result = [["buildah", "pull", "--quiet", image] for image in arguments.base_images]
    result.append(
        [
            "buildah",
            "bud",
            "--isolation=chroot",
            "--layers=false",
            "--pull=never",
            "--format=oci",
            "--timestamp=0",
            "--network=host",
            f"--iidfile={WORK}/image-id",
            f"--file={arguments.dockerfile}",
            context,
        ]
    )
    if arguments.push:
        result.append(
            [
                "buildah",
                "push",
                f"--authfile={arguments.auth_file}",
                f"--digestfile={WORK}/digest",
                image_id,
                f"docker://{arguments.repository}:tessera-{image_id[:12]}",
            ]
        )
    return result


class _Log:
    """A bounded log file; the command lines logged never include the auth file's content."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.size = 0
        self.handle = open(path, "ab")  # noqa: SIM115 - closed in close()

    def write(self, data: bytes | str) -> None:
        raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
        room = MAX_LOG_BYTES - self.size
        if room <= 0:
            return
        raw = raw[:room]
        self.handle.write(raw)
        self.handle.flush()
        self.size += len(raw)

    def close(self) -> None:
        self.handle.close()


def _environment() -> dict[str, str]:
    """A fixed environment: nothing is inherited except PATH, so no author or ambient value
    (proxy variables included, which the worker sets for the allowlist proxy) is lost or added."""
    keep = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy"}
    }
    keep.update(
        {
            "HOME": WORK,
            "TMPDIR": f"{WORK}/tmp",
            "BUILDAH_ISOLATION": "chroot",
            "STORAGE_DRIVER": "vfs",
            "LANG": "C.UTF-8",
        }
    )
    return keep


def _run(command: list[str], log: _Log, environment: dict[str, str]) -> str:
    log.write("+ " + " ".join(shlex.quote(part) for part in command) + "\n")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=environment,
            timeout=None,
            check=False,
        )
    except OSError as error:
        raise Failed(f"could not run {command[0]}") from error
    log.write(completed.stdout[:MAX_LOG_BYTES])
    log.write(completed.stderr[:MAX_LOG_BYTES])
    if completed.returncode != 0:
        raise Failed(f"{command[0]} {command[1]} exited {completed.returncode}")
    return completed.stdout.decode("utf-8", "replace")


def _read_small(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read(4096).strip()
    except OSError as error:
        raise Failed("buildah left no result file") from error


def _digest(text: str, what: str) -> str:
    value = text.removeprefix("sha256:").strip()
    if not _DIGEST.fullmatch(value):
        raise Failed(f"{what} is not a sha256 digest")
    return value


def _image_size(image_id: str, log: _Log, environment: dict[str, str]) -> int:
    out = _run(["buildah", "images", "--json", image_id], log, environment)
    try:
        listing = json.loads(out)
        size = int(listing[0]["size"])
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise Failed("buildah did not report an image size") from error
    if not 1 <= size <= MAX_IMAGE_BYTES:
        raise Failed("the image size is out of range")
    return size


def build(arguments: argparse.Namespace, log: _Log) -> dict[str, object]:
    started = time.monotonic()
    environment = _environment()
    for name in ("tmp", "storage", "run"):
        os.makedirs(f"{WORK}/{name}", mode=0o700, exist_ok=True)
    for stale in ("image-id", "digest"):
        with _suppress():
            os.unlink(f"{WORK}/{stale}")
    plan = commands(arguments)
    for command in plan[:-1] if arguments.push else plan:
        _run(command, log, environment)
    image_id = _digest(_read_small(f"{WORK}/image-id"), "the image id")
    if arguments.push:
        _run(commands(arguments, image_id)[-1], log, environment)
        digest = _digest(_read_small(f"{WORK}/digest"), "the pushed digest")
    else:
        digest = image_id
    size = _image_size(image_id, log, environment)
    if size > arguments.max_transfer_bytes:
        raise Failed("the image is larger than the transfer budget")
    return {
        "schema": RESULT_SCHEMA,
        "image": f"{arguments.repository}@sha256:{digest}",
        "digest": digest,
        "size_bytes": size,
        "pushed": bool(arguments.push),
        "base_images": list(arguments.base_images),
        "seconds": round(time.monotonic() - started, 3),
    }


class _suppress:  # noqa: N801 - tiny local context manager for OSError
    def __enter__(self) -> None:
        return None

    def __exit__(self, kind, value, trace) -> bool:
        return kind is not None and issubclass(kind, OSError)


def write_result(path: str, document: dict[str, object]) -> None:
    """Atomically replace the result file with the strict document (never partial)."""
    directory = os.path.dirname(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".result-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        with _suppress():
            os.unlink(temporary)
        raise


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as error:
        return 2 if error.code else 0
    try:
        check_arguments(arguments)
        if arguments.push:
            check_auth_file(arguments.auth_file)
        dockerfile = resolve_dockerfile(arguments.context, arguments.dockerfile)
        if dockerfile.stat().st_size > MAX_DOCKERFILE_BYTES:
            raise Refused("the Dockerfile is too large")
        check_dockerfile(dockerfile.read_text(encoding="utf-8"), arguments.base_images)
    except (Refused, UnicodeDecodeError, OSError) as error:
        print(f"tessera-image-build: refused: {error}", file=sys.stderr)
        return 2
    # A result left by anything earlier is never mistaken for this build's.
    with _suppress():
        os.unlink(arguments.result)
    log = _Log(arguments.log)
    try:
        document = build(arguments, log)
        write_result(arguments.result, document)
    except Failed as error:
        log.write(f"failed: {error}\n")
        print(f"tessera-image-build: failed: {error}", file=sys.stderr)
        return 1
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
