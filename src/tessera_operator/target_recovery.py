"""Bounded online target backup and UID-preconditioned one-shot Pod recreation."""

from __future__ import annotations

import sqlite3
import time

from mcp.types import ToolAnnotations

from . import activation
from . import kubernetes as target
from . import private_import as admission
from .upgrade_support import _identity as _identity
from .upgrade_support import _pod_backup as _pod_backup

BEFORE = target.SOURCE / "private/local-persistent-target-before"
AFTER = target.SOURCE / "private/local-persistent-target-after"
RECREATE = target.SOURCE / "private/local-persistent-recreate.json"
MAX_STREAM_BYTES = 2 * 1024 * 1024 * 1024


def backup_target(stage: str) -> dict:
    if stage not in {"before", "after"}:
        raise target.AdmissionError("fixed target backup stage required")
    pod = activation.ready_pod()
    if stage == "after":
        intent = admission._receipt(RECREATE)
        if pod["pod_uid"] == intent.get("old_pod_uid") or pod["pv_uid"] != intent.get("pv_uid"):
            raise target.AdmissionError("target Pod not recreated on same PVC")
    output = BEFORE if stage == "before" else AFTER
    result = _pod_backup(pod, stage, output)
    if stage == "after":
        old = _identity(BEFORE / "snapshot.sqlite3")
        if (
            result["incarnation"] != old["incarnation"]
            or result["recovery_state"] != old["recovery_state"]
            or result["backend_records_sha256"] != old["backend_records_sha256"]
            or result["event_cursor"] < old["event_cursor"]
        ):
            raise target.AdmissionError("backend identity or watermark changed after restart")
        with (
            sqlite3.connect(
                f"file:{BEFORE / 'snapshot.sqlite3'}?mode=ro&immutable=1", uri=True
            ) as before,
            sqlite3.connect(
                f"file:{AFTER / 'snapshot.sqlite3'}?mode=ro&immutable=1", uri=True
            ) as after,
        ):
            prior = before.execute("SELECT * FROM events ORDER BY cursor").fetchall()
            new = after.execute(
                "SELECT * FROM events WHERE cursor <= ? ORDER BY cursor", (old["event_cursor"],)
            ).fetchall()
            if prior != new:
                raise target.AdmissionError("record prefix changed after restart")
        result["state"] = "recreated_Pod_same_PVC_verified_record_prefix"
    return result


def recreate() -> dict:
    """Delete only exact owned Pod UID/RV after independent verified backup."""
    if RECREATE.exists() or RECREATE.is_symlink():
        raise target.AdmissionError("prior Pod recreation retained; no retry")
    pod = activation.ready_pod()
    verified = target.validate_backup(BEFORE)
    receipt = admission._receipt(BEFORE.with_name(BEFORE.name + ".intent"))
    if receipt.get("pod_uid") != pod["pod_uid"] or receipt.get("pv_uid") != pod["pv_uid"]:
        raise target.AdmissionError("backup and target identity differ")
    item = target._kubectl("-n", target.NAMESPACE, "get", "pod", pod["pod_name"], "-o", "json")
    meta = item.get("metadata", {})
    if meta.get("uid") != pod["pod_uid"] or not meta.get("resourceVersion"):
        raise target.AdmissionError("Pod UID or revision changed")
    activation._write_once(
        RECREATE,
        {
            "old_pod_uid": pod["pod_uid"],
            "pv_uid": pod["pv_uid"],
            "backup_sha256": verified["snapshot_sha256"],
        },
    )
    status = admission._api_request(
        "DELETE",
        f"/api/v1/namespaces/{target.NAMESPACE}/pods/{pod['pod_name']}",
        {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": pod["pod_uid"], "resourceVersion": meta["resourceVersion"]},
        },
    )
    if status not in {200, 202}:
        raise target.AdmissionError("conditional Pod deletion refused; intent retained")
    for _ in range(30):
        try:
            new = activation.ready_pod()
            if new["pod_uid"] != pod["pod_uid"] and new["pv_uid"] == pod["pv_uid"]:
                return {
                    "state": "Pod_replaced; record_readback_pending",
                    "old_pod_uid": pod["pod_uid"],
                    "new_pod_uid": new["pod_uid"],
                    "pv_uid": pod["pv_uid"],
                }
        except target.AdmissionError:
            pass
        time.sleep(1)
    raise target.AdmissionError("Pod replacement unconfirmed; intent retained")


def register(server) -> None:
    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def local_target_backup_before() -> dict:
        """Consistent online backup in owned Pod to independent private Mac storage."""
        return backup_target("before")

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
    )
    def local_pod_recreate() -> dict:
        """UID/RV conditional deletion of only the backed-up owned Pod."""
        return recreate()

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def local_target_backup_after() -> dict:
        """Post-recreate independent backup; verify incarnation and event prefix."""
        return backup_target("after")
