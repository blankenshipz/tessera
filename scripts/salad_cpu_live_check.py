#!/usr/bin/env python3
"""Bounded, opt-in live check of a Salad CPU-only profile (ADR 0062).

It answers two questions that no offline test can: does Salad accept and report back a
container group with no GPU class (the group shape), and what does a short run cost at the
operator's CPU rates (the billing). It is a dry run unless ``--run`` and an exact
``--confirm`` are both given, so neither default pytest nor a stray invocation can spend money.

    scripts/salad_cpu_live_check.py check  --profiles FILE --profile NAME --contract FILE \\
        --image IMAGE@sha256:DIGEST [--max-cents N]
    scripts/salad_cpu_live_check.py check  ... --run --confirm spend-up-to-Nc \\
        (--credential-file FILE | --credential-dir DIR --credential-version N \\
         [--credential-ref credential-ref:salad-management]) --receipt-directory DIR
    scripts/salad_cpu_live_check.py billing --receipt FILE --portal-cents X

Dry run (the default) reads only the operator's own files, derives the exact route, prints the
plan, the rate and the spend bound, and makes **no network call and reads no credential**.

A live run makes only the vetted Salad transport's calls, in this order: read the replica quota,
create one stopped group (free: nothing runs), read it back and compare its shape (no GPU),
start it, poll until it runs and ends or its running time reaches the bound, stop it and delete
it. The spend is bounded three ways: the ceiling is at most 9 cents (a hard constant, not a
setting), the running time is cut at the seconds that ceiling buys at the profile's rates, and
the group is always stopped and deleted, even on an error. Billing is **not** verified
automatically: Salad exposes no billing read this tool relies on, so the receipt records the
estimate and ``billing`` then compares a figure a person copies from Salad's own usage page.

The management credential is read either from an owner-only (0600) absolute file holding only the
token, or by reference from the operator's credential directory like the rest of the Salad stack
(``--credential-dir`` with the reference and exact version the campaign profile names). It is held
in a wiped buffer and never printed or written to the receipt. Nothing here is a paid default.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

# Hard ceiling on what one check may spend, in cents. Not configurable: a larger bound needs a
# code change and review, not a flag.
HARD_CEILING_CENTS = 9
DEFAULT_CEILING_CENTS = 5
# Never run longer than this once billed, whatever the rate, and never less than this (a rate so
# high that the ceiling buys under 30 s cannot show a meaningful run).
RUN_SECONDS_CAP = 300
MIN_RUN_SECONDS = 30
# Unbilled waits: for the instance to start running, and to settle after a stop.
ALLOCATION_WAIT_SECONDS = 600
SETTLE_WAIT_SECONDS = 90
# Billing continues until the instance is actually stopped, not until the stop is requested, so
# this many seconds are reserved out of the ceiling for the stop to take effect: the running time
# is cut at what the ceiling buys minus this, and the estimate counts through the observed stop.
SETTLE_RESERVE_SECONDS = 30
POLL_SECONDS = 2.0
# A fresh group can read pending or with a change in flight for a moment after create. It is read
# until it reads stopped and settled before start; a stopped group is not billed, so this wait
# costs nothing and is outside the spend ceiling and the settle reserve.
SETTLE_BEFORE_START_SECONDS = 60
DEFAULT_CREDENTIAL_REFERENCE = "credential-ref:salad-management"
RECEIPT_SCHEMA = "tessera.salad.cpu-live-check.v1"
# Salad's usage figures are coarse and our local clock brackets the run loosely; a portal figure
# within the larger of this many cents or this fraction of the estimate agrees with it.
BILLING_TOLERANCE_CENTS = Decimal("1")
BILLING_TOLERANCE_FRACTION = Decimal("0.5")
_END_STATES = {"succeeded", "stopped", "failed"}


class CheckRefused(Exception):
    """The inputs or the confirmation do not permit this run (exit status 2)."""


@dataclass(frozen=True)
class Plan:
    profile: object
    selection: object
    hourly_usd: Decimal
    ceiling_cents: int
    max_run_seconds: int
    worst_case_cents: Decimal
    operation_id: str
    settle_reserve_seconds: int = SETTLE_RESERVE_SECONDS

    @property
    def confirm(self) -> str:
        return f"spend-up-to-{self.ceiling_cents}c"

    def summary(self) -> dict:
        resources = self.selection.resources
        return dict(
            mode="plan",
            profile=self.profile.name,
            shape=dict(
                vcpu=resources.cpu_millicores // 1000,
                memory_mb=resources.memory_mb,
                disk_mb=resources.disk_mb,
                gpu_count=resources.gpu_count,
            ),
            hourly_usd=format(self.hourly_usd, "f"),
            ceiling_cents=self.ceiling_cents,
            max_run_seconds=self.max_run_seconds,
            settle_reserve_seconds=self.settle_reserve_seconds,
            worst_case_cents=format(self.worst_case_cents.quantize(Decimal("0.0001")), "f"),
            group=self.selection.salad.group,
            confirm_to_run=self.confirm,
        )


def build_plan(args) -> Plan:
    """Derive the exact CPU route from the operator's own files. No network, no credential."""
    from tessera_control.salad_campaign_profiles import derive_selection, load_profiles
    from tessera_control.salad_workload_contract import WorkloadContractV1

    if not HARD_CEILING_CENTS >= args.max_cents >= 1:
        raise CheckRefused(f"--max-cents must be 1 to {HARD_CEILING_CENTS}")
    try:
        profiles = load_profiles(args.profiles)
    except ValueError as error:
        raise CheckRefused(str(error)) from None
    profile = profiles.get(args.profile)
    if profile is None:
        raise CheckRefused("no such profile in the profile file")
    if profile.gpu:
        raise CheckRefused("this check is for a CPU-only profile; the named one has a GPU class")
    if not profile.image_allowed(args.image):
        raise CheckRefused("the image digest is not allowlisted by the profile")
    try:
        contract = WorkloadContractV1.model_validate(json.loads(Path(args.contract).read_text()))
    except (OSError, ValueError) as error:
        raise CheckRefused(
            f"workload contract unreadable or invalid: {type(error).__name__}"
        ) from None
    hourly = profile.cpu_rate.hourly_usd(profile.cpu_millicores, profile.memory_mb)
    affordable = int(Decimal(36 * args.max_cents) / hourly)  # seconds the ceiling buys
    # Billing runs until the stop takes effect, so the stop's settle window comes out of the
    # seconds the ceiling buys (the worst case is max_run + the reserve, never more).
    max_run = min(RUN_SECONDS_CAP, profile.route_max_seconds, affordable - SETTLE_RESERVE_SECONDS)
    if max_run < MIN_RUN_SECONDS:
        raise CheckRefused(
            f"at ${hourly:f}/hour the ceiling leaves {max(max_run, 0)} s of running after "
            f"reserving {SETTLE_RESERVE_SECONDS} s for the stop; raise --max-cents (at most "
            f"{HARD_CEILING_CENTS}) or use a cheaper profile"
        )
    if contract.workload_seconds > max_run:
        raise CheckRefused(
            f"the workload declares {contract.workload_seconds} s but the bound allows {max_run} s"
        )
    data = contract.input.snapshot_digest if contract.input is not None else "d" * 64
    operation = secrets.token_hex(16)
    selection = derive_selection(
        profile,
        contract,
        operation_id=operation,
        epoch=1,
        snapshot_digest=data,
        image=args.image,
        max_cost_cents=args.max_cents,
        plan_max_seconds=max_run,
    )
    worst = hourly * Decimal(max_run + SETTLE_RESERVE_SECONDS) / 36
    if worst > args.max_cents:  # unreachable by construction; a hard guard, not an assumption
        raise CheckRefused("internal: worst case exceeds the ceiling")
    return Plan(profile, selection, hourly, args.max_cents, max_run, worst, operation)


