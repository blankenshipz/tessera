"""Weak-class kernel fallback tunnel rule: offline, no Docker, no host reads."""

from __future__ import annotations

import builtins
import copy
import errno
import io
import json
import struct

import pytest
from test_local_docker_attestation import weak_observations, weak_verify
from test_local_docker_fixture import plan
from test_local_docker_namespace import observations, verify

from tessera_worker import local_docker_contract as contract
from tessera_worker import local_docker_namespace as net
from tessera_worker.capability_effects import BackendBlocked
from tessera_worker.local_docker_fixture import unknown_network

FALLBACKS = sorted(net.KERNEL_FALLBACK_TUNNELS)
WEAK = contract.PROFILE_ENGINE_ATTESTED


def test_reviewed_constant_is_the_finite_probed_set():
    assert net.KERNEL_FALLBACK_TUNNELS == {
        "tunl0",
        "gre0",
        "gretap0",
        "erspan0",
        "ip_vti0",
        "ip6_vti0",
        "sit0",
        "ip6tnl0",
        "ip6gre0",
    }


# ------------------------------------------------------------ observer / predicates


def probed(o):
    o["interfaces"] += [dict(index=i + 2, name=n, flags=128) for i, n in enumerate(FALLBACKS)]
    o["sysfs_interfaces"] = sorted(["lo", *FALLBACKS])
    o["sysfs_other"] = [dict(name="bonding_masters", kind="file")]
    return o


def weak_case(change=None):
    p, _v, _peers, snap, o = weak_observations()
    probed(o)
    if change:
        change(o)
    return p, snap, o


def test_probed_namespace_passes_weak_and_fails_strong():
    p, snap, o = weak_case()
    assert weak_verify(snap, o, p)["state"] == "workload_observed"
    sp, host, so = observations()
    verify(host, so, sp)  # sanity: strict lo-only still verifies
    probed(so)
    with pytest.raises(net.NamespaceUnknown, match="unexpected_interface"):
        verify(host, so, sp)


def test_strong_rule_is_default_and_explicit_rule_selects_weak():
    _, _, o = weak_case()
    with pytest.raises(net.NamespaceUnknown, match="unexpected_interface"):
        net.workload_predicates(o)
    net.workload_predicates(o, net.FALLBACK_TUNNELS)
    with pytest.raises(net.NamespaceUnknown, match="unknown_interface_rule"):
        net.workload_predicates(o, "anything")


def set_flags(name, flags):
    def change(o):
        next(link for link in o["interfaces"] if link["name"] == name)["flags"] = flags

    return change


def add(key, **row):
    def change(o):
        o[key].append(row)

    return change


def rename_fallback(o):
    o["interfaces"][3]["name"] = "eth0"
    o["sysfs_interfaces"] = sorted(["lo", "eth0", *(set(FALLBACKS) - {o["interfaces"][3]["name"]})])


def extra_link(name):
    def change(o):
        o["interfaces"].append(dict(index=40, name=name, flags=128))
        o["sysfs_interfaces"] = sorted([*o["sysfs_interfaces"], name])

    return change


def dup(o):
    o["interfaces"].append(dict(index=41, name="sit0", flags=128))


def missing_lo(o):
    o["interfaces"] = [link for link in o["interfaces"] if link["name"] != "lo"]
    o["sysfs_interfaces"] = sorted(FALLBACKS)
    o["addresses"], o["routes"] = [], []


def sysfs_extra(o):
    o["sysfs_interfaces"] = sorted([*o["sysfs_interfaces"], "eth0"])


def sysfs_short(o):
    o["sysfs_interfaces"] = ["lo"]


def other(*entries):
    def change(o):
        o["sysfs_other"] = [dict(name=n, kind=k) for n, k in entries]

    return change


def sysfs_fewer_symlinks(o):
    o["sysfs_interfaces"] = [n for n in o["sysfs_interfaces"] if n != "gre0"]


NEGATIVES = [
    (other(("bonding_masters", "dir")), "ambiguous_sysfs_visibility"),
    (other(("bonding_masters", "other")), "ambiguous_sysfs_visibility"),
    (other(("bonding_masters", "file"), ("evil", "file")), "ambiguous_sysfs_visibility"),
    (other(("bonding_masters", "file"), ("gre0", "file")), "ambiguous_sysfs_visibility"),
    (other(("lo", "dir")), "ambiguous_sysfs_visibility"),
    (sysfs_fewer_symlinks, "ambiguous_sysfs_visibility"),
    (extra_link("eth0"), "unexpected_interface"),
    (extra_link("tunl1"), "unexpected_interface"),
    (set_flags("gre0", 129), "fallback_interface_up"),
    (set_flags("sit0", 65), "fallback_interface_up"),
    (add("addresses", index=3, address="10.0.0.2", prefix=24), "fallback_interface_addressed"),
    (add("addresses", index=3, address="fe80::1", prefix=64), "fallback_interface_addressed"),
    (
        add("routes", index=4, family=2, destination="0.0.0.0", prefix=0, type=1, gateway=False),
        "fallback_interface_routed",
    ),
    (dup, "duplicate_interface"),
    (missing_lo, "loopback_missing"),
    (sysfs_extra, "ambiguous_sysfs_visibility"),
    (sysfs_short, "ambiguous_sysfs_visibility"),
]


