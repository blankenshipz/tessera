"""Installable fail-closed operator setup. No inference or credential discovery."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import ssl
import stat
from pathlib import Path

import httpx

from tessera_control import runtime_binding_models as dto

from .runtime_admission import closed_object
from .runtime_binding_client import RuntimeBindingClient
from .runtime_setup_models import InstalledOwnerCapability, SetupProfile


class SetupDenied(Exception):
    """Fixed diagnostic only. Never carry native errors, tokens or response bodies."""

    def __str__(self):
        allowed = {
            "absolute_exact_reference_required",
            "symlink_reference_denied",
            "bounded_regular_file_required",
            "private_owner_reference_required",
            "reference_changed",
            "credential_reference_invalid",
            "profile_policy_scope_mismatch",
            "distinct_human_subject_publisher_required",
            "public_manifest_digest_mismatch",
            "owner_adapter_digest_mismatch",
            "installed_owner_capability_required",
            "installed_owner_scope_or_pins_mismatch",
            "fixed_reference_required",
            "explicit_idempotency_key_required",
            "authenticated_server_denied_or_reply_uncertain",
            "human_policy_and_enrollment_choices_required",
            "financial_policy_scope_mismatch",
            "unenrolled_current_resolve_reference_required",
            "resolved_binding_scope_mismatch",
        }
        code = super().__str__()
        return code if code in allowed else "setup_prerequisite_or_server_denied"


def exact_file(path, *, private=False, limit=65536):
    path = Path(path)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise SetupDenied("absolute_exact_reference_required")
    for parent in [*path.parents, path]:
        if parent.is_symlink():
            raise SetupDenied("symlink_reference_denied")
    # Refuse FIFOs/devices by descriptor without blocking before the type check.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise SetupDenied("bounded_regular_file_required")
        if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise SetupDenied("private_owner_reference_required")
        data = os.read(fd, limit + 1)
        if len(data) > limit or len(data) != info.st_size:
            raise SetupDenied("reference_changed")
        after = os.fstat(fd)
        if any(
            getattr(after, field) != getattr(info, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise SetupDenied("reference_changed")
        return data
    finally:
        os.close(fd)


def load_json(path, model):
    return model.model_validate(
        json.loads(exact_file(path, private=True), object_pairs_hook=closed_object)
    )


def write_private(path, value):
    path = Path(path)
    # Never overwrite existing approvals/credentials; refuse symlinked parents.
    for parent in [*path.parents, path]:
        if parent.is_symlink():
            raise SetupDenied("symlink_reference_denied")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(json.dumps(value, sort_keys=True, indent=2) + "\n")


def credential(path):
    token = exact_file(path, private=True, limit=8192).decode().strip()
    if not token or any(c.isspace() for c in token):
        raise SetupDenied("credential_reference_invalid")
    return token


def validate_profile(profile):
    request, policy = profile.enrollment_request, profile.policy
    if request is not None and (
        policy is None
        or request.policy_ref != policy.policy_ref
        or request.expected_policy_revision != policy.revision
        or policy.project != profile.project_id
        or request.destination != profile.destination
        or request.publisher_audience != profile.audience
    ):
        raise SetupDenied("profile_policy_scope_mismatch")
    refs = [
        profile.human_credential_file,
        profile.subject_credential_file,
        profile.publisher_credential_file,
    ]
    tokens = [credential(ref) for ref in refs]
    if len(set(refs)) != 3 or len(set(tokens)) != 3:
        raise SetupDenied("distinct_human_subject_publisher_required")
    # Validate approved transport even for preparation; no HTTP downgrade.
    client = RuntimeBindingClient(
        origin=profile.origin,
        audience=profile.audience,
        destination=profile.destination,
        credential_reference=lambda: tokens[2],
    )
    client.close()


def installed_owner(profile):
    binding = profile.owner
    manifest = exact_file(binding.signed_manifest_file, limit=4 * 1024 * 1024)
    if hashlib.sha256(manifest).hexdigest() != binding.approved_manifest_sha256:
        raise SetupDenied("public_manifest_digest_mismatch")
    code = exact_file(binding.adapter_file, limit=1024 * 1024)
    if hashlib.sha256(code).hexdigest() != binding.adapter_sha256:
        raise SetupDenied("owner_adapter_digest_mismatch")
    exact_file(binding.resource_reference_file, private=True)
    # The separately approved executable adapter is trusted code, not model input.
    spec = importlib.util.spec_from_file_location(
        "tessera_explicit_setup_owner", binding.adapter_file
    )
    module = importlib.util.module_from_spec(spec)
    # Execute the SAME verified bytes, avoiding a second unverified path read.
    exec(compile(code, binding.adapter_file, "exec"), module.__dict__)
    capability = module.open_installed_owner(
        binding,
        credential_references=(
            profile.human_credential_file,
            profile.subject_credential_file,
            profile.publisher_credential_file,
        ),
    )
    if type(capability) is not InstalledOwnerCapability or not callable(capability.assert_current):
        raise SetupDenied("installed_owner_capability_required")
    capability.assert_current()
    measured, request = capability.readback, profile.enrollment_request
    if (
        measured.manifest_sha256 != binding.approved_manifest_sha256
        or measured.approval_reference != binding.approval_reference
        or (
            request is not None
            and (
                measured.pins != request.pins
                or measured.publisher_ref != request.publisher_ref
                or measured.opaque_owner_ref != request.opaque_owner_ref
                or measured.session_ref != request.session_ref
                or measured.subject_credential_id != request.subject_credential_id
                or measured.publisher_credential_id != request.publisher_credential_id
            )
        )
    ):
        raise SetupDenied("installed_owner_scope_or_pins_mismatch")
    return capability


def segment(value):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        raise SetupDenied("fixed_reference_required")
    return value


def check_scope(profile, scope, pins):
    expected = profile.enrollment_request
    fields = (
        "publisher_ref",
        "opaque_owner_ref",
        "session_ref",
        "destination",
        "role",
        "context_ref",
        "context_digest",
        "policy_ref",
    )
    if (
        expected is None
        or profile.policy is None
        or pins != expected.pins
        or scope.project != profile.project_id
        or scope.credential != expected.subject_credential_id
        or scope.policy_digest != profile.policy.admission_policy_digest
        or any(getattr(scope, name) != getattr(expected, name) for name in fields)
    ):
        raise SetupDenied("resolved_binding_scope_mismatch")


def request(http, profile, method, route, model, *, payload=None, key=None, human=False):
    if method == "POST" and (not key or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key)):
        raise SetupDenied("explicit_idempotency_key_required")
    token = credential(
        profile.human_credential_file if human else profile.publisher_credential_file
    )
    prefix = "" if human or profile.audience == "control" else "/worker"
    headers = {"Authorization": "Bearer " + token, "X-Agent-Destination": profile.destination}
    if key:
        headers["Idempotency-Key"] = key
    response = http.request(
        method,
        profile.origin.rstrip("/") + prefix + route,
        headers=headers,
        json=payload.model_dump(mode="json") if payload else None,
        follow_redirects=False,
    )
    if response.status_code not in {200, 201} or len(response.content) > 65536:
        raise SetupDenied("authenticated_server_denied_or_reply_uncertain")
    result = json.loads(response.content, object_pairs_hook=closed_object)
    return model.model_validate(result) if model else result


def run(args, *, http=None):
    profile = load_json(args.profile, SetupProfile)
    validate_profile(profile)
    capability = installed_owner(profile)
    if args.command == "prepare-profile":
        if profile.enrollment_request is None:
            raise SetupDenied("human_policy_and_enrollment_choices_required")
        write_private(args.output, profile.model_dump(mode="json"))
        return {"status": "prepared-not-enrolled", "provider_entitlement": "unknown"}
    own_http = http is None
    if own_http:
        exact_file(profile.ca_file, limit=1024 * 1024)
        http = httpx.Client(
            verify=ssl.create_default_context(cafile=profile.ca_file),
            timeout=10,
            follow_redirects=False,
        )
    try:
        capability.assert_current()
        if args.command == "enroll":
            if profile.enrollment_request is None:
                raise SetupDenied("human_policy_and_enrollment_choices_required")
            result = request(
                http,
                profile,
                "POST",
                f"/projects/{segment(profile.project_id)}/runtime-enrollments",
                dto.RuntimeEnrollmentRecord,
                payload=profile.enrollment_request,
                key=args.key,
                human=True,
            )
            check_scope(profile, result.scope, result.pins)
            if (
                result.publisher_credential_id != profile.enrollment_request.publisher_credential_id
                or result.publisher_audience != profile.audience
            ):
                raise SetupDenied("resolved_binding_scope_mismatch")
        elif args.command == "issue-policy":
            payload = load_json(args.input, dto.IssueRuntimePolicy)
            result = request(
                http,
                profile,
                "POST",
                f"/projects/{segment(profile.project_id)}/runtime-policies",
                dto.RuntimePolicyRecord,
                payload=payload,
                key=args.key,
                human=True,
            )
        elif args.command == "operator-financial-ref":
            payload = load_json(args.input, dto.IssueRuntimeFinancialAssertion)
            if profile.policy is None or payload.policy_ref != profile.policy.policy_ref:
                raise SetupDenied("financial_policy_scope_mismatch")
            result = request(
                http,
                profile,
                "POST",
                f"/projects/{segment(profile.project_id)}/runtime-financial-assertions",
                dto.RuntimeFinancialRecord,
                payload=payload,
                key=args.key,
                human=True,
            )
        elif args.command == "revoke":
            payload = dto.RevokeRuntimeRecord(expected_revision=args.revision)
            route = {
                "policy": "runtime-policies",
                "enrollment": "runtime-enrollments",
                "financial": "runtime-financial-assertions",
            }[args.kind]
            result = request(
                http,
                profile,
                "POST",
                f"/{route}/{segment(args.ref)}/revoke",
                None,
                payload=payload,
                key=args.key,
                human=True,
            )
        elif args.command == "reconcile":
            result = request(
                http,
                profile,
                "GET",
                f"/runtime-operations/{segment(args.ref)}",
                dto.RuntimeOperation,
            )
            return {
                "status": "readback-only-not-continuation",
                "state": result.state,
                "operation_ref": result.operation_ref,
                "next_action": "human-review-no-retry-or-refund",
            }
        else:
            if (
                profile.resolve_request is None
                or profile.policy is None
                or profile.enrollment_request is None
            ):
                raise SetupDenied("unenrolled_current_resolve_reference_required")
            result = request(
                http,
                profile,
                "POST",
                "/runtime-bindings/resolve",
                dto.RuntimeBindingSnapshot,
                payload=profile.resolve_request,
                key=args.key,
            )
            check_scope(profile, result.scope, result.pins)
            if (
                result.scope.enrollment != profile.resolve_request.enrollment_ref
                or result.financial_ref != profile.resolve_request.financial_ref
            ):
                raise SetupDenied("resolved_binding_scope_mismatch")
        capability.assert_current()
        # Public DTOs only, never credential values, private resource data or upstream errors.
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    finally:
        if own_http:
            http.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="Exact private public-reference profile")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare-profile").add_argument("--output", required=True)
    for name in ("doctor", "current-resolve", "enroll", "issue-policy", "operator-financial-ref"):
        command = sub.add_parser(name)
        command.add_argument("--key", required=True)
        if name in {"issue-policy", "operator-financial-ref"}:
            command.add_argument("--input", required=True)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--kind", choices=("policy", "enrollment", "financial"), required=True)
    revoke.add_argument("--ref", required=True)
    revoke.add_argument("--revision", type=int, required=True)
    revoke.add_argument("--key", required=True)
    sub.add_parser("reconcile").add_argument("--ref", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except Exception as error:
        code = (
            str(error) if isinstance(error, SetupDenied) else "setup_prerequisite_or_server_denied"
        )
        print(
            json.dumps(
                {
                    "status": "denied",
                    "code": code,
                    "next_action": "supply-approved-owner-link-and-exact-credential-references",
                }
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
