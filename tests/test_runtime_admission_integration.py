"""Local opt-in integration; SYNTHETIC authority/credentials, zero native effects."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_research_consumer as legacy_fixtures
import test_runtime_admission as v2_fixtures

from tessera_agent.research_consumer import ResearchConsumer
from tessera_agent.research_consumer_store import ConsumerBlocked
from tessera_agent.research_factory import ProductionResearchFactory
from tessera_agent.research_service import RoleReferences
from tessera_mcp.harness_binding import BindingBlocked, HarnessBinding, verify_binding_v2
from tessera_mcp.harness_observer import EvidenceBackedHarnessObserver
from tessera_mcp.runtime_admission import (
    AdmissionDenied,
    LocalChecks,
    NativeTerminalPort,
    Pins,
    RuntimeAdmissionVerifier,
    public_digest,
)

synthetic_v2 = v2_fixtures.synthetic_v2
consumer_setup = legacy_fixtures.consumer_setup
output_setup = legacy_fixtures.output_setup
arguments = legacy_fixtures.arguments


def align(rig, constructor):
    binding = constructor["harness_binding"]
    manifest = json.loads(binding.manifest)
    capture = constructor["capture_binding"]
    actions = tuple(sorted(set(manifest["required_enrollment_scopes"]) | {"session", "dispatch"}))
    rig.state["actions"] = actions
    scope = rig.scope.model_copy(
        update=dict(
            principal=capture["actor_principal_id"],
            credential=manifest["subject_credential_id"],
            project=manifest["project_id"],
            destination=manifest["destination"],
            session_ref=hashlib.sha256(capture["session_id"].encode()).hexdigest(),
            actions_digest=public_digest(sorted(actions)),
            role=manifest["role"],
            context_ref=capture["context_id"],
            context_digest=capture["context_digest"],
        )
    )
    pins = Pins(
        **{
            **rig.pins.model_dump(),
            "instance": capture["harness_instance_id"],
            "binding": binding.digest,
            "build": manifest["installed_build_sha256"],
            "harness_schema": manifest["harness_schema_sha256"],
            "effective_config": manifest["config_sha256"],
            "loaded_skill": manifest["skill_sha256"],
            "typed_catalog": manifest["catalog_sha256"],
            "tool_mapping": public_digest(
                {
                    "permitted_tools": manifest["permitted_tools"],
                    "permission_actions": manifest["permission_actions"],
                }
            ),
        }
    )
    rig.state.update(installed=pins, current_scope=scope)
    financial = rig.state["operator_assertion"].model_copy(
        update=dict(project=scope.project, destination=scope.destination)
    )
    rig.state["operator_assertion"] = financial
    rig.receipt.update(
        scope=scope.model_dump(), pins=pins.model_dump(), financial=financial.model_dump()
    )
    rig.receipt["local"]["model"] = manifest["model"][7:]
    rig.state["native_local"] = LocalChecks(**rig.receipt["local"])
    # rig.make checks its original expected pins; construct callbacks first then swap closure.
    rig.state["installed"] = rig.pins
    prior = rig.make()
    rig.state["installed"] = pins
    verifier = RuntimeAdmissionVerifier(
        policy=rig.policy,
        scope=scope,
        pins=pins,
        model=manifest["model"][7:],
        ledger=rig.ledger,
        authorize=prior.authorize,
        authenticate_operator=prior.authenticate_operator,
        authenticate_evidence=prior.authenticate_evidence,
        current_local_checks=prior.current_local_checks,
        terminal_port=NativeTerminalPort(
            lambda: rig.state["installed"], prior.terminal_port.bind_verifier
        ),
        clock=lambda: rig.now,
    )
    counter = 100

    def read():
        nonlocal counter
        counter += 1
        rig.receipt["nonce"] = f"{counter:064x}"
        rig.receipt["ticket"] = f"{counter + 1000:064x}"
        expected = replace(
            rig.expected,
            nonce=rig.receipt["nonce"],
            ticket=rig.receipt["ticket"],
            request_ref=f"{counter + 2000:064x}",
        )
        rig.receipt["request_ref"] = expected.request_ref
        rig.state["native_expected"] = expected
        return json.dumps(rig.receipt).encode(), expected

    return EvidenceBackedHarnessObserver(binding=binding, verifier=verifier, read_preparation=read)


def test_factory_v2_default_fails_before_state_or_private_reads(tmp_path, monkeypatch):
    config = SimpleNamespace(
        planner=SimpleNamespace(admission_version="v2"),
        critic=SimpleNamespace(admission_version="v2"),
    )
    monkeypatch.setattr(
        "tessera_agent.research_factory.private_file",
        lambda _: pytest.fail("No private credential reads permitted"),
    )
    with pytest.raises(ConsumerBlocked, match="approved_v2_factory_ports_missing"):
        ProductionResearchFactory().construct(config, tmp_path / "uncreated")
    assert not (tmp_path / "uncreated").exists()


@pytest.mark.parametrize("version", ["v2", "v3"])
def test_consumer_never_implicitly_converts_legacy_observations(consumer_setup, version):
    constructor, calls, *_ = consumer_setup
    with pytest.raises(ConsumerBlocked):
        ResearchConsumer(**constructor, admission_version=version)
    assert calls == []


def test_v2_binding_observer_consumer_fence_preserves_default_native_denial(
    consumer_setup, synthetic_v2
):
    constructor, calls, *_ = consumer_setup
    observer = align(synthetic_v2, constructor)
    updated = {**constructor, "observation_reader": observer, "admission_version": "v2"}
    consumer = ResearchConsumer(**updated)
    assert synthetic_v2.state["bound"] == 1
    consumer.fence()
    with pytest.raises(AdmissionDenied, match="SYNTHETIC-native-no-dispatch"):
        consumer.tick()
    assert calls == [] and synthetic_v2.state["effects"] == 0
    # Restart retains v2 policy, but cannot downgrade the same durable consumer.
    restarted = ResearchConsumer(**updated)
    restarted.fence()
    with pytest.raises(ConsumerBlocked):
        ResearchConsumer(**constructor)


def test_mutated_generated_binding_denied_before_session_or_native_tick(
    consumer_setup, synthetic_v2
):
    constructor, calls, *_ = consumer_setup
    observer = align(synthetic_v2, constructor)
    binding = constructor["harness_binding"]
    altered = HarnessBinding(binding.config + b" ", binding.manifest, binding.skill)
    raw, expected = observer.read_preparation()
    with pytest.raises(BindingBlocked):
        verify_binding_v2(altered, verifier=observer.verifier, evidence=raw, expected=expected)
    assert calls == []


def test_v2_attested_observer_factory_selection_and_policy_pin(consumer_setup, synthetic_v2):
    constructor, calls, *_ = consumer_setup
    observer = align(synthetic_v2, constructor)
    ref = SimpleNamespace(
        admission_version="v2",
        admission_policy_ref=synthetic_v2.policy.admission_policy_ref,
        admission_policy_digest=synthetic_v2.policy.digest,
        guard=None,
    )
    config = SimpleNamespace(
        control_origin="https://control.example",
        coding_origins=[],
        planner=SimpleNamespace(**vars(ref), harness_origin="https://planner.example"),
        critic=SimpleNamespace(**vars(ref), harness_origin="https://critic.example"),
    )
    factory = ProductionResearchFactory(admission_observer=lambda *args: observer)
    got = factory.attested_observer(
        config, ref, constructor["harness_binding"], constructor["capture_binding"], []
    )
    assert got is observer
    ref.admission_policy_digest = "0" * 64
    with pytest.raises(ConsumerBlocked):
        factory.attested_observer(
            config, ref, constructor["harness_binding"], constructor["capture_binding"], []
        )
    assert calls == []


def test_role_unknown_or_mixed_versions_do_not_convert():
    # A sparse JSON boolean is not an adapter or authenticated principal.
    for data in (
        {"admission_version": "v3"},
        {"admission_version": "v2", "guard": {"authorized": True}},
    ):
        with pytest.raises(ValueError):
            RoleReferences.model_validate(data)


_NATIVE_PYTHON_PORT = r"""
# Test-only trusted issuer embedding. All credentials/guard inputs SYNTHETIC.
import hashlib,json,sys
from pathlib import Path
from datetime import datetime
from tessera_mcp.runtime_admission import (
    AdmissionDenied,AdmissionLedger,AdmissionPolicy,CurrentAuthority,CurrentLocalState,
    Evidence,Financial,LocalChecks,NativeTerminalPort,OperatorStatus,Pins,Quota,
    RuntimeAdmissionVerifier,Scope,TerminalExpectation)
