"""The image build (builder image) recipe stays minimal and unpinned-by-design (ADR 0123)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "Dockerfile.image-build"
WRAPPER = "src/tessera_worker/builder_wrapper.py"


def instructions():
    lines = [line.strip() for line in RECIPE.read_text().splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def test_base_is_an_operator_supplied_arg_without_a_default():
    ins = instructions()
    assert "ARG BUILDAH_IMAGE" in ins
    assert not any(line.startswith("ARG BUILDAH_IMAGE=") for line in ins)
    assert [line for line in ins if line.startswith("FROM ")] == ["FROM ${BUILDAH_IMAGE}"]
    assert ins.index("ARG BUILDAH_IMAGE") < ins.index("FROM ${BUILDAH_IMAGE}")
    assert not any("@sha256:" in line for line in ins)


def test_only_the_wrapper_is_copied_and_nothing_is_added():
    ins = instructions()
    assert [line for line in ins if line.startswith("COPY")] == [
        f"COPY {WRAPPER} /usr/local/bin/tessera-image-build"
    ]
    assert not any(line.startswith("ADD ") for line in ins)
    assert "chmod 0555 /usr/local/bin/tessera-image-build" in " ".join(ins)


def test_runs_non_root_with_no_entrypoint_and_no_privilege():
    ins = instructions()
    assert "USER 65532:65532" in ins
    assert not any(line.startswith(("ENTRYPOINT", "CMD")) for line in ins)
    assert "--privileged" not in RECIPE.read_text()
    assert "--setopt=install_weak_deps=False python3" in " ".join(ins)
    assert "dnf clean all" in " ".join(ins)


def test_no_credential_like_env_or_arg_names_or_values():
    for line in instructions():
        if line.startswith(("ENV ", "ARG ")):
            for word in ("TOKEN", "PASSWORD", "SECRET", "KEY", "AUTH"):
                assert word not in line.upper(), line


def test_storage_conf_and_environment():
    text = " ".join(instructions())
    for part in (
        "'driver = \"vfs\"'",
        "'graphroot = \"/work/storage\"'",
        "'runroot = \"/work/run\"'",
    ):
        assert part in text
    assert "> /etc/containers/storage.conf" in text
    for env in (
        "HOME=/work",
        "TMPDIR=/work/tmp",
        "BUILDAH_ISOLATION=chroot",
        "STORAGE_DRIVER=vfs",
        "PYTHONDONTWRITEBYTECODE=1",
    ):
        assert env in text


def test_no_latest_tag_and_no_personal_paths():
    text = RECIPE.read_text()
    assert not re.search(r":latest\b", text)
    assert "/home/" not in text and "/Users/" not in text and "/workspace/" not in text


def test_dockerignore_lets_the_wrapper_through():
    patterns = (ROOT / ".dockerignore").read_text().split()
    assert patterns[0] == "**"
    assert "!src/" in patterns and "!src/**" in patterns


def test_copy_source_path_matches_the_recipe_layout():
    assert Path(WRAPPER).parts[:2] == ("src", "tessera_worker")


def test_wrapper_source_exists_in_the_repository():
    # Separate on purpose: fails only until the wrapper module lands.
    assert (ROOT / WRAPPER).is_file()
