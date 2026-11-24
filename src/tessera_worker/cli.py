"""CLI for the explicitly opt-in native development worker."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .control_channel import DEFAULT_TIMEOUT_SECONDS
from .docker_network_pool import DEFAULT_PREFIX_LENGTH
from .grant_poller import DevelopmentGrantPoller, GrantPollerConfig, load_poller_config
from .service import (
    DevelopmentWorkerConfig,
    DevelopmentWorkerError,
    DevelopmentWorkerService,
    safe_error_detail,
)
from .supervisor import SupervisorError

_POLL_REQUIRED = ("control_url", "credential_file", "workspace_root", "development_output_root")


_POOL_HELP = "IPv4 CIDR (e.g. 10.213.0.0/16) to allocate Tessera's Docker networks from"


_MAX_OUTPUT_HELP = (
    "largest total output (bytes, 16 MiB to 1 TiB) a plan may ask this worker to collect"
)


_STAGE_WATCHER_HELP = (
    "set the observe-and-report stage watcher beside each local Docker stage explicitly "
    "(ADR 0087; on by default with the marker <workspace-root>/stage-watch.kill; reports go "
    "through this worker's control channel)"
)
_STAGE_WATCHER_KILL_SWITCH_HELP = (
    "absolute path of the operator kill-switch marker (present means engaged); with "
    "--stage-watcher and no marker path no stage watcher starts"
)


def _stage_watcher_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--stage-watcher", action="store_true", help=_STAGE_WATCHER_HELP)
    command.add_argument(
        "--no-stage-watcher",
        action="store_true",
        help="opt out of the default-on stage watcher (ADR 0125)",
    )
    command.add_argument(
        "--stage-watcher-kill-switch", default=None, help=_STAGE_WATCHER_KILL_SWITCH_HELP
    )
    command.add_argument(
        "--stage-watcher-stats",
        action="store_true",
        help="also sample docker stats so reports carry mem_bytes (enables the watcher config)",
    )


def _stage_watch(arguments: argparse.Namespace):  # noqa: ANN202
    """The explicit StageWatchConfig, else None (the worker's default-on marker applies).

    Any watcher flag is explicit, enabled configuration: ``--stage-watcher-kill-switch`` or
    ``--stage-watcher-stats`` alone is honoured, and with no marker path the default marker is
    used (resolved against the worker's private root).
    """
    marker = getattr(arguments, "stage_watcher_kill_switch", None)
    stats = bool(getattr(arguments, "stage_watcher_stats", False))
    flagged = bool(arguments.stage_watcher or marker is not None or stats)
    if getattr(arguments, "no_stage_watcher", False):
        if flagged:
            raise ValueError("--no-stage-watcher conflicts with the other stage watcher flags")
        from .stage_watch_config import StageWatchConfig

        return StageWatchConfig()
    if not flagged:
        return None
    from .stage_watch_config import StageWatchConfig

    return StageWatchConfig(enabled=True, kill_switch_path=marker, stats=stats)


_PUBLIC_ROOT_HELP = (
    "private 0700 directory for pinned public inputs this worker fetches (ADR 0097); "
    "without it a stage with public_artifact inputs is refused"
)
_REGISTRY_AUTH_HELP = (
    "operator registry auth file (private regular file, not read by the worker); mounted "
    "read-only into a pushing image build; needs --build-repository"
)
_BUILD_REPOSITORY_HELP = (
    "registry destination (host[/path]) an image build may push to; repeatable; "
    "needs --registry-auth-file; none registered refuses every pushing build"
)
_PUBLIC_CACHE_HELP = "optional private 0700 download cache, used only for cache-enabled inputs"
_PUBLIC_PROXY_HELP = (
    "optional explicit http://host:port proxy for that fetch; used only when the host is on the "
    "stage's egress allowlist, else the fetch is refused (proxy environment variables are ignored)"
)


class _Parser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        parsed = super().parse_args(args, namespace)
        if parsed.command == "development-poll" and parsed.config is None:
            missing = [name for name in _POLL_REQUIRED if getattr(parsed, name) is None]
            if parsed.allowed_image is None:
                missing.append("allowed_image")
            if missing:
                self.error(f"development-poll needs --config or: {', '.join(missing)}")
        return parsed


def parser() -> argparse.ArgumentParser:
    command = _Parser(prog="tessera-worker")
    commands = command.add_subparsers(dest="command", required=True)
    run = commands.add_parser("development-run")
    run.add_argument("--control-url", required=True)
    run.add_argument("--grant-id", required=True)
    run.add_argument("--credential-file", type=Path, required=True)
    run.add_argument("--workspace-root", type=Path, required=True)
    run.add_argument("--development-output-root", type=Path, required=True)
    run.add_argument("--allowed-image", required=True)
    run.add_argument("--egress-proxy-image", default=None)
    run.add_argument("--max-output-bytes", type=int, default=None, help=_MAX_OUTPUT_HELP)
    run.add_argument("--public-root", type=Path, default=None, help=_PUBLIC_ROOT_HELP)
    run.add_argument("--public-cache-dir", type=Path, default=None, help=_PUBLIC_CACHE_HELP)
    run.add_argument("--registry-auth-file", type=Path, default=None, help=_REGISTRY_AUTH_HELP)
    run.add_argument(
        "--build-repository", action="append", default=None, help=_BUILD_REPOSITORY_HELP
    )
    run.add_argument("--public-fetch-proxy", default=None, help=_PUBLIC_PROXY_HELP)
    run.add_argument("--container-user", default=None, help="numeric uid:gid for containers")
    run.add_argument("--allow-root-container-user", action="store_true")
    run.add_argument("--docker-network-pool", default=None, help=_POOL_HELP)
    run.add_argument("--docker-network-prefix-length", type=int, default=DEFAULT_PREFIX_LENGTH)
    run.add_argument("--control-timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument("--poll-seconds", type=float, default=1.0)
    run.add_argument("--max-ticks", type=int, default=90_000)
    _stage_watcher_arguments(run)
    poll = commands.add_parser("development-poll")
    # ADR 0038 loop contract: --config names the settings file (paths only, no secrets);
    # the flags remain for a one-off run. Runs forever unless --once or --max-cycles.
    poll.add_argument("--config", type=Path, default=None)
    poll.add_argument("--control-url", default=None)
    poll.add_argument("--credential-file", type=Path, default=None)
    poll.add_argument("--workspace-root", type=Path, default=None)
    poll.add_argument("--development-output-root", type=Path, default=None)
    poll.add_argument("--allowed-image", action="append", default=None)
    poll.add_argument("--egress-proxy-image", default=None)
    poll.add_argument("--memory-bytes", type=int, default=None)
    poll.add_argument("--scratch-bytes", type=int, default=None)
    poll.add_argument(
        "--max-scratch-bytes",
        type=int,
        default=None,
        help="operator ceiling on a plan's scratch request (ADR 0059); default 64 MiB",
    )
    poll.add_argument("--max-output-bytes", type=int, default=None, help=_MAX_OUTPUT_HELP)
    poll.add_argument("--public-root", type=Path, default=None, help=_PUBLIC_ROOT_HELP)
    poll.add_argument("--public-cache-dir", type=Path, default=None, help=_PUBLIC_CACHE_HELP)
    poll.add_argument("--registry-auth-file", type=Path, default=None, help=_REGISTRY_AUTH_HELP)
    poll.add_argument(
        "--build-repository", action="append", default=None, help=_BUILD_REPOSITORY_HELP
    )
    poll.add_argument("--public-fetch-proxy", default=None, help=_PUBLIC_PROXY_HELP)
    poll.add_argument("--container-user", default=None, help="numeric uid:gid for containers")
    poll.add_argument("--allow-root-container-user", action="store_true")
    poll.add_argument("--docker-network-pool", default=None, help=_POOL_HELP)
    poll.add_argument("--docker-network-prefix-length", type=int, default=DEFAULT_PREFIX_LENGTH)
    poll.add_argument("--control-timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    poll.add_argument("--poll-seconds", type=float, default=2.0)
    poll.add_argument("--max-grants", type=int, default=None)
    poll.add_argument("--max-cycles", type=int, default=None)
    poll.add_argument("--once", action="store_true")
    poll.add_argument("--health-file", type=Path, default=None)
    _stage_watcher_arguments(poll)
    return command


def _poll_config(arguments: argparse.Namespace) -> GrantPollerConfig:
    if arguments.config is not None:
        return load_poller_config(arguments.config)
    return GrantPollerConfig(
        control_url=arguments.control_url,
        credential_file=arguments.credential_file,
        workspace_root=arguments.workspace_root,
        development_output_root=arguments.development_output_root,
        allowed_images=tuple(arguments.allowed_image),
        egress_proxy_image=arguments.egress_proxy_image,
        memory_bytes=arguments.memory_bytes,
        scratch_bytes=arguments.scratch_bytes,
        max_scratch_bytes=arguments.max_scratch_bytes,
        max_output_bytes=arguments.max_output_bytes,
        public_root=arguments.public_root,
        public_cache_dir=arguments.public_cache_dir,
        registry_auth_file=arguments.registry_auth_file,
        build_repositories=tuple(arguments.build_repository or ()),
        public_fetch_proxy=arguments.public_fetch_proxy,
        container_user=arguments.container_user,
        allow_root_container_user=arguments.allow_root_container_user,
        docker_network_pool=arguments.docker_network_pool,
        docker_network_prefix_length=arguments.docker_network_prefix_length,
        control_timeout_seconds=arguments.control_timeout_seconds,
        poll_seconds=arguments.poll_seconds,
        max_grants=arguments.max_grants,
    )


def _poll(arguments: argparse.Namespace) -> int:
    """The shared service loop around one poller; exit 2 means configuration refused."""
    from .service_loop import StopFlag, run_service

    try:
        config = _poll_config(arguments)
        stage_watch = _stage_watch(arguments)
        if stage_watch is not None:
            # An explicit flag wins over the config file's stage_watcher block.
            config = dataclasses.replace(config, stage_watch=stage_watch)
        poller = DevelopmentGrantPoller(config)
    except (DevelopmentWorkerError, ValueError, OSError):
        # Bounded and path-free: a refused configuration or credential is not retried.
        print(json.dumps({"status": "refused", "reason": "poller_setup_unavailable"}))
        return 2
    stop = StopFlag()
    stop.install()
    cycles = 0

    def step() -> list:
        nonlocal cycles
        entries = poller.step()
        cycles += 1
        if poller.done():
            stop.request()
        return entries

    code = run_service(
        step=step,
        interval=config.poll_seconds,
        once=arguments.once,
        max_cycles=arguments.max_cycles,
        health_file=arguments.health_file,
        name="development-poll",
        stop=stop,
    )
    print(
        json.dumps(
            {
                "abandoned": poller.abandoned,
                "cycles": cycles,
                "errors": poller.errors,
                "grants": poller.grants_run,
                "refused": poller.refusals,
            },
            sort_keys=True,
        )
    )
    return code


def main() -> int:
    arguments = parser().parse_args()
    try:
        if arguments.command == "development-poll":
            return _poll(arguments)
        config = DevelopmentWorkerConfig(
            control_url=arguments.control_url,
            grant_id=arguments.grant_id,
            credential_file=arguments.credential_file,
            workspace_root=arguments.workspace_root,
            development_output_root=arguments.development_output_root,
            allowed_image=arguments.allowed_image,
            egress_proxy_image=arguments.egress_proxy_image,
            max_output_bytes=arguments.max_output_bytes,
            public_root=arguments.public_root,
            public_cache_dir=arguments.public_cache_dir,
            registry_auth_file=arguments.registry_auth_file,
            build_repositories=tuple(arguments.build_repository or ()),
            public_fetch_proxy=arguments.public_fetch_proxy,
            container_user=arguments.container_user,
            allow_root_container_user=arguments.allow_root_container_user,
            docker_network_pool=arguments.docker_network_pool,
            docker_network_prefix_length=arguments.docker_network_prefix_length,
            control_timeout_seconds=arguments.control_timeout_seconds,
            poll_seconds=arguments.poll_seconds,
            max_ticks=arguments.max_ticks,
        )
        stage_watch = _stage_watch(arguments)
        if stage_watch is None:
            service = DevelopmentWorkerService(config)  # default-on marker (ADR 0125)
        else:
            service = DevelopmentWorkerService(config, stage_watch=stage_watch)
        result = service.run()
        print(json.dumps(result, sort_keys=True))
        # Imported here, beside its only use, so this file's import block stays unchanged.
        from .exit_codes import (
            EXIT_CREDENTIAL_REJECTED,
            EXIT_OK,
            EXIT_REPORT_PENDING,
            EXIT_TERMINAL,
        )

        if result.get("terminal") is True:
            # A recorded permanent refusal needs an operator; never exit 0.
            print(
                f"development worker terminal: operation {result.get('operation_id')} "
                f"reason {result.get('reason')}",
                file=sys.stderr,
            )
            return EXIT_TERMINAL
        if result.get("credential_rejected") is True:
            # Scripts can tell a rejected credential from a clean non-terminal result.
            status = result.get("control_status")
            shown = f" (HTTP {status})" if type(status) is int else ""
            print(
                f"development worker: control rejected the worker credential{shown}; "
                "renew or replace the credential, then restart",
                file=sys.stderr,
            )
            return EXIT_CREDENTIAL_REJECTED
        if result.get("delivery_pending") is True:
            print(
                "development worker: an observation is waiting in the outbox "
                f"(retry in {result.get('retry_after_seconds')} s); run it again to deliver",
                file=sys.stderr,
            )
            return EXIT_REPORT_PENDING
        return EXIT_OK
    except (DevelopmentWorkerError, ValueError, OSError) as error:
        # Never include parsed arguments or credential contents in diagnostics.
        print(f"development worker failed: {error}", file=sys.stderr)
        return 1
    except SupervisorError as error:
        # Other supervisor failures: bounded and path-free, never credentials.
        print(f"development worker failed: {safe_error_detail(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