def _read_credential(path) -> bytearray:
    from tessera_control.salad_live_check import _private_bytes

    try:
        raw = _private_bytes(path, 4096).strip()
    except (OSError, ValueError):
        raise CheckRefused("the credential must be an owner-only (0600) absolute file") from None
    if not raw or any(not 33 <= b <= 126 for b in raw):
        raise CheckRefused("the credential file must hold one printable token")
    return bytearray(raw)


def _read_reference(directory, reference, version) -> object:
    """The credential by reference from the operator's credential directory (a SecretBuffer).

    The directory must be an owner-only (0700) directory of 0600 records, as the rest of the
    Salad stack requires; a missing, revoked, expired or other-version record is refused with no
    detail about which."""
    from tessera_control.salad_secret_files import PrivateFileSecretResolver

    try:
        resolver = PrivateFileSecretResolver(directory)
    except (OSError, ValueError):
        raise CheckRefused("the credential directory is not a private (0700) directory") from None
    try:
        return resolver.resolve(reference, version)
    except (OSError, ValueError):
        raise CheckRefused("the credential reference is unavailable at that version") from None
    finally:
        resolver.close()


def _diagnostics(observation) -> dict:
    """The non-secret fields that say why a group is not ready: state, change in flight, counts."""
    observation = observation or {}
    observation = observation.get("refusal", observation)  # a refused start nests its reading
    return {
        key: observation[key]
        for key in (
            "state",
            "pending_change",
            "instance_status_counts",
            "instance_count_keys",
            "instance_counts_complete",
        )
        if key in observation
    }


