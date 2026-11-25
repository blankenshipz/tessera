"""Stage egress allowlist on the receipt (ADR 0134); fakes only, no network, no provider."""

# ruff: noqa: F811
from __future__ import annotations

from test_campaign_public_artifact_salad import (  # noqa: F401
    CDN,
    HOST,
    SHA,
    URL,
    receipt_stage,
    world,
)

from tessera_control.campaign_receipts import public_fetch_entry
from tessera_control.stage_egress_allowlist import refused_transfers, stage_allowlist

GOOD = {"url": URL, "sha256": SHA, "bytes": 900, "seconds": 1.5}


def test_allowlist_is_pinned_and_redirect_hosts_only(world):
    allowlist = stage_allowlist(receipt_stage(world))
    assert allowlist == [
        {"host": CDN, "port": 443, "source": "redirect"},
        {"host": HOST, "port": 443, "source": "pinned"},
    ]


def test_receipt_extends_the_deployed_allowlist_object_with_entries(world):
    """One shape: the ADR 0097 object keeps its fields and gains ``entries``; the verdict and
    refused transfers are siblings on the ``public_fetch`` entry, never inside the object."""
    other = {**GOOD, "url": "https://elsewhere.example.org/w.bin"}
    entry = public_fetch_entry("salad", receipt_stage(world), [GOOD, other])
    allowlist = entry["egress_allowlist"]
    assert set(allowlist) == {
        "hosts",
        "declared",
        "redirect_enforced",
        "network_enforced",
        "statement",
        "entries",
    }
    assert allowlist["hosts"] == entry["hosts"] == [CDN, HOST]
    assert allowlist["entries"] == stage_allowlist(receipt_stage(world))
    assert (allowlist["declared"], allowlist["network_enforced"]) == (True, False)
    assert entry["egress_verdict"] == "violation"
    assert entry["refused_transfers"] == [
        {"host": "elsewhere.example.org", "reason": "not_allowlisted"}
    ]
    assert not {"egress_verdict", "refused_transfers"} & set(allowlist)


def test_reported_transfer_inside_the_allowlist_is_verified(world):
    entry = public_fetch_entry("salad", receipt_stage(world), [GOOD])
    assert entry["egress_verdict"] == "within_allowlist"
    assert "refused_transfers" not in entry


def test_transfer_to_another_host_is_refused_and_recorded(world):
    other = {**GOOD, "url": "https://elsewhere.example.org/w.bin"}
    entry = public_fetch_entry("salad", receipt_stage(world), [GOOD, other])
    assert entry["egress_verdict"] == "violation"
    assert entry["refused_transfers"] == [
        {"host": "elsewhere.example.org", "reason": "not_allowlisted"}
    ]
    assert "elsewhere.example.org/w.bin" not in repr(entry)


def test_allowlisted_host_but_unpinned_file_is_refused():
    allowlist = [{"host": HOST, "port": 443, "source": "pinned"}]
    rows = [{"url": f"https://{HOST}/other.bin", "sha256": SHA}]
    assert refused_transfers(allowlist, {(URL, SHA)}, rows) == [
        {"host": HOST, "reason": "not_pinned"}
    ]
    assert refused_transfers(allowlist, {(URL, SHA)}, [{"sha256": SHA}]) == [
        {"host": None, "reason": "not_allowlisted"}
    ]


def _pinned_stage(url):
    class Source:
        kind = "public_artifact"
        redirect_hosts: list[str] = []
        public_files = [("f", url, SHA, 1000)]

    class Stage:
        inputs = (Source,)
        egress = None

    return Stage


def test_a_trailing_dot_spelling_of_a_pinned_host_is_not_a_violation():
    pinned = "https://data.example.org./f.bin"
    allowlist = stage_allowlist(_pinned_stage(pinned))
    assert allowlist == [{"host": "data.example.org", "port": 443, "source": "pinned"}]
    row = {"url": pinned, "sha256": SHA}
    assert refused_transfers(allowlist, {(pinned, SHA)}, [row]) == []


def test_a_unicode_spelling_of_a_pinned_host_is_not_a_violation():
    pinned = "https://bücher.example.org/f.bin"
    allowlist = stage_allowlist(_pinned_stage(pinned))
    assert allowlist == [{"host": "xn--bcher-kva.example.org", "port": 443, "source": "pinned"}]
    row = {"url": pinned, "sha256": SHA}
    assert refused_transfers(allowlist, {(pinned, SHA)}, [row]) == []
    # The same host reported in its ASCII form is the same host too.
    ascii_url = "https://xn--bcher-kva.example.org/f.bin"
    assert (
        refused_transfers(allowlist, {(ascii_url, SHA)}, [{"url": ascii_url, "sha256": SHA}]) == []
    )


def test_a_genuinely_other_host_is_still_refused_in_any_spelling():
    allowlist = stage_allowlist(_pinned_stage("https://data.example.org./f.bin"))
    for url in ("https://other.example.org./f.bin", "https://bücher.example.net/f.bin"):
        (found,) = refused_transfers(allowlist, {(url, SHA)}, [{"url": url, "sha256": SHA}])
        assert found["reason"] == "not_allowlisted"


def test_a_pin_matched_row_with_malformed_bytes_is_not_a_verified_transfer(world):
    stage = receipt_stage(world)
    bad = {**GOOD, "bytes": "900"}  # not an int: shown as declared_only
    entry = public_fetch_entry("salad", stage, [bad])
    assert [f["evidence"] for f in entry["files"]] == ["declared_only"]
    assert entry["egress_verdict"] == "declared_only"
    assert "refused_transfers" not in entry
    good = public_fetch_entry("salad", stage, [GOOD])
    assert [f["evidence"] for f in good["files"]] == ["provider_reported"]
    assert good["egress_verdict"] == "within_allowlist"


def test_registered_destinations_join_the_allowlist():
    class Dest:
        host, port = "registry.example.net", 8443

    class Stage:
        inputs = ()
        egress = type("P", (), {"destinations": [Dest]})

    assert stage_allowlist(Stage) == [
        {"host": "registry.example.net", "port": 8443, "source": "registered"}
    ]
