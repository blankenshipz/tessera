"""README: "a Salad stage's own outputs stay at 16 MiB" (and at most 16 declared files).

Every layer that bounds a Salad stage's outputs is exercised in-process at the edge:
exactly 16 MiB is accepted, one byte more (or a 17th file) is refused fail closed with a
visible reason. Layers: the campaign spec (Salad provider), the reviewed execution
profile, the guardian service and its custody store, and the shipped gpu_runtime_v1.
"""

import base64
import hashlib
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from pydantic import ValidationError
from salad_campaign_fixture import (
    SaladWorld,
    contract_data,
    salad_provider_reset,  # noqa: F401
    salad_stage,
)
from test_salad_output_custody import BINDING, Case, private_root, sha
from test_salad_workload_contract import execution

from tessera_control.campaign_models import CampaignSpec
from tessera_control.campaign_provider_salad import OUTPUT_LIMITS
from tessera_control.remote_execution_models import SaladExecution
from tessera_control.salad_custody import SaladCustody
from tessera_control.salad_workload_contract import (
    MAX_TOTAL_OUTPUT_BYTES,
    OUTPUT_CHUNK_BYTES,
    WorkloadContractV1,
)

CAP = 16 * 1024 * 1024
ROOT = Path(__file__).resolve().parent.parent


