"""Fake authorized V2 runtime, real journal/custody; no installed runtime proof."""

import copy
import json

import pytest
from test_maintenance_hold import capture, setup

from tessera_operator.bundle_export import BundleExport
from tessera_operator.maintenance_completion import LIMIT, MaintenanceCompletion
from tessera_operator.maintenance_composition import OwnerMaintenance
from tessera_operator.maintenance_entrypoint import MaintenanceEntrypoint
from tessera_operator.schema_supervised import SupervisedDenied, digest


class Runtime:
    runtime_id = "pinned-runtime"

    def get(self, path, *, max_bytes):
        assert path == "/api/session/ses_original/message/msg_original"
        assert max_bytes == LIMIT
        self.reads += 1
        return json.dumps(self.response).encode()


def completed(tmp_path, monkeypatch, *, ack_error=False):
    hold, api, _, enforcement, handoff, plan, pins = setup(tmp_path, monkeypatch)
    runtime = Runtime()
    runtime.reads = 0
    binding = dict(
        runtime_id=runtime.runtime_id,
        session_id="ses_original",
        message_id="msg_original",
        call_id="call_original",
        tool="maintenance_release",
        build="reviewed-build",
    )
    capture(hold, plan, enforcement)
    owner = OwnerMaintenance(None, hold, handoff, structured_api=None)
    exports = {
        phase: BundleExport(value["journal"], value["destination"], clock=hold.clock)
        for phase, value in handoff.fixture_exports.items()
    }
    owner.reconcile_custody(plan, exports)
    handoff.apply(plan, pins, handoff.fixture_exports)
    # Fixture's private quota directory already owns the lower journal at
    # `root/journal`; use that existing owner directory as the operation root.
    # No journal relocation or identity/provenance rewriting is permitted.
    root = hold.directory.parent
    root.chmod(0o700)
    service = MaintenanceEntrypoint(clock=hold.clock)
    # Synthetic owner-input loading only. Actual entrypoint prepare/effect,
    # upper/lower claim writers, custody and release journal execute locally.
    monkeypatch.setattr(
        service,
        "load",
        lambda operation, action: (
            root,
            dict(plan_sha256=digest(plan), deadline=plan["deadline"]),
            dict(sha256="synthetic-owner-input"),
            {},
            plan,
            owner,
        ),
    )
    monkeypatch.setattr(service, "exports", lambda root: exports)
    prepared = service.prepare(plan["operation"], "release")
    before = (root / "release.prepared.json").read_bytes()
    hold.completion = MaintenanceCompletion(runtime, binding, root)
    service.completion = hold.completion
    invocation = dict(
        operation=plan["operation"],
        action="release",
        expected_action_sha256=prepared["action_sha256"],
    )
    runtime.service, runtime.invocation, runtime.root = service, invocation, root
    if ack_error:
        import os
        import stat

        original_fsync = os.fsync

        def fsync(fd):
            if (
                stat.S_ISDIR(os.fstat(fd).st_mode)
                and (hold.directory / "release.ack.json").exists()
            ):
                raise OSError("disposable final directory fsync failure")
            return original_fsync(fd)

        monkeypatch.setattr(os, "fsync", fsync)
        with pytest.raises(OSError):
            service.effect(**invocation)
        runtime.response = {
            "data": {"id": binding["message_id"], "type": "assistant", "content": []}
        }
        return hold, api, runtime, plan
    result = service.effect(**invocation)
    assert (root / "release.prepared.json").read_bytes() == before
    assert (root / "release.claim.json").read_bytes() == before
    assert result["action_claim_sha256"] == prepared["action_sha256"]
    part = dict(
        type="tool",
        id=binding["call_id"],
        name=binding["tool"],
        time={"created": 1, "completed": 2},
        state=dict(
            status="completed",
            input=invocation,
            content=[dict(type="text", text=json.dumps(result))],
        ),
    )
    runtime.response = dict(
        data=dict(
            id=binding["message_id"],
            type="assistant",
            time={"created": 1},
            agent="test",
            model={"providerID": "test", "modelID": "test"},
            content=[part],
        )
    )
    return hold, api, runtime, plan


