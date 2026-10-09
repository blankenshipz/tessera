"""Version-pinned public V2 observation reader, with explicit unprovable admission gaps.

No credential, config, filesystem, message/log, provider request or inference APIs.
The supplied readers must be ordinary authenticated GET adapters for a separately
authorized dedicated research instance and Tessera control. No connection flag,
separate MCP client or caller account boolean can complete admission here.
"""

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit

import httpx

from tessera_mcp.harness_binding import ACCOUNT_MODE, HarnessBinding, verify_binding

_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_MISSING = (
    "effective_config_safe_runtime_projection_unavailable",
    "harness_discovered_full_mcp_catalog_unavailable",
    "selected_skill_raw_bytes_and_activation_unavailable",
    "stable_harness_instance_identity_unavailable",
    "installed_build_and_mcp_sdk_identity_unavailable",
    "active_included_oauth_route_and_no_paid_fallback_unavailable",
)


class ObservationBlocked(ValueError):
    """Sanitized denial containing only fixed reason codes, never upstream values."""

    def __init__(self, *gaps):
        self.gaps = tuple(gaps)
        super().__init__("harness_observation_blocked:" + ",".join(self.gaps))


class EvidenceBackedHarnessObserver:
    """Opt-in v2 evidence, not legacy V2AttestedHarnessObserver/OpenCode V2.

    Owner supplies authenticated preparation reader. No phase diagnostic conversion.
    """

    def __init__(self, *, binding, verifier, read_preparation):
        from .runtime_admission import RuntimeAdmissionVerifier

        if type(verifier) is not RuntimeAdmissionVerifier or not callable(read_preparation):
            raise ObservationBlocked("approved_v2_observer_ports_required")
        self.binding, self.verifier, self.read_preparation = binding, verifier, read_preparation

    def __call__(self):
        from .harness_binding import verify_binding_v2

        evidence, expected = self.read_preparation()
        return verify_binding_v2(
            self.binding, verifier=self.verifier, evidence=evidence, expected=expected
        )


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise ObservationBlocked("invalid_dedicated_harness_origin")
    return value.rstrip("/")


@dataclass(frozen=True)
class PublicObservationReport:
    measured: dict
    gaps: tuple[str, ...]

    def admission_fields(self):
        # Never emit a partial verify_binding kwargs dict that could be filled from a manifest.
        raise ObservationBlocked(*self.gaps)