@pytest.mark.parametrize(("change", "gate"), NEGATIVES)
def test_each_negative_fails_weak_closed_with_distinct_reason(change, gate):
    p, snap, o = weak_case(change)
    with pytest.raises(net.NamespaceUnknown) as caught:
        weak_verify(snap, o, p)
    assert str(caught.value) == gate


def test_failure_retains_bounded_sanitized_interface_names():
    hostile = "../../etc/" + "x" * 500
    p, snap, o = weak_case(extra_link(hostile))
    for i in range(40):
        o["interfaces"].append(dict(index=100 + i, name=f"n{i}", flags=0))
    with pytest.raises(net.NamespaceUnknown) as caught:
        weak_verify(snap, o, p)
    names = caught.value.interfaces
    assert len(names) <= net.MAX_DIAG_NAMES
    assert set(names) <= {"lo", "other", *FALLBACKS}
    event = unknown_network(caught.value)
    assert event["interfaces"] == names and event["gate"] == "unexpected_interface"
    assert hostile not in json.dumps(event)


def test_any_weak_gate_failure_retains_only_sanitized_names():
    p, snap, o = weak_case()
    o["proxy"] = True
    with pytest.raises(net.NamespaceUnknown) as caught:
        weak_verify(snap, o, p)
    event = unknown_network(caught.value)
    assert event["gate"] == "proxy_bypass_unknown"
    assert event["interfaces"] == ["lo", *FALLBACKS]
    # Strong-class and non-interface failures keep the historical event shape.
    assert unknown_network(net.NamespaceUnknown("x")) == dict(state="unknown", gate="x")


def test_fallback_device_needs_no_probe_change_for_strict_lo_only():
    _, snap, o = weak_case()
    # Weak observer gate admits exactly the same shapes as the predicate.
    assert "loop_only" in net.WEAK_OBSERVER and net.WEAK_OBSERVER != net.OBSERVER
    assert "ip6gre0" in net.WEAK_OBSERVER
    compile(net.WEAK_OBSERVER, "<weak-observer>", "exec")
    assert net.OBSERVER.count("len(links) == 1") == 1


# ------------------------------------------------------------ contract / digests


def test_strong_program_observer_and_digests_unchanged_and_weak_differs():
    s, w = plan(), plan(profile=WEAK)
    assert contract.program_for(s) is contract.PROGRAM
    assert contract.observer_for(s) is net.OBSERVER
    assert contract.program_for(w) is contract.WEAK_PROGRAM
    assert "== ['lo']" in contract.PROGRAM
    assert "== ['lo']" not in contract.WEAK_PROGRAM
    assert s.digest != w.digest
    assert w.program_digest != s.program_digest
    compile(contract.WEAK_PROGRAM, "<weak-program>", "exec")


# ------------------------------------------------------------ program execution


class FakeSocketModule:
    AF_INET, SOCK_DGRAM = 2, 2

    def __init__(self, links):
        self.links = links

    def if_nameindex(self):
        return [(link["index"], link["name"]) for link in self.links]

    def socket(self, *args):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def fileno(self):
        return 3


class FakeFcntl:
    def __init__(self, links):
        self.by_name = {link["name"]: link for link in links}

    def ioctl(self, fd, request, arg):
        link = self.by_name[arg[:16].rstrip(b"\0").decode()]
        if request == 0x8913:
            return arg[:16] + struct.pack("H", link["flags"]) + bytes(14)
        assert request == 0x8915
        if not link.get("v4"):
            raise OSError(errno.EADDRNOTAVAIL, "x")
        return arg


def tunnel_links(**changes):
    links = [dict(index=1, name="lo", flags=73, v4=True, v6=1, routes=0)]
    links += [
        dict(index=i + 2, name=n, flags=128, v4=False, v6=0, routes=0)
        for i, n in enumerate(FALLBACKS)
    ]
    for name, change in changes.items():
        next(link for link in links if link["name"] == name).update(change)
    return links


