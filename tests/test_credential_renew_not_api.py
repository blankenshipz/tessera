"""README credential-expiry guarantee (ADR 0103), pinned end to end.

1. No HTTP route, MCP tool or agent-facing surface can rotate, renew, create or replace a Salad
   management credential, or return its value. The route table and the MCP tool registry are
   walked programmatically, the source is scanned for every caller of the private store's mutating
   methods, and the health answers are checked to carry metadata only, with a seeded secret marker
   absent from every response, event, audit row, log record and captured output.
2. A stage needing an expired or revoked credential is refused up front with a recorded reason, and
   no execution grant is issued for it (a healthy credential is the positive control).
3. ``renew`` is capped at 24 hours per call, by the enrollment tool and by the store itself, and the
   cap is measured from now, so repeated renewals never push expiry past 24 hours ahead.

Synthetic markers only; no network, no provider call.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_campaign_credential_admission import enroll as admission_store
from test_campaign_credential_admission import revoke as admission_revoke
from test_credential_health import (
    REFERENCE,
    SECRET,
    reset_salad,  # noqa: F401 - autouse fixture
    salad_profile,
    store,
)
from test_provider_registration_api import World

from tessera_control.campaign_provider_salad import register_provider_instance
from tessera_control.salad_secret_files import PrivateFileSecretResolver
from tessera_operator import salad_enroll

SRC = Path(__file__).resolve().parents[1] / "src"
DAY_SECONDS = 24 * 3600

# Every route whose path says "renew". Each renews something other than a provider management
# credential; adding one here is a deliberate review that it cannot reach the private store.
KNOWN_RENEW_ROUTES = {
    "/credentials/{credential_id}/renew",  # a Tessera-issued worker bearer credential (database)
    "/people/{principal_id}/renew",  # a person's enrollment
    "/worker/leases/renew",  # a worker's execution lease
    "/credential-access-leases/{lease_id}/renew",  # a metadata-only access lease record
}
KNOWN_RENEW_TOOLS = {"tessera_renew_person"}
# The credential health answer: these keys and nothing else (ADR 0103 §1).
HEALTH_KEYS = {
    "checked_at",
    "warn_seconds",
    "blocking_states",
    "worst_state",
    "warning_count",
    "expired_count",
    "items",
}
ITEM_KEYS = {
    "provider_id",
    "profile",
    "reference",
    "state",
    "seconds_remaining",
    "expires_at",
    "revoked",
    "version",
    "pinned_version",
    "reason",
}
# The only modules that may write the private credential store: the owner-run terminal tools.
STORE_WRITERS = {"tessera_operator.salad_enroll", "tessera_control.credential_reenroll"}
# ...and the only modules that may import those tools (the terminal CLI and each other).
WRITER_IMPORTERS = {"tessera_control.credential_reenroll", "tessera_control.cli"}
MUTATORS = {"enroll", "renew", "revoke", "enroll_version"}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _api_routes(routes, prefix=""):
    """(path, APIRoute) for every served route, through included routers of any depth."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            yield from _api_routes(inner.routes, prefix + (getattr(context, "prefix", "") or ""))
        elif isinstance(route, APIRoute):
            yield prefix + route.path, route


