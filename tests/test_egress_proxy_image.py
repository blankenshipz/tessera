"""The egress proxy image recipe stays minimal, pinned and stdlib-only (ADR 0034)."""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "Dockerfile.egress-proxy"
PROXY = ROOT / "src/tessera_worker/egress_proxy.py"


def instructions():
    lines = [line.strip() for line in RECIPE.read_text().splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def test_base_image_is_digest_pinned_and_shared_with_the_main_image():
    base = [line for line in instructions() if line.startswith("FROM ")]
    assert len(base) == 1
    assert re.fullmatch(r"FROM [a-z0-9./:_-]+@sha256:[0-9a-f]{64}", base[0])
    assert base[0].split()[1] in (ROOT / "Dockerfile").read_text()


def test_recipe_packages_only_the_proxy_and_runs_non_root():
    copies = [line for line in instructions() if line.startswith(("COPY", "ADD"))]
    assert copies == [
        "COPY src/tessera_worker/egress_proxy.py /opt/tessera/tessera_worker/egress_proxy.py"
    ]
    assert "USER 65532:65532" in instructions()
    runs = " ".join(line for line in instructions() if line.startswith("RUN"))
    for forbidden in ("apt", "pip", "uv ", "curl", "wget", "apk"):
        assert forbidden not in runs
    assert instructions()[-1] == 'ENTRYPOINT ["python", "-m", "tessera_worker.egress_proxy"]'


def test_packaged_proxy_imports_only_the_standard_library():
    tree = ast.parse(PROXY.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports would break the single-file image"
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}