def run_program(links, *, extra_links=(), v6_extra=(), nameservers=("192.168.65.7",)):
    files = {
        "/proc/net/route": "Iface\tDestination\tGateway\tFlags\n"
        + "".join(
            f"{link['name']}\t00000000\t0\t1\n" for link in links for _ in range(link["routes"])
        ),
        "/proc/net/if_inet6": "".join(
            f"{'0' * 31}1 01 80 10 80 {link['name']}\n" for link in links for _ in range(link["v6"])
        )
        + "".join(f"{'0' * 31}1 01 80 10 80 {name}\n" for name in v6_extra),
        "/proc/net/ipv6_route": f"{'0' * 32} 80 {'0' * 32} 00 {'0' * 32} 00000000 00000001 "
        "00000000 00200200 lo\n",
        "/etc/resolv.conf": "".join(f"nameserver {n}\n" for n in nameservers)
        + "search secret.example\n",
    }

    def fake_open(path, *args, **kwargs):
        return io.StringIO(files[path])

    g = {
        "__builtins__": vars(builtins) | {"open": fake_open},
        "socket": FakeSocketModule([*links, *extra_links]),
        "errno": errno,
        "json": json,
    }
    helpers = contract.WEAK_NETWORK_HELPERS.replace("__FALLBACK__", repr(tuple(FALLBACKS)))
    exec(compile(helpers, "<helpers>", "exec"), g)
    g["fcntl"] = FakeFcntl([*links, *extra_links])
    check = "def run():\n    global check_id\n" + contract.WEAK_NETWORK_CHECK
    exec(compile(check, "<check>", "exec"), g)
    return g


def test_program_check_accepts_probed_shape():
    g = run_program(tunnel_links())
    g["run"]()
    assert g["check_id"] == "network"


@pytest.mark.parametrize(
    "changes",
    [
        dict(sit0=dict(flags=129)),
        dict(gre0=dict(v4=True)),
        dict(gre0=dict(v6=1)),
        dict(tunl0=dict(routes=1)),
    ],
)
def test_program_check_rejects_up_addressed_routed_fallbacks(changes):
    with pytest.raises(AssertionError):
        run_program(tunnel_links(**changes))["run"]()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda links: links.append(
            dict(index=50, name="eth0", flags=128, v4=False, v6=0, routes=0)
        ),
        lambda links: links.append(
            dict(index=50, name="sit0", flags=128, v4=False, v6=0, routes=0)
        ),
        lambda links: links.remove(links[0]),
    ],
)
def test_program_check_rejects_unknown_duplicate_or_missing_lo(mutate):
    links = tunnel_links()
    mutate(links)
    with pytest.raises(AssertionError):
        run_program(links)["run"]()


def test_program_check_rejects_rows_for_unknown_device():
    with pytest.raises(AssertionError):
        run_program(tunnel_links(), v6_extra=["ghost0"])["run"]()


def test_program_diagnostic_is_bounded_path_free_and_hides_nameservers():
    links = tunnel_links(sit0=dict(flags=129, routes=2), gre0=dict(v4=True))
    links.append(dict(index=77, name="../../etc/pass", flags=0, v4=False, v6=0, routes=0))
    g = run_program(links, nameservers=("192.168.65.7", "10.1.2.3"))
    diag = g["netdiag"]()
    raw = json.dumps(diag)
    assert len(raw) <= 1024
    assert diag["nameservers"] == 2
    assert "192.168" not in raw and "10.1" not in raw and "secret" not in raw
    assert "/" not in raw and "passwd" not in raw
    sit0 = next(i for i in diag["interfaces"] if i["name"] == "sit0")
    assert sit0["up"] is True and sit0["routes"] == 2
    assert next(i for i in diag["interfaces"] if i["name"] == "gre0")["v4"] == 1
    assert any(i["name"] == "other" for i in diag["interfaces"])
    assert contract.valid_network_diag(diag)


def test_program_diagnostic_of_many_links_is_clamped_under_1kib():
    links = tunnel_links()
    links += [
        dict(index=100 + i, name=f"v{i}", flags=0, v4=False, v6=0, routes=0) for i in range(60)
    ]
    diag = run_program(links)["netdiag"]()
    assert len(json.dumps(diag)) <= 1024 and diag["link_count"] == 70
    assert contract.valid_network_diag(diag)


def test_program_failure_emits_diag_only_through_weak_text():
    assert "network_diag" in contract.WEAK_PROGRAM
    assert "network_diag" not in contract.PROGRAM
    assert "netdiag" not in contract.PROGRAM


# ------------------------------------------------------------ log protocol


def diag_record(p, **changes):
    value = dict(
        interfaces=[dict(name="lo", index=1, up=True, v4=1, v6=1, routes=0)],
        link_count=1,
        nameservers=1,
    )
    record = dict(
        stage="limits_unknown",
        nonce=p.nonce,
        package=p.digest,
        check="network",
        failure="assertion",
        network_diag=value | changes,
    )
    return json.dumps(record).encode() + b"\n"


