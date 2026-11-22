"""Operator subnet pool for Docker networks Tessera creates (fake Docker client only)."""

from __future__ import annotations

import ipaddress
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tessera_worker.cpu import (
    CpuOperation,
    DockerCpuAdapter,
    NetworkCreateError,
    SubnetPoolExhausted,
)
from tessera_worker.docker_network_pool import DockerNetworkPool, linux_host_routes
from tessera_worker.grant_poller import (
    DevelopmentGrantPoller,
    GrantPollerConfig,
    load_poller_config,
)
from tessera_worker.service import DevelopmentWorkerError
from tessera_worker.supervisor import CpuSupervisor, Lease

IMAGE = "python@sha256:" + "a" * 64
PROXY_IMAGE = "python@sha256:" + "d" * 64
OP = "b" * 32
NOW = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = {
    "profile": "allowlist-proxy-v1",
    "destinations": [{"host": "api.example.org", "port": 443}],
    "max_egress_bytes": 1000,
}
EXHAUSTED = "Error response from daemon: all predefined address pools have been fully subnetted"


def egress_op() -> CpuOperation:
    return CpuOperation(
        OP,
        IMAGE,
        ("python", "-c", "print(2)"),
        "c" * 64,
        10,
        network_profile="allowlist-proxy-v1",
        egress=POLICY,
    )


def no_routes() -> list[ipaddress.IPv4Network]:
    return []


class FakeDocker:
    """Just enough of the docker CLI: network ls/inspect/create/rm, ps, create, start."""

    def __init__(self, *, default_pool_exhausted: bool = True) -> None:
        self.calls: list[list[str]] = []
        # name -> {"id", "subnet", "labels", "created"}
        self.networks: dict[str, dict] = {}
        self.containers: dict[str, str] = {}  # container name -> network name
        self.default_pool_exhausted = default_pool_exhausted
        self.fail_create: str | None = None
        self.race_subnets: list[str] = []  # taken by "someone else" at create time

    def add_network(self, name, subnet, labels=None, created=NOW - timedelta(days=1)):
        self.networks[name] = {
            "id": f"id-{name}",
            "subnet": subnet,
            "labels": labels or {},
            "created": created,
        }

    def _find(self, ref):
        for name, net in self.networks.items():
            if ref in (name, net["id"]):
                return name, net
        return None, None

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        a = list(argv)
        self.calls.append(a)

        def ok(out=""):
            return subprocess.CompletedProcess(a, 0, out, "")

        def fail(err):
            return subprocess.CompletedProcess(a, 1, "", err)

        if a[:2] == ["network", "ls"]:
            if "--filter" in a:
                key = a[a.index("--filter") + 1].removeprefix("label=")
                return ok("\n".join(n["id"] for n in self.networks.values() if key in n["labels"]))
            return ok("\n".join(n["id"] for n in self.networks.values()))
        if a[:2] == ["network", "inspect"]:
            refs = a[a.index("--format") + 2 :] if "--format" in a else a[2:]
            fmt = a[a.index("--format") + 1] if "--format" in a else ""
            out = []
            for ref in refs:
                name, net = self._find(ref)
                if net is None:
                    return fail(f"Error: No such network: {ref}")
                if "Created" in fmt:
                    label = net["labels"].get("tessera.operation", "")
                    out.append(f"{label} {net['created'].strftime('%Y-%m-%dT%H:%M:%SZ')}")
                else:
                    out.append(net["subnet"])
            if "--format" not in a:
                return ok("[]")
            return ok("\n".join(out))
        if a[:2] == ["network", "create"]:
            name = a[-1]
            if self.fail_create:
                return fail(self.fail_create)
            if name in self.networks:
                return fail(f"network with name {name} already exists")
            if "--subnet" not in a:
                if self.default_pool_exhausted:
                    return fail(EXHAUSTED)
                subnet = "172.31.0.0/16"
            else:
                subnet = a[a.index("--subnet") + 1]
                wanted = ipaddress.ip_network(subnet)
                taken = [n["subnet"] for n in self.networks.values()] + self.race_subnets
                if any(wanted.overlaps(ipaddress.ip_network(t)) for t in taken):
                    return fail(
                        "Error response from daemon: invalid pool request: "
                        "Pool overlaps with other one on this address space"
                    )
            labels = {}
            for i, token in enumerate(a):
                if token == "--label":
                    key, _, value = a[i + 1].partition("=")
                    labels[key] = value
            self.add_network(name, subnet, labels, created=NOW)
            return ok(name)
        if a[:2] == ["network", "rm"]:
            name, net = self._find(a[2])
            if net is None:
                return fail("No such network")
            del self.networks[name]
            return ok(name)
        if a[:1] == ["ps"]:
            ref = a[a.index("--filter") + 1].removeprefix("network=")
            name, _ = self._find(ref)
            return ok("\n".join(c for c, n in self.containers.items() if n == name))
        if a[:3] == ["inspect", "--type", "container"]:
            if a[-1] in self.containers:
                return ok("[]")
            return fail("No such object")
        if a[0] == "create" and "--name" in a:
            self.containers[a[a.index("--name") + 1]] = a[a.index("--network") + 1]
        return ok("ok")

    def creates(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["network", "create"]]


