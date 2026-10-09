"""Fail-closed network proof for the closed fixture, not activation authority.

The independent reader must run on the Docker *daemon's* Linux host. A macOS
Docker client cannot stat LinuxKit /proc or Docker's sandbox namespace. Never
substitute the client's PID namespace, an inspect declaration, or workload logs.
No nsenter, capabilities, mounts, packages, or extra containers are needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .capability_effects import BackendBlocked


class NamespaceUnknown(BackendBlocked):
    """Only finite gate identifiers may reach the journal."""


def require(condition, gate):
    if not condition:
        raise NamespaceUnknown(gate)


def bounded_text(path):
    with Path(path).open() as stream:
        value = stream.read(16385)
    require(len(value) <= 16384, "namespace_read_bound")
    return value


def namespace(path):
    link = os.readlink(path)
    st = os.stat(path)
    require(bool(re.fullmatch(r"(?:net|mnt|pid):\[\d+\]", link)), "namespace_link_unknown")
    require(int(link.split("[")[1][:-1]) == st.st_ino, "namespace_inode_mismatch")
    return dict(link=link, inode=st.st_ino, device=st.st_dev)


def process_identity(pid):
    root = Path(f"/proc/{pid}")
    stat = bounded_text(root / "stat")
    return dict(
        start_ticks=int(stat.rsplit(")", 1)[1].split()[19]),
        namespaces={kind: namespace(root / "ns" / kind) for kind in ("net", "mnt", "pid")},
    )


class NativeNamespaceIdentity:
    def preflight(self):
        try:
            host = process_identity(1)
            own = process_identity("self")
            require(host["namespaces"] == own["namespaces"], "observer_not_on_daemon_host")
            require(
                Path("/var/run/docker/netns").is_dir(), "daemon_namespace_directory_unavailable"
            )
            return dict(state="available", source="daemon-host-procfs")
        except NamespaceUnknown:
            raise
        except Exception:
            raise NamespaceUnknown("daemon_host_namespace_observer_unavailable") from None

    def snapshot(self, value, peers):
        try:
            self.preflight()
            pid = value["State"]["Pid"]
            require(type(pid) is int and pid > 1, "workload_pid_unknown")
            require(pid in peers, "peer_inventory_unknown")
            identity = process_identity(pid)
            host = process_identity(1)
            for kind in ("net", "mnt", "pid"):
                require(
                    identity["namespaces"][kind] != host["namespaces"][kind],
                    "host_or_shared_namespace",
                )
            key = value["NetworkSettings"]["SandboxKey"]
            require(
                bool(re.fullmatch(r"/var/run/docker/netns/[a-f0-9]+", key)),
                "sandbox_key_unknown",
            )
            st = os.stat(key)
            require(
                (st.st_ino, st.st_dev)
                == tuple(identity["namespaces"]["net"][k] for k in ("inode", "device")),
                "sandbox_inode_mismatch",
            )
            # Bind host PID to the exact immutable container, not a recycled PID.
            cgroup = bounded_text(f"/proc/{pid}/cgroup")
            require(value["Id"] in cgroup, "container_process_binding_unknown")
            cmd = bounded_text(f"/proc/{pid}/cmdline").split("\0")[:-1]
            require(
                cmd == value["Config"]["Entrypoint"] + value["Config"]["Cmd"],
                "container_command_mismatch",
            )
            for peer in peers:
                require(type(peer) is int and peer > 1, "peer_pid_unknown")
                if peer != pid:
                    require(
                        process_identity(peer)["namespaces"]["net"]
                        != identity["namespaces"]["net"],
                        "host_or_shared_namespace",
                    )
            return dict(
                container_id=value["Id"],
                host_pid=pid,
                started_at=value["State"]["StartedAt"],
                host_namespaces=host["namespaces"],
                sandbox_inode=st.st_ino,
                command_sha256=hashlib.sha256("\0".join(cmd).encode()).hexdigest(),
                **identity,
            )
        except NamespaceUnknown:
            raise
        except Exception:
            raise NamespaceUnknown("independent_namespace_identity_unavailable") from None


# stdlib-only trusted exec, run as the SAME non-root user with zero capabilities.
# Rtnetlink sockets are created here, hence dump the exec process's netns, unlike
# sysfs whose network view belongs to its mount's namespace. No shell/ip/curl.
OBSERVER = r"""
import errno, hashlib, json, os, pathlib, socket, struct, sys
nonce, package = sys.argv[1:]
def ns(pid):
    result = {}
    for kind in ('net', 'mnt', 'pid'):
        path = '/proc/%s/ns/%s' % (pid, kind)
        st = os.stat(path)
        result[kind] = dict(link=os.readlink(path), inode=st.st_ino, device=st.st_dev)
    return result