def _module_name(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        yield _module_name(path), ast.parse(path.read_text(), str(path))


def _imports(module: str, tree: ast.AST) -> set[str]:
    found: set[str] = set()
    package = module.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join([package] + ([base] if base else []))
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


# -- 1. no API or MCP surface ------------------------------------------------------------------


def test_no_http_route_can_rotate_renew_enroll_or_replace_a_provider_credential(world):
    routes = [route for _, route in _api_routes(world.app.routes)]
    paths = {path for path, _ in _api_routes(world.app.routes)}
    assert len(routes) > 100  # the real, fully composed control app
    assert set(world.app.openapi()["paths"]) <= paths  # the walk misses no served route
    forbidden = re.compile(
        r"rotat|reenrol|re-enrol|management-key|salad[^/]*/.*(enrol|renew|secret)"
        r"|credential-health/.+|provider-credential|/secrets?\b",
        re.I,
    )
    assert [p for p in paths if forbidden.search(p)] == []
    renew = {p for p in paths if "renew" in p}
    assert renew == KNOWN_RENEW_ROUTES, "review any new renew route against ADR 0103"
    (health,) = [r for p, r in _api_routes(world.app.routes) if p == "/credential-health"]
    assert health.methods == {"GET"}
    # No route is served by, or imports, a module that writes the private credential store.
    endpoint_modules = {r.endpoint.__module__ for r in routes}
    assert not endpoint_modules & (STORE_WRITERS | {"tessera_control.cli"})
    trees = dict(_modules())
    for name in endpoint_modules:
        if name in trees:
            assert not _imports(name, trees[name]) & STORE_WRITERS, name


def test_only_the_owner_terminal_tools_write_the_private_credential_store():
    """Every call that enrolls, renews or revokes on a ``PrivateFileSecretResolver`` (or calls the
    shared ``enroll_version``) lives in the terminal tools, and only the CLI imports those."""
    writers: dict[str, list[str]] = {}
    importers: set[str] = set()
    for module, tree in _modules():
        if _imports(module, tree) & STORE_WRITERS or any(
            isinstance(n, ast.ImportFrom)
            and n.module == "tessera_operator"
            and any(a.name == "salad_enroll" for a in n.names)
            for n in ast.walk(tree)
        ):
            importers.add(module)
        bound = {
            ast.unparse(target)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func).endswith("PrivateFileSecretResolver")
            for target in node.targets
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in MUTATORS:
                continue
            receiver = ast.unparse(func.value) if isinstance(func, ast.Attribute) else ""
            if name == "enroll_version" or receiver in bound or "resolver" in receiver.lower():
                writers.setdefault(module, []).append(ast.unparse(node)[:60])
    assert set(writers) - {"tessera_control.salad_secret_files"} == STORE_WRITERS, writers
    assert importers == WRITER_IMPORTERS
    # The writers and the CLI are not web or MCP surfaces.
    for module, tree in _modules():
        if module in STORE_WRITERS | {"tessera_control.cli"}:
            text = ast.unparse(tree)
            assert "APIRouter" not in text and "FastMCP" not in text, module


def _mcp_tools(tmp_path):
    pytest.importorskip("mcp")
    import httpx

    from tessera_mcp.client import ControlClient
    from tessera_mcp.config import Config
    from tessera_mcp.server import create_server

    reference = tmp_path / "token"
    reference.write_text("synthetic-agent-token")
    reference.chmod(0o600)
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"actions": []})

    client = ControlClient(
        Config("http://127.0.0.1:8765", reference, "proj1", "dest"), httpx.MockTransport(handler)
    )
    server = create_server(
        client,
        campaign_tools=True,
        provider_trust_tools=True,
        model_spend_tools=True,
        approval_tool=True,
        provider_registration_tools=True,
        review_tools=True,
        review_decision_tool=True,
        person_enrollment_tool=True,
        storage_retention_tool=True,
        model_spend_resolve_tool=True,
    )
    return {t.name: t for t in asyncio.run(server.list_tools())}, seen


def test_no_mcp_tool_can_rotate_renew_or_enter_a_provider_credential(tmp_path):
    tools, seen = _mcp_tools(tmp_path)
    assert len(tools) > 20  # every optional tool family is registered
    assert [n for n in tools if re.search(r"rotat|secret|management_key", n)] == []
    # A person's enrollment renewal is not a provider credential (it calls the people route).
    assert {n for n in tools if "renew" in n} == KNOWN_RENEW_TOOLS
    credential = {n for n in tools if "credential" in n}
    assert credential == {"tessera_credential_health", "tessera_credential_reenroll_command"}
    for name in credential:
        annotations = tools[name].annotations
        assert annotations.readOnlyHint is True and annotations.destructiveHint is False
        fields = set(tools[name].inputSchema.get("properties", {}))
        assert not fields & {"secret", "value", "token", "key", "credential", "expires_at"}, name
    assert seen == []  # listing the tools reads nothing