def pool(cidr="10.213.0.0/16", prefix=24) -> DockerNetworkPool:
    return DockerNetworkPool(cidr, prefix)


def adapter(tmp_path: Path, docker: FakeDocker, network_pool=None) -> DockerCpuAdapter:
    root = tmp_path / "root"
    root.mkdir(mode=0o700, exist_ok=True)
    return DockerCpuAdapter(
        frozenset({IMAGE}),
        docker,
        input_root=root,
        egress_proxy_image=PROXY_IMAGE,
        network_pool=network_pool,
    )


def test_allocation_skips_docker_networks_and_host_routes() -> None:
    docker = FakeDocker()
    p = pool()
    first = p.candidates(OP)[0]
    docker.add_network("other-software", str(first))
    second = p.candidates(OP)[1]
    routes = [ipaddress.ip_network(second)]
    chosen = p.create_network(docker, "net", OP, host_routes=lambda: routes)
    assert chosen not in (first, second)
    assert chosen.subnet_of(p.block)
    assert chosen.prefixlen == 24


def test_overlapping_larger_docker_network_blocks_candidates() -> None:
    docker = FakeDocker()
    docker.add_network("big", "10.213.0.0/17")  # overlaps the first half of the pool
    chosen = pool().create_network(docker, "net", OP, host_routes=no_routes)
    assert chosen.network_address >= ipaddress.ip_address("10.213.128.0")


def test_labels_subnet_and_gateway_are_explicit() -> None:
    docker = FakeDocker()
    chosen = pool().create_network(docker, "tessera-egress-x", OP, host_routes=no_routes)
    (create,) = docker.creates()
    assert create[create.index("--subnet") + 1] == str(chosen)
    assert create[create.index("--gateway") + 1] == str(chosen.network_address + 1)
    assert f"tessera.operation={OP}" in create
    assert "--internal" in create
    assert docker.networks["tessera-egress-x"]["labels"] == {"tessera.operation": OP}


def test_pool_exhaustion_is_a_clear_error() -> None:
    docker = FakeDocker()
    p = pool("10.213.0.0/28", 29)
    docker.add_network("a", "10.213.0.0/29")
    docker.add_network("b", "10.213.0.8/29")
    with pytest.raises(SubnetPoolExhausted, match=r"10\.213\.0\.0/28.*no free subnet"):
        p.create_network(docker, "net", OP, host_routes=no_routes)
    assert docker.creates() == []


def test_lost_race_retries_next_free_subnet() -> None:
    docker = FakeDocker()
    p = pool()
    taken = p.candidates(OP)[0]
    docker.race_subnets.append(str(taken))  # invisible to listing, refused at create
    chosen = p.create_network(docker, "net", OP, host_routes=no_routes)
    assert chosen != taken
    assert len(docker.creates()) == 2


