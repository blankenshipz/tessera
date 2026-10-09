"""Cold installed Tessera adapter imports; absent prerequisites FAIL, never skip."""

import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("tree", type=Path)
args = parser.parse_args()
sys.path.insert(0, str(args.tree.resolve() / "harness/src"))
try:
    import owner_check_adapter
    import runtime_control_client

    from tessera_mcp.runtime_admission import NativeTerminalPort, RuntimeAdmissionVerifier

    assert callable(owner_check_adapter.RegisteredOwnerCheckAdapter)
    assert callable(runtime_control_client.RuntimeControlClient)
    assert callable(RuntimeAdmissionVerifier.verify_terminal)
    assert set(NativeTerminalPort.__dataclass_fields__) == {"installed_pins", "bind_verifier"}
except Exception:
    raise SystemExit(
        "installed Tessera/native adapter prerequisite missing; gate incomplete, not skipped"
    ) from None
print(
    json.dumps(
        {
            "actual_python_exports": 4,
            "mock_substitutions": 0,
            "provider_calls": 0,
            "claim": "actual adapter/verifier imports, not native authorization",
        }
    )
)