def test_weak_log_accepts_bounded_diag_only_on_network_check():
    p = plan(profile=WEAK)
    assert contract.log_records(diag_record(p), p)[0]["network_diag"]["link_count"] == 1
    other = json.loads(diag_record(p))
    other["check"] = "uid"
    with pytest.raises(BackendBlocked):
        contract.log_records(json.dumps(other).encode(), p)


def test_strong_log_rejects_diag():
    p = plan()
    with pytest.raises(BackendBlocked):
        contract.log_records(diag_record(p), p)


@pytest.mark.parametrize(
    "changes",
    [
        dict(nameservers=100),
        dict(nameservers="1"),
        dict(link_count=True),
        dict(extra="192.168.65.7"),
        dict(interfaces=[dict(name="/etc/passwd", index=1, up=True, v4=0, v6=0, routes=0)]),
        dict(interfaces=[dict(name="eth0", index=1, up=True, v4=0, v6=0, routes=0)]),
        dict(interfaces=[dict(name="lo", index=1, up=1, v4=0, v6=0, routes=0)]),
        dict(interfaces=[dict(name="lo", index=1, up=True, v4=0, v6=0, routes=0, ip="x")]),
        dict(interfaces=[dict(name="lo", index=1, up=True, v4=0, v6=0, routes=0)] * 17),
        dict(interfaces="lo"),
    ],
)
def test_weak_log_rejects_malformed_or_hostile_diag(changes):
    p = plan(profile=WEAK)
    with pytest.raises(BackendBlocked):
        contract.log_records(diag_record(p, **changes), p)


def test_weak_log_rejects_extra_record_keys_and_multiple_records():
    p = plan(profile=WEAK)
    value = json.loads(diag_record(p))
    value["path"] = "/etc"
    with pytest.raises(BackendBlocked):
        contract.log_records(json.dumps(value).encode(), p)
    with pytest.raises(BackendBlocked):
        contract.log_records(diag_record(p) * 2, p)


def test_v1_records_still_parse_without_diag():
    p = plan(profile=WEAK)
    rec = dict(
        stage="limits_unknown",
        nonce=p.nonce,
        package=p.digest,
        check="network",
        failure="assertion",
    )
    assert contract.log_records(json.dumps(rec).encode(), p) == [rec]
    assert copy.deepcopy(rec)


def test_bonding_masters_file_alone_is_accepted_and_absent_is_too():
    p, snap, o = weak_case()
    assert weak_verify(snap, o, p)["state"] == "workload_observed"
    o["sysfs_other"] = []
    assert weak_verify(snap, o, p)["state"] == "workload_observed"


def test_strong_still_fails_probed_shape_with_sysfs_other():
    sp, host, so = observations()
    probed(so)
    with pytest.raises(net.NamespaceUnknown):
        verify(host, so, sp)
    assert "sysfs_other" not in net.OBSERVER


def test_sysfs_other_diagnostic_is_sanitized_and_bounded():
    hostile = "../../etc/" + "x" * 300
    p, snap, o = weak_case(other(("bonding_masters", "file"), (hostile, "dir")))
    for i in range(30):
        o["sysfs_other"].append(dict(name=f"n{i}", kind="weird"))
    with pytest.raises(net.NamespaceUnknown) as caught:
        weak_verify(snap, o, p)
    event = unknown_network(caught.value)
    assert event["gate"] == "ambiguous_sysfs_visibility"
    assert len(event["sysfs_other"]) <= net.MAX_DIAG_NAMES
    assert {e["name"] for e in event["sysfs_other"]} <= {"bonding_masters", "other"}
    assert {e["kind"] for e in event["sysfs_other"]} <= {"file", "dir", "other"}
    assert hostile not in json.dumps(event)


def test_weak_observer_classifies_sysfs_by_lstat(tmp_path, monkeypatch):
    import os

    base = tmp_path / "net"
    base.mkdir()
    (base / "bonding_masters").write_text("")
    (base / "adir").mkdir()
    (base / "lo").symlink_to(tmp_path)
    (base / "sit0").symlink_to(tmp_path / "missing")  # dangling link is still a link
    src = net.WEAK_OBSERVER
    start = src.index("def sysfs():")
    helper = src[start : src.index("try:\n    before = identity()")]
    g = {"os": os}
    exec(compile(helper.replace("/sys/class/net", str(base)), "<sysfs>", "exec"), g)
    names, extra = g["sysfs"]()
    assert names == ["lo", "sit0"]
    assert sorted(extra, key=lambda e: e["name"]) == [
        dict(name="adir", kind="dir"),
        dict(name="bonding_masters", kind="file"),
    ]
    assert "sysfs_other=sysfs_other" in src and "os.listdir('/sys/class/net')" not in src
