"""Opt-in local config validation only. No network, credential resolution or activation."""

import argparse
import json
from pathlib import Path

from .salad_proxy_contract import ProxyPolicy, safety_projection


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-config", type=Path)
    args = parser.parse_args(argv)
    result = safety_projection() | {"effects": 0, "configuration": "not_read"}
    code = 0
    if args.validate_config:
        try:
            # Bound the actual read, not just stat; never print supplied values/errors.
            with args.validate_config.open("rb") as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise ValueError("oversized config")
            ProxyPolicy.model_validate_json(raw)
            result["configuration"] = "valid_offline_only"
        except Exception:
            result["configuration"] = "invalid"
            code = 2
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
