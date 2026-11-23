"""Campaign submission, approval, cancel and reads (ADR 0038).

Submission grants nothing. A human approves the exact spec digest; the runner (WP2)
then acts through ``campaign_delegation``. Everything here is one ``_mutate``
transaction per command, so state, events and the idempotency row commit together.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from . import campaign_heavy_guard_readout as heavy_guard_readout
from . import campaign_hold, campaign_placement, program_budget, provider_trust, smoke_credentials
from .approval_trust import decision_provenance, require_decision_actor
from .campaign_gates import CampaignGateCommands, evaluate_unlock, record_evaluations
from .campaign_holdout import campaign_claims, check_unclaimed
from .campaign_io import (
    refuse_submitted_lineage,
    resolve_artifact,
    resolve_image_refs,
    resolve_receipt_refs,
)
from .campaign_models import (
    GRANT_SETTLE_MARGIN_SECONDS,
    TERMINAL_CAMPAIGN,
    ApproveCampaign,
    CancelCampaign,
    SubmitCampaign,
    normalize,
    parse_not_before,
    parse_spec,
    parse_spec_json,
    placement_candidates,
    spec_digest,
    stage_input_class,
    stage_output_class,
    stage_provider,
    topological_order,
)
from .campaign_pending_approval import pending_human_approval
from .campaign_rerun import (
    RERUN_EVENT,
    RerunCampaign,
    RerunRefused,
    lineage_of,
    lineage_remaining,
    load_source,
    plan_rerun,
    rerun_tree,
    reruns_of,
)
from .campaign_salad_adapter import expand_with_registered
from .campaign_salad_commands import (  # noqa: F401
    SALAD_ACTIVE_PHASES,
    SaladWorkerCommands,
    _GuardianFinal,
)
from .campaign_standing_store import StandingApprovalCommands
from .campaign_steering import CampaignSteeringCommands
from .data_class import DataClassRefused, snapshot_data_class
from .execution import ExecutionStore
from .external_agents import ExternalAgentStore
from .snapshots import SnapshotStore
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    _parse_utc,
    _utc_string,
    canonical_digest,
    utc_now,
)

SALAD_REFUSAL = "salad-gpu-v1 stages are not enabled: no Salad profile is configured"
RUNNABLE_STATUSES = (
    "running",
    "stopping",
    "cancel_requested",
    "blocked",
    "paused_delegation_lapsed",
)
MAX_LISTED_INTENTS = 500
# Statuses a human may re-approve. Only the first two resume forward work; in the others
# a re-approval installs a live delegation for reduce-only cleanup and changes no status.
REAPPROVABLE_STATUSES = ("submitted", "paused_delegation_lapsed", "cancel_requested", "blocked")
FORWARD_APPROVAL_STATUSES = ("submitted", "paused_delegation_lapsed")


def _invalid(error: ValidationError) -> StoreError:
    first = error.errors()[0] if error.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))[:200]
    return StoreError(f"Invalid campaign request: {location}: {str(first.get('msg', ''))[:200]}")


def infra_retry_policy(spec: Any) -> list[dict[str, Any]]:
    """What an approver is agreeing to for infrastructure retries (ADR 0058), stage by stage.

    The stored spec omits the policy fields at their defaults, so "off" would be invisible in
    it; this states every stage's bound, the exit codes that count, the providers a retry may
    move to and the attempts its holds are counted over, always."""
    return [
        {
            "stage_key": stage.key,
            "infra_retries": stage.retry.infra_retries,
            "infra_exit_codes": list(stage.retry.infra_exit_codes),
            "reroute_providers": list(stage.retry.reroute_providers),
            "worst_case_attempts": stage.retry.max_attempts + stage.retry.infra_retries,
        }
        for stage in normalize(spec).stages
    ]


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in dict(row).items() if key != "spec_json"}
    return result


def _read_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """The summary plus, while an approved campaign waits for its ``not_before``, ``waiting``."""
    result = _summary(row)
    hold = campaign_hold.waiting(row["spec_json"], row["status"], _parse_utc(utc_now()))
    if hold is not None:
        result["waiting"] = hold
    return result


class LicenseRefused(Conflict):
    """A provider's licence-label restriction refuses a stage's inputs (a permanent refusal for
    that provider; placement records it as ``license_label``). ``labels`` are the sorted
    effective labels that were refused."""

    def __init__(self, message: str = "", labels: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.labels: list[str] = sorted(labels)


class CampaignStore(
    CampaignGateCommands,
    CampaignSteeringCommands,
    StandingApprovalCommands,
    SaladWorkerCommands,
    ExternalAgentStore,
):
    def __init__(self, state_dir):
        super().__init__(state_dir)

    # -- actor classification -------------------------------------------------------

    @staticmethod
    def _external_scope(connection: sqlite3.Connection, actor: Mapping[str, Any]) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM external_agent_scopes WHERE credential_id=?",
                (actor["credential_id"],),
            ).fetchone()
            is not None
        )

    def _require_action(
        self, connection: sqlite3.Connection, actor: dict[str, Any], action: str
    ) -> None:
        """External agents need the enrolled scope; workers never reach campaigns."""
        if actor.get("kind") == "worker":
            raise Forbidden("Workers cannot use campaigns.")
        if self._external_scope(connection, actor):
            self._scope(connection, actor, action)

    @staticmethod
    def _campaign(
        connection: sqlite3.Connection, actor: dict[str, Any], campaign_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT c.* FROM campaigns c JOIN projects p ON p.id = c.project_id
            WHERE c.id = ? AND p.org_id = ?""",
            (campaign_id, actor["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Campaign was not found.")
        Store._project(connection, actor, row["project_id"])
        return row

    # -- spec reference checks (submit and approve) ---------------------------------

    @staticmethod
    def adapter_entries(connection: sqlite3.Connection, live: dict[str, Any], project_id: str):
        """The stage adapter's view of a pinned input: (path, sha256, size) of each file it holds.

        Reads only: a snapshot's recorded manifest (same project) or a pinned artifact resolved as
        an ``artifact`` input is at submit. Anything else is a 409 naming the input."""
        project = {"org_id": live["org_id"], "project_id": project_id}

        def entries(source: Any) -> list[tuple[str, str, int]]:
            if source.kind == "public_artifact":
                return []  # fetched on the provider (ADR 0097): no custody bytes
            if source.kind == "artifact":
                found = resolve_artifact(connection, project, source)
                return [(found.path, found.sha256, found.size_bytes)]
            row = connection.execute(
                "SELECT manifest_json FROM source_snapshots WHERE id=? AND project_id=?",
                (source.snapshot_id, project_id),
            ).fetchone()
            if row is None:
                raise Conflict(f"Input {source.name!r}: the pinned snapshot was not found.")
            return [
                (e["relative_path"], e["sha256"], int(e["size"]))
                for e in json.loads(row["manifest_json"])["entries"]
            ]

        return entries

    @staticmethod
    def recorded_snapshot_class(connection: sqlite3.Connection, snapshot_id: str) -> str:
        """The ADR 0039 recorded class of a snapshot (the one source of truth).

        A thin reader over ``data_class.snapshot_data_class``: an unrecorded snapshot reads
        as private_derived. The spec's claim is never consulted.
        """
        return snapshot_data_class(connection, snapshot_id)["data_class"]

    @staticmethod
    def check_grant_lifetime(provider: Any, stage: Any) -> None:
        """Refuse a grant that expires before its stage can finish (ADR 0048).

        The grant is the only authority a run has, and the worker stops at its expiry, so a
        ``grant_ttl_seconds`` shorter than the stage's ``max_duration_seconds`` plus the
        settle margin can never complete. It is refused at admission with the reason."""
        from .campaign_steps import StepFailed

        try:
            # The provider decides the lifetime (a plugin may take it from its own config).
            ttl = int(provider.grant_request(None, None, stage).ttl_seconds)
        except StepFailed:
            return  # the worker lookup reports this refusal with its own reason
        except (AttributeError, TypeError):
            # A provider whose lifetime needs a live runner cannot be checked here; its
            # ``grant_request`` must be pure when called with ``None`` to be checked. The
            # skip is logged so a provider bug does not pass unnoticed.
            logging.getLogger("tessera.campaign_store").warning(
                "grant lifetime of stage %s not checked: provider needs a runner", stage.key
            )
            return
        required = int(stage.max_duration_seconds) + GRANT_SETTLE_MARGIN_SECONDS
        if ttl < required:
            raise Conflict(
                f"Stage {stage.key!r}: grant_shorter_than_stage_duration: grant_ttl_seconds "
                f"{ttl} is shorter than max_duration_seconds "
                f"{int(stage.max_duration_seconds)} plus the {GRANT_SETTLE_MARGIN_SECONDS}s "
                f"settle margin ({required}s), so the grant would expire before the stage "
                "can finish; raise grant_ttl_seconds or lower max_duration_seconds."
            )

    @staticmethod
    def stage_worker(provider: Any, stage: Any) -> str | None:
        """The worker principal the provider would grant this stage to (a pure lookup)."""
        from .campaign_steps import StepFailed

        try:
            return provider.grant_request(None, None, stage).worker_principal_id
        except StepFailed as error:
            raise Conflict(f"Stage {stage.key!r}: {error.detail}") from None

    @staticmethod
    def trust_hint(provider_id: str, worker: str | None, cls: str = "private_derived") -> str:
        """What the operator adds to the provider trust file to admit ``cls``.

        Provider trust stays explicit operator policy: this only names the entry."""
        who = worker or "<worker principal id>"
        return (
            f'To admit it, make the provider trust file\'s "providers" entry for '
            f'"{provider_id}" list "{cls}" under data_classes and "{who}" under '
            "worker_principal_ids (merge into the existing entry; do not replace it, or other "
            "listed workers lose access), then restart serve."
        )

    @staticmethod
    def check_stage_inputs(
        connection: sqlite3.Connection,
        stage: Any,
        provider: Any,
        approval_digest: str | None = None,
        *,
        spec: Any = None,
        scope: tuple[str, str] | None = None,
    ) -> frozenset[str] | None:
        """ADR 0040 section 4: every input must be one the provider instance may receive.

        A snapshot input goes through ``provider_admits`` (recorded class and code approval);
        a stage or artifact input is always ``private_derived``. Raises Conflict (409) naming
        the stage, input, provider and worker principal; used at submit, at approve and again
        when the runner reserves the stage. Returns the effective licence labels checked
        (None when the provider is unrestricted).
        """
        worker = CampaignStore.stage_worker(provider, stage)
        narrowed = frozenset(stage.send_classes) if stage.send_classes else None
        who = f"provider {stage.provider!r} with worker principal {worker!r}"
        for source in stage.inputs:
            if source.kind == "public_artifact":
                continue  # public data fetched on the provider (ADR 0097): never custody
            if source.kind == "snapshot":
                try:
                    provider_trust.provider_admits(
                        connection,
                        stage.provider,
                        worker,
                        source.snapshot_id,
                        narrowed,
                        approval_digest,
                    )
                except DataClassRefused as error:
                    hint = ""
                    if error.admit_class is not None:
                        hint = " " + CampaignStore.trust_hint(
                            stage.provider, worker, error.admit_class
                        )
                    raise Conflict(
                        f"Stage {stage.key!r} input {source.name!r}: {error} ({who}).{hint}"
                    ) from None
            elif not provider_trust.provider_admits_class(
                stage.provider,
                worker,
                "private_derived",
                narrowed,
                approval_digest,
                connection=connection,
            ):
                raise Conflict(
                    f"Stage {stage.key!r} input {source.name!r} is derived data and {who} is "
                    "not trusted to receive private_derived (provider trust setting). "
                    + CampaignStore.trust_hint(stage.provider, worker)
                )
        return CampaignStore.check_license_labels(connection, stage, spec, scope)

    @staticmethod
    def check_license_labels(
        connection: sqlite3.Connection,
        stage: Any,
        spec: Any,
        scope: tuple[str, str] | None,
    ) -> frozenset[str] | None:
        """Licence-label placement guard (fail closed).

        A provider whose trust entry lists ``allowed_license_labels`` receives a stage only if
        every effective label of every input (snapshot labels, inherited through lineage) is in
        that list; a provider the trust file lists without the key is unrestricted, and one it
        does not list may receive only ``unlabelled`` data. When the labels cannot be
        determined for a restricted provider the stage is refused, never admitted. A busy or
        locked database (``sqlite3.OperationalError``) is transient and propagates so the
        caller retries; it is never turned into a refusal. Returns the labels checked."""
        policy = provider_trust.current_policy(connection)
        allowed = policy.allowed_license_labels(stage.provider)
        if allowed is None:
            return None
        from . import license_labels

        if spec is None or scope is None:
            raise Conflict(
                f"Stage {stage.key!r}: provider {stage.provider!r} restricts licence labels and "
                "they cannot be determined here; refused (fail closed)."
            )
        try:
            labels = license_labels.stage_labels(
                connection, spec, stage.key, org_id=scope[0], project_id=scope[1]
            )
        except sqlite3.OperationalError:
            raise
        except Exception:  # noqa: BLE001 - an unreadable lineage is a refusal, never an admit
            raise Conflict(
                f"Stage {stage.key!r}: licence labels of its inputs could not be determined for "
                f"provider {stage.provider!r}; refused (fail closed)."
            ) from None
        reason = license_labels.license_refusal(stage.provider, labels, policy)
        if reason:
            raise LicenseRefused(f"Stage {stage.key!r}: {reason}", labels)
        return frozenset(labels)

    @staticmethod
    def _check_worker(
        connection: sqlite3.Connection, live: dict[str, Any], project_id: str, worker_id: str
    ) -> None:
        try:
            worker = Store._live_principal(
                connection, {"id": worker_id, "org_id": live["org_id"]}, audience="worker"
            )
        except Forbidden:
            raise Conflict("Campaign worker is not registered.") from None
        if (
            worker["kind"] != "worker"
            or worker["role"] != "agent"
            or worker["project_id"] != project_id
        ):
            raise Conflict("Campaign worker is not authorized for this project.")

    def _check_references(
        self,
        connection: sqlite3.Connection,
        live: dict[str, Any],
        project_id: str,
        spec: Any,
        campaign_id: str | None = None,
    ) -> None:
        normal = normalize(spec)
        # ADR 0060: every approved candidate passes the checks the stage's own provider does,
        # here and again whenever the runner places the stage.
        candidates = [item for stage in normal.stages for item in placement_candidates(stage)]
        providers: dict[tuple[str, str], Any] = {}
        for stage in candidates:
            try:
                provider = providers[stage.key, stage.provider] = stage_provider(stage.provider)
            except KeyError:
                raise Conflict(
                    f"Stage {stage.key!r} names unknown compute provider {stage.provider!r}."
                ) from None
            if stage.scratch_bytes is not None and not getattr(provider, "grants_scratch", False):
                raise Conflict(
                    f"Stage {stage.key!r}: provider {stage.provider!r} does not grant a "
                    "scratch request (scratch_bytes applies to worker-grant CPU providers)."
                )
            provider.check_stage(connection, project_id, stage)
            self.check_grant_lifetime(provider, stage)
            try:
                provider_trust.check_narrowing(
                    stage.provider,
                    None if stage.send_classes is None else list(stage.send_classes),
                    self.stage_worker(provider, stage),
                    connection=connection,
                )
            except ValueError as error:
                raise Conflict(f"Stage {stage.key!r}: {error}") from None
        if normal.worker_principal_id is not None:
            self._check_worker(connection, live, project_id, normal.worker_principal_id)
        digest = provider_trust.current_policy(connection).digest
        project = {"org_id": live["org_id"], "project_id": project_id}
        for stage in normal.stages:
            for source in stage.inputs:
                if source.kind == "artifact":
                    resolve_artifact(connection, project, source)
                if source.kind != "snapshot":
                    continue  # stage, public_artifact (ADR 0097): no snapshot admission
                snapshot = SnapshotStore._snapshot(connection, live, source.snapshot_id)
                if snapshot["project_id"] != project_id or snapshot["digest"] != source.digest:
                    raise Conflict("Campaign input snapshot does not match its pinned digest.")
                if (
                    source.data_class == "public"
                    and snapshot_data_class(connection, source.snapshot_id)["data_class"]
                    != "public"
                ):
                    raise Conflict(
                        "Campaign input snapshot is not recorded public (ADR 0039) "
                        "and cannot be declared public."
                    )
            self._check_code_input(connection, stage)
            self._check_build_context(connection, stage)
            for candidate in placement_candidates(stage):
                self.check_stage_inputs(
                    connection,
                    candidate,
                    providers[stage.key, candidate.provider],
                    digest,
                    spec=normal,
                    scope=(live["org_id"], project_id),
                )
            self._check_reroutes(
                connection,
                project_id,
                stage,
                digest,
                spec=normal,
                scope=(live["org_id"], project_id),
            )
        try:
            # ADR 0068: a sealed partition opens once per scope; a claimed one is refused here
            # as well as at the claim itself.
            check_unclaimed(connection, project_id, spec, campaign_id)
        except ValueError as error:
            raise Conflict(str(error)) from None

    @staticmethod
    def _check_code_input(connection: sqlite3.Connection, stage: Any) -> None:
        """ADR 0095: a stage's code input is a non-empty pinned snapshot whose code approval, if
        it has one, still matches its files. Trust and destination checks are the ordinary
        input checks (``check_stage_inputs``); this only refuses a bundle that cannot be code."""
        if stage.code_input is None:
            return
        from .code_approval import _entries, snapshot_code_approval

        source = next(
            (i for i in stage.inputs if i.name == stage.code_input and i.kind == "snapshot"), None
        )
        if source is None:  # validate_structure refuses this; fail closed if it ever slips
            raise Conflict(f"Stage {stage.key!r}: code_input is not a pinned snapshot input.")
        try:
            files = _entries(connection, source.snapshot_id)
        except (ValueError, TypeError, KeyError):
            files = []
        if not files:
            raise Conflict(
                f"Stage {stage.key!r}: code_input {stage.code_input!r} holds no files, so it "
                "cannot carry the stage's code."
            )
        approval = snapshot_code_approval(connection, source.snapshot_id)
        if approval is not None and approval.get("valid") is not True:
            raise Conflict(
                f"Stage {stage.key!r}: code_input {stage.code_input!r} has a code approval that "
                "does not match its files."
            )

    @staticmethod
    def _check_build_context(connection: sqlite3.Connection, stage: Any) -> None:
        """ADR 0123: an image-build stage's context is baked into layers that are pushed to a
        registry, so it must be a snapshot recorded public with no private-code approval, and the
        Dockerfile must be one of its files. Registry and trust are checked elsewhere."""
        if stage.build is None:
            return
        from .code_approval import _entries, snapshot_code_approval

        label = f"Stage {stage.key!r}: build context {stage.build['context_input']!r}"
        source = next(
            (
                i
                for i in stage.inputs
                if i.name == stage.build["context_input"] and i.kind == "snapshot"
            ),
            None,
        )
        if source is None:  # validate_structure refuses this; fail closed if it ever slips
            raise Conflict(f"{label} is not a pinned snapshot input.")
        recorded = snapshot_data_class(connection, source.snapshot_id)["data_class"]
        if recorded != "public":
            raise Conflict(
                f"{label} is recorded {recorded}, not public: a build context is pushed inside "
                "the image and is never private data."
            )
        if snapshot_code_approval(connection, source.snapshot_id) is not None:
            raise Conflict(
                f"{label} has a private code approval: approved private code is never baked "
                "into a pushed image."
            )
        try:
            files = {entry["relative_path"] for entry in _entries(connection, source.snapshot_id)}
        except (ValueError, TypeError, KeyError):
            files = set()
        if stage.build["dockerfile"] not in files:
            raise Conflict(
                f"{label} holds no file {stage.build['dockerfile']!r} to use as the Dockerfile."
            )

    def _check_reroutes(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        stage: Any,
        digest: Any,
        *,
        spec: Any = None,
        scope: tuple[str, str] | None = None,
    ) -> None:
        """Every provider an infrastructure retry may move to is checked as the stage's own
        is, at submit and approval (ADR 0058, ADR 0047)."""
        for provider_id in stage.retry.reroute_providers:
            self.check_reroute(
                connection, project_id, stage, provider_id, digest, spec=spec, scope=scope
            )

    @staticmethod
    def check_reroute(
        connection: sqlite3.Connection,
        project_id: str,
        stage: Any,
        provider_id: str,
        digest: Any,
        *,
        spec: Any = None,
        scope: tuple[str, str] | None = None,
    ) -> Any:
        """One reroute target must be registered, an unbilled worker-grant provider of the
        stage's runtime, pass the provider's own stage check and the trust narrowing, and be
        trusted to receive the stage's inputs. Raises Conflict naming what failed; returns
        the stage as it would run there. The runner repeats it when it reserves the retry, so
        a registry or trust change after approval is honoured."""
        try:
            provider = stage_provider(provider_id)
        except KeyError:
            raise Conflict(
                f"Stage {stage.key!r}: reroute provider {provider_id!r} is not registered."
            ) from None
        descriptor = provider.descriptor
        if (
            descriptor.kind != "worker_grant"
            or descriptor.billable
            or stage.runtime not in descriptor.runtimes
        ):
            raise Conflict(
                f"Stage {stage.key!r}: reroute provider {provider_id!r} cannot run this "
                "stage (it must be an unbilled worker-grant provider of the same runtime)."
            )
        moved = dataclasses.replace(
            stage, provider=provider_id, worker_principal_id=None, provider_config={}
        )
        provider.check_stage(connection, project_id, moved)
        try:
            provider_trust.check_narrowing(
                provider_id,
                None if moved.send_classes is None else list(moved.send_classes),
                CampaignStore.stage_worker(provider, moved),
                connection=connection,
            )
        except ValueError as error:
            raise Conflict(f"Stage {stage.key!r}: {error}") from None
        CampaignStore.check_stage_inputs(
            connection, moved, provider, digest, spec=spec, scope=scope
        )
        return moved

    # -- commands -------------------------------------------------------------------

    @staticmethod
    def _resolve_image_refs(connection, live, project_id, spec, predecessors):
        """ADR 0123: ``image_from`` becomes the literal built image, before the Salad adapter
        expansion reads the image and before the spec is digested. Returns the spec and the
        predecessor names it used."""
        if getattr(spec, "version", None) != 2 or not any(
            stage.image_from is not None for stage in spec.stages
        ):
            return spec, set()
        value = spec.model_dump(mode="json")
        resolved, used = resolve_image_refs(
            connection, {"org_id": live["org_id"], "project_id": project_id}, value, predecessors
        )
        try:
            return parse_spec(resolved), used
        except ValidationError as error:
            raise _invalid(error) from None

    @staticmethod
    def _resolve_receipt_refs(
        connection,
        live,
        project_id,
        spec,
        predecessors,
        raw=None,
        used_predecessors=(),
        rerun=False,
    ):
        if raw is not None:
            refuse_submitted_lineage(raw, allow_image_lineage=rerun)
        value = spec.model_dump(mode="json")
        resolved = resolve_receipt_refs(
            connection, {"org_id": live["org_id"], "project_id": project_id}, value, predecessors
        )
        # A predecessor must be named by some input or stage ``image_from``; one nothing
        # references is refused, whether or not another predecessor was used.
        referenced = set(used_predecessors) | {
            item["predecessor"]
            for stage in value.get("stages", [])
            for item in stage.get("inputs", [])
            if item.get("kind") == "receipt_output" and item.get("predecessor") is not None
        }
        if set(predecessors) - referenced:
            raise Conflict("predecessors were given but no input references one.")
        if resolved == value:
            return spec
        try:
            return parse_spec(resolved)
        except ValidationError as error:
            raise _invalid(error) from None

    def submit_campaign(
        self, principal, project_id, key, digest, payload, *, lineage=None
    ) -> tuple[int, dict]:
        try:
            body = SubmitCampaign.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None
        raw_spec = payload.get("spec") if isinstance(payload, dict) else None

        def command(connection, live):
            self._project(connection, live, project_id)
            self._require_action(connection, live, "campaign:submit")
            if live["role"] not in {"human", "agent"}:
                raise Forbidden("This credential cannot submit campaigns.")
            # ADR 0076/0077: a Salad adapter request becomes the explicit candidate before the spec
            # is digested; the identity for a spec without one.
            # ADR 0123: image_from becomes the built image before expansion and the digest.
            image_spec, used_predecessors = self._resolve_image_refs(
                connection, live, project_id, body.spec, body.predecessors
            )
            spec = expand_with_registered(
                image_spec,
                self.adapter_entries(connection, live, project_id),
                tenant=(live["org_id"], project_id),
            )
            # ADR 0080: receipt_output references become pinned artifact inputs before the
            # spec is digested, so approval binds the exact bytes and lineage.
            spec = self._resolve_receipt_refs(
                connection,
                live,
                project_id,
                spec,
                body.predecessors,
                raw_spec,
                used_predecessors,
                rerun=lineage is not None,
            )
            normal_spec = normalize(spec)
            smoke_credentials.check_submission(connection, live, normal_spec)
            encoded_spec = json.dumps(spec.model_dump(mode="json"), sort_keys=True)
            spec_hash = spec_digest(spec)
            self._check_references(connection, live, project_id, spec)
            if lineage is not None:
                # The tree's budget is re-read here, under the write lock, so two re-runs planned
                # against the same state cannot both fit (ADR 0074): the plan's check gives the
                # good message, this one is the bound.
                parent = connection.execute(
                    "SELECT * FROM campaigns WHERE id=?", (lineage["rerun_of"],)
                ).fetchone()
                left = 0 if parent is None else lineage_remaining(connection, parent)
                if spec.budget_cap_cents > left:
                    raise Conflict(
                        f"The tree's remaining budget ({max(left, 0)} cents) no longer covers this "
                        f"re-run's cap of {spec.budget_cap_cents} cents: another re-run was "
                        "submitted since it was planned."
                    )
            now, ident = utc_now(), "campaign_" + uuid.uuid4().hex
            campaign = {
                "id": ident,
                "org_id": live["org_id"],
                "project_id": project_id,
                "spec_json": encoded_spec,
                "spec_digest": spec_hash,
                "status": "submitted",
                "stop_reason": None,
                "budget_cap_cents": spec.budget_cap_cents,
                "held_cents": 0,
                "settled_cents": 0,
                "revision": 1,
                "submitted_by_principal_id": live["id"],
                "submitted_credential_id": live["credential_id"],
                "created_at": now,
                "updated_at": now,
            }
            connection.execute(
                """INSERT INTO campaigns VALUES (:id,:org_id,:project_id,:spec_json,:spec_digest,
                :status,:stop_reason,:budget_cap_cents,:held_cents,:settled_cents,:revision,
                :submitted_by_principal_id,:submitted_credential_id,:created_at,:updated_at)""",
                campaign,
            )
            for index, stage_key in enumerate(topological_order(spec)):
                connection.execute(
                    """INSERT INTO campaign_stages (campaign_id, stage_key, topo_index, status,
                    prep_phase, input_data_class, output_data_class, revision, updated_at,
                    provider_id) VALUES (?, ?, ?, 'pending', 'none', ?, ?, 1, ?, ?)""",
                    (
                        ident,
                        stage_key,
                        index,
                        stage_input_class(spec, stage_key),
                        stage_output_class(spec, stage_key),
                        now,
                        normal_spec.stage(stage_key).provider,
                    ),
                )
            self._event(
                connection,
                live,
                project_id,
                None,
                "campaign.submitted",
                1,
                {
                    "campaign_id": ident,
                    "spec_digest": spec_hash,
                    "stage_keys": [stage.key for stage in spec.stages],
                },
            )
            if lineage is not None:
                # The link to the campaign this one re-runs, in the same transaction (ADR 0069).
                self._event(
                    connection,
                    live,
                    project_id,
                    None,
                    RERUN_EVENT,
                    1,
                    {"campaign_id": ident, **lineage},
                )
            return 201, {
                **_summary(campaign),
                "spec": spec.model_dump(mode="json"),
                "infra_retry_policy": infra_retry_policy(spec),
                **({} if lineage is None else {"rerun": lineage}),
            }

        return self._mutate(
            principal, key, "POST", f"/projects/{project_id}/campaigns", digest, command
        )

    def rerun_campaign(self, principal, campaign_id, key, digest, payload) -> tuple[int, dict]:
        """Submit a new campaign that re-runs a finished one's failed stages (or its identical
        spec) and reuses its succeeded outputs (ADR 0069). It is an ordinary submission: it
        grants nothing and a person approves the new digest."""
        try:
            request = RerunCampaign.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None
        connection = _connect(self.database_path)
        try:
            actor = self._read_actor(connection, principal)
            self._require_action(connection, actor, "campaign:submit")
            if request.mode == "failed_stages":
                # Reusing outputs hands the caller their digests (in the pins and the receipt):
                # the same scope the offline retry tool needs to read them.
                self._require_action(connection, actor, "artifact:read")
            old = self._campaign(connection, actor, campaign_id)
            project_id = old["project_id"]
            request_digest = canonical_digest(payload)
            stored = connection.execute(
                """SELECT request_digest, response_json FROM idempotency WHERE principal_id=?
                AND org_id=? AND idempotency_key=? AND method='POST' AND path=?""",
                (actor["id"], actor["org_id"], key, f"/projects/{project_id}/campaigns"),
            ).fetchone()
            source = None if stored is not None else load_source(connection, old)
        finally:
            connection.close()
        if stored is not None:
            earlier = json.loads(stored["response_json"]).get("rerun") or {}
            if (
                earlier.get("rerun_of") == campaign_id
                and earlier.get("request_digest") == request_digest
            ):
                # The same request again: answer from the record without planning, so a custody
                # or output change since then cannot turn a replay into a refusal.
                def refuse(connection, live):
                    raise Conflict("The recorded re-run could not be replayed.")

                return self._mutate(
                    principal,
                    key,
                    "POST",
                    f"/projects/{project_id}/campaigns",
                    stored["request_digest"],
                    refuse,
                )
            source = None
            connection = _connect(self.database_path)
            try:
                source = load_source(connection, self._campaign(connection, actor, campaign_id))
            finally:
                connection.close()
        try:
            plan = plan_rerun(source, request)
        except RerunRefused as error:
            raise Conflict(str(error)) from None
        plan["lineage"]["request_digest"] = request_digest
        try:
            body = SubmitCampaign.model_validate({"spec": plan["spec"]}).model_dump(mode="json")
        except ValidationError as error:
            raise Conflict("The re-run spec is not valid: " + str(_invalid(error))) from None
        # Digested as the submit route digests its body, so a replay and a plain submit agree.
        return self.submit_campaign(
            principal, project_id, key, canonical_digest(body), body, lineage=plan["lineage"]
        )

    def approve_campaign(self, principal, campaign_id, key, digest, payload) -> tuple[int, dict]:
        try:
            body = ApproveCampaign.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            campaign = self._campaign(connection, live, campaign_id)
            if live["role"] != "human":
                raise Forbidden("A human must approve a campaign.")
            require_decision_actor(live, forbidden=Forbidden)
            # A key already used to approve another campaign is a client bug (a batch helper
            # reusing one key): refuse it loudly instead of letting the second approval pass
            # as if it were the first. The same key and campaign with another body is refused
            # by the idempotency check itself (409).
            reused = connection.execute(
                """SELECT 1 FROM idempotency WHERE principal_id=? AND org_id=?
                AND idempotency_key=? AND method='POST' AND path LIKE '/campaigns/%/approve'
                AND path<>? LIMIT 1""",
                (live["id"], live["org_id"], key, f"/campaigns/{campaign_id}/approve"),
            ).fetchone()
            if reused is not None:
                raise Conflict(
                    "Idempotency key was already used to approve a different campaign; "
                    "use a fresh key for each approval."
                )
            if (
                campaign["revision"] != body.expected_revision
                or campaign["spec_digest"] != body.expected_spec_digest
            ):
                raise Conflict("Campaign revision or spec digest is no longer current.")
            if campaign["status"] not in REAPPROVABLE_STATUSES:
                raise Conflict("Campaign is not awaiting approval.")
            forward = campaign["status"] in FORWARD_APPROVAL_STATUSES
            try:
                # Provider validation applies to a forward approval only; a cleanup
                # re-approval must not depend on the provider registry.
                spec = parse_spec_json(campaign["spec_json"], providers=forward)
            except ValidationError:
                raise Conflict("Stored campaign spec failed validation.") from None
            if spec_digest(spec) != campaign["spec_digest"]:
                raise Conflict("Stored campaign spec digest changed.")
            smoke_grant_id = smoke_credentials.check_approval(
                connection, live, campaign, normalize(spec)
            )
            if forward:
                # Cleanup re-approvals must not depend on forward-only references (a retired
                # worker or snapshot must not strand a cancel).
                self._check_references(
                    connection, live, campaign["project_id"], spec, campaign_id=campaign["id"]
                )
            unlock_evaluations: list[dict[str, Any]] = []
            if body.unlock and campaign["status"] != "submitted":
                # Never accept a stated condition and then drop it (ADR 0045).
                raise Conflict("An unlock applies to a campaign's first approval only.")
            if campaign["status"] == "submitted":
                # First admission (ADR 0045): the program cap and any stated unlock
                # requirement are checked in this transaction; a refusal writes nothing.
                refusal = program_budget.check_admission(
                    connection, campaign["project_id"], campaign["budget_cap_cents"]
                )
                if refusal is None and body.unlock:
                    refusal, unlock_evaluations = evaluate_unlock(
                        connection, campaign["org_id"], campaign["project_id"], body.unlock
                    )
                if refusal is not None:
                    raise Conflict(f"Admission refused: {refusal}")
            now_dt = _parse_utc(utc_now())
            now = _utc_string(now_dt)
            superseded = connection.execute(
                "SELECT id FROM campaign_approvals WHERE campaign_id=? AND status='active'",
                (campaign_id,),
            ).fetchone()
            if superseded is not None:
                connection.execute(
                    "UPDATE campaign_approvals SET status='superseded', ended_at=? WHERE id=?",
                    (now, superseded["id"]),
                )
                # A pending intent committed nothing; the runner re-issues it.
                connection.execute(
                    """UPDATE campaign_step_intents SET status='abandoned', completed_at=?
                    WHERE approval_id=? AND status='pending'""",
                    (now, superseded["id"]),
                )
            expires_dt = now_dt + timedelta(seconds=spec.deadline_seconds)
            if forward:
                # Re-approval never extends the campaign's wall-clock bound: it ends
                # ``deadline_seconds`` after the first approval. Cleanup re-approvals are
                # reduce-only and keep a full window so a cancel is never stranded.
                first = connection.execute(
                    "SELECT MIN(approved_at) AS at FROM campaign_approvals WHERE campaign_id=?",
                    (campaign_id,),
                ).fetchone()["at"]
                if first is not None:
                    bound = _parse_utc(first) + timedelta(seconds=spec.deadline_seconds)
                    if bound <= now_dt:
                        raise Conflict("The campaign deadline has passed; submit a new campaign.")
                    expires_dt = min(expires_dt, bound)
            held_until = parse_not_before(getattr(spec, "not_before", None))
            if held_until is not None and held_until >= expires_dt:
                # The approval would lapse before the campaign may start, so it could never run.
                raise Conflict(
                    "not_before is not before this approval's expiry "
                    f"({_utc_string(expires_dt)}); the campaign could never start. Submit it "
                    "with an earlier not_before or a longer deadline_seconds."
                )
            provenance = decision_provenance(live)
            approval = {
                "id": "approval_" + uuid.uuid4().hex,
                "campaign_id": campaign_id,
                "spec_digest": campaign["spec_digest"],
                "approving_principal_id": live["id"],
                "decision_class": provenance["decision_class"],
                "automation_label": provenance.get("automation_label"),
                "approved_at": now,
                "expires_at": _utc_string(expires_dt),
                "status": "active",
                "ended_at": None,
                # The setting this approval was checked against; enforcement points use the
                # intersection of it, the current setting and the stage's send_classes.
                "trust_policy_digest": provider_trust.current_policy(connection).digest,
            }
            connection.execute(
                """INSERT INTO campaign_approvals (id, campaign_id, spec_digest,
                approving_principal_id, decision_class, automation_label, approved_at,
                expires_at, status, ended_at, trust_policy_digest) VALUES (:id,:campaign_id,
                :spec_digest,:approving_principal_id,:decision_class,:automation_label,
                :approved_at,:expires_at,:status,:ended_at,:trust_policy_digest)""",
                approval,
            )
            record_evaluations(
                connection,
                org_id=campaign["org_id"],
                project_id=campaign["project_id"],
                campaign_id=campaign_id,
                approval_id=approval["id"],
                standing_approval_id=None,
                evaluations=unlock_evaluations,
                now=now,
            )
            declared = self._insert_preapprovals(
                connection, campaign, spec, approval, body.preapprovals, forward
            )
            revision = campaign["revision"] + 1
            # Re-approval never resumes forward work after cancel (or while blocked).
            resulting = "running" if forward else campaign["status"]
            connection.execute(
                "UPDATE campaigns SET status=?, revision=?, updated_at=? WHERE id=?",
                (resulting, revision, now, campaign_id),
            )
            self._event(
                connection,
                live,
                campaign["project_id"],
                None,
                "campaign.approved",
                revision,
                {
                    "campaign_id": campaign_id,
                    "approval_id": approval["id"],
                    "spec_digest": campaign["spec_digest"],
                    "expires_at": approval["expires_at"],
                    "superseded_approval_id": superseded["id"] if superseded else None,
                    "resulting_status": resulting,
                    "preapprovals": declared,
                    **({"smoke_grant_id": smoke_grant_id} if smoke_grant_id else {}),
                },
            )
            return 200, {
                **_summary(campaign),
                "status": resulting,
                "revision": revision,
                "updated_at": now,
                "approval": approval,
            }

        return self._mutate(
            principal, key, "POST", f"/campaigns/{campaign_id}/approve", digest, command
        )

    @staticmethod
    def _insert_preapprovals(connection, campaign, spec, approval, entries, forward) -> list[dict]:
        """Store the approve body's Salad review pre-approvals (ADR 0044), fail closed.

        Only a forward approval may carry them; each names a distinct Salad stage of the
        approved spec; nothing carries over from a superseded approval."""
        if not entries:
            return []
        if not forward:
            raise Conflict("A cleanup re-approval cannot carry review pre-approvals.")
        from .campaign_models import normalize, stage_on_candidate, stage_provider

        normal = normalize(spec)
        cap = approval["expires_at"]
        declared, seen = [], set()
        for entry in entries:
            if entry.stage_key in seen:
                raise Conflict("Duplicate pre-approval stage.")
            seen.add(entry.stage_key)
            try:
                stage = normal.stage(entry.stage_key)
            except (StopIteration, ValueError, KeyError):
                raise Conflict("Pre-approval names an unknown stage.") from None
            # The stage's own provider, or one Salad alternate it names (ADR 0066). Only a
            # candidate of the approved spec is reachable, and only while it is registered.
            wanted = entry.provider or stage.provider
            if wanted != stage.provider and wanted not in {a.provider for a in stage.alternates}:
                raise Conflict("Pre-approval provider is not a candidate of that stage.")
            try:
                billable = stage_provider(wanted).descriptor.kind == "salad_route"
            except KeyError:
                raise Conflict("Pre-approval provider is not registered.") from None
            if not billable:
                raise Conflict("Pre-approvals apply to Salad stages only.")
            stage = stage_on_candidate(stage, wanted)
            if entry.max_total_cents < entry.max_attempt_cents:
                raise Conflict("max_total_cents must be at least max_attempt_cents.")
            if entry.max_total_cents > normal.budget_cap_cents:
                raise Conflict("max_total_cents exceeds the campaign's approved budget cap.")
            if entry.max_attempt_cents < stage.max_exposure_cents:
                raise Conflict("max_attempt_cents is below the stage's max_exposure_cents.")
            expires = cap
            if entry.expires_at is not None:
                try:
                    requested = _utc_string(_parse_utc(entry.expires_at))
                except ValueError:
                    raise Conflict("Pre-approval expires_at is not a UTC timestamp.") from None
                expires = min(expires, requested)  # never extended past the approval
            row = {
                "id": "preapproval_" + uuid.uuid4().hex,
                "campaign_id": campaign["id"],
                "approval_id": approval["id"],
                "stage_key": entry.stage_key,
                "source_digest": entry.source_assessment_digest,
                "plan_digest": entry.plan_digest,
                "source_use_digest": entry.source_use_digest,
                "max_attempt_cents": entry.max_attempt_cents,
                "max_total_cents": entry.max_total_cents,
                "expires_at": expires,
                "created_at": approval["approved_at"],
            }
            connection.execute(
                """INSERT INTO campaign_review_preapprovals (id, campaign_id, approval_id,
                stage_key, source_digest, plan_digest, source_use_digest, max_attempt_cents,
                max_total_cents, expires_at, created_at) VALUES (:id,:campaign_id,:approval_id,
                :stage_key,:source_digest,:plan_digest,:source_use_digest,:max_attempt_cents,
                :max_total_cents,:expires_at,:created_at)""",
                row,
            )
            declared.append(
                {k: v for k, v in row.items() if k != "created_at"} | {"provider_id": wanted}
            )
        return declared

    def cancel_campaign(self, principal, campaign_id, key, digest, payload) -> tuple[int, dict]:
        try:
            body = CancelCampaign.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            campaign = self._campaign(connection, live, campaign_id)
            if self._external_scope(connection, live):
                self._scope(connection, live, "campaign:cancel")
                if campaign["submitted_by_principal_id"] != live["id"]:
                    raise Forbidden("Only the submitting agent may cancel this campaign.")
            elif live["role"] != "human":
                raise Forbidden("A human must cancel a campaign.")
            if campaign["status"] in TERMINAL_CAMPAIGN:
                raise Conflict("Campaign is already finished.")
            if campaign["status"] == "cancel_requested":
                return 200, _summary(campaign)
            if campaign["revision"] != body.expected_revision:
                raise Conflict("Campaign revision is no longer current.")
            now, revision = utc_now(), campaign["revision"] + 1
            # A campaign never approved has no effects to unwind, so it ends here.
            status = "cancelled" if campaign["status"] == "submitted" else "cancel_requested"
            reason = ("cancel: " + body.reason)[:300]
            connection.execute(
                "UPDATE campaigns SET status=?, stop_reason=?, revision=?, updated_at=? WHERE id=?",
                (status, reason, revision, now, campaign_id),
            )
            if status == "cancelled":
                connection.execute(
                    """UPDATE campaign_stages SET status='cancelled', revision=revision+1,
                    updated_at=? WHERE campaign_id=? AND status='pending'""",
                    (now, campaign_id),
                )
            revoked = self._revoke_unleased_grants(connection, live, campaign_id)
            self._event(
                connection,
                live,
                campaign["project_id"],
                None,
                "campaign.cancel_requested",
                revision,
                {
                    "campaign_id": campaign_id,
                    "reason": body.reason,
                    "resulting_status": status,
                    "requested_status": campaign["status"],
                    "revoked_grant_ids": revoked,
                },
            )
            return 200, {
                **_summary(campaign),
                "status": status,
                "stop_reason": reason,
                "revision": revision,
                "updated_at": now,
            }

        return self._mutate(
            principal, key, "POST", f"/campaigns/{campaign_id}/cancel", digest, command
        )

    def _revoke_unleased_grants(
        self, connection: sqlite3.Connection, live: dict[str, Any], campaign_id: str
    ) -> list[str]:
        """Fence this campaign's active, never-leased grants in the cancel transaction.

        Same transition and events as a human revoke; a leased grant is left for the
        runner's unwind, which needs observation before it can settle.
        """
        revoked: list[str] = []
        executor = ExecutionStore(self.database_path.parent)
        rows = connection.execute(
            """SELECT DISTINCT g.id, g.revision FROM campaign_stage_attempts a
            JOIN execution_grants g ON g.id = a.grant_id
            WHERE a.campaign_id = ? AND g.status = 'active'
            AND NOT EXISTS (SELECT 1 FROM execution_leases l WHERE l.grant_id = g.id)
            ORDER BY g.id""",
            (campaign_id,),
        ).fetchall()
        for row in rows:
            executor._cancel_grant_transaction(connection, live, row["id"], row["revision"])
            revoked.append(row["id"])
        return revoked

    # -- reads ----------------------------------------------------------------------

    def _read_actor(self, connection, principal) -> dict[str, Any]:
        actor = self._actor(connection, principal)
        self._require_action(connection, actor, "campaign:read")
        return actor

    def get_campaign(self, principal, campaign_id) -> dict:
        connection = _connect(self.database_path)
        try:
            actor = self._read_actor(connection, principal)
            campaign = self._campaign(connection, actor, campaign_id)
            approvals = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM campaign_approvals WHERE campaign_id=? ORDER BY approved_at, id",
                    (campaign_id,),
                )
            ]
            stages = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM campaign_stages WHERE campaign_id=? ORDER BY topo_index",
                    (campaign_id,),
                )
            ]
            # ADR 0107: the latest heavy-stage placement warning or refusal per stage (read-only).
            for entry in heavy_guard_readout.status_view(
                connection, campaign["project_id"], campaign_id
            ):
                for stage in stages:
                    if stage["stage_key"] == entry["stage_key"]:
                        stage["heavy_guard"] = {k: v for k, v in entry.items() if k != "stage_key"}
            attempts = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM campaign_stage_attempts WHERE campaign_id=? "
                    "ORDER BY created_at, stage_key, attempt",
                    (campaign_id,),
                )
            ]
            counts = {
                row["status"]: row["n"]
                for row in connection.execute(
                    "SELECT status, count(*) AS n FROM campaign_step_intents "
                    "WHERE campaign_id=? GROUP BY status",
                    (campaign_id,),
                )
            }
            intents = [
                dict(row)
                for row in connection.execute(
                    """SELECT id, approval_id, stage_key, attempt, step, ordinal, status,
                    idempotency_key, response_status, error_class, created_at, completed_at
                    FROM campaign_step_intents WHERE campaign_id=? ORDER BY created_at, id
                    LIMIT ?""",
                    (campaign_id, MAX_LISTED_INTENTS),
                )
            ]
            rerun_of = lineage_of(connection, campaign_id, campaign["project_id"])
            reruns = reruns_of(connection, campaign_id, campaign["project_id"])
            tree = rerun_tree(connection, campaign) if (rerun_of or reruns) else None
            return {
                **_read_summary(campaign),
                **({} if rerun_of is None else {"rerun": rerun_of}),
                **({} if not reruns else {"reruns": reruns}),
                # ADR 0074: the re-run tree's first cap, what it has committed, what is left.
                **({} if tree is None else {"rerun_tree": tree}),
                "spec": json.loads(campaign["spec_json"]),
                "infra_retry_policy": infra_retry_policy(parse_spec_json(campaign["spec_json"])),
                "approval": next((a for a in approvals if a["status"] == "active"), None),
                "approvals": approvals,
                "stages": stages,
                "attempts": attempts,
                "intents": {"counts": counts, "items": intents},
                # ADR 0060: where each stage with candidates was placed and why others were
                # skipped, its steering and, while it waits, the reasons.
                "placement": campaign_placement.status_view(connection, campaign),
                # ADR 0070: the operator's placement preference and the stages it covers.
                "placement_preference": campaign_placement.preference_view(campaign),
                # ADR 0068: one-time holdout claims this campaign opened, with scored values.
                "holdout_claims": campaign_claims(connection, campaign_id),
                # Audit G-11: what a person still has to decide; empty once nothing is awaited.
                "pending_human_approval": pending_human_approval(connection, campaign),
            }
        finally:
            connection.close()

    def list_campaigns(self, principal, project_id, offset=0, limit=50) -> dict:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            actor = self._read_actor(connection, principal)
            self._project(connection, actor, project_id)
            rows = connection.execute(
                """SELECT * FROM campaigns WHERE project_id=? AND org_id=?
                ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (project_id, actor["org_id"], limit, offset),
            ).fetchall()
            return {"campaigns": [_read_summary(row) for row in rows]}
        finally:
            connection.close()

    def get_campaign_receipt(self, principal, campaign_id) -> dict:
        connection = _connect(self.database_path)
        try:
            actor = self._read_actor(connection, principal)
            self._campaign(connection, actor, campaign_id)
            row = connection.execute(
                "SELECT * FROM campaign_receipts WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if row is None:
                raise NotFound("Campaign receipt was not found.")
            return {
                "campaign_id": campaign_id,
                "receipt": json.loads(row["receipt_json"]),
                "receipt_digest": row["receipt_digest"],
                "created_at": row["created_at"],
            }
        finally:
            connection.close()

    def get_campaign_report(self, principal, campaign_id, *, detail: bool = False) -> dict:
        """The plain-words report of one campaign, from its receipt data (campaign:read)."""
        from . import campaign_report

        connection = _connect(self.database_path)
        try:
            actor = self._read_actor(connection, principal)
            self._campaign(connection, actor, campaign_id)
            report = campaign_report.build_report(connection, campaign_id, detail=detail)
            return {**report, "text": campaign_report.render_text(report)}
        finally:
            connection.close()

    def get_campaign_window_report(
        self, principal, project_id, since, until, *, detail: bool = False, limit: int = 100
    ) -> dict:
        """The plain-words report of the project's campaigns created in [since, until)."""
        from . import campaign_report

        if not 1 <= limit <= 200:
            raise StoreError("Invalid report bounds.")
        for value in (since, until):
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (AttributeError, ValueError):
                raise StoreError("since and until must be ISO-8601 timestamps.") from None
        connection = _connect(self.database_path)
        try:
            actor = self._read_actor(connection, principal)
            self._project(connection, actor, project_id)
            report = campaign_report.window_report(
                connection, project_id, since, until, detail=detail, limit=limit
            )
            return {**report, "text": campaign_report.render_text(report)}
        finally:
            connection.close()

    # -- runner side (never exposed over HTTP) --------------------------------------

    @contextlib.contextmanager
    def runner_connection(self) -> Iterator[sqlite3.Connection]:
        """One IMMEDIATE transaction for the runner; refuses while restore state is held."""
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            recovery = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
            if recovery is None or recovery["state"] != "active":
                raise Forbidden("Control state is held after restore; mutations are disabled.")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def runnable_campaigns(self, connection: sqlite3.Connection) -> list[dict]:
        marks = ",".join("?" for _ in RUNNABLE_STATUSES)
        return [
            dict(row)
            for row in connection.execute(
                f"SELECT * FROM campaigns WHERE status IN ({marks}) ORDER BY created_at, id",
                RUNNABLE_STATUSES,
            )
        ]

    def record_runner_event(
        self,
        connection: sqlite3.Connection,
        campaign: Mapping,
        event_type: str,
        payload: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> None:
        """Append an event attributed to the active approver (else the submitter)."""
        approval = connection.execute(
            """SELECT approving_principal_id FROM campaign_approvals WHERE campaign_id=?
            ORDER BY (status='active') DESC, approved_at DESC, id DESC LIMIT 1""",
            (campaign["id"],),
        ).fetchone()
        actor_id = (
            approval["approving_principal_id"]
            if approval is not None
            else campaign["submitted_by_principal_id"]
        )
        current = connection.execute(
            "SELECT revision FROM campaigns WHERE id=?", (campaign["id"],)
        ).fetchone()
        self._event(
            connection,
            {"id": actor_id, "org_id": campaign["org_id"]},
            campaign["project_id"],
            task_id,
            event_type,
            current["revision"] if current is not None else campaign["revision"],
            {"campaign_id": campaign["id"], **payload},
        )
