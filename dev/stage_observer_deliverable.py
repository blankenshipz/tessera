"""Map the reviewed 27->36 public FILEPINNEDSPARSE handoff without rewriting its archive."""

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from build_release_candidate import observer_bundle

FIELDS = {
    "baseline",
    "baseline_archive_sha256",
    "baseline_index_sha256",
    "claim",
    "excluded",
    "harness_selected_commit",
    "license_paths",
    "mode",
    "opencode_snapshot_version",
    "patch_sha256",
    "target",
    "target_index_sha256",
    "upstream_commit",
    "upstream_release_checksum_verified",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def review_text(data):
    # High-confidence material only; fixture traps/raw field names are not secrets.
    patterns = (
        rb"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        rb"\bsk-[A-Za-z0-9_-]{24,}",
        rb"(?i)Bearer\s+[A-Za-z0-9_-]{40,}",
    )
    if any(re.search(pattern, data) for pattern in patterns):
        raise ValueError(
            "Public source review found credential-shaped material; no content logged."
        )


def stage(source: Path, destination: Path, expected_target_index: str):
    names = (
        "source-manifest.json",
        "BASELINE.sha256",
        "TARGET.sha256",
        "baseline.tar.gz",
        "observer.patch",
    )
    if source.is_symlink() or any(
        (source / name).is_symlink() or (source / name).stat().st_size > 4 * 1024 * 1024
        for name in names
    ):
        raise ValueError("Public handoff files must be bounded ordinary files.")
    manifest = json.loads((source / "source-manifest.json").read_text())
    if (
        set(manifest) != FIELDS
        or manifest["mode"] != "FILEPINNEDSPARSE"
        or manifest["upstream_commit"] is not None
        or manifest["upstream_release_checksum_verified"] is not False
        or not re.fullmatch(r"[0-9a-f]{40}", manifest["harness_selected_commit"])
    ):
        raise ValueError("Public handoff provenance fields differ from their reviewed contract.")
    maps = {}
    for label in ("baseline", "target"):
        rows = manifest[label]
        if any(
            set(row) != {"path", "sha256", "size"}
            or type(row["size"]) is not int
            or not 0 <= row["size"] <= 1024 * 1024
            for row in rows
        ):
            raise ValueError("Public file manifest fields are closed and bounded.")
        maps[label] = {row["path"]: row["sha256"] for row in rows}
        if len(maps[label]) != len(rows):
            raise ValueError("Duplicate public manifest path.")
        index = (source / (label.upper() + ".sha256")).read_bytes()
        expected = "".join(
            f"{value}  {name}\n" for name, value in sorted(maps[label].items())
        ).encode()
        if index != expected or sha(index) != manifest[label + "_index_sha256"]:
            raise ValueError("Public byte index is not the exact manifest.")
    if manifest["target_index_sha256"] != expected_target_index:
        raise ValueError("Public target revision changed before handoff.")
    archive = (source / "baseline.tar.gz").read_bytes()
    patch = (source / "observer.patch").read_bytes()
    if (
        sha(archive) != manifest["baseline_archive_sha256"]
        or sha(patch) != manifest["patch_sha256"]
    ):
        raise ValueError("Public archive/patch hash mismatch.")
    review_text(patch)
    licenses = manifest["license_paths"]
    if licenses != ["LICENSE.opencode", "harness/LICENSE"]:
        raise ValueError("Both actual original licenses must be retained.")
    with tarfile.open(source / "baseline.tar.gz", "r:gz") as tar:
        license_bytes = tar.extractfile("LICENSE.opencode").read(512 * 1024 + 1)
    baseline = {
        "schema_version": "tessera-opencode-observer-baseline-v2",
        "mode": "file-pinned-snapshot",
        "source_origin": "https://opencode.ai",
        "source_version": manifest["opencode_snapshot_version"],
        "source_reference": "OpenCode copied snapshot harness clean local commit "
        + manifest["harness_selected_commit"],
        "upstream_verification": "not_independently_verified",
        "source_date_epoch": int.from_bytes(archive[4:8], "little"),
        "baseline_archive_sha256": sha(archive),
        "baseline_files": maps["baseline"],
        "licenses": {name: maps["baseline"][name] for name in licenses},
        "license_path": "LICENSE.opencode",
        "target_files": maps["target"],
        "patch_sha256": sha(patch),
    }
    with tempfile.TemporaryDirectory(
        prefix="observer-map-", dir="/private/tmp/opencode" if sys.platform == "darwin" else None
    ) as directory:
        bundle = Path(directory)
        (bundle / "LICENSE").write_bytes(license_bytes)
        (bundle / "baseline.json").write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
        (bundle / "observer.patch").write_bytes(patch)
        (bundle / "baseline.tar.gz").write_bytes(archive)
        observer_bundle(bundle)
        with tarfile.open(bundle / "baseline.tar.gz") as tar:
            for member in tar:
                body = tar.extractfile(member).read()
                if len(body) != next(
                    row["size"] for row in manifest["baseline"] if row["path"] == member.name
                ):
                    raise ValueError("Public original size mismatch.")
                review_text(body)
                if (
                    member.name in licenses
                    and b"Permission is hereby granted, free of charge" not in body
                ):
                    raise ValueError(
                        "Actual original MIT grant missing; owner licensing review required."
                    )
        destination.mkdir(parents=True, exist_ok=True)
        final_names = ("baseline.json", "LICENSE", "observer.patch", "baseline.tar.gz")
        if any((destination / name).exists() for name in final_names):
            raise ValueError("Refusing to overwrite staged observer files.")
        for name in final_names:
            shutil.copyfile(bundle / name, destination / name)
    return {name: sha((destination / name).read_bytes()) for name in final_names}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--expected-target-index", required=True)
    args = parser.parse_args()
    print(
        json.dumps(stage(args.source, args.destination, args.expected_target_index), sort_keys=True)
    )
