"""Synthetic observations only; default tests never contact Docker or the network."""

import ast
import copy
import importlib.util
import json
import socket
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_local_docker_fixture import CID, IMAGE_ID, execute, plan

from tessera_worker import local_docker_namespace as net
from tessera_worker.local_docker_engine import LocalDockerEngine


def ns(base):
    return {
        kind: dict(link=f"{kind}:[{base + n}]", inode=base + n, device=4)
        for n, kind in enumerate(("net", "mnt", "pid"))
    }


def observations():
    p = plan()
    host = dict(
        container_id=CID,
        host_pid=100,
        started_at="synthetic",
        start_ticks=1234,
        namespaces=ns(100),
        host_namespaces=ns(200),
        sandbox_inode=100,
        command_sha256="a" * 64,
    )
    identity = {k: host[k] for k in ("namespaces", "start_ticks", "command_sha256")}
    identity["self_namespaces"] = host["namespaces"]
    o = dict(
        nonce=p.nonce,
        package=p.digest,
        before=copy.deepcopy(identity),
        after=copy.deepcopy(identity),
        observer_pid=7,
        uid=65532,
        status="CapEff:\t0000000000000000\nNoNewPrivs:\t1\nSeccomp:\t2\n",
        interfaces=[dict(index=1, name="lo", flags=73)],
        addresses=[
            dict(index=1, address="127.0.0.1", prefix=8),
            dict(index=1, address="::1", prefix=128),
        ],
        routes=[dict(index=1, family=2, destination="127.0.0.0", prefix=8, type=2, gateway=False)],
        proxy=False,
        sysfs_interfaces=["lo"],
        sysfs_mounts=["synthetic - sysfs sysfs ro"],
        probes={f: dict(positive=True, refused=111, unreachable=101) for f in ("ipv4", "ipv6")},
    )
    return p, host, o


def verify(host, o, p):
    return net.verify_observation(json.dumps(o).encode(), host, host, p)


def test_complete_independently_bound_proof():
    p, host, o = observations()
    assert verify(host, o, p)["state"] == "verified"


@pytest.mark.parametrize(
    "field",
    [
        "nonce",
        "package",
        "before",
        "after",
        "interfaces",
        "addresses",
        "routes",
        "probes",
        "proxy",
        "sysfs_mounts",
    ],
)
def test_missing_observation_never_passes(field):
    p, host, o = observations()
    del o[field]
    with pytest.raises(net.NamespaceUnknown):
        verify(host, o, p)


@pytest.mark.parametrize(
    "change,gate",
    [
        ({"sysfs_interfaces": ["eth0", "lo"]}, "ambiguous_sysfs_visibility"),
        (
            {
                "interfaces": [
                    dict(index=1, name="lo", flags=73),
                    dict(index=2, name="eth0", flags=1),
                ]
            },
            "unexpected_interface",
        ),
        ({"proxy": True}, "proxy_bypass_unknown"),
        ({"observer_pid": 1}, "observer_process_unknown"),
        ({"uid": 0}, "observer_process_unknown"),
        ({"routes": []}, "route_observation_unknown"),
        ({"addresses": []}, "unexpected_address"),
        ({"sysfs_mounts": []}, "sysfs_mount_identity_unknown"),
        ({"status": ""}, "observer_security_unknown"),
        ({"nonce": "3" * 64}, "namespace_wrong_operation"),
    ],
)
def test_host_sysfs_false_positive_and_ambiguous_visibility_block(change, gate):
    p, host, o = observations()
    # sysfs lo alone cannot hide an extra actual namespace interface. Conversely,
    # extra sysfs names alone are not called actual network connectivity.
    o.update(change)
    with pytest.raises(net.NamespaceUnknown, match=gate):
        verify(host, o, p)


@pytest.mark.parametrize("kind", ["net", "mnt", "pid"])
def test_shared_host_namespace_blocked(kind):
    p, host, o = observations()
    host["host_namespaces"][kind] = host["namespaces"][kind]
    with pytest.raises(net.NamespaceUnknown, match="host_or_shared_namespace"):
        verify(host, o, p)


def test_wrong_inode_and_process_identity():
    p, host, o = observations()
    host["sandbox_inode"] += 1
    with pytest.raises(net.NamespaceUnknown, match="sandbox_inode_mismatch"):
        verify(host, o, p)
    p, host, o = observations()
    host["start_ticks"] += 1
    with pytest.raises(net.NamespaceUnknown, match="namespace_identity_mismatch"):
        verify(host, o, p)
    p, host, o = observations()
    o["before"]["self_namespaces"] = ns(999)
    o["after"] = copy.deepcopy(o["before"])
    with pytest.raises(net.NamespaceUnknown, match="observer_namespace_mismatch"):
        verify(host, o, p)