class V2PublicHarnessObserver:
    """Executable bounded GET seam; current documented REST surface cannot admit a run.

    harness_endpoint/control_endpoint are authenticated adapters handed in by the installed
    service factory, not credential values or an account-admitted boolean. Constructor
    accepts no catalog/config/build/account override or fallback reader. Fixtures are
    synthetic; supplying functions does not independently establish authentication.
    """

    def __init__(
        self,
        *,
        binding: HarnessBinding,
        harness_endpoint,
        control_endpoint,
        research_origin: str,
        coding_origins: tuple[str, ...],
        session_id: str,
        coding_pids: tuple[int, ...] = (),
        clock=lambda: datetime.now(UTC),
    ):
        if not coding_origins:
            raise ObservationBlocked("coding_service_exclusion_inventory_missing")
        self.origin = _origin(research_origin)
        if self.origin in {_origin(origin) for origin in coding_origins}:
            raise ObservationBlocked("coding_service_reuse_denied")
        if (
            not isinstance(session_id, str)
            or not _ID.fullmatch(session_id)
            or not session_id.startswith("ses_")
        ):
            raise ObservationBlocked("invalid_research_session_id")
        if any(type(pid) is not int or pid <= 0 for pid in coding_pids):
            raise ObservationBlocked("invalid_coding_process_inventory")
        self.binding, self.expected = binding, json.loads(binding.manifest)
        if _origin(harness_endpoint.origin) != self.origin or _origin(
            control_endpoint.origin
        ) != _origin(self.expected["control_origin"]):
            raise ObservationBlocked("authenticated_transport_origin_binding_changed")
        self.harness_get = lambda path: harness_endpoint.call("GET", path)
        self.control_get = lambda path: control_endpoint.call("GET", path)
        self.clock = clock
        self.session_id, self.coding_pids = session_id, coding_pids
        self.query = "?" + urlencode({"location[directory]": self.expected["workspace"]})

    def _get(self, reader, path, *, bound=1048576):
        try:
            value = reader(path)
            encoded = json.dumps(value, sort_keys=True, allow_nan=False).encode()
            if len(encoded) > bound:
                raise ValueError()
            return value
        except Exception:
            raise ObservationBlocked("public_api_unavailable_or_response_bound") from None

    def _located(self, response):
        if (
            not isinstance(response, dict)
            or set(response) != {"location", "data"}
            or not isinstance(response.get("location"), dict)
            or response.get("location", {}).get("directory") != self.expected["workspace"]
        ):
            raise ObservationBlocked("public_api_location_or_shape_changed")
        return response["data"]

    def inspect_public(self) -> PublicObservationReport:
        document = self._get(self.harness_get, "/openapi.json")
        if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
            raise ObservationBlocked("public_api_contract_missing")
        schema_digest = _digest(document)
        if schema_digest != self.expected["harness_schema_sha256"]:
            raise ObservationBlocked("harness_openapi_pin_changed")
        # These routes and response discriminators are documented in V2, not V1.
        required = (
            "/api/info",
            "/api/location",
            "/api/mcp",
            "/api/skill",
            "/api/session/{sessionID}",
        )
        if any("get" not in document.get("paths", {}).get(path, {}) for path in required):
            raise ObservationBlocked("public_api_contract_missing")
        info = self._get(self.harness_get, "/api/info", bound=16384)
        if (
            not isinstance(info, dict)
            or set(info) - {"version", "pid", "urls", "paths", "capabilities"}
            or info.get("version") != self.expected["harness_version"]
            or type(info.get("pid")) is not int
            or info["pid"] <= 0
            or not isinstance(info.get("urls"), list)
            or not isinstance(info.get("paths"), dict)
        ):
            raise ObservationBlocked("harness_version_or_identity_changed")
        if info["pid"] in self.coding_pids:
            raise ObservationBlocked("coding_service_reuse_denied")
        location = self._get(self.harness_get, "/api/location" + self.query, bound=16384)
        if (
            not isinstance(location, dict)
            or location.get("directory") != self.expected["workspace"]
        ):
            raise ObservationBlocked("research_workspace_changed")
        servers = self._located(self._get(self.harness_get, "/api/mcp" + self.query, bound=16384))
        if (
            not isinstance(servers, list)
            or len(servers) != 1
            or not isinstance(servers[0], dict)
            or servers[0].get("name") != "tessera"
            or set(servers[0]) - {"name", "status", "integrationID"}
            or servers[0].get("status") != {"status": "connected"}
        ):
            raise ObservationBlocked("mcp_connection_or_server_inventory_changed")
        skills = self._located(self._get(self.harness_get, "/api/skill" + self.query, bound=131072))
        if not isinstance(skills, list):
            raise ObservationBlocked("registered_skill_shape_changed")
        matching = [
            skill
            for skill in skills
            if isinstance(skill, dict) and skill.get("id") == self.expected["skill_id"]
        ]
        if len(matching) != 1 or not isinstance(matching[0].get("content"), str):
            raise ObservationBlocked("registered_skill_missing_or_ambiguous")
        skill = matching[0]
        if set(skill) - {"id", "name", "description", "autoinvoke", "path", "content"}:
            raise ObservationBlocked("registered_skill_shape_changed")
        body = skill["content"].encode("utf-8")
        if not body or len(body) > 32768:
            raise ObservationBlocked("registered_skill_content_bound")
        session = self._get(self.harness_get, "/api/session/" + self.session_id, bound=65536)
        data = session.get("data") if isinstance(session, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("id") != self.session_id
            or not isinstance(data.get("location"), dict)
            or data.get("location", {}).get("directory") != self.expected["workspace"]
            or data.get("agent") != self.expected["agent"]
        ):
            raise ObservationBlocked("research_session_or_agent_changed")
        model = data.get("model", {})
        if (
            not isinstance(model, dict)
            or set(model) != {"providerID", "id"}
            or not isinstance(model.get("id"), str)
            or model["providerID"] != "openai"
            or "openai/" + model["id"] != self.expected["model"]
        ):
            raise ObservationBlocked("research_model_changed")
        principal = self._get(self.control_get, "/external-agent/status", bound=16384)
        if (
            not isinstance(principal, dict)
            or principal.get("credential_id") != self.expected["subject_credential_id"]
            or principal.get("project_id") != self.expected["project_id"]
            or principal.get("destination") != self.expected["destination"]
        ):
            raise ObservationBlocked("research_control_principal_changed")
        try:
            expires = datetime.fromisoformat(principal["expires_at"].replace("Z", "+00:00"))
            scopes = principal["actions"]
            if (
                not isinstance(scopes, list)
                or any(not isinstance(scope, str) for scope in scopes)
                or not set(self.expected["required_enrollment_scopes"]) <= set(scopes)
                or expires.tzinfo is None
                or expires <= self.clock()
            ):
                raise ValueError()
        except (TypeError, KeyError, ValueError):
            raise ObservationBlocked("research_control_scope_or_expiry_changed") from None
        measured = {
            "observed_harness_version": info["version"],
            "observed_harness_schema_sha256": schema_digest,
            "observed_process_pid": info["pid"],
            "research_origin": self.origin,
            "workspace": location["directory"],
            "session_id": data["id"],
            "provider_id": model["providerID"],
            "model_id": model["id"],
            "registered_skill_id": skill["id"],
            "registered_skill_body_sha256": hashlib.sha256(body).hexdigest(),
            "mcp_connection": "connected_not_catalog_proof",
            "principal_status": {
                key: principal.get(key)
                for key in (
                    "id",
                    "credential_id",
                    "project_id",
                    "destination",
                    "actions",
                    "expires_at",
                )
            },
        }
        return PublicObservationReport(measured, _MISSING)

    def __call__(self):
        """ResearchConsumer observation_reader port: fail closed while required APIs are absent."""
        return self.inspect_public().admission_fields()


