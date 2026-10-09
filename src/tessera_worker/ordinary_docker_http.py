"""Closed schema-54 native HTTP calls, including original-worker readback."""

from __future__ import annotations

import json
from concurrent.futures import TimeoutError
from typing import Literal
from urllib.request import Request

from pydantic import Field

from .control_channel import ControlChannelError, HttpControlChannel
from .ordinary_docker_contract import DIGEST, IDENTIFIER, DockerLease, DockerPackage, Frozen


class DockerFence(Frozen):
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    admitted_attempt_id: str = Field(pattern=IDENTIFIER)
    lease_id: str = Field(pattern=IDENTIFIER)
    expected_epoch: int = Field(ge=1)
    package_digest: str = Field(pattern=DIGEST)
    target_digest: str = Field(pattern=DIGEST)

    @classmethod
    def from_package(cls, package):
        return cls(
            operation_id=package.operation_id,
            admitted_attempt_id=package.admitted_attempt_id,
            lease_id=package.lease_id,
            expected_epoch=package.epoch,
            package_digest=package.package_digest,
            target_digest=package.target_digest,
        )


class DockerDispatchRecord(Frozen):
    fence: DockerFence
    package: DockerPackage
    lease: DockerLease
    state: Literal["queued", "claimed", "running", "unknown"]


class DockerPoll(Frozen):
    dispatches: tuple[DockerDispatchRecord, ...] = Field(max_length=100)


class DockerClaimReceipt(Frozen):
    package: DockerPackage
    package_digest: str = Field(pattern=DIGEST)
    lease: DockerLease


class DockerAuthorityReceipt(Frozen):
    lease: DockerLease
    authority_current: Literal[True]


class DockerWireReport(DockerFence):
    container_id: str | None = Field(default=None, pattern=DIGEST)
    state: Literal["running", "succeeded", "failed", "cancelled", "unknown"]
    termination_confirmed: bool
    stop_requested: bool
    exit_code: int | None = Field(default=None, strict=True, ge=0, le=255)


class DockerReadbackRequest(DockerFence):
    expected_container_id: str | None = Field(default=None, pattern=DIGEST)
    delivery_key: str | None = Field(default=None, min_length=1, max_length=200)


class DockerReportReceipt(DockerWireReport):
    scientific_validity: Literal["not_assessed"]
    human_resolution_required: Literal[True]


class DockerDeliveryReceipt(Frozen):
    idempotency_key: str = Field(min_length=1, max_length=200)
    payload_digest: str = Field(pattern=DIGEST)
    response_status: Literal[200]
    response: DockerReportReceipt


class DockerReadbackReply(Frozen):
    fence: DockerFence
    task_id: str = Field(pattern=IDENTIFIER)
    grant_id: str = Field(pattern=IDENTIFIER)
    worker_principal_id: str = Field(pattern=IDENTIFIER)
    claimed_by_principal_id: str | None = Field(pattern=IDENTIFIER)
    container_id: str | None = Field(pattern=DIGEST)
    desired_state: Literal["admitted", "cancel_requested"]
    dispatch_state: Literal[
        "queued", "claimed", "running", "succeeded", "failed", "cancelled", "unknown"
    ]
    dispatch_revision: int = Field(ge=1)
    can_execute: bool
    authorization: Literal["execute", "stop_only"]
    effective_action: Literal["run", "stop"]
    lease: DockerLease
    latest_report: DockerWireReport | None
    latest_delivery: DockerDeliveryReceipt | None
    delivery_key: str | None = Field(min_length=1, max_length=200)
    delivery: DockerDeliveryReceipt | None


def _validated(model, value):
    try:
        return model.model_validate_json(json.dumps(value))
    except (ValueError, TypeError):
        raise ControlChannelError("native Docker receipt invalid") from None