def _wipe(credential) -> None:
    """Zero a credential buffer (a bytearray or a SecretBuffer); safe to repeat."""
    if isinstance(credential, bytearray):
        for index in range(len(credential)):
            credential[index] = 0
    elif credential is not None and hasattr(credential, "zeroize"):
        credential.zeroize()


def run_live(plan: Plan, credential, *, factory=None, clock=time.monotonic, sleep=None):
    """Drive the bounded journey through the vetted transport; always stop and delete.

    ``factory`` is the transport's connection factory (a fake in tests); ``sleep`` defaults to
    the real one. Returns the receipt (no secret in it). The credential is wiped on every path,
    including one that fails before the journey starts.
    """
    try:
        return _run_live(plan, credential, factory=factory, clock=clock, sleep=sleep)
    finally:
        _wipe(credential)


def _run_live(plan: Plan, credential, *, factory=None, clock=time.monotonic, sleep=None):
    from tessera_control.salad_http import SaladHTTPTransport, group_settled
    from tessera_control.salad_secrets import SecretBuffer

    sleep = sleep or time.sleep
    selection = plan.selection
    secret = credential if isinstance(credential, SecretBuffer) else SecretBuffer(credential)
    # The transport's own settle waits use the same clock and sleep as this run.
    options = {"sleep": sleep, "clock": clock}
    if factory is not None:
        options["connection_factory"] = factory
    receipt = dict(
        schema=RECEIPT_SCHEMA,
        operation=plan.operation_id,
        plan=plan.summary(),
        steps=[],
        shape_ok=None,
        cleanup=dict(stopped=False, deleted=False),
    )
    steps = receipt["steps"]

    def step(name, result):
        entry = dict(step=name, status=result.status, reason=result.reason)
        if result.status != "observed" and result.observation:
            entry["observation"] = _diagnostics(result.observation)  # why, without a body
        steps.append(entry)
        return result

    provider_id = None
    started_at = first_running = natural_end = settled_at = None
    with secret:
        try:
            transport = SaladHTTPTransport(selection, **options)
            rate = step("rate", transport.gpu_class(selection, secret))
            receipt["rate_basis"] = (rate.observation or {}).get("price_basis")
            if not step("preflight", transport.preflight(selection, secret)).status == "observed":
                raise CheckRefused("preflight failed; nothing was created")
            created = step(
                "create_stopped", transport.send("create_stopped", selection, None, secret)
            )
            if created.status not in {"observed", "pending"} or not created.observation:
                if created.reason in {"identity_mismatch", "malformed_response"}:
                    # Salad answered, but not with the group shape asked for (for instance a GPU
                    # class came back): the group may exist and the transport did not surface
                    # its id, so a person must look it up by name.
                    receipt["shape_ok"] = False
                    receipt["cleanup"]["action_needed"] = (
                        "a group named in 'group' may exist in Salad although its shape was "
                        "refused; stop and delete it by hand (a stopped group is not billed)"
                    )
                raise CheckRefused(
                    f"the group could not be created as asked ({created.reason}); "
                    "nothing was started"
                )
            provider_id = created.observation.get("provider_id")
            # The shape check: Salad's own answer must still describe a group with no GPU class.
            # Read it until it settles (stopped, no change in flight, no instance): a fresh group
            # answers pending for a moment, and start is admitted only on a settled read.
            settle_until = clock() + SETTLE_BEFORE_START_SECONDS
            polls = 0
            while True:
                read = transport.send("read_group", selection, provider_id, secret)
                polls += 1
                observed_group = read.observation or {}
                counts = observed_group.get("instance_status_counts") or {}
                if (
                    read.status != "observed"
                    or group_settled(observed_group)
                    # A live instance will not settle by waiting: stop and let start refuse it.
                    or any(v for k, v in counts.items() if k != "stopping_count")
                    or clock() >= settle_until
                ):
                    break
                sleep(POLL_SECONDS)
            step("read_group", read)
            receipt["shape_ok"] = read.status == "observed"
            receipt["settle"] = dict(
                polls=polls, settled=group_settled(observed_group), **_diagnostics(observed_group)
            )
            if not receipt["shape_ok"]:
                raise CheckRefused(f"the created group does not match the CPU shape: {read.reason}")
            if not receipt["settle"]["settled"]:
                raise CheckRefused(
                    "the created group did not settle (stopped, no change in flight, no "
                    "instance) within the wait; nothing was started"
                )
            started_step = step("start", transport.send("start", selection, provider_id, secret))
            # Whether the group reported all four instance counts (a CPU group may omit them).
            steps[-1]["instance_counts_complete"] = observed_group.get("instance_counts_complete")
            if started_step.status not in {"observed", "pending"}:
                raise CheckRefused("Salad did not accept the start; nothing ran")
            begun = clock()
            started_at = begun
            while True:
                observed = transport.send("read_group", selection, provider_id, secret)
                state = (observed.observation or {}).get("state")
                now = clock()
                if state == "running" and first_running is None:
                    first_running = now
                if first_running is not None and (
                    state in _END_STATES or now - first_running >= plan.max_run_seconds
                ):
                    if state in _END_STATES:
                        natural_end = now  # it ended by itself; billing stopped by here
                    break
                if first_running is None and (
                    state in _END_STATES or now - begun >= ALLOCATION_WAIT_SECONDS
                ):
                    break
                sleep(POLL_SECONDS)
            receipt["final_state"] = state
        except CheckRefused as error:
            receipt["aborted"] = str(error)  # recorded in the receipt; cleanup still runs
        except Exception as error:  # noqa: BLE001 - the receipt must survive anything
            # Only the exception's type is kept: its message could carry request detail.
            receipt["aborted"] = f"unexpected {type(error).__name__}; see the cleanup outcome"
        finally:
            if provider_id is not None:
                settled_at = _cleanup(
                    transport, selection, provider_id, secret, receipt, clock, sleep
                )
    # Billing runs to the end of the instance: when it ended by itself, else when the stop was
    # observed to take effect (or, if that was never observed, to the end of the settle wait).
    billing_end = natural_end if natural_end is not None else settled_at
    if first_running is None or billing_end is None:
        run_seconds = 0.0
    else:
        run_seconds = max(0.0, billing_end - first_running)
    estimate = plan.hourly_usd * Decimal(str(run_seconds)) / 36
    receipt.update(
        running_seconds=round(run_seconds, 1),
        started=started_at is not None,
        estimated_cents=format(estimate.quantize(Decimal("0.0001")), "f"),
        within_ceiling=estimate <= plan.ceiling_cents,
        billing_verification="manual: compare the Salad usage page for this group, then run "
        "the billing command with the figure",
        group=plan.selection.salad.group,
    )
    return receipt