trusted=json.loads(Path(sys.argv[1]).read_text())
operation=sys.argv[2]
entry=trusted['operations'][operation]
policy=AdmissionPolicy(**trusted['policy']); scope=Scope(**trusted['scope'])
pins=Pins(**trusted['pins']); now=datetime.fromisoformat(trusted['now'])
expected=TerminalExpectation(**entry['expected'])
ledger=AdmissionLedger(Path(sys.argv[1]).parent/'issuer-ledger')
def authority(action):
    return CurrentAuthority(scope,datetime.fromisoformat(trusted['expires']),action,
        entry['revoked'],True,True,tuple(trusted['actions']))
def operator(ref):
    return OperatorStatus(Financial(**trusted['financial']),'operator',entry['financial_revoked'])
def checks(action,context):
    return CurrentLocalState(LocalChecks(**trusted['local']),Quota(**trusted['quota']),now,expected)
try:
    verifier=RuntimeAdmissionVerifier(policy=policy,scope=scope,pins=pins,model=trusted['local']['model'],
        ledger=ledger,authorize=authority,authenticate_operator=operator,
        authenticate_evidence=lambda raw,context:hashlib.sha256(raw).hexdigest(),
        current_local_checks=checks,terminal_port=NativeTerminalPort(lambda:pins,lambda _:None),
        clock=lambda:now)
    result=verifier.verify_terminal(sys.stdin.buffer.read(),expected)
    print(json.dumps({'state':result['state'],'ticket':result['ticket']}))