def text(path):
    with open(path) as f:
        result = f.read(16385)
    assert len(result) <= 16384
    return result
def identity():
    return dict(namespaces=ns(1), self_namespaces=ns('self'),
        start_ticks=int(text('/proc/1/stat').rsplit(')', 1)[1].split()[19]),
        command_sha256=hashlib.sha256(text('/proc/1/cmdline').rstrip('\0').encode()).hexdigest())
def dump(kind, expected, size):
    rows = []
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as s:
        s.settimeout(0.2)
        s.bind((0, 0))
        seq = kind
        s.sendto(struct.pack('=IHHII', 16 + size, kind, 0x301, seq, 0) + bytes(size), (0, 0))
        total = 0
        for _ in range(16):
            data, ancillary, flags, sender = s.recvmsg(16384)
            assert sender[0] == 0 and not flags & socket.MSG_TRUNC
            total += len(data)
            assert total <= 65536
            offset = 0
            while offset < len(data):
                length, typ, fl, sequence, port = struct.unpack_from('=IHHII', data, offset)
                assert length >= 16 and offset + length <= len(data) and sequence == seq
                assert not fl & 0x10  # NLM_F_DUMP_INTR: never accept a partial dump
                body = data[offset+16:offset+length]
                if typ == 3:
                    assert not body or struct.unpack_from('=i', body)[0] == 0
                    return rows
                assert typ == expected
                attrs = {}
                pos = size
                while pos < len(body):
                    n, key = struct.unpack_from('=HH', body, pos)
                    assert n >= 4 and pos + n <= len(body) and key not in attrs
                    attrs[key] = body[pos+4:pos+n]
                    pos += (n + 3) & ~3
                if kind == 18:
                    family, _, hardware, index, flag, change = struct.unpack_from('=BBHiII', body)
                    rows.append(dict(index=index, name=attrs[3].rstrip(b'\0').decode(), flags=flag))
                elif kind == 22:
                    family, prefix, flag, scope, index = struct.unpack_from('=BBBBI', body)
                    assert family in (socket.AF_INET, socket.AF_INET6)
                    rows.append(dict(index=index, prefix=prefix,
                        address=socket.inet_ntop(family, attrs[1])))
                else:
                    family, dst, src, tos, table, proto, scope, typ, flag = (
                        struct.unpack_from('=BBBBBBBBI', body))
                    assert family in (socket.AF_INET, socket.AF_INET6)
                    rows.append(dict(family=family, prefix=dst, type=typ,
                        index=struct.unpack('=I', attrs[4])[0] if 4 in attrs else None,
                        destination=socket.inet_ntop(family, attrs[1]) if 1 in attrs else None,
                        gateway=5 in attrs or 9 in attrs or 18 in attrs))
                offset += (length + 3) & ~3
        raise ValueError('dump incomplete')
def probes(family, loopback, outside):
    token = bytes.fromhex(nonce)
    with socket.socket(family, socket.SOCK_STREAM) as server:
        server.settimeout(0.2)
        server.bind((loopback, 0))
        server.listen(1)
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(server.getsockname())
            with server.accept()[0] as accepted:
                accepted.settimeout(0.2)
                client.sendall(token)
                assert accepted.recv(33) == token
    # Bound but non-listening port belongs to us: no unrelated local endpoint.
    with socket.socket(family, socket.SOCK_STREAM) as closed:
        closed.bind((loopback, 0))
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            refused = client.connect_ex(closed.getsockname())
    # Documentation-only literals. No DNS, HTTP, proxy, metadata or public service.
    # Only invoked after exact loopback-only address/route validation by this probe.
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(0.2)
        unreachable = client.connect_ex((outside, 9))
    return dict(positive=True, refused=refused, unreachable=unreachable)