def _cleanup(transport, selection, provider_id, secret, receipt, clock, sleep):
    """Stop, wait for it to settle, delete; return when billing is known to have ended.

    Failures are recorded loudly, never raised. The return is the clock reading at the first
    observation of an ended group, or at the end of the wait (or of a failure) when none was,
    so an estimate built on it never undercounts the time the stop took.
    """
    steps = receipt["steps"]
    settled_at = None
    try:
        steps.append(
            dict(step="stop", status=transport.send("stop", selection, provider_id, secret).status)
        )
        deadline = clock() + SETTLE_WAIT_SECONDS
        while clock() < deadline:
            state = (
                transport.send("read_group", selection, provider_id, secret).observation or {}
            ).get("state")
            if state in _END_STATES:
                receipt["cleanup"]["stopped"] = True
                settled_at = clock()
                break
            sleep(POLL_SECONDS)
        if settled_at is None:
            settled_at = clock()  # never observed stopped: bill through the whole wait
        steps.append(
            dict(
                step="delete",
                status=transport.send("delete", selection, provider_id, secret).status,
            )
        )
        receipt["cleanup"]["deleted"] = steps[-1]["status"] in {"observed", "pending"}
    except Exception:  # noqa: BLE001 - cleanup must report, not mask the original error
        receipt["cleanup"]["error"] = "cleanup raised; delete the group by hand"
        if settled_at is None:
            settled_at = clock()
    if not (receipt["cleanup"]["stopped"] and receipt["cleanup"]["deleted"]):
        receipt["cleanup"]["action_needed"] = (
            "stop and delete the container group named in 'group' in Salad now; a group left "
            "running keeps billing"
        )
    return settled_at


