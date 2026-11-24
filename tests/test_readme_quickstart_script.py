"""Pure parts of the README quickstart checker; no service is started."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "check_readme_quickstart", ROOT / "scripts/check_readme_quickstart.py"
)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


def test_script_commands_match_current_readme():
    script.check_readme_matches((ROOT / "README.md").read_text())


def test_drifted_readme_is_rejected():
    text = "### Local desktop/control setup\n\n```sh\nuv sync\n```\n\n## Next\n"
    assert script.readme_quickstart_commands(text) == ["uv sync"]
    with pytest.raises(ValueError, match="no longer documents"):
        script.check_readme_matches(text)
    with pytest.raises(ValueError, match="no 'Local desktop"):
        script.readme_quickstart_commands("nothing")


def test_free_port_is_loopback_usable_and_commands_are_locked():
    assert 0 < script.free_loopback_port() < 65536
    assert script.tessera("init")[:4] == ["uv", "run", "--locked", "tessera"]


def test_workflow_calls_the_script():
    workflow = (ROOT / ".github/workflows/readme-quickstart.yml").read_text()
    assert "scripts/check_readme_quickstart.py" in workflow