class NativeDockerHttpClient:
    """Worker bearer fixed to one origin; no retries or credential discovery.

    Supply an existing authorized runtime token securely. Unit tests use synthetic
    credentials only. HTTPS uses the underlying channel's normal certificate
    validation; HTTP requires an explicit literal-loopback development opt-in.
    """

    def __init__(
        self, base_url, bearer_token, *, allow_loopback_http=False, timeout_seconds=2.0, opener=None
    ):
        origin = HttpControlChannel._origin(base_url)
        if origin.startswith("http:") and not allow_loopback_http:
            raise ControlChannelError("loopback HTTP requires development opt-in")
        self.channel = HttpControlChannel(
            origin, bearer_token, timeout_seconds=timeout_seconds, opener=opener
        )

    def _request(self, method, path, *, body=None, key=None):
        channel = self.channel
        # Only the closed poll query is allowed, never caller-controlled URLs.
        headers = {"Accept": "application/json", "Authorization": "Bearer " + channel.bearer_token}
        if key is not None:
            from .control_channel import _header_value

            headers["Idempotency-Key"] = _header_value(key, "Idempotency key")
        data = None if body is None else channel._body(body)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(channel.origin + path, data=data, headers=headers, method=method)
        future = channel._submit(request, channel.timeout_seconds)
        try:
            return future.result(timeout=channel.timeout_seconds)
        except TimeoutError:
            raise ControlChannelError("control response deadline exceeded") from None
        except ControlChannelError:
            raise
        except Exception:
            raise ControlChannelError("control transport unavailable") from None

    def poll(self, target_digest):
        import re

        if not isinstance(target_digest, str) or not re.fullmatch(DIGEST, target_digest):
            raise ControlChannelError("native Docker target invalid")
        result = _validated(
            DockerPoll,
            self._request("GET", "/worker/docker/dispatches?target_digest=" + target_digest),
        )
        seen = set()
        for row in result.dispatches:
            if row.fence != DockerFence.from_package(row.package) or (
                row.package.target_digest != target_digest or row.package.operation_id in seen
            ):
                raise ControlChannelError("native Docker poll identity mismatch")
            self._lease(row.package, row.lease)
            seen.add(row.package.operation_id)
        return result

    @staticmethod
    def _lease(package, lease):
        if (
            lease.id,
            lease.grant_id,
            lease.worker_principal_id,
            lease.epoch,
            lease.hard_expires_at,
        ) != (
            package.lease_id,
            package.grant_id,
            package.worker_principal_id,
            package.epoch,
            package.hard_expires_at,
        ):
            raise ControlChannelError("native Docker lease identity mismatch")

    def claim(self, package, key):
        # Caller must durably retain this package/fence/key before calling.
        result = _validated(
            DockerClaimReceipt,
            self._request(
                "POST",
                "/worker/docker/claim",
                body=DockerFence.from_package(package).model_dump(mode="json"),
                key=key,
            ),
        )
        if result.package != package or result.package_digest != package.package_digest:
            raise ControlChannelError("native Docker claim identity mismatch")
        self._lease(package, result.lease)
        return result

    def current_authority(self, package):
        result = _validated(
            DockerAuthorityReceipt,
            self._request(
                "POST",
                "/worker/docker/current-authority",
                body=DockerFence.from_package(package).model_dump(mode="json"),
            ),
        )
        self._lease(package, result.lease)
        return result.lease

    def readback(self, package, *, container_id=None, delivery_key=None):
        fence = DockerFence.from_package(package)
        body = _validated(
            DockerReadbackRequest,
            fence.model_dump(mode="json")
            | {
                "expected_container_id": container_id,
                "delivery_key": delivery_key,
            },
        ).model_dump(mode="json")
        result = _validated(
            DockerReadbackReply, self._request("POST", "/worker/docker/readback", body=body)
        )
        if result.fence != fence or (
            result.task_id != package.task_id
            or result.grant_id != package.grant_id
            or result.worker_principal_id != package.worker_principal_id
            or result.claimed_by_principal_id != package.worker_principal_id
            or result.delivery_key != delivery_key
            or container_id is not None
            and result.container_id != container_id
            or result.can_execute != (result.authorization == "execute")
            or result.effective_action != ("run" if result.can_execute else "stop")
        ):
            raise ControlChannelError("native Docker readback identity mismatch")
        self._lease(package, result.lease)
        for receipt in (result.latest_delivery, result.delivery):
            if receipt is not None:
                response = receipt.response.model_dump(
                    exclude={"scientific_validity", "human_resolution_required"}
                )
                from .ordinary_docker_contract import digest

                if DockerFence.model_validate(
                    {k: response[k] for k in DockerFence.model_fields}
                ) != fence or (
                    digest(response) != receipt.payload_digest
                    or receipt is result.delivery
                    and receipt.idempotency_key != delivery_key
                ):
                    raise ControlChannelError("native Docker readback receipt mismatch")
        return result

    def report(self, value: DockerWireReport, key):
        value = _validated(DockerWireReport, value.model_dump(mode="json"))
        result = _validated(
            DockerReportReceipt,
            self._request(
                "POST", "/worker/docker/report", body=value.model_dump(mode="json"), key=key
            ),
        )
        if result.model_dump(exclude={"scientific_validity", "human_resolution_required"}) != (
            value.model_dump()
        ):
            raise ControlChannelError("native Docker report echo mismatch")
        return result