def _write_receipt(directory, receipt) -> Path:
    from tessera_control.salad_live_check import _private_path

    os.close(_private_path(directory, directory=True))  # owner-only 0700 directory required
    path = Path(directory) / f"{receipt['operation']}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as target:
        json.dump(receipt, target, indent=2, sort_keys=True)
    return path


def billing_verdict(receipt: dict, portal_cents: Decimal) -> dict:
    """Compare a person's figure from Salad's usage page with the receipt's estimate."""
    estimate = Decimal(receipt["estimated_cents"])
    allowed = max(BILLING_TOLERANCE_CENTS, estimate * BILLING_TOLERANCE_FRACTION)
    return dict(
        estimated_cents=format(estimate, "f"),
        portal_cents=format(portal_cents, "f"),
        difference_cents=format(portal_cents - estimate, "f"),
        tolerance_cents=format(allowed, "f"),
        agrees=abs(portal_cents - estimate) <= allowed,
        meaning="the operator's CPU rates reproduce Salad's usage within tolerance"
        if abs(portal_cents - estimate) <= allowed
        else "the rates in the profile do not match what Salad billed: correct them",
    )


def _check(args) -> int:
    plan = build_plan(args)
    if not args.run:
        print(json.dumps(plan.summary(), indent=2, sort_keys=True))
        print("dry run: no network call was made and no credential was read", file=sys.stderr)
        return 0
    if args.confirm != plan.confirm:
        raise CheckRefused(f"--confirm must be exactly {plan.confirm!r} to spend up to the ceiling")
    if bool(args.credential_file) == bool(args.credential_dir) or not args.receipt_directory:
        raise CheckRefused(
            "exactly one of --credential-file or --credential-dir, and --receipt-directory, "
            "are required with --run"
        )
    if args.credential_dir and args.credential_version is None:
        raise CheckRefused("--credential-version is required with --credential-dir")
    credential = None
    try:
        # Anything added between reading the credential and the run is covered by the wipe.
        if args.credential_dir:
            credential = _read_reference(
                args.credential_dir, args.credential_ref, args.credential_version
            )
        else:
            credential = _read_credential(args.credential_file)
        receipt = run_live(plan, credential)
    finally:
        _wipe(credential)
    path = _write_receipt(args.receipt_directory, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    print(f"receipt: {path}", file=sys.stderr)
    clean = receipt["cleanup"]["stopped"] and receipt["cleanup"]["deleted"]
    ok = receipt["shape_ok"] and receipt["within_ceiling"] and clean and "aborted" not in receipt
    return 0 if ok else 1


def _billing(args) -> int:
    try:
        receipt = json.loads(Path(args.receipt).read_text())
        portal = Decimal(args.portal_cents)
    except (OSError, ValueError, ArithmeticError) as error:
        raise CheckRefused(f"receipt or figure unreadable: {type(error).__name__}") from None
    if receipt.get("schema") != RECEIPT_SCHEMA or not portal.is_finite() or portal < 0:
        raise CheckRefused("a receipt of this tool and a non-negative figure are required")
    verdict = billing_verdict(receipt, portal)
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0 if verdict["agrees"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    actions = parser.add_subparsers(dest="action", required=True)
    check = actions.add_parser("check", help="plan (default) or run the bounded live check")
    check.add_argument("--profiles", type=Path, required=True)
    check.add_argument("--profile", required=True)
    check.add_argument("--contract", type=Path, required=True)
    check.add_argument("--image", required=True)
    check.add_argument("--max-cents", type=int, default=DEFAULT_CEILING_CENTS)
    check.add_argument("--run", action="store_true", help="spend, up to the ceiling")
    check.add_argument("--confirm", default=None)
    check.add_argument("--credential-file", type=Path, default=None)
    check.add_argument(
        "--credential-dir",
        type=Path,
        default=None,
        help="the operator's credential directory (read the credential by reference)",
    )
    check.add_argument("--credential-ref", default=DEFAULT_CREDENTIAL_REFERENCE)
    check.add_argument("--credential-version", type=int, default=None)
    check.add_argument("--receipt-directory", type=Path, default=None)
    billing = actions.add_parser("billing", help="compare a Salad usage figure with a receipt")
    billing.add_argument("--receipt", type=Path, required=True)
    billing.add_argument("--portal-cents", required=True)
    args = parser.parse_args(argv)
    try:
        return _check(args) if args.action == "check" else _billing(args)
    except CheckRefused as error:
        print(f"salad cpu live check refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
