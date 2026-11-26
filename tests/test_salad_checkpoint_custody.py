# ruff: noqa: F811
"""The reserved checkpoint output is the one custody file a newer upload supersedes; every
other output stays write-once. Fake provider and synthetic JWT only."""

import hashlib

import pytest
from test_salad_output_custody import C, Case
from test_salad_workload_contract import contract
from test_salad_workload_contract import execution as _execution  # noqa: F401

from tessera_control.salad_custody import CustodyBinding, SaladCustody
from tessera_control.salad_workload_contract import (
    CHECKPOINT_OUTPUT_NAME,
    DIAGNOSTIC_OUTPUT_NAME,
    MAX_CHECKPOINT_UPLOADS,
    WorkloadContractV1,
    output_call_budget,
)

OUTPUTS = [
    dict(name="output.json", max_bytes=4096),
    dict(name=CHECKPOINT_OUTPUT_NAME, max_bytes=2 * C),
    dict(name=DIAGNOSTIC_OUTPUT_NAME, max_bytes=2048),
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def case(tmp_path):
    value = Case(tmp_path, outputs=OUTPUTS)
    yield value
    value.service.close()


def test_a_newer_checkpoint_supersedes_the_previous_one_while_the_run_goes_on(case):
    case.begin()
    first = case.upload(CHECKPOINT_OUTPUT_NAME, b"a" * (C + 5))
    assert first["file_complete"] and not first["outputs_complete"]
    second = case.upload(CHECKPOINT_OUTPUT_NAME, b"b" * 10)
    assert second["file_complete"] and not second["outputs_complete"]
    third = case.upload(CHECKPOINT_OUTPUT_NAME, b"c" * (2 * C))
    assert third["file_complete"] and not third["outputs_complete"]
    case.upload("output.json", b'{"ok":1}')
    last = case.upload(DIAGNOSTIC_OUTPUT_NAME, b"{}\n")
    assert last["outputs_complete"]  # only the diagnostic's upload completes the run
    case.stop()
    manifest = case.guardian()["manifest"]
    files = {f["name"]: f for f in manifest["inventories"][0]["files"]}
    assert files[CHECKPOINT_OUTPUT_NAME]["sha256"] == sha(b"c" * (2 * C))
    assert files[CHECKPOINT_OUTPUT_NAME]["bytes"] == 2 * C


def test_a_replayed_identical_checkpoint_is_idempotent_not_a_supersede(case):
    case.begin()
    case.upload(CHECKPOINT_OUTPUT_NAME, b"same")
    again = case.upload(CHECKPOINT_OUTPUT_NAME, b"same")
    assert again["file_complete"]
    rows = case.service.custody.db.execute(
        "SELECT COUNT(*) FROM files WHERE name=?", (CHECKPOINT_OUTPUT_NAME,)
    ).fetchone()[0]
    assert rows == 1


def test_every_other_output_stays_write_once(case):
    case.begin()
    case.upload("output.json", b'{"v":1}')
    with pytest.raises(ValueError):
        case.upload("output.json", b'{"v":2}')


def test_supersedes_are_bounded_by_the_call_budget(case):
    case.begin()
    cap = 2 * C
    allowed = output_call_budget({o["name"]: o["max_bytes"] for o in OUTPUTS})
    assert allowed == 2 * (1 + 2 + 1) + MAX_CHECKPOINT_UPLOADS * 2
    refused = False
    for n in range(allowed + 40):
        try:
            case.upload(CHECKPOINT_OUTPUT_NAME, bytes([n % 250]) * (cap // 4 + n))
        except ValueError:
            refused = True
            break
    assert refused


def test_a_checkpoint_over_its_declared_cap_is_refused(case):
    case.begin()
    with pytest.raises(ValueError):
        case.upload(CHECKPOINT_OUTPUT_NAME, b"z" * (2 * C + 1))


def test_the_budget_counts_checkpoint_supersedes_only_when_declared():
    plain = {"output.json": 10, "w.bin": C}
    assert output_call_budget(plain) == 2 * 2
    with_ckpt = dict(plain, **{CHECKPOINT_OUTPUT_NAME: C + 1})
    assert output_call_budget(with_ckpt) == 2 * 4 + MAX_CHECKPOINT_UPLOADS * 2


def test_superseding_in_the_store_replaces_parts_and_file_row(tmp_path):
    tmp_path.chmod(0o700)
    store = SaladCustody(tmp_path / "c.db")
    binding = CustodyBinding("t", "p", "op", 1, "artifact-ref:output")
    store.register(
        binding, byte_cap=2 * C + 16, file_cap=2, expected_files=2,
        outputs=[
            dict(name=CHECKPOINT_OUTPUT_NAME, max_bytes=2 * C),
            dict(name="d.log", max_bytes=16),
        ],
    )  # fmt: skip

    def put(name, data):
        return store.put_chunk(
            binding, name=name, checksum=sha(data), size=len(data), seq=0,
            chunk_sha256=sha(data), data=data, expires=1e12,
        )  # fmt: skip

    put(CHECKPOINT_OUTPUT_NAME, b"one")
    put(CHECKPOINT_OUTPUT_NAME, b"two!")
    parts = store.db.execute("SELECT data FROM parts WHERE name=?", (CHECKPOINT_OUTPUT_NAME,))
    assert [bytes(r[0]) for r in parts] == [b"two!"]
    assert put("d.log", b"x")["outputs_complete"]
    manifest = store.seal_operation(binding)
    sizes = {f["name"]: f["bytes"] for f in manifest["inventories"][0]["files"]}
    assert sizes == {CHECKPOINT_OUTPUT_NAME: 4, "d.log": 1}


# ------------------------------------------------------------------- contract rules


def with_outputs(*names):
    value = contract()
    value["outputs"] = [dict(name=n, max_bytes=4096) for n in names]
    return value


def test_a_declared_checkpoint_requires_the_diagnostic_output():
    with pytest.raises(ValueError, match="requires the diagnostic"):
        WorkloadContractV1.model_validate(with_outputs("output.json", CHECKPOINT_OUTPUT_NAME))
    WorkloadContractV1.model_validate(
        with_outputs("output.json", CHECKPOINT_OUTPUT_NAME, DIAGNOSTIC_OUTPUT_NAME)
    )


def test_the_diagnostic_output_needs_room_for_its_header_and_a_tail():
    bad = with_outputs("output.json", DIAGNOSTIC_OUTPUT_NAME)
    bad["outputs"][1]["max_bytes"] = 100
    with pytest.raises(ValueError, match="1 KiB"):
        WorkloadContractV1.model_validate(bad)
    WorkloadContractV1.model_validate(with_outputs("output.json", DIAGNOSTIC_OUTPUT_NAME))


def test_contracts_without_the_reserved_names_are_unchanged():
    model = WorkloadContractV1.model_validate(contract())
    assert [o.name for o in model.outputs] == ["output.json"]


def _dir(root, name):
    path = root / name
    path.mkdir()
    return path


def test_a_failure_reported_before_the_last_file_keeps_the_cause_and_takes_that_file(tmp_path):
    from test_salad_output_custody import Case

    from tessera_control.salad_workload_contract import DIAGNOSTIC_OUTPUT_NAME as DIAG

    case = Case(_dir(tmp_path, "x"), outputs=[dict(name="output.json", max_bytes=4096),
                                          dict(name=DIAG, max_bytes=2048)])  # fmt: skip
    try:
        case.begin()
        case.upload("output.json", b'{"partial":true}')
        report = dict(binding=case.request, reason="entry", exit_code=3)
        assert case.service.workload("/v1/workload/failed", report, case.token)["recorded"]
        last = case.upload(DIAG, b"{}\ntraceback")  # lands before the next guardian poll
        assert last["outputs_complete"]
        status = case.guardian()
        assert status["stop_cause"] == "workload_failed:entry"
        assert status["reason"] == "workload_failed"
        # an identical replay of the report is still acknowledged
        assert case.service.workload("/v1/workload/failed", report, case.token)["recorded"]
        case.stop()  # the guardian then stops the group and seals custody
        sealed = case.guardian()
        assert sealed["status"] == "stopped" and sealed["stop_cause"] == "workload_failed:entry"
        names = {f["name"] for f in sealed["manifest"]["inventories"][0]["files"]}
        assert names == {"output.json", DIAG}
    finally:
        case.service.close()


def test_outputs_complete_before_a_report_leaves_the_report_refused(tmp_path):
    """Why the runtime must report first: after completion the cause is output_retained."""
    from test_salad_output_custody import Case

    case = Case(_dir(tmp_path, "y"), outputs=[dict(name="output.json", max_bytes=4096)])
    try:
        case.begin()
        case.upload("output.json", b"{}")
        report = dict(binding=case.request, reason="entry", exit_code=3)
        with pytest.raises(ValueError, match="rejected"):
            case.service.workload("/v1/workload/failed", report, case.token)
        assert case.guardian()["stop_cause"] != "workload_failed:entry"
    finally:
        case.service.close()