class ObserverHttpEndpoint:
    """Supplied authorized server credential; strict origin, TLS and redirect fences.

    Does not discover credentials. Factory must supply dedicated server authorization
    through its existing secure mapper. Never use owner/provider tokens here.
    """

    def __init__(self, origin, authorization, *, transport=None):
        self.origin = _origin(origin)
        if (
            not isinstance(authorization, str)
            or not authorization.startswith(("Basic ", "Bearer "))
            or any(ord(char) < 32 or ord(char) == 127 for char in authorization)
            or len(authorization.split(" ", 1)[1]) < 1
        ):
            raise ObservationBlocked("observer_server_authentication_missing")
        self._client = httpx.Client(
            base_url=self.origin,
            headers={"Authorization": authorization},
            follow_redirects=False,
            trust_env=False,
            timeout=5,
            transport=transport,
        )

    def call(self, method, path, body=None):
        route = urlsplit(path)
        relative = not route.scheme and not route.netloc and not route.fragment
        located = route.query.startswith("location%5Bdirectory%5D=") and "&" not in route.query
        allowed_get = relative and (
            path in {"/openapi.json", "/api/info", "/external-agent/status"}
            or (route.path in {"/api/location", "/api/mcp", "/api/skill"} and located)
            or (
                re.fullmatch(r"/api/session/ses_[A-Za-z0-9_-]{1,196}", route.path) is not None
                and not route.query
            )
        )
        allowed_post = path == "/tessera/runtime-guard/v1/observe" or (
            relative
            and route.path
            in {
                "/api/rpc/tessera.harness.observer.v1/observe",
                "/api/rpc/tessera.harness.observer.v1/produce",
            }
            and located
        )
        if not (
            (method == "GET" and allowed_get and body is None)
            or (method == "POST" and allowed_post and isinstance(body, dict))
        ):
            raise ObservationBlocked("observer_route_not_permitted")
        try:
            with self._client.stream(method, path, json=body) as response:
                if (
                    response.status_code != 200
                    or response.request.url.host != urlsplit(self.origin).hostname
                ):
                    raise ValueError()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 1048576:
                        raise ValueError()
                return json.loads(raw)
        except Exception:
            raise ObservationBlocked("observer_authenticated_endpoint_unavailable") from None

    def close(self):
        self._client.close()