@pytest.mark.parametrize(
    "change",
    [dict(index=2), dict(gateway=True), dict(destination="0.0.0.0", prefix=0), dict(type=1)],
)
def test_route_blocks_before_release(change):
    p, host, o = observations()
    o["routes"][0].update(change)
    with pytest.raises(net.NamespaceUnknown, match="unexpected_route"):
        verify(host, o, p)


@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
@pytest.mark.parametrize(
    "change",
    [
        dict(positive=False),
        dict(unreachable=0),
        dict(unreachable=11),
        dict(unreachable=110),
        dict(refused=0),
    ],
)
def test_positive_negative_and_timeout_connectivity_unknown(family, change):
    p, host, o = observations()
    o["probes"][family].update(change)
    with pytest.raises(net.NamespaceUnknown, match="connectivity_unknown_or_unexpected"):
        verify(host, o, p)


@pytest.mark.parametrize("raw", [b"{}", b"not json", b"x" * 16385, b'{"unknown":"anything"}'])
def test_unavailable_unknown_or_malformed_block(raw):
    p, host, _ = observations()
    with pytest.raises(net.NamespaceUnknown):
        net.verify_observation(raw, host, host, p)


def test_changed_host_snapshot_blocked():
    p, host, o = observations()
    with pytest.raises(net.NamespaceUnknown, match="namespace_identity_changed"):
        net.verify_observation(json.dumps(o).encode(), host, host | {"host_pid": 101}, p)


def test_missing_native_host_proc_is_not_workload_self_attestation(monkeypatch):
    def unavailable(pid):
        raise FileNotFoundError("not a Linux daemon host")

    monkeypatch.setattr(net, "process_identity", unavailable)
    with pytest.raises(net.NamespaceUnknown, match="daemon_host_namespace_observer_unavailable"):
        net.NativeNamespaceIdentity().preflight()


@pytest.mark.parametrize("fault", ["wrong_inode", "shared_peer", "wrong_cmd", "wrong_cgroup"])
def test_native_identity_rejects_wrong_inode_shared_peer_and_recycled_pid(monkeypatch, fault):
    reader = net.NativeNamespaceIdentity()
    monkeypatch.setattr(reader, "preflight", lambda: {})
    monkeypatch.setattr(
        net,
        "process_identity",
        lambda pid: dict(start_ticks=12, namespaces=ns(200 if pid == 1 else 100)),
    )
    monkeypatch.setattr(
        net.os,
        "stat",
        lambda _: SimpleNamespace(st_ino=99 if fault == "wrong_inode" else 100, st_dev=4),
    )
    monkeypatch.setattr(
        net,
        "bounded_text",
        lambda path: (
            ("wrong" if fault == "wrong_cmd" else "python\0")
            if path.endswith("cmdline")
            else ("wrong" if fault == "wrong_cgroup" else CID)
        ),
    )
    value = dict(
        Id=CID,
        State=dict(Pid=100, StartedAt="synthetic"),
        Config=dict(Entrypoint=["python"], Cmd=[]),
        NetworkSettings=dict(SandboxKey="/var/run/docker/netns/abc"),
    )
    with pytest.raises(net.NamespaceUnknown):
        reader.snapshot(value, [100, 200] if fault == "shared_peer" else [100])


def test_no_create_on_preflight_unknown_and_no_release_on_observation_unknown(tmp_path):
    for fault in ("namespace_preflight", "network"):
        (tmp_path / fault).mkdir()
        report, engine, _ = execute(tmp_path / fault, fault=fault)
        assert report["state"] == "blocked" and report["activation"] == "blocked"
        assert report["records"]["network"]["observation"]["state"] == "unknown"
        assert not {"go", "cp", "finish"}.intersection(engine.calls)
        assert "custody" not in report["records"]
        assert engine.calls.count("create") == (0 if fault == "namespace_preflight" else 1)
        assert engine.calls.count("verify_network") == (0 if fault == "namespace_preflight" else 1)
        if fault == "network":
            assert report["termination_confirmed"] and report["cleanup_confirmed"]


def test_engine_runs_trusted_exec_bound_to_exact_container(monkeypatch):
    p, host, o = observations()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["container", "ls"]:
            return (CID + "\n").encode()
        if args[:2] == ["container", "inspect"]:
            return b"100\n"
        assert args == [
            "exec",
            CID,
            "/usr/local/bin/python",
            "-I",
            "-B",
            "-c",
            net.OBSERVER,
            p.nonce,
            p.digest,
        ]
        assert kwargs["limit"] == 16384
        return json.dumps(o).encode()

    reader = SimpleNamespace(snapshot=lambda value, peers: host)
    engine = LocalDockerEngine(run, identity=reader)
    monkeypatch.setattr(engine, "inspect", lambda *args: dict(State=dict(Running=True)))
    checked = []
    monkeypatch.setattr(engine, "verify_security", lambda value: checked.append(value))
    assert engine.verify_network(p, IMAGE_ID, CID)["state"] == "verified"
    assert len(checked) == 2 and sum(a[0] == "exec" for a in calls) == 1


