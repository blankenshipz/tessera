"""Operator-configured subnet pool for the Docker networks Tessera creates.

Docker's default address pools are finite. On a host where other software has created
many networks, ``docker network create`` fails with "all predefined address pools have
been fully subnetted". An operator can instead reserve a CIDR block for Tessera; each
network Tessera creates then gets an explicit ``--subnet``/``--gateway`` from it.

Allocation avoids every subnet used by an existing Docker network and every host route.
It is race-safe across concurrent runs and processes because the Docker daemon is the
arbiter: it refuses an overlapping subnet, and the allocator then re-reads the used set
and tries the next free candidate (bounded). Only networks labelled by Tessera are ever
removed by the orphan pruner; other software's networks are never touched.
"""

from __future__ import annotations

import ipaddress
import re
import sys
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

from .cpu import CpuError, NetworkCreateError, SubnetPoolExhausted

RunCommand = Callable[[Sequence[str]], CompletedProcess[str]]

OPERATION_LABEL = "tessera.operation"
DEFAULT_PREFIX_LENGTH = 24
MAX_PREFIX_LENGTH = 29  # A gateway, the proxy and an experiment container must fit.
MAX_POOL_SUBNETS = 65536
MAX_ALLOCATION_ATTEMPTS = 8
ORPHAN_GRACE = timedelta(minutes=10)
_ORPHAN_FORMAT = (
    f'{{{{index .Labels "{OPERATION_LABEL}"}}}} {{{{.Created.Format "2006-01-02T15:04:05Z07:00"}}}}'
)
_OVERLAP = ("pool overlaps", "overlaps with other one", "overlap")


def _bounded(text: str, limit: int = 400) -> str:
    return " ".join(text.split())[:limit]


