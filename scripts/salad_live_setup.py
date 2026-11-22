"""Owner-run control-side authority chain for ONE approved Salad GPU smoke route.

Creates, through the real control API (in-process FastAPI over the existing
state) and the real store classes, every record `salad_live_check` needs for one
approved Salad smoke route, then writes its private live config
(`tessera.salad.live-config.v1`). It performs no Salad, guardian, custody or
other network call and does not dispatch, reserve or start anything.

Never give owner.header to an agent. No secret values belong in arguments; token
files are generated here and never printed. The output directory must not exist;
every request's idempotency key and body digest is persisted there BEFORE it is
sent, so an interrupted run is reconciled from that journal (never by re-running
into the same directory or renewing identities).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SMOKE_WORKLOAD = ROOT / "workloads" / "salad_gpu_smoke" / "smoke.py"
CONFIG_NAME = "salad-live-config.json"
SUMMARY_NAME = "setup-summary.json"
AUTOMATION_LABEL = "salad-live-setup"
INPUT_BYTES = (
    b"Tessera Salad GPU smoke public input marker. The workload generates its own "
    b"public integer matrix; no external or private data is released.\n"
)

# The platform has no outcome or condition catalog: a program plan declares its own
# outcome id and condition keys as data. This script declares one generic smoke
# outcome with a single selected condition; it names no experiment.
SMOKE_OUTCOME = "gpu_route_smoke"
SMOKE_CONDITIONS = {"bounded_smoke_run": "selected"}
# Members of the split lock the smoke's version-2 gate binds to. Like every new plan,
# the smoke plan declares a version-2 bound-gate protocol; there is no exemption.
SMOKE_PARTITION = "smoke"
SMOKE_MEMBERS = ("public-integers-v1",)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
_IMAGE = re.compile(r"ghcr\.io/[a-z0-9][a-z0-9./_-]*@sha256:[a-f0-9]{64}\Z")
_HEX64 = re.compile(r"[a-f0-9]{64}\Z")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")


class SetupError(RuntimeError):
    pass


def _identifier(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise SetupError("control returned an invalid identifier; reconcile from the journal")
    return value


def _validate_workload(args, live):
    """Smoke: --workload-seconds <=300. Generic: --workload-contract owns the runtime."""
    from tessera_control.salad_workload_contract import WorkloadContractV1
    from tessera_control.salad_workload_jwt import strict_json

    args.workload = None
    args.input_manifest = args.input_digest = args.input_data_class = None
    args.input_code_approval = None
    if args.input_dir is None and any(
        getattr(args, name, None) is not None
        for name in (
            "data_class",
            "license",
            "source",
            "approve_private_code",
            "confirm_structured_config",
        )
    ):
        raise SetupError(
            "--data-class, --license, --source, --approve-private-code and "
            "--confirm-structured-config apply only to --input-dir"
        )
    if args.workload_contract is None:
        if args.input_dir is not None:
            raise SetupError("--input-dir requires a --workload-contract that declares an input")
        if args.workload_seconds is None:
            args.workload_seconds = 300
        if not 1 <= args.workload_seconds <= 300:
            raise SetupError("--workload-seconds must be within 1..300")
        if args.workload_seconds > args.max_seconds:
            raise SetupError("--workload-seconds cannot exceed the --max-seconds route window")
        return
    if args.workload_seconds is not None:
        raise SetupError("--workload-seconds is forbidden with --workload-contract")
    path = args.workload_contract
    if not path.is_absolute() or ".." in path.parts:
        raise SetupError("--workload-contract must be an explicit absolute path")
    try:
        contract = WorkloadContractV1.model_validate(strict_json(live._private_bytes(path, 262144)))
    except (OSError, ValueError):
        raise SetupError(
            "--workload-contract must be an owner-only (0600) JSON file holding a valid "
            "tessera.salad.workload.v1 contract"
        ) from None
    if contract.custody_host != args.custody_dns:
        raise SetupError("workload contract custody_host must equal --custody-dns")
    if contract.workload_seconds > args.max_seconds:
        raise SetupError("contract workload_seconds cannot exceed the --max-seconds route window")
    if not contract.outputs_supported():
        # The guardian's reserve refuses the same contracts (ADR 0036 phase 3).
        raise SetupError("outputs_unsupported: declared outputs exceed the 16 MiB custody total")
    if contract.input is None:
        if args.input_dir is not None:
            raise SetupError("--input-dir is forbidden: the workload contract declares no input")
        from tessera_control.salad_workload_contract import check_gpu_runtime_inline_rules

        try:
            # New-route manifest rules (check_staged_input applies them with an input).
            check_gpu_runtime_inline_rules(contract)
        except ValueError as error:
            raise SetupError(f"--workload-contract refused: {error}") from None
    else:
        _validate_input(args, contract, live)
    args.workload = contract


def workload_source_passage(workload):
    """Source text naming pinned fetch hosts and reviewed redirect targets separately."""
    from tessera_control.salad_workload_contract import fetch_destination

    fetch_hosts = sorted({fetch_destination(f.url).host for f in workload.egress.fetches})
    text = (
        "Reviewed generic workload contract; declared provider-wide egress fetches "
        "from: " + (", ".join(fetch_hosts) if fetch_hosts else "no declared hosts") + "."
    )
    if workload.egress.redirect_hosts:
        text += (
            " Reviewed redirect targets (serve expiring signed URLs; not pinned fetch "
            "sources; bytes still pinned by each fetch's sha256): "
            + ", ".join(workload.egress.redirect_hosts)
            + "."
        )
    return text


def capability_network_basis(workload):
    """Review basis for the capability network of a workload_v1 route."""
    text = "network lists exactly the contract's declared egress fetch destinations"
    if workload.egress.redirect_hosts:
        text += (
            " plus its reviewed redirect targets on port 443 ("
            + ", ".join(workload.egress.redirect_hosts)
            + "; they serve expiring signed URLs and are not pinned fetch sources)"
        )
    return text + " (provider-wide egress, recorded not enforced); no actions/credentials"


def _validate_input(args, contract, live):
    """The staged input must be exactly the snapshot the reviewed contract declares."""
    from tessera_control.salad_workload_input import check_staged_input

    path = args.input_dir
    if path is None:
        raise SetupError(
            "--input-dir is required: the workload contract declares a staged input snapshot"
        )
    if not path.is_absolute() or ".." in path.parts:
        raise SetupError("--input-dir must be an explicit absolute path")
    try:
        os.close(live._private_path(path, directory=True))
    except (OSError, ValueError):
        raise SetupError(
            "--input-dir must be an owner-only (0700) directory without symlinks"
        ) from None
    approval = _input_code_approval(args)
    try:
        args.input_manifest, args.input_digest = check_staged_input(contract, path)
    except (OSError, ValueError) as error:
        raise SetupError(f"--input-dir refused: {error}") from None
    _check_input_split(args, approval)
    if approval is not None:
        # With approved code, an input-sourced entry must be one of the approved paths.
        try:
            manifest, digest = check_staged_input(contract, path, approved_code=set(approval.paths))
        except (OSError, ValueError) as error:
            raise SetupError(f"--input-dir refused: {error}") from None
        if digest != args.input_digest:
            raise SetupError("--input-dir changed while it was being checked")


def _input_code_approval(args):
    """The owner's approval of exact input files as private code (ADR 0039, amendment 1).

    Approved code is staged to third-party compute as private, unlicensed code; it is
    never declared public. Only code and config files within the size caps qualify.
    """
    from tessera_control.code_approval import CodeApprovalRefused, parse_code_approval

    paths = getattr(args, "approve_private_code", None)
    if not paths:
        return None
    try:
        return parse_code_approval(
            {
                "paths": list(paths),
                "structured_config_confirmed": list(
                    getattr(args, "confirm_structured_config", None) or ()
                ),
            }
        )
    except CodeApprovalRefused as error:
        raise SetupError(f"--approve-private-code refused: {error}") from None


def _check_input_split(args, approval):
    """Every input file is approved code or declared public data; refuse before writes."""
    from tessera_control.code_approval import CodeApprovalRefused, approved_files

    entries = args.input_manifest["entries"]
    if approval is not None:
        try:
            args.input_code_approval = approved_files(approval, entries)
        except CodeApprovalRefused as error:
            raise SetupError(f"--approve-private-code refused: {error}") from None
    approved = {f["relative_path"] for f in args.input_code_approval or ()}
    data_files = [e["relative_path"] for e in entries if e["relative_path"] not in approved]
    # Without approved code every file is data. A code-only input needs no data
    # declaration, but one that is given must still be a valid public declaration.
    if data_files or getattr(args, "data_class", None) is not None:
        args.input_data_class = _input_data_class(args, data_files)


def _input_data_class(args, data_files=()):
    """The owner's explicit public-data declaration for --input-dir (ADR 0039).

    Salad is third-party compute: it admits only a snapshot recorded as public, and a
    public record needs a license identifier and a source reference. Nothing defaults
    to public; without the declaration nothing is staged. Admission prefers a public
    snapshot over earlier non-public stagings of the same bytes in the project. Bytes the
    org already holds as derived or migrated data cannot be declared public at all.
    """
    from tessera_control.data_class import DataClassDeclaration

    if getattr(args, "data_class", None) != "public":
        named = ", ".join(sorted(data_files)[:3]) + (", ..." if len(data_files) > 3 else "")
        raise SetupError(
            "--input-dir requires --data-class public --license ID --source URL_OR_REF: "
            "Salad admits only staged data declared public with evidence, plus files "
            "approved with --approve-private-code"
            + (f" (not approved code: {named})" if data_files else "")
        )
    try:
        declaration = DataClassDeclaration(
            data_class="public",
            license=getattr(args, "license", None),
            source=getattr(args, "source", None),
        )
    except ValueError:
        raise SetupError(
            "--data-class public requires an identified --license and a --source URL or "
            "reference (printable, no whitespace)"
        ) from None
    return declaration.model_dump(mode="json")


def _staged_input_snapshot(control, args, project_id):
    """Upload the checked input directory as a chunked snapshot; return its id.

    Uses the human chunked-snapshot routes (ADR 0037): begin with the expected
    digest and inventory, put each chunk, complete (control re-verifies every
    chunk, file and the digest before the snapshot row exists).
    """
    from tessera_control.salad_workload_input import staged_chunks

    begin = control.http(
        "input-snapshot-begin",
        "POST",
        f"/projects/{project_id}/chunked-snapshots",
        dict(
            expected_digest=args.input_digest,
            inventory=args.input_manifest,
            data_class=args.input_data_class,
        )
        | (
            {}
            if args.input_code_approval is None
            else {
                "code_approval": {
                    "paths": [f["relative_path"] for f in args.input_code_approval],
                    "structured_config_confirmed": [
                        f["relative_path"]
                        for f in args.input_code_approval
                        if f.get("structured_config_confirmed")
                    ],
                }
            }
        ),
    )
    upload_id = _identifier(begin["upload_id"])
    if begin.get("expected_digest") != args.input_digest:
        raise SetupError("control echoed a different input digest")
    import base64

    try:
        for seq, content in staged_chunks(args.input_dir, args.input_manifest):
            control.http(
                f"input-chunk-{seq}",
                "PUT",
                f"/projects/{project_id}/chunked-snapshots/{upload_id}/chunks/{seq}",
                dict(
                    content_base64=base64.b64encode(content).decode("ascii"),
                    sha256=hashlib.sha256(content).hexdigest(),
                ),
            )
    except (OSError, ValueError) as error:
        raise SetupError(f"--input-dir changed during upload: {error}") from None
    snapshot = control.http(
        "input-snapshot-complete",
        "POST",
        f"/projects/{project_id}/chunked-snapshots/{upload_id}/complete",
    )
    snapshot_id = _identifier(snapshot.get("id"))
    if snapshot.get("digest") != args.input_digest:
        raise SetupError("control stored a different input digest")
    recorded = snapshot.get("data_class")
    if args.input_data_class is not None and (
        type(recorded) is not dict or recorded.get("data_class") != "public"
    ):
        raise SetupError("control did not record the input as public")
    # Shown for review: who stands behind the public claim (never a human by default).
    args.input_decision_class = (
        None if args.input_data_class is None else str(recorded.get("decision_class"))
    )
    code = snapshot.get("code_approval")
    if args.input_code_approval is not None and (
        type(code) is not dict
        or code.get("valid") is not True
        or code.get("files") != args.input_code_approval
    ):
        raise SetupError("control did not record the approved private code")
    args.input_code_decision_class = None if code is None else str(code.get("decision_class"))
    return snapshot_id


def _input_basis(args):
    """Review text: the public data evidence and the approved private code."""
    parts = []
    if args.input_data_class is not None:
        parts.append(
            "recorded data class public on the owner's declaration (license "
            f"{args.input_data_class['license']}, source {args.input_data_class['source']}, "
            f"decision_class {getattr(args, 'input_decision_class', None)})"
        )
    if args.input_code_approval is not None:
        files = ", ".join(
            f"{f['relative_path']} sha256 {f['sha256']}" for f in args.input_code_approval
        )
        parts.append(
            "private code approved for this remote run, not public and not licensed "
            f"(decision_class {getattr(args, 'input_code_decision_class', None)}): {files}"
        )
        structured = sorted(
            f["relative_path"]
            for f in args.input_code_approval
            if f.get("structured_config_confirmed")
        )
        if structured:
            parts.append(
                "structured, data-capable approved files confirmed by the owner as "
                f"configuration, not data (at most 64 KiB each): {', '.join(structured)}"
            )
    return "; ".join(parts)


def _marker_snapshot(control, journal, args, owner, project_id):
    """The smoke's generated public marker snapshot (local human adoption)."""
    from tessera_control.snapshots import SnapshotStore
    from tessera_control.workspace import capture_workspace, inventory_digest

    # Local human snapshot adoption has no HTTP route; use the real store.
    os.mkdir("input-workspace", 0o700, dir_fd=journal.fd)
    workspace = args.output_dir / "input-workspace"
    workspace_fd = os.open(
        "input-workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=journal.fd
    )
    try:
        journal.write("input.txt", INPUT_BYTES, directory=workspace_fd)
    finally:
        os.close(workspace_fd)
    capture = capture_workspace(workspace)
    snapshot_digest = inventory_digest(capture.inventory)
    snapshot = control.store(
        "snapshot",
        f"/projects/{project_id}/snapshots (local adoption)",
        snapshot_digest,
        lambda key: SnapshotStore(args.state_dir).adopt_snapshot(
            owner, project_id, key, snapshot_digest, capture
        ),
    )
    return snapshot_digest, _identifier(snapshot["id"])


