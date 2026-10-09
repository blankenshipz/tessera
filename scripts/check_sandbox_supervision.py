"""One harmless opt-in local sandbox kill conformance; no activation authority."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from pathlib import Path

from tessera_worker.local_docker_contract import FixturePlan, LocalFixtureConfig
from tessera_worker.local_docker_engine import DockerCLI, LocalDockerEngine
from tessera_worker.local_docker_fixture import FixtureJournal
from tessera_worker.local_docker_guardian import GuardedFixtureAdapter
from tessera_worker.local_docker_namespace import NamespaceUnknown


def save(root, name, value):
    with (root / name).open("x") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)


def preflight(root, plan):
    """Read-only Docker discovery, exclusive evidence intent, zero containers."""
    root.mkdir(mode=0o700)  # existing evidence is never permission for a retry
    private = root / "docker-config"
    private.mkdir(mode=0o700)
    engine = LocalDockerEngine(DockerCLI(LocalFixtureConfig(enabled=True), private))
    evidence = dict(
        state="blocked",
        activation="blocked",
        attempts=0,
        automatic_retry=False,
        operation_id=plan.operation_id,
        nonce=plan.nonce,
        package_digest=plan.digest,
        host_system=os.uname().sysname,
    )
    try:
        evidence["image_id"] = engine.preflight(plan)
        fields = (
            "OperatingSystem",
            "OSType",
            "Architecture",
            "KernelVersion",
            "ServerVersion",
            "CgroupVersion",
            "DefaultRuntime",
            "SecurityOptions",
        )
        template = "{" + ",".join(json.dumps(k) + ":{{json ." + k + "}}" for k in fields) + "}"
        evidence["runtime"] = json.loads(engine.run(["info", "--format", template], timeout=5))
        evidence["namespace_observer"] = engine.namespace_preflight()
        evidence["state"] = "passed"
    except NamespaceUnknown as error:
        evidence["gate"] = str(error)
    except Exception:
        evidence["gate"] = "docker_or_image_preflight_unavailable"
    save(root, "preflight.json", evidence)
    return evidence


def run_once(root, image):
    plan = FixturePlan(
        operation_id=secrets.token_hex(32),
        nonce=secrets.token_hex(32),
        image=image,
        mode="wait",
        runtime_seconds=10,
    )
    if not root.is_absolute() or root.resolve() != root or not root.parent.is_dir():
        raise ValueError("fresh canonical evidence root required")
    checked = preflight(root, plan)
    if checked["state"] != "passed":
        result = checked | dict(evidence_directory=str(root), report={})
        save(root, "supervision-summary.json", result)
        return result
    save(root, "run-intent.json", dict(cases=1, mode="wait", automatic_retry=False))
    case_root = root / "case"
    handle = GuardedFixtureAdapter(LocalFixtureConfig(enabled=True)).launch(plan, case_root)
    deadline = time.monotonic() + 40
    try:
        while handle.process.is_alive() and time.monotonic() < deadline:
            try:
                handle.send()
            except (OSError, EOFError):
                break
            handle.process.join(timeout=0.25)
        handle.process.join(timeout=max(0, deadline - time.monotonic()))
        if handle.process.is_alive():
            raise RuntimeError("guardian unresolved; preserve evidence, do not retry")
        exitcode = handle.process.exitcode
    finally:
        handle.disconnect()
    handle.process.close()
    journal = FixtureJournal(case_root)
    try:
        report = journal.report()
    finally:
        journal.close()
    observations = {k: v["observation"] for k, v in report["records"].items()}
    # Unknown execution is expected for an intentionally hung agent, never success.
    # Kill dispatch alone cannot pass: exact exited-137 readback and cleanup needed.
    passed = (
        exitcode == 0
        and report["state"] == "unknown"
        and report["termination_confirmed"]
        and report["cleanup_confirmed"]
        and observations.get("limits", {}).get("stage") == "ready"
        and observations.get("network", {}).get("state") == "verified"
        and "released" in observations
        and "guardian_armed" in observations
        and "guardian_observed" in observations
        and observations.get("stop_confirmed", {}).get("exit_code") == 137
        and observations.get("outcome_intent", {}).get("reason") == "deadline"
        and "custody" not in observations
    )
    result = dict(
        state="passed" if passed else "blocked",
        activation="blocked",
        attempts=1,
        evidence_directory=str(root),
        report=report,
    )
    save(root, "supervision-summary.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps(dict(state="disabled", effects=0, activation="blocked")))
        return 0
    if not args.image or not args.evidence_dir:
        parser.error("--run requires an already-local digest image and fresh --evidence-dir")
    try:
        result = run_once(args.evidence_dir, args.image)
    except Exception:
        print(
            json.dumps(dict(state="blocked", reason="unavailable_or_unknown", activation="blocked"))
        )
        return 1
    print(
        json.dumps(
            result | {"report": {k: v for k, v in result["report"].items() if k != "records"}},
            sort_keys=True,
        )
    )
    return 0 if result["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
