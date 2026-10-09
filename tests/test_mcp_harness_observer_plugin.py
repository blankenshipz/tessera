"""Compile shipped plugin, execute documented mock SDK hooks; all evidence synthetic."""

import base64
import copy
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from test_mcp_harness_binding import arguments as binding_arguments
from test_mcp_harness_observer import setup as public_setup

from tessera_mcp.harness_binding import HarnessBinding, catalog_sha256, generate_binding
from tessera_mcp.harness_observer import (
    GuardTrust,
    ObservationBlocked,
    ObserverHttpEndpoint,
    V2AttestedHarnessObserver,
    V2PluginHarnessObserver,
    _digest,
)


@pytest.fixture(scope="module")
def compiled_plugin(tmp_path_factory):
    if not shutil.which("tsc") or not shutil.which("node"):
        pytest.skip("TypeScript compiler and Node required for compiled plugin fixtures")
    root = tmp_path_factory.mktemp("compiled-observer")
    plugin = Path("plugins/tessera-harness-observer")
    shutil.copy(plugin / "index.ts", root / "index.ts")
    (root / "package.json").write_text('{"type":"module"}')
    # Mock SDK declarations: compile executable shipped code, not an installed SDK claim.
    (root / "sdk.d.ts").write_text("""
declare module "@opencode/plugin" { export const Plugin: { define(value: any): any } }
declare module "@opencode/plugin/rpc" { export const Rpc: { define(value: any): any } }
declare module "node:crypto" { export function createHash(name: string): any }
declare const Buffer: { byteLength(value: string): number }
""")
    result = subprocess.run(
        [
            "tsc",
            "--target",
            "es2022",
            "--module",
            "nodenext",
            "--skipLibCheck",
            "--outDir",
            "dist",
            "index.ts",
            "sdk.d.ts",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    sdk = root / "node_modules/@opencode/plugin"
    sdk.mkdir(parents=True)
    (sdk / "package.json").write_text(
        json.dumps({"type": "module", "exports": {".": "./index.js", "./rpc": "./rpc.js"}})
    )
    (sdk / "index.js").write_text("export const Plugin = { define: value => value }")
    (sdk / "rpc.js").write_text("export const Rpc = { define: value => value }")
    (root / "run.mjs").write_text("""
import plugin from './dist/index.js';
const mode = process.argv[2];
const tools = [{id:'tessera_tessera_status',description:'Safe description',
 input:{type:'object',properties:{}}}];
const location = {directory:'/research'};
const model = {providerID:'openai',id:'model'};
let hook, observe, produce, definition, disposed=0;
const hooks={};
let active = {type:'credential',id:'public-account-id',label:'PRIVATE_LABEL',method:'oauth'};
if (mode==='key') active.method='key';
if (mode==='env') active={type:'env',name:'PRIVATE_ENV_NAME'};
if (mode==='unknown' || mode==='producer_unknown') active=undefined;
if (mode==='needs_auth') active.status={status:'needs_auth',
 message:'PRIVATE_STATUS',url:'PRIVATE_URL'};
const ctx = {
 app:{name:'OpenCode',version:'2.0.18',channel:'stable'},location,
 options:{dedicatedResearch:true,workspace:'/research',sessionID:'ses_fixture',agent:'tessera-planner',
 skillID:'tessera-governed-research',providerID:'openai',modelID:'model',integrationID:'openai',
 permittedTools:['tessera_tessera_status'],allowedTransportOrigins:['https://route.example.test']},
 agent:{get:async()=>({location,data:{id:'tessera-planner',permissions:
 [{action:'*',resource:'*',effect:'deny'}]}})},
 provider:{get:async()=>({location,data:{id:'openai',integrationID:'openai',package:'fixture-provider',
 get headers(){throw Error('PRIVATE_AUTH_HEADERS')},get settings(){throw Error('PRIVATE_SETTINGS')},
 get body(){throw Error('PRIVATE_BODY')}}})},
 tool:{list:async()=>tools},
 skill:{list:async()=>({location,data:[{id:'tessera-governed-research',name:'Skill',path:'/fixture/SKILL.md',content:'body'}]})},
 integration:{connection:{active:async()=>active,
 resolve:()=>{throw Error('credential resolution forbidden')}}},
 session:{get:async()=>({id:'ses_fixture',agent:'tessera-planner',model,location}),
 hook:async(name, fn)=>{hooks[name]=fn;if(name==='context')hook=fn;
 return {dispose:async()=>disposed++}}},
 rpc:{register:async(port,handlers)=>{definition=port;observe=handlers.observe;produce=handlers.produce;
 return {dispose:async()=>disposed++}}}
};
if(mode==='wrong_version')ctx.app.version='changed';
if(mode==='wrong_workspace')ctx.location={directory:'/coding'};
if(mode==='no_dedicated')ctx.options.dedicatedResearch=false;
if(mode==='producer_drift')ctx.agent.get=async()=>({location,data:{id:'coding-agent',permissions:[]}});
if(mode==='full_catalog'){
 tools.splice(0,tools.length,...JSON.parse(process.argv[3]));
 ctx.options.permittedTools=tools.map(tool=>tool.id);
}
try {
 const cleanup=await plugin.setup(ctx);
 if(mode==='standard_schema')tools[0].input={'~standard':{version:1}};
 if(mode==='extra_tool')tools.push({id:'tessera_owner',description:'unsafe',input:{type:'object'}});
 const event={sessionID:'ses_fixture',agent:'tessera-planner',model,
 tools:{tessera_tessera_status:{description:'Safe description',
 input:{type:'object',properties:{}}}}};
 if(mode==='full_catalog')event.tools=Object.fromEntries(tools.map(tool=>
 [tool.id,{description:tool.description,input:tool.input}]));
 // Getter traps prove no raw context/private text access.
 for (const key of ['system','messages','options'])Object.defineProperty(event,key,
 {get(){throw Error('PRIVATE_RAW_CONTEXT')}});
 if(mode==='extra_snapshot')event.tools.shell={description:'shell',input:{type:'object'}};
 if(mode!=='no_snapshot')await hook(event);
 if(mode.startsWith('producer')){
   const native={sessionID:'ses_fixture',agent:'tessera-planner',model,kind:'primary',
     baseURL:'https://route.example.test/private?SENSITIVE_QUERY',
     url:'wss://unapproved.example.test/private?SENSITIVE_QUERY'};
   Object.defineProperty(native,'headers',{get(){throw Error('PRIVATE_AUTH_HEADERS')}});
   native.request={url:mode==='producer_unsafe'?'https://user:password@bad.test/private':
     'https://route.example.test/private?SENSITIVE_QUERY',method:'POST',
     get headers(){throw Error('PRIVATE_AUTH_HEADERS')},get body(){throw Error('PRIVATE_BODY')}};
   await hooks['model.request'](native);
   await hooks['http.request'](native);
   await hooks['experimental.ws.handshake'](native);
   if(mode==='producer_later_hook')event.tools.shell={description:'later hook',
     input:{type:'object'}};
 }
 const request={protocol:'tessera-harness-observer-v1',nonce:'fresh-nonce',sessionID:'ses_fixture'};
 if(mode==='bad_request')request.account_admitted=true;
 if(mode==='wrong_session')request.sessionID='ses_coding';
 const output=await (mode.startsWith('producer')?produce:observe)(request,
 {signal:new AbortController().signal,
 error:(type,message,data)=>({error:{type,message,data}})});
 await cleanup();
 console.log(JSON.stringify({output,definition,disposed}));
}catch{console.log(JSON.stringify({setupBlocked:true}))}
""")
    return root


def run_plugin(root, mode, tools=None):
    command = ["node", "run.mjs", mode]
    if tools is not None:
        command.append(json.dumps(tools))
    result = subprocess.run(command, cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not any(
        marker in result.stdout
        for marker in (
            "PRIVATE_LABEL",
            "PRIVATE_STATUS",
            "PRIVATE_URL",
            "PRIVATE_ENV_NAME",
            "PRIVATE_RAW_CONTEXT",
        )
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "mode", ["oauth", "key", "env", "unknown", "needs_auth", "no_snapshot", "extra_snapshot"]
)
def test_compiled_projection_gaps_and_redaction(compiled_plugin, mode):
    result = run_plugin(compiled_plugin, mode)
    output = result["output"]
    assert result["disposed"] == 5
    assert output["registeredCatalog"][0]["name"] == "tessera_tessera_status"
    assert output["nonce"] == "fresh-nonce"
    assert output["selectedModel"] == {"providerID": "openai", "id": "model"}
    assert "active_included_oauth_route_and_no_paid_fallback_unavailable" in output["gaps"]
    assert "model_snapshot_final_hook_order_unproven" in output["gaps"]
    assert not any(
        key in output for key in ("admitted", "included_oauth_verified", "effective_config")
    )
    if mode in {"no_snapshot", "extra_snapshot"}:
        assert output["snapshot"] is None
    else:
        assert output["snapshot"]["catalog"] == output["registeredCatalog"]
    if mode != "oauth":
        if mode not in {"no_snapshot", "extra_snapshot"}:
            assert "active_oauth_connection_not_observed" in output["gaps"]
    # Validate registered RPC schema against actual compiled projection.
    import jsonschema

    jsonschema.validate(output, result["definition"]["methods"]["observe"]["output"])


@pytest.mark.parametrize(
    "mode",
    [
        "wrong_version",
        "wrong_workspace",
        "no_dedicated",
        "standard_schema",
        "extra_tool",
        "bad_request",
        "wrong_session",
    ],
)
def test_compiled_plugin_denials(compiled_plugin, mode):
    result = run_plugin(compiled_plugin, mode)
    assert result.get("setupBlocked") or result["output"]["error"]["type"] == "blocked"


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403])
def test_authenticated_endpoint_no_redirect_or_auth_fallback(status):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer fixture-server-only"
        return httpx.Response(status, headers={"location": "https://other.example.test/api/info"})

    endpoint = ObserverHttpEndpoint(
        "https://research.example.test",
        "Bearer fixture-server-only",
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises(ObservationBlocked):
            endpoint.call("GET", "/api/info")
        assert len(calls) == 1
        assert calls[0].url.host == "research.example.test"
    finally:
        endpoint.close()


@pytest.mark.parametrize("auth", ["", "Bearer ", "Basic ", "Bearer fixture\nunsafe", "ApiKey nope"])
def test_auth_missing_or_invalid_denied(auth):
    with pytest.raises(ObservationBlocked):
        ObserverHttpEndpoint("https://research.example.test", auth)


@pytest.mark.parametrize(
    "path",
    [
        "https://other.example.test/api/info",
        "//other.example.test/api/info",
        "/api/credential",
        "/api/config",
        "/api/session/ses_fixture/message",
    ],
)
def test_arbitrary_origin_private_routes_denied(path):
    endpoint = ObserverHttpEndpoint(
        "https://research.example.test",
        "Bearer fixture-server-only",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    try:
        with pytest.raises(ObservationBlocked):
            endpoint.call("GET", path)
    finally:
        endpoint.close()


@pytest.fixture
def protocol_fixture(compiled_plugin):
    # Compose synthetic public schemas with the actual compiled plugin projection.
    kwargs, harness_fixture, control_fixture = public_setup.__wrapped__()
    manifest = json.loads(kwargs["binding"].manifest)
    manifest["harness_version"] = "2.0.18"
    manifest["model"] = "openai/model"
    manifest["permission_actions"] = ["tessera_tessera_status"]
    kwargs["binding"] = HarnessBinding(b"{}", json.dumps(manifest).encode(), b"fixture")
    harness_fixture.responses["/api/info"]["version"] = "2.0.18"
    harness_fixture.responses["/api/session/ses_fixture"]["data"]["model"]["id"] = "model"
    skill_path = next(path for path in harness_fixture.responses if path.startswith("/api/skill"))
    harness_fixture.responses[skill_path]["data"][0]["content"] = "body"
    result = run_plugin(compiled_plugin, "oauth")["output"]
    result["workspace"] = "/srv/research"
    calls = []
    state = {"mutation": None}

    def handle(request):
        calls.append((request.method, str(request.url)))
        assert request.headers["authorization"] == "Bearer fixture-server-only"
        if request.method == "POST":
            body = json.loads(request.content)
            assert set(body) == {"input"}
            output = json.loads(json.dumps(result))
            if request.url.path.endswith("/produce"):
                output = copy.deepcopy(state["producer"])
                output["publicObservation"] = copy.deepcopy(result)
                output["publicObservation"]["nonce"] = body["input"]["nonce"]
            output["nonce"] = body["input"]["nonce"]
            if state["mutation"]:
                state["mutation"](output)
            return httpx.Response(200, json={"output": output})
        path = request.url.raw_path.decode()
        responses = (
            control_fixture.responses
            if request.url.host == "control.example.test"
            else harness_fixture.responses
        )
        data = copy.deepcopy(responses[path])
        if path == "/external-agent/status" and "control_actions" in state:
            data["actions"] = state["control_actions"]
        return httpx.Response(200, json=data)

    harness = ObserverHttpEndpoint(
        harness_fixture.origin, "Bearer fixture-server-only", transport=httpx.MockTransport(handle)
    )
    control = ObserverHttpEndpoint(
        control_fixture.origin, "Bearer fixture-server-only", transport=httpx.MockTransport(handle)
    )
    kwargs.update(harness_endpoint=harness, control_endpoint=control)
    yield kwargs, state, calls
    harness.close()
    control.close()


def test_compiled_rpc_python_observer_composition(protocol_fixture):
    kwargs, _, calls = protocol_fixture
    report = V2PluginHarnessObserver(**kwargs).inspect_public()
    assert report.measured["registered_harness_catalog"][0]["name"] == "tessera_tessera_status"
    assert report.measured["active_connection_metadata"]["method"] == "oauth"
    assert "harness_discovered_full_mcp_catalog_unavailable" not in report.gaps
    with pytest.raises(ObservationBlocked, match="active_included_oauth_route"):
        report.admission_fields()
    assert calls[-1][0] == "POST"
    assert "/api/rpc/tessera.harness.observer.v1/observe?" in calls[-1][1]


@pytest.mark.parametrize(
    "change",
    ["nonce", "workspace", "catalog", "skill", "model", "admitted", "gap", "token", "snapshot"],
)
def test_rpc_protocol_denials(protocol_fixture, change):
    kwargs, state, _ = protocol_fixture

    def mutate(output):
        if change in {"nonce", "workspace"}:
            output[change] = "changed"
        elif change == "catalog":
            output["registeredCatalog"][0]["name"] = "tessera_owner"
        elif change == "skill":
            output["registeredSkill"]["bodySha256"] = "changed"
        elif change == "model":
            output["selectedModel"]["id"] = "changed"
        elif change == "admitted":
            output["admitted"] = True
        elif change == "gap":
            output["gaps"] = []
        elif change == "token":
            output["activeConnection"]["token"] = "unsafe fixture marker"
        else:
            output["snapshot"]["sequence"] = 0

    state["mutation"] = mutate
    with pytest.raises(ObservationBlocked, match="plugin_observation_shape_or_pin_changed"):
        V2PluginHarnessObserver(**kwargs).inspect_public()


@pytest.fixture
def trusted_fixture(protocol_fixture, compiled_plugin):
    # Production classes/paths with entirely synthetic guard/API measurements.
    kwargs, protocol_state, _ = protocol_fixture
    old = json.loads(kwargs["binding"].manifest)
    args = binding_arguments.__wrapped__()
    catalog = args["catalog"]
    tools = [
        {
            "id": "tessera_" + row["name"],
            "description": row["description"],
            "input": row["inputSchema"],
        }
        for row in catalog
    ]
    projection = run_plugin(compiled_plugin, "full_catalog", tools)["output"]
    # Synthetic clock fixture: public SDK shape, not an installed snapshot timestamp.
    projection["snapshot"]["capturedAt"] = "2030-01-01T00:00:00Z"

    def project(output):
        for field in ("registeredCatalog", "snapshot"):
            output[field] = copy.deepcopy(projection[field])

    protocol_state["mutation"] = project
    args.update(
        workspace=old["workspace"],
        project_id=old["project_id"],
        subject_credential_id=old["subject_credential_id"],
        destination=old["destination"],
        model=old["model"],
        approved_models=(old["model"],),
        harness_version="2.0.18",
        harness_schema_sha256=old["harness_schema_sha256"],
        catalog=catalog,
        expected_catalog_sha256=catalog_sha256(catalog),
        permitted_tools=args["permitted_tools"],
    )
    kwargs["binding"] = generate_binding(**args)
    protocol_state["control_actions"] = json.loads(kwargs["binding"].manifest)[
        "required_enrollment_scopes"
    ]
    public = V2PluginHarnessObserver(**kwargs).inspect_public().measured
    policy = {
        "allowed_auth_methods": ["oauth"],
        "billing_routes": ["chatgpt-included"],
        "on_unknown": "deny",
        "on_exhausted": "stop",
    }
    trust = GuardTrust(
        "fixture-independent-guard",
        "human-approval-fixture",
        "3" * 64,
        "enrollment-fixture",
        "instance-fixture",
        "actor",
        "entitlement-fixture",
        "4" * 64,
        _digest(policy),
    )
    manifest = json.loads(kwargs["binding"].manifest)
    measured = {
        "effective_config": json.loads(kwargs["binding"].config),
        "selected_skill_base64": base64.b64encode(args["skill"]).decode(),
        "skill_body_sha256": public["registered_skill_body_sha256"],
        "catalog": catalog,
        "harness_version": manifest["harness_version"],
        "schema_sha256": manifest["harness_schema_sha256"],
        "build_sha256": manifest["installed_build_sha256"],
        "mcp_sdk_version": "1.26.0",
        "principal_id": "actor",
        "provider_id": "openai",
        "model_id": "model",
        "account": {
            "connection_id": "public-account-id",
            "method": "oauth",
            "provider_id": "openai",
            "model_id": "model",
            "route": "chatgpt-included",
            "entitlement_reference": trust.entitlement_reference,
            "entitlement_sha256": trust.entitlement_sha256,
            "policy_sha256": trust.policy_sha256,
            "policy": policy,
        },
        "hooks": {
            "chain": ["tessera-harness-observer", "tessera-runtime-admission-guard"],
            "snapshot_sha256": _digest(public["request_snapshot"]),
            "final_guard_id": "tessera-runtime-admission-guard",
            "final_guard_revision": 1,
            "registered_catalog_sha256": public["registered_harness_catalog_sha256"],
            "effective_config_sha256": _digest(json.loads(kwargs["binding"].config)),
        },
    }
    state = {"mutation": None, "calls": []}
    state["public_projection"] = projection
    state["measurements"] = measured
    origins = {
        "config": "guard-effective-registry-export",
        "catalog": "guard-harness-registry-export",
        "skill": "guard-loaded-skill-and-artifact-readback",
        "identity": "guard-supervised-process-and-installed-artifacts",
        "account": "guard-selected-route-entitlement-and-billing-policy",
        "hooks": "guard-final-request-snapshot-and-plugin-chain",
    }

    def handle(request):
        assert request.url.host == "guard.example.test"
        assert request.headers["authorization"] == "Bearer fixture-guard-only"
        assert request.url.path == "/tessera/runtime-guard/v1/observe"
        state["calls"].append(json.loads(request.content))
        value = copy.deepcopy(measured)
        claim = {
            **state["calls"][-1],
            "issuer": trust.issuer,
            "trust_reference": trust.trust_reference,
            "producer_sha256": trust.producer_sha256,
            "proof_id": "proof-fixture",
            "revision": 1,
            "state": "active",
            "issued_at": "2030-01-01T00:00:00Z",
            "expires_at": "2030-01-01T00:00:20Z",
            "measurements": value,
            "evidence": {},
        }
        if state["mutation"]:
            state["mutation"](claim)
        for key, origin in origins.items():
            groups = {
                "config": value["effective_config"],
                "catalog": value["catalog"],
                "skill": {
                    name: value[name] for name in ("selected_skill_base64", "skill_body_sha256")
                },
                "identity": {
                    name: value[name]
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
                "account": value["account"],
                "hooks": value["hooks"],
            }
            claim["evidence"][key] = {
                "origin": origin,
                "mode": "measured",
                "captured_at": "2030-01-01T00:00:00Z",
                "revision": 1,
                "value_sha256": _digest(groups[key]),
            }
        if state.get("evidence_mutation"):
            state["evidence_mutation"](claim["evidence"])
        return httpx.Response(200, json=claim)

    guard = ObserverHttpEndpoint(
        "https://guard.example.test",
        "Bearer fixture-guard-only",
        transport=httpx.MockTransport(handle),
    )
    yield V2AttestedHarnessObserver(guard_endpoint=guard, guard_trust=trust, **kwargs), state
    guard.close()


def test_production_guard_observer_valid_trusted_fixture(trusted_fixture):
    observer, state = trusted_fixture
    observations = observer()
    assert observations["observed_harness_instance_id"] == "instance-fixture"
    assert observations["included_oauth_verified"] is True
    assert observations["principal_status"]["id"] == "actor"
    assert state["calls"][0]["public_observation_sha256"]
    state["mutation"] = lambda claim: claim.update(state="revoked")
    with pytest.raises(ObservationBlocked):
        observer()
    assert state["calls"][0]["nonce"] != state["calls"][1]["nonce"]


@pytest.mark.parametrize(
    "change",
    [
        "issuer",
        "trust_reference",
        "producer_sha256",
        "nonce",
        "instance_id",
        "binding_sha256",
        "research_origin",
        "public_observation_sha256",
        "state",
        "expired",
        "future",
        "ttl",
        "extra",
        "account",
        "entitlement",
        "policy",
        "hook_order",
        "snapshot",
        "config",
        "build",
        "skill",
    ],
)
def test_trusted_guard_proof_denials(trusted_fixture, change):
    observer, state = trusted_fixture

    def mutate(claim):
        if change in {
            "issuer",
            "trust_reference",
            "producer_sha256",
            "nonce",
            "instance_id",
            "binding_sha256",
            "research_origin",
            "public_observation_sha256",
        }:
            claim[change] = "changed"
        elif change == "state":
            claim["state"] = "revoked"
        elif change == "expired":
            claim["expires_at"] = "2029-01-01T00:00:00Z"
        elif change == "future":
            claim["issued_at"] = "2030-01-01T00:00:01Z"
        elif change == "ttl":
            claim["expires_at"] = "2030-01-01T01:00:00Z"
        elif change == "extra":
            claim["account_admitted"] = True
        elif change == "account":
            claim["measurements"]["account"]["connection_id"] = "another-oauth-account"
        elif change == "entitlement":
            claim["measurements"]["account"]["entitlement_reference"] = "unknown-entitlement"
        elif change == "policy":
            claim["measurements"]["account"]["policy"]["on_unknown"] = "paid-fallback"
        elif change == "hook_order":
            claim["measurements"]["hooks"]["chain"].reverse()
        elif change == "snapshot":
            claim["measurements"]["hooks"]["snapshot_sha256"] = "0" * 64
        elif change == "config":
            claim["measurements"]["effective_config"]["model"] = "openai/paid-fallback"
        elif change == "build":
            claim["measurements"]["build_sha256"] = "0" * 64
        else:
            claim["measurements"]["selected_skill_base64"] = base64.b64encode(b"changed").decode()

    state["mutation"] = mutate
    with pytest.raises(ObservationBlocked, match="trusted_guard_admission_invalid_or_unavailable"):
        observer()


@pytest.mark.parametrize("change", ["human_asserted", "origin", "stale", "digest"])
def test_claim_authentication_not_truth_or_measurement(trusted_fixture, change):
    observer, state = trusted_fixture

    def mutate(evidence):
        item = evidence["account"]
        if change == "human_asserted":
            item["mode"] = "human_asserted"
        elif change == "origin":
            item["origin"] = "plugin-options-echo"
        elif change == "stale":
            item["captured_at"] = "2020-01-01T00:00:00Z"
        else:
            item["value_sha256"] = "0" * 64

    state["evidence_mutation"] = mutate
    with pytest.raises(ObservationBlocked):
        observer()


@pytest.mark.parametrize("captured", ["2029-12-31T23:59:00Z", "2030-01-01T00:00:01Z"])
def test_fresh_guard_cannot_refresh_stale_or_future_snapshot(trusted_fixture, captured):
    observer, state = trusted_fixture
    state["public_projection"]["snapshot"]["capturedAt"] = captured
    state["measurements"]["hooks"]["snapshot_sha256"] = _digest(
        state["public_projection"]["snapshot"]
    )
    with pytest.raises(ObservationBlocked):
        observer()


@pytest.mark.parametrize(
    "mode",
    ["producer", "producer_unsafe", "producer_unknown", "producer_later_hook", "producer_drift"],
)
def test_compiled_safe_partial_producer(compiled_plugin, mode):
    result = run_plugin(compiled_plugin, mode)
    output = result["output"]
    assert result["disposed"] == 5
    assert not any(
        marker in json.dumps(output)
        for marker in (
            "PRIVATE_AUTH_HEADERS",
            "PRIVATE_SETTINGS",
            "PRIVATE_BODY",
            "SENSITIVE_QUERY",
            "password",
        )
    )
    if mode == "producer_drift":
        assert output["error"]["type"] == "blocked"
        return
    assert output["complete"] is False
    assert output["protocol"] == "tessera-runtime-guard-partial-v1"
    assert output["agentPermissions"] == [{"action": "*", "resource": "*", "effect": "deny"}]
    assert "transport_hooks_not_final_dispatch" in output["missing"]
    assert "model_snapshot_final_hook_order_unproven" in output["missing"]
    assert all(
        item["origin"] == "https://route.example.test" for item in output["transportObservations"]
    )
    if mode == "producer_later_hook":
        assert len(output["publicObservation"]["snapshot"]["catalog"]) == 1
    if mode == "producer_unknown":
        assert "active_oauth_connection_not_observed" in output["missing"]
    import jsonschema

    jsonschema.validate(output, result["definition"]["methods"]["produce"]["output"])


def test_partial_producer_python_composition_never_admits(protocol_fixture, compiled_plugin):
    kwargs, state, _ = protocol_fixture
    value = run_plugin(compiled_plugin, "producer")["output"]
    value["issuedAt"] = "2030-01-01T00:00:00Z"
    value["expiresAt"] = "2030-01-01T00:00:30Z"
    for item in value["transportObservations"]:
        item["capturedAt"] = "2030-01-01T00:00:00Z"
    state["producer"] = value
    reader = V2PluginHarnessObserver(**kwargs)
    report = reader.produce_partial()
    assert report.measured["principal_status"]["id"] == "actor"
    assert report.measured["partial_guard_producer"]["complete"] is False
    with pytest.raises(ObservationBlocked):
        report.admission_fields()
    value["complete"] = True
    with pytest.raises(ObservationBlocked):
        reader.produce_partial()


def test_partial_claim_cannot_satisfy_complete_factory(trusted_fixture):
    observer, state = trusted_fixture
    state["mutation"] = lambda claim: claim.update(
        protocol="tessera-runtime-guard-partial-v1", complete=False
    )
    with pytest.raises(ObservationBlocked):
        observer()
