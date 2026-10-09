"""Bounded typed diagnostics; never retain exception text, requests or output bytes."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

ERROR_TYPES = frozenset(
    {
        "RemoteTargetError",
        "KubernetesContractError",
        "KubernetesUnresolvedError",
        "KubernetesExecutionError",
        "ControlChannelError",
        "TimeoutError",
        "ConnectionError",
        "ValueError",
        "OSError",
        "PermissionError",
        "RuntimeError",
        "FileNotFoundError",
    }
)
ERROR_CODES = frozenset(
    {
        "target_validation_failed",
        "kubernetes_http_rejected",
        "kubernetes_quota_exceeded",
        "kubernetes_tls_error",
        "kubernetes_transport_timeout",
        "kubernetes_transport_or_decode_error",
        "projected_token_invalid",
        "network_evidence_stale",
        "network_evidence_invalid",
        "namespace_identity_changed",
        "pvc_identity_changed",
        "network_policy_identity_changed",
        "kubeconfig_identity_changed",
        "deadline_exceeded",
        "callback_in_progress",
        "callback_timeout",
    }
)
STAGES = frozenset(
    {
        "job_admission",
        "preparation",
        "job_create",
        "job_observation",
        "reconciliation",
        "cancellation",
        "output_delivery",
    }
    | {
        mode + "." + step
        for mode in ("prepare", "collect")
        for step in (
            "intent",
            "verify_target",
            "lookup",
            "create",
            "capture_uid",
            "verify_pod",
            "wait",
            "logs",
            "verify_result",
            "commit_result",
            "cleanup",
        )
    }
)


class KubernetesFailureEvidence:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS worker_failures (operation_id TEXT PRIMARY KEY, "
                "stage TEXT NOT NULL, occurred_at TEXT NOT NULL, error_type TEXT NOT NULL, "
                "error_code TEXT NOT NULL, http_status INTEGER)"
            )

    def record(self, operation_id, stage, error):
        if (
            stage not in STAGES
            or not isinstance(operation_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", operation_id)
        ):
            raise ValueError("Invalid diagnostic stage")
        error_type = type(error).__name__
        if error_type not in ERROR_TYPES:
            error_type = "Exception"
        code = getattr(error, "diagnostic_code", None)
        if not isinstance(code, str) or code not in ERROR_CODES:
            code = "exception"
        status = getattr(error, "http_status", None)
        if type(status) is not int or not 100 <= status <= 599:
            status = None
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR IGNORE INTO worker_failures VALUES (?, ?, ?, ?, ?, ?)",
                (operation_id, stage, datetime.now(UTC).isoformat(), error_type, code, status),
            )

    def get(self, operation_id):
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM worker_failures WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return dict(row) if row is not None else None