except AdmissionDenied:
    print('SYNTHETIC-issuer-denied');sys.exit(23)
finally:
    ledger.close()
"""


_NATIVE_BUN_PORT = r"""
import {test,expect,mock,spyOn} from "bun:test";
import {Effect} from EFFECT;
import {Socket} from SOCKET;
import {makeTerminalRuntime,TerminalBoundary} from TERMINAL;
import {createHash} from "node:crypto";
const cfg=await Bun.file(CONFIG).json();
let realCalls=0;
const forbidden=spyOn(globalThis,"fetch").mockImplementation(()=>{
 realCalls++;throw Error("real dispatch forbidden")
});
class TransportError{_tag="Transport";constructor(x){Object.assign(this,x)}}
class AIError extends Error{constructor(x){super(x.reason.message);this.reason=x.reason}}
mock.module(SCHEMA,()=>({TransportError,AIError,AIErrorReason:{make:x=>x}}));
mock.module(HTTP,()=>({}));
const ws=await import(WEBSOCKET);
let fetches=0,constructors=0,sends=0,verifies=0,claims=0,active="first",stageWire;
const trace=[];
const canonical=x=>Array.isArray(x)?x.map(canonical):x&&typeof x==="object"?
 Object.fromEntries(Object.keys(x).sort().map(k=>[k,canonical(x[k])])):x;