def test_visible_ack_after_real_fsync_failure_cannot_promote(tmp_path, monkeypatch):
    hold, api, runtime, plan = completed(tmp_path, monkeypatch, ack_error=True)
    assert hold.reconcile("release")["state"] == "observed_unconfirmed"
    with pytest.raises(SupervisedDenied):
        hold.released(plan)
    with pytest.raises(SupervisedDenied, match="claimed"):
        hold.apply("release", plan)
    assert api.calls == 2
    with pytest.raises(FileExistsError):
        runtime.service.effect(**runtime.invocation)
    assert (runtime.root / "release.failure.json").exists()


def test_original_completion_and_no_replay(tmp_path, monkeypatch):
    hold, api, runtime, plan = completed(tmp_path, monkeypatch)
    assert hold.reconcile("release")["state"] == "captured"
    hold.released(plan)
    assert runtime.reads == 2
    with pytest.raises(SupervisedDenied, match="claimed"):
        hold.apply("release", plan)
    with pytest.raises(SupervisedDenied, match="no further DB exec"):
        hold.held(plan)
    assert api.calls == 2
    with pytest.raises(FileExistsError):
        runtime.service.effect(**runtime.invocation)


@pytest.mark.parametrize(
    "fault",
    [
        "runtime",
        "message",
        "call",
        "tool",
        "build",
        "claim",
        "operation",
        "plan",
        "ack",
        "binding",
        "text",
        "error",
        "running",
        "provider",
        "truncated",
        "oversize",
        "missing",
        "duplicate",
        "quota",
        "expired",
        "input_operation",
        "input_action",
        "input_digest",
        "missing_operation",
        "missing_action",
        "missing_digest",
        "upper_claim",
        "lower_claim",
        "old_wrapper",
    ],
)
def test_mismatches_never_promote(tmp_path, monkeypatch, fault):
    hold, api, runtime, plan = completed(tmp_path, monkeypatch)
    message = runtime.response["data"]
    part = message["content"][0]
    state = part["state"]
    if fault == "runtime":
        runtime.runtime_id = "other"
    elif fault == "message":
        message["id"] = "msg_other"
    elif fault in {"call", "tool"}:
        part["id" if fault == "call" else "name"] = "other"
    elif fault == "build":
        output = json.loads(state["content"][0]["text"])
        output["binding"]["build"] = "other"
        state["content"][0]["text"] = json.dumps(output)
    elif fault.startswith(("input_", "missing_")):
        key = {"operation": "operation", "action": "action", "digest": "expected_action_sha256"}[
            fault.split("_")[1]
        ]
        if fault.startswith("missing_"):
            state["input"].pop(key)
        else:
            state["input"][key] = "wrong"
    elif fault == "old_wrapper":
        state["input"] = dict(phase="release", plan=plan, build="reviewed-build")
    elif fault == "upper_claim":
        path = runtime.root / "release.claim.json"
        value = json.loads(path.read_text())
        value["lower_sha256"] = "changed"
        path.write_text(json.dumps(value))
    elif fault == "lower_claim":
        path = hold.directory / "release.claim.json"
        value = json.loads(path.read_text())
        value["plan_sha256"] = "changed"
        path.write_text(json.dumps(value))
    elif fault in {"claim", "operation", "plan", "ack", "binding"}:
        output = json.loads(state["content"][0]["text"])
        output[
            {"claim": "claim_sha256", "plan": "plan_sha256", "ack": "ack_sha256"}.get(fault, fault)
        ] = "tampered"
        state["content"][0]["text"] = json.dumps(output)
    elif fault == "text":
        message["content"] = [dict(type="text", text=state["content"][0]["text"])]
    elif fault == "error":
        state["status"] = "error"
    elif fault == "running":
        state["status"] = "running"
    elif fault == "provider":
        part["executed"] = True
    elif fault == "truncated":
        state["metadata"] = {"truncated": True}
    elif fault == "oversize":
        runtime.response["padding"] = "x" * LIMIT
    elif fault == "missing":
        hold.completion = None
    elif fault == "duplicate":
        message["content"].append(copy.deepcopy(part))
    elif fault == "quota":
        api.quota["spec"] = {"hard": {"pods": "0"}}
    else:
        hold.clock = lambda: plan["deadline"] + 1
    try:
        result = hold.reconcile("release")
        assert result["state"] == "observed_unconfirmed"
        assert not result["completion_confirmed"]
    except SupervisedDenied:
        pass
    with pytest.raises(SupervisedDenied):
        hold.released(plan)
    assert api.calls == 2
