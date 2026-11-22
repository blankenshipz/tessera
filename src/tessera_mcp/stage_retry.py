"""Build a retry spec for the stages of a finished campaign that did not succeed (ADR 0055).

The builder lives in ``tessera_control.campaign_rerun`` (ADR 0069), where the control plane's
own re-run uses it too; this module keeps the offline MCP entry points. It reads only and
submits nothing: the agent validates and submits the result like any spec, and a human approves
its digest.
"""

from __future__ import annotations

from tessera_control.campaign_rerun import FINISHED, MAX_NAME, RerunRefused, build_retry_spec

RetryRefused = RerunRefused

__all__ = ["FINISHED", "MAX_NAME", "RetryRefused", "build_retry_spec"]
