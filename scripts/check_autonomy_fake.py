"""Explicit fake-effect demonstration; never a live sandbox activation."""

import argparse
import json
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Run one disposable synthetic action")
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({"state": "disabled", "effects": 0, "activation": "blocked"}))
        return
    # Source-checkout development fixture only; no production credential or target.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from test_autonomy import finish, setup_session, start
    from test_runtime_capability_broker import make_broker

    # Resolve the platform's temporary parent, not any researcher-selected path.
    # macOS /tmp is a symlink; the existing workspace guard correctly rejects it.
    with tempfile.TemporaryDirectory(
        prefix="tessera-autonomy-fake-", dir=Path(tempfile.gettempdir()).resolve()
    ) as root:
        fixture = make_broker.__wrapped__(Path(root))
        factory = next(fixture)
        try:
            h = factory()
            store, session = setup_session(h)
            bundle, final = finish(store, h, start(store, h, session))
            assert bundle["state"] == "completed" and final["state"] == "ready"
            print(
                json.dumps(
                    {
                        "state": final["state"],
                        "evidence": "synthetic",
                        "actions": len(final["actions"]),
                        "external_effects": 0,
                        "activation": "blocked",
                        "billing": None,
                        "receipt": final["actions"][0]["receipt"],
                    },
                    sort_keys=True,
                )
            )
        finally:
            fixture.close()


if __name__ == "__main__":
    main()
