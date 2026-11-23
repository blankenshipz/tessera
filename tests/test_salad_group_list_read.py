"""The one project group-list read: absence is proven only by a complete list.

Fake connections only: no network, credentials or provider calls. The documented list
(``list_container_groups``) takes no query parameters and returns ``{"items": [...]}``
with ``maxItems: 100`` and no paging fields, so a list of 100+ items (or one carrying a
pagination key) never proves a name absent; the exact-name read then decides.
"""

import json
from uuid import UUID

import pytest
from test_salad_http import (
    GROUP_404,
    MARKER,
    RESERVED_GROUP,
    Factory,
    gpu_class,
    group,
    selection,
)
from test_salad_safety_service import _lost_create

from tessera_control import salad_http
from tessera_control.salad_http import SaladHTTPTransport
from tessera_control.salad_secrets import SecretBuffer

LIST_PATH = "/api/public/organizations/acme/projects/research/containers"
GROUP_PATH = f"{LIST_PATH}/{RESERVED_GROUP}"
QUOTA = (
    200,
    {"container_groups_quotas": dict(container_replicas_quota=5, container_replicas_used=0)},
)
TAIL = (QUOTA, (200, {"items": [gpu_class()]}), (200, {"available_gpu_high": 1}))


def leftover(i):
    """A realistic stopped leftover attempt group (a couple of KiB on the wire)."""
    data = group()
    data.update(
        id=str(UUID(int=i + 1)), name=f"attempt-{i:032x}", readme="r" * 1200, display_name="x"
    )
    return data


def leftovers(n):
    return {"items": [leftover(i) for i in range(n)]}


def preflight(*responses):
    f = Factory(*responses)
    s = selection()
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).preflight(s, secret)
    assert MARKER.decode() not in result.model_dump_json()
    # Read-only on the fixed allowlisted paths: GETs plus the availability POST only,
    # never a query string, never a mutation of a group.
    for method, path, _, headers in f.calls:
        assert "?" not in path
        assert (method == "GET") or path.endswith("/availability/sce-gpu-availability")
        assert set(headers) <= {"Salad-Api-Key", "Accept", "Content-Type"}
    return result, f


def kinds(f):
    return [
        "list" if p == LIST_PATH else "exact" if p == GROUP_PATH else p.rsplit("/", 1)[-1]
        for _, p, _, _ in f.calls
    ]


def test_a_complete_list_without_the_name_proves_absence_in_four_requests():
    result, f = preflight((200, leftovers(3)), *TAIL)
    assert result.status == "observed" and result.observation["group_absent"] is True
    assert kinds(f) == ["list", "quotas", "gpu-classes", "sce-gpu-availability"]


def test_many_stopped_groups_outgrowing_64_kib_still_prove_absence_when_under_100():
    page = leftovers(99)
    assert len(json.dumps(page)) > salad_http.LIMIT  # the former single-read cap
    result, f = preflight((200, page), *TAIL)
    assert result.status == "observed" and result.observation["group_absent"] is True
    assert kinds(f)[0] == "list" and "exact" not in kinds(f)


def test_more_than_100_stopped_groups_no_longer_block_a_start_when_the_name_is_absent():
    # The former cap (>= 100 items => malformed_response) blocked every create here.
    result, f = preflight((200, leftovers(150)), (404, GROUP_404), *TAIL)
    assert result.status == "observed"
    assert result.observation["group_absent"] is True
    assert result.observation["project_exists"] is True
    assert kinds(f) == ["list", "exact", "quotas", "gpu-classes", "sce-gpu-availability"]


def test_exactly_100_items_is_ambiguous_and_falls_to_the_exact_read():
    result, f = preflight((200, leftovers(100)), (404, GROUP_404), *TAIL)
    assert result.status == "observed" and result.observation["group_absent"] is True
    assert kinds(f)[:2] == ["list", "exact"]


def test_a_group_beyond_a_full_list_is_found_by_the_exact_read():
    result, f = preflight((200, leftovers(100)), (200, group()), *TAIL)
    assert result.status == "observed"
    assert result.observation["group_absent"] is False
    assert result.observation["provider_id"] == UUID(group()["id"]).hex
    assert result.observation["state"] == "stopped"
    assert kinds(f)[:2] == ["list", "exact"]


def test_a_listed_group_is_found_without_the_exact_read():
    page = leftovers(120)
    page["items"].insert(57, group())
    result, f = preflight((200, page), *TAIL)
    assert result.observation["group_absent"] is False and "exact" not in kinds(f)


@pytest.mark.parametrize("key", ["next", "page", "page_size", "total_size", "cursor", "nextLink"])
def test_a_pagination_key_makes_even_a_short_list_unproven(key):
    page = dict(leftovers(2), **{key: 2})
    result, f = preflight((200, page), (404, GROUP_404), *TAIL)
    assert result.observation["group_absent"] is True and kinds(f)[:2] == ["list", "exact"]
    unproven, f = preflight((200, page), (503, {}))
    assert unproven.status == "unknown" and kinds(f) == ["list", "exact"]


@pytest.mark.parametrize(
    "reply,reason",
    [
        ((503, {}), "provider_unavailable"),
        (TimeoutError(), "provider_unavailable"),
        ((404, {"status": 500, "title": "Not Found"}), "malformed_response"),
        ((404, {"title": "Gone"}), "malformed_response"),
        ((404, [1]), "malformed_response"),
        ((404, b"<html>gateway</html>"), "malformed_response"),
        ((202, b""), "malformed_response"),
        ((302, b""), "redirect_denied"),
    ],
)
def test_an_unproven_exact_read_after_a_full_list_is_never_absence(reply, reason):
    result, f = preflight((200, leftovers(100)), reply)
    assert result.status == "unknown" and result.reason == reason
    assert "group_absent" not in json.dumps(result.model_dump(mode="json"))
    assert result.request_kind == "read_group" and kinds(f) == ["list", "exact"]