def test_other_create_error_is_reported_with_docker_text() -> None:
    docker = FakeDocker()
    docker.fail_create = "Error response from daemon: permission denied"
    with pytest.raises(NetworkCreateError, match="permission denied") as caught:
        pool().create_network(docker, "net", OP, host_routes=no_routes)
    assert "permission denied" in caught.value.docker_error
    assert len(docker.creates()) == 1


def test_pool_validation() -> None:
    for cidr, prefix in [
        ("not-a-cidr", 24),
        ("8.8.0.0/16", 24),  # not a private block
        ("10.213.0.1/16", 24),  # host bits set
        ("10.213.0.0/16", 8),
        ("10.213.0.0/16", 30),
        ("10.0.0.0/8", 29),  # more than 65536 subnets
        ("10.213.0.0/16", True),
    ]:
        with pytest.raises(ValueError):
            DockerNetworkPool(cidr, prefix)


def test_unset_pool_keeps_legacy_network_create(tmp_path) -> None:
    docker = FakeDocker(default_pool_exhausted=False)
    adapter(tmp_path, docker).prepare(egress_op())
    (create,) = docker.creates()
    assert create == [
        "network",
        "create",
        "--internal",
        "--label",
        f"tessera.operation={OP}",
        f"tessera-egress-{OP}",
    ]
    assert not any(c[:2] == ["network", "ls"] for c in docker.calls)
    assert adapter(tmp_path, docker).prune_orphan_networks() == []


def test_adapter_with_pool_allocates_and_labels(tmp_path) -> None:
    docker = FakeDocker()
    a = adapter(tmp_path, docker, pool())
    # The default pools are exhausted, but the operator pool still works.
    a._create_network(egress_op(), f"tessera-egress-{OP}")
    (create,) = docker.creates()
    assert "--subnet" in create and "--gateway" in create
    assert f"tessera.operation={OP}" in create


def test_unset_pool_network_failure_carries_docker_error(tmp_path) -> None:
    docker = FakeDocker()  # default pools exhausted, no operator pool
    with pytest.raises(NetworkCreateError, match="fully subnetted"):
        adapter(tmp_path, docker).prepare(egress_op())
    assert not any(c[0] == "create" for c in docker.calls)  # no container was created


def test_network_create_failure_is_a_not_started_launch_failure(tmp_path) -> None:
    docker = FakeDocker()
    a = adapter(tmp_path, docker)
    lease = Lease("lease", OP, "c" * 64, 1, datetime.now(UTC) + timedelta(hours=1), 1, "project")
    with CpuSupervisor(tmp_path / "journal", a, lambda _: True) as supervisor:
        result = supervisor.drive_once(egress_op(), lease)
    # ADR 0046: a definite refusal before any container is a launch error on an uncertain
    # result (never claimed terminal); only the closed class is reported to control, and the
    # Docker text stays a local diagnostic.
    assert result.phase == "uncertain"
    assert result.reason == "prepare_unconfirmed"
    assert result.launch_error == "network_address_pool_exhausted"
    assert not result.observation.termination_confirmed
    assert "fully subnetted" in (result.detail or "")
    # Nothing ran: no container was created or started.
    assert not any(c[0] in {"create", "start"} for c in docker.calls)
    assert docker.containers == {}