def test_opt_in_harness_records_block_before_launch_and_cannot_retry(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "sandbox_ns_cli", Path(__file__).parents[1] / "scripts/check_sandbox_supervision.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DockerCLI", lambda *args: None)

    class Engine:
        def __init__(self, *args):
            pass

        def preflight(self, p):
            return IMAGE_ID

        def run(self, *args, **kwargs):
            return b"{}"

        def namespace_preflight(self):
            raise net.NamespaceUnknown("daemon_host_namespace_observer_unavailable")

    monkeypatch.setattr(module, "LocalDockerEngine", Engine)
    monkeypatch.setattr(
        module.GuardedFixtureAdapter, "launch", lambda *args: pytest.fail("must not launch")
    )
    root = tmp_path.resolve() / "evidence"
    result = module.run_once(root, plan().image)
    assert result["attempts"] == 0 and result["activation"] == "blocked"
    assert result["gate"] == "daemon_host_namespace_observer_unavailable"
    assert not (root / "run-intent.json").exists()
    with pytest.raises(FileExistsError):
        module.run_once(root, plan().image)


def observer_functions():
    # Execute the actual observer functions against fake kernel/socket replies,
    # never its top-level effectful code or a simplified duplicate parser.
    tree = ast.parse(net.OBSERVER)
    functions = ast.Module(
        body=[n for n in tree.body if isinstance(n, ast.FunctionDef)], type_ignores=[]
    )
    scope = dict(struct=struct, socket=socket)
    exec(compile(functions, "<observer-functions>", "exec"), scope)
    return scope


@pytest.mark.parametrize(
    "family,loopback,outside", [(2, "127.0.0.1", "192.0.2.1"), (10, "::1", "2001:db8::1")]
)
def test_actual_probes_use_owned_loopback_and_documentation_literals_only(
    family, loopback, outside
):
    scope = observer_functions()
    scope["nonce"] = plan().nonce
    calls = []

    class Sock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            assert value == 0.2

        def bind(self, target):
            assert target == (loopback, 0)

        def getsockname(self):
            return (loopback, 32123)

        def listen(self, backlog):
            assert backlog == 1

        def connect(self, target):
            assert target == (loopback, 32123)
            calls.append(target)

        def accept(self):
            return Sock(), (loopback, 32124)

        def sendall(self, token):
            assert token == bytes.fromhex(plan().nonce)

        def recv(self, bound):
            assert bound == 33
            return bytes.fromhex(plan().nonce)

        def connect_ex(self, target):
            assert target in ((loopback, 32123), (outside, 9))
            calls.append(target)
            return 111 if target[0] == loopback else 101

    scope["socket"] = SimpleNamespace(SOCK_STREAM=1, socket=lambda *args: Sock())
    assert scope["probes"](family, loopback, outside) == dict(
        positive=True, refused=111, unreachable=101
    )
    assert calls == [(loopback, 32123), (loopback, 32123), (outside, 9)]


@pytest.mark.parametrize(
    "fault", [None, "interrupted", "truncated", "sender", "sequence", "missing_done", "error"]
)
def test_actual_netlink_dump_complete_and_namespace_local(fault):
    scope = observer_functions()
    body = struct.pack("=BBHiII", 0, 0, 772, 1, 73, 0) + struct.pack("=HH", 7, 3) + b"lo\0\0"
    header = struct.pack(
        "=IHHII",
        16 + len(body),
        16,
        0x10 if fault == "interrupted" else 0,
        99 if fault == "sequence" else 18,
        0,
    )
    done = struct.pack("=IHHIIi", 20, 2 if fault == "error" else 3, 0, 18, 0, 0)

    class Sock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            assert value == 0.2

        def bind(self, address):
            assert address == (0, 0)

        def sendto(self, data, address):
            assert address == (0, 0) and struct.unpack_from("=H", data, 4)[0] == 18

        def recvmsg(self, bound):
            return (
                header + body + (b"" if fault == "missing_done" else done),
                [],
                32 if fault == "truncated" else 0,
                (1 if fault == "sender" else 0, 0),
            )

    # AF_NETLINK/NETLINK_ROUTE do not exist on macOS; these constants are Linux ABI.
    scope["socket"] = SimpleNamespace(
        AF_NETLINK=16, SOCK_RAW=3, NETLINK_ROUTE=0, MSG_TRUNC=32, socket=lambda *args: Sock()
    )
    if fault:
        with pytest.raises((AssertionError, ValueError)):
            scope["dump"](18, 16, 16)
    else:
        assert scope["dump"](18, 16, 16) == [dict(index=1, name="lo", flags=73)]