const fingerprint=x=>createHash("sha256").update(JSON.stringify(canonical(x))).digest("hex");
const principal={SYNTHETIC:true};
const binding=Object.freeze(cfg.nativeBinding);
const body=JSON.stringify({model:"model",tools:[]});
const nativeFetch=async()=>{fetches++;trace.push("fetch");return new Response("{}")};
const ports={
 currentAuthority:()=>Effect.succeed({principal,binding,assertLiveAtInvocation:()=>{}}),
 assertNativeOAuthAccountRouteNoFallback:()=>Effect.void,
 assertFinalModelToolsAndOverrides:wire=>Effect.sync(()=>{
   active=wire.transport==="ws-frame"?"frame":"first";
   stageWire=wire;
 }),
 assertOperatorPolicyV2:input=>Effect.sync(()=>{
   trace.push("verify:"+active);verifies++;
   if(input.wire!==stageWire || input.operation_ref!==cfg.operations[active].expected.ticket)
    throw Error("native operation or wire identity changed");
   const call=Bun.spawnSync([PYTHON,PYTHONPORT,CONFIG,active],{
     stdin:Buffer.from(JSON.stringify({
      ...cfg.operations[active].receipt,final_fingerprint:fingerprint(stageWire)
     })),
     env:{PATH:"/usr/bin:/bin",PYTHONPATH:PROJECTSRC,HOME:TEMP},
   });
   if(call.exitCode!==0)throw Error("real Python verifier denied");
   const result=JSON.parse(new TextDecoder().decode(call.stdout));
   if(result.state!=="terminal-validated")throw Error("not terminal validation");
   const approvedFingerprint=fingerprint(stageWire), approvedOperation=input.operation_ref;
   return {assertLiveForInvocation:(wire,operation)=>{
    if(fingerprint(wire)!==approvedFingerprint || operation!==approvedOperation)
     throw Error("approved native continuation changed");
   }};
 }),
 checkNativeQuota:()=>Effect.succeed(cfg.nativeQuota),
 nextOperation:()=>Effect.succeed(cfg.operations[active].expected.ticket),
 claimCurrent:()=>Effect.sync(()=>{claims++;trace.push("claim:"+active)}),
 settle:()=>Effect.void,
};
class NativeSocket extends EventTarget{
 readyState=WebSocket.OPEN;bufferedAmount=0;
 send(frame){sends++;trace.push("send");
  queueMicrotask(()=>this.dispatchEvent(new MessageEvent("message",{data:"done"})))
 }
 close(){this.readyState=WebSocket.CLOSED}
}
test("actual native terminal -> actual Python pre-effect issuer",async()=>{
 const runtime=()=>makeTerminalRuntime(binding,principal,ports,nativeFetch,()=>cfg.clock);
 let denied=false;
 try{
  if(cfg.transport==="http"){
   const r=runtime();
   await r.fetch("https://chatgpt.com/backend-api/codex/responses",{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:cfg.late?'{"model":"foreign"}':body
   });
   if(cfg.replay){
    // Fresh runtime + newly spawned Python process: actual durable ledger rejects restart replay.
    await runtime().fetch("https://chatgpt.com/backend-api/codex/responses",{
     method:"POST",headers:{"Content-Type":"application/json"},body
    });
   }
  }else{
   await Effect.runPromise(Effect.scoped(Effect.gen(function*(){
    const connection=yield* ws.open({url:"wss://chatgpt.com/backend-api/codex/responses",headers:{}});
    if(cfg.transport==="frame")yield* connection.sendText(cfg.late?'{"model":"foreign"}':body);
   })).pipe(Effect.provideService(TerminalBoundary,runtime().boundary),
    Effect.provideService(Socket.WebSocketConstructor,(()=>{
     constructors++;trace.push("constructor");return new NativeSocket()
    }) )));
  }
 }catch{denied=true}
 expect(denied).toBe(cfg.denied);
 expect(fetches).toBe(cfg.expectedFetches);expect(constructors).toBe(cfg.expectedConstructors);
 expect(sends).toBe(cfg.expectedSends);expect(realCalls).toBe(0);
 for(const name of ["fetch","constructor","send"]){
  const i=trace.indexOf(name);if(i>=0){
   const operation=name==="send"?"frame":"first";
   expect(trace[i-1]).toBe("claim:"+operation);
   expect(trace.slice(0,i).includes("verify:"+operation)).toBe(true);
  }
 }
 console.log("TERMINAL_PYTHON_RESULT:"+JSON.stringify({fetches,constructors,sends,verifies,claims,denied,trace,realCalls}));
 forbidden.mockRestore();
});
"""


def _quiescent_terminal_source():
    """Parent supplies exact final file pins; never execute an active unpinned tree."""
    root = Path(
        os.environ.get(
            "TESSERA_OBSERVER_SOURCE",
            Path(__file__).parents[1] / ".tessera/worktrees/opencode-observability-v4",
        )
    )
    inventory_ref = os.environ.get("TESSERA_TERMINAL_SOURCE_INVENTORY")
    bun = os.environ.get("TESSERA_OBSERVER_BUN") or shutil.which("bun")
    if not inventory_ref or not bun:
        pytest.skip(
            "Requires parent quiescent terminal inventory and Bun; no live/default fallback"
        )
    inventory = json.loads(Path(inventory_ref).read_text())
    selected = [
        "packages/ai/src/route/terminal-boundary.ts",
        "packages/ai/src/route/transport/websocket.ts",
    ]
    assert all(
        name in inventory
        and hashlib.sha256((root / name).read_bytes()).hexdigest() == inventory[name]
        for name in selected
    ), "Quiescent native source pins must match before execution"
    effect = root / "packages/core/node_modules/effect/dist/index.js"
    socket = root / "packages/core/node_modules/effect/dist/unstable/socket/index.js"
    if not effect.is_file() or not socket.is_file():
        pytest.skip("Read-only matching Effect dependencies missing; no install")
    return root, bun, inventory, selected, effect, socket


@pytest.mark.parametrize(
    "transport,fault",
    [
        ("http", "ok"),
        ("handshake", "ok"),
        ("frame", "ok"),
        ("http", "replay"),
        ("http", "publisher"),
        ("http", "request"),
        ("http", "principal"),
        ("http", "context"),
        ("http", "policy"),
        ("http", "quota"),
        ("http", "role"),
        ("http", "project"),
        ("http", "destination"),
        ("http", "revoked"),
        ("http", "financial"),
        ("http", "late"),
        ("handshake", "publisher"),
        ("frame", "publisher"),
        ("frame", "revoked"),
        ("frame", "financial"),
        ("frame", "quota"),
        ("frame", "late"),
    ],
)
def test_actual_native_terminal_python_verifier_pre_effect(
    synthetic_v2, tmp_path, transport, fault
):
    """Actual code interoperability only; named SYNTHETIC private guard/issuer resources."""
    root, bun, inventory, selected, effect, socket = _quiescent_terminal_source()
    rig = synthetic_v2
    phases = {"http": "terminal-http", "handshake": "terminal-ws", "frame": "terminal-ws"}
    operations = {}
    for index, name in enumerate(("first", "frame"), 1):
        wire_transport = (
            "http" if transport == "http" else ("ws-frame" if name == "frame" else "ws-handshake")
        )
        body = json.dumps({"model": "model", "tools": []}, separators=(",", ":"))
        expected_wire = dict(
            transport=wire_transport,
            url=("https" if wire_transport == "http" else "wss")
            + "://chatgpt.com/backend-api/codex/responses",
            method={"http": "POST", "ws-handshake": "GET", "ws-frame": "SEND"}[wire_transport],
            headers={},
            body=None if wire_transport == "ws-handshake" else body,
        )
        if wire_transport == "http":
            expected_wire["headers"] = {"content-type": "application/json"}
        expected = replace(
            rig.expected,
            phase=phases[transport],
            nonce=f"{index + 10:064x}",
            ticket=f"{index + 20:064x}",
            request_ref=f"{index + 30:064x}",
            fingerprint=public_digest(expected_wire),
        )
        receipt = json.loads(encoded_for_native(rig))
        receipt.update(
            phase=expected.phase,
            nonce=expected.nonce,
            ticket=expected.ticket,
            request_ref=expected.request_ref,
            final_fingerprint=expected.fingerprint,
        )
        operations[name] = dict(
            expected=vars(expected), receipt=receipt, revoked=False, financial_revoked=False
        )
    target = operations["frame" if transport == "frame" else "first"]
    if fault in {"publisher", "principal", "context", "policy", "role", "project", "destination"}:
        key = {
            "publisher": "publisher_ref",
            "principal": "principal",
            "context": "context_ref",
            "policy": "policy_digest",
            "role": "role",
            "project": "project",
            "destination": "destination",
        }[fault]
        target["receipt"]["scope"][key] = "critic" if fault == "role" else "f" * 64
    elif fault == "request":
        target["receipt"]["request_ref"] = "f" * 64
    elif fault == "quota":
        # Fresh wrapper time cannot refresh the original cached native snapshot.
        target["receipt"]["quota"]["snapshot_checked_at"] = rig.stamp(-60)
    elif fault == "revoked":
        target["revoked"] = True
    elif fault == "financial":
        target["financial_revoked"] = True
    native_binding = {
        name: "a" * 64
        for name in (
            "publisher_ref",
            "instance_ref",
            "project_ref",
            "destination_ref",
            "model_ref",
            "request_nonce",
            "principal_ref",
            "session_ref",
            "grant_ref",
            "enrollment_ref",
            "root_ref",
            "policy_sha256",
            "build_sha256",
            "config_sha256",
            "skill_sha256",
            "catalog_sha256",
        )
    }
    native_binding.update(
        order_revision=4,
        grant_revision=2,
        enrollment_revision=1,
        expires_at=int(rig.now.timestamp() * 1000) + 60000,
    )
    native_binding.update(
        publisher_ref=rig.scope.publisher_ref,
        instance_ref=hashlib.sha256(rig.pins.instance.encode()).hexdigest(),
        principal_ref=hashlib.sha256(rig.scope.principal.encode()).hexdigest(),
        session_ref=rig.scope.session_ref,
        project_ref=hashlib.sha256(rig.scope.project.encode()).hexdigest(),
        destination_ref=hashlib.sha256(rig.scope.destination.encode()).hexdigest(),
        model_ref=hashlib.sha256(
            json.dumps(["openai", "model"], separators=(",", ":")).encode()
        ).hexdigest(),
        grant_ref=hashlib.sha256(rig.scope.grant.encode()).hexdigest(),
        enrollment_ref=hashlib.sha256(rig.scope.enrollment.encode()).hexdigest(),
        policy_sha256=rig.scope.policy_digest,
        build_sha256=rig.pins.build,
        config_sha256=rig.pins.effective_config,
        skill_sha256=rig.pins.loaded_skill,
        catalog_sha256=rig.pins.typed_catalog,
    )
    ok = fault in {"ok", "replay"}
    config = dict(
        policy=rig.policy.model_dump(),
        scope=rig.scope.model_dump(),
        pins=rig.pins.model_dump(),
        now=rig.now.isoformat(),
        expires=rig.stamp(60),
        actions=list(rig.state["actions"]),
        local=rig.state["native_local"].model_dump(),
        quota=rig.state["native_quota"].model_dump(),
        financial=rig.state["operator_assertion"].model_dump(),
        operations=operations,
        nativeBinding=native_binding,
        nativeQuota={
            name: int(datetime.fromisoformat(value).timestamp() * 1000)
            for name, value in rig.state["native_quota"].model_dump().items()
            if name != "outcome"
        },
        clock=int(rig.now.timestamp() * 1000),
        transport=transport,
        replay=fault == "replay",
        late=fault == "late",
        denied=fault != "ok",
        expectedFetches=int(transport == "http" and ok),
        expectedConstructors=int(transport == "frame" or (transport == "handshake" and ok)),
        expectedSends=int(transport == "frame" and fault == "ok"),
    )
    config["nativeQuota"].pop("outcome", None)
    cfg_path = tmp_path / "SYNTHETIC-issuer.json"
    cfg_path.write_text(json.dumps(config))
    python_port = tmp_path / "SYNTHETIC-issuer.py"
    python_port.write_text(_NATIVE_PYTHON_PORT)
    js = _NATIVE_BUN_PORT
    replacements = dict(
        EFFECT=str(effect),
        SOCKET=str(socket),
        TERMINAL=str(root / selected[0]),
        CONFIG=str(cfg_path),
        SCHEMA=str(root / "packages/ai/src/schema/index.js"),
        HTTP=str(root / "packages/ai/src/route/transport/http.js"),
        WEBSOCKET=str(root / selected[1]),
        PYTHON=sys.executable,
        PYTHONPORT=str(python_port),
        PROJECTSRC=str(Path(__file__).parents[1] / "src"),
        TEMP=str(tmp_path),
    )
    # Replace identifiers, never arbitrary source content or private guard code.
    js = re.sub(
        r"\b(" + "|".join(replacements) + r")\b",
        lambda match: json.dumps(replacements[match[0]]),
        js,
    )
    helper = tmp_path / "native-python.test.ts"
    helper.write_text(js)
    result = subprocess.run(
        [bun, "test", str(helper)],
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "BUN_RUNTIME_TRANSPILER_CACHE_PATH": str(tmp_path / "cache"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert all(
        hashlib.sha256((root / name).read_bytes()).hexdigest() == inventory[name]
        for name in selected
    ), "Native source changed during gate"
    (tmp_path / "native-stdout.txt").write_text(result.stdout)
    (tmp_path / "native-stderr.txt").write_text(result.stderr)
    (tmp_path / "native-exit.txt").write_text(str(result.returncode))
    assert result.returncode == 0, result.stderr
    assert "TERMINAL_PYTHON_RESULT:" in result.stdout
    with sqlite3.connect(tmp_path / "issuer-ledger/admission-v2.sqlite3") as db:
        states = [row[0] for row in db.execute("SELECT state FROM tickets")]
    assert len(states) == (2 if transport == "frame" else 1)
    assert states.count("validated") == (
        (2 if transport == "frame" else 1)
        if fault in {"ok", "replay"}
        else int(transport == "frame")
    )


def encoded_for_native(rig):
    return json.dumps(rig.receipt)