def test_prune_removes_only_old_unattached_tessera_networks() -> None:
    docker = FakeDocker()
    old = NOW - timedelta(hours=2)
    docker.add_network("tessera-orphan", "10.213.1.0/24", {"tessera.operation": "o1"}, old)
    docker.add_network("tessera-in-use", "10.213.2.0/24", {"tessera.operation": "o2"}, old)
    docker.containers["tessera-op-o2"] = "tessera-in-use"
    docker.add_network("tessera-young", "10.213.3.0/24", {"tessera.operation": "o3"}, NOW)
    docker.add_network("other-software", "10.213.4.0/24", {"com.example.owner": "x"}, old)
    docker.add_network("unlabelled", "10.213.5.0/24", None, old)
    removed = pool().prune_orphans(docker, now=lambda: NOW + timedelta(minutes=1))
    assert removed == ["id-tessera-orphan"]
    assert set(docker.networks) == {
        "tessera-in-use",
        "tessera-young",
        "other-software",
        "unlabelled",
    }
    removals = [c for c in docker.calls if c[:2] == ["network", "rm"]]
    assert removals == [["network", "rm", "id-tessera-orphan"]]


def test_linux_host_routes_parse_and_ignore_default(tmp_path) -> None:
    table = tmp_path / "route"
    table.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\n"
        "br-x\t0001D50A\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
    )
    assert linux_host_routes(table) == [ipaddress.ip_network("10.213.1.0/24")]
    assert linux_host_routes(tmp_path / "missing") == []


def _poller_config(tmp_path, **changes) -> GrantPollerConfig:
    base = {
        "control_url": "http://127.0.0.1:1",
        "credential_file": tmp_path / "cred",
        "workspace_root": tmp_path / "ws",
        "development_output_root": tmp_path / "out",
        "allowed_images": (IMAGE,),
    }
    return GrantPollerConfig(**(base | changes))


def test_poller_config_validates_pool(tmp_path) -> None:
    assert _poller_config(tmp_path).docker_network_pool is None
    _poller_config(tmp_path, docker_network_pool="10.213.0.0/16", docker_network_prefix_length=26)
    with pytest.raises(DevelopmentWorkerError):
        _poller_config(tmp_path, docker_network_pool="8.8.8.0/24")
    with pytest.raises(DevelopmentWorkerError):
        _poller_config(
            tmp_path, docker_network_pool="10.213.0.0/16", docker_network_prefix_length=40
        )


def test_poller_config_file_accepts_pool_keys(tmp_path) -> None:
    import json
    import os

    path = tmp_path / "poll.json"
    path.write_text(
        json.dumps(
            {
                "schema": "tessera.worker.development-poll.v1",
                "control_url": "http://127.0.0.1:1",
                "credential_file": str(tmp_path / "cred"),
                "workspace_root": str(tmp_path / "ws"),
                "development_output_root": str(tmp_path / "out"),
                "allowed_images": [IMAGE],
                "docker_network_pool": "10.213.0.0/16",
                "docker_network_prefix_length": 25,
            }
        )
    )
    os.chmod(path, 0o600)
    config = load_poller_config(path)
    assert (config.docker_network_pool, config.docker_network_prefix_length) == (
        "10.213.0.0/16",
        25,
    )


class _Channel:
    def get(self, path):
        raise ConnectionError("offline")

    def post(self, *args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError


def test_poller_prunes_once_only_with_a_pool(tmp_path) -> None:
    (tmp_path / "cred").write_text("x")
    calls: list[DockerNetworkPool] = []

    def prune(network_pool):
        calls.append(network_pool)
        return ["id-1"]

    configured = DevelopmentGrantPoller(
        _poller_config(tmp_path, docker_network_pool="10.213.0.0/16"),
        channel=_Channel(),
        prune_networks=prune,
    )
    configured.step()
    configured.step()
    assert len(calls) == 1 and configured.pruned_networks == ["id-1"]

    calls.clear()
    unset = DevelopmentGrantPoller(
        _poller_config(tmp_path), channel=_Channel(), prune_networks=prune
    )
    unset.step()
    assert calls == []


def test_operator_pool_exhaustion_is_reported_as_the_closed_pool_class() -> None:
    from tessera_worker.cpu import CpuLaunchError

    error = SubnetPoolExhausted("no free subnet")
    assert isinstance(error, CpuLaunchError)
    assert error.launch_error == "network_address_pool_exhausted"