def _validate(args):
    from tessera_control.salad_live_check import _private_path

    for name in (
        "state_dir",
        "owner_header",
        "output_dir",
        "custody_ca_file",
        "custody_token_file",
        "credential_directory",
        "guardian_socket",
    ):
        path = getattr(args, name)
        if not path.is_absolute() or ".." in path.parts:
            raise SetupError(f"--{name.replace('_', '-')} must be an explicit absolute path")
    # Exactly the private-path checks the live session applies later.
    try:
        for path in (args.state_dir, args.credential_directory):
            os.close(_private_path(path, directory=True))
        for path in (
            args.state_dir / "tessera.sqlite3",
            args.owner_header,
            args.custody_ca_file,
            args.custody_token_file,
        ):
            os.close(_private_path(path))
        os.close(_private_path(args.output_dir.parent, directory=True))
        if args.executor_dir is not None:
            if not args.executor_dir.is_absolute() or ".." in args.executor_dir.parts:
                raise ValueError("executor dir")
            os.close(_private_path(args.executor_dir, directory=True))
            os.close(_private_path(args.executor_dir / "worker-token"))
    except (OSError, ValueError):
        raise SetupError(
            "state/credential/custody/owner/executor paths and the output parent must "
            "already be explicit absolute owner-only paths (0700 directories, 0600 "
            "single-link files) without symlinks"
        ) from None
    if os.path.lexists(args.output_dir):
        raise SetupError("output directory already exists; use a new path (no resume)")
    if not _IMAGE.fullmatch(args.image):
        raise SetupError("--image must be ghcr.io/<lowercase repository>@sha256:<64 hex>")
    for value, label in ((args.salad_org, "--salad-org"), (args.salad_project, "--salad-project")):
        if not _NAME.fullmatch(value):
            raise SetupError(f"{label} must be an exact lowercase Salad name")
    if not _UUID.fullmatch(args.gpu_class_id):
        raise SetupError("--gpu-class-id must be a lowercase GPU class UUID")
    dns = args.custody_dns
    if (
        len(dns) > 64
        or not dns.endswith(".ts.net")
        or not all(_LABEL.fullmatch(label) for label in dns.split("."))
    ):
        raise SetupError("--custody-dns must be an exact *.ts.net hostname of at most 64 chars")
    if not _HOST.fullmatch(args.custody_admin_host):
        raise SetupError("--custody-admin-host must be an exact lowercase hostname")
    if not _HEX64.fullmatch(args.custody_cert_sha256):
        raise SetupError("--custody-cert-sha256 must be 64 lowercase hex")
    if not re.fullmatch(r"credential-ref:[a-z0-9-]{1,63}", args.credential_reference):
        raise SetupError("--credential-reference must be credential-ref:<name>")
    if not 1 <= args.credential_version <= 2**31 - 1:
        raise SetupError("--credential-version must be a positive integer")
    if not 0 <= args.guardian_uid <= 2**31 - 1:
        raise SetupError("--guardian-uid out of range")
    if not re.fullmatch(r"[A-Z]{2}", args.country):
        raise SetupError("--country must be an ISO 3166-1 alpha-2 code, e.g. US")
    if not re.fullmatch(r"[a-f0-9]{40}", args.source_commit):
        raise SetupError("--source-commit must be the reviewed 40-hex git commit")
    from tessera_control import salad_live_check as live

    for value, low, high, label in (
        # The experiment budget ceiling is capped at 5000 by the control schema.
        (args.max_cost_cents, 1, 5000, "--max-cost-cents"),
        # Route window (create -> output -> stop) includes node allocation and
        # image pull; the workload's own runtime limit is separate and <=300s.
        (args.max_seconds, 1, live.MAX_ROUTE_SECONDS, "--max-seconds"),
        (args.max_polls, 1, live.MAX_POLLS, "--max-polls"),
        (args.authority_minutes, 5, 1440, "--authority-minutes"),
    ):
        if not low <= value <= high:
            raise SetupError(f"{label} must be within {low}..{high}")
    _validate_workload(args, live)
    for value, label in ((args.watchdog_profile, "watchdog"), (args.monitor_profile, "monitor")):
        if not _HEX64.fullmatch(value):
            raise SetupError(f"--{label}-profile must be 64 lowercase hex")
    from tessera_control.salad_resources import salad_resource_refusal

    # Salad's documented create limits (stricter prose, not the schema); the same
    # check runs at route admission and in the transport before the create POST.
    refusal = salad_resource_refusal(
        cpu_millicores=args.cpu_millicores,
        memory_mb=args.memory_mb,
        disk_mb=args.disk_mb,
        gpu_class=args.gpu_class_id,
        gpu_count=1,
    )
    if refusal is not None:
        field, message = refusal
        flag = {"cpu_millicores": "--cpu-millicores", "memory_mb": "--memory-mb",
                "disk_mb": "--disk-mb"}.get(field, "--gpu-class-id")  # fmt: skip
        raise SetupError(f"{flag}: {message}")
    if args.workload is not None:
        work = args.workload
        declared = work.egress.declared_bytes() + (work.input.max_bytes if work.input else 0)
        if declared > args.disk_mb * 1024 * 1024:
            # Control refuses the same route at proposal and admission time.
            raise SetupError("declared fetch and input bytes exceed --disk-mb")
    if args.workload is None and not SMOKE_WORKLOAD.is_file():
        raise SetupError("reviewed smoke workload source is missing from this checkout")


