"""The ``tessera-image-build`` wrapper (ADR 0123), driven with a fake ``buildah``.

Nothing here builds an image, pushes, reads an auth file's content or touches a network: the
fake records its command lines and writes the files the real tool would.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap

import pytest

from tessera_worker import builder_wrapper as wrapper
from tessera_worker.image_build import (
    buildah_commands,
    builder_argv,
    parse_build_result,
)

BASE = "docker.io/library/alpine@sha256:" + "b" * 64
OTHER = "docker.io/library/debian@sha256:" + "c" * 64
IMAGE_ID = "1234567890ab" + "c" * 52
PUSHED = "d" * 64
POLICY = {
    "builder_image": "registry.example.org/builder@sha256:" + "a" * 64,
    "context_input": "ctx",
    "dockerfile": "Dockerfile",
    "base_images": [BASE],
    "repository": "registry.example.org/team/app",
    "registry_host": "registry.example.org",
    "hosts": ["docker.io", "registry.example.org"],
    "push": True,
    "max_transfer_bytes": 1024**3,
}
FAKE = textwrap.dedent(
    """\
    #!{python}
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    config = json.load(open(os.path.join(here, "fake.json")))
    with open(os.path.join(here, "calls.jsonl"), "a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
    sub = sys.argv[1]
    if config.get("fail") == sub:
        print("boom", file=sys.stderr)
        sys.exit(3)
    work = config["work"]
    if sub == "bud" and not config.get("no_image_id"):
        open(os.path.join(work, "image-id"), "w").write("sha256:" + config["image_id"])
    if sub == "push":
        open(os.path.join(work, "digest"), "w").write("sha256:" + config["pushed"])
    if sub == "images":
        print(json.dumps([{"size": config.get("size", 4096)}]))
    """
)


class Box:
    """A temporary world: a context, a fake buildah on PATH, a fake auth file, a /work."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.bin = tmp_path / "bin"
        self.work = tmp_path / "work"
        self.context = tmp_path / "ctx"
        self.out = tmp_path / "out"
        for directory in (self.bin, self.work, self.context, self.out):
            directory.mkdir()
        self.auth = tmp_path / "auth.json"
        self.auth.write_text('{"auths": {"fake": {"auth": "ZmFrZQ=="}}}')
        self.auth.chmod(0o400)
        self.dockerfile = self.context / "Dockerfile"
        self.dockerfile.write_text(f"FROM {BASE}\nCOPY app.py /app.py\n")
        fake = self.bin / "buildah"
        fake.write_text(FAKE.replace("{python}", sys.executable))
        fake.chmod(0o755)
        self.configure()
        monkeypatch.setenv("PATH", f"{self.bin}:{os.environ['PATH']}")
        monkeypatch.setattr(wrapper, "WORK", str(self.work))
        monkeypatch.setattr(wrapper, "REGISTRY_AUTH_PATH", str(self.auth))

    def configure(self, **changes):
        document = {
            "work": str(self.work),
            "image_id": IMAGE_ID,
            "pushed": PUSHED,
            "size": 4096,
        } | changes
        (self.bin / "fake.json").write_text(json.dumps(document))

    def argv(self, push=True, **policy):
        merged = POLICY | policy | {"push": push}
        real = builder_argv(merged)
        out = []
        skip = False
        for word in real:
            if skip:
                skip = False
                continue
            if word == "--auth-file":
                out += [word, str(self.auth)]
                skip = True
            elif word == "--context":
                out += [word, str(self.context)]
                skip = True
            elif word == "--dockerfile":
                out += [word, str(self.dockerfile)]
                skip = True
            elif word == "--result":
                out += [word, str(self.out / "image-build.json")]
                skip = True
            elif word == "--log":
                out += [word, str(self.out / "image-build.log")]
                skip = True
            else:
                out.append(word)
        return out[1:]  # without the program name

    def calls(self):
        path = self.bin / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    @property
    def result(self):
        return self.out / "image-build.json"


@pytest.fixture
def box(tmp_path, monkeypatch):
    return Box(tmp_path, monkeypatch)


def run(box, argv=None):
    return wrapper.main(box.argv() if argv is None else argv)


# -- a successful build ------------------------------------------------------------------------


def test_a_pushing_build_runs_the_contract_commands_and_reports_the_pushed_digest(box):
    assert run(box) == 0
    calls = box.calls()
    assert [c[0] for c in calls] == ["pull", "bud", "push", "images"]
    document = json.loads(box.result.read_text())
    assert document["digest"] == PUSHED
    assert document["image"] == f"{POLICY['repository']}@sha256:{PUSHED}"
    assert document["pushed"] is True and document["base_images"] == [BASE]
    assert document["size_bytes"] == 4096 and document["schema"] == "tessera.image-build.v1"
    push = calls[2]
    assert f"--authfile={box.auth}" in push
    assert push[-1] == f"docker://{POLICY['repository']}:tessera-{IMAGE_ID[:12]}"
    assert "latest" not in push[-1]


def test_the_result_is_accepted_by_the_workers_strict_parser(box):
    """Control records exactly this row, so it is what lands on the receipt."""
    assert run(box) == 0
    row = parse_build_result(box.result.read_bytes(), POLICY)
    assert row["image"] == f"{POLICY['repository']}@sha256:{PUSHED}"
    assert row["digest"] == PUSHED and row["pushed"] is True


def test_the_wrapper_runs_the_same_commands_the_contract_documents(box):
    """The stdlib copy of the buildah steps must not drift from ``buildah_commands``."""
    assert run(box) == 0
    ours = [["buildah", *call] for call in box.calls()[:3]]
    expected = buildah_commands(POLICY, IMAGE_ID)
    normal = [
        [
            part.replace(str(box.auth), "/run/registry-auth/auth.json")
            .replace(str(box.work), "/work")
            .replace(str(box.context), "/inputs/ctx")
            for part in command
        ]
        for command in ours
    ]
    assert normal == expected


def test_a_build_that_does_not_push_reports_the_image_id_and_pushes_nothing(box):
    assert run(box, box.argv(push=False)) == 0
    assert [c[0] for c in box.calls()] == ["pull", "bud", "images"]
    document = json.loads(box.result.read_text())
    assert document["pushed"] is False and document["digest"] == IMAGE_ID
    parse_build_result(box.result.read_bytes(), POLICY | {"push": False})


# -- refusals before anything runs -------------------------------------------------------------


def test_a_pushing_build_without_the_operators_auth_file_is_refused(box):
    box.auth.unlink()
    assert run(box) == 2
    assert box.calls() == [] and not box.result.exists()


def test_the_auth_file_must_be_regular_and_the_operators_path(box, tmp_path):
    link = tmp_path / "link.json"
    link.symlink_to(box.auth)
    wrapper.REGISTRY_AUTH_PATH = str(link)
    argv = box.argv()
    argv[argv.index("--auth-file") + 1] = str(link)
    assert run(box, argv) == 2  # a symlink is not the mounted file
    other = box.argv()
    other[other.index("--auth-file") + 1] = str(tmp_path / "elsewhere.json")
    wrapper.REGISTRY_AUTH_PATH = str(box.auth)
    assert run(box, other) == 2  # another path is never the operator's
    assert box.calls() == []


def test_a_pushing_build_must_be_given_an_auth_file_and_a_plain_one_must_not(box):
    argv = box.argv()
    index = argv.index("--auth-file")
    assert run(box, argv[:index] + argv[index + 2 :]) == 2  # --push with no --auth-file
    plain = box.argv(push=False) + ["--auth-file", str(box.auth)]
    assert run(box, plain) == 2
    assert box.calls() == []


def test_the_auth_file_is_never_read_by_the_wrapper(box, monkeypatch):
    opened = []
    real_open = open

    def tracking(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking)
    assert run(box) == 0
    assert str(box.auth) not in opened
    assert "ZmFrZQ" not in (box.out / "image-build.log").read_text()


@pytest.mark.parametrize(
    "change",
    [
        ["--repository", "Registry.Example.org/team/app"],
        ["--repository", "registry.example.org/team/app@sha256:" + "a" * 64],
        ["--repository", "app"],
        ["--max-transfer-bytes", "0"],
        ["--max-transfer-bytes", str(3 * 1024**3)],
        ["--base-image", "alpine:latest"],
        ["--base-image", "docker.io/library/alpine@sha256:abc"],
        ["--result", "relative.json"],
        ["--log", "relative.log"],
    ],
)
def test_out_of_shape_arguments_are_refused(box, change):
    argv = box.argv()
    if change[0] == "--base-image":
        argv = [
            *argv[: argv.index("--base-image") + 1],
            change[1],
            *argv[argv.index("--base-image") + 2 :],
        ]
    else:
        argv[argv.index(change[0]) + 1] = change[1]
    assert run(box, argv) == 2
    assert box.calls() == []


def test_repeated_bases_and_unknown_flags_are_refused(box):
    argv = box.argv()
    assert run(box, [*argv, "--base-image", BASE]) == 2
    assert run(box, [*argv, "--privileged"]) == 2
    assert run(box, [a for a in argv if a != "--push"]) == 2
    assert box.calls() == []


def test_the_dockerfile_must_be_inside_the_context(box, tmp_path):
    outside = tmp_path / "Dockerfile.outside"
    outside.write_text(f"FROM {BASE}\n")
    argv = box.argv()
    argv[argv.index("--dockerfile") + 1] = str(outside)
    assert run(box, argv) == 2
    link = box.context / "Dockerfile.link"
    link.symlink_to(outside)
    argv[argv.index("--dockerfile") + 1] = str(link)
    assert run(box, argv) == 2  # a link out of the context
    dotted = box.argv()
    dotted[dotted.index("--dockerfile") + 1] = f"{box.context}/a/../Dockerfile"
    assert run(box, dotted) == 2
    assert box.calls() == []


@pytest.mark.parametrize(
    "text",
    [
        "FROM alpine:3.20\n",  # a tag, not a pinned base
        f"FROM {OTHER}\n",  # pinned, but not one of the stage's bases
        "ARG BASE\nFROM ${BASE}\n",  # a build argument can name anything
        f"FROM {BASE}\nFROM python:3.12\n",
        f"FROM {BASE} AS one\nFROM two\n",
        f"FROM {BASE}\nCOPY --from=docker.io/library/busybox /bin/sh /sh\n",
        f"FROM {BASE}\nCOPY --from=$SRC /x /x\n",
        f"FROM {BASE}\nRUN --mount=type=bind,from=evil.example/img,target=/m true\n",
        "COPY app.py /app.py\n",  # no FROM at all
        "\n\n# only a comment\n",
    ],
)
def test_a_dockerfile_that_would_fetch_anything_else_is_refused(box, text):
    box.dockerfile.write_text(text)
    assert run(box) == 2
    assert box.calls() == []


@pytest.mark.parametrize(
    "text",
    [
        f"FROM {BASE}\n",
        f"# build\nFROM --platform=linux/amd64 {BASE} AS build\nFROM build AS final\n",
        (
            f"FROM {BASE} AS build\nRUN true\nFROM scratch\n"
            "COPY --from=build /x /x\nCOPY --from=0 /y /y\n"
        ),
        f"FROM \\\n  {BASE}\n",
    ],
)
def test_a_dockerfile_using_only_the_pinned_bases_and_its_own_stages_is_accepted(box, text):
    box.dockerfile.write_text(text)
    assert run(box) == 0


# -- failures after start ----------------------------------------------------------------------


@pytest.mark.parametrize("step", ["pull", "bud", "push", "images"])
def test_a_failing_step_exits_one_and_writes_no_result(box, step):
    box.configure(fail=step)
    assert run(box) == 1
    assert not box.result.exists()
    assert "boom" in (box.out / "image-build.log").read_text()
    if step == "bud":
        assert "push" not in [c[0] for c in box.calls()]


def test_an_old_result_is_removed_so_a_failed_build_cannot_leave_it(box):
    box.result.write_text("stale")
    box.configure(fail="bud")
    assert run(box) == 1
    assert not box.result.exists()


def test_a_missing_or_malformed_image_id_or_digest_is_a_failure(box):
    box.configure(no_image_id=True)
    assert run(box) == 1
    box.configure(image_id="nothex")
    assert run(box) == 1
    box.configure(pushed="short")
    assert run(box) == 1
    assert not box.result.exists()


def test_an_image_over_the_transfer_budget_is_a_failure(box):
    box.configure(size=POLICY["max_transfer_bytes"] + 1)
    assert run(box) == 1
    assert not box.result.exists()


def test_stale_work_files_never_stand_in_for_this_builds(box):
    (box.work / "image-id").write_text("sha256:" + "e" * 64)
    (box.work / "digest").write_text("sha256:" + "f" * 64)
    box.configure(no_image_id=True)
    assert run(box) == 1  # the old image id was cleared, so nothing is reported


def test_the_log_is_bounded_and_the_result_file_is_a_regular_private_write(box, monkeypatch):
    monkeypatch.setattr(wrapper, "MAX_LOG_BYTES", 64)
    assert run(box) == 0
    assert (box.out / "image-build.log").stat().st_size <= 64
    mode = stat.S_IMODE(box.result.stat().st_mode)
    assert mode == 0o644 and not box.result.is_symlink()
    assert not [p for p in box.out.iterdir() if p.name.startswith(".result-")]


def test_buildah_gets_a_fixed_environment_and_no_ambient_secrets(box, monkeypatch):
    monkeypatch.setenv("REGISTRY_PASSWORD", "hunter2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "nope")
    env = wrapper._environment()
    assert "REGISTRY_PASSWORD" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["BUILDAH_ISOLATION"] == "chroot" and env["STORAGE_DRIVER"] == "vfs"


def test_the_wrapper_is_standard_library_only():
    import ast

    tree = ast.parse(open(wrapper.__file__, encoding="utf-8").read())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}
