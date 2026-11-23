#!/usr/bin/env python3
"""End-to-end check: an agent steers a RUNNING local-docker campaign through tessera_mcp only.

    scripts/mcp_steering_running_check.py            # dry run: the plan and the exact spec
    scripts/mcp_steering_running_check.py --execute \\
        --confirm mcp-steering-running-check-local-docker \\
        --smoke-credentials-dir /abs/DIR --control-url https://CONTROL \\
        --image REGISTRY/IMAGE@sha256:DIGEST --receipt-file /abs/new-report.json

Uses the same owner-issued smoke credentials as scripts/mcp_steering_smoke.py. See
tessera_mcp.steering_running_check for what it does and what it never does.
"""

from tessera_mcp.steering_running_check import main

if __name__ == "__main__":
    raise SystemExit(main())