class Journal:
    """Private exclusive files under a new 0700 directory; never secret values."""

    def __init__(self, path):
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent)
            os.fsync(parent)
            self.fd = os.open(
                path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
        finally:
            os.close(parent)
        info = os.fstat(self.fd)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            raise SetupError("output directory must be owner-only 0700")
        self.path = path
        os.mkdir("journal", 0o700, dir_fd=self.fd)
        self.journal = os.open(
            "journal", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd
        )
        self.sequence = 0

    def write(self, name, value, *, directory=None):
        directory = self.fd if directory is None else directory
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)

    def record(self, step, request):
        self.sequence += 1
        self.write(
            f"{self.sequence:02d}-{step}.request.json", _encode(request), directory=self.journal
        )
        return self.sequence

    def response(self, sequence, step, response):
        self.write(
            f"{sequence:02d}-{step}.response.json", _encode(response), directory=self.journal
        )

    def close(self):
        os.close(self.journal)
        os.close(self.fd)


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class Control:
    """Real control API in-process over existing state, plus real stores where no
    HTTP route exists (local snapshot adoption, trusted-server route review)."""

    def __init__(self, state_dir, owner_token, journal):
        from fastapi.testclient import TestClient

        from tessera_control.api import create_app
        from tessera_control.store import Store

        self.state_dir = state_dir
        self.owner_token = owner_token
        self.journal = journal
        self.client = TestClient(create_app(state_dir), raise_server_exceptions=False)
        self.owner = Store(state_dir).authenticate(owner_token)
        # Owner-made decisions are labelled automation so they are never mistaken
        # for a person's approval (see docs/agent-authority-interface.md).
        self.owner["automation_label"] = AUTOMATION_LABEL
        if self.owner["role"] != "human":
            raise SetupError("owner header must authenticate a human owner")

    def http(self, step, method, path, body=None, *, token=None):
        from tessera_control.store import canonical_digest

        key = secrets.token_hex(24)
        sequence = self.journal.record(
            step,
            dict(
                method=method,
                path=path,
                idempotency_key=key,
                body_digest=None if body is None else canonical_digest(body),
                principal="worker" if token else "owner",
            ),
        )
        headers = {"Authorization": "Bearer " + (token or self.owner_token)}
        if token is None:
            headers["X-Tessera-Automation"] = AUTOMATION_LABEL
        if method != "GET":
            headers["Idempotency-Key"] = key
        response = self.client.request(method, path, json=body, headers=headers)
        if response.status_code not in (200, 201):
            detail = None
            if "credential" not in step:  # validation errors may echo submitted tokens
                try:
                    detail = response.json().get("detail")
                except Exception:
                    detail = None
            raise SetupError(
                f"step {step} refused: HTTP {response.status_code}"
                + (f" ({detail})" if isinstance(detail, str) else "")
                + "; retain the output directory and reconcile from its journal"
            )
        value = response.json()
        self.journal.response(sequence, step, value)
        return value

    def store(self, step, path, payload_digest, call):
        key = secrets.token_hex(24)
        sequence = self.journal.record(
            step,
            dict(
                method="STORE",
                path=path,
                idempotency_key=key,
                body_digest=payload_digest,
                principal="owner",
            ),
        )
        try:
            status, value = call(key)
        except Exception as error:
            raise SetupError(
                f"step {step} refused: {type(error).__name__}: {error}; "
                "retain the output directory and reconcile from its journal"
            ) from None
        self.journal.response(sequence, step, value)
        return value


