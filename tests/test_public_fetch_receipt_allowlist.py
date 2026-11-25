"""The ``public_fetch`` receipt says what the egress allowlist is (ADR 0097 amendment).

Fakes only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tessera_control import campaign_receipts as receipts
from tessera_control.campaign_models import NormalInput
from tessera_control.campaign_receipts import EGRESS_ALLOWLIST_STATEMENT, public_fetch_entry

SHA = "c" * 64
URL = "https://zeta.example.com/w.bin"


def stage(*hosts, pins=True):
    files = (("w", URL, SHA, 1000),) if pins else ()
    source = NormalInput(
        "public_artifact", "weights", public_files=files, redirect_hosts=tuple(hosts)
    )
    return SimpleNamespace(inputs=[source])


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    kinds = {"rented": "remote_job", "local": "worker_grant"}
    monkeypatch.setattr(
        receipts,
        "stage_provider",
        lambda pid: SimpleNamespace(descriptor=SimpleNamespace(kind=kinds[pid])),
    )


def test_provider_side_entry_declares_the_allowlist_without_network_enforcement():
    entry = public_fetch_entry("rented", stage("cdn.example.net", "alpha.example.org"))
    assert entry["hosts"] == ["alpha.example.org", "cdn.example.net", "zeta.example.com"]
    assert entry["egress_allowlist"] == {
        "hosts": entry["hosts"],
        "declared": True,
        "redirect_enforced": True,
        "network_enforced": False,
        "statement": EGRESS_ALLOWLIST_STATEMENT,
        # ADR 0134: the one added key, the per-host rows of the stage allowlist.
        "entries": [
            {"host": "alpha.example.org", "port": 443, "source": "redirect"},
            {"host": "cdn.example.net", "port": 443, "source": "redirect"},
            {"host": "zeta.example.com", "port": 443, "source": "pinned"},
        ],
    }
    assert entry["egress_verdict"] == "declared_only" and "refused_transfers" not in entry
    assert entry["egress_allowlist"]["network_enforced"] is False
    assert "does not restrict" in EGRESS_ALLOWLIST_STATEMENT
    assert entry["placement"] == "provider_side" and entry["via_control"] is False


def test_evidence_label_follows_reported_rows():
    item = stage()
    (declared,) = public_fetch_entry("rented", item)["files"]
    assert declared["evidence"] == "declared_only" and declared["bytes"] is None
    row = {"url": URL, "sha256": SHA, "bytes": 1000, "seconds": 2.0}
    (reported,) = public_fetch_entry("rented", item, [row])["files"]
    assert reported["evidence"] == "provider_reported" and reported["bytes"] == 1000


def test_stage_without_pins_has_no_entry():
    assert public_fetch_entry("rented", stage(pins=False)) is None


def test_local_worker_and_unknown_provider_entries_have_no_allowlist_object():
    row = {"url": URL, "sha256": SHA, "bytes": 1000, "seconds": 2.0}
    for provider in ("local", None):
        entry = public_fetch_entry(provider, stage())
        assert "egress_allowlist" not in entry
        # ADR 0134: the verdict is not gated like the allowlist object.
        assert entry["egress_verdict"] == "declared_only"
        reported = public_fetch_entry(provider, stage(), [row])
        assert "egress_allowlist" not in reported
        assert reported["egress_verdict"] == "within_allowlist"


def test_an_earlier_receipt_object_is_a_subset_of_the_merged_one():
    """Additive only (ADR 0134): every key and value main wrote before stays as it was."""
    entry = public_fetch_entry("rented", stage("cdn.example.net"))
    earlier = {
        "hosts": ["cdn.example.net", "zeta.example.com"],
        "declared": True,
        "redirect_enforced": True,
        "network_enforced": False,
        "statement": EGRESS_ALLOWLIST_STATEMENT,
    }
    merged = entry["egress_allowlist"]
    assert {key: merged[key] for key in earlier} == earlier
    assert set(merged) - set(earlier) == {"entries"}
