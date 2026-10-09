"""Bounded public copied-baseline validation; never invents upstream commit proof."""

import hashlib
import io
import re
import tarfile
from pathlib import PurePosixPath
from urllib.parse import urlsplit

FIELDS = {
    "schema_version",
    "mode",
    "source_origin",
    "source_version",
    "source_reference",
    "upstream_verification",
    "source_date_epoch",
    "baseline_archive_sha256",
    "baseline_files",
    "licenses",
    "patch_sha256",
}
PREFIXES = (
    "packages/core/src/",
    "packages/opencode/src/",
    "packages/plugin/src/",
    "packages/sdk/src/",
    "packages/sdk/js/src/",
)
GUARDS = {"subscription-guard.mjs", "plugins/subscription-guard.mjs"}
PUBLIC_DELIVERABLE = {
    "LICENSE.opencode",
    "harness/LICENSE",
    "harness/bin/codex-quota",
    "harness/config/guard-validation.mjs",
    "harness/plugins/v2/completion-ledger.mjs",
    "harness/plugins/v2/paths.mjs",
    "harness/plugins/v2/subscription-guard.mjs",
    "harness/src/codex_quota.py",
    "harness/src/paths.py",
    "package.json",
    "packages/core/package.json",
    "packages/core/src/app.ts",
    "packages/core/src/permission.ts",
    "packages/core/src/plugin/hooks.ts",
    "packages/core/src/session/model-request.ts",
    "packages/core/src/session/model-transport.ts",
    "packages/core/src/session/provider-context.ts",
    "packages/core/src/session/runner/model.ts",
    "packages/core/src/session/runner/to-llm-message.ts",
    "packages/core/src/session/schema.ts",
    "packages/core/src/session/system-prompt.ts",
    "packages/core/src/tool.ts",
    "packages/core/src/tool/plugin/question.ts",
    "packages/plugin/package.json",
    "packages/plugin/src/effect/session.ts",
    "packages/plugin/src/promise/session.ts",
    "packages/sdk/package.json",
    "harness/plugins/v2/guard-check-publisher.mjs",
    "harness/src/quota_check_receipt.py",
    "harness/tests/guard-check-publisher.test.mjs",
    "harness/tests/test_quota_check_receipt.py",
    "packages/core/src/plugin/model-phase.ts",
    "packages/core/src/session/model-phase-channel.ts",
    "packages/core/test/model-phase-service.test.ts",
    "packages/core/test/model-phase.test.ts",
    "packages/sdk/src/effect/model-phase.ts",
}
PUBLIC_ADDITIONS = {
    "harness/plugins/v2/guard-check-publisher.mjs",
    "harness/src/quota_check_receipt.py",
    "harness/tests/guard-check-publisher.test.mjs",
    "harness/tests/test_quota_check_receipt.py",
    "packages/core/src/plugin/model-phase.ts",
    "packages/core/src/session/model-phase-channel.ts",
    "packages/core/test/model-phase-service.test.ts",
    "packages/core/test/model-phase.test.ts",
    "packages/sdk/src/effect/model-phase.ts",
}