def _execution_profile(args, capability_digest, operation_id, epoch):
    if args.workload is not None:
        contract = args.workload
        return dict(
            platform="linux/amd64",
            command="/tessera/bin/run",
            argv=contract.derived_argv(operation_id, epoch)[1:],
            argv_binding="workload_v1",
            workload=contract.model_dump(mode="json"),
            env=dict(contract.env),
            max_output_bytes=contract.output_bytes,
            max_output_files=len(contract.outputs),
            capability_digest=capability_digest,
            network_policy="provider_broad_egress_explicitly_accepted",
            # Approved private code is not public bytes: the route literal follows admission.
            data_policy=(
                "provider_trust_policy_v1"
                if getattr(args, "input_code_approval", None)
                else "public_released_only"
            ),
            artifact_destination=args.artifact_destination,
            checkpoint_destination=args.checkpoint_destination,
            monitor_profile=args.monitor_profile,
            watchdog_profile=args.watchdog_profile,
            max_cost_cents=args.max_cost_cents,
            credit_exposure="unknown_blocks_new_work_credit_not_hard_cap",
        )
    return dict(
        platform="linux/amd64",
        command="/tessera/bin/run",
        argv=[
            "v1",
            "public-integers-v1",
            operation_id,
            epoch,
            args.custody_dns,
            args.watchdog_profile,
            str(args.workload_seconds),
            "1024",
            "1",
            "4",
        ],
        argv_binding="smoke_operation_v1",
        env={},
        max_output_bytes=1024,
        max_output_files=1,
        capability_digest=capability_digest,
        network_policy="provider_broad_egress_explicitly_accepted",
        data_policy="public_released_only",
        artifact_destination=args.artifact_destination,
        checkpoint_destination=args.checkpoint_destination,
        monitor_profile=args.monitor_profile,
        watchdog_profile=args.watchdog_profile,
        max_cost_cents=args.max_cost_cents,
        credit_exposure="unknown_blocks_new_work_credit_not_hard_cap",
    )


def _selection(args, execution, *, operation_id, snapshot_digest, derived):
    from tessera_control.remote_execution_models import ExecutionSelection

    return ExecutionSelection.model_validate(
        dict(
            target="salad_container",
            target_digest=derived["target_digest"],
            resources=dict(
                cpu_millicores=args.cpu_millicores,
                memory_mb=args.memory_mb,
                gpu_class=args.gpu_class_id,
                gpu_count=1,
                disk_mb=args.disk_mb,
                max_seconds=args.max_seconds,
            ),
            manifests=dict(
                image=args.image,
                data=snapshot_digest,
                code=derived["code_digest"],
                model=derived["model_digest"],
                environment=derived["environment_digest"],
            ),
            salad=dict(
                organization=args.salad_org,
                project=args.salad_project,
                group="attempt-" + operation_id,
                country=args.country,
                provider_trust="broad_egress_ephemeral_reallocation_credit_not_hard_cap",
                execution=execution,
            ),
        )
    )


def _enroll_route_worker(control, args, worker_token, expires_at, inventory):
    """Per-route project, worker credential and registration (no --executor-dir)."""
    project = control.http("project", "POST", "/projects", {"name": args.project_name})
    project_id = _identifier(project["id"])
    worker_credential = control.http(
        "worker-credential",
        "POST",
        "/credentials",
        dict(
            token=worker_token,
            kind="worker",
            audience="worker",
            project_id=project_id,
            expires_at=expires_at,
        ),
    )
    worker_principal = _identifier(worker_credential["principal_id"])
    registration = control.http(
        "worker-registration",
        "POST",
        f"/projects/{project_id}/workers",
        dict(
            worker_principal_id=worker_principal,
            approved_build_identifier="tessera-trusted-salad-server-v1",
            approved_build_digest=inventory,
            trust_class="trusted-server",
            region="salad-" + args.country.lower(),
        ),
    )
    if registration.get("worker_principal_id") != worker_principal:
        raise SetupError("worker registration identity mismatch")
    return project_id, worker_principal, registration


SMOKE_SPLIT_LOCK_NAME = "GPU route smoke reference"


def _smoke_membership_digest():
    """Control's membership digest for the constant smoke partition (see gate_evidence_splits)."""
    from tessera_control.gate_evidence_splits import canonical_digest

    members = sorted([key, None] for key in SMOKE_MEMBERS)
    partition = canonical_digest({"partition": SMOKE_PARTITION, "members": members})
    return canonical_digest({"version": 1, "partitions": {SMOKE_PARTITION: partition}})


def _existing_smoke_split_lock(control, project_id):
    """A stable executor's shared project reuses its one smoke reference lock.

    Control refuses a second lock with the same membership in a project, and the smoke
    membership is constant, so an earlier route's lock is the only valid one. It is read
    back through the API (never trusted from the database row alone) and must carry
    exactly the smoke membership digest.
    """
    import contextlib
    import sqlite3

    expected = _smoke_membership_digest()
    database = (Path(control.state_dir) / "tessera.sqlite3").resolve()
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT id FROM gate_split_locks WHERE project_id=? AND membership_digest=?",
            (project_id, expected),
        ).fetchall()
    for (lock_id,) in rows:
        lock = control.http("split-lock-reuse", "GET", f"/split-locks/{lock_id}")
        if lock.get("project_id") == project_id and lock.get("membership_digest") == expected:
            return lock
    return None


MAX_PROJECT_CONCURRENCY = 8  # control CHECK (max_concurrency BETWEEN 1 AND 8)


def _execution_limits(control, project_id, max_cost_cents, shared):
    """Project limit body: this route's cap on top of any held/settled exposure.

    A per-route project starts at revision 0 with nothing held. A stable executor's
    shared project already carries earlier routes' settled and held exposure: live
    routes end with their grant cancel_requested and nothing reconciles it yet (ADR 0041
    settlement), so each keeps holding its cap. The limit is raised by exactly this
    route's cap at the current revision and concurrency admits one more grant than are
    unresolved, which reproduces the per-route-project bounds; the grant's own
    max_exposure_cents still bounds this route, and the Salad guardian's
    single-operation orphan fence remains the concurrency fence. Control caps
    max_concurrency at 8, so an enrollment serves at most 7 unreconciled routes before
    it must be rotated.
    """
    revision, exposure, unresolved = 0, 0, 0
    if shared:
        response = control.client.get(
            f"/projects/{project_id}/execution-limits",
            headers={"Authorization": "Bearer " + control.owner_token},
        )
        if response.status_code == 200:
            current = response.json()
            revision = current["revision"]
            exposure = current["held_cents"] + current["settled_cents"]
        elif response.status_code != 409:  # 409: limits not configured yet
            raise SetupError(f"execution limits read refused: HTTP {response.status_code}")
        offset = 0
        while True:
            page = control.client.get(
                f"/projects/{project_id}/execution-grants",
                params={"offset": offset, "limit": 200},
                headers={"Authorization": "Bearer " + control.owner_token},
            )
            if page.status_code != 200:
                raise SetupError(f"execution grants read refused: HTTP {page.status_code}")
            grants = page.json()["grants"]
            unresolved += sum(g["status"] in {"active", "cancel_requested"} for g in grants)
            if len(grants) < 200:
                break
            offset += 200
        if unresolved >= MAX_PROJECT_CONCURRENCY:
            raise SetupError(
                f"executor project holds {unresolved} unreconciled grants; rotate the "
                "executor (enroll a new one, update the trust file, reload, revoke the old)"
            )
    return dict(
        expected_revision=revision,
        currency="USD",
        monetary_limit_cents=exposure + max_cost_cents,
        max_concurrency=unresolved + 1,
    )


