"""ADR 0097: a Salad stage's public_fetch receipt and the runtime's ``fetched`` rows.

The in-container runtime (workloads/gpu_runtime_v1) writes ``fetched`` rows into the stage's
custody ``output.json``. Those rows are not platform-authored: the stage entry runs in the same
container, as the same uid, with the same network, and the guardian authenticates an output
upload only by the container's IMDS identity and the argv binding. Stage code can therefore
upload its own ``output.json`` (with any ``fetched`` rows) through the same channel. Control
does not read them, so every Salad file stays ``declared_only`` with null bytes; these tests
pin that honest behaviour, and the pin match (url, sha256, bytes within ``max_bytes``) that
any provider-reported row must pass.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from salad_campaign_fixture import salad_provider_reset  # noqa: F401

from tessera_control.campaign_models import NormalInput
from tessera_control.campaign_provider_salad import SaladRouteProvider, register_provider_instance
from tessera_control.campaign_receipts import _retained_fetched, public_fetch_entry

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "gpu_runtime_fetch_receipt_rows", ROOT / "workloads/gpu_runtime_v1/runtime.py"
)
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)

URL = "https://models.example.com/weights/w.bin"
SHA = "a" * 64
MAX = 1000


def stage():
    source = NormalInput("public_artifact", "weights", public_files=(("w", URL, SHA, MAX),))
    return SimpleNamespace(inputs=(source,))


def runtime_row(**over):
    """A row as the runtime's fetch_all produces it."""
    return {"url": URL, "sha256": SHA, "bytes": 640, "seconds": 0.5, "cache": "off", **over}


def events_db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE events (event_type TEXT, payload_json TEXT)")
    return connection


# -- control never reads the runtime's rows for Salad ----------------------------------------


def test_the_salad_provider_has_no_retained_fetched_reader():
    provider = register_provider_instance(None)
    assert isinstance(provider, SaladRouteProvider)
    assert getattr(provider, "retained_fetched", None) is None


def test_a_salad_attempt_receipt_is_declared_only_with_null_bytes():
    register_provider_instance(None)
    rows = _retained_fetched(events_db(), "salad", {"operation_id": "op1"}, stage())
    assert rows is None
    entry = public_fetch_entry("salad", stage(), rows)
    assert entry["provider"] == "salad" and entry["via_control"] is False
    assert entry["hosts"] == ["models.example.com"]
    assert entry["files"] == [
        {
            "name": "w",
            "source_host": "models.example.com",
            "sha256": SHA,
            "bytes": None,
            "seconds": None,
            "evidence": "declared_only",
        }
    ]
    assert URL not in json.dumps(entry)


def test_the_runtime_adds_its_rows_but_only_after_the_entry_wrote_output_json(tmp_path):
    """The runtime refuses an entry-written ``fetched`` key and adds its own; that guards the
    file it stages, not the custody channel the entry can also reach."""
    out = tmp_path / "out"
    out.mkdir()
    (out / "output.json").write_text(json.dumps({"loss": 1.0}))
    staged = json.loads(rt.read_output(str(out), [runtime_row()], 4096))
    assert staged == {"loss": 1.0, "fetched": [runtime_row()]}
    (out / "output.json").write_text(json.dumps({"fetched": [runtime_row(bytes=1)]}))
    with pytest.raises(rt.Failure):
        rt.read_output(str(out), [runtime_row()], 4096)


def test_the_entry_runs_with_the_runtimes_identity(tmp_path, monkeypatch):
    """Why the rows are not platform-authored: the entry is started with the runtime's own
    environment and no uid, group or namespace change, so it can reach IMDS and the guardian
    exactly as the runtime does. If this changes, revisit the evidence level (ADR 0097)."""
    monkeypatch.setenv("TESSERA_TEST_MARKER", "1")
    seen = {}

    class Proc:
        def wait(self, timeout):
            return 0

    def runner(argv, **kwargs):
        seen.update(kwargs)
        return Proc()

    rt.run_entry("entry.py", str(tmp_path), "site", "out", rt.time.monotonic() + 60, runner)
    assert seen["env"]["TESSERA_TEST_MARKER"] == "1"
    assert not {"user", "group", "extra_groups", "preexec_fn", "umask"} & set(seen)


# -- any provider-reported row must match the pin exactly ------------------------------------


@pytest.mark.parametrize("size", [0, 640, MAX])
def test_a_matching_row_within_max_bytes_is_provider_reported(size):
    (file,) = public_fetch_entry("salad", stage(), [runtime_row(bytes=size)])["files"]
    assert (file["bytes"], file["evidence"]) == (size, "provider_reported")
    assert file["cache"] == "off"


@pytest.mark.parametrize(
    "over",
    [
        {"bytes": MAX + 1},
        {"bytes": -1},
        {"bytes": True},
        {"bytes": "640"},
        {"bytes": None},
        {"sha256": "b" * 64},
        {"url": "https://models.example.com/weights/other.bin"},
        {"url": "https://mirror.example.net/weights/w.bin"},
    ],
)
def test_a_row_that_contradicts_the_pin_stays_declared_only(over):
    (file,) = public_fetch_entry("salad", stage(), [runtime_row(**over)])["files"]
    assert file["bytes"] is None and file["seconds"] is None
    assert file["evidence"] == "declared_only"
    assert "cache" not in file  # nothing asked, nothing trusted: the shape is unchanged


def test_the_receipt_carries_no_url_or_file_content():
    row = runtime_row(content="RAW-BYTES", path="/tessera/work/files/w.bin")
    text = json.dumps(public_fetch_entry("salad", stage(), [row]))
    assert URL not in text and "RAW-BYTES" not in text and "/tessera/work" not in text
