"""Opt-in, resumable *admission* for a two-variant Kubernetes research run.

This is not a worker, a collector, or a scientific reviewer. Supply an existing
project/snapshot, registered worker, immutable node-resident image and explicit
control endpoint. The image must implement the two distinct bounded CPU commands
in the input manifest. The receipt remains private, mode 0600, and is written
after every API response so interrupted admission does not duplicate authority.
No credentials, token values, or output contents are written to the receipt.

Example: python scripts/check_two_variant_kubernetes_research.py --run \
  --manifest /private/tmp/tessera-research-input.json \
  --receipt /private/tmp/tessera-research-receipt.json \
  --owner-token-file /private/tmp/tessera-owner-token

The input manifest is operator-provided and MUST describe observed identities,
not inferred current-context identities. Invoke --phase baseline first, then
--phase intervention only after independently verified baseline output, normal
derived-measurement API, terminal execution and grant reconciliation. This script
deliberately stops before dispatch: a real worker/collector is still necessary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class AdmissionError(RuntimeError):
    pass


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise AdmissionError("manifest must be a JSON object")
    required = (
        "origin",
        "project_id",
        "snapshot_id",
        "snapshot_digest",
        "worker_principal_id",
        "pinned_image",
        "baseline_argv",
        "intervention_argv",
    )
    if any(not value.get(field) for field in required):
        raise AdmissionError("manifest is missing an explicit execution identity")
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{2,5}", value["origin"]):
        raise AdmissionError(
            "only an explicitly port-forwarded loopback control origin is supported"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", value["snapshot_digest"]):
        raise AdmissionError("snapshot digest must be sha256")
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", value["pinned_image"]):
        raise AdmissionError("node-resident image must have an immutable digest")
    commands = [value[name] for name in ("baseline_argv", "intervention_argv")]
    if any(
        not isinstance(cmd, list)
        or not cmd
        or any(not isinstance(arg, str) or not arg or len(arg) > 200 for arg in cmd)
        for cmd in commands
    ):
        raise AdmissionError("each CPU command must be a bounded argv list")
    if commands[0] == commands[1] or any("/outputs/result.txt" not in cmd for cmd in commands):
        raise AdmissionError(
            "variants require distinct commands writing the same bounded output path"
        )
    if commands != [
        ["/hello", "--cpu-sum-squares", "10", "/outputs/result.txt"],
        ["/hello", "--cpu-sum-squares", "20", "/outputs/result.txt"],
    ]:
        raise AdmissionError("commands must be the reviewed 10/20 sum-of-squares CPU pilot")
    return value


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    parent = path.parent.resolve(strict=True)
    if path.is_symlink():
        raise AdmissionError("receipt must not be a symlink")
    temporary = parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(receipt, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _api(
    origin: str,
    token: str,
    method: str,
    route: str,
    body: dict[str, Any] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Host": "localhost"}
    if key:
        headers["Idempotency-Key"] = key
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        origin + route,
        headers=headers,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=15
        ) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        if method == "GET" and route.endswith("/execution-limits") and error.code == 404:
            return {"not_configured": True}
        raise AdmissionError(
            f"{method} {route} rejected ({error.code}); inspect server-side records before retry"
        ) from None
    except (OSError, ValueError) as error:
        raise AdmissionError(
            f"{method} {route} did not return a verified response; retry same receipt/key only"
        ) from error
    if not isinstance(result, dict):
        raise AdmissionError(f"{route} returned non-object response")
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def admit(
    manifest: dict[str, Any], receipt_path: Path, token: str, *, phase: str = "baseline"
) -> dict[str, Any]:
    if phase not in {"baseline", "intervention", "derive-baseline", "derive-intervention"}:
        raise AdmissionError("phase must be baseline, intervention or derive-<variant>")
    # Changing the manifest after partial admission is not a retry.
    fingerprint = _digest(manifest)
    receipt = (
        json.loads(receipt_path.read_text())
        if receipt_path.exists()
        else {
            "manifest_sha256": fingerprint,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
            "steps": {},
        }
    )
    if receipt.get("manifest_sha256") != fingerprint or not isinstance(receipt.get("steps"), dict):
        raise AdmissionError("receipt does not match immutable input manifest")
    _write_receipt(receipt_path, receipt)
    nonce = fingerprint[:20]
    origin = manifest["origin"]

    def post(name: str, path: str, body: dict[str, Any], method: str = "POST") -> dict[str, Any]:
        if name not in receipt["steps"]:
            response = _api(origin, token, method, path, body, f"two-variant-{nonce}-{name}")
            receipt["steps"][name] = response
            _write_receipt(receipt_path, receipt)
        return receipt["steps"][name]

    # Read-only preflight before any additional execution authority is minted.
    project = manifest["project_id"]
    snapshot = manifest["snapshot_id"]
    snapshot_record = _api(origin, token, "GET", f"/snapshots/{snapshot}")
    if snapshot_record.get("digest") != manifest["snapshot_digest"]:
        raise AdmissionError("existing snapshot digest differs from declared input")
    if snapshot_record.get("project_id") != project:
        raise AdmissionError("existing snapshot is not in declared project")
    limits = _api(origin, token, "GET", f"/projects/{project}/execution-limits")
    if limits.get("not_configured") is True:
        limits = post(
            "limits",
            f"/projects/{project}/execution-limits",
            {
                "expected_revision": 0,
                "currency": "USD",
                "monetary_limit_cents": 0,
                "max_concurrency": 1,
            },
            "PUT",
        )
    if (
        limits.get("currency"),
        limits.get("monetary_limit_cents"),
        limits.get("max_concurrency"),
    ) != ("USD", 0, 1):
        raise AdmissionError(
            "project limits must already be zero-dollar, USD and one concurrent grant"
        )
    steps = receipt["steps"]
    variants = []
    for kind in ("baseline", "intervention"):
        argv = manifest[f"{kind}_argv"]
        task = post(
            f"{kind}-task",
            f"/projects/{project}/tasks",
            {
                "title": f"Two-variant CPU {kind}",
                "purpose": "Bounded public CPU comparison",
                "budget_cents": 0,
                "max_duration_seconds": 120,
            },
        )
        plan = post(
            f"{kind}-plan",
            f"/tasks/{task['id']}/plan",
            {
                "expected_revision": task["revision"],
                "title": task["title"],
                "purpose": task["purpose"],
                "budget_cents": 0,
                "max_duration_seconds": 120,
                "execution_spec": {
                    "argv": argv,
                    "input_snapshot_digest": manifest["snapshot_digest"],
                    "pinned_skill_digests": [],
                    "output_paths": ["result.txt"],
                    "profile": "kubernetes-opencode",
                    "network_enabled": False,
                    "max_output_bytes": 1024,
                },
            },
            "PUT",
        )
        approved = post(
            f"{kind}-approval",
            f"/tasks/{task['id']}/approve",
            {
                "expected_revision": plan["revision"],
                "expected_plan_digest": plan["current_plan_digest"],
            },
        )
        if approved.get("execution_granted") is not False:
            raise AdmissionError("plan approval unexpectedly granted execution")
        variants.append(
            {
                "kind": kind,
                "name": f"Public CPU {kind}",
                "method_digest": _digest(argv),
                "input_digest": manifest["snapshot_digest"],
                "metadata": {"protocol": "deterministic-cpu-v1", "output_path": "result.txt"},
                "execution_task_id": task["id"],
                "execution_plan_digest": approved["current_plan_digest"],
            }
        )
    experiment = post(
        "experiment",
        f"/projects/{project}/experiments",
        {
            "goal": "Compare bounded public CPU baseline and intervention outputs",
            "success_criterion": (
                "Two distinct retained outputs with independently derived values"
            ),
            "protocol": (
                "Two immutable commands; each attempt requires a separate grant, "
                "real worker execution and output evidence. Human review pending."
            ),
            "variants": variants,
        },
    )
    if phase.startswith("derive-"):
        kind = phase.removeprefix("derive-")
        if f"{kind}-admitted" not in steps:
            raise AdmissionError("variant has not been admitted")
        exported = _api(origin, token, "GET", f"/experiments/{experiment['id']}")
        admitted_id = steps[f"{kind}-admitted"]["id"]
        recorded = next(
            (
                item
                for item in exported.get("admitted_attempts", [])
                if item.get("id") == admitted_id
            ),
            None,
        )
        if (
            not recorded
            or recorded.get("outcome") != "succeeded"
            or not recorded.get("operation_id")
        ):
            raise AdmissionError("no succeeded operation-bound variant attempt")
        linked = [
            item["artifact_id"]
            for item in exported.get("artifact_links", [])
            if item.get("attempt_id") == admitted_id
        ]
        matching = [
            item
            for item in exported.get("artifacts", [])
            if item.get("id") in linked
            and item.get("operation_id") == recorded["operation_id"]
            and item.get("relative_path") == "result.txt"
        ]
        if len(matching) != 1:
            raise AdmissionError("expected one linked operation-bound result.txt artifact")
        derived = post(
            f"{kind}-derived",
            f"/experiment-attempts/{admitted_id}/derived-measurements",
            {
                "expected_revision": recorded["revision"],
                "name": "sum_squares",
                "artifact_id": matching[0]["id"],
                "json_pointer": "/sum_squares",
            },
        )
        expected = 385 if kind == "baseline" else 2870
        if derived.get("value") != expected or derived.get("artifact_id") != matching[0]["id"]:
            raise AdmissionError("derived server measurement contradicts expected CPU computation")
        return {
            "state": "derived_not_reviewed",
            "phase": phase,
            "attempt_id": admitted_id,
            "measurement_id": derived["id"],
            "receipt": str(receipt_path),
        }
    # Do not mint the intervention grant until the *actual* baseline authority
    # has been reconciled. A local receipt or an attempted worker command is
    # never accepted as terminal provider evidence.
    if phase == "intervention":
        baseline = steps.get("baseline-grant")
        attempt = steps.get("baseline-admitted")
        if not baseline or not attempt:
            raise AdmissionError("baseline must be admitted before intervention")
        observed = _api(origin, token, "GET", f"/execution-grants/{baseline['id']}")
        exported = _api(origin, token, "GET", f"/experiments/{experiment['id']}")
        recorded = next(
            (
                item
                for item in exported.get("admitted_attempts", [])
                if item.get("id") == attempt["id"]
            ),
            None,
        )
        if (
            observed.get("status") != "reconciled"
            or not recorded
            or recorded.get("outcome") != "succeeded"
        ):
            raise AdmissionError(
                "baseline has no reconciled grant and succeeded attributed attempt"
            )
        derived_receipt = steps.get("baseline-derived")
        measurement = next(
            (
                item
                for item in exported.get("measurements", [])
                if item.get("attempt_id") == attempt["id"]
                and derived_receipt
                and item.get("id") == derived_receipt.get("id")
                and item.get("name") == "sum_squares"
                and item.get("value") == 385
                and isinstance(item.get("artifact_id"), str)
            ),
            None,
        )
        linked = measurement and any(
            row.get("attempt_id") == attempt["id"]
            and row.get("artifact_id") == measurement["artifact_id"]
            for row in exported.get("artifact_links", [])
        )
        artifact = measurement and next(
            (
                row
                for row in exported.get("artifacts", [])
                if row.get("id") == measurement["artifact_id"]
                and row.get("operation_id") == recorded.get("operation_id")
            ),
            None,
        )
        if not measurement or not linked or not artifact:
            raise AdmissionError(
                "baseline lacks a linked operation-bound sum_squares measurement of 385"
            )
        if "intervention_expires_at" not in receipt:
            receipt["intervention_expires_at"] = (
                datetime.now(UTC) + timedelta(minutes=20)
            ).isoformat()
            _write_receipt(receipt_path, receipt)
    for kind in ("baseline",) if phase == "baseline" else ("intervention",):
        task = steps[f"{kind}-approval"]
        variant = next(v for v in experiment["design"]["variants"] if v["kind"] == kind)
        grant = post(
            f"{kind}-grant",
            f"/projects/{project}/execution-grants",
            {
                "task_id": task["id"],
                "expected_task_revision": task["revision"],
                "expected_plan_digest": task["current_plan_digest"],
                "worker_principal_id": manifest["worker_principal_id"],
                "snapshot_id": snapshot,
                "pinned_image": manifest["pinned_image"],
                "expires_at": receipt[
                    "expires_at" if kind == "baseline" else "intervention_expires_at"
                ],
                "max_exposure_cents": 0,
                "currency": "USD",
                "acknowledges_unmeasured_infrastructure": True,
            },
        )
        post(
            f"{kind}-admitted",
            f"/experiment-variants/{variant['id']}/admitted-attempts",
            {
                "expected_variant_revision": variant["revision"],
                "expected_design_revision": experiment["current_design_revision"],
                "grant_id": grant["id"],
            },
        )
    return {
        "state": "admitted_not_dispatched",
        "phase": phase,
        "experiment_id": experiment["id"],
        "attempt_ids": {
            kind: steps[f"{kind}-admitted"]["id"]
            for kind in ("baseline", "intervention")
            if f"{kind}-admitted" in steps
        },
        "receipt": str(receipt_path),
        "next": (
            "Package each attempt under a matching worker lease, dispatch with "
            "verified target, run real worker/collector, derive measurements "
            "from retained outputs; human review pending."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--owner-token-file", type=Path)
    parser.add_argument(
        "--phase",
        choices=("baseline", "intervention", "derive-baseline", "derive-intervention"),
        default="baseline",
    )
    args = parser.parse_args()
    if not args.run or not all((args.manifest, args.receipt, args.owner_token_file)):
        parser.error("explicit --run, --manifest, --receipt and --owner-token-file required")
    try:
        manifest = _manifest(args.manifest)
        if args.receipt.exists() and args.receipt.stat().st_mode & 0o077:
            raise AdmissionError("receipt must be owner-only (0600)")
        token = args.owner_token_file.read_text().strip()
        if not token or args.owner_token_file.stat().st_mode & 0o077:
            raise AdmissionError("owner credential file must be nonempty and owner-only (0600)")
        print(json.dumps(admit(manifest, args.receipt, token, phase=args.phase), sort_keys=True))
        return 0
    except (AdmissionError, KeyError, OSError, ValueError, TypeError) as error:
        # Never print lower-level exception content: it may include credential material.
        print(
            json.dumps(
                {
                    "state": "failed_or_partial",
                    "reason": str(error)
                    if isinstance(error, AdmissionError)
                    else type(error).__name__,
                    "next": (
                        "Inspect receipt and server-side events; replay same manifest "
                        "and receipt, never reset retained namespace."
                    ),
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