def _runtime():
    spec = importlib.util.spec_from_file_location(
        "gpu_runtime_v1_output_cap", ROOT / "workloads/gpu_runtime_v1/runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def outputs_totalling(total, files=2):
    """``files`` declared outputs whose caps sum to ``total`` (weights take the rest)."""
    small = [dict(name=f"m{i}.json", max_bytes=1024) for i in range(files - 1)]
    return [dict(name="weights.bin", max_bytes=total - 1024 * (files - 1)), *small]


def test_every_layer_uses_the_same_16_mib_and_16_file_bound():
    rt = _runtime()
    assert MAX_TOTAL_OUTPUT_BYTES == rt.MAX_TOTAL_OUTPUT_BYTES == CAP
    assert rt.MAX_OUTPUT_FILES == OUTPUT_LIMITS.max_files == 16
    assert OUTPUT_LIMITS.max_total_bytes == OUTPUT_LIMITS.max_file_bytes == CAP
    bounds = SaladExecution.model_fields["max_output_bytes"].metadata
    assert [getattr(m, "le", None) for m in bounds if hasattr(m, "le")] == [CAP]


# --- campaign spec: a Salad stage is refused at submission/approval -----------------------


def _salad_spec(world, outputs, max_output_bytes):
    work = contract_data(outputs=outputs)
    stage = salad_stage(
        world.env,
        output_paths=[o["name"] for o in outputs],
        max_output_bytes=max_output_bytes,
    )
    stage["salad"] = dict(stage["salad"], workload=work)
    return dict(world.spec, stages=[stage])


def test_campaign_salad_stage_at_16_mib_is_accepted_and_one_byte_more_is_refused(tmp_path):
    world = SaladWorld(tmp_path)
    exact = CampaignSpec.model_validate(_salad_spec(world, outputs_totalling(CAP), CAP))
    assert exact.stages[0].salad.workload.output_bytes == CAP
    # One byte more on the stage's own bound: refused against the provider's 16 MiB.
    with pytest.raises(ValidationError, match="outputs exceed|max_output_bytes"):
        CampaignSpec.model_validate(_salad_spec(world, outputs_totalling(CAP + 1), CAP + 1))
    # The stage bound kept at 16 MiB but the reviewed contract declaring one byte more.
    with pytest.raises(ValidationError, match="max_output_bytes is below|custody total"):
        CampaignSpec.model_validate(_salad_spec(world, outputs_totalling(CAP + 1), CAP))
    # A 17th declared file is refused even well under the byte total.
    many = [dict(name=f"f{i}.bin", max_bytes=1) for i in range(17)]
    with pytest.raises(ValidationError):
        CampaignSpec.model_validate(_salad_spec(world, many, CAP))


# --- reviewed execution profile -------------------------------------------------------------


def test_execution_profile_refuses_outputs_over_16_mib():
    exact = SaladExecution.model_validate(execution(contract_data(outputs=outputs_totalling(CAP))))
    assert exact.max_output_bytes == exact.workload.output_bytes == CAP
    over = contract_data(outputs=outputs_totalling(CAP + 1))
    assert not WorkloadContractV1.model_validate(over).outputs_supported()
    with pytest.raises(ValidationError, match="less than or equal to 16777216"):
        SaladExecution.model_validate(execution(over))
    # Claiming only 16 MiB for a contract that declares more is refused too.
    with pytest.raises(ValidationError, match="output limits differ"):
        SaladExecution.model_validate(execution(over, max_output_bytes=CAP))


# --- guardian service and custody -------------------------------------------------------------


def test_guardian_retains_exactly_16_mib_and_refuses_any_byte_beyond(tmp_path):
    weights_cap = CAP - 1024
    case = Case(tmp_path, outputs=[dict(name="weights.bin", max_bytes=weights_cap),
                                   dict(name="output.json", max_bytes=1024)])  # fmt: skip
    try:
        assert case.request["max_output_bytes"] == CAP
        assert case.begin()["granted"]
        plan = case.service.workload("/v1/workload/outputs", dict(binding=case.request), case.token)
        assert sum(o["max_bytes"] for o in plan["outputs"]) == CAP
        # A file one byte over its declared share never reserves or stores a byte.
        with pytest.raises(ValueError, match="oversize"):
            case.chunk("weights.bin", b"w" * (weights_cap + 1), 0)
        with pytest.raises(ValueError, match="oversize"):
            case.chunk("output.json", b"j" * 1025, 0)
        weights = bytes(range(256)) * (weights_cap // 256)
        assert len(weights) == weights_cap
        reply = case.upload("weights.bin", weights)
        assert reply["file_complete"] and not reply["outputs_complete"]
        assert case.upload("output.json", b"j" * 1024)["outputs_complete"]
        stored = case.service.custody.db.execute("SELECT SUM(size), COUNT(*) FROM files").fetchone()
        assert tuple(stored) == (CAP, 2)
    finally:
        case.service.close()


def test_custody_store_refuses_an_inventory_over_16_mib(tmp_path):
    custody = SaladCustody(private_root(tmp_path) / "c.db")
    with pytest.raises(ValueError, match="invalid_byte_cap"):
        custody.register(BINDING, byte_cap=CAP + 1, file_cap=1)
    with pytest.raises(ValueError, match="invalid_declared_outputs"):
        custody.register(
            BINDING, byte_cap=CAP, file_cap=1, expected_files=1,
            outputs=[dict(name="weights.bin", max_bytes=CAP + 1)],
        )  # fmt: skip
    with pytest.raises(ValueError, match="invalid_declared_outputs"):
        custody.register(
            BINDING, byte_cap=CAP, file_cap=17, expected_files=17,
            outputs=[dict(name=f"f{i}.bin", max_bytes=1) for i in range(17)],
        )  # fmt: skip
    custody.register(
        BINDING, byte_cap=CAP, file_cap=1, expected_files=1,
        outputs=[dict(name="weights.bin", max_bytes=CAP)],
    )  # fmt: skip
    # The size declared on the first chunk is bound by the per-file cap: CAP + 1 never lands.
    part = b"x" * OUTPUT_CHUNK_BYTES
    with pytest.raises(ValueError, match="oversize"):
        custody.put_chunk(
            BINDING, name="weights.bin", checksum="a" * 64, size=CAP + 1, seq=0,
            chunk_sha256=sha(part), data=part, expires=1e12,
        )  # fmt: skip
    assert custody.db.execute("SELECT COUNT(*) FROM parts").fetchone()[0] == 0


# --- shipped gpu_runtime_v1 ---------------------------------------------------------------------


def test_runtime_refuses_more_than_16_mib_or_16_files_in_its_argv_and_plan():
    rt = _runtime()
    raw = b'{"fetches":[],"schema":"' + rt.MANIFEST_SCHEMA.encode() + b'"}'
    ref = "b64:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def argv(out_bytes, out_files):
        return ["v1", "a" * 32, "7", "custody.example.com", "b" * 64, "120",
                str(out_bytes), str(out_files), hashlib.sha256(raw).hexdigest(), ref]  # fmt: skip

    request = rt.parse_argv(argv(CAP, 16))
    assert (request["max_output_bytes"], request["max_output_files"]) == (CAP, 16)
    for out_bytes, out_files in ((CAP + 1, 1), (CAP, 17)):
        with pytest.raises(rt.Failure):
            rt.parse_argv(argv(out_bytes, out_files))
    # The guardian's plan must agree with the reviewed literals and the 16 MiB per-output cap.
    one = rt.parse_argv(argv(CAP, 1))
    plan = dict(chunk_bytes=rt.OUTPUT_CHUNK_BYTES, outputs=[dict(name="w.bin", max_bytes=CAP)])
    assert rt.validate_plan(plan, one) == [("w.bin", CAP)]
    over = dict(plan, outputs=[dict(name="w.bin", max_bytes=CAP + 1)])
    with pytest.raises(rt.Failure):
        rt.validate_plan(over, one)
