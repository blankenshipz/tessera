"""The image-resident stage entry of gpu_runtime_v1 (ADR 0076): contract rules, runtime, launcher.

Offline. The launcher runs a real subprocess in a temporary directory; nothing contacts Salad,
Docker or a network.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from tessera_control.salad_workload_contract import (
    check_gpu_runtime_manifest_rules,
    image_item_refusal,
    is_image_item,
    is_input_item,
)

ROOT = Path(__file__).resolve().parent.parent / "workloads/gpu_runtime_v1"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = _load("gpu_runtime_v1_image_entry_runtime", ROOT / "runtime.py")
launcher = _load("gpu_runtime_v1_stage_argv", ROOT / "stage_argv.py")

ITEM = {"kind": "entry", "source": "image", "path": "stage_argv.py", "argv": ["python", "x.py"]}
SCHEMA = "tessera.gpu-runtime.manifest.v1"


def manifest(*items):
    return {"schema": SCHEMA, "fetches": list(items)}


# -- contract rules ----------------------------------------------------------------------------


def test_an_image_item_is_not_an_input_item_and_needs_no_input():
    assert is_image_item(ITEM) and not is_input_item(ITEM)
    assert check_gpu_runtime_manifest_rules(manifest(ITEM), input_declared=False) == []


@pytest.mark.parametrize(
    "item",
    [
        {**ITEM, "path": "other.py"},
        {**ITEM, "kind": "file"},
        {**ITEM, "argv": []},
        {**ITEM, "argv": ["python", 3]},
        {**ITEM, "argv": [""]},
        {**ITEM, "argv": ["x"] * 129},
        {**ITEM, "extra": 1},
        {k: v for k, v in ITEM.items() if k != "argv"},
    ],
)
def test_a_malformed_image_item_is_refused(item):
    assert image_item_refusal(item)
    with pytest.raises(ValueError):
        check_gpu_runtime_manifest_rules(manifest(item), input_declared=True)


def test_an_image_entry_must_be_the_only_item():
    wheel = {"url": "https://h.example/w.whl", "sha256": "a" * 64, "max_bytes": 1}
    row = {**wheel, "kind": "wheel", "dest": "w.whl"}
    with pytest.raises(ValueError, match="only item"):
        check_gpu_runtime_manifest_rules(manifest(ITEM, row), input_declared=False)
    second = {"kind": "entry", "source": "input", "path": "main.py"}
    with pytest.raises(ValueError, match="exactly one entry"):
        check_gpu_runtime_manifest_rules(manifest(ITEM, second), input_declared=True)


# -- runtime -----------------------------------------------------------------------------------


def test_the_runtime_accepts_the_image_entry_and_refuses_anything_else_in_that_shape():
    assert rt.validate_manifest(manifest(ITEM)) == manifest(ITEM)
    for bad in (
        {**ITEM, "path": "other.py"},
        {**ITEM, "argv": []},
        {**ITEM, "extra": True},
        {**ITEM, "kind": "file"},
    ):
        with pytest.raises(rt.Failure):
            rt.validate_manifest(manifest(bad))
    row = {
        "url": "https://h.example/w.whl",
        "sha256": "a" * 64,
        "max_bytes": 1,
        "kind": "wheel",
        "dest": "w.whl",
    }
    with pytest.raises(rt.Failure):
        rt.validate_manifest(manifest(ITEM, row))
    assert rt.url_items(manifest(ITEM)) == [] and rt.input_items(manifest(ITEM)) == []


def test_run_entry_passes_the_reviewed_argv_after_a_separator():
    seen = []

    class Proc:
        pid = 0

        def wait(self, timeout=None):
            return 0

    def runner(cmd, **kwargs):
        seen.append((cmd, kwargs["cwd"]))
        return Proc()

    import time

    rt.run_entry("/e.py", "/w", "/s", "/o", time.monotonic() + 30, runner=runner, args=["a", "b"])
    rt.run_entry("/e.py", "/w", "/s", "/o", time.monotonic() + 30, runner=runner)
    assert seen[0][0][-6:] == ["/e.py", "/w", "/o", "--", "a", "b"]
    assert seen[1][0][-3:] == ["/e.py", "/w", "/o"]


def test_execute_runs_the_shipped_entry_with_the_manifest_argv(tmp_path, monkeypatch):
    calls = []

    class Custody:
        def begin_once(self):
            return True

        def output_plan(self):
            return {"outputs": [], "max_bytes": 0}

    monkeypatch.setattr(rt, "validate_plan", lambda plan, request: [])
    monkeypatch.setattr(rt, "stage_outputs", lambda *a, **k: [])
    monkeypatch.setattr(rt, "upload_outputs", lambda custody, staged: [])
    monkeypatch.setattr(rt, "load_manifest", lambda request: manifest(ITEM))

    def entry_runner(entry, work, site, out_dir, deadline, **kwargs):
        calls.append((entry, kwargs))

    import time

    request = {"manifest_in_input": None, "input": None, "max_seconds": 120}
    monkeypatch.setattr(rt, "binding_of", lambda request: {})
    rt.execute(
        request,
        time.monotonic() + 120,
        work=str(tmp_path / "work"),
        custody_factory=lambda *a: Custody(),
        entry_runner=entry_runner,
    )
    assert calls == [(os.path.join(rt.IMAGE_ENTRY_DIR, "stage_argv.py"), {"args": ITEM["argv"]})]


# -- launcher ----------------------------------------------------------------------------------


def test_the_launcher_maps_the_container_mount_points():
    out = launcher.rewrite(
        ["py", "/inputs/a.py", "/inputs", "--data=/inputs/d", "--o=/outputs", "/outputs/x",
         "/inputsX", "--flag", "/etc/inputs", "-o/outputs/x"],
        "/w",
        "/o",
    )  # fmt: skip
    assert out == [
        "py", "/w/input/a.py", "/w/input", "--data=/w/input/d", "--o=/o", "/o/x",
        "/inputsX", "--flag", "/etc/inputs", "-o/outputs/x",
    ]  # fmt: skip


def test_the_launcher_runs_the_stage_argv_against_the_staged_input_and_propagates_the_code(
    tmp_path,
):
    work, out = tmp_path / "work", tmp_path / "out"
    (work / "input").mkdir(parents=True)
    out.mkdir()
    (work / "input" / "in.txt").write_text("hello")
    script = (
        "import sys; d=open(sys.argv[1]).read(); open(sys.argv[2],'w').write(d.upper()); "
        "sys.exit(int(sys.argv[3]))"
    )
    argv = [sys.executable, "-c", script, "/inputs/in.txt", "/outputs/r.txt", "3"]
    assert launcher.main([str(work), str(out), "--", *argv]) == 3
    assert (out / "r.txt").read_text() == "HELLO"
    assert launcher.main([str(work), str(out), "--", sys.executable, "-c", "pass"]) == 0
    assert launcher.main([str(work), str(out), "--", "/no/such/binary"]) == launcher.NOT_FOUND
    assert launcher.main([str(work), str(out)]) == 2
    kill = [sys.executable, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"]
    assert launcher.main([str(work), str(out), "--", *kill]) == 1