def public_path(name: str) -> bool:
    if name in PUBLIC_DELIVERABLE:
        return True
    path = PurePosixPath(name)
    if (
        not re.fullmatch(r"[A-Za-z0-9_./-]{1,180}", name)
        or path.is_absolute()
        or str(path) != name
        or any(part in {".", ".."} for part in path.parts)
        or any(
            word in name.lower()
            for word in (
                "private",
                "secret",
                "credential",
                "auth",
                ".env",
                "node_modules",
                "cache",
                "worktree",
                "receipt",
            )
        )
    ):
        return False
    if name in GUARDS or name == "LICENSE":
        return True
    return name.startswith(PREFIXES) and (path.suffix in {".ts", ".mjs"} or path.name == "LICENSE")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_snapshot(root, baseline, files):
    extended = set(baseline) == FIELDS | {"license_path", "target_files"}
    if not extended and set(baseline) != FIELDS:
        raise ValueError("Snapshot fields are closed; commit claims are not accepted.")
    if (
        baseline["schema_version"] != "tessera-opencode-observer-baseline-v2"
        or baseline["mode"] != "file-pinned-snapshot"
        or baseline["upstream_verification"] != "not_independently_verified"
    ):
        raise ValueError(
            "Copied snapshot cannot establish independently verified upstream provenance."
        )
    for key in ("source_version", "source_reference"):
        value = baseline[key]
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_. /:@+-]{1,200}", value):
            raise ValueError("Public snapshot version/reference must be bounded declared text.")
    uri = urlsplit(baseline["source_origin"])
    if (
        uri.scheme != "https"
        or not uri.hostname
        or uri.username
        or uri.password
        or uri.query
        or uri.fragment
    ):
        raise ValueError("Snapshot origin must be public HTTPS without private connection data.")
    epoch = baseline["source_date_epoch"]
    if type(epoch) is not int or not 0 <= epoch <= 4294967295:
        raise ValueError("Snapshot requires a bounded deterministic epoch.")
    expected = baseline["baseline_files"]
    licenses = baseline["licenses"]
    license_path = baseline.get("license_path", "LICENSE")
    if extended:
        target = baseline["target_files"]
        if (
            not isinstance(target, dict)
            or set(target) != PUBLIC_DELIVERABLE
            or set(expected) != PUBLIC_DELIVERABLE - PUBLIC_ADDITIONS
            or set(licenses) != {"LICENSE.opencode", "harness/LICENSE"}
            or license_path != "LICENSE.opencode"
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in target.values()
            )
        ):
            raise ValueError(
                "Actual public layout must preserve all 27 originals and 36 target files."
            )
    for mapping in (expected, licenses):
        if (
            not isinstance(mapping, dict)
            or not 1 <= len(mapping) <= 64
            or any(
                not public_path(name)
                or not isinstance(value, str)
                or not re.fullmatch(r"[0-9a-f]{64}", value)
                for name, value in mapping.items()
            )
        ):
            raise ValueError("Snapshot file/license pins are outside the public allowlist.")
    if (
        license_path not in licenses
        or any(PurePosixPath(name).name not in {"LICENSE", "LICENSE.opencode"} for name in licenses)
        or any(expected.get(name) != value for name, value in licenses.items())
        or licenses[license_path] != sha(files["LICENSE"])
        or baseline["patch_sha256"] != sha(files["observer.patch"])
    ):
        raise ValueError("Snapshot license or patch pins differ.")
    archive = root / "baseline.tar.gz"
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Snapshot archive is missing, linked or oversized.")
    data = archive.read_bytes()
    if (
        sha(data) != baseline["baseline_archive_sha256"]
        or len(data) < 10
        or data[:3] != b"\x1f\x8b\x08"
        or data[3] != 0
        or int.from_bytes(data[4:8], "little") != epoch
    ):
        raise ValueError("Snapshot archive digest/deterministic gzip metadata mismatch.")
    seen, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as tar:
        for member in tar:
            if (
                len(seen) >= 64
                or member.name in seen
                or member.name not in expected
                or not public_path(member.name)
                or not member.isfile()
                or member.size > 1024 * 1024
                or member.uid != 0
                or member.gid != 0
                or member.uname
                or member.gname
                or member.mode != (0o755 if member.name == "harness/bin/codex-quota" else 0o644)
                or member.mtime != epoch
                or member.pax_headers
            ):
                raise ValueError(
                    "Snapshot archive has unapproved members or non-deterministic metadata."
                )
            total += member.size
            if total > 8 * 1024 * 1024:
                raise ValueError("Snapshot archive expands beyond its bound.")
            body = tar.extractfile(member).read()
            if sha(body) != expected[member.name]:
                raise ValueError("Snapshot original bytes differ from their file pins.")
            seen[member.name] = sha(body)
    if seen != expected:
        raise ValueError("Snapshot archive does not contain every exact pinned original file.")
    paths = re.findall(r"^(?:---|\+\+\+) (\S+)", files["observer.patch"].decode(), re.MULTILINE)
    if not paths or any(
        name != "/dev/null" and (not name.startswith(("a/", "b/")) or not public_path(name[2:]))
        for name in paths
    ):
        raise ValueError("Snapshot patch is outside the approved public core/SDK/guard scope.")
    # Every existing old-side file must have actual original bytes, not a hash echo.
    old = re.findall(r"^--- a/(\S+)", files["observer.patch"].decode(), re.MULTILINE)
    if any(name not in expected for name in old):
        raise ValueError("Patch old-side baseline file missing.")
    return baseline
