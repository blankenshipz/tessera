"""Synthetic public copied originals: integrity proof, never upstream commit proof."""

import gzip
import io
import json
import tarfile

import pytest
from test_release_candidate import builder
from test_release_candidate import source as source

EPOCH = 1767225600
FILE = "packages/sdk/js/src/context.ts"


def snapshot(root, members=None):
    root.mkdir()
    license_bytes = b"MIT synthetic public license\n"
    content = {"LICENSE": license_bytes, FILE: b"export const value = 1;\n"}
    archive_bytes = io.BytesIO()
    with gzip.GzipFile(fileobj=archive_bytes, mode="wb", filename="", mtime=EPOCH) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for name, data, kind in members or [
                (n, v, tarfile.REGTYPE) for n, v in sorted(content.items())
            ]:
                entry = tarfile.TarInfo(name)
                entry.type, entry.size, entry.mode, entry.mtime = kind, len(data), 0o644, EPOCH
                if kind == tarfile.SYMTYPE:
                    entry.linkname = "/private/never-read"
                    entry.size = 0
                archive.addfile(entry, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    patch = (
        f"--- a/{FILE}\n+++ b/{FILE}\n@@ -1 +1 @@\n"
        "-export const value = 1;\n+export const value = 2;\n"
    ).encode()
    baseline = {
        "schema_version": "tessera-opencode-observer-baseline-v2",
        "mode": "file-pinned-snapshot",
        "source_origin": "https://example.test/public/source",
        "source_version": "2.0.18",
        "source_reference": "copied-isolated-source",
        "upstream_verification": "not_independently_verified",
        "source_date_epoch": EPOCH,
        "baseline_archive_sha256": builder.digest(archive_bytes.getvalue()),
        "baseline_files": {n: builder.digest(v) for n, v in content.items()},
        "licenses": {"LICENSE": builder.digest(license_bytes)},
        "patch_sha256": builder.digest(patch),
    }
    (root / "baseline.tar.gz").write_bytes(archive_bytes.getvalue())
    (root / "LICENSE").write_bytes(license_bytes)
    (root / "observer.patch").write_bytes(patch)
    (root / "baseline.json").write_text(json.dumps(baseline))
    return root, baseline


def test_snapshot_has_actual_original_bytes_and_no_commit_claim(tmp_path):
    root, baseline = snapshot(tmp_path / "public")
    assert builder.observer_bundle(root) == baseline
    assert (
        "commit" not in baseline
        and baseline["upstream_verification"] == "not_independently_verified"
    )
    destination = tmp_path / "staged"
    builder.stage_observer(root, destination)
    assert (destination / "baseline.tar.gz").read_bytes() == (root / "baseline.tar.gz").read_bytes()


@pytest.mark.parametrize(
    "change",
    [
        "commit",
        "verification",
        "digest",
        "license",
        "missing_pin",
        "original_hash",
        "origin",
        "archive_symlink",
        "private_pin",
        "archive_epoch",
        "false_mode",
        "duplicate_json",
    ],
)
def test_snapshot_cannot_upgrade_provenance_or_echo_unverified_pins(tmp_path, change):
    root, baseline = snapshot(tmp_path / "public")
    if change == "commit":
        baseline["commit"] = "a" * 40
    if change == "verification":
        baseline["upstream_verification"] = "verified"
    if change == "digest":
        baseline["baseline_archive_sha256"] = "b" * 64
    if change == "license":
        baseline["licenses"]["LICENSE"] = "b" * 64
    if change == "missing_pin":
        baseline["baseline_files"].pop(FILE)
    if change == "original_hash":
        baseline["baseline_files"][FILE] = "b" * 64
    if change == "origin":
        baseline["source_origin"] = "https://user:password@example.test/source"
    if change == "archive_symlink":
        (root / "baseline.tar.gz").unlink()
        (root / "baseline.tar.gz").symlink_to(root / "never-read")
    if change == "private_pin":
        baseline["baseline_files"]["packages/core/src/private-guard.ts"] = "b" * 64
    if change == "archive_epoch":
        baseline["source_date_epoch"] = EPOCH + 1
    if change == "false_mode":
        baseline["mode"] = "commit-pinned"
    (root / "baseline.json").write_text(json.dumps(baseline))
    if change == "duplicate_json":
        text = (root / "baseline.json").read_text()
        (root / "baseline.json").write_text(text[:-1] + ', "mode":"file-pinned-snapshot"}')
    with pytest.raises(ValueError):
        builder.observer_bundle(root)


@pytest.mark.parametrize(
    "change", ["duplicate", "traversal", "private", "cache", "symlink", "unlisted", "oversize"]
)
def test_snapshot_archive_members_are_closed_and_bounded(tmp_path, change):
    members = [
        ("LICENSE", b"MIT synthetic public license\n", tarfile.REGTYPE),
        (FILE, b"export const value = 1;\n", tarfile.REGTYPE),
    ]
    if change == "duplicate":
        members.append(members[-1])
    if change == "traversal":
        members.append(("../outside.ts", b"x", tarfile.REGTYPE))
    if change == "private":
        members.append(("packages/opencode/src/private-guard.ts", b"x", tarfile.REGTYPE))
    if change == "cache":
        members.append(("packages/sdk/js/src/cache/x.ts", b"x", tarfile.REGTYPE))
    if change == "symlink":
        members[-1] = (FILE, b"", tarfile.SYMTYPE)
    if change == "unlisted":
        members.append(("packages/sdk/js/src/other.ts", b"x", tarfile.REGTYPE))
    if change == "oversize":
        members[-1] = (FILE, b"x" * (1024 * 1024 + 1), tarfile.REGTYPE)
    root, _ = snapshot(tmp_path / "public", members)
    with pytest.raises(ValueError):
        builder.observer_bundle(root)


def test_snapshot_archive_ships_in_source_and_wheel_only_exact_extra_file(source, tmp_path):
    bundle, _ = snapshot(tmp_path / "public")
    destination = source / "release/opencode-observer"
    builder.stage_observer(bundle, destination)
    (destination / "README.md").write_text("Declared copied-file provenance, not verified commit\n")
    (destination / "other.tar.gz").write_text("MUST_NOT_SHIP")
    with (source / "pyproject.toml").open("a") as handle:
        handle.write(
            '\n[tool.setuptools.data-files]\n"share/tessera/release/opencode-observer" = '
            '["release/opencode-observer/baseline.tar.g[z]"]\n'
        )
    result = builder.build(source, tmp_path / "candidate", "0.1.0rc1", EPOCH)
    assert result["observer"]["mode"] == "file-pinned-snapshot"
    assert "release/opencode-observer/other.tar.gz" not in {r["path"] for r in result["inputs"]}
    import zipfile

    with zipfile.ZipFile(next((tmp_path / "candidate").glob("*.whl"))) as wheel:
        assert any(n.endswith("opencode-observer/baseline.tar.gz") for n in wheel.namelist())