def _check_executor_at_control(args, executor, worker_token):
    """Before any write: control must agree the executor's credential is live and exact.

    executor.json can drift from control (revoked through the API, a crash between a
    control call and the record write). Without this, setup would fail only at the
    worker lease, after leaving a grant in the shared project.
    """
    from tessera_control.executor_enrollment import MAX_LIFETIME
    from tessera_control.store import Store, StoreError, _connect

    store = Store(args.state_dir)
    try:
        worker = store.authenticate_worker(worker_token)
    except StoreError:
        raise SetupError("executor credential is not live at control; renew or rotate it") from None

    def utc(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)

    connection = _connect(store.database_path)
    try:
        issued = connection.execute(
            "SELECT created_at FROM credentials WHERE id=?", (worker["credential_id"],)
        ).fetchone()
    finally:
        connection.close()
    now, expires = datetime.now(UTC), utc(worker["expires_at"])
    if (
        worker["id"] != executor.worker_principal_id
        or worker["project_id"] != executor.project_id
        or worker["credential_id"] != executor.credential_id
        or expires < now + timedelta(minutes=args.authority_minutes + 10)
    ):
        raise SetupError("executor record does not match control (principal, project or expiry)")
    # The seven-day bound, checked against control's own rows, not only executor.json.
    if (
        issued is None
        or expires > utc(executor.not_after)
        or now - utc(issued["created_at"]) > MAX_LIFETIME
        or expires > utc(issued["created_at"]) + MAX_LIFETIME
    ):
        raise SetupError("executor credential is past its seven-day bound at control; rotate it")


def owner_token_from_header(path):
    """The owner bearer token from a private ``Authorization: Bearer`` header file."""
    from tessera_control.salad_live_check import _private_bytes

    header = _private_bytes(path, 4096).decode("ascii", "strict").strip()
    if not re.fullmatch(r"Authorization: Bearer [A-Za-z0-9_-]{43,256}", header):
        raise SetupError("invalid private owner header")
    return header.removeprefix("Authorization: Bearer ")


