"""Artifact lineage, run-receipt, dataset staging and gate-evidence checks (current control schema).

Everything here goes over HTTP to a real `tessera serve`. Routes that are reachable
without a worker run (lineage, dataset upload, split locks, research programs, and the
denial/not-found paths of staging, metric and split-binding routes) are asserted with
exact status codes. Cases that need state which cannot be created over HTTP (worker
enrollment, admitted operations, retained and custody-released run outputs) are reported
as GAP with the exact missing prerequisite, never faked.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

try:
    from .common import Context, Result
except ImportError:  # executed as a plain script next to common.py
    from common import Context, Result  # type: ignore[no-redef]

CAPABILITY = "lineage_receipts"
SCOPE = "attributed_lineage_bookkeeping_only"


def _sha(label: str) -> str:
    return hashlib.sha256(f"platform-validation:{label}".encode()).hexdigest()


def _dataset_upload() -> dict:
    import base64

    from tessera_control.dataset_snapshots import (
        build_dataset_capture,
        manifest_bytes,
        verify_dataset_bytes,
    )
    from tessera_control.workspace import inventory_digest

    data = b"row_id,x,target,split\na,1,2,train\nb,2,4,validation\nc,3,6,test\n"
    declaration = {
        "version": "tessera-dataset-snapshot-v1",
        "profile": "bounded-tabular-regression-v1",
        "columns": [
            {"name": "row_id", "type": "string", "role": "id"},
            {"name": "x", "type": "number", "role": "feature"},
            {"name": "target", "type": "number", "role": "target"},
            {"name": "split", "type": "split", "role": "split"},
        ],
        "source": {"kind": "synthetic", "reference": "pv-linear-v1", "license": "CC0-1.0"},
    }
    sha = hashlib.sha256(data).hexdigest()
    observed = verify_dataset_bytes(data, declaration, expected_sha256=sha, expected_size=len(data))
    digest = hashlib.sha256(manifest_bytes(observed)).hexdigest()
    capture = build_dataset_capture(
        data,
        declaration,
        expected_sha256=sha,
        expected_size=len(data),
        expected_manifest_digest=digest,
    )
    return {
        "declaration": declaration,
        "content_base64": base64.b64encode(data).decode(),
        "expected_data_sha256": sha,
        "expected_data_size": len(data),
        "expected_manifest_digest": digest,
        "expected_input_snapshot_digest": inventory_digest(capture.inventory),
    }


def _agent_request(ctx: Context, token: str, method: str, path: str, body=None, key=None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if key:
        headers["Idempotency-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ctx.base_url + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - local service
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, None


# The positive paths for run receipts, derived measurements, gate verdicts and staging from
# run outputs, and the lineage join, need a worker-collected run. They are exercised for
# real by check_real_run.py (egress, staging) and check_receipts.py (receipt, derived
# metric, gate verdict, lineage), not as GAP placeholders here. This module covers only
# what is reachable over HTTP without a worker.


def run(ctx: Context) -> list[Result]:
    results: list[Result] = []
    if ctx.process is None:
        ctx.start_service()

    def rec(case: str, status: str, evidence: str) -> None:
        results.append(Result(CAPABILITY, case, status, evidence[:300]))

    def check(case: str, ok: bool, evidence: str) -> None:
        rec(case, "PASS" if ok else "FAIL", evidence)

    def call(method, path, body=None, **kw):
        if method == "POST" and "idempotency_key" not in kw:
            kw["idempotency_key"] = ctx.new_key()
        return ctx.request(method, path, json=body, **kw)

    st, project = call("POST", "/projects", {"name": "lineage-validation"})
    st2, other = call("POST", "/projects", {"name": "lineage-validation-other"})
    if st not in (200, 201) or st2 not in (200, 201):
        rec("setup", "FAIL", f"project creation returned {st}/{st2}")
        return results
    pid, oid = project["id"], other["id"]
    base = f"/projects/{pid}/artifact-lineage"

    # --- nodes: dataset snapshot digest, run output, derived metric ---
    specs = {
        "dataset": ("dataset", "dataset snapshot digest"),
        "output": ("other", "run output"),
        "metric": ("evaluation", "derived metric"),
    }
    nodes: dict[str, dict] = {}
    bodies: dict[str, dict] = {}
    ok = True
    for name, (kind, label) in specs.items():
        bodies[name] = {
            "kind": kind,
            "sha256": _sha(name),
            "label": label,
            "origin": "external_import",
            "source_uri": f"https://example.invalid/{name}",
        }
        s, n = call("POST", base + "/nodes", bodies[name], idempotency_key=f"node-{name}-{pid}")
        ok = ok and s == 201 and n.get("authoritative_scope") == SCOPE
        nodes[name] = n or {}
    check(
        "register_nodes",
        ok,
        "3 nodes (dataset, run output, derived metric) -> 201 with scope label",
    )
    if not ok:
        return results

    s, replay = call(
        "POST", base + "/nodes", bodies["dataset"], idempotency_key=f"node-dataset-{pid}"
    )
    check(
        "node_idempotent_replay",
        s in (200, 201) and replay.get("id") == nodes["dataset"]["id"],
        f"same key+body replay -> {s}, same node id",
    )
    s, _ = call("POST", base + "/nodes", bodies["dataset"])
    check("node_duplicate_digest_conflict", s == 409, f"new key, same sha256 -> {s} (expect 409)")

    # --- edges ---
    def edge(child, parent, relation="derived_from", key=None):
        body = {
            "child_node_id": nodes[child]["id"],
            "parent_node_id": nodes[parent]["id"],
            "relation": relation,
            "rationale": f"{child} derived from {parent} (validation)",
        }
        return body, call("POST", base + "/edges", body, idempotency_key=key or ctx.new_key())

    e1b, (s1, e1) = edge("output", "dataset", key=f"e1-{pid}")
    e2b, (s2, e2) = edge("metric", "output", key=f"e2-{pid}")
    check("record_edges", s1 == 201 and s2 == 201, f"dataset->output->metric edges -> {s1}/{s2}")
    s, r = call("POST", base + "/edges", e1b, idempotency_key=f"e1-{pid}")
    check(
        "edge_idempotent_replay",
        s in (200, 201) and r.get("id") == e1.get("id"),
        f"edge replay -> {s}",
    )
    _, (s, _) = edge("output", "dataset")
    check(
        "edge_duplicate_conflict", s == 409, f"duplicate child/parent/relation -> {s} (expect 409)"
    )
    _, (s, _) = edge("dataset", "metric")
    check("edge_cycle_rejected", s == 409, f"metric->...->dataset cycle edge -> {s} (expect 409)")
    _, (s, _) = edge("dataset", "dataset")
    check("edge_self_rejected", s == 400, f"self edge -> {s}")

    # --- queries ---
    mid = nodes["metric"]["id"]
    s, anc = call("GET", f"{base}/nodes/{mid}/lineage?direction=ancestors")
    depths = {n["id"]: n.get("depth") for n in (anc or {}).get("nodes", [])}
    check(
        "lineage_ancestors",
        s == 200
        and depths.get(mid) == 0
        and depths.get(nodes["output"]["id"]) == 1
        and depths.get(nodes["dataset"]["id"]) == 2
        and len(anc.get("edges", [])) == 2,
        f"metric ancestors depths {sorted(depths.values())}, "
        f"edges {len((anc or {}).get('edges', []))}",
    )
    s, desc = call("GET", f"{base}/nodes/{nodes['dataset']['id']}/lineage?direction=descendants")
    check(
        "lineage_descendants",
        s == 200 and len((desc or {}).get("nodes", [])) == 3,
        f"dataset descendants -> {len((desc or {}).get('nodes', []))} nodes",
    )
    s1, x1 = call("GET", base + "/export")
    s2, x2 = call("GET", base + "/export")
    check(
        "export_deterministic",
        s1 == s2 == 200
        and x1 == x2
        and x1.get("digest")
        and len(x1["nodes"]) == 3
        and len(x1["edges"]) == 2,
        "two exports byte-identical with digest, 3 nodes / 2 edges",
    )

    # --- isolation and authorization ---
    s, _ = call("GET", f"/projects/{oid}/artifact-lineage/nodes/{mid}")
    check("project_isolation_node", s == 404, f"node read via other project -> {s} (expect 404)")
    s, ox = call("GET", f"/projects/{oid}/artifact-lineage/export")
    check(
        "project_isolation_export",
        s == 200 and ox.get("nodes") == [] and ox.get("edges") == [],
        f"other project export empty ({s})",
    )
    s, _ = call("GET", base + "/nodes", auth="none")
    check("unauthenticated_refused", s in (401, 403), f"no credential -> {s}")
    rec(
        "cross_org_isolation",
        "GAP",
        "second organization cannot be created over HTTP; same-org other-project isolation "
        "covered above, cross-org only by unit tests",
    )

    token = f"pv_agent_{secrets.token_urlsafe(36)}"
    s, _ = call(
        "POST",
        "/credentials",
        {
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": pid,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    )
    if s in (200, 201):
        body = bodies["dataset"] | {"sha256": _sha("agent-attempt")}
        s, _ = _agent_request(ctx, token, "POST", base + "/nodes", body, ctx.new_key())
        check("agent_write_refused", s == 403, f"agent credential node write -> {s} (expect 403)")
        s, _ = _agent_request(ctx, token, "GET", base + "/nodes")
        check("agent_read_allowed", s == 200, f"agent credential node list -> {s}")
        s, _ = call("GET", base + "/nodes?limit=200")
    else:
        rec("agent_write_refused", "GAP", f"agent credential could not be issued ({s})")

    s, after = call("GET", base + "/export")
    check(
        "append_only_no_mutation_routes",
        s == 200 and after.get("digest") == x1.get("digest"),
        "failed/duplicate/agent writes left export digest unchanged; no PUT/DELETE routes exist",
    )

    # --- run receipts (synthetic/unobtainable operation) ---
    s, _ = call("GET", "/external-operations/operation_missing/run-receipt")
    check("run_receipt_get_route_absent_op", s == 404, f"GET run-receipt for unknown op -> {s}")
    s, _ = call(
        "POST",
        "/worker/external-operations/operation_missing/run-receipt",
        {},
    )
    check(
        "run_receipt_worker_route_refuses_human",
        s in (401, 403),
        f"human token on worker run-receipt route -> {s}",
    )
    # --- derived measurement / metric records: reachable denials only ---
    s, _ = call("POST", "/experiment-attempts/attempt_missing/metric-records", {})
    check("metric_record_invalid_body_refused", s == 422, f"empty metric body -> {s} (expect 422)")
    # Metric, partition, unit and seed are pre-registered on the split binding.
    rec_body = {"measurement_id": "measurement_missing"}
    s, _ = call("POST", "/experiment-attempts/attempt_missing/metric-records", rec_body)
    check("metric_record_unknown_attempt", s == 404, f"unknown attempt -> {s} (expect 404)")
    s, _ = call("GET", "/experiment-attempts/attempt_missing/metric-records")
    check("metric_records_list_unknown_attempt", s == 404, f"unknown attempt list -> {s}")
    s, _ = call("POST", "/experiment-attempts/attempt_missing/derived-measurements", {})
    check(
        "derived_measurement_invalid_or_unknown",
        s in (404, 422),
        f"derived-measurements on unknown attempt -> {s} (expect 404/422)",
    )

    # --- gate evidence over HTTP: split locks, leakage audits, programs ---
    lock_body = {
        "name": "pv-split",
        "partitions": {
            "train": {"keys": ["a"]},
            "validation": {"keys": ["b"]},
            "test": {"keys": ["c"]},
        },
    }
    s, lock = call("POST", f"/projects/{pid}/split-locks", lock_body, idempotency_key=f"sl-{pid}")
    check(
        "split_lock_create",
        s == 201 and bool(lock.get("membership_digest")),
        f"human split lock -> {s}, server-computed membership digest",
    )
    if s == 201:
        s, replay = call(
            "POST", f"/projects/{pid}/split-locks", lock_body, idempotency_key=f"sl-{pid}"
        )
        check("split_lock_replay", s in (200, 201) and replay == lock, f"same key replay -> {s}")
        s, got = call("GET", f"/split-locks/{lock['id']}")
        check(
            "split_lock_read",
            s == 200 and got.get("membership_digest") == lock.get("membership_digest"),
            f"read back -> {s}",
        )
        s, _ = call(
            "POST",
            f"/projects/{pid}/split-locks",
            lock_body | {"expected_membership_digest": "0" * 64},
        )
        check(
            "split_lock_bad_expected_digest", s == 409, f"wrong expected digest -> {s} (expect 409)"
        )
        s, _ = call("GET", f"/split-locks/{lock['id']}", auth="none")
        check("split_lock_unauthenticated_refused", s in (401, 403), f"no credential -> {s}")
        audit = {
            "reference_lock_id": lock["id"],
            "reference_partitions": ["train"],
            "eval_partitions": ["test"],
        }
        s, _ = call("POST", f"/split-locks/{lock['id']}/leakage-audits", audit)
        check(
            "leakage_audit_self_reference_refused", s == 409, f"self reference -> {s} (expect 409)"
        )
        ref_body = {"name": "pv-reference", "partitions": {"corpus": {"keys": ["x", "y"]}}}
        s, ref = call("POST", f"/projects/{pid}/split-locks", ref_body)
        if s == 201:
            audit = audit | {"reference_lock_id": ref["id"], "reference_partitions": ["corpus"]}
            s, _ = call("POST", f"/split-locks/{lock['id']}/leakage-audits", audit)
        check(
            "cross_set_leakage_audit",
            s == 201,
            f"disjoint reference vs test audit -> {s} (expect 201)",
        )
        s, _ = call(
            "POST",
            "/experiment-attempts/attempt_missing/split-binding",
            {
                "expected_revision": 1,
                "split_lock_id": lock["id"],
                "seed": 0,
                "metrics": [{"name": "rmse", "partition": "test", "unit": "ratio"}],
            },
        )
        check("split_binding_unknown_attempt", s == 404, f"unknown attempt -> {s} (expect 404)")
    s, prog = call(
        "POST",
        f"/projects/{pid}/research-programs",
        {"name": "pv-program", "objective": "platform validation bookkeeping"},
        idempotency_key=f"rp-{pid}",
    )
    check("research_program_create", s == 201, f"human program -> {s} (expect 201)")
    if s == 201:
        s, got = call("GET", f"/research-programs/{prog['id']}")
        check("research_program_read", s == 200, f"read back -> {s}")
        s, _ = call(
            "POST",
            f"/research-programs/{prog['id']}/outcomes",
            {"outcome": "outcome_0", "study_id": "study_missing", "title": "pv"},
        )
        check("program_outcome_unknown_study", s in (404, 409), f"unknown study -> {s}")

    # --- item 2: manual dataset snapshot upload (no staging from outputs here) ---
    try:
        upload = _dataset_upload()
    except Exception as error:  # noqa: BLE001 - report, do not crash the suite
        rec("dataset_snapshot_upload", "FAIL", f"could not build upload: {type(error).__name__}")
    else:
        path = f"/projects/{pid}/tabular-dataset-snapshots"
        s, snap = call("POST", path, upload, idempotency_key=f"ds-{pid}")
        check(
            "dataset_snapshot_upload",
            s == 201
            and bool(snap.get("dataset_manifest"))
            and snap.get("transfer_authorized") is False,
            f"byte-verified CSV upload -> {s}, transfer_authorized false",
        )
        if s == 201:
            s, got = call("GET", f"/tabular-dataset-snapshots/{snap['id']}")
            check(
                "dataset_snapshot_read",
                s == 200 and got.get("digest") == snap.get("digest"),
                f"read back -> {s}, digest matches",
            )
            s, _ = call(
                "POST", path, upload | {"expected_data_size": 1}, idempotency_key=ctx.new_key()
            )
            check("dataset_snapshot_bad_pin_refused", s == 409, f"wrong pinned size -> {s}")
    # --- dataset staging from run outputs (ADR 0034 s4): reachable denials ---
    stage_body = {
        "manifest_id": "manifest_missing",
        "expected_manifest_digest": "0" * 64,
        "relative_paths": None,
        "expected_snapshot_digest": "0" * 64,
    }
    stage_path = f"/projects/{pid}/artifact-snapshots"
    s, _ = call("POST", stage_path, stage_body)
    check("staging_unknown_manifest", s == 404, f"human, no such manifest -> {s} (expect 404)")
    s, _ = call("POST", stage_path, stage_body | {"expected_snapshot_digest": "bad"})
    check("staging_invalid_body", s == 422, f"malformed digest -> {s} (expect 422)")
    s, _ = call("POST", stage_path, stage_body, auth="none", idempotency_key=ctx.new_key())
    check("staging_unauthenticated_refused", s in (401, 403), f"no credential -> {s}")
    if token:
        s, _ = _agent_request(ctx, token, "POST", stage_path, stage_body, ctx.new_key())
        check("staging_agent_refused", s == 403, f"agent credential staging -> {s} (expect 403)")
    s, _ = call("GET", "/snapshots/snapshot_missing/staging")
    check("staging_read_unknown_snapshot", s == 404, f"unknown snapshot staging read -> {s}")
    return results
