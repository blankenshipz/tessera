"""Bounded read snapshots; final identity/CAS effects never rediscover a namespace."""

import json
import time
from copy import deepcopy
from urllib.parse import quote

from .agent_sandbox import AgentSandboxBackend, SandboxUnsafe

DISCOVERY_SECONDS = 10.0
WRITE_SECONDS = 2.0


class DiscoveryClient:
    def __init__(self, client):
        self.original = client
        self.cluster = client.cluster
        self.deadline = time.monotonic() + DISCOVERY_SECONDS
        self.reads = {}
        self.bytes = 0

    def request(self, method, path, body=None, **kwargs):
        remaining = self.deadline - time.monotonic()
        if method != "GET" or remaining <= 0 or len(self.reads) >= 64:
            raise SandboxUnsafe("discovery total deadline/request bound exceeded")
        try:
            value = self.original.request(method, path, body, timeout_seconds=remaining, **kwargs)
        except (OSError, ValueError, RuntimeError):
            raise SandboxUnsafe("discovery transport/evidence unavailable") from None
        if time.monotonic() >= self.deadline:
            raise SandboxUnsafe("discovery total deadline exceeded")
        self.bytes += len(json.dumps(value).encode())
        if self.bytes > 8 * 1024 * 1024:
            raise SandboxUnsafe("discovery snapshot exceeds byte bound")
        self.reads[path] = deepcopy(value)
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            base = path.split("?", 1)[0]
            for item in value["items"]:
                name = item.get("metadata", {}).get("name")
                if isinstance(name, str):
                    self.reads[base + "/" + quote(name, safe="")] = deepcopy(item)
        return value


class PreparedClient:
    def __init__(self, discovery, target):
        self.original, self.cluster = discovery.original, discovery.cluster
        self.reads, self.target = discovery.reads, target
        self.deadline = time.monotonic() + WRITE_SECONDS
        self.before_write = None
        self.created = set()

    def live(self, method, path, body=None, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxUnsafe("final effect total budget exceeded")
        value = self.original.request(method, path, body, timeout_seconds=remaining, **kwargs)
        if time.monotonic() >= self.deadline:
            raise SandboxUnsafe("final effect total budget exceeded")
        return value

    def request(self, method, path, body=None, **kwargs):
        if method == "GET":
            if any(path.startswith(prefix + "/") for prefix in self.created):
                return self.live(method, path, body, **kwargs)
            if path not in self.reads:
                raise SandboxUnsafe("unprepared read under authority transaction")
            return deepcopy(self.reads[path])
        # Short fresh namespace identities and exact-resource CAS only. Never run
        # policy/Pod/PVC pagination under the global control writer.
        for name, uid in (
            ("kube-system", self.target.cluster_uid),
            (self.target.namespace, self.target.namespace_uid),
        ):
            value = self.live("GET", "/api/v1/namespaces/" + name)
            if (
                not value
                or value.get("metadata", {}).get("uid") != uid
                or value.get("metadata", {}).get("deletionTimestamp")
            ):
                raise SandboxUnsafe("final namespace identity changed")
        if method in {"PUT", "DELETE"}:
            prior = self.reads.get(path)
            current = self.live("GET", path)
            if not prior or not current or current.get("metadata", {}).get("deletionTimestamp"):
                raise SandboxUnsafe("final resource identity unavailable")
            for field in ("uid", "namespace", "labels", "annotations", "ownerReferences"):
                if current["metadata"].get(field) != prior["metadata"].get(field):
                    raise SandboxUnsafe("final resource identity changed")
            if current.get("spec") != prior.get("spec"):
                raise SandboxUnsafe("final resource specification changed")
            body = deepcopy(body)
            if method == "PUT":
                body["metadata"]["resourceVersion"] = current["metadata"]["resourceVersion"]
            else:
                body["preconditions"]["resourceVersion"] = current["metadata"]["resourceVersion"]
        if self.before_write is not None:
            self.before_write()
        result = self.live(method, path, body, **kwargs)
        if method == "POST":
            self.created.add(path)
        elif method == "PUT":
            self.reads[path] = deepcopy(result)
        return result


def discover(backend):
    client = DiscoveryClient(backend.client)
    client.witness = None

    def verifier(target):
        if backend.verify_witness is None:
            raise SandboxUnsafe("independent witness verifier unavailable")
        client.witness = backend.verify_witness(target)
        return deepcopy(client.witness)

    return AgentSandboxBackend(client, verifier), client


def prepared(backend, client, target):
    return AgentSandboxBackend(PreparedClient(client, target), lambda _: deepcopy(client.witness))