class V2PluginHarnessObserver(V2PublicHarnessObserver):
    """Actual registered in-harness RPC projection; never turns metadata into admission.

    Same constructor as V2PublicHarnessObserver. Use ObserverHttpEndpoint adapters
    to prohibit redirects and require supplied server auth. RPC is existing V2 HTTP,
    not a new sideport. Full verifier admission is still blocked on explicit gaps.
    """

    def __init__(self, **kwargs):
        if not all(
            isinstance(kwargs.get(name), ObserverHttpEndpoint)
            for name in ("harness_endpoint", "control_endpoint")
        ):
            raise ObservationBlocked("strict_authenticated_observer_transport_required")
        super().__init__(**kwargs)
        self._harness_endpoint = kwargs["harness_endpoint"]

    def inspect_public(self):
        public = super().inspect_public()
        nonce = secrets.token_hex(32)
        try:
            response = self._harness_endpoint.call(
                "POST",
                "/api/rpc/tessera.harness.observer.v1/observe" + self.query,
                {
                    "input": {
                        "protocol": "tessera-harness-observer-v1",
                        "nonce": nonce,
                        "sessionID": self.session_id,
                    }
                },
            )
            value = response["output"]
            fields = {
                "protocol",
                "nonce",
                "sessionID",
                "workspace",
                "harnessVersion",
                "registeredCatalog",
                "registeredSkill",
                "selectedModel",
                "activeConnection",
                "snapshot",
                "gaps",
            }
            if (
                set(response) != {"output"}
                or not isinstance(value, dict)
                or set(value) != fields
                or value["protocol"] != "tessera-harness-observer-v1"
                or value["nonce"] != nonce
                or value["sessionID"] != self.session_id
                or value["workspace"] != self.expected["workspace"]
                or value["harnessVersion"] != self.expected["harness_version"]
                or len(json.dumps(value).encode()) > 131072
            ):
                raise ValueError()
            rows = value["registeredCatalog"]
            if (
                not isinstance(rows, list)
                or not rows
                or len(rows) > 128
                or any(
                    not isinstance(row, dict)
                    or set(row) != {"name", "description", "inputSchema"}
                    or not isinstance(row["description"], str)
                    or not isinstance(row["inputSchema"], dict)
                    or "~standard" in row["inputSchema"]
                    for row in rows
                )
                or sorted(row["name"] for row in rows)
                != sorted(self.expected["permission_actions"])
            ):
                raise ValueError()
            skill = value["registeredSkill"]
            if skill != {
                "id": public.measured["registered_skill_id"],
                "bodySha256": public.measured["registered_skill_body_sha256"],
            } or value["selectedModel"] != {
                "providerID": public.measured["provider_id"],
                "id": public.measured["model_id"],
            }:
                raise ValueError()
            required = set(_MISSING) - {"harness_discovered_full_mcp_catalog_unavailable"}
            gaps = value["gaps"]
            if (
                not isinstance(gaps, list)
                or any(not isinstance(gap, str) or len(gap) > 160 for gap in gaps)
                or not required <= set(gaps)
                or "model_snapshot_final_hook_order_unproven" not in gaps
            ):
                raise ValueError()
            connection = value["activeConnection"]
            if connection is not None:
                expected_keys = (
                    {"type", "id", "method", "needsAuth"}
                    if connection.get("type") == "credential"
                    else {"type", "needsAuth"}
                )
                if (
                    set(connection) != expected_keys
                    or type(connection["needsAuth"]) is not bool
                    or connection["type"] not in {"credential", "env"}
                    or (
                        connection["type"] == "credential"
                        and (
                            connection["method"] not in {"oauth", "key"}
                            or not isinstance(connection["id"], str)
                        )
                    )
                ):
                    raise ValueError()
            snapshot = value["snapshot"]
            if snapshot is not None:
                if (
                    set(snapshot) != {"model", "catalog", "sequence", "capturedAt"}
                    or snapshot["model"] != value["selectedModel"]
                    or snapshot["catalog"] != rows
                    or type(snapshot["sequence"]) is not int
                    or snapshot["sequence"] < 1
                    or not isinstance(snapshot["capturedAt"], str)
                ):
                    raise ValueError()
            measured = {
                **public.measured,
                "registered_harness_catalog": rows,
                "registered_harness_catalog_sha256": _digest(rows),
                "active_connection_metadata": connection,
                "request_snapshot": snapshot,
                "observer_protocol": value["protocol"],
            }
            return PublicObservationReport(measured, tuple(gaps))
        except ObservationBlocked:
            raise
        except Exception:
            raise ObservationBlocked("plugin_observation_shape_or_pin_changed") from None

    def produce_partial(self):
        """Consume actual authenticated shipped producer; partial evidence cannot admit."""
        report = self.inspect_public()
        nonce = secrets.token_hex(32)
        try:
            response = self._harness_endpoint.call(
                "POST",
                "/api/rpc/tessera.harness.observer.v1/produce" + self.query,
                {
                    "input": {
                        "protocol": "tessera-harness-observer-v1",
                        "nonce": nonce,
                        "sessionID": self.session_id,
                    }
                },
            )
            _exact(response, "output")
            value = _exact(
                response["output"],
                "protocol complete nonce sessionID issuedAt expiresAt "
                "publicObservation agentPermissions providerMetadata "
                "transportObservations sources missing",
            )
            now = self.clock()
            if (
                value["protocol"] != "tessera-runtime-guard-partial-v1"
                or value["complete"] is not False
                or value["nonce"] != nonce
                or value["sessionID"] != self.session_id
                or not _time(value["issuedAt"]) <= now < _time(value["expiresAt"])
                or (_time(value["expiresAt"]) - _time(value["issuedAt"])).total_seconds() > 30
                or len(json.dumps(value).encode()) > 131072
            ):
                raise ValueError()
            observation = _exact(
                value["publicObservation"],
                "protocol nonce sessionID workspace harnessVersion "
                "registeredCatalog registeredSkill selectedModel activeConnection snapshot gaps",
            )
            if (
                observation["nonce"] != nonce
                or observation["workspace"] != self.expected["workspace"]
                or observation["sessionID"] != self.session_id
                or observation["registeredCatalog"] != report.measured["registered_harness_catalog"]
                or observation["registeredSkill"]
                != {
                    "id": report.measured["registered_skill_id"],
                    "bodySha256": report.measured["registered_skill_body_sha256"],
                }
                or observation["harnessVersion"] != report.measured["observed_harness_version"]
                or observation["snapshot"] != report.measured["request_snapshot"]
                or observation["activeConnection"] != report.measured["active_connection_metadata"]
                or observation["selectedModel"]
                != {"providerID": report.measured["provider_id"], "id": report.measured["model_id"]}
            ):
                raise ValueError()
            for rule in value["agentPermissions"]:
                _exact(rule, "action resource effect")
                if any(not isinstance(v, str) or len(v) > 512 for v in rule.values()) or rule[
                    "effect"
                ] not in {"allow", "deny", "ask"}:
                    raise ValueError()
            metadata = _exact(value["providerMetadata"], "id integrationID package")
            if metadata["id"] != report.measured["provider_id"] or any(
                not isinstance(v, str) for v in metadata.values()
            ):
                raise ValueError()
            for item in value["transportObservations"]:
                _exact(item, "hook origin method model kind capturedAt")
                parsed = urlsplit(item["origin"])
                if (
                    parsed.scheme not in {"https", "wss"}
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or parsed.path not in {"", "/"}
                    or not 0 <= (now - _time(item["capturedAt"])).total_seconds() <= 30
                    or item["model"] != observation["selectedModel"]
                ):
                    raise ValueError()
            if (
                not isinstance(value["missing"], list)
                or not set(report.gaps) <= set(value["missing"])
                or any(not isinstance(item, str) for item in value["missing"] + value["sources"])
            ):
                raise ValueError()
            return PublicObservationReport(
                {**report.measured, "partial_guard_producer": value}, tuple(value["missing"])
            )
        except Exception:
            raise ObservationBlocked("partial_producer_invalid_or_unavailable") from None


