"""Opt-in authenticated API collection. No workload creation or live activation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .capability_activation import Frozen
from .capability_collector import CollectorJournal, KubernetesEvidenceCollector
from .capability_inspection import RawEvidenceInspector
from .capability_kube_client import (
    AuthenticatedKubernetesClient,
    ClientBinding,
    CollectionError,
    HTTPSKubernetesTransport,
    load_credentials,
    private_read,
)
from .capability_runner_contract import RunnerPlan
from .capability_witness import KINDS, WitnessPin


class CredentialReference(Frozen):
    binding: ClientBinding
    kubeconfig: str | None = Field(default=None, max_length=1024)
    token_file: str | None = Field(default=None, max_length=1024)
    ca_file: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def explicit(self):
        refs = (self.kubeconfig, self.token_file, self.ca_file)
        if any(value is not None and not Path(value).is_absolute() for value in refs):
            raise ValueError("absolute credential references required")
        if self.binding.mode == "kubeconfig":
            if self.kubeconfig is None or self.token_file is not None or self.ca_file is not None:
                raise ValueError("explicit kubeconfig reference required")
        elif self.kubeconfig is not None or self.token_file is None or self.ca_file is None:
            raise ValueError("explicit service-account references required")
        return self


class CollectorConformanceConfig(Frozen):
    version: Literal[1] = 1
    enabled: Literal[True]
    plan: RunnerPlan
    collector: CredentialReference
    dispatcher: CredentialReference
    workload: CredentialReference
    resource_pins: dict[str, tuple[str, str]] = Field(min_length=1, max_length=32)
    journal: str = Field(max_length=1024)
    signing_key_file: str = Field(max_length=1024)

    @field_validator("enabled", mode="before")
    @classmethod
    def explicit_boolean(cls, value):
        if value is not True:
            raise ValueError("explicit boolean opt-in required")
        return value

    @model_validator(mode="after")
    def bounds(self):
        if not Path(self.journal).is_absolute() or not Path(self.signing_key_file).is_absolute():
            raise ValueError("absolute custody references required")
        if (
            sum(
                ref.binding.max_requests for ref in (self.collector, self.dispatcher, self.workload)
            )
            > 256
        ):
            raise ValueError("at most 256 total API requests")
        return self


class WitnessReference(Frozen):
    pin: WitnessPin
    channel: CredentialReference
    request_key_file: str = Field(max_length=1024)
    report_key_file: str = Field(max_length=1024)

    @model_validator(mode="after")
    def references(self):
        if not all(Path(p).is_absolute() for p in (self.request_key_file, self.report_key_file)):
            raise ValueError("absolute witness key references required")
        if (
            self.channel.binding.identity,
            self.channel.binding.namespace,
            self.channel.binding.namespace_uid,
        ) != (
            self.pin.identity,
            self.pin.namespace,
            self.pin.namespace_uid,
        ):
            raise ValueError("witness channel identity mismatch")
        return self


class RuntimeWitnessConformanceConfig(CollectorConformanceConfig):
    # Separate explicit permission for disposable probes; API-only flag never
    # inherits this permission. Unknown fields/claims/commands are forbidden.
    harmless_fixture: Literal["nonce-owned-disposable-v1"]
    witnesses: dict[str, WitnessReference] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def witnesses_bound(self):
        if set(self.witnesses) != set(KINDS):
            raise ValueError("all four independent witnesses required")
        for kind, ref in self.witnesses.items():
            if ref.pin.kind != kind or ref.channel.binding.plan_digest != self.plan.digest:
                raise ValueError("witness scope mismatch")
            if ref.pin.namespace in {
                self.plan.activation.namespace,
                self.plan.activation.control_namespace,
            }:
                raise ValueError("dedicated observer namespace required")
        return self


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--schema", action="store_true")
    mode.add_argument("--collect-live", action="store_true")
    mode.add_argument("--witness-live", action="store_true")
    mode.add_argument("--witness-schema", action="store_true")
    mode.add_argument(
        "--live", action="store_true", help="explicit target-specific witness effects"
    )
    mode.add_argument("--target-schema", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--target", type=Path)
    args = parser.parse_args(argv)
    if args.schema or args.witness_schema or args.target_schema:
        schema = (
            RuntimeWitnessConformanceConfig if args.witness_schema else CollectorConformanceConfig
        )
        if args.target_schema:
            from .capability_witness_target import TargetConformanceConfig

            schema = TargetConformanceConfig
        print(json.dumps(schema.model_json_schema(), sort_keys=True))
        return 0
    if not args.collect_live and not args.witness_live and not args.live:
        print(
            "disabled: explicit --collect-live, --witness-live or --live --target required; "
            "no effects"
        )
        return 0
    journal = None
    try:
        schema = (
            RuntimeWitnessConformanceConfig if args.witness_live else CollectorConformanceConfig
        )
        if args.live:
            from .capability_witness_target import TargetConformanceConfig

            schema = TargetConformanceConfig
            if args.target is None or args.config is not None:
                raise CollectionError("auth-denied")
        elif args.target is not None:
            raise CollectionError("auth-denied")
        config = schema.model_validate_json(
            private_read(args.target if args.live else args.config, 512 * 1024)
        )
        key = private_read(config.signing_key_file, 128)
        clients = []
        for ref in (config.collector, config.dispatcher, config.workload):
            credentials = load_credentials(
                ref.binding,
                kubeconfig=ref.kubeconfig,
                token_file=ref.token_file,
                ca_file=ref.ca_file,
            )
            clients.append(
                AuthenticatedKubernetesClient(
                    binding=ref.binding,
                    plan=config.plan,
                    transport=HTTPSKubernetesTransport(ref.binding, credentials),
                )
            )
        journal = CollectorJournal(Path(config.journal))
        witness = None
        if args.witness_live or args.live:
            from .capability_witness_transport import IndependentRuntimeWitnesses, WitnessRPC

            channels = {}
            for kind, ref in config.witnesses.items():
                credentials = load_credentials(
                    ref.channel.binding,
                    kubeconfig=ref.channel.kubeconfig,
                    token_file=ref.channel.token_file,
                    ca_file=ref.channel.ca_file,
                )
                request_key = private_read(ref.request_key_file, 128)
                report_key = private_read(ref.report_key_file, 128)
                if key in (request_key, report_key):
                    raise CollectionError("auth-denied")
                transport = HTTPSKubernetesTransport(ref.channel.binding, credentials)
                if args.live:
                    from .capability_witness_target import mtls_transport

                    transport = mtls_transport(ref.channel, config.witness_tls[kind])
                channels[kind] = WitnessRPC(
                    pin=ref.pin,
                    transport=transport,
                    request_key=request_key,
                    report_key=report_key,
                )
            witness = IndependentRuntimeWitnesses(channels=channels, journal=journal)
        collector = KubernetesEvidenceCollector(
            client=clients[0],
            dispatcher=clients[1],
            workload=clients[2],
            witness=witness,
            journal=journal,
            key=key,
            resource_pins=config.resource_pins,
        )
        RawEvidenceInspector(client=collector, now=collector.now).inspect(config.plan)
        if args.witness_live or args.live:
            print("observed: four fresh authenticated witnesses; retained evidence; no activation")
            return 0
        # API-only collection never acquires permission for witness effects.
        print("blocked: independent runtime conformance unavailable; retained API evidence")
        return 2
    except CollectionError as exc:
        print("blocked: " + exc.category + "; no activation")
        return 2
    except Exception:
        print("blocked: invalid or incomplete collector conformance; no activation")
        return 2
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
