"""Render offline manifests OR opt in to local identity/RBAC/admission dry runs."""

from __future__ import annotations

import argparse
import ipaddress
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from tessera_worker.comparison_pilot import PilotError
from tessera_worker.comparison_pilot_remote import RemoteKubernetes, bounded_file, load_target
from tessera_worker.pilot_authority_probe import probe_authority
from tessera_worker.pilot_isolation import PilotIsolation


class ProbeJournal:
    """Bounded digest-only transport audit, returned for the operator to retain."""

    def __init__(self):
        self.events = []

    def event(self, kind, value):
        if len(self.events) >= 200:
            raise PilotError("probe audit limit reached")
        self.events.append({"kind": kind, **value})


def run_probe(config, journal):
    if set(config) != {"target", "runtime"} or set(config["runtime"]) != {
        "ca_file",
        "compute_credential_file",
    }:
        raise PilotError("probe config requires only explicit target and runtime references")
    target, runtime = config["target"], config["runtime"]
    cluster, output = load_target(target)
    # Local-only, no ambient kubectl, exec plugins, proxies or cloud auth. Even
    # explicitly configured remote/cloud targets are outside this probe's scope.
    if (
        cluster.context != "docker-desktop"
        or not ipaddress.ip_address(urlsplit(cluster.server).hostname).is_loopback
    ):
        raise PilotError("probe requires an explicit loopback Docker Desktop API target")
    remote = RemoteKubernetes(
        cluster,
        output,
        Path(runtime["ca_file"]),
        Path(runtime["compute_credential_file"]),
        journal,
        target["credential_scope"],
        datetime.now(UTC),
        None,
        isolation=target["isolation"],
    )
    return probe_authority(remote)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--render", action="store_true", help="offline JSON Kubernetes List to stdout"
    )
    mode.add_argument(
        "--probe", action="store_true", help="opt-in local authenticated dry runs only"
    )
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if not args.render and not args.probe:
        print(json.dumps({"state": "not_started", "workloads_started": False}))
        return 0
    journal = ProbeJournal()
    try:
        if args.config is None:
            raise PilotError("explicit configuration required")
        config = json.loads(bounded_file(args.config, 65536))
        if args.render:
            profile = PilotIsolation(**config)
            print(
                json.dumps(
                    {"apiVersion": "v1", "kind": "List", "items": profile.manifests()}, indent=2
                )
            )
            return 0
        result = run_probe(config, journal)
    except Exception:
        # No upstream body, raw config, paths or credentials in diagnostics.
        result = {
            "state": "blocked",
            "workloads_started": False,
            "network_enforcement": "unverified",
        }
    result["audit"] = journal.events
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
