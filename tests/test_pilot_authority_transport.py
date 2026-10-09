"""Fake HTTPS responses for server-side dry-run attribution and call bounds."""

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from test_comparison_pilot_remote import remote_fixture
from test_pilot_isolation import fake_probe_remote, profile

from tessera_worker.comparison_pilot import PilotError
from tessera_worker.pilot_authority_probe import probe_authority
from tessera_worker.pilot_isolation import PROFILE


@pytest.mark.parametrize(
    "response", ["allowed", "policy", "quota", "rbac", "timeout", "oversized", "wrong-policy"]
)
def test_dry_run_requires_positive_or_selected_policy_reply(tmp_path, response):
    remote, journal = remote_fixture(tmp_path)
    remote.isolation = profile()
    calls = []

    def open_request(request, timeout):
        calls.append(request)
        assert timeout == 5
        assert request.full_url.endswith("?dryRun=All")
        if response == "timeout":
            raise TimeoutError("do not print secret diagnostic")
        if response == "allowed":
            return io.BytesIO(b'{"kind":"Job"}')
        message = {
            "policy": f"ValidatingAdmissionPolicy '{remote.isolation.namespace}-jobs' "
            f"denied request: {PROFILE}: constraint 1",
            "quota": "exceeded quota",
            "rbac": "forbidden",
            "wrong-policy": f"other admission: {PROFILE}: constraint 1",
            "oversized": "x" * 65537,
        }[response]
        raise HTTPError(
            request.full_url,
            403,
            "denied",
            {},
            io.BytesIO(json.dumps({"message": message}).encode()),
        )

    remote.opener = SimpleNamespace(open=open_request)
    try:
        if response in {"allowed", "policy"}:
            result = remote.request(
                "POST", remote.jobs_path + "?dryRun=All", {}, admission_probe=True
            )
            assert result == (
                {"kind": "Job"}
                if response == "allowed"
                else {"admission_denied": True, "policy": remote.isolation.namespace}
            )
        else:
            with pytest.raises(PilotError):
                remote.request("POST", remote.jobs_path + "?dryRun=All", {}, admission_probe=True)
        assert len(calls) == 1
        with pytest.raises(PilotError, match="server-side Job dry runs"):
            remote.request("POST", remote.jobs_path, {}, admission_probe=True)
        assert len(calls) == 1
        events = str(journal.db.execute("SELECT payload_json FROM events").fetchall())
        assert "secret diagnostic" not in events and "synthetic-secret" not in events
    finally:
        journal.close()


@pytest.mark.parametrize("failure", ["time", "calls", "mutation"])
def test_probe_bounds_fail_closed(tmp_path, monkeypatch, failure):
    remote, journal, calls = fake_probe_remote(tmp_path)
    if failure == "time":
        ticks = iter((0, 121))
        monkeypatch.setattr(
            "tessera_worker.pilot_authority_probe.time.monotonic", lambda: next(ticks)
        )
    elif failure == "calls":

        def excessive(**_):
            for _ in range(65):
                remote.request("GET", "/api/v1/namespaces/kube-system")

        remote.verify_target = excessive
    else:
        remote.verify_target = lambda **_: remote.request("POST", remote.jobs_path, {})
    try:
        with pytest.raises(PilotError, match="bound|persistent mutations"):
            probe_authority(remote)
        assert len(calls) == (64 if failure == "calls" else 0)
    finally:
        journal.close()