def test_the_mcp_client_has_no_credential_write_path():
    source = (SRC / "tessera_mcp" / "client.py").read_text()
    paths = set(
        re.findall(r"""["'](/[A-Za-z0-9_\-/{}]*credential[A-Za-z0-9_\-/{}]*)["']""", source)
    )
    assert paths <= {"/credential-health"}, paths


def _health_text(world, tmp_path, monkeypatch, caplog, capsys, *, expires_in):
    directory = store(tmp_path, expires_in=expires_in)
    register_provider_instance(salad_profile(world.env), str(directory))
    caplog.set_level(logging.DEBUG)
    answers = [
        world.client.get("/credential-health", headers=world.human()),
        world.client.get("/credential-health", headers=world.agent(["provider_trust:read"])),
        world.client.get("/operator/health", headers=world.root()),
    ]
    assert [a.status_code for a in answers] == [200, 200, 200]
    rows = world.env.query("SELECT * FROM events") + world.env.query(
        "SELECT * FROM operator_audit_events"
    )
    captured = capsys.readouterr()
    dumped = "\n".join(
        [a.text for a in answers]
        + [json.dumps([dict(r) for r in rows], default=str)]
        + [r.getMessage() for r in caplog.records]
        + [captured.out, captured.err]
    )
    return answers, dumped


