"""Public V2 API-shape fixtures only; never live/catalog/account admission evidence."""

import copy
import json
from datetime import UTC, datetime

import pytest

from tessera_mcp.harness_binding import HarnessBinding
from tessera_mcp.harness_observer import ObservationBlocked, V2PublicHarnessObserver, _digest


class PublicEndpointFixture:
    def __init__(self, origin, responses):
        self.origin, self.responses, self.calls = origin, responses, []

    def call(self, method, path):
        self.calls.append((method, path))
        assert method == "GET"
        assert path in self.responses, "Undocumented/private route requested"
        return copy.deepcopy(self.responses[path])


@pytest.fixture
def setup():
    query = "?location%5Bdirectory%5D=%2Fsrv%2Fresearch"
    paths = ("/api/info", "/api/location", "/api/mcp", "/api/skill", "/api/session/{sessionID}")
    schema = {"openapi": "3.1.0", "paths": {path: {"get": {}} for path in paths}}
    manifest = {
        "harness_schema_sha256": _digest(schema),
        "harness_version": "2.fixture",
        "workspace": "/srv/research",
        "skill_id": "tessera-governed-research",
        "agent": "tessera-planner",
        "model": "openai/fixture-model",
        "project_id": "project",
        "subject_credential_id": "credential",
        "destination": "openai:fixture",
        "control_origin": "https://control.example.test",
        "required_enrollment_scopes": ["context:read"],
    }
    binding = HarnessBinding(b"{}", json.dumps(manifest).encode(), b"fixture raw SKILL.md")
    location = {"directory": "/srv/research"}
    harness = PublicEndpointFixture(
        "https://research.example.test",
        {
            "/openapi.json": schema,
            "/api/info": {
                "version": "2.fixture",
                "pid": 1234,
                "urls": ["https://research.example.test"],
                "paths": {"tmp": "/private/fixture"},
            },
            "/api/location" + query: {
                **location,
                "project": {
                    "id": "fixture",
                    "directory": "/srv/research",
                    "canonical": "/srv/research",
                },
            },
            "/api/mcp" + query: {
                "location": location,
                "data": [{"name": "tessera", "status": {"status": "connected"}}],
            },
            "/api/skill" + query: {
                "location": location,
                "data": [
                    {
                        "id": "tessera-governed-research",
                        "name": "Governed research",
                        "path": "/srv/research/skills/tessera-governed-research/SKILL.md",
                        "content": "Fixture parsed skill BODY, not raw file bytes",
                        "autoinvoke": True,
                    }
                ],
            },
            "/api/session/ses_fixture": {
                "data": {
                    "id": "ses_fixture",
                    "projectID": "fixture",
                    "agent": "tessera-planner",
                    "model": {"providerID": "openai", "id": "fixture-model"},
                    "location": location,
                    "cost": 0,
                    "tokens": {},
                    "time": {"created": 1, "updated": 1},
                }
            },
        },
    )
    control = PublicEndpointFixture(
        "https://control.example.test",
        {
            "/external-agent/status": {
                "id": "actor",
                "credential_id": "credential",
                "project_id": "project",
                "destination": "openai:fixture",
                "actions": ["context:read"],
                "expires_at": "2030-01-01T01:00:00Z",
            }
        },
    )
    kwargs = {
        "binding": binding,
        "harness_endpoint": harness,
        "control_endpoint": control,
        "research_origin": harness.origin,
        "coding_origins": ("http://127.0.0.1:4096",),
        "session_id": "ses_fixture",
        "clock": lambda: datetime(2030, 1, 1, tzinfo=UTC),
    }
    return kwargs, harness, control


