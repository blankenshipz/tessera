"""The fetch reach of a stage is exactly its pins (ADR 0097). Fakes only, no network.

Pins the cases the other public-artifact suites leave implicit: every spelling of a host is
either refused or collapses to one canonical name, a stage cannot add a host through another
field, a Salad contract can only admit the pinned hosts, and a stage with no pins has no fetch
reach. The last test records the documented limit: Tessera declares Salad's fetch reach but does
not enforce a network allowlist on the Salad container (ADR 0036, ADR 0097 consequences).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from salad_campaign_fixture import contract_data, profiles, salad_spec_v2, salad_stage_v2
from test_campaign_models import SNAP
from test_campaign_public_artifact_salad import (  # noqa: F401
    PUBLIC_ARTIFACT_MISMATCH,
    pin,
    public_input,
    salad_provider_reset,
    snapshot_input,
    stub_config,
    world,
)
from test_campaign_public_artifact_spec import _file, _normal, _public, _stage
from test_gpu_runtime_v1 import rt

from tessera_control import campaign_provider_salad as salad
from tessera_control.campaign_models import CampaignSpecV2, normalize, public_fetch_policy
from tessera_control.public_fetch_models import public_fetch_host
from tessera_control.salad_campaign_profiles import execution_profile
from tessera_control.salad_workload_contract import (
    WorkloadContractV1,
    capability_network_matches,
    env_refusal,
)

SHA = "c" * 64

# -- spec: redirect hosts must be written in canonical form --------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "CDN.example.org",  # case: never folded silently
        "cdn.example.org.",  # trailing dot
        "cdn.example.org:443",  # port
        "user@cdn.example.org",  # userinfo
        "https://cdn.example.org",  # scheme
        "*.example.org",  # wildcard
        "10.0.0.1",
        "0x7f000001",
        "2130706433",
        "127.1",
        "::1",
        "[::1]",
        "bücher.example.de",  # unicode: only the punycode spelling is accepted
        "localhost",
        "cdn",  # single label
        "cdn.internal",
        "",
    ],
)
def test_a_redirect_host_in_any_non_canonical_spelling_is_refused(host):
    with pytest.raises(ValidationError):
        _normal(_spec_with(_public(redirect_hosts=[host])))


def test_a_punycode_redirect_host_is_refused_by_the_spec_pattern():
    """Stricter than the shared host rule (which IDNA-encodes): the spec's host pattern allows
    no ``--``, so an ``xn--`` redirect host cannot be declared at all. Fails closed (narrower)."""
    with pytest.raises(ValidationError, match="pattern"):
        _policy(_public(redirect_hosts=["xn--bcher-kva.example.de"]))


# -- spec: pinned URLs ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://[::1]/a.bin",
        "https://[::ffff:127.0.0.1]/a.bin",
        "https://0x7f000001/a.bin",
        "https://2130706433/a.bin",
        "https://user@huggingface.co/a.bin",  # userinfo without a password
        "https://huggingface.co@evil.example.com/a.bin",  # the real host is the one after @
        "https://huggingface.co\\@evil.example.com/a.bin",
        "https://huggingface.co%2eevil.example.com/a.bin",
        "https://huggingface.co:80/a.bin",
    ],
)
def test_a_pinned_url_that_could_reach_another_host_is_refused(url):
    with pytest.raises(ValidationError):
        _normal(_spec_with(_public(files=[_file(url=url)])))


def test_spellings_of_one_pinned_host_collapse_to_one_canonical_host():
    files = [
        _file("a.bin", "https://huggingface.co./a.bin"),  # trailing dot
        _file("b.bin", "https://huggingface.co:443/b.bin"),  # explicit default port
        _file("c.bin", "https://huggingface.co/c.bin"),
        _file("d.bin", "https://bücher.example.de/d.bin"),  # unicode -> punycode
    ]
    assert _policy(_public(files=files))["hosts"] == ["huggingface.co", "xn--bcher-kva.example.de"]


def test_the_reach_is_the_pinned_and_redirect_hosts_and_nothing_from_other_inputs():
    stage = _normal(_spec_with(SNAP, _public(redirect_hosts=["cdn.example.org"]))).stage("a")
    policy = public_fetch_policy(stage)
    assert policy["hosts"] == ["cdn.example.org", "huggingface.co"]
    pinned = {public_fetch_host(f["url"]) for f in policy["files"]}
    assert set(policy["hosts"]) == pinned | {"cdn.example.org"}


def test_a_public_stage_cannot_add_its_own_egress():
    """``egress`` beside a pin is refused, whatever shape it takes (the parse or the structure
    check refuses it), so a stage cannot widen the derived allowlist that way."""
    egress = {
        "profile": "allowlist-proxy-v1",
        "destinations": [{"host": "evil.example.com", "port": 443}],
        "max_egress_bytes": 10,
    }
    with pytest.raises((ValidationError, ValueError), match="egress"):
        _normal(_spec_with(_public(), egress=egress))


# -- Salad: the contract can admit only the pinned hosts -------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://models.example.com./w.bin",  # trailing dot
        "https://Models.example.com/w.bin",
        "https://bücher.example.de/w.bin",
        "https://10.0.0.1/w.bin",
        "https://localhost/w.bin",
        "https://user@models.example.com/w.bin",
    ],
)
def test_the_contract_refuses_a_fetch_row_spelled_off_canonical(url):
    with pytest.raises(ValidationError):
        WorkloadContractV1.model_validate(_contract(url))


@pytest.mark.parametrize(
    "spec_url, contract_url",
    [
        ("https://models.example.com./w.bin", "https://models.example.com/w.bin"),
        ("https://models.example.com:443/w.bin", "https://models.example.com/w.bin"),
        ("https://models.example.com/w.bin", "https://models.example.com:443/w.bin"),
    ],
)
def test_the_same_host_spelled_differently_fails_closed_at_admission(spec_url, contract_url):
    item = SimpleNamespace(inputs=[public_input(pin(url=spec_url))])
    config = stub_config(fetches=(pin(url=contract_url),))
    assert salad.public_artifact_refusal(item, config) == PUBLIC_ARTIFACT_MISMATCH


def test_an_admitted_contract_reaches_exactly_the_pinned_hosts_on_443():
    rows = (
        pin(url="https://models.example.com/w.bin"),
        pin(url="https://data.example.org:443/d.bin", sha="d" * 64, name="d"),
    )
    contract = WorkloadContractV1.model_validate(_contract(*(url for _, url, _, _ in rows)))
    item = SimpleNamespace(inputs=[public_input(*rows)])
    config = SimpleNamespace(workload=contract)
    assert salad.public_artifact_refusal(item, config) is None
    network = contract.capability_network()
    assert all(entry["port"] == 443 for entry in network)
    pinned = {public_fetch_host(url) for _, url, _, _ in rows}
    assert {entry["host"] for entry in network} == pinned
    execution = {"argv_binding": "workload_v1", "workload": contract.model_dump(mode="json")}
    widened = [*network, {"host": "evil.example.com", "port": 443}]
    assert capability_network_matches(execution, {"network": network})
    assert not capability_network_matches(execution, {"network": widened})


def test_a_salad_stage_cannot_widen_through_egress_or_env(world):  # noqa: F811
    public = {
        "kind": "public_artifact",
        "name": "weights",
        "files": [
            {
                "name": "w",
                "url": "https://models.example.com/w.bin",
                "sha256": SHA,
                "max_bytes": 1000,
            }
        ],
    }
    for extra in (
        {
            "egress": {
                "profile": "allowlist-proxy-v1",
                "destinations": [{"host": "evil.example.com", "port": 443}],
                "max_egress_bytes": 10,
            }
        },
        {"env": {"HTTPS_PROXY": "http://evil.example.com:3128"}},
    ):
        base = salad_stage_v2(world.env)
        stage = salad_stage_v2(world.env, inputs=[*base["inputs"], public], **extra)
        with pytest.raises((ValidationError, ValueError)):
            normalize(CampaignSpecV2.model_validate(salad_spec_v2(world.env, [stage])))


def test_a_stage_without_pins_has_no_fetch_reach(world):  # noqa: F811
    private = SimpleNamespace(inputs=[snapshot_input(world, "private_derived")])
    assert salad.public_pins(private) is None
    assert public_fetch_policy(_normal(_spec_with(SNAP)).stage("a")) is None
    empty = WorkloadContractV1.model_validate(
        contract_data(egress={"kind": "provider_broad", "fetches": []})
    )
    assert empty.capability_network() == []
    execution = {"argv_binding": "workload_v1", "workload": empty.model_dump(mode="json")}
    assert not capability_network_matches(
        execution, {"network": [{"host": "models.example.com", "port": 443}]}
    )


def test_the_runtime_fetch_ignores_a_proxy_named_in_the_reviewed_env(monkeypatch):
    """A contract env may name a proxy variable (it is not a credential), but the runtime's
    fetch builds its opener with an empty proxy map, so the fetch never goes through it."""
    assert env_refusal("HTTPS_PROXY", "http://proxy.example.com:3128") is None
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:3128")
    seen: list = []

    class Opener:
        def open(self, request, timeout):
            return SimpleNamespace(status=200)

    def build(*handlers):
        seen.extend(handlers)
        return Opener()

    monkeypatch.setattr(rt.urllib.request, "build_opener", build)
    rt.open_url("https://models.example.com/w.bin", 5)
    proxies = [h for h in seen if isinstance(h, rt.urllib.request.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}


def test_salad_run_egress_is_declared_provider_wide_not_enforced(world):  # noqa: F811
    """Documented limit, pinned so a change to it is deliberate: Tessera binds what the fetch
    step may request, but the Salad container keeps provider-wide egress for the whole run
    (fetch and stage code alike); there is no Tessera network allowlist on Salad."""
    profile = profiles(world.env).profiles[0]
    contract = WorkloadContractV1.model_validate(contract_data())
    built = execution_profile(profile, contract, "a" * 64, "op", 1, 5)
    assert built["network_policy"] == "provider_broad_egress_explicitly_accepted"


# -- helpers ----------------------------------------------------------------------------------


def _spec_with(*inputs, **extra):
    from test_campaign_models_v2 import _spec2

    return _spec2(_stage(*inputs, **extra))


def _policy(*inputs):
    return public_fetch_policy(_normal(_spec_with(*inputs)).stage("a"))


def _contract(*urls):
    fetches = [
        {"url": url, "sha256": SHA if i == 0 else "d" * 64, "max_bytes": 1000}
        for i, url in enumerate(urls)
    ]
    return contract_data(egress={"kind": "provider_broad", "fetches": fetches})
