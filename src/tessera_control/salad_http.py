"""Fixed-origin management wire transport; authority remains the caller's responsibility.

Schema: https://docs.salad.com/api-specs/salad-cloud.yaml (0.9.0-alpha.17).
No retry, redirect, environment proxy, logging, or endpoint override. Observations
are provider claims, never independent termination/no-writers evidence. The caller
owns SecretBuffer lifetime. Python/http.client necessarily copy the header value.
"""

import http.client
import json
import re
import time
from decimal import Decimal
from uuid import UUID

from .remote_execution_models import ExecutionSelection
from .salad_proxy_contract import ProxyResult
from .salad_secrets import SecretBuffer
from .store import canonical_digest, utc_now

HOST = "api.salad.com"
PREFIX = "/api/public"
LIMIT = 65536


class _Failure(Exception):
    def __init__(self, reason):
        self.reason = reason


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

    def __init__(self, selection, *, connection_factory=http.client.HTTPSConnection):
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

    def _request(self, method, path, secret, body=None):
        connection = None
        try:
            # One deadline across request/headers/body. No inherited HTTP proxy.
            deadline = time.monotonic() + 2
            connection = self._factory(HOST, port=443, timeout=2)
            headers = {
                "Salad-Api-Key": secret._buffer.decode("ascii"),
                "Accept": "application/json",
            }
            payload = None if body is None else json.dumps(body, allow_nan=False).encode()
            if payload is not None:
                headers["Content-Type"] = "application/json"
                if secret.contains_in(payload):
                    raise _Failure("credential_unavailable")
            connection.request(method, path, body=payload, headers=headers)
            self._remaining(connection, deadline)
            response = connection.getresponse()
            status = response.status
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

    @staticmethod
    def _failed(error):
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
        # Optional in the provider schema is not known/enforceable compatibility.
        # RAM is MB; storage is bytes, not MB; CPU is an integer core count.
        for kind, resource in (("vcpu", "cpu"), ("ram", "memory"), ("storage", "storage_amount")):
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
        # Published GPU USD/hour includes CPU/RAM. Only running seconds are
        # billed; this read is a rate/estimate input, never invoice or cost proof.
        return dict(
            gpu_class_id=selected_id,
            hourly_price_usd=format(price, "f"),
            price_basis="published_gpu_hourly_rate_not_actual_cost",
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
                    # Stopped replicas already count against organization quota.
                    # No capacity increment for the exact existing single group.
                    quota_ok=(1 <= used <= maximum) if matching else used < maximum,
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

    def send(self, operation, selection, provider_id, secret):
        try:
            self._check(selection, secret)
            if provider_id is not None and not re.fullmatch(r"[a-f0-9]{32}", provider_id):
                raise _Failure("identity_mismatch")
            if operation == "create_stopped":
                if provider_id is not None:
                    raise _Failure("identity_mismatch")
                status, data = self._request("POST", self._groups, secret, self._spec)
                if status == 202:
                    return self._result(dict(state="unknown", response_status=202), pending=True)
                if status != 201:
                    raise _Failure("malformed_response")
                group = self._group_observation(data, None)
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
            return self._failed(error)
        except Exception:
            return self._failed(_Failure("malformed_response"))
