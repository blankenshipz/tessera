"""Fixed-origin management wire transport; authority remains the caller's responsibility.

Schema: https://docs.salad.com/api-specs/salad-cloud.yaml (0.9.0-alpha.17).
No retry, redirect, environment proxy, logging, or endpoint override. Observations
are provider claims, never independent termination/no-writers evidence. The caller
owns SecretBuffer lifetime. Python/http.client necessarily copy the header value.
"""

import http.client
import json
import re
import socket
import threading
import time
from decimal import Decimal
from uuid import UUID

from .remote_execution_models import ExecutionSelection
from .salad_proxy_contract import ProxyResult
from .salad_resources import check_salad_resources
from .salad_secrets import SecretBuffer
from .store import canonical_digest, utc_now

HOST = "api.salad.com"
PREFIX = "/api/public"
LIMIT = 65536
# Total client deadline per request, covering DNS, connect, TLS, send and the whole
# response. Reads stay short. The create POST gets longer: live, Salad accepted (and
# later created) a create that our former 2 s deadline had already abandoned. The
# guardian's dispatch-chain bound includes this value (salad_safety_service).
READ_DEADLINE_SECONDS = 2
CREATE_DEADLINE_SECONDS = 12
# Keys that mean a group-list page may be one of several: such a page proves nothing.
PAGINATION_HINTS = ("next", "page", "continu", "cursor", "token", "offset", "more", "link")


def _group_not_found(data):
    """A ProblemDetails-shaped 404 body for the exact-name read, nothing looser.

    Observed live (read-only GET), a missing group in an existing project and a
    missing project return the identical body ``{"type": ".../rfc9110#section-
    15.5.5", "title": "Not Found", "status": 404, "traceId": "..."}`` with no
    ``detail``. So the body cannot tell a missing group from a missing project and
    ``detail`` is neither required nor interpreted: project existence is proven
    only by the separate project group-list read. HTML, empty, non-JSON and
    non-object bodies, or a conflicting ``status``/``title``, stay unknown.
    """
    if type(data) is not dict:
        return False
    if "status" in data and (type(data["status"]) is not int or data["status"] != 404):
        return False
    return "title" not in data or data["title"] == "Not Found"


# A clean client rejection of a create: the request was fully answered with one of
# these statuses and a ProblemDetails object. 401/403/404/409/429/5xx stay unknown.
REJECTION_STATUSES = (400, 422)
SUMMARY_CHARS = 200
SUMMARY_FIELDS = 16


def _summary_text(value):
    """Printable, single-line, bounded provider text, or None when absent/not text."""
    if type(value) is not str:
        return None
    text = "".join(c if c.isprintable() else " " for c in value).strip()
    return text[:SUMMARY_CHARS] or None


def _json_content_type(value):
    """``application/problem+json`` or ``application/json``, parameters allowed."""
    if type(value) is not str:
        return False
    media = value.split(";", 1)[0].strip().lower()
    return media in {"application/problem+json", "application/json"}


def _problem_rejection(data, status):
    """Bounded non-secret summary of a ProblemDetails 400/422 body, else None.

    Shape (RFC 9457 as Salad serves it; observed live on 400s and the 404): a JSON
    object with a non-empty string ``type`` (a URL or a short code such as
    ``invalid_query_syntax``), a string ``title`` and an integer ``status`` equal
    to the HTTP status; ``detail``, ``instance`` and ``traceId`` strings when
    present; ``errors`` an object (ASP.NET validation: field -> list of messages)
    when present. ``errors`` keys (field paths) are kept, never values. The
    summary is diagnostics only, never absence evidence.
    """
    if type(data) is not dict:
        return None
    if type(data.get("status")) is not int or data["status"] != status:
        return None
    if type(data.get("type")) is not str or not data["type"] or type(data.get("title")) is not str:
        return None
    if any(k in data and type(data[k]) is not str for k in ("detail", "instance", "traceId")):
        return None
    errors = data.get("errors")
    if errors is not None and type(errors) is not dict:
        return None
    fields = sorted(text for text in map(_summary_text, errors or ()) if text)[:SUMMARY_FIELDS]
    return dict(
        status=status,
        title=_summary_text(data.get("title")),
        detail=_summary_text(data.get("detail")),
        error_fields=[f[:128] for f in fields],
    )