try:
    before = identity()
    links = dump(18, 16, 16)
    addresses = dump(22, 20, 8)
    routes = dump(26, 24, 12)
    loop_only = (len(links) == 1 and links[0]['name'] == 'lo'
        and all(a['index'] == links[0]['index'] and a['address'] in ('127.0.0.1', '::1')
            for a in addresses)
        and all(r['index'] == links[0]['index'] and not r['gateway']
            and r['destination'] in ('127.0.0.0', '127.0.0.1', '127.255.255.255', '::1')
            and r['prefix'] > 0 for r in routes))
    proxy = any('proxy' in key.lower() for key in os.environ)
    result = dict(nonce=nonce, package=package, before=before,
        observer_pid=os.getpid(), uid=os.getuid(), status=text('/proc/self/status'),
        interfaces=links, addresses=addresses, routes=routes, proxy=proxy,
        sysfs_interfaces=sorted(os.listdir('/sys/class/net')),
        sysfs_mounts=[line for line in text('/proc/self/mountinfo').splitlines()
            if ' - sysfs ' in line], probes={})
    if loop_only and not proxy:
        result['probes'] = dict(
            ipv4=probes(socket.AF_INET, '127.0.0.1', '192.0.2.1'),
            ipv6=probes(socket.AF_INET6, '::1', '2001:db8::1'))
    result['after'] = identity()
    print(json.dumps(result, sort_keys=True))
except Exception:
    print(json.dumps(dict(unknown='workload_namespace_observation_unavailable')))
"""


def verify_observation(raw, before, after, plan):
    """No truthy/partial report or workload 'ready' can substitute for this proof."""
    try:
        require(len(raw) <= 16384, "namespace_observation_bound")
        o = json.loads(raw)
        require("unknown" not in o, "workload_namespace_observation_unavailable")
        require(before == after, "namespace_identity_changed")
        require(
            o["nonce"] == plan.nonce and o["package"] == plan.digest, "namespace_wrong_operation"
        )
        require(o["before"] == o["after"], "namespace_identity_changed")
        for field in ("namespaces", "start_ticks", "command_sha256"):
            require(o["before"][field] == before[field], "namespace_identity_mismatch")
        ns = before["namespaces"]
        require(o["before"]["self_namespaces"] == ns, "observer_namespace_mismatch")
        for kind in ("net", "mnt", "pid"):
            n = ns[kind]
            require(n["link"] == f"{kind}:[{n['inode']}]", "namespace_inode_mismatch")
            require(n != before["host_namespaces"][kind], "host_or_shared_namespace")
        require(before["sandbox_inode"] == ns["net"]["inode"], "sandbox_inode_mismatch")
        require(o["observer_pid"] > 1 and o["uid"] == 65532, "observer_process_unknown")
        for line in ("CapEff:\t0000000000000000", "NoNewPrivs:\t1", "Seccomp:\t2"):
            require(line in o["status"].splitlines(), "observer_security_unknown")
        require(o["proxy"] is False, "proxy_bypass_unknown")
        links = o["interfaces"]
        require(len(links) == 1 and links[0]["name"] == "lo", "unexpected_interface")
        index = links[0]["index"]
        require(index > 0 and links[0]["flags"] & 8, "loopback_identity_unknown")
        require(
            sorted((a["index"], a["address"], a["prefix"]) for a in o["addresses"])
            == sorted([(index, "127.0.0.1", 8), (index, "::1", 128)]),
            "unexpected_address",
        )
        require(bool(o["routes"]), "route_observation_unknown")
        allowed = {("127.0.0.0", 8), ("127.0.0.1", 32), ("127.255.255.255", 32), ("::1", 128)}
        for route in o["routes"]:
            require(
                route["index"] == index
                and not route["gateway"]
                and route["type"] in (2, 3)
                and (route["destination"], route["prefix"]) in allowed,
                "unexpected_route",
            )
        # A differing sysfs view is diagnostic, not proof of leakage OR isolation.
        require(o["sysfs_interfaces"] == ["lo"], "ambiguous_sysfs_visibility")
        require(bool(o["sysfs_mounts"]), "sysfs_mount_identity_unknown")
        for family in ("ipv4", "ipv6"):
            require(
                o["probes"][family] == dict(positive=True, refused=111, unreachable=101),
                "connectivity_unknown_or_unexpected",
            )
        return dict(state="verified", identity=before, observation=o)
    except NamespaceUnknown:
        raise
    except Exception:
        raise NamespaceUnknown("namespace_observation_unavailable_or_malformed") from None
