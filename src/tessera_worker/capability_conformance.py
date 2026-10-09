"""Opt-in offline plan/schema command. Live effects remain a separate closed boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from tessera_control.capability_models import Digest

from .capability_activation import Frozen
from .capability_runner_contract import RunnerPlan


class DockerDesktopCapabilityConformance(Frozen):
    version: Literal[1] = 1
    enabled: Literal[True]
    context: Literal["docker-desktop"]
    server: Literal["https://127.0.0.1:6443"]
    cluster_ca_sha256: Digest
    # Runtime references only. This command never opens them or discovers credentials.
    dispatcher_credential_ref: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,100}$")
    observer_credential_ref: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,100}$")
    fixture: Literal["harmless-pure-wheel-v1"]
    max_requests: int = Field(ge=1, le=128)
    max_seconds: int = Field(ge=1, le=120)
    retries: Literal[0]
    plan: RunnerPlan

    @field_validator("enabled", mode="before")
    @classmethod
    def boolean_opt_in(cls, value):
        if value is not True:
            raise ValueError("explicit opt-in required")
        return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--schema", action="store_true")
    modes.add_argument("--render", action="store_true")
    modes.add_argument("--live", action="store_true")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if not any((args.schema, args.render, args.live)):
        print("disabled: explicit --schema or --render required; no Kubernetes effects")
        return 0
    if args.schema:
        print(json.dumps(DockerDesktopCapabilityConformance.model_json_schema(), sort_keys=True))
        return 0
    if args.live:
        # Refuse before config/credential access. JSON cannot establish conformance.
        print("blocked: live collector, runner image and independent enforcement are not validated")
        return 2
    try:
        if args.config is None:
            raise ValueError
        with args.config.open("rb") as source:
            content = source.read(512 * 1024 + 1)
        if len(content) > 512 * 1024:
            raise ValueError
        config = DockerDesktopCapabilityConformance.model_validate_json(content)
        print(
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "List",
                    "items": config.plan.objects() + [config.plan.job()],
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception:
        print("blocked: invalid bounded conformance config")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
