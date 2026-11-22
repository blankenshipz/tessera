"""Opt-in harmless Docker Desktop conformance. Default invocation has zero effects."""

from __future__ import annotations

import argparse
import json
import secrets
import time
from pathlib import Path

from tessera_worker.local_docker_contract import FixturePlan, LocalFixtureConfig
from tessera_worker.local_docker_fixture import FixtureJournal, LocalFixtureAdapter, recover
from tessera_worker.local_docker_namespace import ATTESTATION, STRONG_CLASS, WEAK_CLASS

PROFILES = {
    STRONG_CLASS: "local-networkless-fixture-v1",
    WEAK_CLASS: "local-networkless-fixture-engine-attested-v1",
}


CASES = ("success", "failure", "cancel", "timeout", "broker-loss")


def run_case(root, image, case, attestation_class=STRONG_CLASS):
    config = LocalFixtureConfig(enabled=True, attestation_class=attestation_class)
    mode = case if case in {"success", "failure"} else "wait"
    plan = FixturePlan(
        profile=PROFILES[attestation_class],
        operation_id=secrets.token_hex(32),
        nonce=secrets.token_hex(32),
        image=image,
        mode=mode,
        runtime_seconds=10,
    )
    handle = LocalFixtureAdapter(config).launch(plan, root)
    started = time.monotonic()
    disconnected = False
    cancelled = False
    while handle.process.is_alive() and time.monotonic() - started < 25:
        elapsed = time.monotonic() - started
        if case == "broker-loss" and elapsed >= 3 and not disconnected:
            handle.disconnect()
            disconnected = True
        if not disconnected:
            try:
                if case == "cancel" and elapsed >= 3 and not cancelled:
                    handle.send("cancel")
                    cancelled = True
                elif not cancelled:
                    handle.send()
            except (BrokenPipeError, EOFError, OSError):
                disconnected = True
        time.sleep(0.25)
    handle.process.join(timeout=1)
    if handle.process.is_alive():
        # Do not kill the supervising process or erase an uncertain operation.
        raise RuntimeError("supervisor still alive; preserve exact evidence for recovery")
    if not disconnected:
        handle.disconnect()
    journal = FixtureJournal(root)
    try:
        report = journal.report()
    finally:
        journal.close()
    # A fresh process/journal owner reads history, never reruns an effect. Also
    # exercises idempotent recovery after completed cleanup and lost broker reply.
    recovered = recover(root, config)
    if recovered != report:
        report = recovered
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--recover", type=Path)
    # Exact value, --run only, never an automatic fallback from the strong class.
    parser.add_argument("--attestation-class", choices=sorted(PROFILES), default=None)
    # Exact case names, --run only. A subset can only ever report a partial state.
    parser.add_argument(
        "--cases", default=None, help="comma-separated subset of: " + ", ".join(CASES)
    )
    args = parser.parse_args(argv)
    selected = list(CASES)
    if args.cases is not None:
        selected = args.cases.split(",")
        if (
            not args.run
            or args.recover
            or not selected
            or len(set(selected)) != len(selected)
            or any(case not in CASES for case in selected)
        ):
            parser.error("--cases needs --run and distinct names from: " + ", ".join(CASES))
    if args.attestation_class is not None and (not args.run or args.recover):
        parser.error("--attestation-class is valid only with --run (and not --recover)")
    attestation_class = args.attestation_class or STRONG_CLASS
    if not args.run:
        print(json.dumps(dict(state="disabled", effects=0, activation="blocked")))
        return 0
    try:
        if args.recover:
            report = recover(args.recover, LocalFixtureConfig(enabled=True))
            print(json.dumps(report, sort_keys=True))
            return 0 if report["cleanup_confirmed"] else 1
        if not args.image or not args.evidence_dir:
            parser.error("--run requires --image and a fresh --evidence-dir")
        # Reject malformed/mutable image and unsafe destination before any effects.
        FixturePlan(
            profile=PROFILES[attestation_class],
            operation_id="0" * 64,
            nonce="0" * 64,
            image=args.image,
            mode="success",
        )
        root = args.evidence_dir
        if not root.is_absolute() or root.resolve() != root or not root.parent.is_dir():
            raise ValueError("invalid evidence parent")
        root.mkdir(mode=0o700)
        reports = {}
        expected = dict(
            success="completed",
            failure="failed",
            cancel="cancelled",
            timeout="unknown",
            **{"broker-loss": "unknown"},
        )
        for case, state in expected.items():
            if case not in selected:
                continue
            report = run_case(root / case, args.image, case, attestation_class)
            reports[case] = report
            # Refuse further work on missing prerequisites or uncertain cleanup.
            if (
                report.get("attestation_class") != attestation_class
                or report["state"] != state
                or not report["termination_confirmed"]
                or not report["cleanup_confirmed"]
            ):
                break
        ok = len(reports) == len(selected) and all(
            r["state"] == expected[c]
            and r["cleanup_confirmed"]
            and r["termination_confirmed"]
            and r.get("attestation_class") == attestation_class
            for c, r in reports.items()
        )
        # A weak-class result is never reported as plain "passed".
        passed = "passed" if attestation_class == STRONG_CLASS else "passed_weak_attestation"
        if len(selected) != len(CASES):
            passed = "partial_" + passed
        result = dict(
            state=passed if ok else "blocked",
            cases_selected=selected,
            attestation_class=attestation_class,
            observer_independence=ATTESTATION[attestation_class]["observer_independence"],
            activation="blocked",
            evidence_directory=str(root),
            cases=reports,
        )
        # No-overwrite summary. SQLite journals retain the original receipt chain.
        with (root / "summary.json").open("x") as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
        concise = result | {
            "cases": {
                case: {key: value for key, value in report.items() if key != "records"}
                for case, report in reports.items()
            }
        }
        print(json.dumps(concise, sort_keys=True))
        return 0 if ok else 1
    except Exception:
        # Never print exception input/command/provider stderr or credential values.
        print(
            json.dumps(
                dict(state="blocked", reason="fixture_unavailable_or_unknown", activation="blocked")
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