@dataclass(frozen=True)
class DockerNetworkPool:
    """An IPv4 block carved into equally sized per-network subnets."""

    cidr: str
    prefix_length: int = DEFAULT_PREFIX_LENGTH

    def __post_init__(self) -> None:
        try:
            block = ipaddress.IPv4Network(self.cidr, strict=True)
        except (ValueError, TypeError) as error:
            raise ValueError("Docker network pool must be an IPv4 CIDR block.") from error
        if (
            type(self.prefix_length) is not int
            or not block.prefixlen <= self.prefix_length <= MAX_PREFIX_LENGTH
            or self.prefix_length - block.prefixlen > 16
        ):
            raise ValueError(
                "Docker network prefix length must lie between the pool's own prefix and "
                f"/{MAX_PREFIX_LENGTH}, with at most 65536 subnets."
            )
        if not block.is_private:
            raise ValueError("Docker network pool must be a private address block.")

    @property
    def block(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.cidr)

    def candidates(self, seed: str) -> list[ipaddress.IPv4Network]:
        """All subnets, rotated by a stable per-operation offset to spread contention."""
        subnets = list(self.block.subnets(new_prefix=self.prefix_length))
        offset = zlib.crc32(seed.encode()) % len(subnets)
        return subnets[offset:] + subnets[:offset]

    def free_subnet(
        self, seed: str, used: Sequence[ipaddress.IPv4Network]
    ) -> ipaddress.IPv4Network:
        for candidate in self.candidates(seed):
            if not any(candidate.overlaps(other) for other in used):
                return candidate
        raise SubnetPoolExhausted(
            f"Docker network pool {self.cidr} (/{self.prefix_length}) has no free subnet; "
            "remove unused Docker networks or enlarge the pool.",
        )

    @staticmethod
    def gateway(subnet: ipaddress.IPv4Network) -> str:
        return str(subnet.network_address + 1)

    def create_arguments(
        self, name: str, operation_id: str, subnet: ipaddress.IPv4Network
    ) -> list[str]:
        return [
            "network",
            "create",
            "--internal",
            "--label",
            f"{OPERATION_LABEL}={operation_id}",
            "--subnet",
            str(subnet),
            "--gateway",
            self.gateway(subnet),
            name,
        ]

    # --- discovery ------------------------------------------------------------------

    @staticmethod
    def docker_subnets(run: RunCommand) -> list[ipaddress.IPv4Network]:
        """IPv4 subnets of every Docker network; raises if they cannot be listed."""
        listing = run(["network", "ls", "--no-trunc", "--quiet"])
        if listing.returncode:
            raise NetworkCreateError(
                "Docker networks could not be listed.", _bounded(listing.stderr)
            )
        identifiers = listing.stdout.split()
        if not identifiers:
            return []
        reply = run(
            [
                "network",
                "inspect",
                "--format",
                "{{range .IPAM.Config}}{{println .Subnet}}{{end}}",
                *identifiers,
            ]
        )
        if reply.returncode:
            raise NetworkCreateError(
                "Docker networks could not be inspected.", _bounded(reply.stderr)
            )
        return _parse_networks(reply.stdout.split())

    def used_subnets(
        self, run: RunCommand, host_routes: Callable[[], list[ipaddress.IPv4Network]]
    ) -> list[ipaddress.IPv4Network]:
        return self.docker_subnets(run) + host_routes()

    # --- allocation -----------------------------------------------------------------

    def create_network(
        self,
        run: RunCommand,
        name: str,
        operation_id: str,
        *,
        host_routes: Callable[[], list[ipaddress.IPv4Network]] | None = None,
    ) -> ipaddress.IPv4Network:
        """Create the labelled internal network on a free pool subnet and return it.

        Raises ``NetworkCreateError`` carrying Docker's own message when creation fails;
        nothing but the network is ever created here.
        """
        routes = host_routes or linux_host_routes
        skipped: list[ipaddress.IPv4Network] = []
        last_error = ""
        for _ in range(MAX_ALLOCATION_ATTEMPTS):
            subnet = self.free_subnet(operation_id, self.used_subnets(run, routes) + skipped)
            reply = run(self.create_arguments(name, operation_id, subnet))
            if not reply.returncode:
                return subnet
            last_error = _bounded(reply.stderr or reply.stdout)
            lowered = last_error.lower()
            if "already exists" in lowered:
                return self._existing_subnet(run, name)
            if any(text in lowered for text in _OVERLAP):
                # Another run (or other software) took it first: never reuse it, re-read.
                skipped.append(subnet)
                continue
            raise NetworkCreateError(
                f"Docker refused to create network {name}: {last_error}", last_error
            )
        raise NetworkCreateError(
            f"Docker network create kept racing for a subnet in {self.cidr}: {last_error}",
            last_error,
        )

    @staticmethod
    def _existing_subnet(run: RunCommand, name: str) -> ipaddress.IPv4Network:
        reply = run(
            [
                "network",
                "inspect",
                "--format",
                "{{range .IPAM.Config}}{{println .Subnet}}{{end}}",
                name,
            ]
        )
        found = _parse_networks(reply.stdout.split()) if not reply.returncode else []
        if not found:
            raise NetworkCreateError(
                f"Docker network {name} exists but its subnet could not be read.",
                _bounded(reply.stderr),
            )
        return found[0]

    # --- orphan pruning -------------------------------------------------------------

    def prune_orphans(
        self,
        run: RunCommand,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        grace: timedelta = ORPHAN_GRACE,
    ) -> list[str]:
        """Remove Tessera-labelled networks with no container and older than ``grace``.

        Only networks carrying the Tessera operation label are considered; any network
        without it is never listed for removal. A network still attached to any container
        (in any state) or created within ``grace`` (another run may be mid-setup) stays.
        Failures are skipped, never raised: pruning is best effort and re-runs next start.
        """
        removed: list[str] = []
        try:
            listing = run(
                [
                    "network",
                    "ls",
                    "--no-trunc",
                    "--filter",
                    f"label={OPERATION_LABEL}",
                    "--format",
                    "{{.ID}}",
                ]
            )
        except CpuError:
            return removed
        if listing.returncode:
            return removed
        for identifier in listing.stdout.split():
            try:
                if self._is_orphan(run, identifier, now(), grace):
                    if not run(["network", "rm", identifier]).returncode:
                        removed.append(identifier)
            except CpuError:
                continue
        return removed

    @staticmethod
    def _is_orphan(run: RunCommand, identifier: str, now: datetime, grace: timedelta) -> bool:
        inspected = run(
            [
                "network",
                "inspect",
                "--format",
                _ORPHAN_FORMAT,
                identifier,
            ]
        )
        parts = inspected.stdout.split()
        if inspected.returncode or len(parts) != 2 or not parts[0] or parts[0] == "<no":
            return False  # Not demonstrably Tessera-labelled.
        try:
            created = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
        except ValueError:
            return False
        if created.tzinfo is None or now - created < grace:
            return False
        attached = run(["ps", "--all", "--quiet", "--filter", f"network={identifier}"])
        return attached.returncode == 0 and not attached.stdout.strip()


def _parse_networks(tokens: Sequence[str]) -> list[ipaddress.IPv4Network]:
    parsed: list[ipaddress.IPv4Network] = []
    for token in tokens:
        if not re.fullmatch(r"[0-9./]+", token):
            continue  # IPv6 or empty entries.
        try:
            parsed.append(ipaddress.IPv4Network(token, strict=False))
        except ValueError:
            continue
    return parsed


def linux_host_routes(path: Path = Path("/proc/net/route")) -> list[ipaddress.IPv4Network]:
    """IPv4 destinations in the kernel routing table (empty where it is unreadable).

    The default route is ignored: it would overlap every candidate. On Docker Desktop the
    daemon runs in a VM whose routes are not visible here; the daemon's own overlap check
    still protects against its own networks.
    """
    if not sys.platform.startswith("linux"):
        return []
    try:
        lines = path.read_text().splitlines()[1:]
    except OSError:
        return []
    routes: list[ipaddress.IPv4Network] = []
    for line in lines:
        fields: Any = line.split()
        if len(fields) < 8:
            continue
        try:
            destination = int(fields[1], 16)
            mask = int(fields[7], 16)
            address = ipaddress.IPv4Address(destination.to_bytes(4, "little"))
            netmask = ipaddress.IPv4Address(mask.to_bytes(4, "little"))
            network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
        except (ValueError, OverflowError):
            continue
        if network.prefixlen:
            routes.append(network)
    return routes
