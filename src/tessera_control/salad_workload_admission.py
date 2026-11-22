"""Admission-time checks for a workload_v1 Salad route (ADR 0036 phase 2).

These run where a route is proposed, approved and admitted (create/start), never
in the model validators: a stored proposal must always parse so heartbeat, stop
and reconcile keep working even if a bound later tightens.

* Every executable Salad profile's resources must be a shape Salad's create accepts
  (``salad_resources.check_salad_resources``: whole vCPUs 1..16, memory 1024..61440
  MB, storage a whole GiB within 1..250 GiB, GPU class UUIDs) at proposal, approval
  and create; never at start of an existing group. A stored proposal outside these
  limits still parses; it is only refused here, as new work.
* The declared fetch bytes plus the declared input bytes must fit ``disk_mb``.
* A declared input must be a snapshot staged in the route's project whose
  manifest-and-index reply fits one guardian reply (INPUT_REPLY_BYTES) and whose
  total fits ``input.max_bytes``.
* The target's provider instance (provider id plus the operation's worker principal)
  admits the input only as the operator's trust setting allows (ADR 0040 section 4):
  data recorded ``public`` always, approved private code only with
  ``private_code_approved``, other data only with ``private_derived``. When admission
  relied on approved code and ``private_derived`` is not allowed, an input-sourced entry
  must be one of the approved paths.
* For a gpu_runtime_v1 manifest carried in the input (``input-manifest:``), the
  staged file at PATH must hash to the argv MANIFEST_SHA256 and its rows must
  equal ``egress.fetches`` (so they hash to the reviewed FETCHES_SHA256). A
  mismatch is refused before any begin-once can happen.
* A gpu_runtime_v1 manifest (inline, or carried in the input) must follow the
  strict new-route rules (``check_gpu_runtime_manifest_rules``: exact top-level
  keys, ``redirect_hosts`` equal to ``egress.redirect_hosts`` exactly when that is
  non-empty, exact item shapes, input paths, exactly one entry), which the model
  validator deliberately does not apply so stored proposals keep parsing.
* Every input-sourced manifest item (``{"kind", "source": "input", "path"}``,
  inline or carried in the input) must name a file of the staged snapshot, so a
  route whose entry is missing from its input is refused before any begin-once.
"""

import hashlib
import json

from .code_approval import check_entry_items_approved, snapshot_code_approval
from .data_class import (
    DataClassRefused,
    admit_provider_input,
    provider_for_target,
    snapshot_data_class,
)
from .provider_trust import provider_admits_class
from .salad_resources import check_salad_resources
from .salad_workload_contract import (
    GPU_RUNTIME_V1,
    INPUT_REPLY_BYTES,
    check_gpu_runtime_inline_rules,
    check_gpu_runtime_manifest,
    check_gpu_runtime_manifest_rules,
    check_input_items_staged,
    gpu_runtime_input_manifest_path,
)

INPUT_CHUNK_BYTES = 4 * 1024 * 1024  # equals tessera_worker.chunked_inputs.CHUNK_BYTES
MAX_RUNTIME_MANIFEST_BYTES = 262144


def _workload(selection):
    from .remote_execution_models import ExecutionSelection

    selection = ExecutionSelection.model_validate(selection)
    salad = selection.salad
    if salad is None or salad.execution is None or salad.execution.argv_binding != "workload_v1":
        return selection, None
    return selection, salad.execution.workload


def input_manifest_reply_bytes(snapshot_id, digest, manifest):
    """Encoded size of the guardian's manifest reply (manifest plus 4 MiB chunk index)."""
    index = []
    for entry in manifest["entries"]:
        for offset in range(0, entry["size"], INPUT_CHUNK_BYTES):
            index.append(
                dict(seq=len(index), relative_path=entry["relative_path"], offset=offset,
                     size=min(INPUT_CHUNK_BYTES, entry["size"] - offset), sha256="0" * 64)
            )  # fmt: skip
    reply = dict(
        schema="tessera.input-manifest.v1",
        snapshot_id=snapshot_id,
        digest=digest,
        manifest=manifest,
        delivery="chunked",
        chunks=index,
    )
    return len(json.dumps(reply, sort_keys=True, separators=(",", ":")).encode())


def _staged_file(connection, snapshot_id, entry, cap):
    if entry["size"] > cap:
        raise ValueError("staged runtime manifest exceeds its cap")
    rows = connection.execute(
        "SELECT content FROM snapshot_chunks WHERE owner_id=? AND relative_path=? ORDER BY offset",
        (snapshot_id, entry["relative_path"]),
    ).fetchall()
    if not rows:
        rows = connection.execute(
            "SELECT content FROM snapshot_files WHERE snapshot_id=? AND relative_path=?",
            (snapshot_id, entry["relative_path"]),
        ).fetchall()
    data = b"".join(bytes(r[0]) for r in rows)
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError("staged runtime manifest failed its integrity check")
    return data