def _resolve(host, port, timeout):
    """getaddrinfo bounded by ``timeout`` (a stuck resolver thread is abandoned)."""
    found = {}

    def resolve():
        try:
            found["addresses"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception:
            found["addresses"] = None

    thread = threading.Thread(target=resolve, daemon=True)
    thread.start()
    thread.join(timeout)
    if not found.get("addresses"):
        raise TimeoutError("resolve_deadline")
    return found["addresses"][0][4][:2]


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class BoundedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS with a real total deadline: ``timeout`` seconds from construction.

    DNS runs in a bounded thread and connect gets only the remaining time; after
    connect a watchdog shuts the socket down at the deadline, so TLS, send and
    every response read end by then too. Certificate and hostname checks are the
    default HTTPS context's, unchanged.
    """

    def __init__(self, host, *, port, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self._deadline = time.monotonic() + timeout
        self._watchdog = None

    def _left(self):
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("connection_deadline")
        return left

    def connect(self):
        address = _resolve(self.host, self.port, self._left())
        sock = socket.create_connection(address, self._left())
        try:
            self._watchdog = threading.Timer(self._left(), _shutdown, (sock,))
            self._watchdog.daemon = True
            self._watchdog.start()
            sock.settimeout(self._left())
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise

    def close(self):
        if self._watchdog is not None:
            self._watchdog.cancel()
        super().close()


# Per-request deadline the guardian's provider-read retry budget is sized against
# (every read; a create gets CREATE_DEADLINE_SECONDS).
REQUEST_TIMEOUT_SECONDS = READ_DEADLINE_SECONDS


class _Failure(Exception):
    def __init__(self, reason):
        self.reason = reason


class _Rejected(_Failure):
    """A clean provider rejection of a create, with its bounded summary."""

    def __init__(self, summary, reason="provider_rejected_request"):
        super().__init__(reason)
        self.summary = summary


# A create POST that provably never left this process (a local error, DNS or connect
# failure before any request bytes were written) cannot create a group later, unlike an
# uncertain reply. It is reported like a clean rejection (status 0: no HTTP exchange) so
# the guardian may release the reservation after its own absence reads, instead of
# holding the single-operation fence until the route deadline plus the lost-create margin.
NOT_SENT_SUMMARY = dict(
    status=0,
    title="The create request was not sent",
    detail=None,
    error_fields=[],
)


def _not_sent():
    return _Rejected(dict(NOT_SENT_SUMMARY), reason="request_not_sent")


def _uuid(value):
    if type(value) is not str or not re.fullmatch(
        r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", value
    ):
        raise _Failure("malformed_response")
    return UUID(value).hex


def _integer(value, *, maximum=2**31 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise _Failure("malformed_response")
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


class SaladHTTPTransport:
    """Pin an immutable server selection; factory injection is for isolated tests only.

    preflight(selection, secret) and send(operation, selection, provider_id, secret)
    return ProxyResult. IDs are the actual UUID encoded as 32 lowercase hex digits,
    not Tessera operation IDs. read_group with None is lost-create reconciliation.
    read_instance reads the exact group's instance collection, not arbitrary IDs.
    """

    def __init__(self, selection, *, connection_factory=BoundedHTTPSConnection):
        self._selection = ExecutionSelection.model_validate_json(selection.model_dump_json())
        s = self._selection
        p, r = s.salad, s.resources
        if s.target != "salad_container" or p is None or p.execution is None:
            raise ValueError("exact Salad executable selection required")
        if not all(
            re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]", x)
            for x in (p.organization, p.project, p.group)
        ) or not re.fullmatch(r"attempt-[a-f0-9]{32}", p.group):
            raise ValueError("reserved Salad names required")
        if r.cpu_millicores % 1000 or r.memory_mb < 1024 or r.disk_mb < 1024:
            raise ValueError("Salad resources cannot represent this selection exactly")
        if r.gpu_count != 1:
            raise ValueError("single GPU selection required")
        try:
            _uuid(r.gpu_class)
        except _Failure:
            raise ValueError("exact Salad GPU class UUID required") from None
        self._factory = connection_factory
        self._digest = canonical_digest(s.model_dump(mode="json"))
        self._org = f"{PREFIX}/organizations/{p.organization}"
        self._groups = f"{self._org}/projects/{p.project}/containers"
        self._group = f"{self._groups}/{p.group}"
        self._resources = dict(
            cpu=r.cpu_millicores // 1000,
            memory=r.memory_mb,
            storage_amount=r.disk_mb * 1024 * 1024,
            gpu_classes=[r.gpu_class],
        )
        self._spec = dict(
            name=p.group,
            autostart_policy=False,
            replicas=1,
            restart_policy="never",
            country_codes=[p.country.lower()],
            **{"scheduled-scaling-enabled": False, "scaling-actions": []},
            container=dict(
                image=s.manifests.image,
                command=[p.execution.command, *p.execution.argv],
                environment_variables=dict(p.execution.env),
                resources=self._resources,
                priority="high",
            ),
        )

    def _check(self, selection, secret):
        if selection.model_dump(mode="json") != self._selection.model_dump(mode="json"):
            raise _Failure("identity_mismatch")
        if type(secret) is not SecretBuffer or not secret._buffer:
            raise _Failure("credential_unavailable")
        if not all(33 <= b <= 126 for b in secret._buffer):
            raise _Failure("credential_unavailable")

    def _request(
        self,
        method,
        path,
        secret,
        body=None,
        *,
        absent_ok=False,
        rejection_ok=False,
        total=READ_DEADLINE_SECONDS,
    ):
        """One bounded request. Only with ``absent_ok`` is a fully read (Content-Length
        matched when sent), in-deadline HTTP 404 returned, as ``(404, parsed JSON
        object)``; every other non-2xx, and any other 404, stays a failure.

        Only with ``rejection_ok`` (the create POST) does a fully read, in-deadline
        HTTP 400/422 whose body is a ProblemDetails object raise ``_Rejected`` with a
        bounded summary; any other body for those statuses, and every other non-2xx,
        stays ``provider_unavailable``/``malformed_response``."""
        connection = None
        try:
            # One deadline across request/headers/body. No inherited HTTP proxy.
            # ``total`` is READ_DEADLINE_SECONDS, or CREATE_DEADLINE_SECONDS for create.
            deadline = time.monotonic() + total
            connection = self._factory(HOST, port=443, timeout=total)
            headers = {
                "Salad-Api-Key": secret._buffer.decode("ascii"),
                "Accept": "application/json",
            }
            payload = None if body is None else json.dumps(body, allow_nan=False).encode()
            if payload is not None:
                headers["Content-Type"] = "application/json"
                if secret.contains_in(payload):
                    raise _Failure("credential_unavailable")
            # Connect first: a DNS or connect failure means no request bytes were sent.
            connect = getattr(connection, "connect", None)
            if callable(connect):
                connect()
            if method == "POST":
                self._post_sent = True
            connection.request(method, path, body=payload, headers=headers)
            self._remaining(connection, deadline)
            response = connection.getresponse()
            status = response.status
            getheader = getattr(response, "getheader", None)
            declared = getheader("Content-Length") if callable(getheader) else None
            content_type = getheader("Content-Type") if callable(getheader) else None
            self._remaining(connection, deadline)
            raw = bytearray()
            while len(raw) <= LIMIT:
                self._remaining(connection, deadline)
                chunk = response.read1(min(4096, LIMIT + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > LIMIT or secret.contains_in(raw):
                raise _Failure("malformed_response")
            if 300 <= status < 400:
                raise _Failure("redirect_denied")
            if rejection_ok and status in REJECTION_STATUSES:
                if not _json_content_type(content_type):
                    raise _Failure("provider_unavailable")  # e.g. a gateway HTML page
                if declared is not None and (
                    not str(declared).isdigit() or int(declared) != len(raw)
                ):
                    raise _Failure("provider_unavailable")  # short or overlong body
                try:
                    data = json.loads(
                        raw,
                        object_pairs_hook=_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                    )
                except (ValueError, UnicodeError):
                    raise _Failure("provider_unavailable") from None
                summary = _problem_rejection(data, status)
                if summary is None or secret.contains_in(
                    json.dumps(summary, sort_keys=True).encode()
                ):
                    raise _Failure("provider_unavailable")
                raise _Rejected(summary)
            if absent_ok and status == 404:
                if declared is not None and (
                    not str(declared).isdigit() or int(declared) != len(raw)
                ):
                    raise _Failure("malformed_response")  # short or overlong body
                data = json.loads(
                    raw,
                    object_pairs_hook=_object,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                )
                if type(data) is not dict:
                    raise _Failure("malformed_response")
                return status, data
            if status == 202:
                return status, None
            if status not in {200, 201}:
                raise _Failure("provider_unavailable")
            data = json.loads(
                raw,
                object_pairs_hook=_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
            if type(data) is not dict:
                raise _Failure("malformed_response")
            return status, data
        except _Failure:
            raise
        except (ValueError, UnicodeError):
            raise _Failure("malformed_response") from None
        except Exception:
            raise _Failure("provider_unavailable") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    @staticmethod
    def _remaining(connection, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Failure("provider_unavailable")
        if connection.sock is not None:
            connection.sock.settimeout(remaining)

    def _group_identity(self, data, provider_id):
        p = self._selection.salad
        actual = _uuid(data["id"])
        if provider_id is not None and actual != provider_id:
            raise _Failure("identity_mismatch")
        if (
            data["name"] != p.group
            or data["organization_name"] != p.organization
            or data["project_name"] != p.project
        ):
            raise _Failure("identity_mismatch")
        return actual

    def _group_observation(self, data, provider_id):
        p = self._selection.salad
        actual = self._group_identity(data, provider_id)
        expected = self._spec
        for key in (
            "name",
            "autostart_policy",
            "replicas",
            "restart_policy",
            "country_codes",
            "scheduled-scaling-enabled",
            "scaling-actions",
        ):
            if type(data[key]) is not type(expected[key]) or data[key] != expected[key]:
                raise _Failure("identity_mismatch")
        if data["organization_name"] != p.organization or data["project_name"] != p.project:
            raise _Failure("identity_mismatch")
        container = data["container"]
        for key in ("image", "command", "environment_variables"):
            # A missing field cannot establish the reviewed executable profile,
            # including the case where the approved environment is empty.
            if key not in container or container[key] != expected["container"][key]:
                raise _Failure("identity_mismatch")
        if data["priority"] != "high" or type(data["pending_change"]) is not bool:
            raise _Failure("identity_mismatch")
        for key, value in self._resources.items():
            found = container["resources"].get(key)
            if type(found) is not type(value) or found != value:
                raise _Failure("identity_mismatch")
        if any(
            data.get(k) is not None
            for k in (
                "networking",
                "queue_connection",
                "queue_autoscaler",
                "startup_probe",
                "readiness_probe",
                "liveness_probe",
            )
        ):
            raise _Failure("identity_mismatch")
        if (
            container.get("logging") is not None
            or container.get("registry_authentication") is not None
        ):
            raise _Failure("identity_mismatch")
        state = data["current_state"]["status"]
        if state not in {"pending", "running", "stopped", "succeeded", "failed", "deploying"}:
            raise _Failure("malformed_response")
        counts = data["current_state"]["instance_status_counts"]
        counts = {
            k: _integer(counts[k])
            for k in ("allocating_count", "creating_count", "running_count", "stopping_count")
        }
        return dict(
            provider_id=actual,
            state=state,
            instance_status_counts=counts,
            pending_change=data["pending_change"],
            version=_integer(data["version"]),
        )

    def _result(self, observation, *, pending=False, observed_at=None):
        return ProxyResult(
            status="pending" if pending else "observed",
            reason="accepted_pending" if pending else "observed",
            observation=dict(
                observation, selection_digest=self._digest, observed_at=observed_at or utc_now()
            ),
        )

    def _failed(self, error):
        if isinstance(error, _Rejected):
            # Still "unknown": a rejection is the provider's claim. Only the guardian's
            # own exact-name and project-list reads may release the reservation.
            return ProxyResult(
                status="unknown",
                reason=error.reason,
                observation=dict(
                    provider_rejection=error.summary,
                    selection_digest=self._digest,
                    observed_at=utc_now(),
                ),
            )
        return ProxyResult(status="unknown", reason=error.reason)

    @staticmethod
    def _require_stopped(group):
        # This is provider-side admission, not independent no-writers evidence.
        if (
            group["state"] != "stopped"
            or group["pending_change"] is not False
            or any(group["instance_status_counts"].values())
        ):
            raise _Failure("admission_denied")

    def _read_gpu_class(self, secret):
        status, data = self._request("GET", f"{self._org}/gpu-classes", secret)
        if status != 200:
            raise _Failure("malformed_response")
        items = data["items"]
        if type(items) is not list or len(items) > 100:
            raise _Failure("malformed_response")
        selected_id = self._selection.resources.gpu_class
        matches = [item for item in items if _uuid(item["id"]) == _uuid(selected_id)]
        if len(matches) != 1:
            raise _Failure("identity_mismatch")
        selected = matches[0]
        if "gpu_count" in selected and _integer(selected["gpu_count"]) != 1:
            raise _Failure("admission_denied")
        # Optional in the provider schema. The public
        # gpu-classes response publishes NO bounds for any class; then there is no
        # local compatibility proof and Salad's own create validation decides. A
        # partial set is inconsistent and stays unknown; a full set is enforced.
        # RAM is MB; storage is bytes, not MB; CPU is an integer core count.
        kinds = (("vcpu", "cpu"), ("ram", "memory"), ("storage", "storage_amount"))
        bound_fields = [f"{edge}_{kind}" for kind, _ in kinds for edge in ("min", "max")]
        published = [field in selected for field in bound_fields]
        if any(published) and not all(published):
            raise _Failure("malformed_response")
        for kind, resource in kinds if all(published) else ():
            maximum = 2**63 - 1 if kind == "storage" else 2**31 - 1
            low = _integer(selected[f"min_{kind}"], maximum=maximum)
            high = _integer(selected[f"max_{kind}"], maximum=maximum)
            if low > high:
                raise _Failure("malformed_response")
            if not low <= self._resources[resource] <= high:
                raise _Failure("admission_denied")
        prices = selected["prices"]
        if type(prices) is not list or not 1 <= len(prices) <= 100:
            raise _Failure("malformed_response")
        high_prices = [price for price in prices if price["priority"] == "high"]
        if len(high_prices) != 1:
            raise _Failure("malformed_response")
        raw_price = high_prices[0]["price"]
        if (
            type(raw_price) is not str
            or len(raw_price) > 20
            or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", raw_price)
        ):
            raise _Failure("malformed_response")
        price = Decimal(raw_price)
        if not price.is_finite() or price <= 0:
            raise _Failure("malformed_response")
        # Published GPU USD/hour includes CPU/RAM ("GPU rates include vCPUs and
        # RAM", docs.salad.com billing). Storage billing is undocumented and
        # unmodelled. Only running seconds are billed (allocation/download are
        # free); this read is a rate/estimate input, never invoice or cost proof.
        return dict(
            gpu_class_id=selected_id,
            hourly_price_usd=format(price, "f"),
            price_basis="published_gpu_hourly_rate_not_actual_cost",
            resource_bounds="enforced" if all(published) else "not_published",
        )

    def gpu_class(self, selection, secret):
        """One bounded read of current class compatibility and published high rate.

        No caller/env price input, cache, or implicit retry. A guardian may use this
        rate for conservative exposure estimates, not actual invoiced usage.
        """
        try:
            self._check(selection, secret)
            observed_at = utc_now()
            return self._result(self._read_gpu_class(secret), observed_at=observed_at)
        except _Failure as error:
            return self._failed(error)
        except Exception:
            return self._failed(_Failure("malformed_response"))

    def preflight(self, selection, secret):
        """Four requests, each with a two-second timeout, not a two-second total.

        Stamp the earliest read. The caller must enforce admission's five-second
        freshness window; a slow preflight can legitimately expire before admission.
        """
        try:
            self._check(selection, secret)
            observed_at = utc_now()
            _, groups = self._request("GET", self._groups, secret)
            items = groups["items"]
            if type(items) is not list or len(items) >= 100:
                raise _Failure("malformed_response")  # No proof of absence on a full page.
            matching = [g for g in items if g["name"] == selection.salad.group]
            if len(matching) > 1:
                raise _Failure("identity_mismatch")
            group = self._group_observation(matching[0], None) if matching else {}
            _, quotas = self._request("GET", f"{self._org}/quotas", secret)
            quota = quotas["container_groups_quotas"]
            maximum = _integer(quota["container_replicas_quota"])
            used = _integer(quota["container_replicas_used"])
            gpu = self._read_gpu_class(secret)
            _, availability = self._request(
                "POST",
                f"{self._org}/availability/sce-gpu-availability",
                secret,
                dict(self._resources, country_codes=self._spec["country_codes"]),
            )
            count = _integer(availability["available_gpu_high"])
            return self._result(
                dict(
                    group,
                    **gpu,
                    project_exists=True,
                    group_absent=not matching,
                    # Stopped groups do NOT count toward
                    # container_replicas_used, so starting one needs room for a
                    # replica. If the exact group is already counted, no increment.
                    quota_ok=used < maximum or (bool(matching) and 1 <= used <= maximum),
                    available=count > 0,
                ),
                observed_at=observed_at,
            )
        except _Failure as error:
            return self._failed(error)
        except Exception:
            return self._failed(_Failure("malformed_response"))

    def inspect_stop(self, selection, provider_id, secret):
        """Server-only stop preflight: exact scope/name/bound UUID, no drift gate.

        This is an identity observation, NOT stopped-state or execution readiness.
        It is deliberately not a public proxy operation or a relaxed read_group.
        """
        try:
            self._check(selection, secret)
            if not isinstance(provider_id, str) or not re.fullmatch(r"[a-f0-9]{32}", provider_id):
                raise _Failure("identity_mismatch")
            _, data = self._request("GET", self._group, secret)
            return self._result(
                dict(provider_id=self._group_identity(data, provider_id), state="unknown")
            )
        except _Failure as error:
            return self._failed(error)
        except Exception:
            return self._failed(_Failure("malformed_response"))

    def read_absence(self, selection, secret):
        """Server-only lost-create evidence for the exact reserved name. Not a proxy op.

        ``group_not_found`` is True only for an authenticated, fully read HTTP 404 on
        the exact reserved group path with a ProblemDetails-shaped JSON body (see
        ``_group_not_found``). That 404 alone does not prove the project exists: only
        the list read below does. A transport error, timeout, redirect, 401/403, 5xx
        or a non-ProblemDetails 404 body is a failure (status "unknown"), never
        absence. A 200/202 on the exact name is ``group_not_found=False``.
        Only after a definite 404 the project's group list is read; it is the only
        proof that the project (and so the meaning of the 404) exists.
        ``list_read`` is "absent" for a complete page (fewer than 100
        well-formed items, no pagination keys) without the name, "absent_partial"
        for a reached, well-formed page of 100+ items without it, "present" when any
        item has the name, and "unavailable" when the read failed, was malformed or
        carries pagination keys (unless it already shows the name).
        """
        try:
            self._check(selection, secret)
            status, data = self._request("GET", self._group, secret, absent_ok=True)
            if status != 404:
                return self._result(dict(group_not_found=False, list_read="present"))
            if not _group_not_found(data):
                raise _Failure("malformed_response")
            list_read = "unavailable"
            try:
                _, groups = self._request("GET", self._groups, secret)
                items = groups.get("items")
                if type(items) is list and all(
                    type(g) is dict and type(g.get("name")) is str for g in items
                ):
                    paged = any(hint in key.lower() for key in groups for hint in PAGINATION_HINTS)
                    if any(g["name"] == selection.salad.group for g in items):
                        list_read = "present"
                    elif not paged:
                        list_read = "absent" if len(items) < 100 else "absent_partial"
            except _Failure:
                list_read = "unavailable"
            return self._result(dict(group_not_found=True, list_read=list_read))
        except _Failure as error:
            return self._failed(error)
        except Exception:
            return self._failed(_Failure("malformed_response"))

    def send(self, operation, selection, provider_id, secret):
        self._post_sent = None  # None: no create POST attempted in this call
        try:
            self._check(selection, secret)
            if provider_id is not None and not re.fullmatch(r"[a-f0-9]{32}", provider_id):
                raise _Failure("identity_mismatch")
            if operation == "create_stopped":
                if provider_id is not None:
                    raise _Failure("identity_mismatch")
                try:
                    # Refuse a shape Salad's documented limits reject before any POST.
                    check_salad_resources(self._selection.resources)
                except ValueError:
                    raise _Failure("admission_denied") from None
                self._post_sent = False  # _request sets True only right before sending
                status, data = self._request(
                    "POST",
                    self._groups,
                    secret,
                    self._spec,
                    rejection_ok=True,
                    total=CREATE_DEADLINE_SECONDS,
                )
                if status == 202:
                    return self._result(dict(state="unknown", response_status=202), pending=True)
                if status != 201:
                    raise _Failure("malformed_response")
                group = self._group_observation(data, None)
                if (
                    group["state"] == "pending"
                    and group["pending_change"] is True
                    and not any(group["instance_status_counts"].values())
                ):
                    # A new autostart=false group answers
                    # 201 as pending with a change in flight before settling stopped.
                    # Keep its verified identity but report unknown/pending; a later
                    # exact read must observe stopped before start is admitted.
                    return self._result(
                        dict(group, state="unknown", response_status=201), pending=True
                    )
                self._require_stopped(group)
                return self._result(group)
            if operation not in {"read_group", "read_instance", "start", "stop", "delete"}:
                raise _Failure("admission_denied")
            if operation != "read_group" and provider_id is None:
                raise _Failure("identity_mismatch")
            _, data = self._request("GET", self._group, secret)
            # Stop only the bound tenant/project/name/UUID even if mutable
            # workload configuration drifted. Such drift still fences every
            # execution/read/delete path; a stop acknowledgement stays pending.
            group = (
                {"provider_id": self._group_identity(data, provider_id)}
                if operation == "stop"
                else self._group_observation(data, provider_id)
            )
            if operation == "read_group":
                return self._result(group)
            if operation == "read_instance":
                _, data = self._request("GET", f"{self._group}/instances", secret)
                instances = data["instances"]
                if type(instances) is not list or len(instances) > 1:
                    raise _Failure("reallocation_uncertain")
                normalized = []
                for instance in instances:
                    state = instance["state"]
                    if state not in {
                        "allocating",
                        "downloading",
                        "creating",
                        "running",
                        "stopping",
                    }:
                        raise _Failure("malformed_response")
                    normalized.append(
                        dict(
                            instance_id=_uuid(instance["id"]),
                            machine_id=_uuid(instance["machine_id"]),
                            state=state,
                            version=_integer(instance["version"]),
                        )
                    )
                return self._result(dict(group, instances=normalized))
            if operation == "start":
                # Recheck the immediately preceding exact GET, not stale preflight.
                # Caller has consumed its claim; failure never resets or retries it.
                self._require_stopped(group)
            path = self._group if operation == "delete" else f"{self._group}/{operation}"
            method = "DELETE" if operation == "delete" else "POST"
            status, _ = self._request(method, path, secret)
            if status != 202:
                raise _Failure("malformed_response")
            return self._result(
                dict(provider_id=group["provider_id"], state="unknown", response_status=202),
                pending=True,
            )
        except _Failure as error:
            if self._post_sent is False and not isinstance(error, _Rejected):
                return self._failed(_not_sent())  # nothing reached the provider
            return self._failed(error)
        except Exception:
            if self._post_sent is False:
                return self._failed(_not_sent())
            return self._failed(_Failure("malformed_response"))
