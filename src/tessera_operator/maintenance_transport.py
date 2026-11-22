"""Unregistered fixed quota dry-run adapter; never a general Kubernetes API.

Entry is the existing producer's claimed request, useful action is one dry-run,
result is bounded sanitized Status, exit closes stream/client. Reconnect belongs
to the producer's retained claim, never a transport retry. No native admission is
issued here; installation and EACH invocation need their existing owner gates.
"""

import json
import re
import time

from . import upgrade_support as target
from .maintenance_hold import NAME
from .schema_supervised import SupervisedDenied, digest

LIMIT = 65536


def fixed_request(request):
    """Validate before auth or I/O; return a detached exact request."""
    try:
        operation = request["body"]["metadata"]["annotations"]["tessera-maintenance-operation"]
        if not isinstance(operation, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,62}", operation
        ):
            raise ValueError()
        name = "tessera-quota-probe-" + digest(operation)[:16]
        expected = dict(
            method="POST",
            path=f"/api/v1/namespaces/{target.NAMESPACE}/pods",
            query={"dryRun": "All", "fieldValidation": "Strict"},
            body=dict(
                apiVersion="v1",
                kind="Pod",
                metadata=dict(
                    name=name,
                    namespace=target.NAMESPACE,
                    annotations={"tessera-maintenance-operation": operation},
                ),
                spec=dict(
                    restartPolicy="Never",
                    automountServiceAccountToken=False,
                    schedulingGates=[{"name": "tessera.invalid/never-schedule"}],
                    containers=[dict(name="probe", image="registry.k8s.io/pause:3.10")],
                ),
            ),
        )
        # JSON equality also distinguishes bool from numeric impostors.
        if json.dumps(request, sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise ValueError()
        return expected
    except Exception:
        raise SupervisedDenied("only exact maintenance quota dry-run allowed") from None


def status_result(code, value, name):
    """Only exact quota grammar may leave this boundary as a message.

    Unknown fields/messages (including user names, URLs, echoed auth) are never
    returned. Malformed and ordinary auth/RBAC denials cannot become quota proof.
    """
    if (
        not isinstance(value, dict)
        or value.get("apiVersion") != "v1"
        or value.get("kind") != "Status"
        or type(value.get("code")) is not int
        or value["code"] != code
        or value.get("status") != "Failure"
    ):
        return dict(code=code, body={}, classification="malformed_status")
    prefix = f'pods "{name}" is forbidden: exceeded quota: {NAME}, requested: pods=1, used: pods='
    message = value.get("message")
    quota = (
        code == 403
        and value.get("reason") == "Forbidden"
        and value.get("details") == dict(name=name, kind="pods")
        and isinstance(message, str)
        and re.fullmatch(re.escape(prefix) + r"[0-9]+, limited: pods=0", message) is not None
    )
    if quota:
        return dict(
            code=code,
            classification="quota_admission_rejection",
            body=dict(
                apiVersion="v1",
                kind="Status",
                code=403,
                status="Failure",
                reason="Forbidden",
                details=dict(name=name, kind="pods"),
                message=message,
            ),
        )
    category = (
        "authentication_denied"
        if code == 401
        else "authorization_denied"
        if code == 403
        else "not_quota_proof"
    )
    return dict(code=code, body={}, classification=category)


class FixedQuotaDryRunTransport:
    def __call__(self, request):
        request = fixed_request(request)  # no config/credential/client I/O before validation
        name = request["body"]["metadata"]["name"]
        deadline = time.monotonic() + 10
        try:
            with target._api_client() as (client, server):
                with client.stream(
                    "POST", server + request["path"], params=request["query"], json=request["body"]
                ) as response:
                    # Do not follow or expose redirect destinations.
                    if 300 <= response.status_code < 400:
                        return dict(
                            code=response.status_code, body={}, classification="redirect_denied"
                        )
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise SupervisedDenied("quota status encoding unsupported")
                    chunks, size = [], 0
                    for chunk in response.iter_raw():
                        size += len(chunk)
                        if size > LIMIT or time.monotonic() >= deadline:
                            raise SupervisedDenied("quota status bound exceeded")
                        chunks.append(chunk)
                    if time.monotonic() >= deadline:
                        raise SupervisedDenied("quota status bound exceeded")
                    try:
                        value = json.loads(b"".join(chunks))
                    except (ValueError, UnicodeError):
                        return dict(
                            code=response.status_code, body={}, classification="malformed_status"
                        )
                    return status_result(response.status_code, value, name)
        except Exception:
            # Never expose private config, certificate references, response or
            # exception messages. Producer retains uncertain claim; no retries.
            raise SupervisedDenied(
                "quota transport unavailable or response bound exceeded"
            ) from None