_GUARD_PROTOCOL = "tessera-runtime-guard-admission-v1"
_MEASUREMENT_ORIGINS = {
    "config": "guard-effective-registry-export",
    "catalog": "guard-harness-registry-export",
    "skill": "guard-loaded-skill-and-artifact-readback",
    "identity": "guard-supervised-process-and-installed-artifacts",
    "account": "guard-selected-route-entitlement-and-billing-policy",
    "hooks": "guard-final-request-snapshot-and-plugin-chain",
}


@dataclass(frozen=True)
class GuardTrust:
    """Human-approved issuer/trust pins, not asserted runtime measurements.

    Obtain through the authorized runtime factory, never from plugin options or
    the attestation response. producer_sha256 pins the approved implementation;
    the authenticated issuer still bears responsibility for truthful measurements.
    """

    issuer: str
    trust_reference: str
    producer_sha256: str
    enrollment_id: str
    instance_id: str
    principal_id: str
    entitlement_reference: str
    entitlement_sha256: str
    policy_sha256: str


def _exact(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError()
    return value


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError()
    return parsed


class V2AttestedHarnessObserver(V2PluginHarnessObserver):
    """Production consumption boundary for an independently authenticated guard.

    No producer is fabricated here. HTTPS authenticates the configured issuer,
    not the truth of its claims. The issuer must implement the measurement contract
    and current enrollment revocation check documented in the package README.
    """

    def __init__(self, *, guard_endpoint, guard_trust: GuardTrust, **kwargs):
        super().__init__(**kwargs)
        if (
            not isinstance(guard_endpoint, ObserverHttpEndpoint)
            or not guard_endpoint.origin.startswith("https://")
            or guard_endpoint.origin in {self.origin, self.expected["control_origin"]}
        ):
            raise ObservationBlocked("independent_authenticated_guard_required")
        if (
            not isinstance(guard_trust, GuardTrust)
            or any(
                not isinstance(value, str) or not value or len(value) > 512
                for value in vars(guard_trust).values()
            )
            or any(
                not re.fullmatch(r"[a-f0-9]{64}", getattr(guard_trust, name))
                for name in ("producer_sha256", "entitlement_sha256", "policy_sha256")
            )
        ):
            raise ObservationBlocked("approved_guard_trust_pins_missing")
        self.guard_endpoint, self.guard_trust = guard_endpoint, guard_trust

    def __call__(self):
        report = self.inspect_public()
        nonce = secrets.token_hex(32)
        trust = self.guard_trust
        request = {
            "protocol": _GUARD_PROTOCOL,
            "nonce": nonce,
            "enrollment_id": trust.enrollment_id,
            "binding_sha256": self.binding.digest,
            "instance_id": trust.instance_id,
            "session_id": self.session_id,
            "research_origin": self.origin,
            "public_observation_sha256": _digest(report.measured),
        }
        try:
            claim = self.guard_endpoint.call("POST", "/tessera/runtime-guard/v1/observe", request)
            _exact(
                claim,
                "protocol nonce enrollment_id binding_sha256 instance_id session_id "
                "research_origin public_observation_sha256 issuer trust_reference "
                "producer_sha256 proof_id revision "
                "state issued_at expires_at measurements evidence",
            )
            if any(claim[key] != value for key, value in request.items()):
                raise ValueError()
            if (
                claim["issuer"] != trust.issuer
                or claim["trust_reference"] != trust.trust_reference
                or claim["producer_sha256"] != trust.producer_sha256
                or claim["state"] != "active"
                or not isinstance(claim["proof_id"], str)
                or not _ID.fullmatch(claim["proof_id"])
                or type(claim["revision"]) is not int
                or claim["revision"] < 1
            ):
                raise ValueError()
            now = self.clock()
            issued, expires = _time(claim["issued_at"]), _time(claim["expires_at"])
            if not issued <= now < expires or (expires - issued).total_seconds() > 30:
                raise ValueError()
            measured = _exact(
                claim["measurements"],
                "effective_config selected_skill_base64 skill_body_sha256 "
                "catalog harness_version schema_sha256 build_sha256 mcp_sdk_version principal_id "
                "provider_id model_id account hooks",
            )
            evidence = _exact(claim["evidence"], "config catalog skill identity account hooks")
            for key, origin in _MEASUREMENT_ORIGINS.items():
                item = _exact(evidence[key], "origin mode captured_at revision value_sha256")
                if (
                    item["origin"] != origin
                    or item["mode"] != "measured"
                    or type(item["revision"]) is not int
                    or item["revision"] < 1
                    or not issued <= _time(item["captured_at"]) <= now
                ):
                    raise ValueError()
                values = {
                    "config": measured["effective_config"],
                    "catalog": measured["catalog"],
                    "skill": {
                        name: measured[name]
                        for name in ("selected_skill_base64", "skill_body_sha256")
                    },
                    "identity": {
                        name: measured[name]
                        for name in (
                            "harness_version",
                            "schema_sha256",
                            "build_sha256",
                            "mcp_sdk_version",
                            "principal_id",
                            "provider_id",
                            "model_id",
                        )
                    },
                    "account": measured["account"],
                    "hooks": measured["hooks"],
                }
                if item["value_sha256"] != _digest(values[key]):
                    raise ValueError()
            public = report.measured
            if (
                measured["principal_id"] != trust.principal_id
                or public["principal_status"].get("id") != trust.principal_id
                or measured["provider_id"] != public["provider_id"]
                or measured["model_id"] != public["model_id"]
                or measured["harness_version"] != public["observed_harness_version"]
                or measured["schema_sha256"] != public["observed_harness_schema_sha256"]
                or measured["skill_body_sha256"] != public["registered_skill_body_sha256"]
            ):
                raise ValueError()
            catalog = measured["catalog"]
            # Explicit sdk-native-name-map-v1; no hidden schemas or separate-client catalog.
            projected = []
            for row in catalog:
                _exact(row, "name description inputSchema")
                projected.append({**row, "name": "tessera_" + row["name"]})
            if (
                sorted(projected, key=lambda row: row["name"])
                != public["registered_harness_catalog"]
            ):
                raise ValueError()
            account = _exact(
                measured["account"],
                "connection_id method provider_id model_id route "
                "entitlement_reference entitlement_sha256 policy_sha256 policy",
            )
            connection = public["active_connection_metadata"]
            if (
                connection
                != {
                    "type": "credential",
                    "id": account["connection_id"],
                    "method": "oauth",
                    "needsAuth": False,
                }
                or account["method"] != "oauth"
                or account["provider_id"] != measured["provider_id"]
                or account["model_id"] != measured["model_id"]
                or account["route"] != "chatgpt-included"
                or account["entitlement_reference"] != trust.entitlement_reference
                or account["entitlement_sha256"] != trust.entitlement_sha256
                or account["policy_sha256"] != trust.policy_sha256
                or _digest(account["policy"]) != trust.policy_sha256
                or account["policy"]
                != {
                    "allowed_auth_methods": ["oauth"],
                    "billing_routes": ["chatgpt-included"],
                    "on_unknown": "deny",
                    "on_exhausted": "stop",
                }
            ):
                raise ValueError()
            hooks = _exact(
                measured["hooks"],
                "chain snapshot_sha256 final_guard_id final_guard_revision "
                "registered_catalog_sha256 effective_config_sha256",
            )
            snapshot = public["request_snapshot"]
            if (
                snapshot is None
                or not 0 <= (now - _time(snapshot["capturedAt"])).total_seconds() <= 30
                or not isinstance(hooks["chain"], list)
                or not hooks["chain"]
                or len(hooks["chain"]) > 64
                or any(not isinstance(name, str) for name in hooks["chain"])
                or len(set(hooks["chain"])) != len(hooks["chain"])
                or hooks["chain"][-1] != hooks["final_guard_id"]
                or hooks["final_guard_id"] != "tessera-runtime-admission-guard"
                or type(hooks["final_guard_revision"]) is not int
                or hooks["final_guard_revision"] < 1
                or hooks["snapshot_sha256"] != _digest(snapshot)
                or hooks["registered_catalog_sha256"] != public["registered_harness_catalog_sha256"]
                or hooks["effective_config_sha256"] != _digest(measured["effective_config"])
            ):
                raise ValueError()
            skill = base64.b64decode(measured["selected_skill_base64"], validate=True)
            if not 1 <= len(skill) <= 32768:
                raise ValueError()
            observations = {
                "effective_config": measured["effective_config"],
                "selected_skill": skill,
                "observed_catalog": catalog,
                "provider_id": measured["provider_id"],
                "model_id": measured["model_id"],
                "account_mode": ACCOUNT_MODE,
                # Derived only after issuer-bound route/entitlement/policy/connection checks.
                "included_oauth_verified": True,
                "observed_harness_version": measured["harness_version"],
                "observed_harness_schema_sha256": measured["schema_sha256"],
                "observed_build_sha256": measured["build_sha256"],
                "observed_mcp_sdk_version": measured["mcp_sdk_version"],
                "principal_status": public["principal_status"],
            }
            verify_binding(
                self.binding, **observations, now=now, expected_binding_sha256=self.binding.digest
            )
            return {**observations, "observed_harness_instance_id": claim["instance_id"]}
        except Exception:
            raise ObservationBlocked("trusted_guard_admission_invalid_or_unavailable") from None
