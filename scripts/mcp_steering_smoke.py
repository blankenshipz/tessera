#!/usr/bin/env python3
"""A $0 end-to-end smoke of steering a campaign through tessera_mcp (dry run by default).

    scripts/mcp_steering_smoke.py                      # print the plan and the spec, do nothing
    scripts/mcp_steering_smoke.py --execute --confirm mcp-steering-smoke-local-docker \\
        --control-url https://CONTROL --project-id PROJECT --destination DEST \\
        --agent-token-file /abs/agent.token --person-token-file /abs/person.token \\
        --image REGISTRY/IMAGE@sha256:DIGEST [--ca-file CA] [--receipt-file /abs/report.json]
    # or, with credentials issued by `tessera dev smoke-credentials issue` ($0, local-docker only):
    scripts/mcp_steering_smoke.py --execute --confirm mcp-steering-smoke-local-docker \\
        --control-url https://CONTROL --smoke-credentials-dir /abs/DIR \\
        --image REGISTRY/IMAGE@sha256:DIGEST

See tessera_mcp.steering_smoke for what it does and what it never does.
"""

from tessera_mcp.steering_smoke import main

if __name__ == "__main__":
    raise SystemExit(main())