def setup(args):
    from tessera_control.capability_models import CapabilityDeclaration
    from tessera_control.remote_execution import RemoteExecutionStore
    from tessera_control.remote_execution_models import TargetPolicy
    from tessera_control.salad_live_check import _config, source_digest
    from tessera_control.salad_proxy_contract import ManagementCredential, ProxyPolicy
    from tessera_control.store import canonical_digest

    _validate(args)
    owner_token = owner_token_from_header(args.owner_header)

    image_digest = args.image.split("@", 1)[1]
    if args.workload is None:
        smoke_sha256 = hashlib.sha256(SMOKE_WORKLOAD.read_bytes()).hexdigest()
    else:
        # The reviewed contract is the code identity of a generic workload route.
        smoke_sha256 = canonical_digest(args.workload.model_dump(mode="json"))
    inventory = source_digest()
    declaration = CapabilityDeclaration(
        environment_id=args.capability_environment_id,
        image_digest=image_digest,
        network=[] if args.workload is None else args.workload.capability_network(),
    ).model_dump(mode="json")
    capability_digest = canonical_digest(declaration)
    derived = dict(
        # Opaque route/adapter key; the fixture used "f"*64. Bound to the exact target.
        target_digest=canonical_digest(
            dict(
                target="salad_container",
                organization=args.salad_org,
                project=args.salad_project,
                gpu_class=args.gpu_class_id,
                country=args.country,
                image=args.image,
            )
        ),
        code_digest=smoke_sha256,
        # The smoke workload loads no model; digest of the explicit "no model" marker.
        model_digest=canonical_digest(
            {"model": None, "workload": "salad_gpu_smoke" if args.workload is None else "contract"}
        ),
        environment_digest=canonical_digest(
            dict(
                platform="linux/amd64",
                env={} if args.workload is None else dict(args.workload.env),
                image=args.image,
            )
        ),
    )
    # Validate the whole profile shape before any mutation (operation ID unknown yet).
    _selection(
        args,
        _execution_profile(args, capability_digest, "operation-id", "operation-epoch"),
        operation_id="0" * 32,
        snapshot_digest=args.input_digest or "0" * 64,
        derived=derived,
    )

    now = datetime.now(UTC)
    expires_at = (now + timedelta(minutes=args.authority_minutes)).isoformat()
    executor = None
    if args.executor_dir is not None:
        # A stable, owner-enrolled executor (docs/provider-trust.md, Stable executor):
        # reuse its project, worker principal and registration so the provider trust
        # setting can list the principal once. It must stay live for this whole route.
        from tessera_control import executor_enrollment

        try:
            executor, worker_token = executor_enrollment.load(
                args.executor_dir,
                provider="salad",
                needed_until=now + timedelta(minutes=args.authority_minutes + 10),
            )
        except executor_enrollment.ExecutorError as error:
            raise SetupError(f"executor refused: {error}") from None
        expected_labels = (
            "tessera-trusted-salad-server-v1",
            "trusted-server",
            "salad-" + args.country.lower(),
        )
        labels = (executor.approved_build_identifier, executor.trust_class, executor.region)
        if labels != expected_labels:
            raise SetupError("executor registration labels do not match this Salad route")
        _check_executor_at_control(args, executor, worker_token)
    else:
        worker_token = secrets.token_urlsafe(48)
    if len({worker_token, owner_token}) != 2:
        raise SetupError("generated identities must be distinct")

    journal = Journal(args.output_dir)
    try:
        out = args.output_dir
        # Tokens are persisted (0600, own file) BEFORE the issuing request is sent.
        if executor is None:
            journal.write("worker-token", worker_token.encode())
        control = Control(args.state_dir, owner_token, journal)
        owner = control.owner

        if executor is not None:
            project_id = _identifier(executor.project_id)
            worker_principal = _identifier(executor.worker_principal_id)
            registration = dict(id=executor.registration_id, worker_principal_id=worker_principal)
            worker_credential_file = args.executor_dir / executor_enrollment.TOKEN_NAME
        else:
            project_id, worker_principal, registration = _enroll_route_worker(
                control, args, worker_token, expires_at, inventory
            )
            worker_credential_file = out / "worker-token"
        if args.input_digest is not None:
            # The reviewed staged input (the contract's declared input snapshot).
            snapshot_digest = args.input_digest
            snapshot_id = _staged_input_snapshot(control, args, project_id)
        else:
            snapshot_digest, snapshot_id = _marker_snapshot(
                control, journal, args, owner, project_id
            )
        control.http(
            "snapshot-release",
            "POST",
            f"/snapshots/{snapshot_id}/releases",
            dict(
                expected_digest=snapshot_digest,
                worker_principal_id=worker_principal,
                classification="public",
                permits_worker_transfer=True,
                rationale=(
                    "Public smoke marker only; the workload generates public integers."
                    if args.input_digest is None
                    else "Owner-staged public workload input declared by the reviewed contract."
                ),
                expires_at=expires_at,
            ),
        )
        plan_text = dict(
            title="Salad GPU smoke",
            purpose="One bounded owner-approved Salad CUDA smoke run with quarantined output.",
            budget_cents=args.max_cost_cents,
            # Plan bound is billed workload time (shared 3600 s limit); the Salad route
            # window may exceed it by the unbilled provisioning allowance.
            max_duration_seconds=min(args.max_seconds, 3600),
        )
        task = control.http("task", "POST", f"/projects/{project_id}/tasks", plan_text)
        task_id = _identifier(task["id"])
        if args.workload is None:
            argv_template = _execution_profile(
                args, capability_digest, "operation-id", "operation-epoch"
            )["argv"]
            output_paths, max_output_bytes = ["output.json"], 1024
        else:
            argv_template = list(args.workload.argv_template)
            output_paths = [o.name for o in args.workload.outputs]
            max_output_bytes = args.workload.output_bytes
        task = control.http(
            "task-plan",
            "PUT",
            f"/tasks/{task_id}/plan",
            dict(
                plan_text,
                expected_revision=task["revision"],
                execution_spec=dict(
                    argv=["/tessera/bin/run", *argv_template],
                    input_snapshot_digest=snapshot_digest,
                    pinned_skill_digests=[],
                    output_paths=output_paths,
                    profile="kubernetes-opencode",
                    # Base plan contract only: it is not a claim about Salad's
                    # provider-wide egress, which workload_v1 declares explicitly.
                    network_enabled=False,
                    max_output_bytes=max_output_bytes,
                ),
            ),
        )
        task = control.http(
            "task-approve",
            "POST",
            f"/tasks/{task_id}/approve",
            dict(
                expected_revision=task["revision"], expected_plan_digest=task["current_plan_digest"]
            ),
        )
        control.http(
            "execution-limits",
            "PUT",
            f"/projects/{project_id}/execution-limits",
            _execution_limits(control, project_id, args.max_cost_cents, executor is not None),
        )
        grant = control.http(
            "execution-grant",
            "POST",
            f"/projects/{project_id}/execution-grants",
            dict(
                task_id=task_id,
                expected_task_revision=task["revision"],
                expected_plan_digest=task["current_plan_digest"],
                worker_principal_id=worker_principal,
                snapshot_id=snapshot_id,
                pinned_image=args.image,
                expires_at=expires_at,
                max_exposure_cents=args.max_cost_cents,
                currency="USD",
                acknowledges_unmeasured_infrastructure=True,
            ),
        )
        grant_id = _identifier(grant["id"])
        variant_body = dict(
            method_digest=smoke_sha256,
            input_digest=snapshot_digest,
            metadata={"workload": "salad_gpu_smoke"},
            execution_task_id=task_id,
            execution_plan_digest=grant["plan_digest"],
        )
        experiment = control.http(
            "experiment",
            "POST",
            f"/projects/{project_id}/experiments",
            dict(
                goal="Observe one bounded Salad CUDA smoke output under quarantined custody.",
                success_criterion="A human reviews the retained output against the reference.",
                protocol="Run only the admitted baseline smoke attempt once; no retries.",
                variants=[
                    dict(variant_body, kind="baseline", name="baseline"),
                    dict(variant_body, kind="intervention", name="intervention-not-admitted"),
                ],
            ),
        )
        experiment_id = _identifier(experiment["id"])
        # Real human budget authorization BEFORE admission; every bound is declared.
        budget = control.http(
            "budget-authorization",
            "POST",
            f"/experiments/{experiment_id}/budget/authorization",
            dict(
                expected_revision=0,
                ceiling_cents=args.max_cost_cents,
                expires_at=expires_at,
                evidence_digest=canonical_digest(
                    dict(
                        approval="owner-approved-salad-smoke",
                        max_cost_cents=args.max_cost_cents,
                        image=args.image,
                        gpu_class=args.gpu_class_id,
                        organization=args.salad_org,
                        project=args.salad_project,
                    )
                ),
                source_reference=args.budget_source_reference,
                prior_settled_cents=0,
                prior_pending_cents=0,
                ancillary_cents=0,
                shutdown_cents=0,
            ),
        )
        baseline = next(v for v in experiment["design"]["variants"] if v["kind"] == "baseline")
        admitted = control.http(
            "admit-attempt",
            "POST",
            f"/experiment-variants/{_identifier(baseline['id'])}/admitted-attempts",
            dict(
                expected_variant_revision=baseline["revision"],
                expected_design_revision=experiment["current_design_revision"],
                grant_id=grant_id,
            ),
        )
        if executor is not None:
            # A long-lived executor's registry sighting can be older than the 60 s
            # disconnect bound; the worker itself reports liveness before leasing.
            control.http("worker-heartbeat", "POST", "/worker/heartbeat", {}, token=worker_token)
        lease = control.http(
            "lease", "POST", "/worker/leases", {"grant_id": grant_id}, token=worker_token
        )
        lease_id = _identifier(lease["id"])
        response = control.http(
            "execution-package",
            "POST",
            f"/worker/leases/{lease_id}/package",
            dict(
                expected_epoch=lease["epoch"],
                admitted_attempt_id=_identifier(admitted["id"]),
                execution_adapter="kubernetes-job",
            ),
            token=worker_token,
        )
        package = response["package"]
        operation_id, epoch = package["operation_id"], package["epoch"]

        # Human program/source-use reviews linked to the admitted experiment.
        study = control.http(
            "study",
            "POST",
            f"/projects/{project_id}/studies",
            dict(
                purpose="Operational Salad GPU smoke, not a scientific experiment",
                protocol="One bounded admitted attempt with quarantined output",
            ),
        )
        study_id = _identifier(study["id"])
        if args.workload is None:
            source_passage = "Public integer matrix generated in-workload; no external data."
        else:
            source_passage = workload_source_passage(args.workload)
        source = control.http(
            "source",
            "POST",
            f"/studies/{study_id}/sources",
            dict(
                uri="urn:tessera:workload:"
                + ("salad_gpu_smoke" if args.workload is None else "contract-v1"),
                quoted_passage=source_passage,
                content_digest=smoke_sha256,
                locator="workloads/salad_gpu_smoke/smoke.py"
                if args.workload is None
                else "workload-contract.json",
            ),
        )
        program_variant = control.http(
            "study-variant",
            "POST",
            f"/studies/{study_id}/variants",
            dict(
                name="Salad GPU smoke baseline",
                method_digest=smoke_sha256,
                input_digest=snapshot_digest,
                metadata={"experiment_id": experiment_id},
            ),
        )
        # Version-2 bound gates reference a platform split lock; the smoke's lock holds
        # only the public generated-input marker, so it implies no dataset.
        split_lock = _existing_smoke_split_lock(control, project_id) if executor else None
        if split_lock is None:
            split_lock = control.http(
                "split-lock",
                "POST",
                f"/projects/{project_id}/split-locks",
                dict(
                    name=SMOKE_SPLIT_LOCK_NAME,
                    partitions={SMOKE_PARTITION: dict(keys=list(SMOKE_MEMBERS))},
                ),
            )
        smoke_protocol = dict(
            version=2,
            # The route requires this identity to equal the selection's manifests.
            identity=dict(
                data_digest=package["snapshot_digest"],
                code_digest=derived["code_digest"],
                environment_digest=derived["environment_digest"],
            ),
            gates=[
                dict(
                    id="smoke_output_matches_reference",
                    category="experimental_ground_truth",
                    variant_id=_identifier(program_variant["id"]),
                    experiment_variant_id=_identifier(baseline["id"]),
                    metric="reference_match",
                    unit="fraction",
                    split_lock_id=_identifier(split_lock["id"]),
                    split_partition=SMOKE_PARTITION,
                    split_digest=split_lock["membership_digest"],
                    procedure=(
                        "Platform-derived fraction of retained smoke outputs matching the "
                        "deterministic reference. One admitted smoke attempt cannot meet the "
                        "replicate minimum, so this gate stays inconclusive; route approval "
                        "is not a scientific result."
                    ),
                    comparison="eq",
                    threshold=1.0,
                    statistic="mean",
                    minimum_replicates=2,
                    minimum_samples=1,
                    allowed_sources=["derived"],
                ),
            ],
        )
        control.http(
            "source-assess",
            "POST",
            f"/sources/{_identifier(source['id'])}/assess",
            dict(
                expected_revision=1,
                verdict="supported",
                reason="Workload source reviewed by the owner",
            ),
        )

        def decide(step, action):
            context = control.http(
                step + "-context", "GET", f"/interaction/studies/{study_id}/program-context"
            )
            scope = dict(
                type="research_program",
                version=1,
                study_id=study_id,
                outcome=SMOKE_OUTCOME,
                action=action,
                basis_digest=context["basis_digest"],
                supersedes_id=None,
                reason="Owner-approved Salad GPU smoke route",
            )
            if action == "plan":
                scope.update(
                    brief=dict(
                        objective="Exercise one bounded Salad CUDA smoke run",
                        hypothesis="The approved image produces the deterministic smoke output",
                        success_gate="Retained output matches the deterministic reference",
                        failure_gate="Any missing, mismatched or unretained output",
                        stop_gate="Stop at the declared time and cost cap",
                        target_domain="Infrastructure smoke",
                        provenance_policy="Record image, operation, hashes and uncertainty",
                        data_source_ids=[source["id"]],
                        compute=dict(
                            budget_cents=args.max_cost_cents,
                            max_wall_seconds=args.max_seconds,
                            max_gpu_hours=round(args.max_seconds / 3600, 6),
                            target="salad_container",
                            resources="One Salad GPU container, exact reviewed class",
                        ),
                        dataset_recommendation="No dataset; public generated integers only",
                        representation_recommendation="Not applicable to the smoke",
                        split_recommendation="Not applicable to the smoke",
                        metric_recommendation="Exact checked sum and CUDA assertions",
                        uncertainty="Workload-reported proof, not GPU attestation",
                        missing_prerequisites=[],
                    ),
                    conditions={
                        name: dict(
                            disposition=disposition,
                            rationale="Only the bounded smoke is in scope; deferred is not tested",
                            variant_ids=(
                                [program_variant["id"]] if disposition == "selected" else []
                            ),
                        )
                        for name, disposition in SMOKE_CONDITIONS.items()
                    },
                    evaluation_protocol=smoke_protocol,
                )
            else:
                scope.update(
                    source_ids=[source["id"]], intended_use="Smoke workload provenance only"
                )
            proposal = control.http(
                step,
                "POST",
                "/interaction/decisions",
                dict(
                    project_id=project_id,
                    kind="access" if action == "source_use" else "direction",
                    scope=scope,
                    expires_at=expires_at,
                    reviewer_principal_id=owner["id"],
                ),
            )
            return control.http(
                step + "-resolve",
                "POST",
                "/interaction/decisions/resolve",
                dict(
                    decision_id=proposal["id"],
                    expected_revision=proposal["revision"],
                    decision="approve",
                    rationale="Owner review of the bounded smoke route; no scientific claim",
                ),
            )

        program = decide("program-review", "plan")
        source_review = decide("source-review", "source_use")

        execution = _execution_profile(args, capability_digest, operation_id, str(epoch))
        selection = _selection(
            args,
            execution,
            operation_id=operation_id,
            snapshot_digest=package["snapshot_digest"],
            derived=derived,
        )
        policy = ProxyPolicy(
            org_id=owner["org_id"],
            project_id=project_id,
            target=TargetPolicy(selection=selection),
            management=ManagementCredential(
                reference=args.credential_reference, version=args.credential_version
            ),
        )
        body = dict(
            operation_id=operation_id,
            package_digest=response["package_digest"],
            attempt_id=response["admitted_attempt_id"],
            plan_digest=package["plan_digest"],
            lease_id=package["lease_id"],
            epoch=epoch,
            selection=selection.model_dump(mode="json"),
            study_id=study_id,
            program_review_id=program["id"],
            source_review_id=source_review["id"],
        )
        # Trusted-server route review: operator-owned allowlist, never request-supplied.
        routes = RemoteExecutionStore(args.state_dir, policies=(policy.target,))
        route = control.store(
            "route-propose",
            f"/tasks/{task_id}/remote-executions",
            canonical_digest(body),
            lambda key: routes.propose(owner, task_id, key, body),
        )
        route_id = _identifier(route["id"])
        for choice in ("approve", "start"):
            command = dict(
                expected_revision=route["revision"],
                command=choice,
                proposal_digest=route["proposal_digest"],
            )
            route = control.store(
                "route-" + choice,
                f"/remote-executions/{route_id}/commands",
                canonical_digest(command),
                lambda key, command=command: routes.command(owner, route_id, key, command),
            )
        current = control.http("task-current", "GET", f"/tasks/{task_id}")
        capability = control.http(
            "capability-request",
            "POST",
            f"/tasks/{task_id}/capabilities",
            dict(
                expected_plan_revision=current["revision"] - 1,
                expected_plan_digest=body["plan_digest"],
                declaration=declaration,
            ),
        )
        control.http(
            "capability-approve",
            "POST",
            f"/capabilities/{_identifier(capability['id'])}/approve",
            dict(
                expected_revision=1,
                declaration_digest=capability["declaration_digest"],
                expires_at=expires_at,
            ),
        )

        policy_json = policy.model_dump(mode="json")
        policy_json["management"] = policy.management.model_dump(mode="json")
        config = dict(
            schema="tessera.salad.live-config.v1",
            state_dir=str(args.state_dir),
            route_id=route_id,
            task_id=route["task_id"],
            org_id=route["org_id"],
            project_id=route["project_id"],
            proposal_digest=route["proposal_digest"],
            policy=policy_json,
            worker_credential_file=str(worker_credential_file),
            credential_directory=str(args.credential_directory),
            guardian_socket=str(args.guardian_socket),
            guardian_uid=args.guardian_uid,
            custody=dict(
                host=args.custody_admin_host,
                port=443,
                token_file=str(args.custody_token_file),
                ca_file=str(args.custody_ca_file),
                certificate_sha256=args.custody_cert_sha256,
            ),
            approved_max_cost_cents=args.max_cost_cents,
            max_seconds=args.max_seconds,
            max_polls=args.max_polls,
            source_revision=args.source_commit + "+sha256:" + inventory,
        )
        journal.write(CONFIG_NAME, json.dumps(config, sort_keys=True, indent=2).encode())
        _config(out / CONFIG_NAME)  # exact loader the live check applies

        summary = dict(
            schema="tessera.salad.live-setup.v1",
            status="route_approved_not_dispatched",
            config_file=str(out / CONFIG_NAME),
            worker_credential_file=str(worker_credential_file),
            journal=str(out / "journal"),
            records=dict(
                project_id=project_id,
                worker_principal_id=worker_principal,
                worker_registration_id=registration.get("id"),
                executor=None if executor is None else executor.public(),
                snapshot_id=snapshot_id,
                task_id=task_id,
                grant_id=grant_id,
                experiment_id=experiment_id,
                budget_ceiling_cents=budget.get("ceiling_cents"),
                admitted_attempt_id=response["admitted_attempt_id"],
                lease_id=lease_id,
                operation_id=operation_id,
                epoch=epoch,
                study_id=study_id,
                split_lock_id=split_lock["id"],
                program_review_id=program["id"],
                source_review_id=source_review["id"],
                route_id=route_id,
                proposal_digest=route["proposal_digest"],
                capability_id=capability["id"],
                salad_group=selection.salad.group,
            ),
            expiries=dict(
                authority_expires_at=expires_at,
                lease_expires_at=lease["expires_at"],
                lease_hard_expires_at=lease["hard_expires_at"],
            ),
            review_required=[
                dict(
                    field="selection.target_digest",
                    value=derived["target_digest"],
                    basis="canonical digest of target/org/project/gpu/country/image "
                    "(fixture used f*64); opaque dispatcher key",
                ),
                dict(
                    field="selection.manifests.code",
                    value=derived["code_digest"],
                    basis=(
                        "sha256 of workloads/salad_gpu_smoke/smoke.py in this checkout; "
                        "NOT proven to equal the code inside --image"
                        if args.workload is None
                        else "canonical digest of the reviewed workload contract; "
                        "NOT proven to equal the code inside --image"
                    ),
                ),
                dict(
                    field="selection.manifests.model",
                    value=derived["model_digest"],
                    basis="placeholder digest of an explicit no-model marker",
                ),
                dict(
                    field="selection.manifests.environment",
                    value=derived["environment_digest"],
                    basis="digest of platform/env/image; placeholder identity",
                ),
                dict(
                    field="selection.manifests.data",
                    value=snapshot_digest,
                    basis=(
                        "inventory digest of the generated public input marker snapshot"
                        if args.input_digest is None
                        else "chunked snapshot of --input-dir; equals the contract's "
                        "input.snapshot_digest; "
                        + _input_basis(args)
                        + "; released for worker transfer, pulled by the runtime from the "
                        "guardian after begin-once"
                    ),
                ),
                dict(
                    field="execution.watchdog_profile",
                    value=args.watchdog_profile,
                    basis="--watchdog-profile (default is the fixture placeholder b*64); "
                    "also argv[5] and must match the deployed watchdog policy",
                ),
                dict(
                    field="execution.monitor_profile",
                    value=args.monitor_profile,
                    basis="--monitor-profile (default is the fixture placeholder a*64)",
                ),
                dict(
                    field="execution.artifact_destination",
                    value=args.artifact_destination,
                    basis="fixture value unless overridden; custody binding artifact",
                ),
                dict(
                    field="execution.checkpoint_destination",
                    value=args.checkpoint_destination,
                    basis="fixture value unless overridden",
                ),
                dict(
                    field="execution.argv",
                    value=execution["argv"],
                    basis=(
                        "smoke_operation_v1: operation/epoch from the admitted package, "
                        "1024 output bytes, 1 file, dimension 4 (fixture)"
                        if args.workload is None
                        else "workload_v1: reviewed --workload-contract argv template with "
                        "operation/epoch from the admitted package and the custody host"
                    ),
                ),
                dict(
                    field="execution.env",
                    value=execution["env"],
                    basis="fixture (no environment)"
                    if args.workload is None
                    else "non-secret env from --workload-contract",
                ),
                dict(
                    field="execution.max_output_bytes",
                    value=execution["max_output_bytes"],
                    basis="fixture" if args.workload is None else "sum of contract outputs",
                ),
                dict(
                    field="resources",
                    value=selection.resources.model_dump(mode="json"),
                    basis="--cpu-millicores/--memory-mb/--disk-mb; defaults are the fixture "
                    "values and are likely too small for a CUDA torch image",
                ),
                dict(
                    field="capability.declaration",
                    value=declaration,
                    basis="environment_id from --capability-environment-id; image digest "
                    "from --image; "
                    + (
                        "no network/actions/credentials"
                        if args.workload is None
                        else capability_network_basis(args.workload)
                    ),
                ),
                dict(
                    field="capability_digest",
                    value=capability_digest,
                    basis="canonical digest of the declaration",
                ),
                dict(
                    field="worker.approved_build_digest",
                    value=inventory if executor is None else executor.approved_build_digest,
                    basis=(
                        "observed source_digest() of installed tessera packages"
                        if executor is None
                        else "recorded at executor enrollment (--executor-dir)"
                        + (
                            ""
                            if executor.approved_build_digest == inventory
                            else "; DIFFERS from the installed source_digest() "
                            + inventory
                            + ": the code changed since enrollment, rotate the executor "
                            "if the registration should describe this build"
                        )
                    ),
                ),
                dict(
                    field="worker registration labels",
                    value=dict(
                        identifier="tessera-trusted-salad-server-v1",
                        trust_class="trusted-server",
                        region="salad-" + args.country.lower(),
                    ),
                    basis="descriptive metadata chosen by this script",
                ),
                dict(
                    field="budget.evidence_digest/source_reference",
                    value=dict(source_reference=args.budget_source_reference),
                    basis="digest of the approved cap/image/gpu/org/project; reference "
                    "from --budget-source-reference",
                ),
                dict(
                    field="program plan outcome, conditions and version-2 protocol",
                    value=dict(
                        outcome=SMOKE_OUTCOME,
                        conditions=SMOKE_CONDITIONS,
                        gate="reference_match eq 1.0 (mean, >=2 replicates) on the smoke lock",
                    ),
                    basis="script-declared smoke data; one attempt leaves the gate inconclusive",
                ),
                dict(
                    field="experiment variants",
                    value="baseline admitted; intervention declared but never admitted",
                    basis="design requires both kinds",
                ),
                dict(
                    field="source_revision",
                    value=config["source_revision"],
                    basis="--source-commit (supplied) + observed package inventory",
                ),
            ],
            warnings=[
                "The execution lease lasts 30 s (LEASE_SECONDS) and the live session does "
                "not renew it; route start and the guardian deadline are bounded by "
                "lease_expires_at above.",
                "No Salad, guardian, custody or network call was made; nothing was reserved "
                "or dispatched. Approval records are not evidence of execution.",
            ],
        )
        journal.write(SUMMARY_NAME, json.dumps(summary, sort_keys=True, indent=2).encode())
        return summary
    finally:
        journal.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--owner-header", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--salad-org", required=True)
    parser.add_argument("--salad-project", required=True)
    parser.add_argument("--gpu-class-id", required=True)
    parser.add_argument("--country", required=True)
    parser.add_argument("--custody-dns", required=True)
    parser.add_argument("--custody-admin-host", required=True)
    parser.add_argument("--custody-ca-file", type=Path, required=True)
    parser.add_argument("--custody-cert-sha256", required=True)
    parser.add_argument("--custody-token-file", type=Path, required=True)
    parser.add_argument("--credential-directory", type=Path, required=True)
    parser.add_argument("--credential-reference", default="credential-ref:salad-management")
    parser.add_argument("--credential-version", type=int, required=True)
    parser.add_argument("--guardian-socket", type=Path, required=True)
    parser.add_argument("--guardian-uid", type=int, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--max-cost-cents", type=int, default=500)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--max-polls", type=int, default=30)
    parser.add_argument("--workload-seconds", type=int, default=None)
    parser.add_argument("--workload-contract", type=Path, default=None)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=None,
        help="owner-only directory staged as the contract's declared input snapshot",
    )
    parser.add_argument(
        "--data-class",
        choices=("public", "private_derived"),
        default=None,
        help="required with --input-dir and must be public: Salad admits only public data",
    )
    parser.add_argument("--license", default=None, help="license identifier, e.g. CC-BY-4.0")
    parser.add_argument("--source", default=None, help="source URL or reference of the data")
    parser.add_argument(
        "--approve-private-code",
        action="append",
        default=None,
        metavar="PATH",
        help="repeatable: an exact relative path in --input-dir approved as private code or "
        "config for this remote run; it stays private and is never declared public",
    )
    parser.add_argument(
        "--confirm-structured-config",
        action="append",
        default=None,
        metavar="PATH",
        help="repeatable: an approved .json/.yaml/.yml/.toml/.cfg/.ini path that you confirm "
        "is configuration, not data (required for each such file; at most 64 KiB)",
    )
    parser.add_argument("--authority-minutes", type=int, default=60)
    parser.add_argument("--project-name", default="Salad GPU smoke")
    parser.add_argument(
        "--executor-dir",
        type=Path,
        default=None,
        help="reuse a stable owner-enrolled executor (scripts/executor_enrollment.py) instead "
        "of minting a worker principal and project for this route",
    )
    parser.add_argument("--watchdog-profile", default="b" * 64)
    parser.add_argument("--monitor-profile", default="a" * 64)
    parser.add_argument("--artifact-destination", default="artifact-ref:quarantine")
    parser.add_argument("--checkpoint-destination", default="checkpoint-ref:external")
    parser.add_argument("--capability-environment-id", default="salad-gpu-smoke")
    parser.add_argument("--budget-source-reference", default="owner-approved-salad-gpu-smoke")
    parser.add_argument("--cpu-millicores", type=int, default=1000)
    parser.add_argument("--memory-mb", type=int, default=1024)
    parser.add_argument("--disk-mb", type=int, default=1024)
    args = parser.parse_args(argv)
    try:
        summary = setup(args)
    except SetupError as error:
        print(f"Setup stopped: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(
            f"Setup stopped ({type(error).__name__}); retain any output directory and reconcile "
            "from its journal. No identity renewal is automatic.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