def _staging_rank(connection, snapshot_id, manifest_json):
    """Admission preference among stagings of the same bytes: public, code-only, private."""
    approval = snapshot_code_approval(connection, snapshot_id)
    public = snapshot_data_class(connection, snapshot_id)["data_class"] == "public"
    if approval is None:
        return 0 if public else 2
    try:
        approved = {f["relative_path"] for f in approval.get("files", [])}
        code_only = all(
            e["relative_path"] in approved for e in json.loads(manifest_json)["entries"]
        )
    except (KeyError, TypeError, ValueError):
        return 2
    return 1 if code_only else 2


def check_workload_admission(
    connection, project_id, selection, worker_principal_id=None, *, create_shape=True
):
    """Raise ValueError when a workload_v1 route must not be proposed, approved or admitted.

    ``worker_principal_id`` is the principal holding the route's grant: with the target's
    provider id it names the instance the trust setting is checked for (ADR 0040 section
    4). Returns the admission evidence (provider id, trust policy digest, basis, input
    classes) when a staged input was admitted, else None.

    ``create_shape`` applies the Salad create-shape limits (proposal, approval and the
    create action). Starting an already created, stopped group passes False: those
    documentation-derived limits must not strand a group that exists.
    """
    selection, work = _workload(selection)
    if create_shape and selection.salad is not None and selection.salad.execution is not None:
        check_salad_resources(selection.resources)
    if work is None:
        return None
    declared = work.egress.declared_bytes() + (work.input.max_bytes if work.input else 0)
    if declared > selection.resources.disk_mb * 1024 * 1024:
        raise ValueError("declared fetch and input bytes exceed the route's disk_mb")
    # Strict manifest rules (shape, paths, exactly one entry) for new routes only;
    # the model validator stays lenient so stored proposals keep parsing.
    inline = check_gpu_runtime_inline_rules(work)
    if work.input is None:
        return None
    rows = connection.execute(
        "SELECT id, manifest_json FROM source_snapshots WHERE project_id=? AND digest=? "
        # ADR 0040 section 4 ranks the stagings in Python (public, code-only, private).
        "ORDER BY created_at, id LIMIT 100",
        (project_id, work.input.snapshot_digest),
    ).fetchall()
    if not rows:
        raise ValueError("the declared input snapshot is not staged in this project")
    # ADR 0040 section 4: the first staging of these bytes that the setting admits wins,
    # preferring public, then code-only, then private. Each candidate gets the full check.
    row, admitted, refusal = None, None, None
    for candidate in sorted(rows, key=lambda r: _staging_rank(connection, r[0], r[1])):
        try:
            admitted = admit_provider_input(
                connection, selection.target, candidate[0], worker_principal_id
            )
        except DataClassRefused as error:
            refusal = refusal or error
            continue
        row = candidate
        break
    if row is None:
        raise refusal
    approved_code = admitted.code_paths
    provider_id = provider_for_target(selection.target)
    evidence = {
        "provider_id": provider_id,
        "worker_principal_id": worker_principal_id,
        "trust_policy_digest": admitted.policy_digest,
        "admission_basis": admitted.basis,
        "input_data_class": admitted.input_data_class,
        "input_code": admitted.input_code,
    }
    if (
        admitted.basis != "public_only"
        and selection.salad.execution.data_policy != "provider_trust_policy_v1"
    ):
        # The route's literal must not claim public bytes for input that is not public.
        raise ValueError(
            "the route's data_policy must be provider_trust_policy_v1 for input that is not "
            "recorded public without approved code"
        )
    snapshot_id, manifest = row[0], json.loads(row[1])
    if manifest["total_bytes"] > work.input.max_bytes:
        raise ValueError("the staged input exceeds the contract's input.max_bytes")
    if input_manifest_reply_bytes(snapshot_id, work.input.snapshot_digest, manifest) > (
        INPUT_REPLY_BYTES
    ):
        raise ValueError("the staged input's manifest and chunk index exceed one guardian reply")
    if work.runtime != GPU_RUNTIME_V1:
        return evidence
    path = gpu_runtime_input_manifest_path(work)
    if path is None:
        runtime_manifest = inline
    else:
        entry = next((e for e in manifest["entries"] if e["relative_path"] == path), None)
        if entry is None:
            raise ValueError("the runtime manifest named by argv is not in the staged input")
        raw = _staged_file(connection, snapshot_id, entry, MAX_RUNTIME_MANIFEST_BYTES)
        runtime_manifest = check_gpu_runtime_manifest(work, raw, work.argv_template[8])
        check_gpu_runtime_manifest_rules(runtime_manifest, True, work.egress.redirect_hosts)
    if runtime_manifest is not None:
        check_input_items_staged(runtime_manifest, manifest["entries"])
        if approved_code and not provider_admits_class(
            provider_id, worker_principal_id, "private_derived", connection=connection
        ):
            # Amendment 1: when approved code carried the admission and the instance may
            # not receive private data, the input entry must be approved code. The route
            # path has no campaign context, so no approval-time digest or send_classes
            # narrow this check; the recorded current setting alone decides.
            check_entry_items_approved(runtime_manifest, approved_code)
    return evidence
