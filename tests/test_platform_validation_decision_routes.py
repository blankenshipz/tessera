"""Dataset-snapshot upload is a decision route for the live validation harness."""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from platform_validation import common  # noqa: E402

UPLOAD = "/projects/p1/tabular-dataset-snapshots"


def test_upload_route_is_a_decision_call():
    assert common.is_decision_call("POST", UPLOAD)
    assert common.is_decision_call("post", UPLOAD + "?x=1")
    assert not common.is_decision_call("GET", UPLOAD)
    assert not common.is_decision_call("POST", "/projects/p1/tabular-dataset-snapshots/extra")
    assert not common.is_decision_call("POST", "/projects//tabular-dataset-snapshots")
    assert not common.is_decision_call("POST", "/projects/p1/other-snapshots")


def test_neighbouring_routes_unchanged():
    assert common.is_decision_call("POST", "/tasks/t1/approve")
    assert common.is_decision_call("POST", "/snapshots/s1/releases")
    assert not common.is_decision_call("POST", "/projects")
    assert not common.is_decision_call("GET", "/snapshots/s1/releases")


@pytest.mark.parametrize(("path", "labelled"), [(UPLOAD, True), ("/projects", False)])
def test_human_request_headers(tmp_path, monkeypatch, path, labelled):
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake(req, timeout=None):
        seen.update({k.lower(): v for k, v in req.header_items()})
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    header = tmp_path / "owner.header"
    header.write_text("Authorization: Bearer synthetic\n")
    ctx = common.Context(state_dir=tmp_path, base_url="http://x", owner_header_path=header)
    ctx.request("POST", path, json={}, auth="human")
    assert (common.AUTOMATION_HEADER.lower() in seen) is labelled
