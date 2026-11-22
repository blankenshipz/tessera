"""Item 3: observed run receipt -> derived metric -> gate verdict -> lineage join.

Two REAL local-Docker runs (generic `python -c` program, local image only) of two
experiment-admitted attempts of one baseline variant, driven through the public surface
(`real_run.py`): the worker CLI really executes each admitted grant, and everything else
is HTTP. Each step reports PASS, or an honest GAP/FAIL with the exact refusal.

What is automatic and what is human-registered is stated per case:

* AUTOMATIC (worker -> control, no human): the observed run receipt
  (`POST /worker/external-operations/{op}/run-receipt`), the sealed/quarantined output
  manifest, the operation/attempt binding at package time.
* HUMAN routes over real ids: grant reconcile, attempt resolve, artifact link, derived
  measurement (the SERVER reads the retained bytes), split lock/binding, metric record,
  replicate group (server selects members, computes mean/SD/CI), plan/evaluation/source-use
  reviews, and every lineage node and edge.
* The lineage join is a human-registered join of real ids; nothing emits lineage.

This makes NO external network request (local Docker, loopback HTTP and the local
SQLite state only), so it is not gated by TESSERA_PV_LIVE_EGRESS.

These checks are partly white-box, not purely public surface: the public HTTP surface and the
worker CLI drive everything, but expected digests (command, evaluation protocol, canonical
digests) are computed by importing `tessera_control` internals in-process, and a few cases
read the SQLite state read-only. Status and GAP/FAIL rule: a refusal or non-2xx from a route
that must work for a real run (result checkpoint, go resolve, ...) is a FAIL (regression); GAP
is kept only for design limits stated exactly (control stores no seal digest; no automatic
lineage emission; no lineage kinds for measurements).

Nothing here is science: the program writes a byte count and a random score so the
derivation is checkable against the real bytes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys

from .common import REPO, Context, Result, db_rows
from .real_run import RealRun, local_python_image

CAPABILITY = "receipts-lineage"

PROGRAM = r"""
import json, os
data = bytes(range(256)) * 16
open("/outputs/blob.bin", "wb").write(data)
score = os.urandom(1)[0] / 255
open("/outputs/result.json", "w").write(
    json.dumps({"metrics": {"bytes": len(data), "score": score}})
)
"""
OUTPUTS = ["result.json", "blob.bin"]
METRICS = [{"name": "score", "partition": "test", "unit": "fraction"}]


def _canon(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _s(value: object, n: int = 12) -> str:
    return str(value)[:n]


class Case:
    """Collects results; a crashed step is a FAIL carrying the exact error."""

    def __init__(self) -> None:
        self.out: list[Result] = []

    def add(self, case: str, status: str, evidence: str) -> None:
        self.out.append(Result(CAPABILITY, case, status, evidence))

    def guard(self, case: str, fn):
        try:
            return fn()
        except Exception as error:  # noqa: BLE001 - report the exact refusal, keep going
            self.add(case, "FAIL", f"{type(error).__name__}: {error}")
            return None


def run(ctx: Context) -> list[Result]:
    c = Case()
    if shutil.which("docker") is None:
        return [Result(CAPABILITY, "prerequisites", "GAP", "docker CLI not found; not run")]
    image = local_python_image()
    if image is None:
        return [
            Result(CAPABILITY, "prerequisites", "GAP", "no local python@sha256 image; never pulls")
        ]
    ctx.start_service()
    driver = RealRun(ctx, image)
    try:
        driver.setup()
        _chain(ctx, driver, c)
    except Exception as error:  # noqa: BLE001
        c.add("driver-error", "FAIL", f"{type(error).__name__}: {error}")
    finally:
        left = driver.cleanup()
        status, evidence = driver.residue_report(left)
        c.add("docker-residue", status, evidence)
    return c.out


def _chain(ctx: Context, driver: RealRun, c: Case) -> None:
    pid = driver.project["id"]
    call = ctx.call
    experiment = driver.plan_experiment(PROGRAM, output_paths=OUTPUTS)
    exp_variant = driver.exp_variant("baseline")

    # -- gate prerequisites that MUST precede dispatch: split lock, audits, binding ----------
    test_keys = [f"t{i}" for i in range(6)]
    lock = call(
        "POST",
        f"/projects/{pid}/split-locks",
        {
            "name": "generic split v1",
            "partitions": {
                "train": {
                    "keys": [f"r{i}" for i in range(12)],
                    "groups": [f"g{i}" for i in range(12)],
                },
                "test": {"keys": test_keys, "groups": [f"s{i}" for i in range(6)]},
            },
        },
    )
    corpus = call(
        "POST",
        f"/projects/{pid}/split-locks",
        {"name": "generic reference corpus", "partitions": {"corpus": {"keys": ["c1", "c2"]}}},
    )
    cross = call(
        "POST",
        f"/split-locks/{lock['id']}/leakage-audits",
        {
            "reference_lock_id": corpus["id"],
            "reference_partitions": ["corpus"],
            "eval_partitions": ["test"],
        },
    )
    records: list = []
    measured: list[dict] = []
    binding_ok = True
    for seed in range(2):
        # One unresolved grant per task: the second attempt is admitted after the first
        # grant is reconciled. Each split binding still precedes that attempt's dispatch.
        rec = driver.admit_run("baseline")
        records.append(rec)
        view = driver.attempt_view(experiment["id"], rec.attempt["id"])
        bound = c.guard(
            "split-binding",
            lambda view=view, seed=seed, rec=rec: call(
                "POST",
                f"/experiment-attempts/{rec.attempt['id']}/split-binding",
                {
                    "expected_revision": view["revision"],
                    "split_lock_id": lock["id"],
                    "seed": seed,
                    "metrics": METRICS,
                },
            ),
        )
        binding_ok = binding_ok and bound is not None
        if seed == 0:
            c.add(
                "gate-prereqs-pre-dispatch",
                "PASS" if binding_ok and cross["status"] == "clean" else "FAIL",
                f"split lock {_s(lock['id'][-8:], 8)} within_lock="
                f"{lock['within_lock_audit']['status']} membership "
                f"{_s(lock['membership_digest'])}; cross-set audit vs corpus={cross['status']}; "
                "each admitted attempt (state admitted, grant-bound) is split-bound with its "
                "seed BEFORE its worker run",
            )
        driver.execute(rec)
        if not rec.quarantined:
            c.add(
                f"real-run-{seed}",
                "FAIL",
                f"worker rc={rec.worker_returncode} state={rec.worker.get('state')} "
                f"reason={rec.worker.get('reason')} detail={rec.worker.get('detail')} "
                f"stderr={rec.worker_stderr}",
            )
            return
        if seed == 0:
            _receipt_cases(ctx, driver, rec, c)
        got = c.guard(
            f"finish-attempt-{seed}",
            lambda rec=rec, seed=seed: _finish_attempt(ctx, driver, rec, seed, c),
        )
        if got is None:
            return
        measured.append(got)
    _derived_case(ctx, driver, records[0], measured[0], c)
    acceptance = c.guard(
        "result-acceptance", lambda: _acceptance(ctx, driver, records[0], measured[0], c)
    )
    del acceptance
    group = c.guard("replicate-group", lambda: _group(ctx, driver, exp_variant, lock, measured, c))
    if group is None:
        return
    c.guard(
        "gate-verdict",
        lambda: _verdict(ctx, driver, experiment, exp_variant, lock, corpus, group, measured, c),
    )
    c.guard("lineage-join", lambda: _lineage(ctx, driver, records, measured, group, c))


def _tamper_cases(ctx: Context, driver: RealRun, op: str, receipt: dict, c: Case) -> None:
    """Control must refuse a receipt that disagrees with its own records (worker credential).

    The faithful body must replay as 200. Each variant changes one binding and keeps every
    other field valid (the receipt digest is recomputed where the payload changes), so the
    refusal can only come from control's verification, whose message differs from the
    generic immutable-receipt conflict.
    """
    good = {
        "lease_id": receipt["lease_id"],
        "expected_epoch": receipt["epoch"],
        "provider_id": receipt["provider_id"],
        "command_digest": receipt["command_digest"],
        "exit_code": receipt["exit_code"],
        "started_at": receipt["started_at"],
        "finished_at": receipt["finished_at"],
        "output_digest": receipt["output_digest"],
        "receipt_payload": receipt["receipt_payload"],
        "receipt_digest": receipt["receipt_digest"],
    }
    path = f"/worker/external-operations/{op}/run-receipt"

    def post(body: dict) -> tuple[int, object]:
        return ctx.request(
            "POST", path, body, auth="none", headers=driver.worker_auth(),
            idempotency_key=ctx.new_key(),
        )  # fmt: skip

    def payload_variant(**changes: object) -> dict:
        payload = {**good["receipt_payload"], **changes}
        return {**good, "receipt_payload": payload, "receipt_digest": _sha(_canon(payload))}

    status, _ = post(good)
    variants = {
        "wrong output_digest": {**good, "output_digest": _sha("not the manifest")},
        "wrong command_digest": {**good, "command_digest": _sha("not the argv")},
        "wrong payload operation_id": payload_variant(operation_id="0" * 32),
        "wrong payload plan_digest": payload_variant(plan_digest=_sha("not the plan")),
    }
    refused, odd = [], []
    for name, body in variants.items():
        got, answer = post(body)
        detail = (answer or {}).get("detail") if isinstance(answer, dict) else None
        if got == 409 and detail == "Run receipt does not match control's recorded operation.":
            refused.append(name)
        else:
            odd.append(f"{name}: {got} {_s(detail, 50)}")
    c.add(
        "receipt-bound-to-control-records",
        "PASS" if status == 200 and not odd else "FAIL",
        f"a faithful replay of the stored receipt returns {status} (expect 200); "
        f"{len(refused)}/{len(variants)} tampered variants were refused 409 with control's "
        f"mismatch message ({', '.join(refused)}); unexpected: {odd or 'none'}. Control checks "
        "operation id, plan digest, argv digest and its own artifact manifest digest, so a "
        "receipt cannot cite values control did not record; an enrolled worker can still "
        "assert exit code, timestamps and provider identity, which control cannot observe",
    )


# --------------------------------------------------------------------------------------
def _receipt_cases(ctx: Context, driver: RealRun, rec, c: Case) -> None:
    op = rec.operation_id
    worker_claim = rec.worker.get("run_receipt")
    receipt = ctx.call("GET", f"/external-operations/{op}/run-receipt")
    manifest = driver.manifest_for(op)
    want_command = _sha(_canon(driver.argv))
    problems = []
    if worker_claim is None or worker_claim.get("status") != "recorded":
        problems.append(f"worker did not report a recorded run_receipt: {worker_claim}")
    seal_digest = None
    if manifest is not None:
        # Recompute the worker's sealed-output manifest digest from public facts only: the
        # formula in tessera_worker.sealed_outputs (seal id from lease/epoch/operation, the
        # sorted artifact entries) applied to what control lists for the run.
        seal_id = (
            "output_seal_"
            + _sha(f"tessera-sealed-output-v1:{receipt['lease_id']}:{receipt['epoch']}:{op}")[:32]
        )
        seal_digest = _sha(
            _canon(
                {
                    "version": 1,
                    "seal_id": seal_id,
                    "lease_id": receipt["lease_id"],
                    "epoch": receipt["epoch"],
                    "operation_id": op,
                    "artifacts": [
                        {k: a[k] for k in ("relative_path", "sha256", "size")}
                        for a in sorted(manifest["artifacts"], key=lambda a: a["relative_path"])
                    ],
                }
            )
        )
    expected = {
        "provider_kind": "local_docker_cpu",
        "operational_only": True,
        "provider_success": "not_established",
        "scientific_validity": "not_assessed",
        "exit_code": 0,
        "command_digest": want_command,
        "output_digest": manifest["manifest_digest"] if manifest is not None else None,
        "receipt_evidence": "observed_provider_execution",
    }
    for key, want in expected.items():
        if receipt.get(key) != want:
            problems.append(f"{key}={_s(receipt.get(key), 24)!r} expected {_s(want, 24)!r}")
    payload = receipt.get("receipt_payload") or {}
    if payload.get("operation_id") != op:
        problems.append("receipt_payload.operation_id is not the operation")
    if seal_digest is not None and payload.get("sealed_manifest_digest") != seal_digest:
        problems.append("receipt_payload.sealed_manifest_digest != recomputed worker seal digest")
    if worker_claim and worker_claim.get("receipt_digest") != receipt.get("receipt_digest"):
        problems.append("worker-reported receipt_digest differs from the stored one")
    c.add(
        "observed-receipt",
        "FAIL" if problems else "PASS",
        "; ".join(problems)
        or (
            f"worker posted it (automatic, no human step): provider_kind={receipt['provider_kind']} "
            f"operational_only={receipt['operational_only']} "
            f"provider_success={receipt['provider_success']} "
            f"scientific_validity={receipt['scientific_validity']} exit_code={receipt['exit_code']} "
            f"output_digest={_s(receipt['output_digest'])} == control's artifact manifest digest "
            f"(worker seal digest {_s(seal_digest)} kept in the payload); "
            f"command_digest={_s(receipt['command_digest'])} == sha256(canonical argv); "
            f"receipt_digest={_s(receipt['receipt_digest'])} provider_id={_s(receipt['provider_id'])}"
        ),
    )
    if manifest is not None:
        _tamper_cases(ctx, driver, op, receipt, c)
    fake = db_rows(
        ctx.state_dir, f"SELECT count(*) FROM external_operation_receipts WHERE operation_id='{op}'"
    )[0][0]
    observed = db_rows(
        ctx.state_dir, f"SELECT count(*) FROM execution_run_receipts WHERE operation_id='{op}'"
    )[0][0]
    c.add(
        "no-fake-receipt",
        "PASS" if (fake, observed) == (0, 1) else "FAIL",
        f"read-only DB count for the real operation: external_operation_receipts(fake)={fake}, "
        f"execution_run_receipts(observed)={observed}",
    )
    if manifest is not None:
        rec.attempt_manifest = manifest  # type: ignore[attr-defined]
    rec.receipt = receipt  # type: ignore[attr-defined]


def _finish_attempt(ctx: Context, driver: RealRun, rec, index: int, c: Case) -> dict:
    call, eid, aid = ctx.call, rec.experiment_id, rec.attempt["id"]
    manifest = driver.manifest_for(rec.operation_id)
    if manifest is None:
        raise RuntimeError("no artifact manifest for the real run")
    grant = driver.grant_view(rec.grant_id)
    reconciled = call(
        "POST",
        f"/execution-grants/{rec.grant_id}/reconcile",
        {
            "expected_revision": grant["revision"],
            "settlement_cents": 0,
            "provider_terminated": True,
        },
    )
    if index == 0:
        c.add(
            "grant-reconcile",
            "PASS" if reconciled.get("status") == "reconciled" else "FAIL",
            f"POST /execution-grants/{{id}}/reconcile with the real receipt present -> "
            f"status={reconciled.get('status')} revision={reconciled.get('revision')}. Human route; "
            "its event is not in approval_trust.DECISION_EVENT_TYPES, so no automation label is "
            "needed and common.is_decision_call correctly leaves it unlabelled",
        )
    view = driver.attempt_view(eid, aid)
    call(
        "POST",
        f"/experiment-admitted-attempts/{aid}/resolve",
        {"expected_revision": view["revision"], "outcome": "succeeded"},
    )
    result = next(a for a in manifest["artifacts"] if a["relative_path"] == "result.json")
    view = driver.attempt_view(eid, aid)
    call(
        "POST",
        f"/experiment-attempts/{aid}/artifacts",
        {"expected_revision": view["revision"], "artifact_ids": [result["id"]]},
    )
    derived = {}
    for name, pointer in (("score", "/metrics/score"), ("bytes", "/metrics/bytes")):
        if name == "bytes" and index != 0:
            continue
        view = driver.attempt_view(eid, aid)
        derived[name] = call(
            "POST",
            f"/experiment-attempts/{aid}/derived-measurements",
            {
                "expected_revision": view["revision"],
                "name": name,
                "artifact_id": result["id"],
                "json_pointer": pointer,
            },
        )
    record = call(
        "POST",
        f"/experiment-attempts/{aid}/metric-records",
        {"measurement_id": derived["score"]["id"], "checkpoint_sha": result["sha256"]},
    )
    return {
        "manifest": manifest,
        "artifact": result,
        "derived": derived,
        "metric_record": record,
        "reconciled": reconciled,
    }


def _derived_case(ctx: Context, driver: RealRun, rec, m: dict, c: Case) -> None:
    def work():
        manifest, artifact = m["manifest"], m["artifact"]
        driver.retain_and_release(manifest)  # human custody release so we can fetch real bytes
        raw = driver.fetch_artifact(manifest, "result.json")
        document = json.loads(raw)
        blob = driver.fetch_artifact(manifest, "blob.bin")
        problems = []
        for name, pointer in (("bytes", "/metrics/bytes"), ("score", "/metrics/score")):
            got = m["derived"][name]
            value = got["value"]
            want = document["metrics"][pointer.rsplit("/", 1)[1]]
            if value.get("number") != want:
                problems.append(f"{name}: value {value.get('number')} != bytes {want}")
            if (
                value.get("derivation") != "retained-json-number-v1"
                or value.get("json_pointer") != pointer
            ):
                problems.append(f"{name}: derivation/pointer {value}")
            if value.get("artifact_sha256") != hashlib.sha256(raw).hexdigest():
                problems.append(f"{name}: artifact_sha256 does not match the real bytes")
            if got.get("source") != "derived" or got.get("scientific_validity") != "not_assessed":
                problems.append(
                    f"{name}: source={got.get('source')} sv={got.get('scientific_validity')}"
                )
        if document["metrics"]["bytes"] != len(blob):
            problems.append(f"program byte count {document['metrics']['bytes']} != {len(blob)}")
        c.add(
            "derived-metric",
            "FAIL" if problems else "PASS",
            "; ".join(problems)
            or (
                f"attempt {rec.attempt['id'][-8:]} (admitted grant run by the real worker): "
                f"POST derived-measurements /metrics/bytes -> number={m['derived']['bytes']['value']['number']} "
                f"(== len(blob.bin)={len(blob)}), /metrics/score -> "
                f"{m['derived']['score']['value']['number']:.6f}; both carry derivation="
                f"retained-json-number-v1, artifact_sha256={_s(artifact['sha256'])} == sha256 of the "
                "bytes re-fetched through the custody route, source=derived, "
                "scientific_validity=not_assessed. The server read the bytes; the caller sent only "
                "a pointer"
            ),
        )

    c.guard("derived-metric", work)


def _acceptance(ctx: Context, driver: RealRun, rec, m: dict, c: Case) -> None:
    receipt = rec.receipt
    manifest = m["manifest"]
    grant = driver.grant_view(rec.grant_id)
    body = {
        "expected_grant_revision": grant["revision"],
        "expected_epoch": receipt["epoch"],
        "lease_id": receipt["lease_id"],
        "operation_id": rec.operation_id,
        "manifest_id": manifest["id"],
        "expected_manifest_digest": manifest["manifest_digest"],
        "validation_contract": "synthetic-declared-output-v1",
        "expected_artifacts": [
            {k: a[k] for k in ("id", "relative_path", "sha256", "size")}
            for a in manifest["artifacts"]
        ],
        "expected_total_bytes": sum(a["size"] for a in manifest["artifacts"]),
    }
    status, payload = ctx.request(
        "POST",
        f"/execution-grants/{rec.grant_id}/result-checkpoint",
        body,
        idempotency_key=ctx.new_key(),
    )
    if status not in (200, 201):
        c.add(
            "result-acceptance",
            "FAIL",
            f"POST /execution-grants/{{id}}/result-checkpoint on the REAL operation "
            f"{_s(rec.operation_id)} -> {status}: {_s(payload, 220)}. The observed acceptance "
            "ledger is only reachable through a result checkpoint; a non-2xx here is a regression",
        )
        return
    accept = {
        "expected_revision": 0,
        "action": "accept",
        "rationale": "Real local-Docker bytes, bookkeeping only; no scientific claim.",
        "expected_checkpoint_digest": payload["commit_digest"],
        "expected_manifest_digest": manifest["manifest_digest"],
        "expected_validation_digest": payload["validation_digest"],
        "expected_receipt_digest": receipt["receipt_digest"],
        "expected_grant_revision": grant["revision"],
        "lease_id": receipt["lease_id"],
        "operation_id": rec.operation_id,
        "expected_epoch": receipt["epoch"],
    }
    got = ctx.call("POST", f"/result-checkpoints/{payload['id']}/acceptances", accept)
    ok = (
        got.get("evidence_label") == "provider_observed_unassessed"
        and got.get("receipt_evidence") == "observed_provider_execution"
    )
    c.add(
        "result-acceptance",
        "PASS" if ok else "FAIL",
        f"checkpoint {payload['id'][-8:]} committed; acceptance evidence_label="
        f"{got.get('evidence_label')} receipt_evidence={got.get('receipt_evidence')} "
        f"provider_kind={got.get('provider_kind')} exit_code={got.get('exit_code')} "
        f"artifact_release={got.get('artifact_release')}",
    )


def _group(ctx: Context, driver: RealRun, exp_variant: dict, lock: dict, measured, c: Case):
    group = ctx.call(
        "POST",
        f"/projects/{driver.project['id']}/replicate-groups",
        {
            "experiment_variant_id": exp_variant["id"],
            "metric": "score",
            "split_lock_id": lock["id"],
            "partition": "test",
        },
    )
    values = [m["derived"]["score"]["value"]["number"] for m in measured]
    mean = sum(values) / len(values)
    ok = (
        group["n"] == 2
        and abs(group["mean"] - mean) < 1e-12
        and group["sources"] == ["derived"]
        and group.get("imputation") == "none"
    )
    c.add(
        "replicate-group",
        "PASS" if ok else "FAIL",
        f"2 real attempts (seeds 0,1): platform n={group['n']} mean={group['mean']:.6f} "
        f"(independent mean {mean:.6f}) std={group['std']:.4f} ci=[{group['ci_low']:.4f},"
        f"{group['ci_high']:.4f}] sources={group['sources']} min_sample_count="
        f"{group['min_sample_count']} result_digest={_s(group['result_digest'])}",
    )
    return group if ok else None


def _verdict(ctx, driver, experiment, exp_variant, lock, corpus, group, measured, c: Case) -> None:
    sys.path.insert(0, str(REPO / "src"))
    from tessera_control.evaluation_models import EvaluationProtocol
    from tessera_control.store import canonical_digest

    call, pid = ctx.call, driver.project["id"]
    study = call(
        "POST",
        f"/projects/{pid}/studies",
        {"purpose": "Generic outcome review, not science", "protocol": "Local Docker fixture"},
    )
    source = call(
        "POST",
        f"/studies/{study['id']}/sources",
        {
            "uri": "https://example.test/generic",
            "quoted_passage": "Fixture source only",
            "content_digest": "a" * 64,
            "locator": "fixture:1",
        },
    )
    variant = call(
        "POST",
        f"/studies/{study['id']}/variants",
        {
            "name": "Generic registered condition",
            "method_digest": "b" * 64,
            "input_digest": "c" * 64,
            "metadata": {"synthetic": True, "experiment_id": experiment["id"]},
        },
    )
    call(
        "POST",
        f"/sources/{source['id']}/assess",
        {"expected_revision": 1, "verdict": "supported", "reason": "Fixture support only"},
    )
    call(
        "POST",
        f"/variants/{variant['id']}/record",
        {"expected_revision": 1, "status": "completed", "outcome": {"fixture": True}},
    )
    finding = call(
        "POST",
        f"/studies/{study['id']}/findings",
        {
            "statement": "Synthetic human interpretation only",
            "source_ids": [source["id"]],
            "variant_ids": [variant["id"]],
        },
    )
    finding = call("POST", f"/findings/{finding['id']}/accept", {"expected_revision": 1})

    def context():
        return call("GET", f"/interaction/studies/{study['id']}/program-context")

    def scope(action, **changes):
        ctxv = context()
        prior = [
            r
            for r in ctxv["reviews"]
            if r["scope"]["outcome"] == "generic_outcome" and r["scope"]["action"] == action
        ]
        value = {
            "type": "research_program",
            "version": 1,
            "study_id": study["id"],
            "outcome": "generic_outcome",
            "action": action,
            "basis_digest": ctxv["basis_digest"],
            "supersedes_id": prior[-1]["id"] if prior else None,
            "reason": "Explicit fixture proposal",
        }
        if action == "plan":
            value.update(
                brief={
                    "objective": "Check the evidence chain",
                    "hypothesis": "None; bookkeeping only",
                    "success_gate": "Platform aggregate meets a vacuous threshold",
                    "failure_gate": "Aggregate below threshold",
                    "stop_gate": "Stop at declared resource cap",
                    "target_domain": "Generic",
                    "provenance_policy": "Record hashes and uncertainty",
                    "data_source_ids": [source["id"]],
                    "compute": {
                        "budget_cents": 0,
                        "max_wall_seconds": 60,
                        "max_gpu_hours": 0.0,
                        "target": "local_cpu",
                        "resources": "Local Docker CPU only",
                    },
                    "dataset_recommendation": "Fixture only",
                    "representation_recommendation": "None",
                    "split_recommendation": "Locked split",
                    "metric_recommendation": "Platform replicate aggregate",
                    "uncertainty": "No scientific result exists",
                    "missing_prerequisites": [],
                },
                conditions={
                    "condition_a": {
                        "disposition": "selected",
                        "rationale": "Only condition",
                        "variant_ids": [variant["id"]],
                    }
                },
            )
        if action == "source_use":
            value.update(source_ids=[source["id"]], intended_use="Fixture-only review")
        value.update(changes)
        return value

    def propose(sc):
        return call(
            "POST",
            "/interaction/decisions",
            {
                "project_id": pid,
                "kind": "access" if sc["action"] == "source_use" else "direction",
                "scope": sc,
                "expires_at": _expiry(),
            },
        )

    def resolve(proposal, *, expected=(200, 201)):
        return call(
            "POST",
            "/interaction/decisions/resolve",
            {
                "decision_id": proposal["id"],
                "expected_revision": proposal["revision"],
                "decision": "approve",
                "rationale": "Human fixture review; no scientific validity claim",
            },
            expected=expected,
        )

    gate = {
        "id": "g1",
        "category": "calibration",
        "variant_id": variant["id"],
        "experiment_variant_id": exp_variant["id"],
        "metric": "score",
        "unit": "fraction",
        "split_lock_id": lock["id"],
        "split_partition": "test",
        "split_digest": lock["membership_digest"],
        "training_examples": None,
        "procedure": "Generic fixture; no scientific measurement",
        "comparison": "ge",
        "threshold": 0.0,
        "statistic": "mean",
        "minimum_replicates": 2,
        "minimum_samples": 5,
        "allowed_sources": ["derived"],
        "required_reference_lock_ids": [corpus["id"]],
    }
    protocol = {
        "version": 2,
        "identity": {
            "data_digest": driver.seed["digest"],
            "code_digest": _sha(_canon(driver.argv)),
            "environment_digest": driver.image.removeprefix("python@sha256:"),
        },
        "gates": [gate],
    }
    # Go needs every linked variant admitted AND terminally observed (a withdrawn-before-dispatch
    # attempt is "not_observed" and blocks go), so the intervention variant gets a real run too.
    other = driver.admit_run("intervention")
    driver.execute(other)
    if not other.quarantined:
        raise RuntimeError(
            f"intervention run failed: state={other.worker.get('state')} "
            f"reason={other.worker.get('reason')} detail={other.worker.get('detail')}"
        )
    grant = driver.grant_view(other.grant_id)
    call(
        "POST",
        f"/execution-grants/{other.grant_id}/reconcile",
        {
            "expected_revision": grant["revision"],
            "settlement_cents": 0,
            "provider_terminated": True,
        },
    )
    view = driver.attempt_view(experiment["id"], other.attempt["id"])
    call(
        "POST",
        f"/experiment-admitted-attempts/{other.attempt['id']}/resolve",
        {"expected_revision": view["revision"], "outcome": "succeeded"},
    )
    plan = propose(scope("plan", evaluation_protocol=protocol))
    resolve(plan)
    plan_row = next(r for r in context()["reviews"] if r["id"] == plan["id"])
    resolve(propose(scope("source_use")))
    record = {
        "version": 2,
        "plan_decision_id": plan["id"],
        "plan_scope_digest": plan_row["scope_digest"],
        "protocol_digest": canonical_digest(
            EvaluationProtocol.model_validate(protocol).model_dump(mode="json")
        ),
        "identity": dict(protocol["identity"]),
        "evidence_kind": "platform_replicate_aggregate",
        "observations": [{"gate_id": "g1", "replicate_group_id": group["id"]}],
    }
    # A caller-reported float next to the group id must be refused (no bare numbers).
    bare = dict(record, observations=[{**record["observations"][0], "value": 0.99}])
    status, payload = ctx.request(
        "POST",
        "/interaction/decisions",
        {
            "project_id": pid,
            "kind": "direction",
            "scope": scope("evaluation", evaluation=bare),
            "expires_at": _expiry(),
        },
        idempotency_key=ctx.new_key(),
    )
    # The API's RequestValidationError handler returns a fixed body for every pydantic
    # rejection ({"detail": "Invalid request."}, api.py), so the rule cannot be read from the
    # response. The float is attributed as the cause differentially: the request is exactly
    # the accepted `record` (proposed and resolved below) plus the single `value` field.
    float_refused = status == 422 and payload == {"detail": "Invalid request."}
    # The attribution only holds if the two bodies differ in exactly the `value` field.
    only_value_differs = (
        {k: v for k, v in bare.items() if k != "observations"}
        == {k: v for k, v in record.items() if k != "observations"}
        and len(bare["observations"]) == len(record["observations"]) == 1
        and set(bare["observations"][0]) - set(record["observations"][0]) == {"value"}
        and {k: v for k, v in bare["observations"][0].items() if k != "value"}
        == record["observations"][0]
    )
    evaluation = propose(scope("evaluation", evaluation=record))
    resolve(evaluation)
    report = next(r for r in context()["reviews"] if r["id"] == evaluation["id"])[
        "evaluation_report"
    ]
    g = report["gates"][0]
    ok = (
        report["status"] == "pass"
        and report.get("scientific_validity") == "not_assessed"
        and report.get("execution_granted") is False
        and g["group"]["result_digest"] == group["result_digest"]
        and float_refused
        and only_value_differs
    )
    c.add(
        "gate-verdict",
        "PASS" if ok else "FAIL",
        f"platform-computed evaluation_report status={report['status']} gate g1 "
        f"status={g['status']} reason={g.get('reason')} binding="
        f"{report.get('evidence_binding')} cites group {_s(group['id'][-8:], 8)} "
        f"result_digest={_s(g['group']['result_digest'])} mean={group['mean']:.6f} "
        f"(threshold mean>=0.0, vacuous by design); a caller float beside the group id was "
        f"{'refused' if float_refused else 'NOT refused as required (need 422)'}"
        f" with {status} {_s(payload, 60)} (the body is fixed for all validation errors; the "
        "asserted request bodies differ only by the `value` field "
        f"({only_value_differs}), and the one without it is accepted below); "
        f"execution_granted={report.get('execution_granted')}. "
        "Reviews resolved as labelled owner automation (decision route)",
    )
    driver.gate_ctx = {"evaluation": evaluation, "group": group}  # type: ignore[attr-defined]
    go = propose(
        scope(
            "go_no_go",
            recommendation="go",
            finding_ids=[finding["id"]],
            evaluation_id=evaluation["id"],
        )
    )
    status, payload = ctx.request(
        "POST",
        "/interaction/decisions/resolve",
        {
            "decision_id": go["id"],
            "expected_revision": go["revision"],
            "decision": "approve",
            "rationale": "Human fixture review; no scientific validity claim",
        },
        idempotency_key=ctx.new_key(),
    )
    if status == 200:
        row = next(r for r in context()["reviews"] if r["id"] == go["id"])
        c.add(
            "gate-go-approval",
            "PASS"
            if row.get("gate_evidence_binding") == "platform_replicate_aggregate"
            else "FAIL",
            f"human go approved with gate_evidence_binding={row.get('gate_evidence_binding')}; "
            f"execution_granted={context().get('execution_granted')} (a go does not grant execution)",
        )
    else:
        c.add(
            "gate-go-approval",
            "FAIL",
            f"go resolve -> {status}: {_s(payload, 260)}. Verdict above is still the "
            "platform-computed evaluation; a non-200 go resolve is a regression",
        )


def _expiry() -> str:
    from .common import _later

    return _later(60)


def _lineage(ctx: Context, driver: RealRun, records, measured, group, c: Case) -> None:
    call, pid = ctx.call, driver.project["id"]
    base = f"/projects/{pid}/artifact-lineage"
    rec, m = records[0], measured[0]
    aid, op = rec.attempt["id"], rec.operation_id
    before = call("GET", f"{base}/nodes")
    auto_nodes = len(before.get("nodes", before if isinstance(before, list) else []))

    def node(body):
        return call("POST", f"{base}/nodes", body)

    dataset = node(
        {
            "kind": "dataset",
            "sha256": driver.seed["digest"],
            "label": "seed snapshot (real input of the run)",
            "origin": "external_import",
        }
    )
    output = node(
        {
            "kind": "other",
            "sha256": m["artifact"]["sha256"],
            "label": "run output result.json",
            "origin": "tessera_attempt",
            "producing_attempt_id": aid,
            "operation_id": op,
            "artifact_id": m["artifact"]["id"],
        }
    )
    metric_value = m["derived"]["score"]["value"]
    metric = node(
        {
            "kind": "other",
            "sha256": _sha(_canon(metric_value)),
            "label": "derived measurement score (sha256 of its canonical value)",
            "origin": "tessera_attempt",
            "producing_attempt_id": aid,
            "operation_id": op,
        }
    )
    evaluation = node(
        {
            "kind": "evaluation",
            "sha256": group["result_digest"],
            "label": "replicate group result_digest (platform aggregate)",
            "origin": "external_import",
        }
    )

    def edge(child, parent, relation, why):
        return call(
            "POST",
            f"{base}/edges",
            {
                "child_node_id": child["id"],
                "parent_node_id": parent["id"],
                "relation": relation,
                "rationale": why,
            },
        )

    edge(output, dataset, "derived_from", "Run read the seed snapshot as /inputs.")
    edge(metric, output, "derived_from", "Measurement derived from this output's bytes.")
    edge(evaluation, metric, "derived_from", "Aggregate includes this derived measurement.")
    edge(evaluation, output, "evaluated_on", "Aggregate evaluated on this run's output.")

    def ancestors(start):
        got = call("GET", f"{base}/nodes/{start['id']}/lineage?direction=ancestors")
        return {n["id"]: n["depth"] for n in got["nodes"]}, got["edges"]

    metric_up, _ = ancestors(metric)
    eval_up, eval_edges = ancestors(evaluation)
    chain_ok = (
        metric_up.get(output["id"]) == 1
        and metric_up.get(dataset["id"]) == 2
        and eval_up.get(metric["id"]) == 1
        and eval_up.get(output["id"]) == 1
        and eval_up.get(dataset["id"]) == 2
        and len(eval_edges) == 4
    )
    export = call("GET", f"{base}/export")
    experiment_export = call("GET", f"/experiments/{rec.experiment_id}/export")
    in_export = {n["id"] for n in export["nodes"]} >= {
        dataset["id"],
        output["id"],
        metric["id"],
        evaluation["id"],
    }
    c.add(
        "lineage-join",
        "PASS" if chain_ok and in_export else "FAIL",
        f"HUMAN-registered join of real ids (4 nodes, 4 edges via POST .../artifact-lineage): "
        f"dataset={_s(dataset['sha256'])} (seed snapshot digest) <- output "
        f"{_s(output['sha256'])} (artifact {output['artifact_id'][-8:]}, attempt {aid[-8:]}, "
        f"operation {_s(op)}) <- derived metric <- evaluation {_s(evaluation['sha256'])} "
        f"(group result_digest). Ancestors of metric: output depth {metric_up.get(output['id'])}, "
        f"dataset depth {metric_up.get(dataset['id'])}; of evaluation: metric "
        f"{eval_up.get(metric['id'])}, output {eval_up.get(output['id'])}, dataset "
        f"{eval_up.get(dataset['id'])}, {len(eval_edges)} edges. Lineage export digest "
        f"{_s(export['digest'])} lists all 4 nodes",
    )
    gaps = []
    if auto_nodes == 0:
        gaps.append(
            "after three real runs, receipts, derived measurements and a replicate group, "
            "GET .../artifact-lineage/nodes was EMPTY: nothing emits nodes/edges automatically"
        )
    if "lineage" not in json.dumps(list(experiment_export)):
        gaps.append(
            "GET /experiments/{id}/export has no lineage section (Open hook 3: "
            f"export_experiment does not call lineage_export_payload; keys={sorted(experiment_export)[:8]})"
        )
    gaps.append(
        "lineage node kinds are model_checkpoint|dataset|labels|evaluation|other: there is no "
        "kind for a derived measurement, replicate group or gate verdict, so those were "
        "registered as other/evaluation with a caller-chosen sha256 (group result_digest, "
        "sha of the measurement value); nodes assert digests, never that bytes exist"
    )
    gaps.append(
        "no edge or node binds the replicate group's member measurement ids, the evaluation "
        "decision id or the receipt digest; the receipt is not a lineage field at all"
    )
    c.add("automatic-lineage-join", "GAP", "; ".join(gaps))


# ruff: noqa: E501