@pytest.mark.parametrize("expires_in", [10 * DAY_SECONDS, 3600, -3600])
def test_health_answers_carry_metadata_only_and_never_the_seeded_secret(
    world, tmp_path, monkeypatch, caplog, capsys, expires_in
):
    answers, dumped = _health_text(
        world, tmp_path, monkeypatch, caplog, capsys, expires_in=expires_in
    )
    for answer in answers[:2]:
        body = answer.json()
        assert set(body) == HEALTH_KEYS
        (item,) = body["items"]
        assert set(item) <= ITEM_KEYS
        assert item["reference"] == REFERENCE and item["version"] == 1
        assert item["state"] in {"ok", "warning", "expired"}
    credentials = answers[2].json()["credentials"]
    for item in credentials["items"]:
        assert set(item) <= ITEM_KEYS
    marker = SECRET.decode()
    assert marker not in dumped
    assert marker[: len(marker) // 2] not in dumped  # not even a prefix


def test_the_health_read_never_resolves_the_secret(world, tmp_path, monkeypatch):
    """Only the metadata header is read: resolve (which returns the value) is never called."""
    directory = store(tmp_path, expires_in=-60)
    register_provider_instance(salad_profile(world.env), str(directory))

    def refuse(*args, **kwargs):
        raise AssertionError("the health route must not resolve a secret")

    monkeypatch.setattr(PrivateFileSecretResolver, "resolve", refuse)
    for name in ("enroll", "renew", "revoke"):
        monkeypatch.setattr(PrivateFileSecretResolver, name, refuse)
    assert world.client.get("/credential-health", headers=world.human()).status_code == 200
    assert world.client.get("/operator/health", headers=world.root()).status_code == 200


# -- 2. refused up front, with a recorded reason, and no grant -----------------------------------


def _grants(world):
    return world.row("SELECT COUNT(*) AS n FROM execution_grants")["n"]


def _stage_grants(world):
    return [
        r["grant_id"]
        for r in world.env.query(
            "SELECT grant_id FROM campaign_stage_attempts WHERE campaign_id=?", world.campaign_id
        )
        if r["grant_id"]
    ]


@pytest.mark.parametrize("state", ["revoked", "expired"])
def test_a_blocked_stage_is_failed_with_a_reason_and_no_grant_is_issued(tmp_path, state):
    world = SaladWorld(tmp_path)
    directory = admission_store(tmp_path)
    register_provider_instance(world.profiles, str(directory))
    world.start()  # admitted while the credential is usable
    before = _grants(world)
    if state == "revoked":
        admission_revoke(directory)
    else:  # the service now reads a store whose same reference and version has expired
        (tmp_path / "later").mkdir()
        expired = admission_store(tmp_path / "later", expires_at=101.0, clock=lambda: 100.0)
        register_provider_instance(world.profiles, str(expired))
    world.tick(3)
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert f"stage_failed:g1:credential_{state}" in campaign["stop_reason"]
    (blocked,) = world.events("campaign.stage_credential_blocked")
    assert blocked["state"] == state and blocked["reason"].startswith(f"credential_{state}:")
    assert _grants(world) == before and _stage_grants(world) == []
    assert campaign["held_cents"] == 0


def test_a_healthy_credential_does_reach_a_grant(tmp_path):
    """Positive control: the same world with a usable credential does issue the stage's grant."""
    world = SaladWorld(tmp_path)
    register_provider_instance(world.profiles, str(admission_store(tmp_path)))
    world.start()
    before = _grants(world)
    world.tick_until(
        lambda: world.row("SELECT 1 FROM campaign_salad_stages WHERE source_id IS NOT NULL")
    )
    world.assess_source()
    world.tick_until_phase("awaiting_lease")
    assert _stage_grants(world) != []
    assert _grants(world) > before
    assert world.events("campaign.stage_credential_blocked") == []


def test_submit_with_a_revoked_credential_leaves_no_campaign_and_no_grant(tmp_path):
    from tessera_control.store import Conflict

    world = SaladWorld(tmp_path)
    register_provider_instance(world.profiles, str(admission_store(tmp_path, revoke=True)))
    before = (_grants(world), world.row("SELECT COUNT(*) AS n FROM campaigns")["n"])
    with pytest.raises(Conflict, match=r"^credential_revoked: "):
        world.env.submit(spec=world.spec)
    assert (_grants(world), world.row("SELECT COUNT(*) AS n FROM campaigns")["n"]) == before


# -- 3. renew: at most 24 hours per call ---------------------------------------------------------


def _resolver(directory, now):
    return PrivateFileSecretResolver(directory, now=lambda: now)


@pytest.fixture
def private(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    from tessera_control.salad_secrets import SecretBuffer

    resolver = PrivateFileSecretResolver(directory)
    try:
        resolver.enroll(REFERENCE, 1, SecretBuffer(bytearray(SECRET)), expires_at=time.time() + 600)
    finally:
        resolver.close()
    return directory


def test_the_store_refuses_a_renewal_past_24_hours_whatever_the_caller(private):
    now = time.time()
    resolver = _resolver(private, now)
    try:
        with pytest.raises(ValueError, match="renewal rejected"):
            resolver.renew(REFERENCE, expected_version=1, expires_at=now + DAY_SECONDS + 1)
        assert resolver.read_metadata(REFERENCE)["version"] == 1  # unchanged
        renewed = resolver.renew(REFERENCE, expected_version=1, expires_at=now + DAY_SECONDS)
        assert renewed.version == 2 and renewed.expires_at == now + DAY_SECONDS
    finally:
        resolver.close()


def test_the_tool_accepts_24_hours_refuses_25_and_defaults_to_24(private, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)  # renew must never read a secret
    base = ["renew", "--credential-directory", str(private), "--reference", REFERENCE]
    assert salad_enroll.main([*base, "--expires-in-hours", "25"]) == 2
    assert "bad_expiry" in capsys.readouterr().err
    started = time.time()
    assert salad_enroll.main(base) == 0  # the default is the cap
    out = capsys.readouterr()
    printed = json.loads(out.out)
    assert started + DAY_SECONDS - 5 <= printed["expires_at"] <= time.time() + DAY_SECONDS
    assert SECRET.decode() not in out.out + out.err


def test_repeated_renewals_never_reach_past_24_hours_from_now(private, capsys):
    base = ["renew", "--credential-directory", str(private), "--reference", REFERENCE]
    for _ in range(3):
        assert salad_enroll.main([*base, "--expires-in-hours", "24"]) in {0, 2}
        capsys.readouterr()
    resolver = PrivateFileSecretResolver(private)
    try:
        assert resolver.read_metadata(REFERENCE)["expires_at"] <= time.time() + DAY_SECONDS
    finally:
        resolver.close()