def test_a_mismatched_exact_group_after_a_full_list_fails_closed():
    data = dict(group(), project_name="other")
    result, f = preflight((200, leftovers(100)), (200, data))
    assert result.status == "unknown" and result.reason == "identity_mismatch"


@pytest.mark.parametrize(
    "page",
    [
        {"items": None},
        {"items": {}},
        {},
        {"items": [{}]},
        {"items": [{"name": 7}]},
        {"items": ["attempt"]},
        {"items": [leftover(1), {"id": "no-name"}]},
    ],
)
def test_a_malformed_list_fails_closed_before_any_other_read(page):
    result, f = preflight((200, page))
    assert result.status == "unknown" and result.reason == "malformed_response"
    assert result.request_kind == "list" and kinds(f) == ["list"]


def test_a_duplicated_name_in_the_list_is_an_identity_mismatch():
    result, f = preflight((200, {"items": [group(), group()]}))
    assert result.reason == "identity_mismatch" and kinds(f) == ["list"]


@pytest.mark.parametrize("reply", [(503, {}), TimeoutError(), (429, {"m": "slow"}), (302, b"")])
def test_a_failed_list_read_is_never_absence(reply):
    result, f = preflight(reply)
    assert result.status == "unknown" and result.request_kind == "list" and len(f.calls) == 1


def test_the_list_body_is_still_bounded():
    big = b'{"items": [' + b" " * salad_http.LIST_LIMIT + b"]}"
    result, f = preflight((200, big))
    assert (result.reason, result.request_kind) == ("malformed_response", "list")


def test_only_the_list_read_gets_the_larger_body_bound():
    # An exact group read keeps the 64 KiB cap.
    data = dict(group(), readme="r" * (salad_http.LIMIT + 1))
    result, f = preflight((200, leftovers(100)), (200, data))
    assert (result.reason, result.request_kind) == ("malformed_response", "read_group")


def test_a_list_echoing_the_credential_is_refused():
    page = leftovers(1)
    page["items"][0]["readme"] = MARKER.decode()
    result, _ = preflight((200, page))
    assert result.reason == "malformed_response"


# --- the guardian's lost-create read goes through the same list read --------------------


def absence(*responses):
    f = Factory(*responses)
    s = selection()
    with SecretBuffer(bytearray(MARKER)) as secret:
        result = SaladHTTPTransport(s, connection_factory=f).read_absence(s, secret)
    return result, f


def list_read(result):
    return result.observation["list_read"]


def test_guardian_absence_read_accepts_a_complete_list_beyond_64_kib():
    result, f = absence((404, GROUP_404), (200, leftovers(99)))
    assert result.observation["group_not_found"] is True and list_read(result) == "absent"
    assert [p for _, p, _, _ in f.calls] == [GROUP_PATH, LIST_PATH]


def test_guardian_absence_read_stays_partial_on_100_plus_and_unavailable_when_paged():
    assert list_read(absence((404, GROUP_404), (200, leftovers(150)))[0]) == "absent_partial"
    assert list_read(absence((404, GROUP_404), (200, leftovers(100)))[0]) == "absent_partial"
    paged = dict(leftovers(2), page=1)
    assert list_read(absence((404, GROUP_404), (200, paged))[0]) == "unavailable"
    big = b'{"items": [' + b" " * salad_http.LIST_LIMIT + b"]}"
    assert list_read(absence((404, GROUP_404), (200, big))[0]) == "unavailable"
    listed = leftovers(150)
    listed["items"].append(group())
    assert list_read(absence((404, GROUP_404), (200, listed))[0]) == "present"


def _guardian_with_wire(tmp_path, *responses):
    """A lost-create guardian whose read_absence uses the real wire over fake responses."""
    s, b, clock, provider = _lost_create(tmp_path)
    f = Factory(*responses)
    fake = provider.factory

    def factory(selection):
        inner = fake(selection)

        class Wired:
            def __getattr__(self, name):
                return getattr(inner, name)

            def read_absence(self, selection, secret):
                provider.calls.append("read_absence")
                transport = SaladHTTPTransport(selection, connection_factory=f)
                return transport.read_absence(selection, secret)

        return Wired()

    s.transport_factory = factory
    return s, b, clock, f


def test_guardian_resolves_a_lost_create_from_a_complete_list_beyond_64_kib(tmp_path):
    from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS

    s, b, clock, f = _guardian_with_wire(tmp_path, (404, GROUP_404), (200, leftovers(99)))
    try:
        clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        s.loop()
        status = s.admin(dict(operation="status", reservation=b["reservation"]))
        assert status["status"] == "reconciled" and status["reason"] == "lost_create_absent"
        assert status["lost_create"]["list_read"] == "absent"
        assert all(c[0] == "GET" for c in f.calls)
    finally:
        s.close()


def test_guardian_does_not_auto_resolve_from_a_list_of_100_plus(tmp_path):
    from tessera_control.salad_safety_service import LOST_CREATE_MARGIN_SECONDS

    s, b, clock, f = _guardian_with_wire(tmp_path, (404, GROUP_404), (200, leftovers(150)))
    try:
        clock[0] = b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        s.loop()
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
    finally:
        s.close()