def test_supported_public_measurements_never_complete_admission(setup):
    kwargs, harness, control = setup
    observer = V2PublicHarnessObserver(**kwargs)
    report = observer.inspect_public()
    assert report.measured["model_id"] == "fixture-model"
    assert report.measured["observed_harness_version"] == "2.fixture"
    assert report.measured["mcp_connection"] == "connected_not_catalog_proof"
    assert report.measured["principal_status"]["id"] == "actor"
    assert len(report.gaps) == 6
    for absent in (
        "effective_config",
        "observed_catalog",
        "selected_skill",
        "included_oauth_verified",
        "account_mode",
        "observed_harness_instance_id",
        "observed_build_sha256",
        "observed_mcp_sdk_version",
    ):
        assert absent not in report.measured
    with pytest.raises(ObservationBlocked, match="harness_discovered_full_mcp_catalog_unavailable"):
        report.admission_fields()
    with pytest.raises(ObservationBlocked):
        observer()
    assert all(method == "GET" for method, _ in harness.calls + control.calls)
    assert not any(
        "config" in path
        or "credential" in path
        or "/message" in path
        or "provider" in path
        or "/rpc" in path
        or "integration" in path
        for _, path in harness.calls
    )


@pytest.mark.parametrize("change", ["origin", "transport", "control_origin", "no_inventory", "pid"])
def test_coding_service_and_transport_reuse_denied(setup, change):
    kwargs, harness, _ = setup
    if change == "origin":
        kwargs["coding_origins"] = (harness.origin,)
    elif change == "transport":
        kwargs["research_origin"] = "https://different.example.test"
    elif change == "control_origin":
        kwargs["control_endpoint"].origin = "https://other-control.example.test"
    elif change == "no_inventory":
        kwargs["coding_origins"] = ()
    else:
        kwargs["coding_pids"] = (1234,)
    with pytest.raises(ObservationBlocked):
        V2PublicHarnessObserver(**kwargs).inspect_public()


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "schema",
        "missing_route",
        "workspace",
        "extra_server",
        "catalog_injection",
        "skill_missing",
        "model",
        "session",
        "principal",
        "scope",
        "expiry",
    ],
)
def test_public_shape_and_current_pin_denials(setup, change):
    kwargs, harness, control = setup
    responses = harness.responses
    mcp_path = next(path for path in responses if path.startswith("/api/mcp"))
    skill_path = next(path for path in responses if path.startswith("/api/skill"))
    location_path = next(path for path in responses if path.startswith("/api/location"))
    if change == "version":
        responses["/api/info"]["version"] = "changed"
    elif change == "schema":
        responses["/openapi.json"]["changed"] = True
    elif change == "missing_route":
        schema = responses["/openapi.json"]
        del schema["paths"]["/api/skill"]
        manifest = json.loads(kwargs["binding"].manifest)
        manifest["harness_schema_sha256"] = _digest(schema)
        kwargs["binding"] = HarnessBinding(b"{}", json.dumps(manifest).encode(), b"fixture")
    elif change == "workspace":
        responses[location_path]["directory"] = "/coding"
    elif change == "extra_server":
        responses[mcp_path]["data"].append(
            {"name": "owner_proxy", "status": {"status": "connected"}}
        )
    elif change == "catalog_injection":
        responses[mcp_path]["data"][0]["tools"] = [{"name": "manifest-echo"}]
    elif change == "skill_missing":
        responses[skill_path]["data"] = []
    elif change == "model":
        responses["/api/session/ses_fixture"]["data"]["model"]["id"] = "paid-fallback"
    elif change == "session":
        responses["/api/session/ses_fixture"]["data"]["id"] = "ses_coding"
    elif change == "principal":
        control.responses["/external-agent/status"]["credential_id"] = "owner"
    elif change == "scope":
        control.responses["/external-agent/status"]["actions"] = []
    else:
        control.responses["/external-agent/status"]["expires_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(ObservationBlocked):
        V2PublicHarnessObserver(**kwargs).inspect_public()


def test_upstream_error_redaction_and_account_boolean_not_supported(setup):
    kwargs, harness, _ = setup

    def unavailable(method, path):
        raise ValueError("unsafe-secret-body-marker")

    harness.call = unavailable
    with pytest.raises(ObservationBlocked) as caught:
        V2PublicHarnessObserver(**kwargs).inspect_public()
    assert "unsafe-secret-body-marker" not in str(caught.value)
    with pytest.raises(TypeError):
        V2PublicHarnessObserver(**kwargs, included_oauth_verified=True)
